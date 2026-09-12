"""Reading a function's source, and naming where it came from."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from ..errors import CompileError

# Directories that mark the top of a project, for rendering the source path in
# the generated header relative to something stable.
ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", ".git")


def parse_function(func: Callable[..., Any]) -> tuple[ast.FunctionDef, str, int, list[str]]:
    try:
        lines, first_lineno = inspect.getsourcelines(func)
    except (OSError, TypeError) as exc:  # pragma: no cover - needs an exotic environment
        raise CompileError(
            f"cannot read the source of {func.__name__!r}. A script must be defined in a "
            "file on disk, not in a REPL or an exec() string."
        ) from exc

    source = textwrap.dedent("".join(lines))
    module = ast.parse(source)
    node = module.body[0]
    if not isinstance(node, ast.FunctionDef):
        raise CompileError(f"@script can only be applied to a function, got {type(node).__name__}")
    node.decorator_list = []
    filename = inspect.getsourcefile(func) or "<unknown>"
    return node, filename, first_lineno, source.splitlines()


@cache
def project_root(directory: Path) -> Path | None:
    """The nearest ancestor that looks like the top of a project."""
    for candidate in (directory, *directory.parents):
        if any((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
    return None


def provenance(filename: str) -> str:
    """The source path as the generated header should record it.

    Repo-relative, because the header is part of the script body and the body
    is what EVALSHA hashes. An absolute path would give the same script a
    different SHA on a laptop, in CI and in a container -- so the server's
    script cache would be cold once per environment rather than once per
    script -- and would put build-machine paths on the Redis server, where
    they show up in SCRIPT output.
    """
    path = Path(filename)
    if not path.is_absolute():
        return filename
    root = project_root(path.parent)
    return path.name if root is None else path.relative_to(root).as_posix()
