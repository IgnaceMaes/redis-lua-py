"""The places where Lua and Python disagree, and what we do about them.

Each test here guards a translation that would otherwise be silently wrong.
They run the script as well as inspecting it, because "it compiles" is not the
claim being made.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

import pytest

from redis_lua_py import (
    CompiledScript,
    Key,
    NilTruncationWarning,
    UnsupportedSyntax,
    redis,
    script,
)

Body = Callable[[CompiledScript[object]], str]

#: A module-level constant that folds to nil, for the refusal below.
NOTHING = None


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


class TestTruncatedReplies:
    """A nil inside a returned table cuts the reply off at that point.

    Redis converts a returned Lua array by walking it from index 1 and stopping
    at the first nil, so the caller sees a shorter list rather than a null in
    the middle. Which values are actually nil is the part worth being exact
    about: a command with nothing to return hands Lua ``false``, not ``nil``,
    and false converts to a null *element* without ending the array.
    """

    def test_a_missing_value_is_a_null_element_not_a_truncation(self, client: Any) -> None:
        @script
        def s(k: Key) -> list[bytes | int]:
            return [1, redis.get(k), 3]

        # Three values, the middle one null: `false` does not end the array.
        assert s(client, k="absent") == [1, None, 3]

    def test_an_unassigned_name_does_truncate(self, client: Any) -> None:
        """The real trap, and the one the compiler warns about."""
        with pytest.warns(NilTruncationWarning, match="'head' is not assigned"):

            @script
            def s(k: Key, n: int) -> list[int]:
                if n > 0:
                    head = 2
                return [1, head, 3]

        assert s(client, k="x", n=1) == [1, 2, 3]
        assert s(client, k="x", n=0) == [1]  # nil at index 2, and the rest is gone

    def test_a_literal_none_in_a_returned_table_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="truncates the reply"):

            @script
            def s(k: Key) -> list[int]:
                return [1, None, 3]

    def test_a_constant_folding_to_nil_is_refused_the_same_way(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="truncates the reply"):

            @script
            def s(k: Key) -> list[int]:
                return [1, NOTHING, 3]

    def test_a_bare_none_return_is_still_fine(self, client: Any) -> None:
        """Only a nil *inside* a table is a problem; a nil reply is a nil reply."""

        @script
        def s(k: Key) -> None:
            return None

        assert s(client, k="x") is None


class TestDefiniteAssignment:
    """What the truncation warning does and does not fire on."""

    def test_a_branch_that_returns_establishes_the_name_below_it(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")

            @script
            def s(k: Key, n: int) -> list[int]:
                if n <= 0:
                    return [0]
                head = 2
                return [1, head]

    def test_both_arms_assigning_is_enough(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")

            @script
            def s(k: Key, n: int) -> list[int]:
                if n > 0:
                    head = 1
                else:
                    head = 2
                return [1, head]

    def test_a_loop_body_does_not_count_because_it_may_not_run(self) -> None:
        with pytest.warns(NilTruncationWarning, match="'head'"):

            @script
            def s(k: Key, n: int) -> list[int]:
                for i in range(n):
                    head = i
                return [1, head]

    def test_a_name_returned_outside_a_table_is_not_flagged(self) -> None:
        """A nil reply is visible to the caller; a truncated list is not."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")

            @script
            def s(k: Key, n: int) -> int:
                if n > 0:
                    head = 1
                return head


class TestNumbersHandedToRedis:
    """A Lua number is a double, which has no exact form past 2^53."""

    def test_a_large_integer_arrives_exactly(self, client: Any) -> None:
        @script
        def store(k: Key, n: int) -> bytes:
            redis.set(k, n)
            return redis.get(k)

        assert store(client, k="n", n=2**53 + 1) == b"9007199254740993"

    def test_a_float_is_not_rewritten_with_seventeen_digits(self, client: Any) -> None:
        @script
        def store(k: Key, x: float) -> bytes:
            redis.set(k, x)
            return redis.get(k)

        assert store(client, k="f", x=0.1) == b"0.1"

    def test_arithmetic_still_sees_a_number(self, client: Any) -> None:
        @script
        def twice(k: Key, n: int) -> int:
            redis.set(k, n)
            return n * 2 + 1

        assert twice(client, k="n", n=20) == 41
        assert client.get("n") == b"20"
