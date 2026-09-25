"""Redis Lua scripts compiled by redis-lua-py from codegen_scripts. Do not edit.

Each script is a function taking a redis-py client, sync or async, and then the
arguments it was written with. Its Lua is the constant of the same name in
capitals.

Regenerate with:

    python -m redis_lua_py generate codegen_scripts --out tests/generated_scripts.py
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterable
from typing import Any, Protocol, overload
from weakref import WeakKeyDictionary

__all__ = [
    "AWKWARD",
    "ECHO",
    "PUSH_ALL",
    "RATE_LIMIT",
    "TOUCH_ALL",
    "awkward",
    "echo",
    "push_all",
    "rate_limit",
    "touch_all",
]


#: What a generated signature accepts for a key, the same as redis-py does.
_Key = str | bytes | memoryview

#: What a generated signature accepts for an argument it has no better type for.
_Arg = str | bytes | memoryview | int | float


class _SyncClient(Protocol):
    """Enough of a sync redis-py client to type its call before the async one.

    ``__enter__`` is the discriminator because every sync redis-py client has
    one and no async client does. Checked first, it keeps a wrapper that
    forwards everything through ``__getattr__`` -- which mypy takes to supply
    ``__aenter__`` as well -- from being typed as async.
    """

    def __enter__(self) -> Any: ...

    def register_script(self, script: str) -> Any: ...


class _AsyncClient(Protocol):
    """Enough of an async redis-py client to tell it from a sync one.

    Only used to type the two shapes of call. ``__aenter__`` is the
    discriminator because every async client has one and no sync client does,
    on every supported redis-py -- ``aclose`` only arrived in redis-py 5.
    """

    async def __aenter__(self) -> Any: ...

    def register_script(self, script: str) -> Any: ...


def _encode(
    name: str, value: object, error: type[Exception] = TypeError
) -> str | bytes | memoryview:
    """Render a Python value as a Redis argument.

    Redis has no argument types: everything on the wire is a byte string. This
    only accepts values whose string form is unambiguous, so that a stray None
    or object fails here rather than arriving in Lua as something surprising.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str | bytes | memoryview):
        return value
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    raise error(
        f"argument {name!r} is a {type(value).__name__}, which has no Redis representation. "
        "Pass a str, bytes, int, float or bool."
    )


def _items(name: str, value: object, error: type[Exception] = TypeError) -> list[Any]:
    """The elements passed for a list parameter.

    A string is iterable too, and splitting a key into characters is never
    what was meant, so it is refused rather than spread.
    """
    if isinstance(value, str | bytes | memoryview) or not isinstance(value, Iterable):
        raise error(
            f"argument {name!r} takes a list, got a {type(value).__name__}. "
            "Wrap a single value in a list."
        )
    return list(value)


def _encode_all(
    name: str, value: object, error: type[Exception] = TypeError
) -> list[str | bytes | memoryview]:
    """The elements passed for a list of arguments, each rendered for Redis."""
    return [_encode(name, item, error) for item in _items(name, value, error)]


def _is_cluster_pipeline(client: object) -> bool:
    cls = type(client)
    return cls.__name__ == "ClusterPipeline" and cls.__module__.startswith("redis.")


def _registered(lua: str, registry: WeakKeyDictionary[Any, Any], client: Any) -> Any:
    """The redis-py Script for this source on this client, registered once.

    redis-py's own Script object already implements the EVALSHA-then-EVAL
    dance and the NOSCRIPT retry, so this defers to it rather than
    reimplementing script caching.
    """
    try:
        registered = registry.get(client)
    except TypeError:  # a client that does not support weak references
        return client.register_script(lua)
    if registered is None:
        registered = client.register_script(lua)
        registry[client] = registered
    return registered


def _run(
    lua: str,
    registry: WeakKeyDictionary[Any, Any],
    client: Any,
    keys: list[Any],
    argv: list[Any],
) -> Any:
    """Run a script: a value from a sync client, an awaitable from an async one."""
    if _is_cluster_pipeline(client):
        # redis-py refuses EVALSHA on a cluster pipeline, and a queued
        # EVALSHA could not recover from NOSCRIPT at execute time anyway,
        # so the source travels with the command.
        return client.eval(lua, len(keys), *keys, *argv)
    return _registered(lua, registry, client)(keys=keys, args=argv, client=client)


# rate_limit -- KEYS: key; ARGV: limit, ttl
RATE_LIMIT = """\
-- rate_limit
-- Generated by redis-lua-py from tests/codegen_scripts.py:19. Do not edit.
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local current = redis.call('INCR', key)
if current == 1 then
  redis.call('EXPIRE', key, ttl)
end
if current > limit then
  return -1
end
return limit - current
"""
_RATE_LIMIT_CLIENTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


@overload
def rate_limit(client: _SyncClient, /, key: _Key, limit: int, ttl: int) -> int: ...
@overload
def rate_limit(
    client: _AsyncClient,
    /,
    key: _Key,
    limit: int,
    ttl: int,
) -> Awaitable[int]: ...
@overload
def rate_limit(client: Any, /, key: _Key, limit: int, ttl: int) -> int: ...
def rate_limit(client: Any, /, key: _Key, limit: int, ttl: int) -> Any:
    return _run(
        RATE_LIMIT,
        _RATE_LIMIT_CLIENTS,
        client,
        [key],
        [_encode("limit", limit), _encode("ttl", ttl)],
    )


# touch_all -- KEYS: *keys; ARGV: ttl
TOUCH_ALL = """\
-- touch_all
-- Generated by redis-lua-py from tests/codegen_scripts.py:29. Do not edit.
local ttl = tonumber(ARGV[1])
local keys = {}
for __i1 = 1, #KEYS do
  keys[#keys + 1] = KEYS[__i1]
end
local count = 0
for __i2 = 1, #keys do
  local key = keys[__i2]
  count = count + redis.call('EXPIRE', key, ttl)
end
return count
"""
_TOUCH_ALL_CLIENTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


@overload
def touch_all(client: _SyncClient, /, keys: Iterable[_Key], ttl: int) -> int: ...
@overload
def touch_all(
    client: _AsyncClient,
    /,
    keys: Iterable[_Key],
    ttl: int,
) -> Awaitable[int]: ...
@overload
def touch_all(client: Any, /, keys: Iterable[_Key], ttl: int) -> int: ...
def touch_all(client: Any, /, keys: Iterable[_Key], ttl: int) -> Any:
    """Put a time to live on every key.

    Returns how many of the keys exist.
    """
    return _run(
        TOUCH_ALL,
        _TOUCH_ALL_CLIENTS,
        client,
        [*_items("keys", keys)],
        [_encode("ttl", ttl)],
    )


# awkward -- KEYS: none; ARGV: none
AWKWARD = """\
-- awkward
-- Generated by redis-lua-py from tests/codegen_scripts.py:41. Do not edit.
return 'back\\\\slash, \"\"\"triple quotes\"\"\" and a trailing quote"'
"""
_AWKWARD_CLIENTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


@overload
def awkward(client: _SyncClient, /) -> bytes: ...
@overload
def awkward(client: _AsyncClient, /) -> Awaitable[bytes]: ...
@overload
def awkward(client: Any, /) -> bytes: ...
def awkward(client: Any, /) -> Any:
    return _run(
        AWKWARD,
        _AWKWARD_CLIENTS,
        client,
        [],
        [],
    )


# echo -- KEYS: none; ARGV: value, suffix
ECHO = """\
-- echo
-- Generated by redis-lua-py from tests/codegen_scripts.py:46. Do not edit.
local value = ARGV[1]
local suffix = ARGV[2]
return value .. suffix
"""
_ECHO_CLIENTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


@overload
def echo(client: _SyncClient, /, value: str, *, suffix: str) -> bytes: ...
@overload
def echo(client: _AsyncClient, /, value: str, *, suffix: str) -> Awaitable[bytes]: ...
@overload
def echo(client: Any, /, value: str, *, suffix: str) -> bytes: ...
def echo(client: Any, /, value: str, *, suffix: str) -> Any:
    """Return the value with the suffix after it."""
    return _run(
        ECHO,
        _ECHO_CLIENTS,
        client,
        [],
        [_encode("value", value), _encode("suffix", suffix)],
    )


# push_all -- KEYS: client; ARGV: *values
PUSH_ALL = """\
-- push_all
-- Generated by redis-lua-py from tests/codegen_scripts.py:52. Do not edit.
local client = KEYS[1]
local values = {}
for __i1 = 1, #ARGV do
  values[#values + 1] = ARGV[__i1]
end
for __i2 = 1, #values do
  local value = values[__i2]
  redis.call('RPUSH', client, value)
end
return redis.call('LLEN', client)
"""
_PUSH_ALL_CLIENTS: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()


@overload
def push_all(_client: _SyncClient, /, client: _Key, values: Iterable[str]) -> int: ...
@overload
def push_all(
    _client: _AsyncClient,
    /,
    client: _Key,
    values: Iterable[str],
) -> Awaitable[int]: ...
@overload
def push_all(_client: Any, /, client: _Key, values: Iterable[str]) -> int: ...
def push_all(_client: Any, /, client: _Key, values: Iterable[str]) -> Any:
    return _run(
        PUSH_ALL,
        _PUSH_ALL_CLIENTS,
        _client,
        [client],
        [*_encode_all("values", values)],
    )
