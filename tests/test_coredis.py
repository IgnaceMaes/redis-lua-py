"""coredis, an async-only client, running the same scripts and libraries.

coredis is not redis-py underneath: its execute_command takes a prepared
request rather than the words of a command, its Script hands back a request
to await, and its pipelines load their scripts themselves. fakeredis emulates
only redis-py, so this runs only against a real server (set REDIS_URL).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from conftest import LIVE, REDIS_URL

if not LIVE:
    pytest.skip("coredis needs a real server; set REDIS_URL", allow_module_level=True)

coredis = pytest.importorskip("coredis")

import generated_scripts  # noqa: E402
from redis_lua_py import Key, Library, redis, script  # noqa: E402


@script
def counter(key: Key, by: int) -> int:
    return redis.incrby(key, by)


@script
def fetch(key: Key) -> bytes:
    return redis.get(key)


# Function names are global on the server, so these must not collide with the
# libraries in test_functions.py.
library = Library("rlp_coredis")


@library.function
def coredis_bump(key: Key, by: int) -> int:
    return redis.incrby(key, by)


@library.function(flags=["no-writes"])
def coredis_peek(key: Key) -> int:
    return int(redis.get(key) or 0)


@library.function
def coredis_bump_all(keys: list[Key]) -> int:
    total = 0
    for k in keys:
        total += redis.incr(k)
    return total


@asynccontextmanager
async def connected(**options: Any) -> AsyncIterator[Any]:
    """A coredis client on an empty database, with no functions loaded.

    Not a fixture: a coredis client is an anyio context, which has to be left
    in the task that entered it, and pytest-asyncio sets a fixture up and
    tears it down in tasks of its own.
    """
    async with coredis.Redis.from_url(REDIS_URL, **options) as client:
        await client.flushdb()
        await client.function_flush()
        yield client


class TestScripts:
    async def test_a_call_returns_the_reply(self) -> None:
        async with connected() as client:
            assert await counter(client, key="k", by=2) == 2
            assert await counter(client, "k", 3) == 5

    async def test_a_reply_is_bytes(self) -> None:
        async with connected() as client:
            await client.set("k", "v")
            assert await fetch(client, key="k") == b"v"

    async def test_decode_responses_decodes_the_reply(self) -> None:
        """As with redis-py, a decoding client turns the bytes a script returns into str."""
        async with connected(decode_responses=True) as client:
            await client.set("k", "v")
            assert await fetch(client, key="k") == "v"

    async def test_registration_is_cached_per_client(self) -> None:
        async with connected() as client:
            assert await counter(client, key="k", by=1) == 1
            assert counter._for(client) is counter._for(client)

    async def test_a_script_survives_a_flushed_script_cache(self) -> None:
        async with connected() as client:
            assert await counter(client, key="k", by=1) == 1
            await client.script_flush()
            # EVALSHA now fails with NOSCRIPT; coredis's Script loads it and retries.
            assert await counter(client, key="k", by=1) == 2

    async def test_bind(self) -> None:
        async with connected() as client:
            bound = counter.bind(client)
            assert await bound(key="k", by=2) == 2
            assert await bound("k", 3) == 5

    async def test_a_pipeline(self) -> None:
        async with connected() as client:
            await client.script_flush()
            async with client.pipeline() as pipe:
                first = counter(pipe, key="k", by=2)
                second = counter(pipe, key="k", by=3)
            # coredis loads a pipeline's scripts before it executes the pipeline.
            assert (await first, await second) == (2, 5)

    async def test_a_generated_function(self) -> None:
        async with connected() as client:
            assert await generated_scripts.rate_limit(client, key="k", limit=2, ttl=60) == 1
            assert 0 < await client.ttl("k") <= 60


class TestLibraries:
    async def test_the_first_call_loads_the_library(self) -> None:
        async with connected() as client:
            assert await coredis_bump(client, key="n", by=5) == 5
            assert await coredis_bump(client, key="n", by=1) == 6

    async def test_a_no_writes_function(self) -> None:
        async with connected() as client:
            await library.load(client)
            await client.set("n", 4)
            assert await coredis_peek(client, key="n") == 4
            assert await coredis_peek(client, key="missing") == 0

    async def test_a_list_of_keys(self) -> None:
        async with connected() as client:
            assert await coredis_bump_all(client, keys=["a", "b", "a"]) == 4

    async def test_load_replaces(self) -> None:
        async with connected() as client:
            assert await library.load(client) == b"rlp_coredis"
            assert await library.load(client) == b"rlp_coredis"

    async def test_bind(self) -> None:
        async with connected() as client:
            assert await coredis_bump.bind(client)(key="n", by=2) == 2

    async def test_a_pipeline_after_loading(self) -> None:
        async with connected() as client:
            await library.load(client)
            async with client.pipeline() as pipe:
                bumped = coredis_bump(pipe, key="p", by=2)
                peeked = coredis_peek(pipe, key="p")
            assert (await bumped, await peeked) == (2, 2)

    async def test_another_error_is_not_swallowed(self) -> None:
        async with connected() as client:
            await client.lpush("n", ["x"])
            with pytest.raises(coredis.exceptions.ResponseError, match="wrong kind of value"):
                await coredis_bump(client, key="n", by=1)
