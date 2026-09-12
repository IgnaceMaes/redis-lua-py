"""A module where the name ``redis`` is bound to the redis-py client.

Not a test module. It exists so the compiler can be pointed at a function
defined in a realistic collision, rather than one faked with monkeypatching.
"""

from __future__ import annotations

import redis

from redis_lua_py import Key


def uses_the_client_by_mistake(k: Key) -> int:
    """Looks like a script, but ``redis`` here is the client library."""
    return redis.incr(k)


def make_client() -> redis.Redis:
    return redis.Redis()
