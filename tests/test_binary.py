"""Binary values survive a round trip through a script, exactly.

Redis has no argument types: KEYS and ARGV are byte strings, and so is every
Lua string. Nothing here decodes, so bytes that are not UTF-8 -- a compressed
or serialised payload -- pass through unchanged. This file is the promise that
they do.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from redis_lua_py import CompiledScript, Key, redis, script

Body = Callable[[CompiledScript[object]], str]

#: Not valid UTF-8 in any position, and carrying a NUL and a quote for good
#: measure: decoding this anywhere along the way would be visible.
BLOB = b"\xff\xfe\x00\x01\x80'\\\x7f\xc3\x28" * 200

MARKER = b"\x00\xff"


@script
def append_chunk(buffer: Key, counter: Key, chunk: bytes, ttl: int) -> list[int]:
    """Append a binary chunk and report the new length."""
    size = redis.append(buffer, chunk)
    redis.expire(buffer, ttl)
    return [size, redis.incr(counter)]


@script
def read_range(buffer: Key, start: int, stop: int) -> bytes:
    return redis.getrange(buffer, start, stop)


class TestArgv:
    def test_a_bytes_parameter_is_passed_through_untouched(self, body: Body) -> None:
        """No tonumber, no tostring: a bytes annotation is a passthrough."""
        emitted = body(append_chunk)
        assert "local chunk = ARGV[1]" in emitted
        # Only handed back to Redis, so the int is left as text too.
        assert "local ttl = ARGV[2]" in emitted

    def test_non_utf8_argv_arrives_intact(self, client: Any) -> None:
        size, count = append_chunk(client, buffer="buf", counter="chunks", chunk=BLOB, ttl=60)
        assert size == len(BLOB)
        assert count == 1
        assert client.get("buf") == BLOB

    def test_appending_twice_concatenates_exactly(self, client: Any) -> None:
        append_chunk(client, buffer="buf", counter="chunks", chunk=BLOB, ttl=60)
        append_chunk(client, buffer="buf", counter="chunks", chunk=BLOB, ttl=60)
        assert client.get("buf") == BLOB + BLOB

    def test_a_returned_byte_range_is_exact(self, client: Any) -> None:
        append_chunk(client, buffer="buf", counter="chunks", chunk=BLOB, ttl=60)
        assert read_range(client, buffer="buf", start=0, stop=9) == BLOB[:10]
        assert read_range(client, buffer="buf", start=3, stop=3) == BLOB[3:4]

    def test_a_memoryview_is_accepted(self, client: Any) -> None:
        append_chunk(client, buffer="buf", counter="chunks", chunk=memoryview(BLOB), ttl=60)
        assert client.get("buf") == BLOB


class TestLiterals:
    def test_a_bytes_literal_in_a_body_keeps_its_bytes(self, body: Body) -> None:
        @script
        def s(k: Key) -> bytes:
            redis.set(k, MARKER)
            return redis.get(k)

        # Escaped numerically rather than as text: the generated source travels
        # to Redis encoded, and these bytes are not UTF-8.
        assert "redis.call('SET', k, '\\000\\255')" in body(s)

    def test_a_bytes_literal_round_trips_through_redis(self, client: Any) -> None:
        @script
        def s(k: Key) -> bytes:
            redis.set(k, MARKER)
            return redis.get(k)

        assert s(client, k="marker") == MARKER
        assert client.get("marker") == MARKER

    def test_a_nul_escape_cannot_run_into_the_next_byte(self, client: Any) -> None:
        """Lua reads up to three digits after a backslash, so NUL needs all three."""

        @script
        def s(k: Key) -> bytes:
            redis.set(k, b"\x001")
            return redis.get(k)

        assert s(client, k="nul") == b"\x001"
