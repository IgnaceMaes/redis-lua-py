"""How the compiler decides what a receiver like `redis.` actually refers to.

Resolution is by value rather than by spelling, so the namespace survives any
alias, and a name that turns out to be redis-py is refused rather than quietly
compiled against the wrong object.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import redis as redis_py

import module_with_client
import module_without_import
from redis_lua_py import CompiledScript, Key, UnsupportedSyntax, call, cjson, script
from redis_lua_py import redis as r
from redis_lua_py._compile import compile_function
from redis_lua_py._compile.base import is_redis_py

Body = Callable[[CompiledScript], str]


class TestAliases:
    def test_namespace_works_under_an_alias(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            return r.incr(k)

        assert "redis.call('INCR', k)" in body(s)

    def test_call_alias_still_works(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            return call.incr(k)

        assert "redis.call('INCR', k)" in body(s)

    def test_cjson_works_under_its_own_name(self, body: Body) -> None:
        @script
        def s(k: Key) -> str:
            return cjson.encode(r.get(k))

        assert "cjson.encode(redis.call('GET', k))" in body(s)

    def test_an_unbound_name_falls_back_to_the_conventional_spelling(self) -> None:
        compiled = compile_function(module_without_import.uses_a_bare_name)
        assert "redis.call('INCR', k)" in compiled.lua


class TestClientCollision:
    def test_a_module_binding_redis_to_the_client_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="bound to redis-py here"):
            compile_function(module_with_client.uses_the_client_by_mistake)

    def test_the_error_points_at_the_line_and_suggests_a_fix(self) -> None:
        with pytest.raises(UnsupportedSyntax) as info:
            compile_function(module_with_client.uses_the_client_by_mistake)

        rendered = str(info.value)
        assert "module_with_client.py" in rendered
        assert "redis.incr(k)" in rendered
        assert info.value.hint is not None
        assert "from redis_lua_py import redis as r" in info.value.hint

    def test_detection_covers_the_module_and_its_clients(self) -> None:
        assert is_redis_py(redis_py)
        assert is_redis_py(redis_py.Redis())
        assert is_redis_py(redis_py.asyncio.Redis())
        # Our own namespace must never be mistaken for the client.
        assert not is_redis_py(r)
        assert not is_redis_py(call)
        assert not is_redis_py(42)


class TestOtherReceivers:
    def test_a_local_variable_receiver_is_rejected(self) -> None:
        with pytest.raises(UnsupportedSyntax, match=r"method call \.foo\(\) is not supported"):

            @script
            def s(k: Key) -> int:
                items = r.lrange(k, 0, 100)
                return items.foo()

    def test_cjson_rejects_an_unknown_function(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="cjson has no 'parse' function"):

            @script
            def s(k: Key) -> str:
                return cjson.parse(r.get(k))
