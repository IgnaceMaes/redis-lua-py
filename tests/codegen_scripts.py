"""Scripts for the codegen tests to generate from.

Not a test module. It is imported by name, the way a project's own script
module would be, so that the dotted-name path is exercised for real.
``generated_scripts.py`` is what generating from it produces, checked in.
"""

from __future__ import annotations

from redis_lua_py import Key, redis, script

#: Folded into the Lua as a literal, and awkward on purpose: it holds what the
#: Python rendering of that Lua has to escape.
AWKWARD = 'back\\slash, """triple quotes""" and a trailing quote"'

NOT_A_SCRIPT = 3


@script
def rate_limit(key: Key, limit: int, ttl: int) -> int:
    current = redis.incr(key)
    if current == 1:
        redis.expire(key, ttl)
    if current > limit:
        return -1
    return limit - current


@script
def touch_all(keys: list[Key], ttl: int) -> int:
    """Put a time to live on every key.

    Returns how many of the keys exist.
    """
    count = 0
    for key in keys:
        count = count + redis.expire(key, ttl)
    return count


@script
def awkward() -> bytes:
    return AWKWARD


@script
def echo(value: str, *, suffix: str) -> bytes:
    """Return the value with the suffix after it."""
    return value + suffix


@script
def push_all(client: Key, values: list[str]) -> int:
    for value in values:
        redis.rpush(client, value)
    return redis.llen(client)


#: A second name for the same script, which is collected once.
limiter = rate_limit
