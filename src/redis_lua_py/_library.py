"""Redis Functions: Python functions collected into a library, loaded once.

A script travels with EVALSHA and lives in a cache the server may drop. A
function library, since Redis 7, is loaded with FUNCTION LOAD, persists and
replicates with the data, and each function in it is called by name with
FCALL. The same compiled body serves both; only the framing differs.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, overload

from . import _lua as lua
from ._compile import check_flags, compile_body, helper_order, helper_source
from ._script import AsyncClient, BoundScript, SyncClient, resolve_arguments
from .errors import CompileError

R = TypeVar("R")

# Redis accepts only these characters in library and function names.
_NAME = re.compile(r"[A-Za-z0-9_]+")


def _check_name(kind: str, name: str) -> str:
    if not _NAME.fullmatch(name):
        raise CompileError(
            f"{kind} name {name!r} must be letters, digits and underscores only, "
            "which is all Redis accepts"
        )
    return name


def _is_coredis(client: object) -> bool:
    # Told apart by where its classes live, so that coredis stays optional and
    # is never imported here.
    return type(client).__module__.startswith("coredis.")


def _is_pipeline(client: object) -> bool:
    cls = type(client)
    return cls.__name__ in {"Pipeline", "ClusterPipeline"} and cls.__module__.startswith(
        ("redis.", "coredis.")
    )


def _function_missing(error: BaseException) -> bool:
    return "Function not found" in str(error)


def _modern_function_load(client: object) -> Any:
    """The client's function_load, if it takes the library code first.

    Early redis-py releases, 4.2.0 among them, still have the Redis 7
    release-candidate signature, function_load(engine, library, code), which
    no released Redis accepts. coredis names the same first parameter
    ``function_code``.
    """
    function_load = getattr(client, "function_load", None)
    if function_load is None:
        return None
    try:
        parameters = list(inspect.signature(function_load).parameters)
    except (TypeError, ValueError):  # a callable without an inspectable signature
        return None
    return function_load if parameters[:1] in (["code"], ["function_code"]) else None


def _send(client: Any, command: str, function: str, keys: list[Any], argv: list[Any]) -> Any:
    """Send FCALL or FCALL_RO as the client spells it."""
    if _is_coredis(client):
        # coredis's execute_command takes a prepared request, not the words of
        # a command, and it only prepares one through a method per command.
        method = client.fcall_ro if command == "FCALL_RO" else client.fcall
        return method(function, keys=keys, args=argv)
    return client.execute_command(command, function, len(keys), *keys, *argv)


class Library:
    """A Redis Functions library, built from Python functions.

    Decorate functions with :meth:`function` to add them. Calling one of them
    sends FCALL, or FCALL_RO for a function flagged ``no-writes``, and loads
    the library first if the server turns out not to have it.
    """

    def __init__(self, name: str) -> None:
        self.name = _check_name("library", name)
        self._functions: dict[str, LibraryFunction[Any]] = {}

    @overload
    def function(self, func: Callable[..., R], /) -> LibraryFunction[R]: ...

    @overload
    def function(
        self, /, *, name: str | None = ..., flags: Iterable[str] = ...
    ) -> Callable[[Callable[..., R]], LibraryFunction[R]]: ...

    def function(
        self,
        func: Callable[..., R] | None = None,
        /,
        *,
        name: str | None = None,
        flags: Iterable[str] = (),
    ) -> LibraryFunction[R] | Callable[[Callable[..., R]], LibraryFunction[R]]:
        """Compile a function into this library. Usable bare or called.

        The body follows exactly the same rules as a script's. ``name``
        overrides the function name Redis registers, and ``flags`` takes the
        flags Redis defines, such as ``no-writes``.
        """
        checked_flags = tuple(flags) if not isinstance(flags, str) else (flags,)

        def wrap(target: Callable[..., R]) -> LibraryFunction[R]:
            compiled = compile_body(target, name=name)
            function_name = _check_name("function", compiled.name)
            if function_name in self._functions:
                raise CompileError(
                    f"library {self.name!r} already has a function named {function_name!r}"
                )
            registered: LibraryFunction[R] = LibraryFunction(
                name=function_name,
                library=self,
                body=compiled.body,
                helpers=compiled.helpers,
                flags=check_flags(checked_flags, owner=function_name),
                params=compiled.params,
                keys=compiled.keys,
                args=compiled.args,
                variadic_key=compiled.variadic_key,
                variadic_arg=compiled.variadic_arg,
                doc=compiled.doc,
                source=compiled.provenance,
            )
            self._functions[function_name] = registered
            return registered

        return wrap if func is None else wrap(func)

    @property
    def functions(self) -> tuple[LibraryFunction[Any], ...]:
        """The functions in this library, in the order they were added."""
        return tuple(self._functions.values())

    @property
    def lua(self) -> str:
        """The library's source, exactly as FUNCTION LOAD receives it.

        Helpers any function needs are emitted once, at the top, where every
        callback can see them.
        """
        parts = [f"#!lua name={self.name}", "-- Generated by redis-lua-py. Do not edit."]
        needed = helper_order(h for function in self._functions.values() for h in function.helpers)
        parts.extend(helper_source(helper) for helper in needed)
        parts.extend(function.registration() for function in self._functions.values())
        return "\n".join(parts) + "\n"

    def load(self, client: Any) -> Any:
        """Load the library, replacing any earlier version, with FUNCTION LOAD REPLACE.

        Calling a function loads the library when the server lacks it, so this
        is only needed ahead of a pipeline, or to load at deploy time. Returns
        what the client returns: an awaitable for an async client.
        """
        function_load = _modern_function_load(client)
        if function_load is not None:
            # Preferred where it exists, since redis-py and coredis both route
            # it to every primary of a cluster.
            return function_load(self.lua, replace=True)
        return client.execute_command("FUNCTION", "LOAD", "REPLACE", self.lua)

    def call(
        self, client: Any, command: str, function: str, keys: list[Any], argv: list[Any]
    ) -> Any:
        """Send FCALL or FCALL_RO, loading the library and retrying if it is missing."""
        if _is_pipeline(client):
            # A queued call cannot load the library once it turns out to be
            # missing; load() it before queueing.
            return _send(client, command, function, keys, argv)
        try:
            result = _send(client, command, function, keys, argv)
        except Exception as error:
            if not _function_missing(error):
                raise
            self.load(client)
            return _send(client, command, function, keys, argv)
        if inspect.isawaitable(result):
            return self._call_async(client, result, command, function, keys, argv)
        return result

    async def _call_async(
        self,
        client: Any,
        pending: Awaitable[Any],
        command: str,
        function: str,
        keys: list[Any],
        argv: list[Any],
    ) -> Any:
        try:
            return await pending
        except Exception as error:
            if not _function_missing(error):
                raise
            await self.load(client)
            return await _send(client, command, function, keys, argv)

    def __repr__(self) -> str:
        return f"<library {self.name} ({', '.join(self._functions)})>"


@dataclass(frozen=True)
class LibraryFunction(Generic[R]):
    """A function in a Redis Functions library, callable against a client.

    Called like a script: pass a sync client and you get a value, an async one
    and you get an awaitable. The type parameter is the return annotation.
    """

    name: str
    library: Library = field(repr=False, compare=False)
    body: str
    helpers: tuple[str, ...]
    flags: tuple[str, ...]
    params: tuple[str, ...]
    keys: tuple[str, ...]
    args: tuple[str, ...]
    variadic_key: str | None = None
    variadic_arg: str | None = None
    doc: str | None = None
    source: str = ""

    @property
    def read_only(self) -> bool:
        """True for a ``no-writes`` function, which is called with FCALL_RO."""
        return "no-writes" in self.flags

    @property
    def lua(self) -> str:
        """The source of the whole library this function belongs to."""
        return self.library.lua

    def registration(self) -> str:
        """The ``redis.register_function`` call that defines this function."""
        body = [f"    {line}" if line else "" for line in self.body.splitlines()]
        lines = [
            f"-- {self.name}, from {self.source}",
            "redis.register_function{",
            f"  function_name = {lua.quote(self.name)},",
            "  callback = function(KEYS, ARGV)",
            *body,
            "  end,",
        ]
        if self.flags:
            quoted = ", ".join(lua.quote(flag) for flag in self.flags)
            lines.append(f"  flags = {{{quoted}}},")
        lines.append("}")
        return "\n".join(lines)

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
        command = "FCALL_RO" if self.read_only else "FCALL"
        return self.library.call(client, command, self.name, keys, argv)

    @overload
    def bind(self, client: SyncClient) -> BoundScript[R]: ...

    @overload
    def bind(self, client: AsyncClient) -> BoundScript[Awaitable[R]]: ...

    @overload
    def bind(self, client: Any) -> BoundScript[R]: ...

    def bind(self, client: Any) -> BoundScript[Any]:
        """Attach a client, so that callers stop repeating it."""
        return BoundScript(self, client)

    def resolve(self, *positional: object, **keyword: object) -> tuple[list[Any], list[Any]]:
        """Resolve call arguments into the keys and arguments FCALL takes."""
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

    def __repr__(self) -> str:
        return f"<function {self.library.name}.{self.name}({', '.join(self.params)})>"
