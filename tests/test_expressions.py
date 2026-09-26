"""Chained comparisons, lambdas, and + on values of unknown type.

Every test runs the script.
"""

from __future__ import annotations

from typing import Any

import pytest

from redis_lua_py import Key, UnsupportedSyntax, redis, script


class TestChainedComparisons:
    def test_names_and_literals_are_repeated(self, client: Any) -> None:
        @script
        def within(n: int, low: int, high: int) -> list[int]:
            return [1 if low <= n < high else 0, 1 if 0 < n == low else 0]

        assert "low <= n and n < high" in within.lua
        assert within(client, n=3, low=3, high=5) == [1, 1]
        assert within(client, n=5, low=3, high=5) == [0, 0]

    def test_a_middle_operand_runs_once(self, client: Any) -> None:
        @script
        def bump(k: Key, limit: int) -> int:
            if 0 < redis.incr(k) <= limit:
                return 1
            return 0

        assert [bump(client, k="n", limit=2) for _ in range(3)] == [1, 1, 0]
        assert client.get("n") == b"3"

    def test_a_later_operand_only_runs_when_the_comparison_before_it_held(
        self, client: Any
    ) -> None:
        @script
        def probe(k: Key, n: int) -> int:
            if n < 0 < redis.incr(k) < 10:
                return 1
            return 0

        assert probe(client, k="n", n=5) == 0
        assert client.get("n") is None
        assert probe(client, k="n", n=-1) == 1
        assert client.get("n") == b"1"

    def test_membership_chains_too(self, client: Any) -> None:
        @script
        def check(word: str) -> int:
            return 1 if "a" in word in "banana" else 0

        assert check(client, word="nan") == 1
        assert check(client, word="nab") == 0


class TestLambdas:
    def test_a_named_lambda_is_a_local_function(self, client: Any) -> None:
        @script
        def quadrupled(n: int) -> int:
            double = lambda x: x * 2  # noqa: E731
            return double(double(n))

        assert "local function double(x)" in quadrupled.lua
        assert quadrupled(client, n=4) == 16

    def test_a_named_lambda_can_recurse(self, client: Any) -> None:
        @script
        def factorial(n: int) -> int:
            fact = lambda k: 1 if k <= 1 else k * fact(k - 1)  # noqa: E731
            return fact(n)

        assert factorial(client, n=5) == 120

    def test_a_lambda_passed_to_a_helper(self, client: Any) -> None:
        @script
        def transformed(k: Key, n: int) -> list[int]:
            def apply_twice(f, x):
                return f(f(x))

            step = redis.incr(k)
            return [apply_twice(lambda v: v + step, n), apply_twice(lambda v: v * v, n)]

        assert transformed(client, k="c", n=3) == [5, 81]

    def test_a_lambda_sees_a_name_assigned_after_it(self, client: Any) -> None:
        @script
        def late(n: int) -> int:
            scaled = lambda x: x * factor  # noqa: E731
            factor = n
            return scaled(10)

        assert late(client, n=3) == 30

    def test_a_lambda_is_a_value_like_any_other(self, client: Any) -> None:
        @script
        def pick(flag: int) -> int:
            op = (lambda a, b: a - b) if flag else (lambda a, b: a * b)
            return op(7, 3)

        assert pick(client, flag=1) == 4
        assert pick(client, flag=0) == 21

    def test_a_default_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="plain positional parameters"):

            @script
            def s(n: int) -> int:
                add = lambda x, y=1: x + y  # noqa: E731
                return add(n)

    def test_reading_a_loop_variable_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="loop variable 'i'"):

            @script
            def s(n: int) -> int:
                total = 0
                for i in range(n):
                    add = lambda x: x + i  # noqa: E731, B023
                    total = add(total)
                return total

    def test_a_helper_reading_a_loop_variable_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="loop variable 'item'"):

            @script
            def s(k: Key) -> int:
                def seen():
                    return redis.sismember(k, item)

                count = 0
                for item in redis.lrange(k, 0, -1):
                    count += seen()
                return count


class TestAddition:
    def test_two_strings_of_unknown_type_are_joined(self, client: Any) -> None:
        @script
        def joined(a: Key, b: Key) -> bytes:
            return redis.get(a) + redis.get(b)

        client.set("a", "1")
        client.set("b", "2")
        assert "__add(" in joined.lua
        assert joined(client, a="a", b="b") == b"12"

    def test_two_lists_are_joined(self, client: Any) -> None:
        @script
        def both(a: Key, b: Key) -> list[bytes]:
            return redis.lrange(a, 0, -1) + redis.lrange(b, 0, -1)

        client.rpush("a", "x")
        client.rpush("b", "y", "z")
        assert both(client, a="a", b="b") == [b"x", b"y", b"z"]

    def test_two_numbers_of_unknown_type_are_added(self, client: Any) -> None:
        @script
        def total(a: Key, b: Key) -> int:
            return redis.incr(a) + redis.incr(b)

        assert total(client, a="a", b="b") == 2

    def test_a_side_known_to_be_a_number_keeps_it_arithmetic(self, client: Any) -> None:
        @script
        def bump(k: Key) -> int:
            total = 0
            total += int(redis.get(k))
            return total + redis.incr(k)

        client.set("n", "5")
        assert "__add" not in bump.lua
        assert bump(client, k="n") == 11

    def test_the_least_or_greatest_of_numbers_is_a_number(self, client: Any) -> None:
        @script
        def refill(key: Key, capacity: float, rate: float, now: float) -> int:
            tokens = float(redis.get(key))
            delta = max(0, now)
            tokens = min(capacity, tokens + delta * rate)
            return tokens

        client.set("bucket", "2")
        assert "__add" not in refill.lua
        assert "tokens + delta * rate" in refill.lua
        assert refill(client, key="bucket", capacity=10, rate=2, now=3) == 8
        assert refill(client, key="bucket", capacity=5, rate=2, now=3) == 5
