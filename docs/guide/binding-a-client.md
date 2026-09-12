# Binding a client

Passing the client to every call gets repetitive. `bind` attaches one:

```python
limiter = rate_limit.bind(client)

limiter(key="user:42", limit=10, ttl=60)
limiter(key="user:43", limit=10, ttl=60)
```

A bound script exposes the same `.lua`, `.keys` and `.args` as the original,
binds async clients just as well, and leaves the unbound form working — the
script itself is unchanged and still usable against any other client.

```python
limiter = rate_limit.bind(async_client)
remaining = await limiter(key="user:42", limit=10, ttl=60)
```

The result is a [`BoundScript[T]`](../reference/api.md#boundscript), where `T`
is the script's own return type for a sync client and an awaitable of it for an
async one — so `await` still gets you back to the annotated type.
