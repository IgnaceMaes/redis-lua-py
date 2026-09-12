# Binary values

Nothing here decodes. `KEYS`, `ARGV` and every Lua string are byte strings, so
a `bytes` argument arrives in the script as exactly those bytes and comes back
as exactly those bytes.

```python
import zlib


@script
def cache_compressed(key: Key, blob: bytes, ttl: int) -> int:
    redis.set(key, blob)
    redis.expire(key, ttl)
    return len(blob)


cache_compressed(client, key="report:42", blob=zlib.compress(report), ttl=300)
```

A `bytes` annotation is a passthrough: no `tonumber`, no decoding, no round
trip through text. `memoryview` is accepted the same way. A `bytes` literal in
a body is emitted as numeric escapes — `b"\x00\xff"` becomes `'\000\255'` —
so it survives the journey to the server, where the script itself travels as
text.

This is a guarantee rather than an observation:
[tests/test_binary.py](https://github.com/ignacemaes/redis-lua-py/blob/main/tests/test_binary.py)
round-trips non-UTF-8 bytes through `ARGV`, through a stored value, and back
out of a returned `GETRANGE`, against both fakeredis and a real server.
