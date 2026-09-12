# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools>=4.55", "uharfbuzz>=0.40"]
# ///
"""Generate the redis-lua-py brand assets: README banners, logo, logomark, social preview.

    uv run .github/assets/generate.py

Geist and Geist Mono (OFL) are fetched from the vercel/geist-font release on first run
and cached under ~/.cache/geist-font. All text is converted to outlines with HarfBuzz
and fontTools, so the SVGs render identically everywhere and load no fonts. Editing the
copy means editing this file and regenerating, not editing the paths.
"""

from __future__ import annotations

import io
import os
import sys
import urllib.request
import zipfile

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

GEIST_RELEASE = (
    "https://github.com/vercel/geist-font/releases/download/v1.7.2/geist-font-v1.7.2.zip"
)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else HERE
FONTS = os.environ.get("GEIST_DIR") or os.path.expanduser("~/.cache/geist-font/geist-font")

if not os.path.isdir(FONTS):
    print("fetching Geist from", GEIST_RELEASE, file=sys.stderr)
    with urllib.request.urlopen(GEIST_RELEASE) as resp:
        zipfile.ZipFile(io.BytesIO(resp.read())).extractall(os.path.dirname(FONTS))
os.makedirs(OUT, exist_ok=True)


def fmt(v: float) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


class Face:
    def __init__(self, rel: str):
        path = os.path.join(FONTS, rel)
        self.tt = TTFont(path)
        self.upem = self.tt["head"].unitsPerEm
        self.gs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.hb = hb.Font(hb.Face(hb.Blob.from_file_path(path)))
        self.hb.scale = (self.upem, self.upem)

    def shape(self, text: str):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, {"calt": False, "liga": False, "kern": True})
        return list(zip(buf.glyph_infos, buf.glyph_positions, strict=True))

    def width(self, text: str, size: float) -> float:
        return sum(p.x_advance for _, p in self.shape(text)) * size / self.upem

    def path(self, text: str, size: float) -> tuple[str, float]:
        s = size / self.upem
        pen = SVGPathPen(self.gs, ntos=fmt)
        x = 0
        for info, pos in self.shape(text):
            name = self.order[info.codepoint]
            tp = TransformPen(pen, (s, 0, 0, -s, (x + pos.x_offset) * s, -pos.y_offset * s))
            self.gs[name].draw(tp)
            x += pos.x_advance
        return pen.getCommands(), x * s


SANS = Face("Geist/otf/Geist-Regular.otf")
SANS_MED = Face("Geist/otf/Geist-Medium.otf")
SANS_SEMI = Face("Geist/otf/Geist-SemiBold.otf")
MONO = Face("GeistMono/otf/GeistMono-Regular.otf")
MONO_MED = Face("GeistMono/otf/GeistMono-Medium.otf")

THEMES = {
    "light": {
        "bg": "#FFFFFF",
        "fg": "#000000",
        "secondary": "#666666",
        "dim": "#8F8F8F",
        "hairline": "#EAEAEA",
        "key": "#0070F3",
        "arg": "#B76E00",
    },
    "dark": {
        "bg": "#000000",
        "fg": "#FFFFFF",
        "secondary": "#A1A1A1",
        "dim": "#6F6F6F",
        "hairline": "#1F1F1F",
        "key": "#52A8FF",
        "arg": "#F5A623",
    },
}


def text(face: Face, s: str, size: float, x: float, y: float, fill: str) -> tuple[str, float]:
    d, w = face.path(s, size)
    return f'  <path transform="translate({fmt(x)} {fmt(y)})" d="{d}" fill="{fill}"/>', w


def run(face: Face, segments, size: float, x: float, y: float, t) -> list[str]:
    """A line of text made of (string, colour-token) segments laid end to end."""
    out = []
    for s, tok in segments:
        el, w = text(face, s, size, x, y, t[tok])
        out.append(el)
        x += w
    return out


# ---------------------------------------------------------------- the mark
# A square and the same square turned 45 degrees, sharing a corner: one script in two
# shapes. Drawn on a 64 grid at 0/45/90 degrees, one stroke weight, mitred joins.
SQ = (8, 8, 36, 36)  # x1 y1 x2 y2
DC, DR = 40, 20  # diamond centre and half-diagonal
STROKE = 4.4


def mark(stroke: str, sw: float = STROKE, uid: str = "m") -> str:
    x1, y1, x2, y2 = SQ
    r_mask = DR + sw / 2 * 2**0.5  # diamond's outer stroke boundary, along the axes
    diamond = f"M{DC} {DC - DR} L{DC + DR} {DC} L{DC} {DC + DR} L{DC - DR} {DC} Z"
    mask_d = (
        f"M{DC} {fmt(DC - r_mask)} L{fmt(DC + r_mask)} {DC} "
        f"L{DC} {fmt(DC + r_mask)} L{fmt(DC - r_mask)} {DC} Z"
    )
    return (
        f'<defs><mask id="{uid}"><rect width="64" height="64" fill="#fff"/>'
        f'<path d="{mask_d}" fill="#000"/></mask></defs>'
        f'<g fill="none" stroke="{stroke}" stroke-width="{sw}" stroke-linejoin="miter">'
        f'<path mask="url(#{uid})" d="M{x1} {y1} H{x2} V{y2} H{x1} Z"/>'
        f'<path d="{diamond}"/></g>'
    )


# ---------------------------------------------------------------- the banner
W, H = 1200, 340
DIV = 500  # divider between the two halves
PAD = 40  # inner padding of the right panel
CODE = 14  # mono size in the panel
LINE = 22  # line height in the panel

PY = [
    [("@script", "fg")],
    [
        ("def hit(key: ", "fg"),
        ("Key", "key"),
        (", ttl: ", "fg"),
        ("int", "arg"),
        (") -> int:", "fg"),
    ],
    [("    n = redis.incr(key)", "fg")],
    [("    if n == 1:", "fg")],
    [("        redis.expire(key, ttl)", "fg")],
    [("    return n", "fg")],
]
LUA = [
    [("local key = ", "fg"), ("KEYS[1]", "key")],
    [("local ttl = ", "fg"), ("tonumber(ARGV[1])", "arg")],
    [("local n = redis.call('INCR', key)", "fg")],
    [("if n == 1 then", "fg")],
    [("  redis.call('EXPIRE', key, ttl)", "fg")],
    [("end", "fg")],
    [("return n", "fg")],
]


def banner(theme: str) -> str:
    t = THEMES[theme]
    el = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="redis-lua-py - Redis Lua scripts as real Python functions">',
        f'  <rect width="{W}" height="{H}" rx="14" fill="{t["bg"]}"/>',
        f'  <rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="13.5" fill="none" '
        f'stroke="{t["hairline"]}"/>',
        f'  <g transform="translate(54.67 62.33) scale(1.1667)">{mark(t["fg"])}</g>',
    ]
    el.append(text(SANS_SEMI, "redis-lua-py", 56, 60, 192, t["fg"])[0])
    el.append(
        text(SANS, "Redis Lua scripts as real Python functions.", 20, 62, 236, t["secondary"])[0]
    )
    el.append(text(SANS, "Python 3.11+, sync and async redis-py, MIT.", 15, 62, 270, t["dim"])[0])

    # right panel
    el.append(f'  <rect x="{DIV}" y="0" width="1" height="{H}" fill="{t["hairline"]}"/>')
    x0 = DIV + PAD
    x1 = W - PAD
    py_w = max(sum(MONO.width(s, CODE) for s, _ in line) for line in PY)
    lua_w = max(sum(MONO.width(s, CODE) for s, _ in line) for line in LUA)
    gap = (x1 - x0) - py_w - lua_w
    xl = x0
    xr = x1 - lua_w
    el.append(text(MONO, "limits.py", 13, xl, 74, t["dim"])[0])
    el.append(text(MONO, "hit.lua", 13, xr, 74, t["dim"])[0])
    el.append(f'  <rect x="{x0}" y="94" width="{x1 - x0}" height="1" fill="{t["hairline"]}"/>')
    y = 124
    for i in range(max(len(PY), len(LUA))):
        if i < len(PY):
            el += run(MONO, PY[i], CODE, xl, y + i * LINE, t)
        if i < len(LUA):
            el += run(MONO, LUA[i], CODE, xr, y + i * LINE, t)
    # arrow in the gutter, on the vertical centre of the code block
    ax = x0 + py_w + gap / 2
    ay = y + (len(LUA) - 1) * LINE / 2
    aw = MONO.width("→", 16)
    el.append(text(MONO, "→", 16, ax - aw / 2, ay + 5, t["dim"])[0])
    el.append(f'  <rect x="{x0}" y="280" width="{x1 - x0}" height="1" fill="{t["hairline"]}"/>')
    el.append(
        text(
            MONO,
            "checked by mypy  ·  compiled at import  ·  sent with EVALSHA",
            13,
            x0,
            308,
            t["dim"],
        )[0]
    )
    el.append("</svg>")
    return "\n".join(el) + "\n"


def logo() -> str:
    # 64 tile, the mark scaled to leave the same breathing room as the mailgrade tile.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" '
        'role="img" aria-label="redis-lua-py">\n'
        '  <rect width="64" height="64" rx="14" fill="#000000"/>\n'
        f'  <g transform="translate(7.6 7.6) scale(0.76)">{mark("#FFFFFF", 6.0, "l")}</g>\n'
        "</svg>\n"
    )


def logomark() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" '
        'role="img" aria-label="redis-lua-py">\n'
        f"  {mark('currentColor', STROKE, 'lm')}\n"
        "</svg>\n"
    )


def social() -> str:
    t = THEMES["dark"]
    sw, sh = 1280, 640
    el = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{sw}" height="{sh}" '
        f'viewBox="0 0 {sw} {sh}">',
        f'  <rect width="{sw}" height="{sh}" fill="{t["bg"]}"/>',
    ]
    # composition: mark on the left of the wordmark, tagline below, install line at the bottom
    wm_size = 84
    wm_w = SANS_SEMI.width("redis-lua-py", wm_size)
    mk_scale = 1.75
    mk_w = 64 * mk_scale
    gap = 40
    total = mk_w + gap + wm_w
    x = (sw - total) / 2
    base = 330
    my = fmt(base - 64 * mk_scale + 6)
    el.append(f'  <g transform="translate({fmt(x)} {my}) scale({mk_scale})">{mark(t["fg"])}</g>')
    el.append(text(SANS_SEMI, "redis-lua-py", wm_size, x + mk_w + gap, base, t["fg"])[0])
    tag = "Redis Lua scripts as real Python functions."
    tw = SANS.width(tag, 30)
    el.append(text(SANS, tag, 30, (sw - tw) / 2, base + 64, t["secondary"])[0])
    inst = "uv add redis-lua-py"
    iw = MONO.width(inst, 22)
    el.append(text(MONO, inst, 22, (sw - iw) / 2, base + 160, t["dim"])[0])
    el.append("</svg>")
    return "\n".join(el) + "\n"


for name, content in {
    "banner-light.svg": banner("light"),
    "banner-dark.svg": banner("dark"),
    "logo.svg": logo(),
    "logomark.svg": logomark(),
    "social-preview.svg": social(),
}.items():
    with open(os.path.join(OUT, name), "w") as f:
        f.write(content)
    print(name, os.path.getsize(os.path.join(OUT, name)))
