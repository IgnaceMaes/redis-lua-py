"""Command names are checked against Redis' own command table.

Uppercasing an attribute produces a plausible command for any spelling at all,
so the one mistake nothing else catches is a name Redis does not have: it
compiles, it reads correctly, and it raises the first time its branch runs.
"""

from __future__ import annotations

import ast
import inspect
import keyword
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest

from redis_lua_py import CompiledScript, Key, UnsupportedSyntax, redis, script
from redis_lua_py._command_stubs import RedisCommands
from redis_lua_py._commands import COMMANDS, SUBCOMMANDS
from redis_lua_py._compile.statements import Compiler
from redis_lua_py._compile.tables import COMMAND_ALIASES, REDIS_CONSTANTS, REDIS_DIRECT

Body = Callable[[CompiledScript[object]], str]


class TestRedisPySpellings:
    def test_delete_compiles_to_del(self, body: Body) -> None:
        """The headline case: redis-py spells it .delete(), Redis spells it DEL."""

        @script
        def s(k: Key) -> int:
            return redis.delete(k)

        assert "redis.call('DEL', k)" in body(s)
        assert "DELETE" not in body(s)

    def test_del_itself_still_works(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            return redis.delete(k)

        @script
        def t(k: Key) -> int:
            return redis.unlink(k)

        assert "redis.call('DEL', k)" in body(s)
        assert "redis.call('UNLINK', k)" in body(t)


class TestUnknownCommands:
    def test_a_command_redis_does_not_have_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="Redis has no GETT command"):

            @script
            def s(k: Key) -> bytes:
                return redis.gett(k)

    def test_the_error_points_at_the_call_and_suggests_a_spelling(self) -> None:
        with pytest.raises(UnsupportedSyntax) as info:

            @script
            def s(k: Key) -> int:
                return redis.incrementby(k, 2)

        rendered = str(info.value)
        assert "test_commands.py" in rendered
        assert "^" in rendered
        assert info.value.hint is not None
        assert "redis.incrby()" in info.value.hint

    def test_a_name_with_no_near_miss_points_at_the_escape_hatch(self) -> None:
        with pytest.raises(UnsupportedSyntax) as info:

            @script
            def s(k: Key) -> bytes:
                return redis.frobnicate(k)

        assert info.value.hint is not None
        assert "redis.call(" in info.value.hint

    def test_redis_call_is_never_checked(self, body: Body) -> None:
        """The escape hatch has to stay open for modules and newer servers."""

        @script
        def s(k: Key) -> bytes:
            return redis.call("JSON.GET", k, "$.status")

        assert "redis.call('JSON.GET', k, '$.status')" in body(s)


class TestSubcommands:
    def test_underscores_become_a_container_subcommand(self, body: Body) -> None:
        @script
        def s(k: Key) -> bytes:
            return redis.object_encoding(k)

        assert "redis.call('OBJECT', 'ENCODING', k)" in body(s)

    def test_an_unknown_subcommand_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="OBJECT has no ENCODNG subcommand"):

            @script
            def s(k: Key) -> bytes:
                return redis.object_encodng(k)

    def test_a_container_with_no_subcommand_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="CONFIG is a container command"):

            @script
            def s(k: Key) -> bytes:
                return redis.config("GET", "maxmemory")

    def test_a_hyphenated_subcommand_is_found_through_its_underscores(self, body: Body) -> None:
        """CLIENT NO-EVICT cannot be spelled with a hyphen as an attribute."""

        @script
        def s(k: Key) -> bytes:
            return redis.client_no_evict("on")

        assert "redis.call('CLIENT', 'NO-EVICT', 'on')" in body(s)

    def test_tokens_after_a_valid_subcommand_stay_literal(self, body: Body) -> None:
        @script
        def s(k: Key) -> bytes:
            return redis.config_get_maxmemory()

        assert "redis.call('CONFIG', 'GET', 'MAXMEMORY')" in body(s)


class TestUnderscoresInWireNames:
    def test_a_read_only_variant_keeps_its_underscore(self, body: Body) -> None:
        """SORT_RO is one token; splitting would make RO a stray argument."""

        @script
        def s(k: Key) -> list[bytes]:
            return redis.sort_ro(k)

        assert "redis.call('SORT_RO', k)" in body(s)

    def test_a_plain_command_taking_a_subcommand_argument_is_left_alone(self, body: Body) -> None:
        """DEBUG is not a container, so DEBUG OBJECT is a command and an argument."""

        @script
        def s(k: Key) -> bytes:
            return redis.debug_object(k)

        assert "redis.call('DEBUG', 'OBJECT', k)" in body(s)


def test_the_table_matches_the_commands_redis_py_exposes() -> None:
    """Nothing a redis-py user would reasonably write is refused by mistake.

    The compiler's checking is only worth having if it agrees with the client
    library about what a command is. Module namespaces (``bf``, ``json``) and
    the bare container commands are the deliberate exceptions: both are spelled
    differently or not commands at all.
    """
    import ast
    import inspect

    import redis as redis_py

    from redis_lua_py._compile.statements import Compiler
    from redis_lua_py._compile.tables import REDIS_DIRECT

    compiler = Compiler(
        ast.parse("def f(): pass").body[0],  # type: ignore[arg-type]
        filename="<check>",
        first_lineno=1,
        lines=["def f(): pass"],
        globalns={},
    )
    node = ast.parse("f()").body[0].value  # type: ignore[attr-defined]

    plumbing = {
        "pipeline", "transaction", "lock", "close", "execute_command", "parse_response",
        "register_script", "from_url", "from_pool", "load_external_module",
    }  # fmt: skip
    # Namespaces for module commands, which are spelled with a dot (JSON.GET)
    # and so can only be reached through redis.call anyway.
    modules = {"bf", "cf", "cms", "ft", "graph", "json", "tdigest", "topk", "ts", "vset"}
    # Container commands: valid as attributes only with a subcommand.
    containers = {"client", "cluster", "command", "object", "pubsub", "sentinel"}
    # redis-py helpers that are not commands, and STRALGO, gone since Redis 7.
    not_commands = {"keyspace_notifications", "stralgo"}
    expected_failures = modules | containers | not_commands

    checked = set()
    refused = set()
    for name, member in inspect.getmembers(redis_py.Redis):
        if name.startswith("_") or not callable(member):
            continue
        if name in plumbing or name in REDIS_DIRECT or name.endswith("_iter"):
            continue
        if name.startswith(("get_", "set_")):
            continue  # get_encoder, set_response_callback and friends
        checked.add(name)
        try:
            compiler.command_tokens(node, name)  # type: ignore[arg-type]
        except UnsupportedSyntax:
            refused.add(name)

    # Each redis-py release exposes a different set of these (graph left, vset
    # arrived), so only the exceptions this one actually has are expected.
    assert refused == expected_failures & checked


class TestTypedStubs:
    """What an editor shows for ``redis.<name>`` has to be what the compiler does with it."""

    stubs: ClassVar[dict[str, object]] = {
        name: member
        for name, member in vars(RedisCommands).items()
        if not name.startswith("_") and callable(member)
    }

    def test_every_stub_compiles_to_the_command_its_docstring_shows(self) -> None:
        compiler = Compiler(
            ast.parse("def f(): pass").body[0],  # type: ignore[arg-type]
            filename="<check>",
            first_lineno=1,
            lines=["def f(): pass"],
            globalns={},
        )
        node = ast.parse("f()").body[0].value  # type: ignore[attr-defined]

        mismatched = {}
        for name, member in self.stubs.items():
            tokens = compiler.command_tokens(node, name)
            usage = (inspect.getdoc(member) or "").split("Syntax::")[1].split()
            if tuple(usage[: len(tokens)]) != tokens:
                mismatched[name] = tokens

        assert mismatched == {}

    def test_every_command_has_a_stub(self) -> None:
        """Regenerating the table without the stubs would leave editors a release behind."""
        # RESTORE-ASKING is spelled restore_asking, which reaches RESTORE instead.
        spellings = {
            name
            for name in COMMANDS
            if name not in SUBCOMMANDS and name.split("-")[0] not in COMMANDS - {name}
        }
        spellings |= {f"{head}_{sub}" for head, subs in SUBCOMMANDS.items() for sub in subs}
        expected = {name.lower().replace("-", "_") for name in spellings} | set(COMMAND_ALIASES)
        expected = {name for name in expected if not keyword.iskeyword(name)} - REDIS_DIRECT

        assert set(self.stubs) == expected

    def test_the_namespace_types_the_lua_api_too(self) -> None:
        """The functions and constants that keep their own names are written by hand."""
        from redis_lua_py import _runtime

        tree = ast.parse(Path(_runtime.__file__).read_text())
        (namespace,) = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "RedisNamespace"
        ]
        members = {node.name for node in namespace.body if isinstance(node, ast.FunctionDef)}
        members |= {
            node.target.id
            for node in namespace.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }

        assert members >= REDIS_DIRECT
        assert members >= REDIS_CONSTANTS
