# Installation

```bash
uv add redis-lua-py
```

Or with pip:

```bash
pip install redis-lua-py
```

## Requirements

- Python 3.10 or newer. Modules [generated ahead of time](guide/build-time.md)
  run on Python 3.9 too, without this package installed.
- A Redis client: [redis-py](https://github.com/redis/redis-py) 4.2 or newer,
  or [coredis](https://github.com/alisaifee/coredis)

The package has no dependencies of its own. It only calls the client you pass
it, so it never installs one: add redis-py or coredis yourself, as you would
to talk to Redis at all.

Script caching, `EVALSHA` and the `NOSCRIPT` reload are handled by redis-py's
own script machinery, which this library defers to rather than reimplementing.
Anything redis-py can talk to, this can run against: a standalone server, a
cluster, Sentinel, or an in-process [fakeredis](guide/testing.md).

[coredis](https://github.com/alisaifee/coredis) clients work as well, and are
tested against. See [Async](guide/async.md#coredis).

### A client wrapper

An application that wraps its client, to prefix keys per tenant for example,
needs one method: `register_script(source)`. It returns a callable with the
shape of redis-py's `Script`, `(keys=..., args=..., client=...)`, and a script
runs through it:

```python
class PrefixedScript:
    def __init__(self, owner: "PrefixedClient", script: Script) -> None:
        self._owner, self._script = owner, script

    def __call__(self, keys=(), args=(), client=None):
        keys = [f"{self._owner.prefix}:{key}" for key in keys]
        return self._script(keys=keys, args=args)


class PrefixedClient:
    def register_script(self, source: str) -> PrefixedScript:
        return PrefixedScript(self, self._redis.register_script(source))
```

The wrapper's other methods never come into it, so an `eval` with its own
signature is fine.

## For testing

[fakeredis](https://github.com/cunla/fakeredis-py) embeds a real Lua
interpreter, so your scripts execute for real without a server:

```bash
uv add --dev "fakeredis[lua]"
```

The `lua` extra is what brings the interpreter. See
[Testing your scripts](guide/testing.md).
