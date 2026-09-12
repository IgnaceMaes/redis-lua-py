# Async

The same script object works with either client. Pass a sync client and you get
a value; pass an async one and you get an awaitable.

```python
from redis.asyncio import Redis

client = Redis()
remaining = await rate_limit(client, key="user:42", limit=10, ttl=60)
```

There is no separate decorator, no separate script, and no separate
compilation — one `@script` serves both, and the overloads on `__call__` mean
a type checker knows which of the two you are looking at:

```python
sync_client: redis.Redis
async_client: redis.asyncio.Redis

a = rate_limit(sync_client, key="u:1", limit=10, ttl=60)   # int
b = rate_limit(async_client, key="u:1", limit=10, ttl=60)  # Awaitable[int]
```

Script caching, `EVALSHA`, and the `NOSCRIPT` reload are handled by redis-py's
own script machinery, which this defers to rather than reimplementing.

[`bind`](binding-a-client.md) works on async clients just as well, and carries
the awaitable through.
