# Where Lua differs from Python

These are the gaps that matter. Most are closed for you; the rest are refused.

## Truthiness is closed

Lua counts `0` and `''` as true. Any condition that is not already a boolean is
routed through a generated `__truthy` helper, so `if count:` means what it
means in Python.

## Missing values are closed

A Redis command with nothing to return hands Lua `false`, not `nil`. This is
the classic trap: a hand-written `== nil` never matches, so the branch silently
never runs. `x is None` compiles to a helper accepting both, which also takes
`x` as an argument — so `if redis.hget(k, f) is None:` does not run the command
twice. `x == None` and `x != None` compile to the same helper, since that is
plainly what they mean.

## Indexing is closed

Lua tables are 1-based. `items[0]` compiles to `items[1]`. Write Python indices
and let the compiler shift them. Negative indices are refused, because Lua has
no equivalent.

## Assignment scope is closed

Python scopes a name to the whole function; Lua's `local` scopes it to the
enclosing block. A name assigned inside an `if` and read after it is hoisted to
the top of the script, so it does not silently read back `nil`.

## `+` is arithmetic, not concatenation

Use an f-string, which compiles to Lua's `..`.

## `and`, `or` and conditional expressions are closed

In Python, `a or b` returns one of its operands, chosen by Python's
truthiness. Lua's own `or` uses Lua's truthiness, where `0` and `''` are true,
so the Lua idiom `tonumber(x) or 0` means something different there.

A right side that is a name or a literal compiles to a small `__or` / `__and`
helper. Anything else is wrapped in a function called on the spot, so that it
only runs when Python would run it. `flag or redis.incr(k)` does not
increment when `flag` is set.

`a if c else b` compiles to `c and a or b` when `a` is a literal. Otherwise it
becomes the same kind of function, because `c and a or b` is wrong whenever
`a` can be false or nil.

## Dicts iterate in no particular order

`.items()`, `.keys()` and `.values()` compile to Lua's `pairs()`, which visits
entries in no fixed order. Sort the result if the order reaches the caller.

Iterating a dict directly with `for k in d` walks its array part, which a dict
does not have, so the loop never runs. Say `.keys()`.

## Redis replies are flat lists, not dicts

To Lua, `HGETALL` returns `[field, value, field, value, ...]`, not a table
keyed by field, so `.items()` on it does not mean what it would in redis-py.
Walk it in pairs instead:

```python
fields = redis.hgetall(k)
for i in range(0, len(fields), 2):
    redis.hset(copy, fields[i], fields[i + 1])
```

## There is no `continue`

Lua 5.1 does not have one. Invert the condition and nest the rest of the body.

## A loop variable does not outlive its loop

Unlike in Python.

## A nil inside a returned table truncates the reply

Redis converts a returned array by walking it from the first element and
stopping at the first `nil`, so the caller gets a shorter list rather than a
null in the middle of one.

Which values are actually nil is the part worth being exact about. A command
with nothing to return hands Lua `false`, and `false` converts to a null
*element* without ending the array — `return [1, redis.get(missing), 3]` really
does reach the caller as `[1, None, 3]`. What truncates is a genuine nil, and
in practice that means a name that was not assigned on this path. The compiler
warns where it can see one:

```
'first' is not assigned on every path to this return, and a nil in a returned
table truncates the reply there
  File "/srv/app/queue.py", line 31
    return [1, first, count]
               ^
  hint: Give it a value before the branch, so that every branch returns a
  table of the same shape.
```

and refuses a `None` written out in the table, since that one is never what
anybody meant. Silence the warning with:

```python
import warnings

from redis_lua_py import NilTruncationWarning

warnings.filterwarnings("ignore", category=NilTruncationWarning)
```

if your script really does mean to stop there.

## Return values follow Redis' own conversion rules

`True` becomes `1`, `False` and `None` become nil, floats are truncated to
integers. Return a string, or `cjson.encode(...)`, when you need one preserved
exactly. See [What the caller gets](../guide/return-values.md).
