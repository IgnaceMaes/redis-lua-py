# Brand assets

| File | Use |
| --- | --- |
| `banner-light.svg` / `banner-dark.svg` | README header, 1200×340, picked by `prefers-color-scheme` |
| `logo.svg` | 64×64 tile, safe down to 16px |
| `logomark.svg` | the bare mark, `stroke: currentColor` |
| `social-preview.svg` / `.png` | 1280×640, upload the PNG under Settings → Social preview |
| `generate.py` | regenerates all of the above: `uv run .github/assets/generate.py` |

## The mark

A square, and the same square turned 45°, sharing a corner: one script in two
shapes. It is drawn on a 64 grid at 0°, 45° and 90° only, in one stroke weight,
with mitred joins and no curves. The diamond sits in front and masks the square
where they overlap, so the mark works on any background.

## Colours

Monochrome, with colour reserved for the two things a script declares.

| Token | Dark | Light |
| --- | --- | --- |
| background | `#000000` | `#FFFFFF` |
| foreground | `#FFFFFF` | `#000000` |
| secondary / dim | `#A1A1A1` / `#6F6F6F` | `#666666` / `#8F8F8F` |
| hairline | `#1F1F1F` | `#EAEAEA` |
| `Key` → `KEYS` | `#52A8FF` | `#0070F3` |
| `int` → `tonumber(ARGV)` | `#F5A623` | `#B76E00` |

## Type

[Geist](https://vercel.com/font) for the wordmark and prose, Geist Mono for
code, both OFL. All text is converted to outlines, so the SVGs render
identically everywhere and load no fonts. Editing the copy means editing
`generate.py` and regenerating, not editing the paths. The PNG is a headless
Chrome render of `social-preview.svg`:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
  --window-size=1280,640 --hide-scrollbars --force-device-scale-factor=1 \
  --screenshot=.github/assets/social-preview.png .github/assets/social-preview.svg
```
