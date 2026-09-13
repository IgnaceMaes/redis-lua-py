# Where Lua differs from Python

These are the gaps that matter. Most are closed for you; the rest are refused.

## Truthiness is closed

Lua counts `0` and `''` as true. Any condition that is not known to be a
boolean is routed through a generated `__truthy` helper, so `if count:` means
what it means in Python. A comparison, `not`, `startswith()`, `endswith()`, a
`bool` parameter, and a name only ever assigned one of those are used as they
are, and `and`/`or` between them compile to Lua's own operators.

## Missing values are closed

A Redis command with nothing to return hands Lua `false`, not `nil`. This is
the classic trap: a hand-written `== nil` never matches, so the branch silently
never runs. `x is None` compiles to a helper accepting both, which also takes
`x` as an argument — so `if redis.hget(k, f) is None:` does not run the command
twice. `x == None` and `x != None` compile to the same helper, since that is
plainly what they mean.

## Indexing is closed

Lua tables are 1-based. `items[0]` compiles to `items[1]`. Write Python indices
and let the compiler shift them.

A dict key must not be shifted, and Lua cannot tell a list from a dict. So the
compiler looks first at what is subscripted. A dict is indexed by its keys as
they are, integer keys included, and a list's index is shifted by one. When
that is not known, it looks at the subscript:

- a string literal, or a value known to be a string, is used as it is;
- an integer literal, or a value known to be a number, is shifted by one;
- anything else goes through a small `__key` helper, which shifts numbers and
  leaves everything else alone, at runtime.

A value's type is known from its annotation, a literal, the builtin, method or
operator that produced it, a `range()` or `enumerate()` loop variable, or
every assignment to the name agreeing. So after `counts = {}`, `counts[0]` is
the key `0`. A table that comes from elsewhere -- a Redis reply,
`cjson.decode`, a helper's parameter -- is not known, and an integer subscript
on it is taken to be a position.

`items[-1]` compiles to `items[#items]`, which needs a name to count back from.
Indexing a string gives a one-character string, as it does in Python, through
`string.sub`. Slices, `v[i:j]` and `v[i:j:k]`, work on lists and strings, with
negative and missing bounds, and bounds are clamped exactly as Python clamps
them. A negative step walks backwards, so `word[::-1]` reverses.

## Assignment scope is closed

Python scopes a name to the whole function; Lua's `local` scopes it to the
enclosing block. A name assigned inside an `if` and read after it is hoisted to
the top of the script, so it does not silently read back `nil`.

## Strings are closed

Lua's `+` is only arithmetic. It will add `"1" + "2"` to `3`. So `+`
compiles to Lua's `..` wherever either side is known to be a string, as
above, to Lua's `+` wherever either side is known to be a number, and
`"=" * n` to `string.rep`. Where neither side is known, as with two Redis
replies, a small `__add` helper decides at runtime: two strings are joined,
two lists are joined into a new list, and anything else is added.

The string methods compile to Lua's string library, and to small helpers where
Python means something Lua's own functions do not:

- **`find`, `replace`, `split`, `startswith` and `endswith`** take a plain
  substring. `string.find` would read `.` as a pattern.
- **`strip`, `lstrip` and `rstrip`** strip whitespace, and take no argument.
- **`x in s`** is a substring test on a string. On a list it is an element test;
  on a dict it is a key test. A table whose type is not known, such as a
  decoded JSON value, is taken to be a dict when any of its keys is not a list
  position, and a list otherwise.

`"%s: %d" % (name, n)` and f-string format specs such as `{price:8.2f}` compile
to `string.format`. Width, precision, sign and zero padding are supported. A
string aligns left and a number right, as in Python, so a width with no type,
on a value of unknown type, asks for one. Fill characters, explicit alignment
and grouping are refused.

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

`for k in d` walks the keys of a dict the compiler knows to be one, as
`.keys()` does, and `len(d)` counts them. On a table whose type is not known,
say `.keys()`: there, `for k in d` and `len(d)` see only list positions, which
a dict does not have.

## Redis replies are flat lists, not dicts

To Lua, `HGETALL` returns `[field, value, field, value, ...]`, not a table
keyed by field, so `.items()` on it does not mean what it would in redis-py.
Walk it in pairs instead:

```python
fields = redis.hgetall(k)
for i in range(0, len(fields), 2):
    redis.hset(copy, fields[i], fields[i + 1])
```

## `continue` is closed

Lua 5.1 has neither `continue` nor `goto`. A loop body that uses `continue`
runs inside `repeat ... until true`, a block that runs once, and `continue`
leaves that block. A `break` in the same loop sets a flag on the way out, and
the loop breaks on it.

```lua
for i = 0, n - 1 do
  repeat
    if i % 2 == 0 then
      do break end
    end
    total = total + i
  until true
end
```

A loop without `continue` compiles as it always did.

## Exceptions are messages

Lua has errors, not exception classes. `try` compiles to a local function run
under `pcall`, so an error from `redis.call`, from `raise`, or from Lua itself
lands in the `except` block. `except Exception as e` binds `e` to the error's
message, as a string.

That shapes what is accepted:

- **One `except` clause**, bare or `except Exception`. An error carries no
  type, so `except ValueError` would quietly catch everything, and is refused.
  Branch on the message instead.
- **`raise SomeError("message")`** compiles to `error("message", 0)`. The class
  is not kept, and the caller receives an error reply carrying the message. A
  bare `raise` inside `except` raises the caught error again.
- **`finally`** runs after the `try`, `except` and `else` blocks, and an error
  that was not handled is raised again once it has run.
- **`return` inside `try`** returns from the script, as it would in Python.
- **`break` and `continue` cannot leave a `try` body**, because it runs as a
  separate function. Set a flag inside the `try` and act on it after.

A script is atomic but not transactional: writes made before an error are
kept, whether or not something catches it.

## A loop variable does not outlive its loop

Unlike in Python. Lua also gives each step of a loop a variable of its own,
which a function defined inside the loop would keep, where in Python every
step shares one. So a `lambda` or a helper function that reads a loop variable
is refused; pass the value in as a parameter instead.

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
