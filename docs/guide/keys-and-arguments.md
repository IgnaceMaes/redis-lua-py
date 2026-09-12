# Keys and arguments

A parameter annotated `Key` becomes `KEYS`, in declaration order. Everything
else becomes `ARGV`.

```python
@script
def rate_limit(key: Key, limit: int, ttl: int) -> int: ...
```

```lua
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
```

!!! warning "Annotate every key"

    This distinction is not cosmetic. Redis Cluster routes a script by its
    declared keys, and a key smuggled in as an argument is invisible to the
    router — the script will execute on the wrong node.

## Argument types

`ARGV` always arrives in Lua as a string. The annotation decides what happens
to it on the way in.

| Annotation | In the script |
| --- | --- |
| `Key` | `KEYS[n]`, and what the cluster routes on |
| `int`, `float` | wrapped in `tonumber`, so it is a number by the time your comparison runs |
| `str` | passed through as the string it already is |
| `bool` | encoded as `"1"` or `"0"` |
| `bytes`, `memoryview` | passed through untouched — see [Binary values](binary-values.md) |

### `float` carries a caveat

Annotate `float` for arithmetic, not for a value you mean to write back
unchanged. `tonumber` makes it a Lua number, and Lua renders a number back to
text with `%.14g`, so a value with more significant digits than that does not
come back as it went in. Annotate `str` and call `str()` at the call site when
the value is only being carried.

### `bool` deletes a conditional

`bool` encodes to `"1"` or `"0"`. Paired with an `int` annotation that deletes
the `1 if flag else 0` from the call site: pass `True`, and the body gets `1`.

## Positional or keyword

Scripts accept either; keyword is clearer at the call site and is what the
errors suggest.

```python
rate_limit(client, key="user:42", limit=10, ttl=60)
rate_limit(client, "user:42", 10, 60)
```

A call with the wrong number or the wrong names raises
[`ScriptArgumentError`](../reference/errors.md).
