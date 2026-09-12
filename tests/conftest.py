"""Test fixtures.

By default everything runs against fakeredis, which embeds a real Lua
interpreter, so the suite needs no server. Set REDIS_URL to run the same tests
against a live Redis instead.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import fakeredis
import fakeredis.aioredis
import pytest

from redis_lua_py import CompiledScript

REDIS_URL = os.environ.get("REDIS_URL")

#: True when the suite is exercising a real server rather than fakeredis.
LIVE = REDIS_URL is not None


@pytest.fixture
def client() -> Iterator[Any]:
    if REDIS_URL:
        import redis

        conn = redis.Redis.from_url(REDIS_URL)
        conn.flushdb()
        yield conn
        conn.close()
    else:
        conn = fakeredis.FakeRedis()
        yield conn
        conn.close()


@pytest.fixture
async def async_client() -> AsyncIterator[Any]:
    if REDIS_URL:
        import redis.asyncio

        conn = redis.asyncio.Redis.from_url(REDIS_URL)
        await conn.flushdb()
        yield conn
    else:
        conn = fakeredis.aioredis.FakeRedis()
        yield conn
    # aclose arrived in redis-py 5, and close is deprecated from then on.
    await (conn.aclose() if hasattr(conn, "aclose") else conn.close())


@pytest.fixture
def body() -> Callable[[CompiledScript], str]:
    """The emitted Lua with comment lines removed.

    The generated header records an absolute source path, which differs per
    machine and would make any golden comparison useless.
    """

    def _body(script: CompiledScript) -> str:
        kept = [line for line in script.lua.splitlines() if not line.lstrip().startswith("--")]
        return "\n".join(kept).strip()

    return _body
