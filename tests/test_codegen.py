"""Generating Lua ahead of time, for code that ships without this package."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import codegen_scripts
import generated_scripts
from redis_lua_py import Key, RedisLuaError, ScriptArgumentError, StaleLuaError, codegen, script
from redis_lua_py import redis as r
from redis_lua_py.__main__ import main
from redis_lua_py.codegen import _python_string

TESTS = Path(__file__).parent

#: Generated from codegen_scripts and checked in, so that mypy and ruff read
#: real output. Regenerate it from the tests directory:
#:
#:     uv run python -m redis_lua_py generate codegen_scripts --out generated_scripts.py
GENERATED = TESTS / "generated_scripts.py"


class Opaque:
    """A type no generated module could import."""


@script
def opaque(key: Key, value: Opaque) -> Opaque:
    return value


@script
def maybe(key: Key, count: int | float) -> bytes | None:
    return r.get(key)


@script
def shadowing(_encode: str) -> bytes:
    return _encode


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("generated_lua", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module_of(**scripts: Any) -> ModuleType:
    module = ModuleType("handmade")
    for name, value in scripts.items():
        setattr(module, name, value)
    return module


def test_collect_finds_every_script_once_in_definition_order():
    scripts = codegen.collect(codegen_scripts)

    assert list(scripts) == ["rate_limit", "touch_all", "awkward", "echo", "push_all"]
    assert scripts["rate_limit"] is codegen_scripts.rate_limit


def test_collect_accepts_a_dotted_name():
    assert codegen.collect("codegen_scripts") == codegen.collect(codegen_scripts)


def test_a_module_without_scripts_is_refused():
    with pytest.raises(RedisLuaError, match="no @script functions"):
        codegen.collect(ModuleType("empty"))


def test_the_checked_in_module_is_current():
    codegen.check(codegen_scripts, GENERATED)


@pytest.fixture(params=["runtime", "generated"])
def scripts(request: pytest.FixtureRequest) -> ModuleType:
    """The same scripts, called through @script or through the generated module."""
    return codegen_scripts if request.param == "runtime" else generated_scripts


class TestCallingEitherWay:
    def test_by_keyword(self, scripts, client):
        assert scripts.rate_limit(client, key="u:42", limit=2, ttl=60) == 1
        assert scripts.rate_limit(client, key="u:42", limit=2, ttl=60) == 0
        assert scripts.rate_limit(client, key="u:42", limit=2, ttl=60) == -1

    def test_by_position(self, scripts, client):
        assert scripts.rate_limit(client, "u:42", 2, 60) == 1

    async def test_with_an_async_client(self, scripts, async_client):
        assert await scripts.rate_limit(async_client, key="u:42", limit=2, ttl=60) == 1

    def test_a_list_of_keys(self, scripts, client):
        client.set("a", 1)
        client.set("b", 1)

        assert scripts.touch_all(client, keys=["a", "b", "missing"], ttl=60) == 2
        assert client.ttl("a") > 0

    def test_a_list_of_arguments_and_a_parameter_named_client(self, scripts, client):
        assert scripts.push_all(client, client="list", values=["x", "y"]) == 2
        assert client.lrange("list", 0, -1) == [b"x", b"y"]

    def test_a_keyword_only_parameter(self, scripts, client):
        assert scripts.echo(client, "a", suffix="b") == b"ab"

    def test_values_are_encoded_for_redis(self, scripts, client):
        assert scripts.echo(client, True, suffix="") == b"1"
        assert scripts.echo(client, 2.5, suffix="") == b"2.5"
        assert scripts.echo(client, b"\xff", suffix="") == b"\xff"

    def test_a_value_redis_cannot_represent_is_refused(self, scripts, client):
        with pytest.raises(TypeError if scripts is generated_scripts else ScriptArgumentError):
            scripts.echo(client, None, suffix="")

    def test_a_string_for_a_list_is_refused(self, scripts, client):
        with pytest.raises(TypeError if scripts is generated_scripts else ScriptArgumentError):
            scripts.touch_all(client, keys="abc", ttl=60)

    def test_a_cluster_pipeline_gets_the_source(self, scripts):
        sent: list[tuple[object, ...]] = []
        pipeline = type("ClusterPipeline", (), {"__module__": "redis.cluster"})()
        pipeline.eval = lambda *args: sent.append(args)  # type: ignore[attr-defined]

        scripts.rate_limit(pipeline, key="k", limit=1, ttl=2)

        assert sent == [(codegen_scripts.rate_limit.lua, 1, "k", "1", "2")]


class TestGeneratedFunctions:
    def test_python_checks_the_arguments(self, client):
        with pytest.raises(TypeError, match="ttl"):
            generated_scripts.rate_limit(client, key="k", limit=1)  # type: ignore[call-arg]
        with pytest.raises(TypeError, match="tll"):
            generated_scripts.rate_limit(client, key="k", limit=1, tll=1)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            generated_scripts.echo(client, "a", "b")  # type: ignore[misc]

    def test_register_once_per_client(self, client):
        generated_scripts.rate_limit(client, key="k", limit=1, ttl=2)
        registered = generated_scripts._RATE_LIMIT_CLIENTS[client]

        generated_scripts.rate_limit(client, key="k", limit=1, ttl=2)

        assert generated_scripts._RATE_LIMIT_CLIENTS[client] is registered

    def test_signatures_repeat_the_source(self):
        text = GENERATED.read_text()

        assert "def rate_limit(client: Any, /, key: _Key, limit: int, ttl: int) -> int: ..." in text
        assert "    client: _AsyncClient,\n    /,\n    key: _Key,\n" in text
        assert ") -> Awaitable[int]: ..." in text
        assert "def touch_all(client: Any, /, keys: Iterable[_Key], ttl: int) -> int: ..." in text
        assert "def echo(client: Any, /, value: str, *, suffix: str) -> bytes: ..." in text
        assert "(_client: Any, /, client: _Key, values: Iterable[str]) -> int: ..." in text

    def test_docstrings_come_along(self):
        # getdoc, because __doc__ only drops the indentation from Python 3.13.
        assert inspect.getdoc(generated_scripts.touch_all) == codegen_scripts.touch_all.doc
        assert inspect.getdoc(generated_scripts.echo) == codegen_scripts.echo.doc

    def test_annotations_naming_anything_else_are_widened(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(module_of(opaque=opaque, maybe=maybe), out)
        text = out.read_text()

        assert "def opaque(client: Any, /, key: _Key, value: _Arg) -> Any: ..." in text
        assert "count: int | float) -> bytes | None: ..." in text

    def test_imports_only_the_standard_library(self):
        tree = ast.parse(GENERATED.read_text())
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }

        assert imported == {"__future__", "collections", "typing", "weakref"}
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Import)]

    def test_a_parameter_the_body_needs_is_refused(self, tmp_path):
        with pytest.raises(RedisLuaError, match="parameter named '_encode'"):
            codegen.generate(module_of(shadowing=shadowing), tmp_path / "_lua.py")

    def test_a_script_named_like_the_runtime_is_refused(self, tmp_path):
        with pytest.raises(RedisLuaError, match="_run"):
            codegen.generate(module_of(_run=codegen_scripts.rate_limit), tmp_path / "_lua.py")


class TestPythonModule:
    def test_holds_each_script_exactly(self):
        assert codegen_scripts.rate_limit.lua == generated_scripts.RATE_LIMIT
        assert codegen_scripts.touch_all.lua == generated_scripts.TOUCH_ALL
        assert codegen_scripts.awkward.lua == generated_scripts.AWKWARD
        assert generated_scripts.__all__ == [
            "AWKWARD",
            "ECHO",
            "PUSH_ALL",
            "RATE_LIMIT",
            "TOUCH_ALL",
            "awkward",
            "echo",
            "push_all",
            "rate_limit",
            "touch_all",
        ]

    def test_records_what_the_caller_passes(self):
        text = GENERATED.read_text()

        assert "# rate_limit -- KEYS: key; ARGV: limit, ttl\n" in text
        assert "# touch_all -- KEYS: *keys; ARGV: ttl\n" in text
        assert "# awkward -- KEYS: none; ARGV: none\n" in text
        assert "# push_all -- KEYS: client; ARGV: *values\n" in text

    def test_records_how_to_regenerate(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)
        text = out.read_text()

        assert "python -m redis_lua_py generate codegen_scripts --out _lua.py" in text
        assert str(tmp_path) not in text

    def test_the_constants_still_run_through_plain_redis_py(self, client):
        rate_limit = client.register_script(generated_scripts.RATE_LIMIT)
        assert rate_limit(keys=["u:42"], args=[2, 60]) == 1
        assert rate_limit(keys=["u:42"], args=[2, 60]) == 0

        awkward = client.register_script(generated_scripts.AWKWARD)
        assert awkward() == codegen_scripts.AWKWARD.encode()

    def test_colliding_names_are_refused(self, tmp_path):
        module = module_of(rate_limit=codegen_scripts.rate_limit, RATE_LIMIT=shadowing)

        with pytest.raises(RedisLuaError, match="RATE_LIMIT"):
            codegen.generate(module, tmp_path / "_lua.py")

    @pytest.mark.parametrize(
        "text",
        [
            '"""',
            '""""',
            'ends in a quote"',
            '\\"""',
            "back\\slash\\",
            "carriage\rreturn",
            "nul\0 and escape\x1b and delete\x7f",
            "tab\tand newline\n",
            "café and a line separator" + chr(0x2028),
            "",
        ],
    )
    def test_any_text_reads_back_exactly(self, text):
        assert ast.literal_eval(_python_string(text)) == text


class TestLuaDirectory:
    def test_writes_one_file_per_script(self, tmp_path):
        out = tmp_path / "lua"
        codegen.generate(codegen_scripts, out)

        assert sorted(p.name for p in out.iterdir()) == [
            "awkward.lua",
            "echo.lua",
            "push_all.lua",
            "rate_limit.lua",
            "touch_all.lua",
        ]
        assert (out / "rate_limit.lua").read_text() == codegen_scripts.rate_limit.lua

    def test_removes_its_own_leftovers_but_not_handwritten_lua(self, tmp_path):
        out = tmp_path / "lua"
        codegen.generate(codegen_scripts, out)
        renamed = out / "old_name.lua"
        renamed.write_text(codegen_scripts.rate_limit.lua)
        handwritten = out / "handwritten.lua"
        handwritten.write_text("return 1\n")

        with pytest.raises(StaleLuaError, match=r"old_name\.lua"):
            codegen.check(codegen_scripts, out)
        assert codegen.generate(codegen_scripts, out) == [renamed]
        assert not renamed.exists()
        assert handwritten.exists()
        codegen.check(codegen_scripts, out)

    def test_a_single_lua_file_is_refused(self, tmp_path):
        with pytest.raises(RedisLuaError, match="directory"):
            codegen.generate(codegen_scripts, tmp_path / "scripts.lua")


class TestCheck:
    @pytest.mark.parametrize("name", ["_lua.py", "lua"])
    def test_passes_right_after_generating(self, tmp_path, name):
        codegen.generate(codegen_scripts, tmp_path / name)

        codegen.check(codegen_scripts, tmp_path / name)

    def test_regenerating_touches_nothing(self, tmp_path):
        out = tmp_path / "_lua.py"
        assert codegen.generate(codegen_scripts, out) == [out]
        before = out.stat().st_mtime_ns

        assert codegen.generate(codegen_scripts, out) == []
        assert out.stat().st_mtime_ns == before

    def test_fails_with_a_diff_and_the_command_that_fixes_it(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)
        out.write_text(out.read_text().replace("'INCR'", "'INCRBY'"))

        with pytest.raises(StaleLuaError) as excinfo:
            codegen.check(codegen_scripts, out)

        message = str(excinfo.value)
        assert f"python -m redis_lua_py generate codegen_scripts --out {out}" in message
        assert "-local current = redis.call('INCRBY', key)" in message
        assert "+local current = redis.call('INCR', key)" in message

    def test_a_missing_output_is_out_of_date(self, tmp_path):
        with pytest.raises(StaleLuaError, match="missing"):
            codegen.check(codegen_scripts, tmp_path / "_lua.py")

    def test_fails_a_test_rather_than_erroring_it(self):
        assert issubclass(StaleLuaError, AssertionError)


class TestCommandLine:
    def test_generate_then_check(self, tmp_path, capsys):
        out = tmp_path / "_lua.py"

        assert main(["generate", "codegen_scripts", "--out", str(out)]) == 0
        assert f"wrote {out}" in capsys.readouterr().out
        assert main(["generate", "codegen_scripts", "--out", str(out), "--check"]) == 0
        assert "up to date" in capsys.readouterr().out

    def test_check_exits_nonzero_when_out_of_date(self, tmp_path, capsys):
        out = tmp_path / "_lua.py"

        assert main(["generate", "codegen_scripts", "--out", str(out), "--check"]) == 1
        assert "out of date" in capsys.readouterr().err
        assert not out.exists()

    def test_reports_removed_leftovers(self, tmp_path, capsys):
        out = tmp_path / "lua"
        codegen.generate(codegen_scripts, out)
        (out / "old_name.lua").write_text(codegen_scripts.rate_limit.lua)

        assert main(["generate", "codegen_scripts", "--out", str(out)]) == 0
        assert f"removed {out / 'old_name.lua'}" in capsys.readouterr().out

    def test_an_unimportable_module_is_reported(self, tmp_path, capsys):
        assert main(["generate", "no_such_module", "--out", str(tmp_path / "_lua.py")]) == 1
        assert "cannot import no_such_module" in capsys.readouterr().err

    def test_runs_as_python_dash_m(self, tmp_path):
        out = tmp_path / "_lua.py"
        result = subprocess.run(
            [sys.executable, "-m", "redis_lua_py", "generate", "codegen_scripts", "--out", out],
            cwd=TESTS,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert codegen_scripts.rate_limit.lua == load(out).RATE_LIMIT
