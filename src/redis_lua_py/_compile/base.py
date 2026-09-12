"""The compiler's state, and what it knows before compiling an expression.

The compiler is built in layers, each a subclass of the one before:

- ``base``: state, errors, what a name resolves to, and what kind of value an
  expression holds;
- ``scope``: the signature, and which names are hoisted;
- ``expressions``: operators, subscripts, strings and conditions;
- ``calls``: builtins, methods, and Redis commands;
- ``control``: if, loops, try and raise;
- ``statements``: the statements themselves, and the finished ``Compiler``.

A lower layer only calls into a higher one through the abstract methods here.
"""

from __future__ import annotations

import ast
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import ModuleType
from typing import Any, NoReturn

from .. import _lua as lua
from .._runtime import _Namespace
from ..errors import UnsupportedSyntax
from .analysis import UNBOUND, as_literal, binop_kind, dotted_name, value_kind
from .tables import MATH_BY_OBJECT, RECEIVER_FALLBACK, REDIS_CONSTANTS


def is_redis_py(value: object) -> bool:
    """True for the redis-py package itself or one of its client objects."""
    if isinstance(value, ModuleType):
        return (value.__name__ or "").split(".")[0] == "redis"
    return (type(value).__module__ or "").split(".")[0] == "redis"


@dataclass
class Loop:
    """The loop a break or continue belongs to."""

    #: Set when the loop also uses continue: a break then has to leave the
    #: `repeat ... until true` block first, and says so through this flag.
    break_flag: str | None


class CompilerBase(ABC):
    """State, error reporting, name resolution and kind inference."""

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
        self.variadic_key: str | None = None
        self.variadic_arg: str | None = None
        # Helpers defined in the body, which calls resolve to.
        self.local_functions: set[str] = set()
        # Every name the body assigns, and the statement that assigns it first.
        self.assigned: set[str] = set()
        self.first_assignment: dict[str, ast.stmt] = {}
        # The names a statement assigns for the first time, by statement id.
        self.first_names: dict[int, list[str]] = {}
        # The runtime helpers, from helpers.HELPERS, that this body calls.
        self.helpers: set[str] = set()
        # What a parameter or loop variable is known to be; see kind().
        self.kinds: dict[str, str] = {}
        # Every value assigned to each name, to work out what a local holds.
        self.values: dict[str, list[ast.expr]] = {}
        # Names bound in ways that say nothing about their type.
        self.opaque: set[str] = set()
        # The names for loops bind, which Lua scopes to the loop itself.
        self.loop_targets: set[str] = set()
        # Parameters of a function defined in the body, which may hold a function.
        self.callable_params: set[str] = set()
        self._resolving: set[str] = set()
        # What a break or continue would leave: a loop, or None for the body of
        # a try, which runs as a function and so is out of any loop's reach.
        self.flow: list[Loop | None] = []
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
        value = self.globalns.get(name, UNBOUND)
        if value is UNBOUND:
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
        parts = dotted_name(node)
        if parts is not None:
            if (
                len(parts) == 2
                and parts[1] in REDIS_CONSTANTS
                and self.namespace_kind(parts[0]) == "redis"
            ):
                return lua.Index(lua.Name("redis"), lua.Str(parts[1]))
            root = self.globalns.get(parts[0], UNBOUND)
            # A namespace answers to every attribute with a call stub, and
            # redis-py has a clearer error of its own; neither is a constant.
            if root is not UNBOUND and not isinstance(root, _Namespace) and not is_redis_py(root):
                value: object = root
                for attr in parts[1:]:
                    value = getattr(value, attr, UNBOUND)
                    if value is UNBOUND:
                        break
                else:
                    return self.fold(node, ".".join(parts), value)
        self.fail(node, f"attribute access .{node.attr} is not supported here")

    def fold(self, node: ast.expr, label: str, value: object) -> lua.Expr:
        literal = as_literal(value)
        if literal is None:
            self.fail(
                node,
                f"{label!r} is a module-level {type(value).__name__}, which has no Lua literal",
                hint="Only an int, float, str, bytes or bool constant is folded into the "
                "script. Pass anything else as an argument, or name the literal it "
                "reduces to.",
            )
        return literal

    # ------------------------------------------------------------- resolution

    def namespace_kind(self, name: str) -> str | None:
        """The namespace a bare name refers to, without any error reporting."""
        if name in self.known:
            return None
        value = self.globalns.get(name, UNBOUND)
        if isinstance(value, _Namespace):
            return value.kind
        return RECEIVER_FALLBACK.get(name) if value is UNBOUND else None

    def is_namespace(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and self.namespace_kind(node.id) is not None

    def receiver_kind(self, node: ast.Call, name: str) -> str | None:
        """Work out what the receiver of an attribute call refers to.

        Resolution is by value, through the globals of the module that defined
        the script, so the namespace works under any alias. Only a name bound
        to nothing at all falls back to the conventional spellings.
        """
        value = self.globalns.get(name, UNBOUND)
        if isinstance(value, _Namespace):
            return value.kind
        if value is UNBOUND:
            return RECEIVER_FALLBACK.get(name)
        if is_redis_py(value):
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

    def math_attr(self, func: ast.expr) -> str | None:
        """The math function a call target names, as `math.floor` or a bare `floor`."""
        if isinstance(func, ast.Name) and func.id not in self.known:
            value = self.globalns.get(func.id, UNBOUND)
            try:
                return MATH_BY_OBJECT.get(value)
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

    def is_callable(self, name: str) -> bool:
        """True for a helper, a name bound to a lambda, or a parameter of either."""
        if name in self.local_functions or name in self.callable_params:
            return True
        return name in self.known and self.name_kind(name) == "func"

    # ------------------------------------------------------------------ kinds

    def kind(self, node: ast.expr) -> str | None:
        """What an expression is statically known to be, or None.

        One of "str", "num", "list", "dict" or "func". Lua does not care, but
        several translations do: a subscript is shifted to 1-based for a list
        and not for a dict, `+` joins strings and adds numbers, `in` searches a
        list and looks up a dict's keys, and a format spec aligns a string and
        a number differently. None means "only known at runtime", and the
        translation then decides there, or refuses.
        """
        match node:
            case ast.Constant(value=bool()):
                return None
            case ast.Constant(value=str() | bytes()):
                return "str"
            case ast.Constant(value=int() | float()):
                return "num"
            case ast.JoinedStr():
                return "str"
            case ast.List() | ast.Tuple():
                return "list"
            case ast.Dict():
                return "dict"
            case ast.Lambda():
                return "func"
            case ast.Name(id=name):
                return self.name_kind(name)
            case ast.UnaryOp(op=ast.USub() | ast.UAdd(), operand=operand):
                inner = self.kind(operand)
                return inner if inner in {"num", "?"} else None
            case ast.BinOp(op=op, left=left, right=right):
                return binop_kind(op, self.kind(left), self.kind(right))
            case ast.IfExp(body=then, orelse=otherwise):
                a, b = self.kind(then), self.kind(otherwise)
                return a if a == b else None
            case ast.Subscript(value=value, slice=ast.Slice()):
                inner = self.kind(value)
                return inner if inner in {"str", "list"} else None
            case ast.Subscript(value=value):
                return "str" if self.kind(value) == "str" else None
            case ast.Call(func=ast.Name(id=name)) if not self.is_callable(name):
                if name in {"str", "tostring", "chr"}:
                    return "str"
                if name in {"int", "float", "tonumber", "len", "abs", "ord"}:
                    return "num"
                return "num" if self.math_attr(node.func) is not None else None
            case ast.Call(func=ast.Attribute(value=receiver, attr=attr)):
                if self.math_attr(node.func) is not None:
                    return "num"
                if self.is_namespace(receiver):
                    return None
                if attr in {"upper", "lower", "strip", "lstrip", "rstrip", "replace", "join"}:
                    return "str"
                if attr == "split":
                    return "list"
                return "num" if attr == "find" else None
            case ast.Attribute():
                parts = dotted_name(node)
                if parts is None or self.namespace_kind(parts[0]) is not None:
                    return None
                constant: object = self.globalns.get(parts[0], UNBOUND)
                for attr in parts[1:]:
                    constant = getattr(constant, attr, UNBOUND)
                return value_kind(constant)
        return None

    def name_kind(self, name: str) -> str | None:
        if name not in self.known:
            return value_kind(self.globalns.get(name, UNBOUND))
        if name in self._resolving:
            # A name whose value depends on itself, as in `n += 1`, is whatever
            # its other assignments make it.
            return "?"
        if name in self.opaque:
            return None
        values = self.values.get(name, [])
        if not values:
            return self.kinds.get(name)
        self._resolving.add(name)
        try:
            found = {self.kind(value) for value in values}
        finally:
            self._resolving.discard(name)
        if name in self.kinds:
            found.add(self.kinds[name])
        found.discard("?")
        return found.pop() if len(found) == 1 else None

    # ------------------------------------------------------ the layers above

    @abstractmethod
    def call(self, node: ast.Call) -> lua.Expr:
        """Compile a call; see ``calls``."""

    @abstractmethod
    def block(self, body: list[ast.stmt]) -> list[lua.Stat]:
        """Compile a list of statements; see ``statements``."""

    @abstractmethod
    def lambda_expr(self, node: ast.Lambda) -> lua.Expr:
        """Compile a lambda; see ``statements``."""
