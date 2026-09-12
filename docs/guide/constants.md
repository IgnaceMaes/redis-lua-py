# Constants from the module

A script has no closure: the body runs on the server, where nothing from your
Python process exists. A module-level constant is the exception worth making,
because it is already a literal and can simply be folded in.

```python
SESSION_TTL_SECONDS = 30 * 60


@script
def touch_session(session: Key) -> int:
    hits = redis.incr(session)
    redis.expire(session, SESSION_TTL_SECONDS)  # -> redis.call('EXPIRE', session, 1800)
    return hits
```

`int`, `float`, `str`, `bytes` and `bool` are folded, including through a
dotted name — an `IntEnum` member, or an attribute of a settings object.
Anything else is refused with the same caret as everything else, because there
is no literal to fold:

```
'SESSION_TTL' is a module-level timedelta, which has no Lua literal
  File "/srv/app/sessions.py", line 18
    redis.expire(session, SESSION_TTL)
                          ^
  hint: Only an int, float, str, bytes or bool constant is folded into the
  script. Pass anything else as an argument, or name the literal it reduces to.
```

!!! note "Folded once, at import"

    The value is read once, when the module is imported and the script
    compiles. A name rebound afterwards does not change the script — which is
    what "constant" means, but worth saying out loud.
