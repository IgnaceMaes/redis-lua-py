# Testing your scripts

Replacing working Lua in a production path needs evidence. Two things supply
most of it, and both are short.

## Snapshot the Lua

`.lua` is the whole script, so a golden test is a string comparison — and the
diff against the Lua you are replacing is the review.

```python
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden" / "rate_limit.lua"


def test_generated_lua_is_unchanged():
    assert rate_limit.lua == GOLDEN.read_text()
```

The header path is repo-relative, so this is stable across machines and CI.
Use `@script(header=False)` if you would rather compare the body alone — see
[What it compiles to](generated-lua.md#the-header-and-the-sha).

## Run the behaviour, without a server

[fakeredis](https://github.com/cunla/fakeredis-py) embeds a real Lua
interpreter, so your script executes for real against an in-process server:

```python
import fakeredis


def test_rate_limit_refuses_past_the_limit():
    client = fakeredis.FakeRedis()

    assert rate_limit(client, key="u:42", limit=2, ttl=60) == 1
    assert rate_limit(client, key="u:42", limit=2, ttl=60) == 0
    assert rate_limit(client, key="u:42", limit=2, ttl=60) == -1
```

Install it with:

```bash
uv add --dev "fakeredis[lua]"
```

The `lua` extra is what brings the interpreter. This library's own suite runs
that way and against a real Redis in CI, and the two agree — including on reply
conversion, which is the part you would most want a real server for.
