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
- [redis-py](https://github.com/redis/redis-py) 4.2 or newer — the only
  dependency, and installed with the package

Script caching, `EVALSHA` and the `NOSCRIPT` reload are handled by redis-py's
own script machinery, which this library defers to rather than reimplementing.
Anything redis-py can talk to, this can run against: a standalone server, a
cluster, Sentinel, or an in-process [fakeredis](guide/testing.md).

[coredis](https://github.com/alisaifee/coredis) clients work as well, and are
tested against, but coredis is not a dependency: install it yourself. See
[Async](guide/async.md#coredis).

## For testing

[fakeredis](https://github.com/cunla/fakeredis-py) embeds a real Lua
interpreter, so your scripts execute for real without a server:

```bash
uv add --dev "fakeredis[lua]"
```

The `lua` extra is what brings the interpreter. See
[Testing your scripts](guide/testing.md).
