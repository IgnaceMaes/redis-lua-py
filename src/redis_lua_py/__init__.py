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
from typing import Any, overload

from ._compile import compile_function
from ._runtime import Key, call, cjson, redis
from ._script import BoundScript, CompiledScript
from .errors import (
    CompileError,
    RedisLuaError,
    ScriptArgumentError,
    UnsupportedSyntax,
)

__all__ = [
    "BoundScript",
    "CompileError",
    "CompiledScript",
    "Key",
    "RedisLuaError",
    "ScriptArgumentError",
    "UnsupportedSyntax",
    "call",
    "cjson",
    "redis",
    "script",
]

__version__ = "0.1.0"  # x-release-please-version


@overload
def script(func: Callable[..., Any], /) -> CompiledScript: ...


@overload
def script(*, name: str | None = ...) -> Callable[[Callable[..., Any]], CompiledScript]: ...


def script(
    func: Callable[..., Any] | None = None, /, *, name: str | None = None
) -> CompiledScript | Callable[[Callable[..., Any]], CompiledScript]:
    """Compile a function into a Redis Lua script.

    Parameters annotated :class:`Key` become ``KEYS``, in declaration order;
    every other parameter becomes ``ARGV``. An ``int`` or ``float`` annotation
    additionally wraps the argument in ``tonumber``, since ARGV always arrives
    as a string.

    Raises :class:`UnsupportedSyntax` at decoration time, pointing at the line
    at fault, if the body strays outside the supported subset.
    """

    def wrap(target: Callable[..., Any]) -> CompiledScript:
        return compile_function(target, name=name)

    return wrap if func is None else wrap(func)
