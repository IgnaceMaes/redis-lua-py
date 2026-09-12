"""The callable object a decorated function becomes."""

from __future__ import annotations

import difflib
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar, overload
from weakref import WeakKeyDictionary

from .errors import ScriptArgumentError

#: What a script's return annotation describes: the value the *caller* gets
#: back, after Redis has converted the Lua value and redis-py has decoded it.
R = TypeVar("R")

#: What calling a bound script produces -- ``R``, or an awaitable of it.
T = TypeVar("T")


class AsyncClient(Protocol):
    """Enough of an async redis-py client to tell it from a sync one.

    Only used to type the two shapes of call. ``__aenter__`` is the
    discriminator because every async client has one and no sync client does,
    on every supported redis-py -- ``aclose`` only arrived in redis-py 5.
    """

    async def __aenter__(self) -> Any: ...  # pragma: no cover - a typing shape

    def register_script(self, script: str) -> Any: ...  # pragma: no cover


def encode(name: str, value: object) -> str | bytes | memoryview:
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
    raise ScriptArgumentError(
        f"argument {name!r} is a {type(value).__name__}, which has no Redis representation. "
        "Pass a str, bytes, int, float or bool."
    )


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
    _registry: WeakKeyDictionary[Any, Any] = field(
        default_factory=WeakKeyDictionary, compare=False, repr=False
    )

    @overload
    def __call__(
        self, client: AsyncClient, /, *positional: object, **keyword: object
    ) -> Awaitable[R]: ...

    @overload
    def __call__(self, client: Any, /, *positional: object, **keyword: object) -> R: ...

    def __call__(self, client: Any, /, *positional: object, **keyword: object) -> Any:
        keys, argv = self.resolve(*positional, **keyword)
        return self._for(client)(keys=keys, args=argv, client=client)

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
        if len(positional) > len(self.params):
            raise ScriptArgumentError(
                f"{self.name}() takes {len(self.params)} argument(s), got {len(positional)}"
            )

        values: dict[str, object] = dict(zip(self.params, positional, strict=False))
        for key, value in keyword.items():
            if key not in self.params:
                suggestion = difflib.get_close_matches(key, self.params, n=1)
                hint = f"; did you mean {suggestion[0]!r}?" if suggestion else ""
                raise ScriptArgumentError(
                    f"{self.name}() has no parameter {key!r}{hint} "
                    f"(parameters: {', '.join(self.params)})"
                )
            if key in values:
                raise ScriptArgumentError(f"{self.name}() got two values for {key!r}")
            values[key] = value

        missing = [p for p in self.params if p not in values]
        if missing:
            raise ScriptArgumentError(f"{self.name}() is missing argument(s): {', '.join(missing)}")

        return (
            [values[k] for k in self.keys],
            [encode(a, values[a]) for a in self.args],
        )

    def _for(self, client: Any) -> Any:
        """Get the redis-py Script bound to this client, registering it once.

        redis-py's own Script object already implements the EVALSHA-then-EVAL
        dance and the NOSCRIPT retry, so this defers to it rather than
        reimplementing script caching.
        """
        try:
            registered = self._registry.get(client)
        except TypeError:  # a client that does not support weak references
            return client.register_script(self.lua)
        if registered is None:
            registered = client.register_script(self.lua)
            self._registry[client] = registered
        return registered

    def __repr__(self) -> str:
        signature = ", ".join(self.params)
        return f"<script {self.name}({signature}) from {self.source}>"


@dataclass(frozen=True)
class BoundScript(Generic[T]):
    """A script with its client already attached, produced by :meth:`bind`.

    The type parameter is what a call returns: the script's own return type
    for a sync client, an awaitable of it for an async one.
    """

    script: CompiledScript[Any]
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
