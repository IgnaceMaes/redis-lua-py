"""The command line: compile scripts to Lua ahead of time.

python -m redis_lua_py generate myproj.scripts --out src/myproj/_lua.py
python -m redis_lua_py generate myproj.scripts --out src/myproj/lua/ --check
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import codegen
from .errors import RedisLuaError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redis-lua-py",
        description="Compile @script functions to Lua ahead of time.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser(
        "generate",
        help="write a module's scripts to disk",
        description="Write every @script in a module to disk, for code that should not "
        "depend on redis-lua-py at runtime.",
    )
    generate.add_argument(
        "module", help="dotted name of the module holding the scripts, e.g. myproj.scripts"
    )
    generate.add_argument(
        "--out",
        required=True,
        help="a .py path for one module of string constants, "
        "or a directory for one .lua file per script",
    )
    generate.add_argument(
        "--check",
        action="store_true",
        help="write nothing, and exit 1 if the output is out of date",
    )
    options = parser.parse_args(argv)

    # `python -m` puts the working directory on the path and the console script
    # does not; a project's own modules should import the same either way.
    cwd = os.getcwd()
    if cwd not in sys.path and "" not in sys.path:
        sys.path.insert(0, cwd)

    try:
        if options.check:
            codegen.check(options.module, options.out)
            print(f"{options.out} is up to date")
            return 0
        changed = codegen.generate(options.module, options.out)
    except ImportError as exc:
        print(f"redis-lua-py: cannot import {options.module}: {exc}", file=sys.stderr)
        return 1
    except RedisLuaError as exc:
        print(f"redis-lua-py: {exc}", file=sys.stderr)
        return 1

    for path in changed:
        print(f"{'wrote' if path.exists() else 'removed'} {path}")
    if not changed:
        print(f"{options.out} is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
