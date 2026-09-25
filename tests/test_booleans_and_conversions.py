"""Booleans, int() and str/bytes conversions, where Lua's meaning differs from Python's.

Each test runs the script as well as inspecting it: a translation that reads
well but answers differently is the failure being guarded against.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from redis_lua_py import CompiledScript, Key, UnsupportedSyntax, cjson, redis, script

Body = Callable[[CompiledScript[object]], str]


class TestBoolArguments:
    """A bool travels as "1" or "0", and "0" is a string Lua counts as true."""

    def test_false_is_false_in_a_condition(self, client: Any) -> None:
        @script
        def s(flag: bool) -> str:
            if flag:
                return "yes"
            return "no"

        assert s(client, flag=True) == b"yes"
        assert s(client, flag=False) == b"no"

    def test_arrives_as_a_lua_boolean(self, body: Body) -> None:
        @script
        def s(flag: bool) -> int:
            if not flag:
                return 0
            return 1

        emitted = body(s)
        assert "local flag = ARGV[1] == '1'" in emitted
        assert "__truthy" not in emitted

    def test_int_of_a_bool(self, client: Any, body: Body) -> None:
        @script
        def s(flag: bool) -> int:
            return int(flag) + 1

        assert "__int" not in body(s)
        assert s(client, flag=True) == 2
        assert s(client, flag=False) == 1


class TestKnownBooleans:
    """A value known to be a boolean needs no truthiness helper."""

    def test_a_comparison_kept_in_a_name(self, client: Any, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            empty = redis.llen(k) == 0
            if empty:
                return 0
            return 1

        assert "__truthy" not in body(s)
        assert s(client, k="list") == 0
        client.rpush("list", "x")
        assert s(client, k="list") == 1

    def test_startswith_and_endswith(self, client: Any, body: Body) -> None:
        @script
        def s(k: Key, prefix: str, suffix: str) -> int:
            value = redis.get(k)
            if value is not None and value.startswith(prefix) and not value.endswith(suffix):
                return 1
            return 0

        assert "__truthy" not in body(s)
        client.set("k", "pointer:abc")
        assert s(client, k="k", prefix="pointer:", suffix="xyz") == 1
        assert s(client, k="k", prefix="inline:", suffix="xyz") == 0
        assert s(client, k="k", prefix="pointer:", suffix="abc") == 0
        assert s(client, k="missing", prefix="pointer:", suffix="xyz") == 0

    def test_and_or_between_booleans_use_luas_operators(self, client: Any, body: Body) -> None:
        @script
        def s(a: int, b: int) -> list[int]:
            both = a > 0 and b > 0
            either = a > 0 or b > 0
            return [int(both), int(either)]

        emitted = body(s)
        for helper in ("__truthy", "__and", "__or", "__int"):
            assert helper not in emitted
        assert s(client, a=1, b=0) == [0, 1]
        assert s(client, a=1, b=1) == [1, 1]
        assert s(client, a=0, b=0) == [0, 0]

    def test_a_bool_constant(self, client: Any, body: Body) -> None:
        @script
        def s(n: int) -> int:
            found = False
            if n > 3:
                found = True
            if found:
                return 1
            return 0

        assert "__truthy" not in body(s)
        assert s(client, n=5) == 1
        assert s(client, n=1) == 0


class TestInt:
    """int() truncates toward zero, where tonumber keeps the fraction."""

    @pytest.mark.parametrize(
        ("value", "expected"), [("7", 7), ("3.7", 3), ("-3.7", -3), ("-0.5", 0), ("12", 12)]
    )
    def test_truncates_toward_zero(self, client: Any, value: str, expected: int) -> None:
        @script
        def s(v: str) -> list[int]:
            # Scaled, so Redis' own truncation of a numeric reply cannot hide ours.
            return [int(v) * 10]

        assert s(client, v=value) == [expected * 10]

    def test_a_missing_value_raises(self, client: Any) -> None:
        @script
        def s(k: Key) -> int:
            return int(redis.get(k))

        with pytest.raises(Exception, match="invalid literal for int"):
            s(client, k="missing")

    def test_a_base_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="int\\(\\) takes exactly one argument"):

            @script
            def s(v: str) -> int:
                return int(v, 16)  # type: ignore[call-overload]


class TestEncodeDecode:
    """Lua strings are bytes, so encode() and decode() have nothing to convert."""

    def test_pass_through(self, client: Any, body: Body) -> None:
        @script
        def s(k: Key, text: str) -> bytes:
            redis.set(k, text.encode())
            return str(redis.get(k).decode("utf-8")).encode("utf-8")

        emitted = body(s)
        assert "encode" not in emitted
        assert "decode" not in emitted
        assert s(client, k="k", text="héllo") == "héllo".encode()

    def test_another_encoding_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="only supports UTF-8"):

            @script
            def s(text: str) -> bytes:
                return text.encode("latin-1")


class TestIsinstance:
    """isinstance() tests Lua's type(), which has one number and one table type."""

    def test_rejects_json_that_is_not_an_object(self, client: Any) -> None:
        @script
        def s(k: Key) -> int:
            data = cjson.decode(redis.get(k))
            if not isinstance(data, dict):
                return 0
            return 1

        client.set("doc", '{"a": 1}')
        assert s(client, k="doc") == 1
        client.set("doc", '"a string"')
        assert s(client, k="doc") == 0
        client.set("doc", "42")
        assert s(client, k="doc") == 0

    def test_each_builtin_type(self, client: Any) -> None:
        @script
        def s(k: Key) -> list[int]:
            data = cjson.decode(redis.get(k))
            return [
                int(isinstance(data["s"], str)),
                int(isinstance(data["n"], int)),
                int(isinstance(data["n"], float)),
                int(isinstance(data["b"], bool)),
                int(isinstance(data["l"], list)),
                int(isinstance(data["s"], bytes)),
                int(isinstance(data["b"], int)),
            ]

        client.set("doc", '{"s": "x", "n": 1.5, "b": true, "l": [1]}')
        assert s(client, k="doc") == [1, 1, 1, 1, 1, 1, 0]

    def test_several_types_as_a_tuple_or_a_union(self, client: Any, body: Body) -> None:
        @script
        def s(k: Key) -> list[int]:
            v = cjson.decode(redis.get(k))
            return [int(isinstance(v, (str, int))), int(isinstance(v, str | bool))]

        emitted = body(s)
        assert "type(v) == 'string' or type(v) == 'number'" in emitted
        client.set("doc", "7")
        assert s(client, k="doc") == [1, 0]
        client.set("doc", "true")
        assert s(client, k="doc") == [0, 1]

    def test_is_a_known_boolean(self, body: Body) -> None:
        @script
        def s(k: Key) -> int:
            if isinstance(redis.get(k), str):
                return 1
            return 0

        assert "__truthy" not in body(s)

    def test_a_class_is_refused(self) -> None:
        class Point:
            pass

        with pytest.raises(UnsupportedSyntax, match="only tests for a builtin type"):

            @script
            def s(k: Key) -> int:
                return int(isinstance(redis.get(k), Point))

    def test_several_types_of_an_expression_is_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="needs a name"):

            @script
            def s(k: Key) -> int:
                return int(isinstance(redis.get(k), (str, int)))


class TestCjsonNull:
    """cjson.null writes a JSON null, where None would drop the field."""

    def test_encodes_as_null(self, client: Any) -> None:
        @script
        def s(k: Key) -> bytes:
            data = cjson.decode(redis.get(k))
            data["error"] = cjson.null
            data["gone"] = None
            return cjson.encode(data)

        client.set("doc", '{"error": "boom", "gone": 1}')
        assert s(client, k="doc") == b'{"error":null}'

    def test_compares_to_a_decoded_null(self, client: Any) -> None:
        @script
        def s(k: Key) -> int:
            data = cjson.decode(redis.get(k))
            if data["v"] == cjson.null:
                return 1
            return 0

        client.set("doc", '{"v": null}')
        assert s(client, k="doc") == 1
        client.set("doc", '{"v": 0}')
        assert s(client, k="doc") == 0
