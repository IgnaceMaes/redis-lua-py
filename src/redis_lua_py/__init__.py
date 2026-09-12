"""Write Redis Lua scripts as real Python functions, not strings.

    from redis_lua_py import Key, redis, script

    @script
    def rate_limit(key: Key, limit: int, ttl: int) -> int:
        current = redis.incr(key)
        if current == 1:
            redis.expire(key, ttl)
        if current > limit:
            return -1
        return limit - current

    remaining = rate_limit(client, key="u:42", limit=10, ttl=60)

The body is never executed by Python: it is read as source at import time and
compiled to Lua, which you can inspect at ``rate_limit.lua``.

The ``redis`` namespace above is resolved by value, not by spelling, so import
it under whatever name suits the module::

    from redis_lua_py import redis as r     # frees the name for redis-py

Binding a client once is often tidier than passing it to every call::

    limiter = rate_limit.bind(client)
    limiter(key="u:42", limit=10, ttl=60)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, overload

from ._compile import compile_function
from ._runtime import Key, call, cjson, redis
from ._script import BoundScript, CompiledScript
from .errors import (
    CompileError,
    NilTruncationWarning,
    RedisLuaError,
    RedisLuaWarning,
    ScriptArgumentError,
    UnsupportedSyntax,
)

R = TypeVar("R")

__all__ = [
    "BoundScript",
    "CompileError",
    "CompiledScript",
    "Key",
    "NilTruncationWarning",
    "RedisLuaError",
    "RedisLuaWarning",
    "ScriptArgumentError",
    "UnsupportedSyntax",
    "call",
    "cjson",
    "redis",
    "script",
]

__version__ = "0.2.1"  # x-release-please-version


@overload
def script(func: Callable[..., R], /) -> CompiledScript[R]: ...


@overload
def script(
    *, name: str | None = ..., header: bool = ...
) -> Callable[[Callable[..., R]], CompiledScript[R]]: ...


def script(
    func: Callable[..., R] | None = None,
    /,
    *,
    name: str | None = None,
    header: bool = True,
) -> CompiledScript[R] | Callable[[Callable[..., R]], CompiledScript[R]]:
    """Compile a function into a Redis Lua script.

    Parameters annotated :class:`Key` become ``KEYS``, in declaration order;
    every other parameter becomes ``ARGV``. An ``int`` or ``float`` annotation
    additionally wraps the argument in ``tonumber``, since ARGV always arrives
    as a string. A ``bytes`` annotation passes the argument through untouched,
    so binary values survive exactly.

    The return annotation describes what the *caller* gets back, and is carried
    through to the call: ``-> int`` makes the script a ``CompiledScript[int]``.
    The compiler itself does not read it.

    Define scripts at module level, where they compile once at import.

    ``name`` overrides the name in the generated header and in errors.
    ``header=False`` drops the provenance comment entirely, for anyone who
    wants the script body and nothing else.

    Raises :class:`UnsupportedSyntax` at decoration time, pointing at the line
    at fault, if the body strays outside the supported subset.
    """

    def wrap(target: Callable[..., R]) -> CompiledScript[R]:
        return compile_function(target, name=name, header=header)

    return wrap if func is None else wrap(func)
