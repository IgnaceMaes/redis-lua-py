"""Calls: helpers, builtins, the math module, methods, and Redis commands."""

from __future__ import annotations

import ast
import difflib

from .. import _lua as lua
from .._commands import COMMANDS, SUBCOMMANDS
from .expressions import ExpressionCompiler
from .tables import (
    BUILTIN_FUNCS,
    COMMAND_ALIASES,
    COMMAND_SPELLINGS,
    MATH_FUNCS,
    METHOD_HINT,
    REDIS_DIRECT,
    STRING_METHODS,
)


class CallCompiler(ExpressionCompiler):
    """Compiles calls to helpers, builtins, methods, and the redis and cjson namespaces."""

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
            case ast.Name(id=name) if name in BUILTIN_FUNCS:
                return lua.Call(lua.Name(BUILTIN_FUNCS[name]), args)
            case ast.Attribute(value=receiver, attr="join") if not self.is_namespace(receiver):
                if len(args) != 1:
                    self.fail(node, "join() takes exactly one argument")
                return lua.Call(lua.Name("table.concat"), (args[0], self.expr(receiver)))
            case ast.Attribute(value=ast.Name(id=recv) as receiver, attr="pop" | "insert") if (
                recv in self.known
            ):
                return self.list_method(node, receiver)
            case ast.Attribute(value=receiver, attr=attr) if (
                attr in STRING_METHODS and not self.is_namespace(receiver)
            ):
                return self.string_method(node, receiver, attr, args)
            case ast.Attribute(value=ast.Name(id=recv), attr=attr):
                return self.namespace_call(node, recv, attr, args)
            case ast.Attribute(attr=attr):
                self.fail(node, f"method call .{attr}() is not supported", hint=METHOD_HINT)
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

    def math_call(self, node: ast.Call, attr: str, args: tuple[lua.Expr, ...]) -> lua.Expr:
        if attr == "log":
            if len(args) != 1:
                self.fail(
                    node,
                    "math.log() with a base is not supported",
                    hint="Lua 5.1's math.log takes no base; divide by math.log(base) instead.",
                )
            return lua.Call(lua.Name("math.log"), args)
        target = MATH_FUNCS.get(attr)
        if target is None:
            self.fail(
                node,
                f"math.{attr}() has no Lua counterpart",
                hint=f"Available: {listing(sorted([*MATH_FUNCS, 'log']), limit=10)}.",
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

    def string_method(
        self, node: ast.Call, receiver: ast.expr, attr: str, args: tuple[lua.Expr, ...]
    ) -> lua.Expr:
        """The str methods, and dict.get, over Lua's string library and helpers."""
        target = self.expr(receiver)
        allowed = {"get": (1, 2), "split": (0, 1), "replace": (2,)}.get(attr)
        if allowed is None:
            allowed = (0,) if attr in {"upper", "lower", "strip", "lstrip", "rstrip"} else (1,)
        if len(args) not in allowed:
            if attr in {"strip", "lstrip", "rstrip"}:
                self.fail(
                    node,
                    f"{attr}() with characters to strip is not supported",
                    hint="Only stripping whitespace, with no argument, is supported.",
                )
            expected = " or ".join(str(n) for n in allowed)
            self.fail(node, f"{attr}() takes {expected} argument(s) inside a script")

        if attr in {"upper", "lower"}:
            return lua.Call(lua.Name(f"string.{attr}"), (target,))
        if attr in {"strip", "lstrip", "rstrip"}:
            pattern = {"strip": "^%s*(.-)%s*$", "lstrip": "^%s*(.*)$", "rstrip": "^(.-)%s*$"}
            return lua.Call(lua.Name("string.match"), (target, lua.Str(pattern[attr])))
        if attr == "get":
            self.helpers.add("__get")
            default = args[1] if len(args) == 2 else lua.Nil()
            return lua.Call(lua.Name("__get"), (target, args[0], default))
        helper = f"__{attr}"
        self.helpers.add(helper)
        return lua.Call(lua.Name(helper), (target, *args))

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
            hint=METHOD_HINT,
        )

    def redis_call(self, node: ast.Call, attr: str, args: tuple[lua.Expr, ...]) -> lua.Expr:
        if attr in REDIS_DIRECT:
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
        alias = COMMAND_ALIASES.get(attr)
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
            hint=did_you_mean(attr, COMMAND_SPELLINGS)
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
                f"{listing(sorted(name.lower() for name in valid))}",
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
            hint=did_you_mean(
                joined.lower().replace("-", "_"),
                {name.lower().replace("-", "_") for name in valid},
                prefix=f"{head.lower()}_",
            ),
        )


def listing(names: list[str], limit: int = 6) -> str:
    """A comma-separated sample, only trailing off when there is more."""
    shown = ", ".join(names[:limit])
    return f"{shown}, ..." if len(names) > limit else shown


def did_you_mean(attr: str, options: set[str], prefix: str = "") -> str | None:
    """A hint naming the closest spelling, when there is a close one."""
    matches = difflib.get_close_matches(attr, options, n=1, cutoff=0.7)
    if not matches:
        return None
    return f"Did you mean redis.{prefix}{matches[0]}()?"
