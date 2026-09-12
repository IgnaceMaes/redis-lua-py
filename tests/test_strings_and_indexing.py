"""Strings and subscripts.

Membership, slicing, negative indices, string methods and formatting, and the
one thing every subscript has to get right: a position counts from 0 in Python
and from 1 in Lua, while a dict key is used as it is. Every test runs the
script.
"""

from __future__ import annotations

from typing import Any

import pytest

from redis_lua_py import Key, UnsupportedSyntax, cjson, redis, script


class TestKeysAndPositions:
    def test_a_string_key_in_a_loop_variable_is_not_shifted(self, client: Any) -> None:
        @script
        def total(doc: str) -> int:
            obj = cjson.decode(doc)
            result = 0
            for k in obj.keys():  # noqa: SIM118 - the loop variable is the point
                result += obj[k]
            return result

        assert "__key(k)" in total.lua
        assert total(client, doc='{"a": 1, "b": 2}') == 3

    def test_a_str_parameter_is_used_as_a_key(self, client: Any) -> None:
        @script
        def field(doc: str, name: str) -> int:
            return cjson.decode(doc)[name]

        assert "[name]" in field.lua
        assert field(client, doc='{"x": 7}', name="x") == 7

    def test_a_local_assigned_a_string_is_used_as_a_key(self, client: Any) -> None:
        @script
        def field(doc: str) -> int:
            wanted = "count"
            return cjson.decode(doc)[wanted]

        assert "[wanted]" in field.lua
        assert field(client, doc='{"count": 4}') == 4

    def test_a_value_only_known_at_runtime_is_decided_there(self, client: Any) -> None:
        @script
        def pick(k: Key, at: Key) -> bytes:
            items = redis.lrange(k, 0, -1)
            return items[redis.llen(at)]

        client.rpush("l", "a", "b", "c")
        client.rpush("m", "x")
        assert "__key(redis.call('LLEN', at))" in pick.lua
        assert pick(client, k="l", at="m") == b"b"

    def test_arithmetic_is_known_to_give_a_number(self, client: Any) -> None:
        @script
        def last(k: Key) -> bytes:
            items = redis.lrange(k, 0, -1)
            n = redis.llen(k)
            return items[n - 1]

        client.rpush("l", "a", "b", "c")
        assert "items[n - 1 + 1]" in last.lua
        assert last(client, k="l") == b"c"

    def test_a_counter_is_known_to_be_a_number(self) -> None:
        @script
        def s(k: Key) -> bytes:
            items = redis.lrange(k, 0, -1)
            i = 0
            i += 1
            return items[i]

        assert "items[i + 1]" in s.lua


class TestIndexing:
    def test_negative_indices(self, client: Any) -> None:
        @script
        def ends(k: Key) -> list[bytes]:
            items = redis.lrange(k, 0, -1)
            return [items[-1], items[-2]]

        client.rpush("l", "a", "b", "c")
        assert "items[#items]" in ends.lua
        assert ends(client, k="l") == [b"c", b"b"]

    def test_indexing_a_string_gives_a_character(self, client: Any) -> None:
        @script
        def letters(word: str) -> list[bytes]:
            return [word[0], word[-1], word[2]]

        assert letters(client, word="redis") == [b"r", b"s", b"d"]


class TestSlicing:
    def test_slicing_a_list(self, client: Any) -> None:
        @script
        def pieces(k: Key) -> list[list[bytes]]:
            items = redis.lrange(k, 0, -1)
            return [items[1:3], items[:2], items[-2:]]

        client.rpush("l", "a", "b", "c", "d")
        assert pieces(client, k="l") == [[b"b", b"c"], [b"a", b"b"], [b"c", b"d"]]

    def test_slicing_a_string(self, client: Any) -> None:
        @script
        def parts(word: str) -> list[bytes]:
            return [word[:3], word[3:], word[-2:], word[1:-1], word[4:2]]

        assert parts(client, word="redis") == [b"red", b"is", b"is", b"edi", b""]

    @pytest.mark.parametrize("step", [2, 3, -1, -2])
    def test_a_step_slices_as_python_does(self, client: Any, step: int) -> None:
        @script
        def stepped(word: str, lo: str, hi: str, k: int, letters: list[str]) -> list[bytes]:
            i = None if lo == "" else int(lo)
            j = None if hi == "" else int(hi)
            return [word[i:j:k], ",".join(letters[i:j:k])]

        word = "abcdefg"
        for lo in ["", "0", "2", "-3", "9", "-9"]:
            for hi in ["", "0", "5", "-2", "9", "-9"]:
                i = None if lo == "" else int(lo)
                j = None if hi == "" else int(hi)
                expected = [word[i:j:step].encode(), ",".join(word[i:j:step]).encode()]
                got = stepped(client, word=word, lo=lo, hi=hi, k=step, letters=list(word))
                assert got == expected, (lo, hi, step)

    def test_reversing_with_a_literal_step(self, client: Any) -> None:
        @script
        def backwards(word: str, k: Key) -> list[Any]:
            return [word[::-1], redis.lrange(k, 0, -1)[::-1]]

        client.rpush("l", "x", "y")
        assert backwards(client, word="abc", k="l") == [b"cba", [b"y", b"x"]]


class TestMembership:
    def test_in_a_literal_tuple(self, client: Any) -> None:
        @script
        def is_vowel(c: str) -> int:
            if c in ("a", "e", "i", "o", "u"):
                return 1
            return 0

        assert "c == 'a' or c == 'e'" in is_vowel.lua
        assert is_vowel(client, c="e") == 1
        assert is_vowel(client, c="z") == 0

    def test_substring(self, client: Any) -> None:
        @script
        def mentions(text: str) -> int:
            if "redis" in text:
                return 1
            return 0

        assert "string.find(text, 'redis', 1, true)" in mentions.lua
        assert mentions(client, text="i like redis") == 1
        assert mentions(client, text="valkey") == 0

    def test_element_of_a_reply(self, client: Any) -> None:
        @script
        def member(k: Key, value: str) -> int:
            if value in redis.lrange(k, 0, -1):
                return 1
            return 0

        client.rpush("l", "a", "b")
        assert member(client, k="l", value="b") == 1
        assert member(client, k="l", value="z") == 0

    def test_key_of_a_decoded_object_and_not_in(self, client: Any) -> None:
        @script
        def has(doc: str, key: str) -> list[int]:
            obj = cjson.decode(doc)
            return [1 if key in obj else 0, 1 if key not in obj else 0]

        assert has(client, doc='{"a": 1}', key="a") == [1, 0]
        assert has(client, doc='{"a": 1}', key="b") == [0, 1]

    def test_a_dict_is_looked_up_by_key_a_number_included(self, client: Any) -> None:
        @script
        def lookup(n: int) -> list[int]:
            names = {1: "one", 2: "two"}
            return [1 if n in names else 0, 1 if "one" in names else 0]

        assert "names[n] ~= nil" in lookup.lua
        assert lookup(client, n=1) == [1, 0]
        assert lookup(client, n=3) == [0, 0]

    def test_a_list_is_searched(self, client: Any) -> None:
        @script
        def has(wanted: str, keys: list[Key]) -> int:
            return 1 if wanted in keys else 0

        assert "__inlist(keys, wanted)" in has.lua
        assert has(client, wanted="b", keys=["a", "b"]) == 1
        assert has(client, wanted="c", keys=["a", "b"]) == 0

    def test_a_table_only_known_at_runtime_is_a_dict_if_a_key_is_not_a_position(
        self, client: Any
    ) -> None:
        @script
        def probe(x: str) -> list[int]:
            def has(table, item):
                return 1 if item in table else 0

            return [has({1: "a", "b": 2}, x), has(["a", "b"], x)]

        assert probe(client, x="b") == [1, 1]
        assert probe(client, x="a") == [0, 1]


class TestDictKeys:
    def test_a_number_key_is_not_a_position(self, client: Any) -> None:
        @script
        def table(n: int) -> list[Any]:
            counts = {}
            counts[0] = "zero"
            counts[n] = "n"
            return [counts[0], counts.get(n), len(counts)]

        assert "counts[0] = 'zero'" in table.lua
        assert table(client, n=2) == [b"zero", b"n", 2]

    def test_iterating_a_dict_walks_its_keys(self, client: Any) -> None:
        @script
        def weighted() -> int:
            weights = {1: 10, 2: 20}
            total = 0
            for k in weights:
                total += k * weights[k]
            return total

        assert weighted(client) == 50

    def test_list_methods_on_a_dict_are_refused(self) -> None:
        with pytest.raises(UnsupportedSyntax, match=r"dict\.pop\(\) is not supported"):

            @script
            def s() -> int:
                d = {"a": 1}
                return d.pop("a")


class TestStringMethods:
    def test_case_and_whitespace(self, client: Any) -> None:
        @script
        def tidy(text: str) -> list[bytes]:
            return [text.upper(), text.lower(), text.strip(), text.lstrip(), text.rstrip()]

        assert tidy(client, text="  MiXed  ") == [
            b"  MIXED  ",
            b"  mixed  ",
            b"MiXed",
            b"MiXed  ",
            b"  MiXed",
        ]

    def test_startswith_endswith_and_find(self, client: Any) -> None:
        @script
        def probe(key: str) -> list[int]:
            return [
                1 if key.startswith("user:") else 0,
                1 if key.endswith(":42") else 0,
                key.find(":"),
                key.find("?"),
            ]

        assert probe(client, key="user:42") == [1, 1, 4, -1]

    def test_split(self, client: Any) -> None:
        @script
        def fields(line: str) -> list[list[bytes]]:
            return [line.split(","), "a b  c".split()]  # noqa: SIM905 - under test

        assert fields(client, line="x,,y") == [[b"x", b"", b"y"], [b"a", b"b", b"c"]]

    def test_replace_is_plain_not_a_pattern(self, client: Any) -> None:
        @script
        def swapped(text: str) -> list[bytes]:
            return [text.replace(".", "::"), text.replace("", "-")]

        assert swapped(client, text="a.b") == [b"a::b", b"-a-.-b-"]

    def test_dict_get(self, client: Any) -> None:
        @script
        def lookup(doc: str) -> list[int]:
            obj = cjson.decode(doc)
            return [obj.get("a"), obj.get("missing", 5)]

        assert lookup(client, doc='{"a": 1}') == [1, 5]

    def test_ord_and_chr(self, client: Any) -> None:
        @script
        def shifted(c: str) -> bytes:
            return chr(ord(c) + 1)

        assert shifted(client, c="a") == b"b"


class TestFormatting:
    def test_percent_formatting(self, client: Any) -> None:
        @script
        def line(name: str, n: int) -> bytes:
            return "%s has %d items (%.1f%%)" % (name, n, n / 3)  # noqa: UP031 - under test

        assert line(client, name="cart", n=2) == b"cart has 2 items (0.7%)"

    def test_fstring_format_specs(self, client: Any) -> None:
        @script
        def row(name: str, price: float, qty: int) -> bytes:
            return f"[{name:6}|{price:8.2f}|{qty:03d}|{qty:x}]"

        assert row(client, name="tea", price=3.5, qty=12) == b"[tea   |    3.50|012|c]"

    def test_repetition_and_concatenation(self, client: Any) -> None:
        @script
        def banner(word: str, n: int) -> bytes:
            line = "=" * n
            title = "[" + word + "]"
            title += "!"
            return line + title

        assert "string.rep('=', n)" in banner.lua
        assert banner(client, word="hi", n=3) == b"===[hi]!"


class TestRefusals:
    def test_a_slice_step_of_zero(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="step cannot be zero"):

            @script
            def s(word: str) -> str:
                return word[::0]

    def test_slicing_a_dict(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="cannot be sliced"):

            @script
            def s() -> int:
                d = {"a": 1}
                return d[0:1]

    def test_a_negative_index_on_an_expression(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="needs a name"):

            @script
            def s(k: Key) -> str:
                return redis.lrange(k, 0, -1)[-1]

    def test_assigning_to_a_slice(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="cannot be assigned"):

            @script
            def s(k: Key) -> int:
                items = redis.lrange(k, 0, -1)
                items[0:2] = [1, 2]
                return 1

    def test_alignment_in_a_format_spec(self) -> None:
        with pytest.raises(UnsupportedSyntax, match=r"no string\.format counterpart"):

            @script
            def s(name: str) -> str:
                return f"{name:>10}"

    def test_a_width_without_a_type_on_an_unknown_value(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="width without a type"):

            @script
            def s(k: Key) -> str:
                return f"{redis.get(k):10}"

    def test_a_repr_conversion(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="!r"):

            @script
            def s(name: str) -> str:
                return f"{name!r}"

    def test_a_percent_r(self) -> None:
        with pytest.raises(UnsupportedSyntax, match="%r conversion"):

            @script
            def s(name: str) -> str:
                return "%r" % name  # noqa: UP031 - under test
