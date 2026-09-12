# Errors and warnings

Every error this package raises carries a caret under the line at fault, and a
hint saying what to write instead.

```
'and'/'or' are only supported in an if or while condition
  File "/srv/app/limits.py", line 12
    flag = a and b
           ^
  hint: In Python these return an operand, which does not survive the
  difference in truthiness. Use an if statement instead.
```

## The hierarchy

```
RedisLuaError
├── CompileError
│   └── UnsupportedSyntax
└── ScriptArgumentError

RedisLuaWarning  (a UserWarning)
└── NilTruncationWarning
```

## `RedisLuaError`

Base class for every error this package raises. Catch this to catch all of
them.

## `CompileError`

A Python function could not be turned into Lua. Raised at decoration time —
that is, when the module is imported.

## `UnsupportedSyntax`

The function used Python that has no meaning inside a Redis script. A subclass
of `CompileError`, raised when the body strays outside
[the supported subset](supported-subset.md).

Failing at import, loudly, is deliberate. A body that looks like Python but is
never run by Python is exactly where a quiet mistranslation would cost the
most.

## `ScriptArgumentError`

A script was called with the wrong keys or arguments — the wrong number of
them, an unknown keyword, two values for one parameter, or a missing one. This
is the only error in the list raised at call time rather than at import.

```
rate_limit() has no parameter 'tll'; did you mean 'ttl'? (parameters: key, limit, ttl)
```

## `RedisLuaWarning`

Base class for every warning this package raises. A `UserWarning`, so it is
shown by default and silenced the usual way.

## `NilTruncationWarning`

A returned table may hold a nil, which truncates the reply at that point —
Redis stops converting the array there, so the caller gets a shorter list
rather than a null in the middle of one. See
[Where Lua differs from Python](lua-vs-python.md#a-nil-inside-a-returned-table-truncates-the-reply).

Silence it if your script really does mean to stop there:

```python
import warnings

from redis_lua_py import NilTruncationWarning

warnings.filterwarnings("ignore", category=NilTruncationWarning)
```
