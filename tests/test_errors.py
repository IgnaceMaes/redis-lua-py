"""Everything the compiler refuses, and how clearly it says so."""

from __future__ import annotations

import pytest

from redis_lua_py import Key, UnsupportedSyntax, redis, script


def test_with_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="'with' is not supported"):

        @script
        def s(k: Key) -> int:
            with open(k):
                return 1


def test_starred_unpacking_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="only names and subscripts"):

        @script
        def s(k: Key) -> int:
            head, *_rest = redis.lrange(k, 0, -1)
            return head


def test_closure_variable_is_rejected() -> None:
    outside = 5
    with pytest.raises(UnsupportedSyntax, match="undefined name 'outside'"):

        @script
        def s(k: Key) -> int:
            return outside


def test_negative_index_on_an_expression_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="needs a name"):

        @script
        def s(k: Key) -> str:
            return redis.lrange(k, 0, -1)[-1]


def test_bit_shift_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="LShift operator"):

        @script
        def s(a: int, b: int) -> int:
            return a << b


def test_except_with_a_type_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="cannot be told apart"):

        @script
        def s(k: Key) -> int:
            try:
                return redis.incr(k)
            except ValueError:
                return 0


def test_import_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="cannot import"):

        @script
        def s(k: Key) -> int:
            import os  # noqa: F401  (the point of the test; do not let ruff remove it)

            return 1


def test_helper_with_a_default_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="plain positional parameters"):

        @script
        def s(k: Key) -> int:
            def helper(n=1):
                return n

            return helper()


def test_calling_a_python_function_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="sorted\\(\\) is not available"):

        @script
        def s(k: Key) -> list[str]:
            return sorted(redis.lrange(k, 0, -1))


def test_unpacking_the_wrong_number_of_values_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="cannot unpack 3 values into 2"):

        @script
        def s(k: Key) -> int:
            a, b = 1, 2, 3
            return a


def test_default_value_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="default values"):

        @script
        def s(k: Key, n: int = 5) -> int:
            return n


def test_varargs_are_rejected_with_a_pointer_to_list_parameters() -> None:
    with pytest.raises(UnsupportedSyntax, match=r"\*args") as info:

        @script
        def s(k: Key, *rest: str) -> int:
            return 1

    assert info.value.hint is not None
    assert "list[Key]" in info.value.hint


def test_raise_from_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match=r"raise \.\.\. from"):

        @script
        def s(n: int) -> int:
            raise RuntimeError("wrapped") from None


def test_fstring_alignment_is_rejected_with_a_hint() -> None:
    with pytest.raises(UnsupportedSyntax, match=r"no string\.format counterpart") as info:

        @script
        def s(a: str) -> str:
            return f"{a:^20}"

    assert info.value.hint is not None
    assert "alignment" in info.value.hint


def test_error_names_the_line_and_marks_the_column() -> None:
    with pytest.raises(UnsupportedSyntax) as info:

        @script
        def s(a: int, b: int) -> int:
            return a << b

    rendered = str(info.value)
    assert "test_errors.py" in rendered
    assert "a << b" in rendered
    assert "^" in rendered
    assert info.value.lineno > 0


def test_error_carries_a_hint() -> None:
    with pytest.raises(UnsupportedSyntax) as info:

        @script
        def s(k: Key, values: list[str]) -> int:
            return redis.rpush(k, *values, "end")

    assert info.value.hint is not None
    assert "unpack()" in info.value.hint


def test_an_except_name_that_is_a_lua_keyword_is_rejected() -> None:
    with pytest.raises(UnsupportedSyntax, match="'end' is a reserved word in Lua"):

        @script
        def s(k: Key) -> int:
            try:
                redis.get(k)
            except Exception as end:
                return len(end)
            return 0
