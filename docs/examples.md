# Examples

## Rate limiting

The whole of a fixed-window limiter, atomic because it is one script:

```python
from redis_lua_py import Key, redis, script


@script
def rate_limit(key: Key, limit: int, ttl: int) -> int:
    current = redis.incr(key)
    if current == 1:
        redis.expire(key, ttl)
    if current > limit:
        return -1
    return limit - current
```

```python
remaining = rate_limit(client, key="user:42", limit=10, ttl=60)
if remaining < 0:
    raise TooManyRequests
```

## Claiming due jobs from a queue

```python
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
```

```lua
local queue = KEYS[1]
local processing = KEYS[2]
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ids = redis.call('ZRANGEBYSCORE', queue, 0, now, 'LIMIT', 0, limit)
local claimed = {}
for __i1 = 1, #ids do
  local job_id = ids[__i1]
  if redis.call('ZREM', queue, job_id) == 1 then
    redis.call('HSET', processing, job_id, now)
    claimed[#claimed + 1] = job_id
  end
end
return claimed
```

Two keys, declared as keys — which is what Redis Cluster routes on. See
[Keys and arguments](guide/keys-and-arguments.md).

## A session touch, with a folded constant

```python
SESSION_TTL_SECONDS = 30 * 60


@script
def touch_session(session: Key) -> int:
    hits = redis.incr(session)
    redis.expire(session, SESSION_TTL_SECONDS)
    return hits
```

The constant is read once, at import, and folded into the script as `1800`.
See [Constants from the module](guide/constants.md).

## Caching a compressed blob

```python
import zlib


@script
def cache_compressed(key: Key, blob: bytes, ttl: int) -> int:
    redis.set(key, blob)
    redis.expire(key, ttl)
    return len(blob)


cache_compressed(client, key="report:42", blob=zlib.compress(report), ttl=300)
```

A `bytes` annotation is a passthrough — no `tonumber`, no decoding, no round
trip through text. See [Binary values](guide/binary-values.md).

## Returning a table of mixed types

```python
@script
def preview(doc: Key) -> list[int | bytes]:
    size = redis.strlen(doc)
    if size == 0:
        return [0, b""]
    return [1, redis.getrange(doc, 0, 1023)]
```

Both branches return a table of the same shape, which is what keeps a nil from
truncating the reply. See
[What the caller gets](guide/return-values.md).
