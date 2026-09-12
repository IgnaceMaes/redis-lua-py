"""Redis Functions, and script flags.

A library of compiled functions is loaded with FUNCTION LOAD and called with
FCALL. fakeredis has neither FUNCTION nor script flags, so what needs a server
runs only against a real one (set REDIS_URL). The library source, and the
logic that loads a missing library and retries, are checked without one.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ResponseError

from conftest import LIVE
from redis_lua_py import CompileError, Key, Library, redis, script

live = pytest.mark.skipif(not LIVE, reason="fakeredis has no FUNCTION command; set REDIS_URL")

counters = Library("rlp_counters")


@counters.function
def bump(key: Key, by: int) -> int:
    return redis.incrby(key, by)


@counters.function(flags=["no-writes"])
def peek(key: Key) -> int:
    return int(redis.get(key) or 0)


@counters.function
def bump_all(keys: list[Key]) -> int:
    total = 0
    for k in keys:
        total += redis.incr(k)
    return total


class TestLibrarySource:
    def test_the_first_line_names_the_library(self) -> None:
        assert counters.lua.startswith("#!lua name=rlp_counters\n")

    def test_each_function_is_registered(self) -> None:
        assert counters.lua.count("redis.register_function{") == 3
        assert "function_name = 'bump'," in counters.lua
        assert "callback = function(KEYS, ARGV)" in counters.lua
        assert "flags = {'no-writes'}," in counters.lua

    def test_helpers_are_emitted_once_above_the_functions(self) -> None:
        source = counters.lua
        assert source.count("local function __truthy") == 1
        assert source.index("local function __truthy") < source.index("redis.register_function")

    def test_a_no_writes_function_is_read_only(self) -> None:
        assert peek.read_only
        assert not bump.read_only

    def test_every_function_exposes_the_library_source(self) -> None:
        assert bump.lua == counters.lua
        assert counters.functions == (bump, peek, bump_all)

    def test_an_invalid_library_name(self) -> None:
        with pytest.raises(CompileError, match="letters, digits and underscores"):
            Library("my-lib")

    def test_a_duplicate_function_name(self) -> None:
        library = Library("duplicates")

        @library.function
        def f(k: Key) -> int:
            return redis.incr(k)

        with pytest.raises(CompileError, match="already has a function named 'f'"):

            @library.function(name="f")
            def g(k: Key) -> int:
                return redis.incr(k)

    def test_an_unknown_flag(self) -> None:
        library = Library("flagged")
        with pytest.raises(CompileError, match="unknown flag 'no-write'"):

            @library.function(flags=["no-write"])
            def f(k: Key) -> int:
                return redis.get(k)


class TestScriptFlags:
    def test_flags_go_on_the_first_line(self) -> None:
        @script(flags=["no-writes", "allow-stale"])
        def read(k: Key) -> bytes:
            return redis.get(k)

        assert read.lua.startswith("#!lua flags=no-writes,allow-stale\n-- read\n")

    def test_generated_lua_for_a_flagged_script_is_recognised(self, tmp_path: Any) -> None:
        """codegen tells its own files by the header, which a flags line pushes down."""
        from redis_lua_py.codegen import _is_generated

        @script(flags=["no-writes"])
        def read(k: Key) -> bytes:
            return redis.get(k)

        generated = tmp_path / "read.lua"
        generated.write_text(read.lua)
        assert _is_generated(generated)

    def test_an_unknown_script_flag(self) -> None:
        with pytest.raises(CompileError, match="unknown flag"):

            @script(flags=["read-only"])
            def read(k: Key) -> bytes:
                return redis.get(k)


class FakeClient:
    """Answers FCALL with "Function not found" until the library is loaded."""

    def __init__(self) -> None:
        self.loaded: str | None = None
        self.calls: list[tuple[Any, ...]] = []

    def execute_command(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.loaded is None:
            raise ResponseError("ERR Function not found")
        return ("ran", *args)

    def function_load(self, code: str, replace: bool = False) -> str:
        self.calls.append(("FUNCTION LOAD", replace))
        self.loaded = code
        return "rlp_counters"


class FakeAsyncClient(FakeClient):
    async def execute_command(self, *args: Any) -> Any:  # type: ignore[override]
        return FakeClient.execute_command(self, *args)

    async def function_load(self, code: str, replace: bool = False) -> str:  # type: ignore[override]
        return FakeClient.function_load(self, code, replace)


class TestLoadingOnDemand:
    def test_a_missing_library_is_loaded_and_the_call_retried(self) -> None:
        client = FakeClient()

        assert bump(client, key="k", by=2) == ("ran", "FCALL", "bump", 1, "k", "2")
        assert client.loaded == counters.lua
        assert [call[0] for call in client.calls] == ["FCALL", "FUNCTION LOAD", "FCALL"]

    def test_a_no_writes_function_is_called_with_fcall_ro(self) -> None:
        client = FakeClient()
        counters.load(client)

        assert peek(client, key="k") == ("ran", "FCALL_RO", "peek", 1, "k")

    def test_a_list_of_keys_is_spread(self) -> None:
        client = FakeClient()
        counters.load(client)

        assert bump_all(client, keys=["a", "b"]) == ("ran", "FCALL", "bump_all", 2, "a", "b")

    def test_an_old_function_load_signature_is_not_used(self) -> None:
        """redis-py 4.2.0 has function_load(engine, library, code), from a Redis 7 RC."""

        class OldRedisPy(FakeClient):
            def execute_command(self, *args: Any) -> Any:
                if args[:3] == ("FUNCTION", "LOAD", "REPLACE"):
                    self.calls.append(args[:3])
                    self.loaded = args[3]
                    return "rlp_counters"
                return FakeClient.execute_command(self, *args)

            def function_load(  # type: ignore[override]
                self, engine: str, library: str, code: str, replace: bool = False
            ) -> str:
                raise AssertionError("the release-candidate signature must not be called")

        client = OldRedisPy()

        assert bump(client, key="k", by=2) == ("ran", "FCALL", "bump", 1, "k", "2")
        assert client.loaded == counters.lua
        assert ("FUNCTION", "LOAD", "REPLACE") in client.calls

    def test_another_error_is_not_swallowed(self) -> None:
        class Failing(FakeClient):
            def execute_command(self, *args: Any) -> Any:
                raise ResponseError("WRONGTYPE Operation against a key")

        with pytest.raises(ResponseError, match="WRONGTYPE"):
            bump(Failing(), key="k", by=1)

    async def test_an_async_client(self) -> None:
        client = FakeAsyncClient()

        assert await bump(client, key="k", by=1) == ("ran", "FCALL", "bump", 1, "k", "1")
        assert client.loaded == counters.lua


@live
class TestAgainstRedis:
    def test_the_first_call_loads_the_library(self, client: Any) -> None:
        client.function_flush()

        assert bump(client, key="n", by=5) == 5
        assert bump(client, key="n", by=1) == 6
        assert peek(client, key="n") == 6
        assert peek(client, key="missing") == 0

    def test_a_list_of_keys(self, client: Any) -> None:
        client.function_flush()

        assert bump_all(client, keys=["a", "b", "a"]) == 4

    def test_a_pipeline_after_loading(self, client: Any) -> None:
        counters.load(client)
        pipe = client.pipeline()
        bump(pipe, key="p", by=2)
        peek(pipe, key="p")

        assert pipe.execute() == [2, 2]

    async def test_an_async_client(self, async_client: Any) -> None:
        await async_client.function_flush()

        assert await bump(async_client, key="n", by=3) == 3

    def test_a_script_with_flags_runs(self, client: Any) -> None:
        @script(flags=["no-writes"])
        def read(k: Key) -> bytes:
            return redis.get(k)

        client.set("k", "v")
        assert read(client, k="k") == b"v"

    def test_a_no_writes_script_cannot_write(self, client: Any) -> None:
        @script(flags=["no-writes"])
        def write(k: Key) -> int:
            return redis.incr(k)

        with pytest.raises(ResponseError, match="Write commands are not allowed"):
            write(client, k="k")
