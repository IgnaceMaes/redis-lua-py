"""continue, and errors: try/except/else/finally, raise and assert.

Lua 5.1 has neither continue nor exceptions, so each of these is rebuilt from
what it does have -- a block left with break, and pcall. Every test runs the
script, because the claim is about behaviour, not spelling.
"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ResponseError

from redis_lua_py import Key, UnsupportedSyntax, cjson, redis, script


class TestContinue:
    def test_continue_skips_to_the_next_step(self, client: Any) -> None:
        @script
        def odd_sum(n: int) -> int:
            total = 0
            for i in range(n):
                if i % 2 == 0:
                    continue
                total += i
            return total

        assert "until true" in odd_sum.lua
        assert odd_sum(client, n=10) == 25

    def test_continue_and_break_in_the_same_loop(self, client: Any) -> None:
        @script
        def s(n: int) -> int:
            total = 0
            for i in range(n):
                if i == 2:
                    continue
                if i == 5:
                    break
                total += i
            return total

        assert s(client, n=10) == 8

    def test_continue_in_a_while_loop(self, client: Any) -> None:
        @script
        def s(n: int) -> int:
            i = 0
            total = 0
            while i < n:
                i += 1
                if i % 3 == 0:
                    continue
                total += i
            return total

        assert s(client, n=6) == 12

    def test_continue_only_affects_the_innermost_loop(self, client: Any) -> None:
        @script
        def off_diagonal(n: int) -> int:
            count = 0
            for i in range(n):
                for j in range(n):
                    if j == i:
                        continue
                    count += 1
            return count

        assert off_diagonal(client, n=3) == 6

    def test_continue_over_dict_items(self, client: Any) -> None:
        @script
        def positive_total(doc: str) -> int:
            total = 0
            for key, value in cjson.decode(doc).items():
                if value < 0:
                    continue
                total += value
            return total

        assert positive_total(client, doc='{"a": 3, "b": -10, "c": 4}') == 7

    def test_a_loop_without_continue_is_unchanged(self) -> None:
        @script
        def s(n: int) -> int:
            total = 0
            for i in range(n):
                total += i
            return total

        assert "repeat" not in s.lua


class TestTryExcept:
    def test_except_catches_a_failing_command(self, client: Any) -> None:
        @script
        def safe_incr(k: Key) -> int:
            try:
                return redis.incr(k)
            except Exception:
                return -1

        client.set("k", "abc")
        assert safe_incr(client, k="k") == -1
        client.set("k", "1")
        assert safe_incr(client, k="k") == 2

    def test_except_as_binds_the_message(self, client: Any) -> None:
        @script
        def attempt(k: Key) -> bytes:
            try:
                redis.incr(k)
            except Exception as e:
                return f"failed: {e}"
            return "ok"

        client.set("k", "abc")
        assert b"not an integer" in attempt(client, k="k")
        client.set("k", "1")
        assert attempt(client, k="k") == b"ok"

    def test_else_runs_only_without_an_error(self, client: Any) -> None:
        @script
        def outcome(k: Key) -> bytes:
            result = "unset"
            try:
                redis.incr(k)
            except Exception:
                result = "failed"
            else:
                result = "incremented"
            return result

        client.set("k", "abc")
        assert outcome(client, k="k") == b"failed"
        client.set("k", "1")
        assert outcome(client, k="k") == b"incremented"

    def test_a_name_assigned_in_try_is_readable_after_it(self, client: Any) -> None:
        @script
        def value_of(k: Key) -> int:
            try:
                value = redis.incr(k)
            except Exception:
                value = -1
            return value

        client.set("k", "abc")
        assert value_of(client, k="k") == -1
        client.set("k", "4")
        assert value_of(client, k="k") == 5

    def test_return_inside_try_inside_a_loop(self, client: Any) -> None:
        @script
        def first_counter(keys: list[Key]) -> int:
            for k in keys:
                try:
                    return redis.incr(k)
                except Exception:
                    pass
            return -1

        client.set("a", "abc")
        client.set("b", "5")
        assert first_counter(client, keys=["a", "b"]) == 6
        assert first_counter(client, keys=["a"]) == -1

    def test_nested_try(self, client: Any) -> None:
        @script
        def s(a: Key, b: Key) -> int:
            try:
                try:
                    return redis.incr(a)
                except Exception:
                    return redis.incr(b)
            except Exception:
                return -1

        client.set("a", "x")
        client.set("b", "1")
        assert s(client, a="a", b="b") == 2
        client.set("b", "y")
        assert s(client, a="a", b="b") == -1


class TestFinally:
    def test_finally_runs_and_the_error_still_reaches_the_caller(self, client: Any) -> None:
        @script
        def s(k: Key, marker: Key) -> int:
            try:
                return redis.incr(k)
            finally:
                redis.set(marker, "ran")

        client.set("k", "abc")
        with pytest.raises(ResponseError, match="not an integer"):
            s(client, k="k", marker="m")
        assert client.get("m") == b"ran"

        client.set("k", "1")
        client.delete("m")
        assert s(client, k="k", marker="m") == 2
        assert client.get("m") == b"ran"

    def test_finally_after_a_handled_error(self, client: Any) -> None:
        @script
        def s(k: Key, marker: Key) -> int:
            try:
                return redis.incr(k)
            except Exception:
                return -1
            finally:
                redis.incr(marker)

        client.set("k", "abc")
        assert s(client, k="k", marker="m") == -1
        assert client.get("m") == b"1"


class TestRaise:
    def test_raise_reaches_the_caller_with_its_message(self, client: Any) -> None:
        @script
        def guard(n: int) -> int:
            if n > 3:
                raise ValueError(f"limit exceeded: {n}")
            return n

        assert guard(client, n=1) == 1
        with pytest.raises(ResponseError, match="limit exceeded: 5"):
            guard(client, n=5)

    def test_a_raise_is_caught_by_except(self, client: Any) -> None:
        @script
        def s(n: int) -> bytes:
            try:
                if n > 3:
                    raise RuntimeError("too big")
                return "fine"
            except Exception as e:
                return f"caught {e}"

        assert s(client, n=1) == b"fine"
        assert s(client, n=9) == b"caught too big"

    def test_a_bare_raise_rethrows(self, client: Any) -> None:
        @script
        def s(k: Key, log: Key) -> int:
            try:
                return redis.incr(k)
            except Exception:
                redis.set(log, "failed")
                raise

        client.set("k", "abc")
        with pytest.raises(ResponseError, match="not an integer"):
            s(client, k="k", log="l")
        assert client.get("l") == b"failed"

    def test_assert(self, client: Any) -> None:
        @script
        def positive(n: int) -> int:
            assert n > 0, "n must be positive"
            return n

        assert positive(client, n=2) == 2
        with pytest.raises(ResponseError, match="n must be positive"):
            positive(client, n=-1)


class TestRefusals:
    def test_break_out_of_a_try_block(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="cannot leave a try block"):

            @script
            def s(n: int) -> int:
                for i in range(n):
                    try:
                        break
                    except Exception:
                        pass
                return n

    def test_except_with_a_specific_type(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="cannot be told apart"):

            @script
            def s(k: Key) -> int:
                try:
                    return redis.incr(k)
                except ValueError:
                    return 0

    def test_two_except_clauses(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="only one except clause"):

            @script
            def s(k: Key) -> int:
                try:
                    return redis.incr(k)
                except Exception:
                    return 0
                except BaseException:
                    return 1

    def test_raise_from(self) -> None:
        with pytest.raises(UnsupportedSyntax, match=r"raise \.\.\. from"):

            @script
            def s(n: int) -> int:
                try:
                    return n
                except Exception as e:
                    raise RuntimeError("wrapped") from e

    def test_a_bare_raise_outside_except(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="bare raise"):

            @script
            def s(n: int) -> int:
                raise
