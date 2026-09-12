"""A small Lua IR and the emitter that turns it into source text.

The IR is deliberately Lua-shaped rather than Python-shaped: every semantic
gap between the two languages is closed in ``_compile``, so that by the time a
node reaches this module it means exactly what the emitted text means. That
keeps the emitter dumb, which is what makes the output reviewable.

This module is private. Its shape is not part of the public API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Lua 5.1 operator precedence, lowest binding first. Used to decide where
# parentheses are actually required, so the emitted source stays readable.
_PRECEDENCE: dict[str, int] = {
    "or": 1,
    "and": 2,
    "<": 3,
    ">": 3,
    "<=": 3,
    ">=": 3,
    "~=": 3,
    "==": 3,
    "..": 4,
    "+": 5,
    "-": 5,
    "*": 6,
    "/": 6,
    "%": 6,
    "^": 8,
}
_UNARY_PRECEDENCE = 7
_RIGHT_ASSOCIATIVE = {"..", "^"}

_LUA_KEYWORDS = frozenset(
    [
        "and",
        "break",
        "do",
        "else",
        "elseif",
        "end",
        "false",
        "for",
        "function",
        "if",
        "in",
        "local",
        "nil",
        "not",
        "or",
        "repeat",
        "return",
        "then",
        "true",
        "until",
        "while",
    ]
)

# NUL is spelled with all three digits on purpose: Lua reads up to three, so a
# bare "\0" followed by a digit in the data would be read as a different byte.
_ESCAPES = {"\\": "\\\\", "'": "\\'", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\000"}

# Binary data gets no shorthand at all; every byte outside printable ASCII is
# written as a three-digit escape, which cannot run into the byte after it.
_BYTE_ESCAPES = {ord("\\"): "\\\\", ord("'"): "\\'"}


def quote(value: str) -> str:
    """Render a Python string as a single-quoted Lua literal."""
    out = []
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def quote_bytes(value: bytes) -> str:
    r"""Render a Python bytes literal as a Lua string of exactly those bytes.

    Lua strings are byte strings, so every byte has a representation -- but the
    generated source travels to Redis as text, and anything outside ASCII would
    have to survive that encoding intact. Escaping numerically sidesteps the
    question: the literal is pure ASCII, and Lua's ``\ddd`` puts the original
    bytes back.
    """
    out = []
    for byte in value:
        if byte in _BYTE_ESCAPES:
            out.append(_BYTE_ESCAPES[byte])
        elif 0x20 <= byte < 0x7F:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03d}")
    return "'" + "".join(out) + "'"


def is_identifier(name: str) -> bool:
    return name.isidentifier() and name not in _LUA_KEYWORDS


class Node:
    __slots__ = ()


class Expr(Node):
    __slots__ = ()


class Stat(Node):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Nil(Expr):
    pass


@dataclass(frozen=True, slots=True)
class Bool(Expr):
    value: bool


@dataclass(frozen=True, slots=True)
class Num(Expr):
    value: int | float


@dataclass(frozen=True, slots=True)
class Str(Expr):
    value: str


@dataclass(frozen=True, slots=True)
class Bytes(Expr):
    """A bytes literal. Lua has no separate type; only the spelling differs."""

    value: bytes


@dataclass(frozen=True, slots=True)
class Name(Expr):
    id: str


@dataclass(frozen=True, slots=True)
class BinOp(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True, slots=True)
class UnOp(Expr):
    op: str  # "not", "-", "#"
    operand: Expr


@dataclass(frozen=True, slots=True)
class Index(Expr):
    obj: Expr
    key: Expr


@dataclass(frozen=True, slots=True)
class Call(Expr):
    func: Expr
    args: tuple[Expr, ...]


@dataclass(frozen=True, slots=True)
class Table(Expr):
    array: tuple[Expr, ...] = ()
    hash: tuple[tuple[Expr, Expr], ...] = ()


@dataclass(slots=True)
class Local(Stat):
    names: list[str]
    values: list[Expr]


@dataclass(slots=True)
class Assign(Stat):
    targets: list[Expr]
    values: list[Expr]


@dataclass(slots=True)
class If(Stat):
    branches: list[tuple[Expr, list[Stat]]]
    orelse: list[Stat] = field(default_factory=list)


@dataclass(slots=True)
class NumericFor(Stat):
    var: str
    start: Expr
    stop: Expr
    step: Expr | None
    body: list[Stat]


@dataclass(slots=True)
class While(Stat):
    test: Expr
    body: list[Stat]


@dataclass(slots=True)
class Return(Stat):
    value: Expr | None = None


@dataclass(slots=True)
class Break(Stat):
    pass


@dataclass(slots=True)
class ExprStat(Stat):
    expr: Expr


@dataclass(slots=True)
class Comment(Stat):
    text: str


@dataclass(frozen=True, slots=True)
class Function(Expr):
    """An anonymous function, emitted on one line.

    Only ever produced to be called on the spot. That is how an expression that
    needs statements -- `a or b` whose right side must not run early, or a
    conditional expression whose branch may be false -- keeps Python's meaning.
    """

    params: tuple[str, ...]
    body: tuple[Stat, ...]


@dataclass(frozen=True, slots=True)
class Values(Expr):
    """Several values where Lua takes a list of them, as in `return true, v`."""

    items: tuple[Expr, ...]


@dataclass(slots=True)
class Repeat(Stat):
    """`repeat ... until true`: a block that runs once and can be left with break.

    That is how `continue` is spelled in Lua 5.1, which has neither `continue`
    nor `goto`.
    """

    body: list[Stat]


@dataclass(slots=True)
class LocalFunction(Stat):
    name: str
    params: list[str]
    body: list[Stat]


@dataclass(slots=True)
class GenericFor(Stat):
    names: list[str]
    iterator: Expr
    body: list[Stat]


def emit_expr(node: Expr, parent_prec: int = 0) -> str:
    """Render an expression, parenthesising only where precedence demands it."""
    match node:
        case Nil():
            return "nil"
        case Bool(value=v):
            return "true" if v else "false"
        case Num(value=v):
            if isinstance(v, float) and v != v:
                return "(0/0)"
            if v in (float("inf"), float("-inf")):
                # repr() would give `inf`, which Lua reads as an unset global.
                text = "math.huge" if v > 0 else "-math.huge"
            else:
                text = repr(v) if isinstance(v, float) else str(v)
            # A negative literal under a unary operator would otherwise emit
            # `--5`, which Lua reads as the start of a comment.
            if text.startswith("-") and parent_prec >= _UNARY_PRECEDENCE:
                return f"({text})"
            return text
        case Str(value=v):
            return quote(v)
        case Bytes(value=v):
            return quote_bytes(v)
        case Name(id=v):
            return v
        case Table(array=arr, hash=pairs):
            items = [emit_expr(a) for a in arr]
            items += [
                (
                    f"[{emit_expr(k)}] = {emit_expr(val)}"
                    if not (isinstance(k, Str) and is_identifier(k.value))
                    else f"{k.value} = {emit_expr(val)}"
                )
                for k, val in pairs
            ]
            return "{" + ", ".join(items) + "}"
        case Index(obj=obj, key=key):
            base = emit_expr(obj, 9)
            if isinstance(key, Str) and is_identifier(key.value):
                return f"{base}.{key.value}"
            return f"{base}[{emit_expr(key)}]"
        case Call(func=func, args=args):
            rendered = ", ".join(emit_expr(a) for a in args)
            callee = f"({emit_expr(func)})" if isinstance(func, Function) else emit_expr(func, 9)
            return f"{callee}({rendered})"
        case Values(items=items):
            return ", ".join(emit_expr(item) for item in items)
        case Function(params=params, body=body):
            inner = " ".join(line.strip() for line in emit_block(list(body)))
            return f"function({', '.join(params)}) {inner} end"
        case UnOp(op=op, operand=operand):
            spacer = " " if op == "not" else ""
            text = f"{op}{spacer}{emit_expr(operand, _UNARY_PRECEDENCE)}"
            return f"({text})" if parent_prec > _UNARY_PRECEDENCE else text
        case BinOp(op=op, left=left, right=right):
            prec = _PRECEDENCE[op]
            # For a right-associative operator the left operand needs the
            # tighter bound, and vice versa.
            if op in _RIGHT_ASSOCIATIVE:
                left_prec, right_prec = prec + 1, prec
            else:
                left_prec, right_prec = prec, prec + 1
            text = f"{emit_expr(left, left_prec)} {op} {emit_expr(right, right_prec)}"
            return f"({text})" if prec < parent_prec else text
        case _:  # pragma: no cover - guards against an IR node with no emitter
            raise TypeError(f"cannot emit expression node {type(node).__name__}")


def emit_block(body: list[Stat], indent: int = 0) -> list[str]:
    lines: list[str] = []
    pad = "  " * indent
    for stat in body:
        match stat:
            case Comment(text=text):
                lines += [f"{pad}-- {line}" for line in text.splitlines()]
            case Local(names=names, values=values):
                target = ", ".join(names)
                if values:
                    rhs = ", ".join(emit_expr(v) for v in values)
                    lines.append(f"{pad}local {target} = {rhs}")
                else:
                    lines.append(f"{pad}local {target}")
            case Assign(targets=targets, values=values):
                lhs = ", ".join(emit_expr(t) for t in targets)
                rhs = ", ".join(emit_expr(v) for v in values)
                lines.append(f"{pad}{lhs} = {rhs}")
            case ExprStat(expr=expr):
                lines.append(f"{pad}{emit_expr(expr)}")
            case Return(value=value):
                rendered = "" if value is None else f" {emit_expr(value)}"
                lines.append(f"{pad}return{rendered}")
            case Break():
                # Lua 5.1 requires `break` to be the final statement of a
                # block; wrapping it in `do ... end` makes that true wherever
                # Python allowed it.
                lines.append(f"{pad}do break end")
            case While(test=test, body=inner):
                lines.append(f"{pad}while {emit_expr(test)} do")
                lines += emit_block(inner, indent + 1)
                lines.append(f"{pad}end")
            case Repeat(body=inner):
                lines.append(f"{pad}repeat")
                lines += emit_block(inner, indent + 1)
                lines.append(f"{pad}until true")
            case LocalFunction(name=name, params=params, body=inner):
                lines.append(f"{pad}local function {name}({', '.join(params)})")
                lines += emit_block(inner, indent + 1)
                lines.append(f"{pad}end")
            case GenericFor(names=names, iterator=iterator, body=inner):
                lines.append(f"{pad}for {', '.join(names)} in {emit_expr(iterator)} do")
                lines += emit_block(inner, indent + 1)
                lines.append(f"{pad}end")
            case NumericFor(var=var, start=start, stop=stop, step=step, body=inner):
                header = f"{pad}for {var} = {emit_expr(start)}, {emit_expr(stop)}"
                if step is not None:
                    header += f", {emit_expr(step)}"
                lines.append(header + " do")
                lines += emit_block(inner, indent + 1)
                lines.append(f"{pad}end")
            case If(branches=branches, orelse=orelse):
                for i, (test, inner) in enumerate(branches):
                    keyword = "if" if i == 0 else "elseif"
                    lines.append(f"{pad}{keyword} {emit_expr(test)} then")
                    lines += emit_block(inner, indent + 1)
                if orelse:
                    lines.append(f"{pad}else")
                    lines += emit_block(orelse, indent + 1)
                lines.append(f"{pad}end")
            case _:  # pragma: no cover - guards against an IR node with no emitter
                raise TypeError(f"cannot emit statement node {type(stat).__name__}")
    return lines


def emit(body: list[Stat]) -> str:
    return "\n".join(emit_block(body)) + "\n"
