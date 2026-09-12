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

## A variable number of keys or arguments

Annotate a parameter `list[Key]` to take any number of keys, or `list[str]`,
`list[int]` and so on to take any number of arguments. Its elements fill
whatever is left of `KEYS` or `ARGV` after the fixed parameters, wherever the
list is declared, and arrive in the body as a table:

```python
@script
def delete_tagged(tag: Key, keys: list[Key], stamp: int) -> int:
    redis.set(tag, stamp)
    return redis.delete(*keys)


delete_tagged(client, tag="purged", keys=["a", "b", "c"], stamp=1700000000)
```

```lua
local tag = KEYS[1]
local stamp = tonumber(ARGV[1])
local keys = {}
for __i1 = 2, #KEYS do
  keys[#keys + 1] = KEYS[__i1]
end
redis.call('SET', tag, stamp)
return redis.call('DEL', unpack(keys))
```

A script can take one list of keys and one list of arguments, since `KEYS`
and `ARGV` only have positions to tell them apart. Every element of a
`list[Key]` is a declared key, so Redis Cluster routes on all of them. An
element annotation of `int` or `float` converts each element, as it would a
single argument. Passing a string where a list is expected raises
[`ScriptArgumentError`](../reference/errors.md#scriptargumenterror) rather
than spreading it into characters.

## Positional or keyword

Scripts accept either; keyword is clearer at the call site and is what the
errors suggest.

```python
rate_limit(client, key="user:42", limit=10, ttl=60)
rate_limit(client, "user:42", 10, 60)
```

A call with the wrong number or the wrong names raises
[`ScriptArgumentError`](../reference/errors.md).
