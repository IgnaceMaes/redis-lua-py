# Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run mypy
```

## The test suite

Tests run against [fakeredis](https://github.com/cunla/fakeredis-py), which
executes real Lua, so `uv run pytest` needs no server. Set `REDIS_URL` to also
run them against a live Redis:

```bash
REDIS_URL=redis://localhost:6379/0 uv run pytest
```

CI runs both, and the two agree — including on reply conversion, which is the
part you would most want a real server for.

## The command table

`src/redis_lua_py/_commands.py` is generated, not written. It is the table
command names are checked against, and it comes from the command definitions
in the Redis source. Refresh it when a Redis release adds commands, and commit
the result:

```bash
uv run python scripts/generate_commands.py 8.10.1
```

A stale table can never block a caller: `redis.call('NEW.CMD', ...)` is
deliberately never checked. See
[Calling Redis commands](guide/calling-redis-commands.md#the-escape-hatch).

## The docs site

This site is built with [Zensical](https://zensical.org). Serve it locally
with live reload:

```bash
uv run zensical serve
```

Build it the way CI does:

```bash
uv run zensical build --clean
```

`.github/workflows/docs.yml` publishes `main` to GitHub Pages on every push.
