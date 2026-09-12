"""Running compiled scripts against Redis, sync and async."""

from __future__ import annotations

from typing import Any

from redis_lua_py import Key, redis, script


@script
def rate_limit(key: Key, limit: int, ttl: int) -> int:
    current = redis.incr(key)
    if current == 1:
        redis.expire(key, ttl)
    if current > limit:
        return -1
    return limit - current


@script
def claim_jobs(queue: Key, processing: Key, now: int, limit: int) -> list[bytes]:
    """Atomically move due jobs from a sorted set into a processing hash."""
    ids = redis.zrangebyscore(queue, 0, now, "LIMIT", 0, limit)
    claimed = []
    for job_id in ids:
        if redis.zrem(queue, job_id) == 1:
            redis.hset(processing, job_id, now)
            claimed.append(job_id)
    return claimed


def test_rate_limit_counts_down_then_refuses(client: Any) -> None:
    assert rate_limit(client, key="u", limit=2, ttl=60) == 1
    assert rate_limit(client, key="u", limit=2, ttl=60) == 0
    assert rate_limit(client, key="u", limit=2, ttl=60) == -1


def test_rate_limit_sets_the_expiry_once(client: Any) -> None:
    rate_limit(client, key="u", limit=5, ttl=60)
    assert 0 < client.ttl("u") <= 60


def test_positional_call(client: Any) -> None:
    assert rate_limit(client, "u", 5, 60) == 4


def test_claim_jobs_moves_only_due_work(client: Any) -> None:
    client.zadd("queue", {"a": 1, "b": 2, "later": 5_000})
    claimed = claim_jobs(client, queue="queue", processing="wip", now=100, limit=10)

    assert claimed == [b"a", b"b"]
    assert client.hgetall("wip") == {b"a": b"100", b"b": b"100"}
    assert client.zrange("queue", 0, -1) == [b"later"]


def test_claim_jobs_respects_the_limit(client: Any) -> None:
    client.zadd("queue", {"a": 1, "b": 2, "c": 3})
    assert claim_jobs(client, queue="queue", processing="wip", now=100, limit=2) == [b"a", b"b"]


def test_claim_jobs_on_an_empty_queue(client: Any) -> None:
    assert claim_jobs(client, queue="queue", processing="wip", now=100, limit=10) == []


def test_registration_is_cached_per_client(client: Any) -> None:
    """Re-registering on every call would rehash the body for nothing."""
    assert rate_limit(client, key="u", limit=5, ttl=60) == 4
    assert rate_limit._for(client) is rate_limit._for(client)


def test_script_survives_a_flushed_script_cache(client: Any) -> None:
    assert rate_limit(client, key="u", limit=5, ttl=60) == 4
    client.script_flush()
    # EVALSHA now fails with NOSCRIPT; redis-py reloads and retries.
    assert rate_limit(client, key="u", limit=5, ttl=60) == 3


async def test_async_execution(async_client: Any) -> None:
    assert await rate_limit(async_client, key="u", limit=2, ttl=60) == 1
    assert await rate_limit(async_client, key="u", limit=2, ttl=60) == 0
    assert await rate_limit(async_client, key="u", limit=2, ttl=60) == -1


async def test_async_claim_jobs(async_client: Any) -> None:
    await async_client.zadd("queue", {"a": 1, "b": 2})
    claimed = await claim_jobs(async_client, queue="queue", processing="wip", now=100, limit=10)
    assert claimed == [b"a", b"b"]
