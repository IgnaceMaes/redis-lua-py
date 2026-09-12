"""Generating Lua ahead of time, for code that ships without this package."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import codegen_scripts
from redis_lua_py import RedisLuaError, StaleLuaError, codegen
from redis_lua_py.__main__ import main
from redis_lua_py.codegen import _python_string

TESTS = Path(__file__).parent


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("generated_lua", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_finds_every_script_once_in_definition_order():
    scripts = codegen.collect(codegen_scripts)

    assert list(scripts) == ["rate_limit", "touch_all", "awkward"]
    assert scripts["rate_limit"] is codegen_scripts.rate_limit


def test_collect_accepts_a_dotted_name():
    assert codegen.collect("codegen_scripts") == codegen.collect(codegen_scripts)


def test_a_module_without_scripts_is_refused():
    with pytest.raises(RedisLuaError, match="no @script functions"):
        codegen.collect(ModuleType("empty"))


class TestPythonModule:
    def test_holds_each_script_exactly(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)
        generated = load(out)

        assert codegen_scripts.rate_limit.lua == generated.RATE_LIMIT
        assert codegen_scripts.touch_all.lua == generated.TOUCH_ALL
        assert codegen_scripts.awkward.lua == generated.AWKWARD
        assert generated.__all__ == ["RATE_LIMIT", "TOUCH_ALL", "AWKWARD"]

    def test_imports_nothing(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)

        tree = ast.parse(out.read_text())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]

    def test_records_what_the_caller_passes(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)
        text = out.read_text()

        assert "# rate_limit -- KEYS: key; ARGV: limit, ttl\n" in text
        assert "# touch_all -- KEYS: *keys; ARGV: ttl\n" in text
        assert "# awkward -- KEYS: none; ARGV: none\n" in text

    def test_records_how_to_regenerate(self, tmp_path):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)
        text = out.read_text()

        assert "python -m redis_lua_py generate codegen_scripts --out _lua.py" in text
        assert str(tmp_path) not in text

    def test_runs_through_plain_redis_py(self, tmp_path, client):
        out = tmp_path / "_lua.py"
        codegen.generate(codegen_scripts, out)
        generated = load(out)

        rate_limit = client.register_script(generated.RATE_LIMIT)
        assert rate_limit(keys=["u:42"], args=[2, 60]) == 1
        assert rate_limit(keys=["u:42"], args=[2, 60]) == 0
        assert rate_limit(keys=["u:42"], args=[2, 60]) == -1

        awkward = client.register_script(generated.AWKWARD)
        assert awkward() == codegen_scripts.AWKWARD.encode()

    def test_colliding_constant_names_are_refused(self, tmp_path):
        module = ModuleType("colliding")
        module.rate_limit = codegen_scripts.rate_limit  # type: ignore[attr-defined]
        module.RATE_LIMIT = codegen_scripts.touch_all  # type: ignore[attr-defined]

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
            "caf\u00e9 and a line separator" + chr(0x2028),
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
