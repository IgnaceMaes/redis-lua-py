"""What the compiler emits for each supported construct."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from redis_lua_py import CompiledScript, CompileError, Key, call, cjson, redis, script

Body = Callable[[CompiledScript[object]], str]


def test_keys_and_args_split_by_annotation() -> None:
    @script
    def s(first: Key, second: Key, count: int, label: str) -> int:
        return count

    assert s.keys == ("first", "second")
    assert s.args == ("count", "label")
    assert s.params == ("first", "second", "count", "label")


def test_numeric_annotations_get_tonumber(body: Body) -> None:
    @script
    def s(n: int, f: float, text: str) -> int:
        return n

    emitted = body(s)
    assert "local n = tonumber(ARGV[1])" in emitted
    assert "local f = tonumber(ARGV[2])" in emitted
    # A str parameter is already a string in Lua; converting it would be noise.
    assert "local text = ARGV[3]" in emitted


def test_full_script_emission(body: Body) -> None:
    @script
    def rate_limit(key: Key, limit: int, ttl: int) -> int:
        current = redis.incr(key)
        if current == 1:
            redis.expire(key, ttl)
        if current > limit:
            return -1
        return limit - current

    assert body(rate_limit) == (
        "local key = KEYS[1]\n"
        "local limit = tonumber(ARGV[1])\n"
        "local ttl = tonumber(ARGV[2])\n"
        "local current = redis.call('INCR', key)\n"
        "if current == 1 then\n"
        "  redis.call('EXPIRE', key, ttl)\n"
        "end\n"
        "if current > limit then\n"
        "  return -1\n"
        "end\n"
        "return limit - current"
    )


def test_command_name_is_uppercased(body: Body) -> None:
    @script
    def s(k: Key) -> int:
        return redis.zrangebyscore(k, 0, 10)

    assert "redis.call('ZRANGEBYSCORE', k, 0, 10)" in body(s)


def test_underscores_become_subcommand_tokens(body: Body) -> None:
    @script
    def s(k: Key) -> str:
        return redis.script_load("return 1")

    assert "redis.call('SCRIPT', 'LOAD', 'return 1')" in body(s)


def test_redis_helpers_keep_their_own_name(body: Body) -> None:
    @script
    def s(k: Key) -> str:
        if redis.pcall("GET", k) is None:
            return redis.error_reply("missing")
        return redis.status_reply("ok")

    emitted = body(s)
    assert "redis.pcall('GET', k)" in emitted
    assert "redis.error_reply('missing')" in emitted
    assert "redis.status_reply('ok')" in emitted


def test_call_alias_is_the_same_namespace(body: Body) -> None:
    @script
    def s(k: Key) -> int:
        return call.incr(k)

    assert "redis.call('INCR', k)" in body(s)


def test_cjson(body: Body) -> None:
    @script
    def s(k: Key) -> str:
        return cjson.encode(cjson.decode(redis.get(k)))

    assert "cjson.encode(cjson.decode(redis.call('GET', k)))" in body(s)


def test_elif_becomes_elseif(body: Body) -> None:
    @script
    def s(n: int) -> int:
        if n == 1:
            return 10
        elif n == 2:
            return 20
        else:
            return 30

    emitted = body(s)
    assert "elseif n == 2 then" in emitted
    # An elif chain must not nest, or the `end`s would pile up.
    assert emitted.count("end") == 1


def test_range_loops(body: Body) -> None:
    @script
    def s(n: int) -> int:
        total = 0
        for i in range(n):
            total += i
        for j in range(2, 5):
            total += j
        for k in range(10, 0, -2):
            total += k
        return total

    emitted = body(s)
    # Python's range excludes its stop value; Lua's numeric for includes it.
    assert "for i = 0, n - 1 do" in emitted
    assert "for j = 2, 4 do" in emitted
    assert "for k = 10, 1, -2 do" in emitted


def test_iterating_a_name_needs_no_temporary(body: Body) -> None:
    @script
    def s(k: Key) -> int:
        items = redis.lrange(k, 0, 100)
        total = 0
        for item in items:
            total += 1
        return total

    emitted = body(s)
    assert "for __i1 = 1, #items do" in emitted
    assert "local item = items[__i1]" in emitted
    assert "__seq" not in emitted


def test_iterating_a_call_binds_it_once(body: Body) -> None:
    @script
    def s(k: Key) -> int:
        total = 0
        for item in redis.lrange(k, 0, 100):
            total += 1
        return total

    emitted = body(s)
    # Re-evaluating the iterable per step would re-run the command.
    assert "local __seq1 = redis.call('LRANGE', k, 0, 100)" in emitted
    assert "for __i1 = 1, #__seq1 do" in emitted


def test_break_is_wrapped_for_lua51(body: Body) -> None:
    @script
    def s(n: int) -> int:
        total = 0
        while total < n:
            total += 1
            if total == 3:
                break
            total += 1
        return total

    # Lua 5.1 requires break to end its block; `do break end` guarantees it.
    assert "do break end" in body(s)


def test_append_and_table_literal(body: Body) -> None:
    @script
    def s(k: Key) -> list[str]:
        out = []
        out.append(redis.get(k))
        return out

    emitted = body(s)
    assert "local out = {}" in emitted
    assert "out[#out + 1] = redis.call('GET', k)" in emitted


def test_len_becomes_hash_operator(body: Body) -> None:
    @script
    def s(k: Key) -> int:
        return len(redis.lrange(k, 0, -1))

    assert "#redis.call('LRANGE', k, 0, -1)" in body(s)


def test_dict_literal(body: Body) -> None:
    @script
    def s(k: Key) -> str:
        payload = {"id": k, "n": 1}
        return cjson.encode(payload)

    assert "local payload = {id = k, n = 1}" in body(s)


def test_arithmetic_and_precedence(body: Body) -> None:
    @script
    def s(a: int, b: int) -> int:
        return (a + b) * 2 - a % 3

    assert "return (a + b) * 2 - a % 3" in body(s)


def test_floor_division(body: Body) -> None:
    @script
    def s(a: int, b: int) -> int:
        return a // b

    assert "math.floor(a / b)" in body(s)


def test_script_name_can_be_overridden() -> None:
    @script(name="custom")
    def s(k: Key) -> int:
        return 1

    assert s.name == "custom"
    assert s.lua.startswith("-- custom")


def test_docstring_is_captured_not_emitted(body: Body) -> None:
    @script
    def s(k: Key) -> int:
        """Increment and return."""
        return redis.incr(k)

    assert s.doc == "Increment and return."
    assert "Increment and return" not in body(s)


def test_repr_is_informative() -> None:
    @script
    def s(k: Key, n: int) -> int:
        return n

    assert "s(k, n)" in repr(s)


class TestGeneratedHeader:
    """The header is part of the body, so it is part of the SHA."""

    def test_the_source_path_is_repo_relative(self) -> None:
        @script
        def s(k: Key) -> int:
            return redis.incr(k)

        header = s.lua.splitlines()[1]
        assert "from tests/test_compile.py:" in header
        # An absolute path would give the same script a different SHA on a
        # laptop, in CI and in a container, so the script cache would be cold
        # once per environment rather than once per script.
        assert "/Users" not in header
        assert not header.split("from ")[1].startswith("/")

    def test_the_header_still_names_the_script_and_the_generator(self) -> None:
        @script
        def s(k: Key) -> int:
            return redis.incr(k)

        assert s.lua.startswith("-- s\n-- Generated by redis-lua-py from ")

    def test_the_header_can_be_dropped_entirely(self) -> None:
        @script(header=False)
        def s(k: Key) -> int:
            return redis.incr(k)

        assert s.lua == "local k = KEYS[1]\nreturn redis.call('INCR', k)\n"

    def test_repr_still_carries_the_absolute_path_for_debugging(self) -> None:
        @script
        def s(k: Key) -> int:
            return redis.incr(k)

        # Never hashed, so it can stay as useful as possible.
        assert "test_compile.py" in repr(s)


def test_a_script_must_be_defined_in_a_file() -> None:
    """Hence: define scripts at module level, where they compile once."""
    namespace: dict[str, object] = {"script": script, "Key": Key, "redis": redis}
    with pytest.raises(CompileError, match="not in a REPL or an exec"):
        exec("@script\ndef s(k: Key) -> int:\n    return redis.incr(k)\n", namespace)
