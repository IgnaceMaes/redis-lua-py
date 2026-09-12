"""A module that imports no namespace at all.

Not a test module. The compiler falls back to the conventional spellings when
a receiver name is bound to nothing, which is what this exercises.
"""

from __future__ import annotations

from redis_lua_py import Key


def uses_a_bare_name(k: Key) -> int:
    # `redis` is deliberately not imported here; the compiler's fallback
    # resolves it, even though a linter would flag the name.
    return redis.incr(k)  # noqa: F821
