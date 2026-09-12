"""The places where Lua and Python disagree, and what we do about them.

Each test here guards a translation that would otherwise be silently wrong.
They run the script as well as inspecting it, because "it compiles" is not the
claim being made.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from redis_lua_py import CompiledScript, Key, redis, script

Body = Callable[[CompiledScript], str]


class TestMissingValues:
    """Redis reports an absent value to Lua as false, never as nil."""

    def test_is_none_uses_the_helper(self, body: Body) -> None:
        @script
        def s(k: Key) -> str:
            if redis.get(k) is None:
                return "absent"
            return "present"

        emitted = body(s)
        assert "__isnil(redis.call('GET', k))" in emitted
        # A bare `== nil` here is the classic bug: it never matches.
        assert "== nil then" not in emitted

    def test_is_none_detects_a_missing_key(self, client: Any) -> None:
        @script
        def s(k: Key) -> str:
            if redis.get(k) is None:
                return "absent"
            return "present"

        assert s(client, k="nope") == b"absent"
        client.set("yes", "1")
        assert s(client, k="yes") == b"present"

    def test_is_not_none(self, client: Any) -> None:
        @script
        def s(k: Key) -> str:
            if redis.hget(k, "field") is not None:
                return "present"
            return "absent"

        assert s(client, k="h") == b"absent"
        client.hset("h", "field", "v")
        assert s(client, k="h") == b"present"

    def test_operand_is_evaluated_once(self, client: Any) -> None:
        """`is None` on a mutating command must not run it twice."""

        @script
        def s(k: Key) -> int:
            if redis.incr(k) is None:
                return -1
            return redis.get(k)

        assert int(s(client, k="counter")) == 1


class TestTruthiness:
    """Lua counts 0 and '' as true; Python does not."""

    def test_zero_is_falsy(self, client: Any) -> None:
        @script
        def s(n: int) -> str:
            if n:
                return "truthy"
            return "falsy"

        assert s(client, n=0) == b"falsy"
        assert s(client, n=1) == b"truthy"

    def test_empty_string_is_falsy(self, client: Any) -> None:
        @script
        def s(text: str) -> str:
            if text:
                return "truthy"
            return "falsy"

        assert s(client, text="") == b"falsy"
        assert s(client, text="x") == b"truthy"

    def test_empty_table_is_falsy(self, client: Any) -> None:
        @script
        def s(k: Key) -> str:
            if redis.lrange(k, 0, -1):
                return "truthy"
            return "falsy"

        assert s(client, k="empty") == b"falsy"
        client.rpush("full", "a")
        assert s(client, k="full") == b"truthy"

    def test_comparisons_skip_the_helper(self, body: Body) -> None:
        @script
        def s(n: int) -> int:
            if n > 5:
                return 1
            return 0

        # Already a boolean; wrapping it would just be noise.
        assert "__truthy" not in body(s)

    def test_not_and_boolops_in_conditions(self, client: Any) -> None:
        @script
        def s(a: int, b: int) -> str:
            if a > 0 and b > 0:
                return "both"
            if not a:
                return "no-a"
            return "other"

        assert s(client, a=1, b=1) == b"both"
        assert s(client, a=0, b=1) == b"no-a"
        assert s(client, a=1, b=0) == b"other"


class TestIndexing:
    """Lua tables start at 1."""

    def test_literal_index_is_folded(self, body: Body) -> None:
        @script
        def s(k: Key) -> str:
            items = redis.lrange(k, 0, -1)
            return items[0]

        assert "return items[1]" in body(s)

    def test_computed_index_is_offset(self, body: Body) -> None:
        @script
        def s(k: Key, i: int) -> str:
            items = redis.lrange(k, 0, -1)
            return items[i]

        assert "return items[i + 1]" in body(s)

    def test_string_key_is_not_offset(self, body: Body) -> None:
        @script
        def s(k: Key) -> str:
            payload = {"name": "x"}
            return payload["name"]

        assert "payload.name" in body(s)

    def test_first_element_at_runtime(self, client: Any) -> None:
        @script
        def s(k: Key) -> str:
            items = redis.lrange(k, 0, -1)
            return items[0]

        client.rpush("l", "first", "second")
        assert s(client, k="l") == b"first"


class TestScope:
    """Python scopes a name to the function; Lua's local scopes it to a block."""

    def test_name_first_assigned_in_a_block_is_hoisted(self, body: Body) -> None:
        @script
        def s(n: int) -> int:
            if n > 0:
                result = 1
            else:
                result = 2
            return result

        emitted = body(s)
        assert emitted.startswith("local n = tonumber(ARGV[1])\nlocal result")
        assert "local result = 1" not in emitted

    def test_hoisted_name_survives_the_block(self, client: Any) -> None:
        @script
        def s(n: int) -> int:
            if n > 0:
                result = 10
            else:
                result = 20
            return result

        assert s(client, n=1) == 10
        assert s(client, n=-1) == 20

    def test_top_level_first_assignment_is_not_hoisted(self, body: Body) -> None:
        @script
        def s(n: int) -> int:
            total = 0
            if n > 0:
                total = total + n
            return total

        emitted = body(s)
        # A local declared at the top level already covers the nested write.
        assert "local total = 0" in emitted
        assert "local total\n" not in emitted

    def test_reassignment_after_the_block(self, client: Any) -> None:
        @script
        def s(n: int) -> int:
            total = 0
            for i in range(n):
                total = total + i
            return total

        assert s(client, n=5) == 10


class TestStrings:
    def test_fstring_compiles_to_concat(self, body: Body) -> None:
        @script
        def s(a: str, b: str) -> str:
            return f"{a}-{b}!"

        # .. is right-associative, so a correct fold needs no parentheses.
        assert "return tostring(a) .. '-' .. tostring(b) .. '!'" in body(s)

    def test_fstring_at_runtime(self, client: Any) -> None:
        @script
        def s(a: str, n: int) -> str:
            return f"{a}={n}"

        assert s(client, a="x", n=7) == b"x=7"

    def test_quotes_are_escaped(self, body: Body) -> None:
        @script
        def s(k: Key) -> str:
            return "it's\na test"

        assert r"'it\'s\na test'" in body(s)


class TestReturnConversion:
    """Redis' own Lua-to-reply rules, which we document rather than fight."""

    def test_true_becomes_one(self, client: Any) -> None:
        @script
        def s() -> bool:
            return True

        assert s(client) == 1

    def test_false_becomes_nil(self, client: Any) -> None:
        @script
        def s() -> bool:
            return False

        assert s(client) is None

    def test_float_is_truncated(self, client: Any) -> None:
        @script
        def s() -> float:
            return 3.7

        assert s(client) == 3

    def test_table_becomes_a_list(self, client: Any) -> None:
        @script
        def s() -> list[str]:
            out = []
            out.append("a")
            out.append("b")
            return out

        assert s(client) == [b"a", b"b"]
