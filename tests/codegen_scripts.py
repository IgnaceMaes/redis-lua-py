"""Scripts for the codegen tests to generate from.

Not a test module. It is imported by name, the way a project's own script
module would be, so that the dotted-name path is exercised for real.
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
    for key in keys:
        redis.expire(key, ttl)
    return 1


@script
def awkward() -> bytes:
    return AWKWARD


#: A second name for the same script, which is collected once.
limiter = rate_limit
