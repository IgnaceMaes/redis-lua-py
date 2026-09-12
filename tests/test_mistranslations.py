"""Constructs that used to compile and then do the wrong thing.

Each of these produced Lua that either failed to load or ran and gave a wrong
answer, which is the one failure mode this compiler exists to rule out. They
run the script, not just inspect it.
"""

from __future__ import annotations

import math
from typing import Any

from redis_lua_py import Key, cjson, redis, script

UNLIMITED = math.inf
FLOOR = -5


def test_a_parameter_reassigned_in_a_block_keeps_its_value(client: Any) -> None:
    @script
    def clamp(n: int, ceiling: int) -> int:
        if n > ceiling:
            n = ceiling
        return n

    assert "local n\n" not in clamp.lua
    assert clamp(client, n=3, ceiling=5) == 3
    assert clamp(client, n=9, ceiling=5) == 5


def test_a_key_reassigned_in_a_loop_keeps_its_value(client: Any) -> None:
    @script
    def suffixed(k: Key, rounds: int) -> bytes:
        for i in range(rounds):
            k = f"{k}:{i}"
        return k

    assert suffixed(client, k="a", rounds=2) == b"a:0:1"
    assert suffixed(client, k="a", rounds=0) == b"a"


def test_infinity_folds_to_math_huge(client: Any) -> None:
    @script
    def bounded(n: int) -> int:
        if n < UNLIMITED:
            return 1
        return 0

    assert "math.huge" in bounded.lua
    assert bounded(client, n=10**12) == 1


def test_negating_a_negative_constant_is_not_a_comment(client: Any) -> None:
    @script
    def flipped(n: int) -> int:
        return n - -FLOOR

    assert "--" not in flipped.lua.split("\n", 2)[-1]
    assert flipped(client, n=10) == 5


def test_equals_none_matches_a_missing_value(client: Any) -> None:
    @script
    def state(k: Key) -> bytes:
        if redis.get(k) == None:  # noqa: E711
            return "absent"
        if None != redis.get(k):  # noqa: E711, SIM300 - both spellings are the point
            return "present"
        return "unreachable"

    assert "__isnil(redis.call('GET', k))" in state.lua
    assert "not __isnil(redis.call('GET', k))" in state.lua
    assert "== nil then" not in state.lua
    assert state(client, k="x") == b"absent"
    client.set("x", "1")
    assert state(client, k="x") == b"present"


def test_set_repl_and_redis_constants_pass_through() -> None:
    @script
    def touch(k: Key) -> int:
        redis.set_repl(redis.REPL_ALL)
        redis.log(redis.LOG_WARNING, "touched")
        return redis.incr(k)

    assert "redis.set_repl(redis.REPL_ALL)" in touch.lua
    assert "redis.log(redis.LOG_WARNING, 'touched')" in touch.lua
    assert "'SET', 'REPL'" not in touch.lua


def test_redis_constants_are_not_read_from_cjson() -> None:
    import pytest

    from redis_lua_py import UnsupportedSyntax

    with pytest.raises(UnsupportedSyntax, match="attribute access"):

        @script
        def s() -> int:
            return cjson.LOG_WARNING


def test_statements_after_return_are_dropped(client: Any) -> None:
    @script
    def first(n: int) -> int:
        for i in range(n):
            return i
            n = n + 1
        return n
        n = 0

    assert first(client, n=3) == 0
    assert first(client, n=0) == 0
