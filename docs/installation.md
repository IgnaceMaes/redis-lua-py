# Installation

```bash
uv add redis-lua-py
```

Or with pip:

```bash
pip install redis-lua-py
```

## Requirements

- Python 3.11 or newer
- [redis-py](https://github.com/redis/redis-py) 5.0 or newer — the only
  dependency, and installed with the package

Script caching, `EVALSHA` and the `NOSCRIPT` reload are handled by redis-py's
own script machinery, which this library defers to rather than reimplementing.
Anything redis-py can talk to, this can run against: a standalone server, a
cluster, Sentinel, or an in-process [fakeredis](guide/testing.md).

## For testing

[fakeredis](https://github.com/cunla/fakeredis-py) embeds a real Lua
interpreter, so your scripts execute for real without a server:

```bash
uv add --dev "fakeredis[lua]"
```

The `lua` extra is what brings the interpreter. See
[Testing your scripts](guide/testing.md).
