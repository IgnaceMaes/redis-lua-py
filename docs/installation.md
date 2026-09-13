# Installation

```bash
uv add redis-lua-py
```

Or with pip:

```bash
pip install redis-lua-py
```

## Requirements

- Python 3.10 or newer
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

## For testing

[fakeredis](https://github.com/cunla/fakeredis-py) embeds a real Lua
interpreter, so your scripts execute for real without a server:

```bash
uv add --dev "fakeredis[lua]"
```

The `lua` extra is what brings the interpreter. See
[Testing your scripts](guide/testing.md).
