"""The callable object a decorated function becomes."""

from __future__ import annotations

import difflib
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, overload
from weakref import WeakKeyDictionary

from ._portable import _AsyncClient, _encode, _encode_all, _items, _registered, _run, _SyncClient
from .errors import ScriptArgumentError

#: What a script's return annotation describes: the value the *caller* gets
#: back, after Redis has converted the Lua value and redis-py has decoded it.
R = TypeVar("R")

#: What calling a bound script produces -- ``R``, or an awaitable of it.
T = TypeVar("T")

#: Enough of an async client, redis-py's or coredis's, to tell it from a sync
#: one. It lives with the rest of the call path in ``_portable``, which
#: generated modules copy.
AsyncClient = _AsyncClient

#: A sync redis-py client, typed first so that a wrapper forwarding attributes
#: through ``__getattr__`` is not mistaken for an async one.
SyncClient = _SyncClient


def resolve_arguments(
    owner: str,
    params: tuple[str, ...],
    keys: tuple[str, ...],
    args: tuple[str, ...],
    variadic_key: str | None,
    variadic_arg: str | None,
    positional: tuple[object, ...],
    keyword: dict[str, object],
) -> tuple[list[Any], list[Any]]:
    """Resolve call arguments into the KEYS and ARGV lists, for a script or a function."""
    if len(positional) > len(params):
        raise ScriptArgumentError(
            f"{owner}() takes {len(params)} argument(s), got {len(positional)}"
        )

    values: dict[str, object] = dict(zip(params, positional, strict=False))
    for key, value in keyword.items():
        if key not in params:
            suggestion = difflib.get_close_matches(key, params, n=1)
            hint = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
            raise ScriptArgumentError(
                f"{owner}() has no parameter {key!r}{hint} (parameters: {', '.join(params)})"
            )
        if key in values:
            raise ScriptArgumentError(f"{owner}() got two values for {key!r}")
        values[key] = value

    missing = [p for p in params if p not in values]
    if missing:
        raise ScriptArgumentError(f"{owner}() is missing argument(s): {', '.join(missing)}")

    resolved_keys: list[Any] = []
    for k in keys:
        if k == variadic_key:
            resolved_keys.extend(_items(k, values[k], ScriptArgumentError))
        else:
            resolved_keys.append(values[k])
    argv: list[Any] = []
    for a in args:
        if a == variadic_arg:
            argv.extend(_encode_all(a, values[a], ScriptArgumentError))
        else:
            argv.append(_encode(a, values[a], ScriptArgumentError))
    return resolved_keys, argv


class ScriptLike(Protocol):
    """What a bound script needs from what it binds: a script or a library function."""

    @property
    def name(self) -> str: ...  # pragma: no cover - a typing shape

    @property
    def lua(self) -> str: ...  # pragma: no cover

    @property
    def params(self) -> tuple[str, ...]: ...  # pragma: no cover

    @property
    def keys(self) -> tuple[str, ...]: ...  # pragma: no cover

    @property
    def args(self) -> tuple[str, ...]: ...  # pragma: no cover

    @property
    def doc(self) -> str | None: ...  # pragma: no cover

    def __call__(self, client: Any, /, *positional: object, **keyword: object) -> Any: ...


@dataclass(frozen=True)
class CompiledScript(Generic[R]):
    """A Python function compiled to Lua, callable against a Redis client.

    Calling it runs EVALSHA and falls back to EVAL the first time, or whenever
    the server has dropped the script from its cache. Pass a sync client and
    you get a value; pass an async client and you get an awaitable.

    The type parameter is the function's return annotation, which describes
    what the *caller* receives -- not what the body returns on the Lua side.
    Redis renders every reply as bytes, so a script returning a Lua string is
    annotated ``bytes``; see the README on writing a body against that.
    """

    name: str
    lua: str
    params: tuple[str, ...]
    keys: tuple[str, ...]
    args: tuple[str, ...]
    doc: str | None = None
    source: str = ""
    #: The ``list[Key]`` parameter, whose elements fill the rest of KEYS.
    variadic_key: str | None = None
    #: The list parameter whose elements fill the rest of ARGV.
    variadic_arg: str | None = None
    #: Each parameter's annotation as written in the source, or None, in the
    #: order of ``params``; what a generated function's signature is built from.
    param_annotations: tuple[str | None, ...] = ()
    #: The return annotation as written in the source, or None.
    return_annotation: str | None = None
    #: The parameters declared after a bare ``*``.
    keyword_only: tuple[str, ...] = ()
    _registry: WeakKeyDictionary[Any, Any] = field(
        default_factory=WeakKeyDictionary, compare=False, repr=False
    )

    @overload
    def __call__(self, client: SyncClient, /, *positional: object, **keyword: object) -> R: ...

    @overload
    def __call__(
        self, client: AsyncClient, /, *positional: object, **keyword: object
    ) -> Awaitable[R]: ...

    @overload
    def __call__(self, client: Any, /, *positional: object, **keyword: object) -> R: ...

    def __call__(self, client: Any, /, *positional: object, **keyword: object) -> Any:
        keys, argv = self.resolve(*positional, **keyword)
        return _run(self.lua, self._registry, client, keys, argv)

    @overload
    def bind(self, client: SyncClient) -> BoundScript[R]: ...

    @overload
    def bind(self, client: AsyncClient) -> BoundScript[Awaitable[R]]: ...

    @overload
    def bind(self, client: Any) -> BoundScript[R]: ...

    def bind(self, client: Any) -> BoundScript[Any]:
        """Attach a client, so that callers stop repeating it.

        The unbound form keeps working; this only removes the first argument.
        Sync and async clients bind the same way, and the bound call returns
        whatever the underlying client would have.
        """
        return BoundScript(self, client)

    def resolve(self, *positional: object, **keyword: object) -> tuple[list[Any], list[Any]]:
        """Resolve call arguments into the KEYS and ARGV lists."""
        return resolve_arguments(
            self.name,
            self.params,
            self.keys,
            self.args,
            self.variadic_key,
            self.variadic_arg,
            positional,
            keyword,
        )

    def _for(self, client: Any) -> Any:
        """Get the redis-py Script bound to this client, registering it once."""
        return _registered(self.lua, self._registry, client)

    def __repr__(self) -> str:
        signature = ", ".join(self.params)
        return f"<script {self.name}({signature}) from {self.source}>"


@dataclass(frozen=True)
class BoundScript(Generic[T]):
    """A script with its client already attached, produced by :meth:`bind`.

    The type parameter is what a call returns: the script's own return type
    for a sync client, an awaitable of it for an async one.
    """

    script: ScriptLike
    client: Any

    def __call__(self, *positional: object, **keyword: object) -> T:
        result: T = self.script(self.client, *positional, **keyword)
        return result

    @property
    def name(self) -> str:
        return self.script.name

    @property
    def lua(self) -> str:
        return self.script.lua

    @property
    def params(self) -> tuple[str, ...]:
        return self.script.params

    @property
    def keys(self) -> tuple[str, ...]:
        return self.script.keys

    @property
    def args(self) -> tuple[str, ...]:
        return self.script.args

    @property
    def doc(self) -> str | None:
        return self.script.doc

    def __repr__(self) -> str:
        signature = ", ".join(self.script.params)
        return f"<bound script {self.script.name}({signature})>"
