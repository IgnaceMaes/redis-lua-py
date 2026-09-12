"""Turn a Python function into Lua.

The supported subset is deliberately small. Anything outside it raises
:class:`UnsupportedSyntax` pointing at the offending line, because a script
body that *looks* like Python but is never executed by Python is exactly the
place where a silent mistranslation would be most expensive.
"""

from __future__ import annotations

import ast
import difflib
import inspect
import textwrap
import warnings
from collections.abc import Callable
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn, TypeVar

from . import _lua as lua
from ._commands import COMMANDS, SUBCOMMANDS
from ._runtime import _Namespace
from ._script import CompiledScript
from .errors import CompileError, NilTruncationWarning, UnsupportedSyntax, render_location

R = TypeVar("R")

# Members of the `redis` table that are not commands and keep their own name.
_REDIS_DIRECT = frozenset(
    {
        "call",
        "pcall",
        "error_reply",
        "status_reply",
        "sha1hex",
        "log",
        "replicate_commands",
        "set_repl",
        "setresp",
        "acl_check_cmd",
        "breakpoint",
        "debug",
    }
)

# Constants on the `redis` table, for redis.log levels and redis.set_repl.
_REDIS_CONSTANTS = frozenset(
    {
        "LOG_DEBUG",
        "LOG_VERBOSE",
        "LOG_NOTICE",
        "LOG_WARNING",
        "REPL_ALL",
        "REPL_AOF",
        "REPL_REPLICA",
        "REPL_SLAVE",
        "REPL_NONE",
        "REDIS_VERSION",
        "REDIS_VERSION_NUM",
    }
)

# Receivers are normally resolved by value. These spellings are the fallback
# for a name that is not bound in the defining module at all.
_RECEIVER_FALLBACK = {"redis": "redis", "call": "redis", "cjson": "cjson"}

# redis-py method names that do not match the wire name of the command they
# send. Spelling one of these the way redis-py does is not a mistake worth an
# error -- it names exactly one command, unambiguously -- so it is simply
# translated. Every other name is checked against the command table.
_COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "delete": ("DEL",),
}

# Directories that mark the top of a project, for rendering the source path in
# the generated header relative to something stable.
_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git")

_UNBOUND = object()


def _is_redis_py(value: object) -> bool:
    """True for the redis-py package itself or one of its client objects."""
    if isinstance(value, ModuleType):
        return (value.__name__ or "").split(".")[0] == "redis"
    return (type(value).__module__ or "").split(".")[0] == "redis"


_COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "~=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}

_BIN_OPS: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.Mod: "%",
    ast.Pow: "^",
}

# Python builtins with an exact Lua counterpart.
_BUILTIN_FUNCS: dict[str, str] = {
    "int": "tonumber",
    "float": "tonumber",
    "str": "tostring",
    "tonumber": "tonumber",
    "tostring": "tostring",
    "abs": "math.abs",
    "min": "math.min",
    "max": "math.max",
}

_TRUTHY_HELPER = """\
-- Python truthiness: 0, '', empty tables and nil are all false.
local function __truthy(v)
  if v == nil or v == false then return false end
  if v == 0 or v == '' then return false end
  if type(v) == 'table' and next(v) == nil then return false end
  return true
end
"""

_ISNIL_HELPER = """\
-- A Redis command with nothing to return hands Lua false, not nil, so an
-- `is None` test has to accept both. Taking v as an argument also means the
-- operand is evaluated once, not once per comparison.
local function __isnil(v)
  return v == nil or v == false
end
"""


class _Compiler:
    def __init__(
        self,
        func: ast.FunctionDef,
        *,
        filename: str,
        first_lineno: int,
        lines: list[str],
        globalns: dict[str, Any],
    ) -> None:
        self.func = func
        self.globalns = globalns
        self.filename = filename
        self.first_lineno = first_lineno
        self.lines = lines
        self.known: set[str] = set()
        self.keys: list[str] = []
        self.args: list[str] = []
        self.params: list[str] = []
        self.numeric_args: set[str] = set()
        self.hoisted: list[str] = []
        self.simple_assigns: set[int] = set()
        self.needs_truthy = False
        self.needs_isnil = False
        self._temp = 0

    # ----------------------------------------------------------------- errors

    def fail(self, node: ast.AST, message: str, hint: str | None = None) -> NoReturn:
        lineno = getattr(node, "lineno", 1)
        source_line = self.lines[lineno - 1] if 0 < lineno <= len(self.lines) else None
        raise UnsupportedSyntax(
            message,
            filename=self.filename,
            lineno=self.first_lineno + lineno - 1,
            col=getattr(node, "col_offset", 0),
            source_line=source_line,
            hint=hint,
        )

    # --------------------------------------------------------------- constants

    def module_constant(self, node: ast.expr, name: str) -> lua.Expr:
        """Resolve a name the body did not bind, against the defining module.

        A script has no closure: the body runs on the server, where nothing
        from the Python process exists. A module-level constant is the
        exception worth making, because the value is already a literal -- it
        can simply be folded into the script. Without this the only way to
        write a TTL the module already names is to repeat the number, which
        turns the library's own argument about reviewability against it.
        """
        value = self.globalns.get(name, _UNBOUND)
        if value is _UNBOUND:
            self.fail(
                node,
                f"undefined name {name!r}",
                hint="A script can only use its parameters, the names it assigns, and "
                "module-level constants holding an int, float, str, bytes or bool.",
            )
        return self.fold(node, name, value)

    def module_attribute(self, node: ast.Attribute) -> lua.Expr:
        """Fold a dotted module-level constant, such as an enum member.

        ``Priority.HIGH`` and ``settings.SESSION_TTL`` name a constant just as
        plainly as a bare global does, and naming constants that way is common
        enough that refusing it would push bodies back towards magic numbers.
        """
        parts = _dotted_name(node)
        if parts is not None:
            if (
                len(parts) == 2
                and parts[1] in _REDIS_CONSTANTS
                and self.namespace_kind(parts[0]) == "redis"
            ):
                return lua.Index(lua.Name("redis"), lua.Str(parts[1]))
            root = self.globalns.get(parts[0], _UNBOUND)
            # A namespace answers to every attribute with a call stub, and
            # redis-py has a clearer error of its own; neither is a constant.
            if root is not _UNBOUND and not isinstance(root, _Namespace) and not _is_redis_py(root):
                value: object = root
                for attr in parts[1:]:
                    value = getattr(value, attr, _UNBOUND)
                    if value is _UNBOUND:
                        break
                else:
                    return self.fold(node, ".".join(parts), value)
        self.fail(node, f"attribute access .{node.attr} is not supported here")

    def fold(self, node: ast.expr, label: str, value: object) -> lua.Expr:
        literal = _as_literal(value)
        if literal is None:
            self.fail(
                node,
                f"{label!r} is a module-level {type(value).__name__}, which has no Lua literal",
                hint="Only an int, float, str, bytes or bool constant is folded into the "
                "script. Pass anything else as an argument, or name the literal it "
                "reduces to.",
            )
        return literal

    # ---------------------------------------------------------------- scoping

    def collect_assigned(self) -> None:
        """Decide which names need hoisting to function scope.

        Python scopes assignments to the whole function; Lua's ``local`` scopes
        them to the enclosing block. A name assigned inside an ``if`` and read
        after it must therefore be declared up front, or the read sees ``nil``.

        A name whose *first* assignment sits at the top level of the body needs
        no hoist: a ``local`` there is already in scope for everything that
        follows, nested blocks included. Only names that first appear inside a
        block get declared up front.
        """
        nodes: dict[str, list[ast.stmt]] = {}

        def record(name: str, node: ast.stmt) -> None:
            nodes.setdefault(name, []).append(node)

        for node in ast.walk(self.func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        record(target.id, node)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(
                node.target, ast.Name
            ):
                record(node.target.id, node)
            elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                # Loop targets get Lua's own loop scope; only record them so
                # that reads of the name resolve.
                self.known.add(node.target.id)

        top_level = {id(stmt) for stmt in self.func.body}
        for name, assignments in nodes.items():
            self.known.add(name)
            if name in self.params:
                # Already a local from the prelude. Declaring it again would
                # shadow the argument with nil.
                continue
            # ast.walk is breadth-first, so ask for source order explicitly.
            first = min(assignments, key=lambda n: (n.lineno, n.col_offset))
            if id(first) in top_level:
                self.simple_assigns.add(id(first))
            else:
                self.hoisted.append(name)

    # -------------------------------------------------------------- signature

    def compile_signature(self) -> list[lua.Stat]:
        sig = self.func.args
        if sig.vararg or sig.kwarg:
            self.fail(self.func, "*args and **kwargs are not supported in a script signature")
        if sig.posonlyargs:
            self.fail(self.func, "positional-only parameters are not supported")

        prelude: list[lua.Stat] = []
        for arg in [*sig.args, *sig.kwonlyargs]:
            name = arg.arg
            if not lua.is_identifier(name):
                self.fail(arg, f"{name!r} is a reserved word in Lua")
            self.params.append(name)
            self.known.add(name)

            if self._is_key(arg.annotation):
                self.keys.append(name)
                source: lua.Expr = lua.Index(lua.Name("KEYS"), lua.Num(len(self.keys)))
            else:
                self.args.append(name)
                source = lua.Index(lua.Name("ARGV"), lua.Num(len(self.args)))
                if self._is_numeric(arg.annotation):
                    # ARGV always arrives as strings; an int/float annotation
                    # is the author asking for the conversion.
                    self.numeric_args.add(name)
                    source = lua.Call(lua.Name("tonumber"), (source,))
            prelude.append(lua.Local([name], [source]))

        if sig.defaults or any(d is not None for d in sig.kw_defaults):
            self.fail(
                self.func,
                "default values are not supported",
                hint="Redis has no notion of an absent ARGV entry; pass the value explicitly.",
            )
        return prelude

    @staticmethod
    def _annotation_name(node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Subscript):
            return _Compiler._annotation_name(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.rsplit(".", 1)[-1].split("[", 1)[0]
        return None

    def _is_key(self, node: ast.expr | None) -> bool:
        return self._annotation_name(node) == "Key"

    def _is_numeric(self, node: ast.expr | None) -> bool:
        return self._annotation_name(node) in {"int", "float"}

    # ------------------------------------------------------------ expressions

    def expr(self, node: ast.expr) -> lua.Expr:
        match node:
            case ast.Constant(value=None):
                return lua.Nil()
            case ast.Constant(value=bool() as v):
                return lua.Bool(v)
            case ast.Constant(value=int() | float() as v):
                return lua.Num(v)
            case ast.Constant(value=str() as v):
                return lua.Str(v)
            case ast.Constant(value=bytes() as v):
                return lua.Bytes(v)
            case ast.Name(id=name):
                if name not in self.known:
                    return self.module_constant(node, name)
                return lua.Name(name)
            case ast.BinOp():
                return self.binop(node)
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                return lua.UnOp("-", self.expr(operand))
            case ast.UnaryOp(op=ast.UAdd(), operand=operand):
                return self.expr(operand)
            case ast.UnaryOp(op=ast.Not()):
                return self.condition(node)
            case ast.Compare():
                return self.compare(node)
            case ast.BoolOp():
                self.fail(
                    node,
                    "'and'/'or' are only supported in an if or while condition",
                    hint="In Python these return an operand, which does not survive the "
                    "difference in truthiness. Use an if statement instead.",
                )
            case ast.IfExp():
                self.fail(
                    node,
                    "conditional expressions (a if c else b) are not supported",
                    hint="Lua's `c and a or b` is wrong when a is false or nil. "
                    "Use an if statement.",
                )
            case ast.Call():
                return self.call(node)
            case ast.Subscript():
                return self.subscript(node)
            case ast.List(elts=elts) | ast.Tuple(elts=elts):
                return lua.Table(array=tuple(self.expr(e) for e in elts))
            case ast.Dict(keys=keys, values=values):
                pairs = []
                for key_node, value_node in zip(keys, values, strict=True):
                    if key_node is None:
                        self.fail(node, "dict unpacking (**) is not supported")
                    pairs.append((self.expr(key_node), self.expr(value_node)))
                return lua.Table(hash=tuple(pairs))
            case ast.JoinedStr(values=values):
                return self.fstring(node, values)
            case ast.Attribute():
                return self.module_attribute(node)
            case _:
                self.fail(node, f"{type(node).__name__} expressions are not supported")

    def binop(self, node: ast.BinOp) -> lua.Expr:
        left, right = self.expr(node.left), self.expr(node.right)
        if isinstance(node.op, ast.FloorDiv):
            return lua.Call(lua.Name("math.floor"), (lua.BinOp("/", left, right),))
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            self.fail(node, f"the {type(node.op).__name__} operator is not supported")
        if op == "+" and (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)):
            self.fail(
                node,
                "'+' is arithmetic in Lua and will not concatenate strings",
                hint="Use an f-string, which compiles to Lua's .. operator.",
            )
        return lua.BinOp(op, left, right)

    def compare(self, node: ast.Compare) -> lua.Expr:
        if len(node.ops) != 1:
            self.fail(
                node,
                "chained comparisons are not supported",
                hint="Split 'a < b < c' into 'a < b and b < c'.",
            )
        op_node, right_node = node.ops[0], node.comparators[0]
        left = self.expr(node.left)

        if isinstance(op_node, ast.Is | ast.IsNot):
            if not _is_none(right_node):
                self.fail(node, "'is' is only supported against None")
            return self.none_check(left, negate=isinstance(op_node, ast.IsNot))

        # `== None` means the same as `is None` to anyone reading it, and
        # compiling it to `== nil` would never match the false Redis sends.
        if isinstance(op_node, ast.Eq | ast.NotEq) and (
            _is_none(right_node) or _is_none(node.left)
        ):
            operand = self.expr(right_node) if _is_none(node.left) else left
            return self.none_check(operand, negate=isinstance(op_node, ast.NotEq))

        op = _COMPARE_OPS.get(type(op_node))
        if op is None:
            self.fail(
                node,
                f"the {type(op_node).__name__} comparison is not supported",
                hint="Lua 5.1 has no 'in' operator; loop over the table instead."
                if isinstance(op_node, ast.In | ast.NotIn)
                else None,
            )
        return lua.BinOp(op, left, self.expr(right_node))

    def none_check(self, operand: lua.Expr, *, negate: bool) -> lua.Expr:
        # Not `== nil`: Redis reports a missing value to Lua as false.
        self.needs_isnil = True
        check: lua.Expr = lua.Call(lua.Name("__isnil"), (operand,))
        return lua.UnOp("not", check) if negate else check

    def fstring(self, node: ast.expr, values: list[ast.expr]) -> lua.Expr:
        parts: list[lua.Expr] = []
        for value in values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(lua.Str(value.value))
            elif isinstance(value, ast.FormattedValue):
                if value.format_spec is not None or value.conversion not in (-1, 115):
                    self.fail(value, "format specs and conversions are not supported in f-strings")
                parts.append(lua.Call(lua.Name("tostring"), (self.expr(value.value),)))
            else:  # pragma: no cover - JoinedStr only holds these two kinds
                self.fail(value, "unsupported f-string component")
        if not parts:
            return lua.Str("")
        # Lua's .. is right-associative; folding the same way avoids emitting a
        # nest of parentheses that mean nothing.
        result = parts[-1]
        for part in reversed(parts[:-1]):
            result = lua.BinOp("..", part, result)
        return result

    def subscript(self, node: ast.Subscript) -> lua.Expr:
        if isinstance(node.slice, ast.Slice):
            self.fail(
                node,
                "slicing is not supported",
                hint="Loop over the table, or slice with a Redis command such as LRANGE.",
            )
        obj = self.expr(node.value)
        return lua.Index(obj, self.index(node.slice))

    def index(self, node: ast.expr) -> lua.Expr:
        """Translate a 0-based Python index to Lua's 1-based one."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return lua.Str(node.value)  # string key: no offset
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if node.value < 0:
                self.fail(
                    node,
                    "negative indexing is not supported",
                    hint="Lua tables have no negative indices; use t[len(t) - 1] instead.",
                )
            return lua.Num(node.value + 1)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            self.fail(node, "negative indexing is not supported")
        return lua.BinOp("+", self.expr(node), lua.Num(1))

    # ----------------------------------------------------------------- calls

    def call(self, node: ast.Call) -> lua.Expr:
        if node.keywords:
            self.fail(node, "keyword arguments are not supported in a script body")
        args = tuple(self.expr(a) for a in node.args)

        match node.func:
            case ast.Name(id="len"):
                if len(args) != 1:
                    self.fail(node, "len() takes exactly one argument")
                return lua.UnOp("#", args[0])
            case ast.Name(id=name) if name in _BUILTIN_FUNCS:
                return lua.Call(lua.Name(_BUILTIN_FUNCS[name]), args)
            case ast.Attribute(value=ast.Name(id=recv), attr=attr):
                return self.namespace_call(node, recv, attr, args)
            case ast.Attribute(attr=attr):
                self.fail(
                    node,
                    f"method call .{attr}() is not supported",
                    hint="Only the redis and cjson namespaces, and list.append(), are available.",
                )
            case ast.Name(id=name):
                self.fail(
                    node,
                    f"{name}() is not available inside a script",
                    hint="A script cannot call Python functions; only Redis commands "
                    "and a small set of builtins.",
                )
            case _:
                self.fail(node, "unsupported call target")

    def namespace_call(
        self, node: ast.Call, recv: str, attr: str, args: tuple[lua.Expr, ...]
    ) -> lua.Expr:
        kind = self.receiver_kind(node, recv)
        if kind == "cjson":
            if attr not in {"encode", "decode"}:
                self.fail(node, f"cjson has no {attr!r} function")
            return lua.Call(lua.Index(lua.Name("cjson"), lua.Str(attr)), args)
        if kind == "redis":
            return self.redis_call(node, attr, args)
        self.fail(
            node,
            f"method call .{attr}() is not supported",
            hint="Only the redis and cjson namespaces, and list.append(), are available.",
        )

    def namespace_kind(self, name: str) -> str | None:
        """The namespace a bare name refers to, without any error reporting."""
        if name in self.known:
            return None
        value = self.globalns.get(name, _UNBOUND)
        if isinstance(value, _Namespace):
            return value.kind
        return _RECEIVER_FALLBACK.get(name) if value is _UNBOUND else None

    def receiver_kind(self, node: ast.Call, name: str) -> str | None:
        """Work out what the receiver of an attribute call refers to.

        Resolution is by value, through the globals of the module that defined
        the script, so the namespace works under any alias. Only a name bound
        to nothing at all falls back to the conventional spellings.
        """
        value = self.globalns.get(name, _UNBOUND)
        if isinstance(value, _Namespace):
            return value.kind
        if value is _UNBOUND:
            return _RECEIVER_FALLBACK.get(name)
        if _is_redis_py(value):
            # Silently compiling this would aim the script at the client
            # library, which is the one mistake this whole design invites.
            self.fail(
                node,
                f"{name!r} is bound to redis-py here, not to the script namespace",
                hint="Import the namespace under another name "
                "(from redis_lua_py import redis as r), or the client under "
                "another name (import redis as redis_client).",
            )
        return None

    def redis_call(self, node: ast.Call, attr: str, args: tuple[lua.Expr, ...]) -> lua.Expr:
        if attr in _REDIS_DIRECT:
            return lua.Call(lua.Index(lua.Name("redis"), lua.Str(attr)), args)
        if attr.startswith("_"):
            self.fail(node, f"redis.{attr} is not a Redis command")
        tokens = tuple(lua.Str(token) for token in self.command_tokens(node, attr))
        return lua.Call(lua.Index(lua.Name("redis"), lua.Str("call")), tokens + args)

    def command_tokens(self, node: ast.Call, attr: str) -> tuple[str, ...]:
        """Turn an attribute name into the command tokens Redis expects.

        Checked against the command table rather than uppercased and hoped
        for. Uppercasing alone turns any attribute into a plausible command:
        ``redis.delete(k)`` reads correctly to anyone who knows redis-py, where
        the method really is ``.delete()``, and compiles to a DELETE that Redis
        does not have. Nothing would then catch it until that branch first ran
        -- inside a script whose whole purpose was to be atomic.
        """
        alias = _COMMAND_ALIASES.get(attr)
        if alias is not None:
            return alias

        upper = attr.upper()
        if upper in COMMANDS and upper not in SUBCOMMANDS:
            # SORT_RO and the other read-only variants carry a real underscore;
            # splitting would make the RO a stray argument.
            return (upper,)

        # `zrangebyscore` -> ZRANGEBYSCORE; `script_load` -> SCRIPT LOAD.
        tokens = tuple(part for part in upper.split("_") if part)
        head, rest = tokens[0], tokens[1:]
        if head in SUBCOMMANDS:
            return (head, *self.subcommand_tokens(node, attr, head, rest))
        if head in COMMANDS:
            # Any further tokens stay literal arguments, which is how the
            # subcommands of a non-container command are written: DEBUG OBJECT.
            return tokens
        if "-".join(tokens) in COMMANDS:
            return ("-".join(tokens),)
        self.fail(
            node,
            f"Redis has no {' '.join(tokens)} command",
            hint=_did_you_mean(attr, _COMMAND_SPELLINGS)
            or f"If it comes from a module or a newer server, call it by name: "
            f"redis.call('{' '.join(tokens)}', ...), which is never checked.",
        )

    def subcommand_tokens(
        self, node: ast.Call, attr: str, head: str, rest: tuple[str, ...]
    ) -> tuple[str, ...]:
        valid = SUBCOMMANDS[head]
        if not rest:
            self.fail(
                node,
                f"{head} is a container command and needs a subcommand",
                hint=f"Write redis.{attr}_<subcommand>(...), one of: "
                f"{_listing(sorted(name.lower() for name in valid))}",
            )
        # CLIENT NO-EVICT and MEMORY MALLOC-STATS are hyphenated, which an
        # attribute name cannot carry. The joined spelling is tried first so
        # that the hyphen wins over a same-named single-word subcommand.
        joined = "-".join(rest)
        if joined in valid:
            return (joined,)
        if rest[0] in valid:
            return rest
        self.fail(
            node,
            f"{head} has no {rest[0]} subcommand",
            hint=_did_you_mean(
                joined.lower().replace("-", "_"),
                {name.lower().replace("-", "_") for name in valid},
                prefix=f"{head.lower()}_",
            ),
        )

    # ------------------------------------------------------------ conditions

    def condition(self, node: ast.expr) -> lua.Expr:
        """Compile an expression used for its truth value.

        Lua treats 0 and '' as true, so anything that is not already a boolean
        gets routed through the __truthy helper.
        """
        match node:
            case ast.Compare():
                return self.compare(node)
            case ast.BoolOp(op=op, values=values):
                lua_op = "and" if isinstance(op, ast.And) else "or"
                result = self.condition(values[0])
                for value in values[1:]:
                    result = lua.BinOp(lua_op, result, self.condition(value))
                return result
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return lua.UnOp("not", self.condition(operand))
            case ast.Constant(value=bool() as v):
                return lua.Bool(v)
            case _:
                self.needs_truthy = True
                return lua.Call(lua.Name("__truthy"), (self.expr(node),))

    # ----------------------------------------------------------- statements

    def block(self, body: list[ast.stmt]) -> list[lua.Stat]:
        out: list[lua.Stat] = []
        reachable = True
        for stmt in body:
            # Everything is still compiled, so a mistake is reported wherever it
            # sits. But Lua refuses to load a statement after `return`, and
            # Python would never run one, so the unreachable tail is dropped.
            compiled = self.stmt(stmt)
            if reachable:
                out.extend(compiled)
            if isinstance(stmt, ast.Return | ast.Break):
                reachable = False
        return out

    def stmt(self, node: ast.stmt) -> list[lua.Stat]:
        match node:
            case ast.Pass():
                return []
            case ast.Expr(value=ast.Constant(value=str())):
                return []  # a stray string literal, e.g. a docstring
            case ast.Expr(value=ast.Call() as inner):
                return self.call_statement(inner)
            case ast.Expr():
                self.fail(node, "this expression has no effect in Lua")
            case ast.Assign(targets=targets, value=value):
                if len(targets) != 1:
                    self.fail(node, "chained assignment (a = b = c) is not supported")
                return self.assign(node, targets[0], value)
            case ast.AnnAssign(target=target, value=value):
                if value is None:
                    self.fail(node, "a bare annotation declares nothing in Lua")
                return self.assign(node, target, value)
            case ast.AugAssign(target=target, op=op, value=value):
                synthetic = ast.BinOp(left=target, op=op, right=value)
                ast.copy_location(synthetic, node)
                return self.assign(node, target, synthetic, augmented=True)
            case ast.Return(value=None):
                return [lua.Return(None)]
            case ast.Return(value=ast.expr() as value):
                rendered = self.expr(value)
                self.check_reply(value, rendered)
                return [lua.Return(rendered)]
            case ast.If():
                return [self.if_stmt(node)]
            case ast.For():
                return [self.for_stmt(node)]
            case ast.While(test=test, orelse=orelse, body=body):
                if orelse:
                    self.fail(node, "while/else is not supported")
                return [lua.While(self.condition(test), self.block(body))]
            case ast.Break():
                return [lua.Break()]
            case ast.Continue():
                self.fail(
                    node,
                    "Lua 5.1 has no 'continue' statement",
                    hint="Invert the condition and put the rest of the loop body inside the if.",
                )
            case ast.Assert():
                self.fail(
                    node,
                    "assert is not supported",
                    hint="Return redis.error_reply('...') to signal failure to the caller.",
                )
            case ast.Try() | ast.Raise():
                self.fail(
                    node,
                    "exception handling is not supported",
                    hint="Use redis.pcall() and check the result for an 'err' field.",
                )
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef() | ast.Lambda():
                self.fail(node, "a script cannot define nested functions or classes")
            case ast.Import() | ast.ImportFrom():
                self.fail(node, "a script cannot import anything")
            case ast.With() | ast.AsyncWith():
                self.fail(node, "'with' is not supported")
            case ast.Global() | ast.Nonlocal():
                self.fail(node, "'global' and 'nonlocal' are not supported")
            case _:
                self.fail(node, f"{type(node).__name__} statements are not supported")

    def check_reply(self, node: ast.expr, rendered: lua.Expr) -> None:
        """Refuse a returned table that is known to hold a nil.

        Redis converts a returned Lua array by walking it until the first nil
        and stopping there, so the reply is silently cut short rather than
        carrying a null in the middle. A literal nil in the table is never what
        the author meant, and it is invisible at the call site: the caller gets
        a shorter list, not a null.
        """
        if not isinstance(node, ast.List | ast.Tuple) or not isinstance(rendered, lua.Table):
            return
        for element, compiled in zip(node.elts, rendered.array, strict=True):
            if isinstance(compiled, lua.Nil):
                self.fail(
                    element,
                    "a nil in a returned table truncates the reply at that point",
                    hint="Redis stops converting the array at the first nil, so the caller "
                    "sees a shorter list rather than a null. Return a placeholder value "
                    "such as 0 or '' instead.",
                )

    def call_statement(self, node: ast.Call) -> list[lua.Stat]:
        # `items.append(x)` is the one method call worth special-casing: it is
        # how you build a return value, and Lua spells it t[#t + 1] = x.
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.known
        ):
            if len(node.args) != 1:
                self.fail(node, "append() takes exactly one argument")
            target = self.expr(node.func.value)
            slot = lua.Index(target, lua.BinOp("+", lua.UnOp("#", target), lua.Num(1)))
            return [lua.Assign([slot], [self.expr(node.args[0])])]
        return [lua.ExprStat(self.call(node))]

    def assign(
        self, node: ast.stmt, target: ast.expr, value: ast.expr, *, augmented: bool = False
    ) -> list[lua.Stat]:
        rhs = self.expr(value)
        match target:
            case ast.Name(id=name):
                if not lua.is_identifier(name):
                    self.fail(target, f"{name!r} is a reserved word in Lua")
                if not augmented and id(node) in self.simple_assigns:
                    return [lua.Local([name], [rhs])]
                return [lua.Assign([lua.Name(name)], [rhs])]
            case ast.Subscript():
                return [lua.Assign([self.subscript(target)], [rhs])]
            case ast.Tuple() | ast.List():
                self.fail(target, "tuple unpacking is not supported")
            case _:
                self.fail(target, "unsupported assignment target")

    def if_stmt(self, node: ast.If) -> lua.If:
        branches = [(self.condition(node.test), self.block(node.body))]
        orelse = node.orelse
        # Collapse `else: if ...` chains into Lua's elseif.
        while len(orelse) == 1 and isinstance(orelse[0], ast.If):
            nested = orelse[0]
            branches.append((self.condition(nested.test), self.block(nested.body)))
            orelse = nested.orelse
        return lua.If(branches, self.block(orelse))

    def for_stmt(self, node: ast.For) -> lua.Stat:
        if node.orelse:
            self.fail(node, "for/else is not supported")
        if not isinstance(node.target, ast.Name):
            self.fail(node.target, "only a single loop variable is supported")
        var = node.target.id
        self.known.add(var)

        if (
            isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Name)
            and node.iter.func.id == "range"
        ):
            return self.range_loop(node, var, node.iter)

        # Iterating a table: bind the sequence once, then walk it by index so
        # that a call in the iterable is not re-evaluated every step.
        iterable = self.expr(node.iter)
        self._temp += 1
        idx = f"__i{self._temp}"

        # Bind the iterable to a temporary so a call is not re-evaluated on
        # every step. A plain name is already stable, so leave it alone.
        prefix: list[lua.Stat] = []
        if isinstance(iterable, lua.Name):
            seq: lua.Expr = iterable
        else:
            seq = lua.Name(f"__seq{self._temp}")
            prefix = [lua.Local([f"__seq{self._temp}"], [iterable])]

        body: list[lua.Stat] = [
            lua.Local([var], [lua.Index(seq, lua.Name(idx))]),
            *self.block(node.body),
        ]
        return _Block([*prefix, lua.NumericFor(idx, lua.Num(1), lua.UnOp("#", seq), None, body)])

    def range_loop(self, node: ast.For, var: str, call: ast.Call) -> lua.Stat:
        args = call.args
        if not 1 <= len(args) <= 3:
            self.fail(call, "range() takes one to three arguments")

        step: lua.Expr | None = None
        descending = False
        if len(args) == 3:
            step_value = _literal_int(args[2])
            if step_value is None:
                self.fail(args[2], "range() step must be an integer literal")
            if step_value == 0:
                self.fail(args[2], "range() step cannot be zero")
            descending = step_value < 0
            step = lua.Num(step_value)

        if len(args) == 1:
            start: lua.Expr = lua.Num(0)
            stop_node = args[0]
        else:
            start = self.expr(args[0])
            stop_node = args[1]

        # Python's range excludes the stop value; Lua's numeric for includes it.
        stop = self._offset(self.expr(stop_node), 1 if descending else -1)
        return lua.NumericFor(var, start, stop, step, self.block(node.body))

    @staticmethod
    def _offset(expr: lua.Expr, delta: int) -> lua.Expr:
        if isinstance(expr, lua.Num) and isinstance(expr.value, int):
            return lua.Num(expr.value + delta)
        return lua.BinOp("+" if delta > 0 else "-", expr, lua.Num(abs(delta)))


def _unassigned_in_returns(
    body: list[ast.stmt], assigned: set[str], tracked: set[str]
) -> list[tuple[ast.expr, str]]:
    """Names returned inside a table that are not assigned on every path.

    The mirror image of the hoisting the compiler already does. Hoisting keeps
    a name assigned in one branch readable after the block -- but on a path
    where the assignment did not run, the name is nil, and a nil inside a
    returned table truncates the reply there. That is invisible at the call
    site, so it is worth pointing at.

    Only names the body assigns somewhere are tracked; a name it never assigns
    is a compile error already. Branches that always leave are excluded from
    the intersection, so `if not x: return 0` really does establish x below.
    """
    found: list[tuple[ast.expr, str]] = []

    def walk(statements: list[ast.stmt], live: set[str]) -> bool:
        """Compile-time flow over one block; True if it always leaves."""
        for statement in statements:
            match statement:
                case ast.Return(value=ast.List(elts=elts) | ast.Tuple(elts=elts)):
                    found.extend(
                        (element, element.id)
                        for element in elts
                        if isinstance(element, ast.Name)
                        and element.id in tracked
                        and element.id not in live
                    )
                    return True
                case ast.Return() | ast.Break():
                    return True
                case ast.Assign(targets=targets):
                    live.update(t.id for t in targets if isinstance(t, ast.Name))
                case (
                    ast.AnnAssign(target=ast.Name(id=name))
                    | ast.AugAssign(target=ast.Name(id=name))
                ):
                    live.add(name)
                case ast.If(body=then, orelse=otherwise):
                    surviving = []
                    branch = set(live)
                    if not walk(then, branch):
                        surviving.append(branch)
                    if otherwise:
                        other = set(live)
                        if not walk(otherwise, other):
                            surviving.append(other)
                    else:
                        # The absent else is a path, and it assigns nothing.
                        surviving.append(set(live))
                    if not surviving:
                        return True
                    # Replaced, not intersected in place: every surviving set
                    # starts as a copy of `live`, so the names they agree on
                    # are exactly what is assigned after the block.
                    live.clear()
                    live.update(set.intersection(*surviving))
                case ast.For(target=ast.Name(id=name), body=inner):
                    # A loop may run zero times, so nothing it assigns is live
                    # after it -- including the loop variable, which in Lua
                    # does not outlive the loop at all.
                    walk(inner, set(live) | {name})
                case ast.While(body=inner) | ast.For(body=inner):
                    walk(inner, set(live))
        return False

    walk(body, set(assigned))
    return found


class _Block(lua.Stat):
    """Several statements where the grammar expects one."""

    __slots__ = ("body",)

    def __init__(self, body: list[lua.Stat]) -> None:
        self.body = body


#: Every spelling the compiler accepts for a command, for suggesting a near
#: miss. Container commands are left out: they need a subcommand, and the
#: subcommand error says so more precisely.
_COMMAND_SPELLINGS = {name.lower() for name in COMMANDS if name not in SUBCOMMANDS} | set(
    _COMMAND_ALIASES
)


def _listing(names: list[str], limit: int = 6) -> str:
    """A comma-separated sample, only trailing off when there is more."""
    shown = ", ".join(names[:limit])
    return f"{shown}, ..." if len(names) > limit else shown


def _did_you_mean(attr: str, options: set[str], prefix: str = "") -> str | None:
    """A hint naming the closest spelling, when there is a close one."""
    matches = difflib.get_close_matches(attr, options, n=1, cutoff=0.7)
    if not matches:
        return None
    return f"Did you mean redis.{prefix}{matches[0]}()?"


def _dotted_name(node: ast.expr) -> tuple[str, ...] | None:
    """The parts of a dotted name, if that is all the expression is."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def _as_literal(value: object) -> lua.Expr | None:
    """The Lua literal for a Python constant, or None if it has none.

    ``bool`` is checked before ``int`` because it is one, and normalising
    through ``int()``/``str()`` keeps subclasses -- an ``IntEnum`` member, say
    -- from emitting their ``repr``.
    """
    match value:
        case bool():
            return lua.Bool(value)
        case int():
            return lua.Num(int(value))
        case float():
            return lua.Num(float(value))
        case str():
            return lua.Str(str(value))
        case bytes():
            return lua.Bytes(bytes(value))
        case None:
            return lua.Nil()
        case _:
            return None


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _literal_int(node: ast.expr) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal_int(node.operand)
        return None if inner is None else -inner
    return None


def _flatten(body: list[lua.Stat]) -> list[lua.Stat]:
    out: list[lua.Stat] = []
    for stat in body:
        if isinstance(stat, _Block):
            out.extend(_flatten(stat.body))
        elif isinstance(stat, lua.If):
            stat.branches = [(t, _flatten(b)) for t, b in stat.branches]
            stat.orelse = _flatten(stat.orelse)
            out.append(stat)
        elif isinstance(stat, lua.While | lua.NumericFor):
            stat.body = _flatten(stat.body)
            out.append(stat)
        else:
            out.append(stat)
    return out


def parse_function(func: Callable[..., Any]) -> tuple[ast.FunctionDef, str, int, list[str]]:
    try:
        lines, first_lineno = inspect.getsourcelines(func)
    except (OSError, TypeError) as exc:  # pragma: no cover - needs an exotic environment
        raise CompileError(
            f"cannot read the source of {func.__name__!r}. A script must be defined in a "
            "file on disk, not in a REPL or an exec() string."
        ) from exc

    source = textwrap.dedent("".join(lines))
    module = ast.parse(source)
    node = module.body[0]
    if not isinstance(node, ast.FunctionDef):
        raise CompileError(f"@script can only be applied to a function, got {type(node).__name__}")
    node.decorator_list = []
    filename = inspect.getsourcefile(func) or "<unknown>"
    return node, filename, first_lineno, source.splitlines()


@cache
def _project_root(directory: Path) -> Path | None:
    """The nearest ancestor that looks like the top of a project."""
    for candidate in (directory, *directory.parents):
        if any((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate
    return None


def provenance(filename: str) -> str:
    """The source path as the generated header should record it.

    Repo-relative, because the header is part of the script body and the body
    is what EVALSHA hashes. An absolute path would give the same script a
    different SHA on a laptop, in CI and in a container -- so the server's
    script cache would be cold once per environment rather than once per
    script -- and would put build-machine paths on the Redis server, where
    they show up in SCRIPT output.
    """
    path = Path(filename)
    if not path.is_absolute():
        return filename
    root = _project_root(path.parent)
    return path.name if root is None else path.relative_to(root).as_posix()


def compile_function(
    func: Callable[..., R], *, name: str | None = None, header: bool = True
) -> CompiledScript[R]:
    node, filename, first_lineno, lines = parse_function(func)
    compiler = _Compiler(
        node,
        filename=filename,
        first_lineno=first_lineno,
        lines=lines,
        # Receivers are resolved against the defining module, so the namespace
        # is recognised under whatever name it was imported as.
        globalns=getattr(func, "__globals__", {}),
    )

    prelude = compiler.compile_signature()
    compiler.collect_assigned()

    body = node.body
    doc = ast.get_docstring(node)
    if doc is not None:
        body = body[1:]

    statements = _flatten(compiler.block(body))
    if compiler.hoisted:
        prelude.append(lua.Local(sorted(compiler.hoisted), []))

    for element, unassigned in _unassigned_in_returns(body, set(compiler.params), compiler.known):
        warnings.warn_explicit(
            render_location(
                f"{unassigned!r} is not assigned on every path to this return, and a nil "
                "in a returned table truncates the reply there",
                filename=filename,
                lineno=first_lineno + element.lineno - 1,
                col=element.col_offset,
                source_line=lines[element.lineno - 1] if element.lineno <= len(lines) else None,
                hint="Give it a value before the branch, so that every branch returns a "
                "table of the same shape.",
            ),
            NilTruncationWarning,
            filename,
            first_lineno + element.lineno - 1,
        )

    parts: list[str] = []
    if header:
        parts.append(
            f"-- {name or func.__name__}\n"
            f"-- Generated by redis-lua-py from "
            f"{provenance(filename)}:{first_lineno}. Do not edit."
        )
    if compiler.needs_truthy:
        parts.append(_TRUTHY_HELPER.rstrip())
    if compiler.needs_isnil:
        parts.append(_ISNIL_HELPER.rstrip())
    parts.append(lua.emit(prelude + statements).rstrip())

    return CompiledScript(
        name=name or func.__name__,
        lua="\n".join(parts) + "\n",
        params=tuple(compiler.params),
        keys=tuple(compiler.keys),
        args=tuple(compiler.args),
        doc=doc,
        source=f"{filename}:{first_lineno}",
    )
