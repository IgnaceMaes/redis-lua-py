"""Questions about Python syntax trees, answered without compiling anything."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator

from .. import _lua as lua

UNBOUND = object()


def unassigned_in_returns(
    body: list[ast.stmt], assigned: set[str], tracked: set[str]
) -> list[tuple[ast.expr, str]]:
    """Names returned inside a table that are not assigned on every path.

    The mirror image of the hoisting the compiler already does. Hoisting keeps
    a name assigned in one branch readable after the block -- but on a path
    where the assignment did not run, the name is nil, and a nil inside a
    returned table truncates the reply there. That is invisible at the call
    site, so it is worth pointing at.

    Only names the body assigns somewhere are tracked; a name it never assigns
    is a compile error already. Branches that always leave are excluded from
    the intersection, so `if not x: return 0` really does establish x below.
    """
    found: list[tuple[ast.expr, str]] = []

    def walk(statements: list[ast.stmt], live: set[str]) -> bool:
        """Compile-time flow over one block; True if it always leaves."""
        for statement in statements:
            match statement:
                case ast.Return(value=ast.List(elts=elts) | ast.Tuple(elts=elts)):
                    found.extend(
                        (element, element.id)
                        for element in elts
                        if isinstance(element, ast.Name)
                        and element.id in tracked
                        and element.id not in live
                    )
                    return True
                case ast.Return() | ast.Break():
                    return True
                case ast.Assign(targets=targets):
                    live.update(name for t in targets for name in target_names(t))
                case (
                    ast.AnnAssign(target=ast.Name(id=name))
                    | ast.AugAssign(target=ast.Name(id=name))
                ):
                    live.add(name)
                case ast.If(body=then, orelse=otherwise):
                    surviving = []
                    branch = set(live)
                    if not walk(then, branch):
                        surviving.append(branch)
                    if otherwise:
                        other = set(live)
                        if not walk(otherwise, other):
                            surviving.append(other)
                    else:
                        # The absent else is a path, and it assigns nothing.
                        surviving.append(set(live))
                    if not surviving:
                        return True
                    # Replaced, not intersected in place: every surviving set
                    # starts as a copy of `live`, so the names they agree on
                    # are exactly what is assigned after the block.
                    live.clear()
                    live.update(set.intersection(*surviving))
                case ast.For(target=ast.Name(id=name), body=inner):
                    # A loop may run zero times, so nothing it assigns is live
                    # after it -- including the loop variable, which in Lua
                    # does not outlive the loop at all.
                    walk(inner, set(live) | {name})
                case ast.While(body=inner) | ast.For(body=inner):
                    walk(inner, set(live))
                case ast.Try(body=inner, handlers=handlers, orelse=orelse, finalbody=final):
                    # Any statement of the try may be the one that failed, so
                    # nothing it assigns counts as established afterwards.
                    walk(inner + orelse, set(live))
                    for handler in handlers:
                        walk(handler.body, set(live))
                    walk(final, set(live))
        return False

    walk(body, set(assigned))
    return found


def dotted_name(node: ast.expr) -> tuple[str, ...] | None:
    """The parts of a dotted name, if that is all the expression is."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return tuple(reversed(parts))


def as_literal(value: object) -> lua.Expr | None:
    """The Lua literal for a Python constant, or None if it has none.

    ``bool`` is checked before ``int`` because it is one, and normalising
    through ``int()``/``str()`` keeps subclasses -- an ``IntEnum`` member, say
    -- from emitting their ``repr``.
    """
    match value:
        case bool():
            return lua.Bool(value)
        case int():
            return lua.Num(int(value))
        case float():
            return lua.Num(float(value))
        case str():
            return lua.Str(str(value))
        case bytes():
            return lua.Bytes(bytes(value))
        case None:
            return lua.Nil()
        case _:
            return None


def walk_scope(root: ast.AST) -> Iterator[ast.AST]:
    """Like ast.walk, but not into the bodies of nested functions or classes."""
    pending = list(ast.iter_child_nodes(root))
    while pending:
        node = pending.pop(0)
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            pending.extend(ast.iter_child_nodes(node))


def only_forwarded(
    root: ast.FunctionDef, name: str, is_command: Callable[[ast.Call], bool]
) -> bool:
    """True if every use of a name is a whole argument to a Redis command.

    Such a value is text going back to Redis, whatever it is annotated as.
    Anything else -- arithmetic, a comparison, an assignment to it, a use in
    a nested function that may shadow it -- makes the answer False, as does
    no use at all.
    """
    forwarded = {
        id(arg)
        for statement in root.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and is_command(node)
        for arg in node.args
    }
    uses = [
        node
        for statement in root.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and node.id == name
    ]
    return bool(uses) and all(id(use) in forwarded for use in uses)


def target_names(target: ast.expr) -> list[str]:
    """The names an assignment or loop target binds."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in target_names(element)]
    return []


def declares_exactly(node: ast.stmt, names: list[str]) -> bool:
    """True if every target of the statement is a name it assigns first."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Tuple | ast.List):
            return all(isinstance(e, ast.Name) for e in target.elts) and sorted(
                target_names(target)
            ) == sorted(names)
    return True


def is_cheap(node: ast.expr) -> bool:
    """An operand that can be evaluated early without changing anything."""
    match node:
        case ast.Constant() | ast.Name():
            return True
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant()):
            return True
        case ast.Attribute():
            return dotted_name(node) is not None
    return False


def loop_has(body: list[ast.stmt], kind: type[ast.stmt]) -> bool:
    """True if a break or continue in this body belongs to the loop around it.

    Nested loops own their own, and a nested function cannot reach the loop.
    """
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, kind):
            return True
        if isinstance(
            node, ast.For | ast.While | ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


def contains_return(body: list[ast.stmt]) -> bool:
    """True if a return in this body leaves the function around it."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return):
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return False


def is_catch_all(node: ast.expr) -> bool:
    if isinstance(node, ast.Tuple):
        return all(is_catch_all(element) for element in node.elts)
    return isinstance(node, ast.Name) and node.id in {"Exception", "BaseException"}


def callee_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else "Exception"


def without_finally(node: ast.Try) -> ast.Try:
    inner = ast.Try(body=node.body, handlers=node.handlers, orelse=node.orelse, finalbody=[])
    return ast.copy_location(inner, node)


def value_kind(value: object) -> str | None:
    """The kind of a module-level constant, as folded into the script."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str | bytes):
        return "str"
    if isinstance(value, int | float):
        return "num"
    return None


def binop_kind(op: ast.operator, left: str | None, right: str | None) -> str | None:
    """The kind of a binary operation from its operands' kinds.

    Python only adds a number to a number and a string to a string, so for `+`
    one known side is enough. "?" stands for a name still being worked out, as
    in `n = n + 1`: it takes whatever kind its other assignments give it.
    """
    sides = (left, right)
    if isinstance(op, ast.Add):
        if "str" in sides:
            return "str"
        if "num" in sides:
            return "num"
        if left == right == "list":
            return "list"
        return "?" if "?" in sides else None
    if isinstance(op, ast.Mult) and "str" in sides:
        return "str"
    if isinstance(op, ast.Mod):
        return left if left in {"str", "num", "?"} else None
    if isinstance(op, ast.Sub | ast.Div | ast.FloorDiv | ast.Pow):
        return "num"
    if set(sides) <= {"num", "?"}:
        return "?" if left == right == "?" else "num"
    return None


def is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def literal_int(node: ast.expr) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = literal_int(node.operand)
        return None if inner is None else -inner
    return None


def offset(expr: lua.Expr, delta: int) -> lua.Expr:
    if isinstance(expr, lua.Num) and isinstance(expr.value, int):
        return lua.Num(expr.value + delta)
    return lua.BinOp("+" if delta > 0 else "-", expr, lua.Num(abs(delta)))
