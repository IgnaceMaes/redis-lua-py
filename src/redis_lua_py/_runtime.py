"""Names that script bodies refer to.

Nothing here ever runs. A decorated function's body is read as source and
compiled to Lua; Python never executes it. These objects exist so that the
body is a valid Python expression to your editor, your linter and your type
checker, and so that calling one by mistake fails loudly instead of silently
doing nothing.
"""

from __future__ import annotations

from typing import Any, NoReturn


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


#: Call Redis commands: ``redis.incr(key)`` becomes ``redis.call('INCR', key)``.
#: Underscores split into subcommand tokens, so ``redis.script_load(x)``
#: becomes ``redis.call('SCRIPT', 'LOAD', x)``.
#:
#: Import it under any name you like. If the module also imports the redis-py
#: client, ``from redis_lua_py import redis as r`` keeps the two apart.
redis = _Namespace("redis")

#: Alias for :data:`redis`, for modules that would rather not rename anything.
call = _Namespace("redis")

#: The JSON library Redis exposes to scripts: ``cjson.encode`` / ``cjson.decode``.
cjson = _Namespace("cjson")
