"""Module-level constants are folded into a script body.

A script has no closure -- the body runs on the server -- but a constant is
already a literal, so the one thing a body can borrow from its module is a
value that needs no Python to produce.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from enum import IntEnum

import pytest

from redis_lua_py import CompiledScript, Key, UnsupportedSyntax, redis, script

Body = Callable[[CompiledScript[object]], str]

SESSION_TTL_SECONDS = 30 * 60
CHUNK_RATIO = 0.5
KEY_PREFIX = "session:"
SENTINEL_BYTES = b"\x00\x01"
STRICT = True

SESSION_TTL = timedelta(minutes=30)


class Priority(IntEnum):
    LOW = 1
    HIGH = 9


class Settings:
    label = "primary"
    window = timedelta(minutes=5)


SETTINGS = Settings()


class TestFolding:
    def test_an_int_constant_is_folded(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            return redis.expire(k, SESSION_TTL_SECONDS)

        # The point of the exercise: the body names the constant, the Lua
        # carries the number, and no magic number appears in the Python.
        assert "redis.call('EXPIRE', k, 1800)" in body(s)

    def test_float_str_bytes_and_bool_constants(self, body: Body) -> None:
        @script
        def s(k: Key) -> bytes:
            redis.set(k, KEY_PREFIX)
            redis.set(k, SENTINEL_BYTES)
            redis.set(k, STRICT)
            return redis.set(k, CHUNK_RATIO)

        emitted = body(s)
        assert "redis.call('SET', k, 'session:')" in emitted
        assert "redis.call('SET', k, '\\000\\001')" in emitted
        assert "redis.call('SET', k, true)" in emitted
        assert "redis.call('SET', k, 0.5)" in emitted

    def test_an_int_enum_member_folds_to_its_value(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            return redis.zadd(k, Priority.HIGH, "job")

        assert "redis.call('ZADD', k, 9, 'job')" in body(s)

    def test_a_dotted_constant_folds(self, body: Body) -> None:
        @script
        def s(k: Key) -> bytes:
            return redis.set(k, SETTINGS.label)

        assert "redis.call('SET', k, 'primary')" in body(s)

    def test_a_local_name_still_wins_over_the_module(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            SESSION_TTL_SECONDS = 60  # noqa: N806
            return redis.expire(k, SESSION_TTL_SECONDS)

        emitted = body(s)
        assert "local SESSION_TTL_SECONDS = 60" in emitted
        assert "1800" not in emitted


class TestRefusals:
    def test_a_non_literal_constant_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="module-level timedelta"):

            @script
            def s(k: Key) -> int:
                return redis.expire(k, SESSION_TTL)

    def test_the_refusal_says_what_may_be_folded(self) -> None:
        with pytest.raises(UnsupportedSyntax) as info:

            @script
            def s(k: Key) -> int:
                return redis.expire(k, SESSION_TTL)

        assert info.value.hint is not None
        assert "int, float, str, bytes or bool" in info.value.hint

    def test_a_non_literal_attribute_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match=r"'SETTINGS\.window' is a module-level"):

            @script
            def s(k: Key) -> int:
                return redis.expire(k, SETTINGS.window)

    def test_an_attribute_of_something_unrelated_is_still_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match=r"attribute access \.upper is not supported"):

            @script
            def s(k: Key, name: str) -> bytes:
                return redis.set(k, name.upper)

    def test_a_name_bound_nowhere_is_still_an_undefined_name(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="undefined name 'outside'"):

            @script
            def s(k: Key) -> int:
                return outside  # noqa: F821

    def test_a_closure_variable_is_not_a_module_constant(self) -> None:
        """Only the module's globals are in reach; a local of the enclosing
        function is a value Python holds, and the server never sees it."""
        outside = 5

        with pytest.raises(UnsupportedSyntax, match="undefined name 'outside'"):

            @script
            def s(k: Key) -> int:
                return outside
