"""Calling a compiled script: the part that has to work without this package.

Everything below the imports is copied, verbatim, into every module that
:mod:`redis_lua_py.codegen` generates, and :mod:`._script` calls the same
functions at runtime. That is how a generated wrapper and a ``CompiledScript``
stay in step: there is only one copy of this code to get right.

So this module imports only the standard library, and nothing from the
package, and every name it defines starts with an underscore, to stay out of
the way of the scripts in a generated module.

It is also the one module here that runs on Python 3.9, because a library
that vendors generated code may still support it. Annotations are never
evaluated, so they can use ``X | Y``; anything evaluated cannot, which is why
the aliases below spell ``Union`` and ``isinstance`` takes tuples.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol, Union
from weakref import WeakKeyDictionary

#: What a generated signature accepts for a key, the same as redis-py does.
_Key = Union[str, bytes, memoryview]

#: What a generated signature accepts for an argument it has no better type for.
_Arg = Union[str, bytes, memoryview, int, float]


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
    if isinstance(value, (str, bytes, memoryview)):
        return value
    if isinstance(value, (int, float)):
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
    if isinstance(value, (str, bytes, memoryview)) or not isinstance(value, Iterable):
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
