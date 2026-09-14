"""Names that script bodies refer to.

Nothing here ever runs. A decorated function's body is read as source and
compiled to Lua; Python never executes it. These objects exist so that the
body is a valid Python expression to your editor, your linter and your type
checker, and so that calling one by mistake fails loudly instead of silently
doing nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn, cast

if TYPE_CHECKING:
    from ._command_stubs import RedisCommands


class Key(str):
    """Marks a parameter as a Redis key.

    Parameters annotated ``Key`` become ``KEYS[n]`` inside the script, in
    declaration order; everything else becomes ``ARGV[n]``. Getting this right
    matters: Redis Cluster routes a script by its declared keys, so a key
    passed as an argument will be invisible to the router.
    """

    __slots__ = ()


class _Namespace:
    """Attribute access that type checkers accept and runtime refuses.

    The compiler identifies these by value rather than by the name they are
    imported under, so every alias works and nothing is reserved.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    @property
    def kind(self) -> str:
        """Which Lua table this stands for: ``redis`` or ``cjson``."""
        return str(object.__getattribute__(self, "_name"))

    def __getattr__(self, item: str) -> Any:
        namespace = object.__getattribute__(self, "_name")

        def _stub(*_args: object, **_kwargs: object) -> NoReturn:
            raise RuntimeError(
                f"{namespace}.{item}() is a Lua construct and cannot run in Python. "
                "It is only meaningful inside the body of an @script function, "
                "which is compiled rather than executed."
            )

        return _stub

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<redis_lua_py {object.__getattribute__(self, '_name')} namespace>"


if TYPE_CHECKING:

    class RedisNamespace(RedisCommands):
        """The ``redis`` table a script runs against, as type checkers see it.

        Every Redis command is a method, documented from Redis' own command
        definitions: ``redis.hset(key, field, value)`` compiles to
        ``redis.call('HSET', key, field, value)``. Underscores split into
        subcommand tokens, so ``redis.script_load(x)`` compiles to
        ``redis.call('SCRIPT', 'LOAD', x)``. The Lua API's own functions and
        constants keep their names.

        Any other attribute is still allowed here, for the spellings the compiler
        accepts beyond one method per command -- ``redis.debug_object(k)`` -- and
        is checked by the compiler instead.
        """

        def __getattr__(self, name: str) -> Any: ...

        def call(self, command: Any, /, *args: Any) -> Any:
            """Run a Redis command by name, and return its reply.

            An error reply raises a Lua error that aborts the script. Never
            checked at compile time, which makes it the escape hatch for module
            commands and anything newer than the command table::

                redis.call("JSON.SET", doc, "$.status", '"done"')
            """

        def pcall(self, command: Any, /, *args: Any) -> Any:
            """Run a Redis command by name, returning an error reply rather than raising it.

            An error comes back as a table with an ``err`` field, which the script
            can inspect or return.
            """

        def error_reply(self, message: Any, /) -> Any:
            """Build an error reply: a table with ``message`` in its ``err`` field.

            Returning it from the script makes the call fail with that error.
            """

        def status_reply(self, status: Any, /) -> Any:
            """Build a status reply, such as ``OK``: a table with ``status`` in its ``ok`` field."""

        def sha1hex(self, value: Any, /) -> Any:
            """Return the SHA1 digest of a string, as 40 lowercase hex characters."""

        def log(self, level: Any, message: Any, /, *args: Any) -> Any:
            """Write a message to the Redis server log.

            ``level`` is one of ``redis.LOG_DEBUG``, ``redis.LOG_VERBOSE``,
            ``redis.LOG_NOTICE`` or ``redis.LOG_WARNING``.
            """

        def replicate_commands(self) -> Any:
            """Switch the script to effects replication.

            Deprecated: since Redis 7.0 effects replication is the only mode, and
            this does nothing but return true.
            """

        def set_repl(self, flags: Any, /) -> Any:
            """Choose where the commands that follow are replicated.

            ``flags`` is ``redis.REPL_ALL`` (the default), ``redis.REPL_AOF``,
            ``redis.REPL_REPLICA`` or ``redis.REPL_NONE``.
            """

        def setresp(self, version: Any, /) -> Any:
            """Set the protocol, 2 or 3, in which ``redis.call`` hands replies to the script.

            Available since Redis 6.0.
            """

        def acl_check_cmd(self, command: Any, /, *args: Any) -> Any:
            """Return whether the current user's ACL permits running this command.

            Available since Redis 7.0.
            """

        def breakpoint(self) -> Any:
            """Stop at this line when the script runs under the Lua debugger."""

        def debug(self, *values: Any) -> Any:
            """Print values to the Lua debugger's console. Does nothing outside it."""

        LOG_DEBUG: int
        """The ``debug`` level for ``redis.log``."""

        LOG_VERBOSE: int
        """The ``verbose`` level for ``redis.log``."""

        LOG_NOTICE: int
        """The ``notice`` level for ``redis.log``."""

        LOG_WARNING: int
        """The ``warning`` level for ``redis.log``."""

        REPL_ALL: int
        """For ``redis.set_repl``: replicate to the AOF and to replicas."""

        REPL_AOF: int
        """For ``redis.set_repl``: replicate to the AOF only."""

        REPL_REPLICA: int
        """For ``redis.set_repl``: replicate to replicas only."""

        REPL_SLAVE: int
        """For ``redis.set_repl``: the old name of ``REPL_REPLICA``."""

        REPL_NONE: int
        """For ``redis.set_repl``: replicate nowhere."""

        REDIS_VERSION: str
        """The server's version, as a string such as ``"7.2.4"``. Redis 7.0 and later."""

        REDIS_VERSION_NUM: int
        """The server's version as a number, ``0x00MMmmpp``. Redis 7.0 and later."""

    class CjsonNamespace:
        """The JSON library Redis exposes to scripts, as type checkers see it."""

        def encode(self, value: Any, /) -> Any:
            """Serialize a Lua value to a JSON string."""

        def decode(self, text: Any, /) -> Any:
            """Parse a JSON string into a Lua value: objects and arrays become tables."""


#: Call Redis commands: ``redis.incr(key)`` becomes ``redis.call('INCR', key)``.
#: Underscores split into subcommand tokens, so ``redis.script_load(x)``
#: becomes ``redis.call('SCRIPT', 'LOAD', x)``.
#:
#: Import it under any name you like. If the module also imports the redis-py
#: client, ``from redis_lua_py import redis as r`` keeps the two apart.
#:
#: Type checkers see it as a ``RedisNamespace``, whose methods document every
#: command, so editors show each one's syntax on hover.
redis = cast("RedisNamespace", _Namespace("redis"))

#: Alias for :data:`redis`, for modules that would rather not rename anything.
call = cast("RedisNamespace", _Namespace("redis"))

#: The JSON library Redis exposes to scripts: ``cjson.encode`` / ``cjson.decode``.
cjson = cast("CjsonNamespace", _Namespace("cjson"))
