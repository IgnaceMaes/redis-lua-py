"""The return annotation describes the caller's side, and reaches the caller.

This file is checked by mypy as well as run by pytest -- ``assert_type`` is a
no-op at runtime and an assertion at type-check time, so the claims below fail
the build rather than merely being documented. See ``[tool.mypy]`` in
pyproject.toml, which lists this file alongside ``src``.
"""

from __future__ import annotations

import sys

import redis
import redis.asyncio

import generated_scripts
from redis_lua_py import BoundScript, CompiledScript, Key, Library, LibraryFunction, script
from redis_lua_py import redis as r

if sys.version_info >= (3, 11):
    from typing import assert_type
else:
    from typing_extensions import assert_type


@script
def counter(k: Key) -> int:
    return r.incr(k)


@script
def payload(k: Key) -> bytes:
    """Redis renders every reply as bytes; that is what the caller gets."""
    return r.get(k)


def check_the_script_object() -> None:
    assert_type(counter, CompiledScript[int])
    assert_type(payload, CompiledScript[bytes])


def check_a_sync_call(client: redis.Redis) -> None:
    assert_type(counter(client, k="x"), int)
    assert_type(payload(client, k="x"), bytes)


def check_a_sync_bind(client: redis.Redis) -> None:
    bound = counter.bind(client)
    assert_type(bound, BoundScript[int])
    assert_type(bound(k="x"), int)


async def check_an_async_call(client: redis.asyncio.Redis) -> None:
    assert_type(await counter(client, k="x"), int)
    assert_type(await payload(client, k="x"), bytes)


async def check_an_async_bind(client: redis.asyncio.Redis) -> None:
    bound = counter.bind(client)
    assert_type(await bound(k="x"), int)


typed_library = Library("typing_checks")


@typed_library.function
def counted(k: Key) -> int:
    return r.incr(k)


def check_a_library_function(client: redis.Redis) -> None:
    assert_type(counted, LibraryFunction[int])
    assert_type(counted(client, k="x"), int)
    assert_type(counted.bind(client)(k="x"), int)


async def check_an_async_library_function(client: redis.asyncio.Redis) -> None:
    assert_type(await counted(client, k="x"), int)


def check_a_generated_function(client: redis.Redis) -> None:
    assert_type(generated_scripts.rate_limit(client, key="x", limit=1, ttl=1), int)
    assert_type(generated_scripts.echo(client, "a", suffix="b"), bytes)
    assert_type(generated_scripts.touch_all(client, keys=["a", b"b"], ttl=1), int)


async def check_an_async_generated_function(client: redis.asyncio.Redis) -> None:
    assert_type(await generated_scripts.rate_limit(client, key="x", limit=1, ttl=1), int)


def test_the_annotation_does_not_change_what_runs() -> None:
    """The compiler ignores the annotation; only the caller's types see it."""
    assert counter.name == "counter"
    assert "redis.call('INCR', k)" in counter.lua
