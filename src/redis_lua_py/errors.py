"""Errors raised while compiling or running a script."""

from __future__ import annotations


class RedisLuaError(Exception):
    """Base class for every error this package raises."""


class CompileError(RedisLuaError):
    """A Python function could not be turned into Lua."""


class UnsupportedSyntax(CompileError):
    """The function used Python that has no meaning inside a Redis script.

    Carries the location so the message points at the offending line rather
    than at the decorator.
    """

    def __init__(
        self,
        message: str,
        *,
        filename: str,
        lineno: int,
        col: int,
        source_line: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.message = message
        self.filename = filename
        self.lineno = lineno
        self.col = col
        self.source_line = source_line
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [f"{self.message}", f'  File "{self.filename}", line {self.lineno}']
        if self.source_line is not None:
            stripped = self.source_line.rstrip()
            indent = len(stripped) - len(stripped.lstrip())
            parts.append(f"    {stripped.lstrip()}")
            caret_col = max(self.col - indent, 0)
            parts.append("    " + " " * caret_col + "^")
        if self.hint:
            parts.append(f"  hint: {self.hint}")
        return "\n".join(parts)


class ScriptArgumentError(RedisLuaError):
    """A script was called with the wrong keys or arguments."""
