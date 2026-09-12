"""The callable object a decorated function becomes."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from .errors import ScriptArgumentError


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
class CompiledScript:
    """A Python function compiled to Lua, callable against a Redis client.

    Calling it runs EVALSHA and falls back to EVAL the first time, or whenever
    the server has dropped the script from its cache. Pass a sync client and
    you get a value; pass an async client and you get an awaitable.
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

    def __call__(self, client: Any, /, *positional: object, **keyword: object) -> Any:
        keys, argv = self.resolve(*positional, **keyword)
        return self._for(client)(keys=keys, args=argv, client=client)

    def bind(self, client: Any) -> BoundScript:
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
class BoundScript:
    """A script with its client already attached, produced by :meth:`bind`."""

    script: CompiledScript
    client: Any

    def __call__(self, *positional: object, **keyword: object) -> Any:
        return self.script(self.client, *positional, **keyword)

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
