"""Turning a Python call into KEYS and ARGV, and attaching a client."""

from __future__ import annotations

from typing import Any

import pytest

from redis_lua_py import BoundScript, Key, ScriptArgumentError, redis, script


@script
def sample(first: Key, second: Key, count: int, label: str) -> int:
    return count


@script
def counter(key: Key, by: int) -> int:
    return redis.incrby(key, by)


class TestResolution:
    def test_keyword_arguments(self) -> None:
        keys, argv = sample.resolve(first="a", second="b", count=3, label="x")
        assert keys == ["a", "b"]
        assert argv == ["3", "x"]

    def test_positional_arguments(self) -> None:
        keys, argv = sample.resolve("a", "b", 3, "x")
        assert keys == ["a", "b"]
        assert argv == ["3", "x"]

    def test_mixed_arguments(self) -> None:
        keys, argv = sample.resolve("a", "b", count=3, label="x")
        assert keys == ["a", "b"]
        assert argv == ["3", "x"]

    def test_missing_argument_is_named(self) -> None:
        with pytest.raises(ScriptArgumentError, match="missing argument\\(s\\): label"):
            sample.resolve(first="a", second="b", count=3)

    def test_unknown_argument_suggests_a_parameter(self) -> None:
        with pytest.raises(ScriptArgumentError, match="did you mean 'count'"):
            sample.resolve(first="a", second="b", cout=3, label="x")

    def test_duplicate_argument(self) -> None:
        with pytest.raises(ScriptArgumentError, match="two values for 'first'"):
            sample.resolve("a", first="b", second="c", count=1, label="x")

    def test_too_many_positionals(self) -> None:
        with pytest.raises(ScriptArgumentError, match="takes 4 argument"):
            sample.resolve("a", "b", 1, "x", "extra")


class TestEncoding:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (3, "3"),
            (3.5, "3.5"),
            (True, "1"),
            (False, "0"),
            ("text", "text"),
            (b"bytes", b"bytes"),
        ],
    )
    def test_argument_encoding(self, value: object, expected: object) -> None:
        @script
        def s(v: str) -> str:
            return v

        _, argv = s.resolve(v=value)
        assert argv == [expected]

    def test_unencodable_argument_is_refused(self) -> None:
        @script
        def s(v: str) -> str:
            return v

        # None has no unambiguous Redis representation; guessing one would be
        # worse than refusing.
        with pytest.raises(ScriptArgumentError, match="NoneType"):
            s.resolve(v=None)

        with pytest.raises(ScriptArgumentError, match="no Redis representation"):
            s.resolve(v=[1, 2])


class TestClientBinding:
    def test_bind_returns_a_callable_without_the_client(self, client: Any) -> None:
        bound = counter.bind(client)
        assert isinstance(bound, BoundScript)
        assert bound(key="k", by=2) == 2
        assert bound(key="k", by=3) == 5

    def test_bound_and_unbound_forms_agree(self, client: Any) -> None:
        bound = counter.bind(client)
        assert bound(key="k", by=1) == 1
        assert counter(client, key="k", by=1) == 2

    def test_bind_accepts_positional_arguments(self, client: Any) -> None:
        assert counter.bind(client)("k", 4) == 4

    def test_binding_does_not_mutate_the_script(self, client: Any) -> None:
        counter.bind(client)
        # The script stays reusable against any other client.
        assert counter(client, key="k", by=7) == 7

    def test_argument_errors_still_surface(self, client: Any) -> None:
        with pytest.raises(ScriptArgumentError, match="missing argument"):
            counter.bind(client)(key="k")

    def test_bound_script_exposes_the_script_metadata(self, client: Any) -> None:
        bound = counter.bind(client)
        assert bound.name == "counter"
        assert bound.keys == ("key",)
        assert bound.args == ("by",)
        assert bound.params == ("key", "by")
        assert "INCRBY" in bound.lua
        assert "bound script counter(key, by)" in repr(bound)

    async def test_bind_works_with_an_async_client(self, async_client: Any) -> None:
        bound = counter.bind(async_client)
        assert await bound(key="k", by=2) == 2
        assert await bound(key="k", by=3) == 5
