---
hide:
  - navigation
---

# redis-lua-py

Write Redis Lua scripts as real Python functions, not as strings. Compiled at
import, checked by `mypy`, sent with `EVALSHA`. Sync and async redis-py.

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

The body is never executed by Python. It is read as source when the module is
imported, compiled to Lua, and sent to Redis with `EVALSHA`. Your editor
highlights it, your linter sees it, and `mypy` checks the signature — none of
which is true of a string.

Define scripts at module level, where they compile once at import. A script
defined inside a function recompiles on every call, and one defined through
`exec` has no source to read and is refused.

```python
from redis import Redis

client = Redis()
remaining = rate_limit(client, key="user:42", limit=10, ttl=60)
```

Importing the client as `from redis import Redis` leaves the name `redis` free
for the script namespace, so the two never collide.

## Install

```bash
uv add redis-lua-py
```

See [Installation](installation.md) for other package managers and the
requirements, or go straight to the [Quickstart](quickstart.md).

## What you get

- **Lua you would have been willing to write.** Every script exposes its
  output at `.lua` — read it in review, paste it into `redis-cli`, check it
  into a golden test. See [What it compiles to](guide/generated-lua.md).
- **Command names checked at compile time**, against Redis' own command table,
  so a typo is refused where you can see it rather than raised inside a script
  whose whole purpose was to be atomic. See
  [Calling Redis commands](guide/calling-redis-commands.md).
- **`KEYS` and `ARGV` from the signature.** A parameter annotated `Key` becomes
  a key, which is also what Redis Cluster routes on. See
  [Keys and arguments](guide/keys-and-arguments.md).
- **The differences between Lua and Python closed or refused** — truthiness,
  1-based indexing, `false` versus `nil`, block scope. See
  [Where Lua differs from Python](reference/lua-vs-python.md).
- **Typed on the caller's side.** A script is a `CompiledScript[R]`, so
  `rate_limit(...)` returns an `int` rather than `Any`, and an async client
  gives you `Awaitable[R]`. See [What the caller gets](guide/return-values.md).
- **Sync and async from the same script object.** See [Async](guide/async.md).
- **Binary-safe throughout.** Nothing here decodes. See
  [Binary values](guide/binary-values.md).

## Where to go next

<div class="grid cards" markdown>

- **[Quickstart](quickstart.md)** — a script, a client, a result.
- **[The supported subset](reference/supported-subset.md)** — exactly what a
  script body may contain.
- **[Testing your scripts](guide/testing.md)** — golden Lua and fakeredis.
- **[API reference](reference/api.md)** — every exported name.

</div>
