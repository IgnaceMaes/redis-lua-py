"""The fixed vocabularies the compiler checks names against."""

from __future__ import annotations

import ast
import math

from .._commands import COMMANDS, SUBCOMMANDS

# Members of the `redis` table that are not commands and keep their own name.
REDIS_DIRECT = frozenset(
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
REDIS_CONSTANTS = frozenset(
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
RECEIVER_FALLBACK = {"redis": "redis", "call": "redis", "cjson": "cjson"}


# redis-py method names that do not match the wire name of the command they
# send. Spelling one of these the way redis-py does is not a mistake worth an
# error -- it names exactly one command, unambiguously -- so it is simply
# translated. Every other name is checked against the command table.
COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "delete": ("DEL",),
}

#: Every spelling the compiler accepts for a command, for suggesting a near
#: miss. Container commands are left out: they need a subcommand, and the
#: subcommand error says so more precisely.
COMMAND_SPELLINGS = {name.lower() for name in COMMANDS if name not in SUBCOMMANDS} | set(
    COMMAND_ALIASES
)

COMPARE_OPS: dict[type[ast.cmpop], str] = {
    ast.Eq: "==",
    ast.NotEq: "~=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}


BIN_OPS: dict[type[ast.operator], str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.Mod: "%",
    ast.Pow: "^",
}


# Python builtins with an exact Lua counterpart.
BUILTIN_FUNCS: dict[str, str] = {
    "int": "tonumber",
    "float": "tonumber",
    "str": "tostring",
    "tonumber": "tonumber",
    "tostring": "tostring",
    "abs": "math.abs",
    "min": "math.min",
    "max": "math.max",
    "ord": "string.byte",
    "chr": "string.char",
}


# Functions of the math module with a Lua counterpart of the same meaning.
# math.log is handled apart, since only its one-argument form has one.
MATH_FUNCS: dict[str, str] = {
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
MATH_BY_OBJECT: dict[object, str] = {getattr(math, name): name for name in [*MATH_FUNCS, "log"]}


METHOD_HINT = (
    "Available: the redis and cjson namespaces; list append, insert and pop; str join, "
    "upper, lower, strip, lstrip, rstrip, startswith, endswith, find, split and replace; "
    "and dict get."
)


# Methods of str (and dict.get) that compile, wherever the receiver is not a
# namespace. None of these names is a list method, so a receiver of unknown
# type is not ambiguous.
STRING_METHODS = frozenset(
    {
        "upper",
        "lower",
        "strip",
        "lstrip",
        "rstrip",
        "startswith",
        "endswith",
        "find",
        "split",
        "replace",
        "get",
    }
)
