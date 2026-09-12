"""Errors and warnings raised while compiling or running a script."""

from __future__ import annotations


def render_location(
    message: str,
    *,
    filename: str,
    lineno: int,
    col: int,
    source_line: str | None = None,
    hint: str | None = None,
) -> str:
    """Render a message under a caret pointing into the offending line.

    Shared by the compile error and the compile warnings, so that everything
    the compiler says about a script is laid out the same way.
    """
    parts = [f"{message}", f'  File "{filename}", line {lineno}']
    if source_line is not None:
        stripped = source_line.rstrip()
        indent = len(stripped) - len(stripped.lstrip())
        parts.append(f"    {stripped.lstrip()}")
        caret_col = max(col - indent, 0)
        parts.append("    " + " " * caret_col + "^")
    if hint:
        parts.append(f"  hint: {hint}")
    return "\n".join(parts)


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
        super().__init__(
            render_location(
                message,
                filename=filename,
                lineno=lineno,
                col=col,
                source_line=source_line,
                hint=hint,
            )
        )


class ScriptArgumentError(RedisLuaError):
    """A script was called with the wrong keys or arguments."""


class RedisLuaWarning(UserWarning):
    """Base class for every warning this package raises.

    Warnings mark a translation that is probably wrong but might not be, where
    an error would be presumptuous. Silence one with the standard machinery::

        warnings.filterwarnings("ignore", category=NilTruncationWarning)
    """


class NilTruncationWarning(RedisLuaWarning):
    """A returned table may hold a nil, which truncates the reply at that point.

    Redis converts a returned Lua array by walking it until the first ``nil``,
    so a name that is not assigned on every path silently shortens the reply
    rather than producing a null in the middle of it.
    """
