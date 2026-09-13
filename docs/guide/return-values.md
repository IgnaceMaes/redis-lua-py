# What the caller gets

The return annotation describes the **caller's** side: the value that comes
back from Redis, not the value the body hands to Lua. The compiler does not
read it at all.

It is carried through to the call, so a script is a `CompiledScript[R]` and
`rate_limit` returns an `int` rather than `Any`:

```python
remaining = rate_limit(client, key="user:42", limit=10, ttl=60)  # int
```

An async client gives you `Awaitable[R]`, so `await` gets you back to `R`.
[`bind`](binding-a-client.md) carries it too, on both.

## Annotate what Redis actually sends

Redis renders every reply as bytes, which is what to annotate — and what to
write in the body when a branch needs a placeholder:

```python
@script
def preview(doc: Key) -> list[int | bytes]:
    size = redis.strlen(doc)
    if size == 0:
        return [0, b""]
    return [1, redis.getrange(doc, 0, 1023)]
```

`b""` and `""` compile to the same Lua string; only one of them also describes
what the caller receives, which keeps the body and the signature agreeing
about the same thing. For a string built in the body, `str(n).encode()` does
the same: Lua strings are already bytes, so `encode()` compiles to nothing, and
the body type-checks against `-> bytes`.

Return values follow Redis' own conversion rules: `True` becomes `1`, `False`
and `None` become nil, floats are truncated to integers. Return a string, or
`cjson.encode(...)`, when you need one preserved exactly. A nil inside a
returned table truncates the reply — see
[Where Lua differs from Python](../reference/lua-vs-python.md#a-nil-inside-a-returned-table-truncates-the-reply).

## `warn_return_any` under mypy --strict

Inside a body, every value that came from Redis is `Any` — nothing about
`redis.get(k)` is knowable ahead of time. Under `mypy --strict` that makes
`warn_return_any` fire on a body that returns a command result directly, on
the one function whose body Python never runs. Turn it off for the module your
scripts live in:

```toml title="pyproject.toml"
[[tool.mypy.overrides]]
module = "myapp.scripts"
warn_return_any = false
```
