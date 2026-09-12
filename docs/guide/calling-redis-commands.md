# Calling Redis commands

`redis.<command>(...)` becomes `redis.call('<COMMAND>', ...)`. Underscores
split into subcommand tokens, so `redis.script_load(x)` compiles to
`redis.call('SCRIPT', 'LOAD', x)`.

`redis.pcall`, `redis.error_reply`, `redis.status_reply`, `redis.sha1hex`,
`redis.log` and `cjson.encode` / `cjson.decode` pass through under their own
names.

## Names are checked, not just uppercased

Uppercasing turns *any* attribute into a plausible command, which makes a name
Redis does not have the one mistake with nothing standing in its way: it
compiles, it survives review, and it raises the first time its branch runs —
inside a script whose whole purpose was to be atomic.

So command names are checked at compile time against Redis' own command table,
and a miss is refused where you can see it:

```
Redis has no EXPIRES command
  File "/srv/app/limits.py", line 14
    redis.expires(key, 60)
    ^
  hint: Did you mean redis.expire()?
```

The handful of redis-py method names that do not match the wire name are
translated rather than refused, because each names exactly one command and
nothing else: `redis.delete(k)` compiles to `redis.call('DEL', k)`. Container
commands are checked down to the subcommand, and a hyphenated one is reached
through its underscores — `redis.client_no_evict("on")` compiles to
`redis.call('CLIENT', 'NO-EVICT', 'on')`.

## The escape hatch

`redis.call(...)` is deliberately never checked. It is the escape hatch for
module commands, which are spelled with a dot anyway, and for anything a newer
server has that the table does not:

```python
redis.call("JSON.SET", doc, "$.status", '"done"')
```

The table is generated from the command definitions in the Redis source — the
same files the server is built from — and currently tracks Redis 8.10.
Regenerate it with:

```bash
uv run python scripts/generate_commands.py
```

## When the client is imported too

Import the client *class* and nothing collides, because the name `redis` is
never taken:

```python
from redis import Redis
from redis_lua_py import Key, redis, script
```

If you want the client module itself, the namespace is resolved by value rather
than by spelling, so import it under any name you like:

```python
import redis  # the client
from redis_lua_py import Key, script
from redis_lua_py import redis as r  # the script namespace


@script
def claim(queue: Key, now: int) -> list[bytes]:
    return r.zrangebyscore(queue, 0, now)


client = redis.Redis()
```

`call` is also exported as an alias of `redis`, if you would rather rename
nothing at all.

Getting this wrong is caught rather than compiled. If the name in scope turns
out to be redis-py, the script is refused instead of being quietly aimed at the
client library:

```
'redis' is bound to redis-py here, not to the script namespace
  File "/srv/app/jobs.py", line 9
    return redis.zrangebyscore(queue, 0, now)
           ^
  hint: Import the namespace under another name (from redis_lua_py import
  redis as r), or the client under another name (import redis as redis_client).
```
