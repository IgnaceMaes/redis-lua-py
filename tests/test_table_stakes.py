"""The constructs real-world Redis Lua leans on most.

Measured across the Lua shipped by rq, dramatiq, docket, limits, cacheops,
throttled-py, PyrateLimiter, channels_redis and others: a variable number of
keys, splatting a list into a command, and/or as values, helper functions,
dict and enumerate loops, tuple unpacking, and the math module. Every test
runs the script rather than only inspecting it.
"""

from __future__ import annotations

import math
from math import ceil
from typing import Any

import pytest

from redis_lua_py import Key, ScriptArgumentError, UnsupportedSyntax, cjson, redis, script


class TestVariadicKeysAndArguments:
    def test_a_list_of_keys_fills_the_rest_of_keys(self, client: Any) -> None:
        @script
        def delete_all(marker: Key, keys: list[Key], stamp: str) -> int:
            redis.set(marker, stamp)
            return redis.delete(*keys)

        client.mset({"a": 1, "b": 2, "c": 3})
        assert delete_all.keys == ("marker", "keys")
        assert delete_all(client, marker="m", keys=["a", "b"], stamp="now") == 2
        assert client.get("c") == b"3"
        assert client.get("m") == b"now"

    def test_a_list_of_numbers_is_converted(self, client: Any) -> None:
        @script
        def total(values: list[int]) -> int:
            result = 0
            for v in values:
                result += v
            return result

        assert "tonumber(ARGV[" in total.lua
        assert total(client, values=[1, 2, 39]) == 42

    def test_fixed_parameters_come_first_wherever_the_list_is(self, client: Any) -> None:
        @script
        def tag(members: list[str], key: Key, prefix: str) -> list[bytes]:
            out = []
            for m in members:
                out.append(f"{prefix}{m}")
            redis.sadd(key, *out)
            return out

        assert tag(client, members=["a", "b"], key="s", prefix="x:") == [b"x:a", b"x:b"]
        assert client.smembers("s") == {b"x:a", b"x:b"}

    def test_an_empty_list(self, client: Any) -> None:
        @script
        def count(keys: list[Key]) -> int:
            return len(keys)

        assert count(client, keys=[]) == 0

    def test_a_string_is_not_a_list(self, client: Any) -> None:
        @script
        def count(keys: list[Key]) -> int:
            return len(keys)

        with pytest.raises(ScriptArgumentError, match="takes a list"):
            count(client, keys="abc")

    def test_only_one_list_of_keys(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="only one list"):

            @script
            def s(a: list[Key], b: list[Key]) -> int:
                return 0


class TestSplat:
    def test_splat_into_a_command(self, client: Any) -> None:
        @script
        def push(k: Key, items: list[str]) -> int:
            return redis.rpush(k, *items)

        assert "unpack(items)" in push.lua
        assert push(client, k="l", items=["a", "b", "c"]) == 3

    def test_splat_into_redis_call(self, client: Any) -> None:
        @script
        def zadd_pairs(k: Key, pairs: list[str]) -> int:
            return redis.call("ZADD", k, *pairs)

        assert zadd_pairs(client, k="z", pairs=["1", "a", "2", "b"]) == 2

    def test_a_splat_must_come_last(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="must be the last"):

            @script
            def s(k: Key, items: list[str]) -> int:
                return redis.rpush(k, *items, "end")


class TestBooleanValues:
    def test_or_supplies_a_default_for_a_missing_value(self, client: Any) -> None:
        @script
        def current(k: Key) -> int:
            return int(redis.get(k) or 0)

        assert current(client, k="n") == 0
        client.set("n", 7)
        assert current(client, k="n") == 7

    def test_or_follows_python_truthiness(self, client: Any) -> None:
        @script
        def pick(a: int, b: int) -> int:
            return a or b

        # Lua's own `or` would hand back the 0.
        assert pick(client, a=0, b=5) == 5
        assert pick(client, a=3, b=5) == 3

    def test_and_returns_the_falsy_operand(self, client: Any) -> None:
        @script
        def both(a: int, b: int) -> int:
            return a and b

        assert both(client, a=0, b=5) == 0
        assert both(client, a=2, b=5) == 5

    def test_the_right_side_only_runs_when_needed(self, client: Any) -> None:
        @script
        def lazy(k: Key, flag: int) -> int:
            return flag or redis.incr(k)

        assert lazy(client, k="hits", flag=1) == 1
        assert client.get("hits") is None
        assert lazy(client, k="hits", flag=0) == 1
        assert client.get("hits") == b"1"

    def test_conditional_expression_with_a_literal_branch(self, client: Any) -> None:
        @script
        def over(current: int, limit: int) -> list[int]:
            return [1 if current > limit else 0, current]

        assert "current > limit and 1 or 0" in over.lua
        assert over(client, current=5, limit=3) == [1, 5]
        assert over(client, current=2, limit=3) == [0, 2]

    def test_conditional_expression_whose_branch_can_be_false(self, client: Any) -> None:
        @script
        def maybe(k: Key, use: int) -> bytes:
            value = redis.get(k) if use else "fallback"
            if value is None:
                return "missing"
            return value

        # `use and redis.get(k) or 'fallback'` would answer fallback here.
        assert maybe(client, k="absent", use=1) == b"missing"
        assert maybe(client, k="absent", use=0) == b"fallback"


class TestHelperFunctions:
    def test_a_helper_is_a_local_function(self, client: Any) -> None:
        @script
        def weighted(previous: Key, current: Key) -> int:
            def count(key):
                return int(redis.get(key) or 0)

            return count(previous) // 2 + count(current)

        client.set("p", 10)
        client.set("c", 3)
        assert "local function count(key)" in weighted.lua
        assert weighted(client, previous="p", current="c") == 8

    def test_names_a_helper_assigns_stay_local_to_it(self, client: Any) -> None:
        @script
        def s(n: int) -> list[int]:
            total = n

            def bump(x):
                total = x + 100
                return total

            result = bump(1)
            return [total, result]

        assert s(client, n=5) == [5, 101]

    def test_a_helper_reads_a_name_assigned_after_it(self, client: Any) -> None:
        @script
        def s(n: int) -> int:
            def scaled(x):
                return x * factor

            factor = 3
            return scaled(n)

        assert s(client, n=4) == 12

    def test_recursion(self, client: Any) -> None:
        @script
        def factorial(n: int) -> int:
            def f(k):
                if k <= 1:
                    return 1
                return k * f(k - 1)

            return f(n)

        assert factorial(client, n=5) == 120

    def test_a_helper_inside_a_block_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="top level"):

            @script
            def s(n: int) -> int:
                if n:

                    def f(x):
                        return x

                return n


class TestLoops:
    def test_items_over_a_decoded_object(self, client: Any) -> None:
        @script
        def total(doc: str) -> int:
            result = 0
            for key, value in cjson.decode(doc).items():
                result += value
            return result

        assert total(client, doc='{"a": 1, "b": 2}') == 3

    def test_keys_and_values(self, client: Any) -> None:
        @script
        def shape(doc: str) -> list[int]:
            obj = cjson.decode(doc)
            n = 0
            for k in obj.keys():  # noqa: SIM118 - .keys() is the construct under test
                n += 1
            s = 0
            for v in obj.values():
                s += v
            return [n, s]

        assert shape(client, doc='{"a": 1, "b": 2, "c": 4}') == [3, 7]

    def test_enumerate_counts_from_zero(self, client: Any) -> None:
        @script
        def indexed(k: Key) -> list[bytes]:
            out = []
            for i, member in enumerate(redis.lrange(k, 0, -1)):
                out.append(f"{i}={member}")
            return out

        client.rpush("l", "a", "b")
        assert indexed(client, k="l") == [b"0=a", b"1=b"]

    def test_enumerate_with_a_start(self, client: Any) -> None:
        @script
        def indexed(k: Key) -> list[bytes]:
            out = []
            for i, member in enumerate(redis.lrange(k, 0, -1), 1):
                out.append(f"{i}={member}")
            return out

        client.rpush("l", "a", "b")
        assert indexed(client, k="l") == [b"1=a", b"2=b"]

    def test_unpacking_each_element(self, client: Any) -> None:
        @script
        def fill(k: Key) -> int:
            total = 0
            for name, n in [["a", 1], ["b", 2]]:
                redis.hset(k, name, n)
                total += n
            return total

        assert fill(client, k="h") == 3
        assert client.hgetall("h") == {b"a": b"1", b"b": b"2"}


class TestTupleUnpacking:
    def test_swap(self, client: Any) -> None:
        @script
        def swap(a: int, b: int) -> list[int]:
            a, b = b, a
            return [a, b]

        assert swap(client, a=1, b=2) == [2, 1]

    def test_unpacking_a_reply_runs_the_command_once(self, client: Any) -> None:
        @script
        def first_two(k: Key) -> list[bytes]:
            head, second = redis.lrange(k, 0, 1)
            return [second, head]

        client.rpush("l", "x", "y")
        assert first_two.lua.count("LRANGE") == 1
        assert first_two(client, k="l") == [b"y", b"x"]

    def test_unpacked_in_a_block_and_read_after_it(self, client: Any) -> None:
        @script
        def bounds(flag: int) -> list[int]:
            if flag:
                lo, hi = 1, 2
            else:
                lo, hi = 3, 4
            return [lo, hi]

        assert bounds(client, flag=1) == [1, 2]
        assert bounds(client, flag=0) == [3, 4]


class TestMath:
    def test_math_functions(self, client: Any) -> None:
        @script
        def rounding(a: float, b: float) -> list[int]:
            return [math.ceil(a / b), math.floor(a / b), ceil(a)]

        assert rounding(client, a=7, b=2) == [4, 3, 7]

    def test_math_log_with_a_base_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="with a base"):

            @script
            def s(x: float) -> int:
                return math.log(x, 2)

    def test_a_math_function_without_a_counterpart(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="no Lua counterpart"):

            @script
            def s(a: int, b: int) -> int:
                return math.gcd(a, b)


class TestListAndStringMethods:
    def test_join(self, client: Any) -> None:
        @script
        def joined(k: Key) -> bytes:
            return ",".join(redis.lrange(k, 0, -1))

        client.rpush("l", "a", "b", "c")
        assert joined(client, k="l") == b"a,b,c"

    def test_insert_and_pop(self, client: Any) -> None:
        @script
        def shuffle() -> list[int]:
            xs = [1, 2, 3]
            xs.insert(0, 0)
            last = xs.pop()
            first = xs.pop(0)
            xs.append(last + first)
            return xs

        assert shuffle(client) == [1, 2, 3]


def test_a_cluster_pipeline_sends_the_source_with_eval() -> None:
    """redis-py blocks EVALSHA on a cluster pipeline, so EVAL is queued instead."""
    from redis.cluster import ClusterPipeline

    @script
    def bump(k: Key, n: int) -> int:
        return redis.incrby(k, n)

    queued: list[tuple[object, ...]] = []
    pipe = object.__new__(ClusterPipeline)
    pipe.eval = lambda *args: queued.append(args) or pipe  # type: ignore[method-assign]

    assert bump(pipe, k="x", n=2) is pipe
    assert queued == [(bump.lua, 1, "x", "2")]
