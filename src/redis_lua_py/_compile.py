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
import math
import textwrap
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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

# Functions of the math module with a Lua counterpart of the same meaning.
# math.log is handled apart, since only its one-argument form has one.
_MATH_FUNCS: dict[str, str] = {
    "floor": "math.floor",
    "ceil": "math.ceil",
    "sqrt": "math.sqrt",
    "fabs": "math.abs",
    "fmod": "math.fmod",
    "exp": "math.exp",
    "log10": "math.log10",
    "pow": "math.pow",
}

# The same functions by value, so `from math import floor` resolves too.
_MATH_BY_OBJECT: dict[object, str] = {getattr(math, name): name for name in [*_MATH_FUNCS, "log"]}

_METHOD_HINT = (
    "Only the redis and cjson namespaces, list.append(), list.insert(), list.pop() "
    "and str.join() are available."
)

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

_OR_HELPER = """\
-- Python's `a or b`: a when it is truthy by Python's rules, otherwise b.
local function __or(a, b)
  if __truthy(a) then return a end
  return b
end
"""

_AND_HELPER = """\
-- Python's `a and b`: b when a is truthy by Python's rules, otherwise a.
local function __and(a, b)
  if __truthy(a) then return b end
  return a
end
"""

_ERRMSG_HELPER = """\
-- The message of a caught error. Redis hands pcall a string, but a table with
-- an err field or a userdata are possible too, so each is turned into one.
local function __errmsg(e)
  if type(e) == 'table' and e.err ~= nil then return e.err end
  return tostring(e)
end
"""


@dataclass
class _Loop:
    """The loop a break or continue belongs to."""

    #: Set when the loop also uses continue: a break then has to leave the
    #: `repeat ... until true` block first, and says so through this flag.
    break_flag: str | None


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
        self.needs_or = False
        self.needs_and = False
        self.variadic_key: str | None = None
        self.variadic_arg: str | None = None
        # Helpers defined in the body, which calls resolve to.
        self.local_functions: set[str] = set()
        # Every name the body assigns, and the statement that assigns it first.
        self.assigned: set[str] = set()
        self.first_assignment: dict[str, ast.stmt] = {}
        # The names a statement assigns for the first time, by statement id.
        self.first_names: dict[int, list[str]] = {}
        self.needs_errmsg = False
        # What a break or continue would leave: a loop, or None for the body of
        # a try, which runs as a function and so is out of any loop's reach.
        self.flow: list[_Loop | None] = []
        # How deep inside try bodies we are, where a return has to say so.
        self.protected_depth = 0
        # The error variable of each enclosing except block, for a bare raise.
        self.handlers: list[str] = []
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

        # A helper function has a scope of its own, compiled separately, so
        # what it assigns is not the script's business.
        for node in _walk_scope(self.func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in _target_names(target):
                        record(name, node)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(
                node.target, ast.Name
            ):
                record(node.target.id, node)
            elif isinstance(node, ast.For):
                # Loop targets get Lua's own loop scope; only record them so
                # that reads of the name resolve.
                self.known.update(_target_names(node.target))
            elif isinstance(node, ast.FunctionDef):
                self.known.add(node.name)
                self.local_functions.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                # Bound as a local inside the except block itself.
                self.known.add(node.name)

        by_id: dict[int, ast.stmt] = {}
        for name, assignments in nodes.items():
            self.known.add(name)
            self.assigned.add(name)
            if name in self.params:
                # Already a local from the prelude. Declaring it again would
                # shadow the argument with nil.
                continue
            # ast.walk is breadth-first, so ask for source order explicitly.
            first = min(assignments, key=lambda n: (n.lineno, n.col_offset))
            self.first_assignment[name] = first
            self.first_names.setdefault(id(first), []).append(name)
            by_id[id(first)] = first

        top_level = {id(stmt) for stmt in self.func.body}
        for node_id, names in self.first_names.items():
            # `local a, b = ...` only works when the statement introduces
            # every name it assigns; otherwise the new ones are declared up
            # front and the statement becomes a plain assignment.
            if node_id in top_level and _declares_exactly(by_id[node_id], names):
                self.simple_assigns.add(node_id)
            else:
                self.hoisted.extend(names)

    # -------------------------------------------------------------- signature

    def compile_signature(self) -> list[lua.Stat]:
        sig = self.func.args
        if sig.vararg or sig.kwarg:
            self.fail(
                self.func,
                "*args and **kwargs are not supported in a script signature",
                hint="Annotate one parameter as list[Key] for a variable number of keys, "
                "or as list[str] for a variable number of arguments.",
            )
        if sig.posonlyargs:
            self.fail(self.func, "positional-only parameters are not supported")

        prelude: list[lua.Stat] = []
        for arg in [*sig.args, *sig.kwonlyargs]:
            name = arg.arg
            if not lua.is_identifier(name):
                self.fail(arg, f"{name!r} is a reserved word in Lua")
            self.params.append(name)
            self.known.add(name)

            item = self._list_item(arg.annotation)
            if item is not None:
                # KEYS and ARGV have no names, only positions, so a list can
                # only take whatever is left after the fixed parameters: one
                # list of keys, and one list of arguments.
                if self._is_key(item):
                    if self.variadic_key is not None:
                        self.fail(arg, "only one list[Key] parameter is supported")
                    self.variadic_key = name
                else:
                    if self.variadic_arg is not None:
                        self.fail(arg, "only one list parameter of arguments is supported")
                    self.variadic_arg = name
                    if self._is_numeric(item):
                        self.numeric_args.add(name)
                continue

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

        if self.variadic_key is not None:
            self.keys.append(self.variadic_key)
            prelude += self.rest_of("KEYS", self.variadic_key, len(self.keys), numeric=False)
        if self.variadic_arg is not None:
            self.args.append(self.variadic_arg)
            prelude += self.rest_of(
                "ARGV",
                self.variadic_arg,
                len(self.args),
                numeric=self.variadic_arg in self.numeric_args,
            )

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

    @staticmethod
    def _list_item(node: ast.expr | None) -> ast.expr | None:
        """The element annotation of a ``list[...]`` parameter, if it is one."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(node, ast.Subscript) and _Compiler._annotation_name(node.value) in {
            "list",
            "List",
            "Sequence",
        }:
            return node.slice
        return None

    def rest_of(self, table: str, name: str, start: int, *, numeric: bool) -> list[lua.Stat]:
        """Collect KEYS or ARGV from ``start`` onwards into a local table.

        A loop rather than ``{unpack(ARGV, start)}``, which would put every
        element on the Lua stack at once and fail past a few thousand.
        """
        self._temp += 1
        idx = f"__i{self._temp}"
        item: lua.Expr = lua.Index(lua.Name(table), lua.Name(idx))
        if numeric:
            item = lua.Call(lua.Name("tonumber"), (item,))
        target = lua.Name(name)
        slot = lua.Index(target, lua.BinOp("+", lua.UnOp("#", target), lua.Num(1)))
        return [
            lua.Local([name], [lua.Table()]),
            lua.NumericFor(
                idx,
                lua.Num(start),
                lua.UnOp("#", lua.Name(table)),
                None,
                [lua.Assign([slot], [item])],
            ),
        ]

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
                return self.boolop_value(node)
            case ast.IfExp():
                return self.ifexp(node)
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
        args = self.call_args(node)

        math_attr = self.math_attr(node.func)
        if math_attr is not None:
            return self.math_call(node, math_attr, args)

        match node.func:
            case ast.Name(id=name) if name in self.local_functions:
                return lua.Call(lua.Name(name), args)
            case ast.Name(id="len"):
                if len(args) != 1:
                    self.fail(node, "len() takes exactly one argument")
                return lua.UnOp("#", args[0])
            case ast.Name(id=name) if name in _BUILTIN_FUNCS:
                return lua.Call(lua.Name(_BUILTIN_FUNCS[name]), args)
            case ast.Attribute(value=receiver, attr="join") if not self.is_namespace(receiver):
                if len(args) != 1:
                    self.fail(node, "join() takes exactly one argument")
                return lua.Call(lua.Name("table.concat"), (args[0], self.expr(receiver)))
            case ast.Attribute(value=ast.Name(id=recv) as receiver, attr="pop" | "insert") if (
                recv in self.known
            ):
                return self.list_method(node, receiver)
            case ast.Attribute(value=ast.Name(id=recv), attr=attr):
                return self.namespace_call(node, recv, attr, args)
            case ast.Attribute(attr=attr):
                self.fail(node, f"method call .{attr}() is not supported", hint=_METHOD_HINT)
            case ast.Name(id=name):
                self.fail(
                    node,
                    f"{name}() is not available inside a script",
                    hint="A script cannot call Python functions from outside it; define a "
                    "helper inside the body, or use a Redis command or a builtin.",
                )
            case _:
                self.fail(node, "unsupported call target")

    def call_args(self, node: ast.Call) -> tuple[lua.Expr, ...]:
        """Compile call arguments, turning a trailing ``*xs`` into ``unpack(xs)``."""
        args: list[lua.Expr] = []
        for position, arg in enumerate(node.args):
            if isinstance(arg, ast.Starred):
                if position != len(node.args) - 1:
                    self.fail(
                        arg,
                        "a starred argument must be the last one",
                        hint="Lua's unpack() only expands in the last position. Append the "
                        "trailing values to the list first, then splat it.",
                    )
                args.append(lua.Call(lua.Name("unpack"), (self.expr(arg.value),)))
            else:
                args.append(self.expr(arg))
        return tuple(args)

    def is_namespace(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and self.namespace_kind(node.id) is not None

    def math_attr(self, func: ast.expr) -> str | None:
        """The math function a call target names, as `math.floor` or a bare `floor`."""
        if isinstance(func, ast.Name) and func.id not in self.known:
            value = self.globalns.get(func.id, _UNBOUND)
            try:
                return _MATH_BY_OBJECT.get(value)
            except TypeError:  # an unhashable module global
                return None
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id not in self.known
            and self.globalns.get(func.value.id) is math
        ):
            return func.attr
        return None

    def math_call(self, node: ast.Call, attr: str, args: tuple[lua.Expr, ...]) -> lua.Expr:
        if attr == "log":
            if len(args) != 1:
                self.fail(
                    node,
                    "math.log() with a base is not supported",
                    hint="Lua 5.1's math.log takes no base; divide by math.log(base) instead.",
                )
            return lua.Call(lua.Name("math.log"), args)
        target = _MATH_FUNCS.get(attr)
        if target is None:
            self.fail(
                node,
                f"math.{attr}() has no Lua counterpart",
                hint=f"Available: {_listing(sorted([*_MATH_FUNCS, 'log']), limit=10)}.",
            )
        return lua.Call(lua.Name(target), args)

    def list_method(self, node: ast.Call, receiver: ast.Name) -> lua.Expr:
        """``xs.pop()`` and ``xs.insert(i, x)``, with the index shifted to 1-based."""
        assert isinstance(node.func, ast.Attribute)
        target = self.expr(receiver)
        if node.func.attr == "pop":
            if not node.args:
                return lua.Call(lua.Name("table.remove"), (target,))
            if len(node.args) == 1:
                return lua.Call(lua.Name("table.remove"), (target, self.index(node.args[0])))
            self.fail(node, "pop() takes at most one argument")
        if len(node.args) != 2:
            self.fail(node, "insert() takes exactly two arguments")
        return lua.Call(
            lua.Name("table.insert"),
            (target, self.index(node.args[0]), self.expr(node.args[1])),
        )

    def boolop_value(self, node: ast.BoolOp) -> lua.Expr:
        """``a or b`` used for its value, with Python's truthiness and laziness.

        Lua's own and/or test Lua truthiness, where 0 and '' are true, so the
        idiomatic ``tonumber(x) or 0`` would mean something else. A right side
        that is a name or a literal goes through a helper; anything else is
        wrapped in a function, so it only runs when Python would run it.
        """
        is_or = isinstance(node.op, ast.Or)
        self.needs_truthy = True
        result = self.expr(node.values[0])
        for value in node.values[1:]:
            right = self.expr(value)
            if _is_cheap(value):
                if is_or:
                    self.needs_or = True
                else:
                    self.needs_and = True
                result = lua.Call(lua.Name("__or" if is_or else "__and"), (result, right))
                continue
            self._temp += 1
            operand = f"__v{self._temp}"
            test: lua.Expr = lua.Call(lua.Name("__truthy"), (lua.Name(operand),))
            if not is_or:
                test = lua.UnOp("not", test)
            body = (lua.If([(test, [lua.Return(lua.Name(operand))])]), lua.Return(right))
            result = lua.Call(lua.Function((operand,), body), (result,))
        return result

    def ifexp(self, node: ast.IfExp) -> lua.Expr:
        """``a if c else b``, which Lua 5.1 has no expression for.

        ``c and a or b`` is exactly right when ``a`` cannot be false or nil,
        which a literal cannot; otherwise the branches go in a function.
        """
        test = self.condition(node.test)
        then, otherwise = self.expr(node.body), self.expr(node.orelse)
        if isinstance(then, lua.Num | lua.Str | lua.Bytes | lua.Table) or then == lua.Bool(True):
            return lua.BinOp("or", lua.BinOp("and", test, then), otherwise)
        body = (lua.If([(test, [lua.Return(then)])]), lua.Return(otherwise))
        return lua.Call(lua.Function((), body), ())

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
            hint=_METHOD_HINT,
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
            if isinstance(stmt, ast.Return | ast.Break | ast.Continue | ast.Raise):
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
                return [self.return_stat(None)]
            case ast.Return(value=ast.expr() as value):
                rendered = self.expr(value)
                self.check_reply(value, rendered)
                return [self.return_stat(rendered)]
            case ast.If():
                return [self.if_stmt(node)]
            case ast.For():
                return [self.for_stmt(node)]
            case ast.While(test=test, orelse=orelse, body=body):
                if orelse:
                    self.fail(node, "while/else is not supported")
                return [lua.While(self.condition(test), self.loop_body(body))]
            case ast.Break():
                loop = self.enclosing_loop(node, "break")
                if loop.break_flag is not None:
                    flag = lua.Name(loop.break_flag)
                    return [lua.Assign([flag], [lua.Bool(True)]), lua.Break()]
                return [lua.Break()]
            case ast.Continue():
                self.enclosing_loop(node, "continue")
                # The body of a loop that continues sits in `repeat ... until
                # true`, so leaving that block is exactly Python's continue.
                return [lua.Break()]
            case ast.Assert(test=test, msg=msg):
                message = lua.Str("AssertionError") if msg is None else self.expr(msg)
                failed = lua.UnOp("not", self.condition(test))
                return [lua.If([(failed, [self.error_stat(message)])])]
            case ast.Raise():
                return [self.raise_stmt(node)]
            case ast.Try():
                return self.try_stmt(node)
            case ast.FunctionDef():
                return [self.nested_function(node)]
            case ast.AsyncFunctionDef() | ast.ClassDef():
                self.fail(node, "a script cannot define async functions or classes")
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
        if isinstance(target, ast.Tuple | ast.List):
            return self.unpack_assign(node, target.elts, value)
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
            case _:
                self.fail(target, "unsupported assignment target")

    def unpack_assign(
        self, node: ast.stmt, elts: list[ast.expr], value: ast.expr
    ) -> list[lua.Stat]:
        """``a, b = ...``: Lua's multiple assignment, which also evaluates first.

        A tuple on the right maps one to one. Anything else is bound once and
        indexed, so ``head, tail = redis.lrange(k, 0, 1)`` runs the command once.
        """
        targets: list[lua.Expr] = []
        for element in elts:
            if isinstance(element, ast.Name):
                if not lua.is_identifier(element.id):
                    self.fail(element, f"{element.id!r} is a reserved word in Lua")
                targets.append(lua.Name(element.id))
            elif isinstance(element, ast.Subscript):
                targets.append(self.subscript(element))
            else:
                self.fail(element, "only names and subscripts can be unpacked into")

        prefix: list[lua.Stat] = []
        if isinstance(value, ast.Tuple | ast.List) and not any(
            isinstance(v, ast.Starred) for v in value.elts
        ):
            if len(value.elts) != len(elts):
                self.fail(node, f"cannot unpack {len(value.elts)} values into {len(elts)} targets")
            values = [self.expr(v) for v in value.elts]
        else:
            self._temp += 1
            bound = f"__t{self._temp}"
            prefix.append(lua.Local([bound], [self.expr(value)]))
            values = [lua.Index(lua.Name(bound), lua.Num(i + 1)) for i in range(len(elts))]

        if id(node) in self.simple_assigns:
            names = [e.id for e in elts if isinstance(e, ast.Name)]
            return [*prefix, lua.Local(names, values)]
        return [*prefix, lua.Assign(targets, values)]

    def nested_function(self, node: ast.FunctionDef) -> lua.Stat:
        """Compile a helper defined in the body to a Lua local function.

        It gets a compiler of its own, so that the names it assigns are local
        to it as they would be in Python, while it can still read the names of
        the script around it, call other helpers, and call itself.
        """
        if not any(stmt is node for stmt in self.func.body):
            self.fail(
                node,
                "a helper function must be defined at the top level of the script body",
                hint="Move the def out of the if or loop it is in.",
            )
        if node.decorator_list:
            self.fail(node, "a helper function cannot be decorated")
        sig = node.args
        if sig.vararg or sig.kwarg or sig.posonlyargs or sig.kwonlyargs or sig.defaults:
            self.fail(
                node,
                "a helper function takes plain positional parameters only",
                hint="Calls inside a script are positional, so defaults, *args and "
                "keyword-only parameters have nothing to bind to.",
            )
        for name in [node.name, *(a.arg for a in sig.args)]:
            if not lua.is_identifier(name):
                self.fail(node, f"{name!r} is a reserved word in Lua")
        params = [a.arg for a in sig.args]

        child = _Compiler(
            node,
            filename=self.filename,
            first_lineno=self.first_lineno,
            lines=self.lines,
            globalns=self.globalns,
        )
        child.params = params
        child.known = self.known | set(params) | {node.name}
        child.local_functions = self.local_functions | {node.name}
        child._temp = self._temp
        child.collect_assigned()

        body = node.body[1:] if ast.get_docstring(node) is not None else node.body
        statements = _flatten(child.block(body))
        if child.hoisted:
            statements.insert(0, lua.Local(sorted(set(child.hoisted)), []))

        self._temp = child._temp
        self.needs_truthy |= child.needs_truthy
        self.needs_isnil |= child.needs_isnil
        self.needs_or |= child.needs_or
        self.needs_and |= child.needs_and
        self.needs_errmsg |= child.needs_errmsg

        # Python resolves a free name when the helper runs; Lua binds it where
        # the function is written. A script name first assigned after the def
        # is declared up front instead, so the helper sees it too.
        reads = {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        for name in reads - set(params) - child.assigned:
            first = self.first_assignment.get(name)
            if first is None or id(first) not in self.simple_assigns or first.lineno < node.lineno:
                continue
            self.simple_assigns.discard(id(first))
            self.hoisted.extend(self.first_names[id(first)])

        return lua.LocalFunction(node.name, params, statements)

    # --------------------------------------------------------- control flow

    def loop_body(
        self, body: list[ast.stmt], binding: list[lua.Stat] | None = None
    ) -> list[lua.Stat]:
        """Compile a loop body, making room for continue when it uses one.

        Lua 5.1 has no continue and no goto. A body that continues runs inside
        `repeat ... until true`, where continue is a break out of that block. A
        break in the same loop sets a flag first, and the loop breaks on it.
        """
        continues = _loop_has(body, ast.Continue)
        flag = None
        if continues and _loop_has(body, ast.Break):
            self._temp += 1
            flag = f"__brk{self._temp}"
        self.flow.append(_Loop(break_flag=flag))
        try:
            inner = self.block(body)
        finally:
            self.flow.pop()

        out = list(binding or [])
        if not continues:
            return out + inner
        if flag is not None:
            out.append(lua.Local([flag], [lua.Bool(False)]))
        out.append(lua.Repeat(inner))
        if flag is not None:
            out.append(lua.If([(lua.Name(flag), [lua.Break()])]))
        return out

    def enclosing_loop(self, node: ast.stmt, keyword: str) -> _Loop:
        loop = self.flow[-1] if self.flow else None
        if loop is None:
            self.fail(
                node,
                f"'{keyword}' cannot leave a try block",
                hint="The try body runs as a function under pcall, so the loop around it "
                "is out of reach. Set a flag inside the try, and act on it after.",
            )
        return loop

    def return_stat(self, value: lua.Expr | None) -> lua.Stat:
        """A return, which inside a try body also has to leave the protected call."""
        if self.protected_depth:
            return lua.Return(lua.Values((lua.Bool(True), value or lua.Nil())))
        return lua.Return(value)

    def error_stat(self, message: lua.Expr) -> lua.Stat:
        """``error(message, 0)``.

        Level 0 keeps Lua's `user_script:12:` position out of the message, so
        the caller, or an except block, sees exactly what was raised.
        """
        if not isinstance(message, lua.Str):
            message = lua.Call(lua.Name("tostring"), (message,))
        return lua.ExprStat(lua.Call(lua.Name("error"), (message, lua.Num(0))))

    def raise_stmt(self, node: ast.Raise) -> lua.Stat:
        """``raise SomeError('message')``, which reaches the caller as an error reply."""
        if node.cause is not None:
            self.fail(
                node,
                "raise ... from ... is not supported",
                hint="An error inside a script carries only a message; raise it on its own.",
            )
        exc = node.exc
        if exc is None:
            if not self.handlers:
                self.fail(node, "a bare raise can only re-raise inside an except block")
            self.needs_errmsg = True
            return self.error_stat(lua.Call(lua.Name("__errmsg"), (lua.Name(self.handlers[-1]),)))
        if isinstance(exc, ast.Call) and not exc.keywords:
            if len(exc.args) > 1:
                self.fail(exc, "an error raised in a script carries a single message")
            if exc.args:
                return self.error_stat(self.expr(exc.args[0]))
            return self.error_stat(lua.Str(_callee_name(exc.func)))
        if isinstance(exc, ast.Name):
            if exc.id in self.known:
                # The name an `except ... as e` bound, which holds a message.
                return self.error_stat(self.expr(exc))
            return self.error_stat(lua.Str(exc.id))
        self.fail(exc, "only raise SomeError('message') is supported")

    def try_stmt(self, node: ast.Try) -> list[lua.Stat]:
        """try/except/else/finally, over pcall.

        The body runs as a local function under pcall, so an error inside it --
        from redis.call, a raise, or Lua itself -- lands in the except block. A
        return inside the body has to leave that function first, so it hands
        back a flag and the value, which are returned again once pcall is done.
        """
        if len(node.handlers) > 1:
            self.fail(
                node.handlers[1],
                "only one except clause is supported",
                hint="An error inside a script carries a message, but no type to tell "
                "clauses apart. Catch Exception, and branch on the message.",
            )
        handler = node.handlers[0] if node.handlers else None
        if handler is not None and handler.type is not None and not _is_catch_all(handler.type):
            self.fail(
                handler.type,
                f"except {ast.unparse(handler.type)} cannot be told apart from any other error",
                hint="An error inside a script carries only a message, so every except "
                "catches everything. Write except Exception, and branch on the message.",
            )

        if node.finalbody:
            # The try/except/else runs protected as a whole, then the finally
            # block, then any error is raised again or the return goes through.
            inner: list[ast.stmt] = node.body if handler is None else [_without_finally(node)]
            setup, ok, result, value, returns = self.protected(inner)
            out = [*setup, *self.block(node.finalbody)]
            self.needs_errmsg = True
            reraise = self.error_stat(lua.Call(lua.Name("__errmsg"), (lua.Name(result),)))
            out.append(lua.If([(lua.UnOp("not", lua.Name(ok)), [reraise])]))
            if returns:
                out.append(lua.If([(lua.Name(result), [self.return_stat(lua.Name(value))])]))
            return out

        assert handler is not None  # a try without finally has a handler
        setup, ok, result, value, returns = self.protected(node.body)
        success: list[lua.Stat] = []
        if returns:
            success.append(lua.If([(lua.Name(result), [self.return_stat(lua.Name(value))])]))
        success += self.block(node.orelse)

        failure: list[lua.Stat] = []
        if handler.name is not None:
            self.needs_errmsg = True
            message = lua.Call(lua.Name("__errmsg"), (lua.Name(result),))
            failure.append(lua.Local([handler.name], [message]))
        self.handlers.append(result)
        try:
            failure += self.block(handler.body)
        finally:
            self.handlers.pop()

        if success:
            return [*setup, lua.If([(lua.Name(ok), success)], failure)]
        return [*setup, lua.If([(lua.UnOp("not", lua.Name(ok)), failure)])]

    def protected(self, body: list[ast.stmt]) -> tuple[list[lua.Stat], str, str, str, bool]:
        """Compile a body into a local function and a pcall of it.

        Returns the statements, the names pcall's results are bound to, and
        whether the body returns, in which case a third result carries the value.
        """
        self._temp += 1
        n = self._temp
        function, ok, result, value = f"__try{n}", f"__ok{n}", f"__r{n}", f"__v{n}"
        self.flow.append(None)
        self.protected_depth += 1
        try:
            inner = self.block(body)
        finally:
            self.protected_depth -= 1
            self.flow.pop()
        returns = _contains_return(body)
        names = [ok, result, value] if returns else [ok, result]
        call = lua.Call(lua.Name("pcall"), (lua.Name(function),))
        return (
            [lua.LocalFunction(function, [], inner), lua.Local(names, [call])],
            ok,
            result,
            value,
            returns,
        )

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
        names = self.loop_names(node.target)
        self.known.update(names)
        it = node.iter

        if (
            isinstance(it, ast.Call)
            and isinstance(it.func, ast.Attribute)
            and it.func.attr in {"items", "keys", "values"}
            and not it.args
            and not self.is_namespace(it.func.value)
        ):
            return self.pairs_loop(node, names, it.func)
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range":
            if len(names) != 1:
                self.fail(node.target, "range() yields one value per step")
            return self.range_loop(node, names[0], it)
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
            return self.enumerate_loop(node, names, it)

        iterable = self.expr(it)
        prefix, seq, idx = self.bind_sequence(iterable)
        element: lua.Expr = lua.Index(seq, lua.Name(idx))
        binding: list[lua.Stat]
        if len(names) == 1:
            binding = [lua.Local(names, [element])]
        else:
            # `for name, n in pairs:` unpacks each element by position.
            self._temp += 1
            item = f"__e{self._temp}"
            binding = [
                lua.Local([item], [element]),
                lua.Local(
                    names, [lua.Index(lua.Name(item), lua.Num(i + 1)) for i in range(len(names))]
                ),
            ]
        body = self.loop_body(node.body, binding)
        return _Block([*prefix, lua.NumericFor(idx, lua.Num(1), lua.UnOp("#", seq), None, body)])

    def loop_names(self, target: ast.expr) -> list[str]:
        if isinstance(target, ast.Name):
            names = [target.id]
        elif isinstance(target, ast.Tuple | ast.List) and all(
            isinstance(e, ast.Name) for e in target.elts
        ):
            names = [e.id for e in target.elts if isinstance(e, ast.Name)]
        else:
            self.fail(target, "a loop can only bind a name, or a flat tuple of names")
        for name in names:
            if not lua.is_identifier(name):
                self.fail(target, f"{name!r} is a reserved word in Lua")
        return names

    def bind_sequence(self, iterable: lua.Expr) -> tuple[list[lua.Stat], lua.Expr, str]:
        """Bind a sequence once, and name an index to walk it with.

        Walking by index, rather than with ipairs, keeps a call in the iterable
        from being re-evaluated on every step. A plain name is already stable,
        so it is left alone.
        """
        self._temp += 1
        idx = f"__i{self._temp}"
        if isinstance(iterable, lua.Name):
            return [], iterable, idx
        seq = f"__seq{self._temp}"
        return [lua.Local([seq], [iterable])], lua.Name(seq), idx

    def pairs_loop(self, node: ast.For, names: list[str], method: ast.Attribute) -> lua.Stat:
        """``d.items()``, ``d.keys()`` and ``d.values()``, over Lua's pairs().

        pairs() visits entries in no particular order, as does Lua itself;
        sort the result if the order reaches the caller.
        """
        wanted = 2 if method.attr == "items" else 1
        if len(names) != wanted:
            shape = "a key and a value" if wanted == 2 else "one value"
            self.fail(node.target, f"{method.attr}() yields {shape} per step")
        table = self.expr(method.value)
        if method.attr == "values":
            self._temp += 1
            names = [f"__k{self._temp}", names[0]]
        iterator = lua.Call(lua.Name("pairs"), (table,))
        return lua.GenericFor(names, iterator, self.loop_body(node.body))

    def enumerate_loop(self, node: ast.For, names: list[str], call: ast.Call) -> lua.Stat:
        if call.keywords or not 1 <= len(call.args) <= 2:
            self.fail(call, "enumerate() takes an iterable and an optional start")
        if len(names) != 2:
            self.fail(node.target, "enumerate() yields an index and a value per step")
        start = _literal_int(call.args[1]) if len(call.args) == 2 else 0
        if start is None:
            self.fail(call.args[1], "enumerate() start must be an integer literal")

        prefix, seq, idx = self.bind_sequence(self.expr(call.args[0]))
        # Lua counts from 1; Python's enumerate counts from `start`.
        position: lua.Expr = lua.Name(idx)
        if start != 1:
            position = self._offset(position, start - 1)
        body = self.loop_body(
            node.body,
            [
                lua.Local([names[0]], [position]),
                lua.Local([names[1]], [lua.Index(seq, lua.Name(idx))]),
            ],
        )
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
        return lua.NumericFor(var, start, stop, step, self.loop_body(node.body))

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
                    live.update(name for t in targets for name in _target_names(t))
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
                case ast.Try(body=inner, handlers=handlers, orelse=orelse, finalbody=final):
                    # Any statement of the try may be the one that failed, so
                    # nothing it assigns counts as established afterwards.
                    walk(inner + orelse, set(live))
                    for handler in handlers:
                        walk(handler.body, set(live))
                    walk(final, set(live))
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


def _walk_scope(root: ast.AST) -> Iterator[ast.AST]:
    """Like ast.walk, but not into the bodies of nested functions or classes."""
    pending = list(ast.iter_child_nodes(root))
    while pending:
        node = pending.pop(0)
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            pending.extend(ast.iter_child_nodes(node))


def _target_names(target: ast.expr) -> list[str]:
    """The names an assignment or loop target binds."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _declares_exactly(node: ast.stmt, names: list[str]) -> bool:
    """True if every target of the statement is a name it assigns first."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Tuple | ast.List):
            return all(isinstance(e, ast.Name) for e in target.elts) and sorted(
                _target_names(target)
            ) == sorted(names)
    return True


def _is_cheap(node: ast.expr) -> bool:
    """An operand that can be evaluated early without changing anything."""
    match node:
        case ast.Constant() | ast.Name():
            return True
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant()):
            return True
        case ast.Attribute():
            return _dotted_name(node) is not None
    return False


def _loop_has(body: list[ast.stmt], kind: type[ast.stmt]) -> bool:
    """True if a break or continue in this body belongs to the loop around it.

    Nested loops own their own, and a nested function cannot reach the loop.
    """
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, kind):
            return True
        if isinstance(
            node, ast.For | ast.While | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


def _contains_return(body: list[ast.stmt]) -> bool:
    """True if a return in this body leaves the function around it."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return):
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


def _is_catch_all(node: ast.expr) -> bool:
    if isinstance(node, ast.Tuple):
        return all(_is_catch_all(element) for element in node.elts)
    return isinstance(node, ast.Name) and node.id in {"Exception", "BaseException"}


def _callee_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else "Exception"


def _without_finally(node: ast.Try) -> ast.Try:
    inner = ast.Try(body=node.body, handlers=node.handlers, orelse=node.orelse, finalbody=[])
    return ast.copy_location(inner, node)


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
        elif isinstance(
            stat, lua.While | lua.NumericFor | lua.GenericFor | lua.LocalFunction | lua.Repeat
        ):
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
        prelude.append(lua.Local(sorted(set(compiler.hoisted)), []))

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
    if compiler.needs_or:
        parts.append(_OR_HELPER.rstrip())
    if compiler.needs_and:
        parts.append(_AND_HELPER.rstrip())
    if compiler.needs_errmsg:
        parts.append(_ERRMSG_HELPER.rstrip())
    parts.append(lua.emit(prelude + statements).rstrip())

    return CompiledScript(
        name=name or func.__name__,
        lua="\n".join(parts) + "\n",
        params=tuple(compiler.params),
        keys=tuple(compiler.keys),
        args=tuple(compiler.args),
        variadic_key=compiler.variadic_key,
        variadic_arg=compiler.variadic_arg,
        doc=doc,
        source=f"{filename}:{first_lineno}",
    )
