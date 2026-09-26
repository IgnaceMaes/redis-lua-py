"""The signature, and the names a body assigns."""

from __future__ import annotations

import ast

from .. import _lua as lua
from .analysis import declares_exactly, only_forwarded, target_names, walk_scope
from .base import CompilerBase
from .tables import REDIS_DIRECT


class ScopeCompiler(CompilerBase):
    """Binds KEYS and ARGV to parameters, and decides which names are hoisted."""

    def collect_assigned(self) -> None:
        """Decide which names need hoisting to function scope.

        Python scopes assignments to the whole function; Lua's ``local`` scopes
        them to the enclosing block. A name assigned inside an ``if`` and read
        after it must therefore be declared up front, or the read sees ``nil``.

        A name whose *first* assignment sits at the top level of the body needs
        no hoist: a ``local`` there is already in scope for everything that
        follows, nested blocks included. Only names that first appear inside a
        block get declared up front.
        """
        nodes: dict[str, list[ast.stmt]] = {}

        def record(name: str, node: ast.stmt) -> None:
            nodes.setdefault(name, []).append(node)

        # A helper function has a scope of its own, compiled separately, so
        # what it assigns is not the script's business.
        for node in walk_scope(self.func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in target_names(target):
                        record(name, node)
                    if isinstance(target, ast.Name):
                        self.values.setdefault(target.id, []).append(node.value)
                    else:
                        self.opaque.update(target_names(target))
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(
                node.target, ast.Name
            ):
                record(node.target.id, node)
                if isinstance(node, ast.AnnAssign):
                    declared = self._annotation_kind(self._without_none(node.annotation))
                    if declared is not None:
                        self.declared.setdefault(node.target.id, declared)
                if isinstance(node, ast.AugAssign):
                    value: ast.expr = ast.BinOp(left=node.target, op=node.op, right=node.value)
                    self.values.setdefault(node.target.id, []).append(value)
                elif node.value is not None:
                    self.values.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, ast.For):
                # Loop targets get Lua's own loop scope; only record them so
                # that reads of the name resolve.
                self.known.update(target_names(node.target))
                self.loop_targets.update(target_names(node.target))
            elif isinstance(node, ast.FunctionDef):
                self.known.add(node.name)
                self.local_functions.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                # Bound as a local inside the except block itself, to the message.
                self.known.add(node.name)
                self.kinds[node.name] = "str"

        by_id: dict[int, ast.stmt] = {}
        for name, assignments in nodes.items():
            self.known.add(name)
            self.assigned.add(name)
            if name in self.params:
                # Already a local from the prelude. Declaring it again would
                # shadow the argument with nil.
                continue
            # ast.walk is breadth-first, so ask for source order explicitly.
            first = min(assignments, key=lambda n: (n.lineno, n.col_offset))
            self.first_assignment[name] = first
            self.first_names.setdefault(id(first), []).append(name)
            by_id[id(first)] = first

        top_level = {id(stmt) for stmt in self.func.body}
        for node_id, names in self.first_names.items():
            # `local a, b = ...` only works when the statement introduces
            # every name it assigns; otherwise the new ones are declared up
            # front and the statement becomes a plain assignment.
            if node_id in top_level and declares_exactly(by_id[node_id], names):
                self.simple_assigns.add(node_id)
            else:
                self.hoisted.extend(names)

    def compile_signature(self) -> list[lua.Stat]:
        sig = self.func.args
        if sig.vararg or sig.kwarg:
            self.fail(
                self.func,
                "*args and **kwargs are not supported in a script signature",
                hint="Annotate one parameter as list[Key] for a variable number of keys, "
                "or as list[str] for a variable number of arguments.",
            )
        if sig.posonlyargs:
            self.fail(self.func, "positional-only parameters are not supported")

        prelude: list[lua.Stat] = []
        for arg in [*sig.args, *sig.kwonlyargs]:
            name = arg.arg
            if not lua.is_identifier(name):
                self.fail(arg, f"{name!r} is a reserved word in Lua")
            self.params.append(name)
            self.known.add(name)

            item = self._list_item(arg.annotation)
            if item is not None:
                # KEYS and ARGV have no names, only positions, so a list can
                # only take whatever is left after the fixed parameters: one
                # list of keys, and one list of arguments.
                self.kinds[name] = "list"
                if self._is_key(item):
                    if self.variadic_key is not None:
                        self.fail(arg, "only one list[Key] parameter is supported")
                    self.variadic_key = name
                else:
                    if self.variadic_arg is not None:
                        self.fail(
                            arg,
                            "only one list parameter of arguments is supported",
                            hint="Interleave the values in one list and index it "
                            "with a stride, as in values[3 * i + 1].",
                        )
                    self.variadic_arg = name
                    if self._is_numeric(item):
                        self.numeric_args.add(name)
                continue

            if self._is_key(arg.annotation):
                self.keys.append(name)
                source: lua.Expr = lua.Index(lua.Global("KEYS"), lua.Num(len(self.keys)))
            else:
                self.args.append(name)
                source = lua.Index(lua.Global("ARGV"), lua.Num(len(self.args)))
                if self._is_numeric(arg.annotation):
                    # ARGV always arrives as strings; an int/float annotation
                    # is the author asking for the conversion. Unless the value
                    # only goes back to Redis: a Lua number is a double, so
                    # converting would round an integer past 2^53 on the way.
                    self.numeric_args.add(name)
                    if not only_forwarded(self.func, name, self._is_command):
                        source = lua.Call(lua.Global("tonumber"), (source,))
                elif self._annotation_name(arg.annotation) == "bool":
                    # A bool arrives as "1" or "0", and "0" is a string Lua and
                    # Python both count as true. Comparing makes it a boolean.
                    source = lua.BinOp("==", source, lua.Str("1"))
            kind = self._annotation_kind(arg.annotation)
            if kind is not None:
                self.kinds[name] = kind
            prelude.append(lua.Local([name], [source]))

        if self.variadic_key is not None:
            self.keys.append(self.variadic_key)
            prelude += self.rest_of("KEYS", self.variadic_key, len(self.keys), numeric=False)
        if self.variadic_arg is not None:
            self.args.append(self.variadic_arg)
            prelude += self.rest_of(
                "ARGV",
                self.variadic_arg,
                len(self.args),
                numeric=self.variadic_arg in self.numeric_args,
            )

        if sig.defaults or any(d is not None for d in sig.kw_defaults):
            self.fail(
                self.func,
                "default values are not supported",
                hint="Redis has no notion of an absent ARGV entry; pass the value explicitly.",
            )
        return prelude

    @staticmethod
    def _annotation_name(node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Subscript):
            return ScopeCompiler._annotation_name(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.rsplit(".", 1)[-1].split("[", 1)[0]
        return None

    def _is_command(self, node: ast.Call) -> bool:
        """A call that sends its arguments to Redis as they are."""
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and self.namespace_kind(func.value.id) == "redis"
            and (func.attr not in REDIS_DIRECT or func.attr in {"call", "pcall"})
        )

    def _is_key(self, node: ast.expr | None) -> bool:
        return self._annotation_name(node) == "Key"

    def _is_numeric(self, node: ast.expr | None) -> bool:
        return self._annotation_name(node) in {"int", "float"}

    def _annotation_kind(self, node: ast.expr | None) -> str | None:
        name = self._annotation_name(node)
        if name in {"int", "float"}:
            return "num"
        if name == "bool":
            return "bool"
        if name in {"Key", "str", "bytes", "memoryview"}:
            return "str"
        return None

    @staticmethod
    def _without_none(node: ast.expr | None) -> ast.expr | None:
        """``X`` from ``X | None`` or ``Optional[X]``: None is no kind of its own."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = [
                side
                for side in (node.left, node.right)
                if not (isinstance(side, ast.Constant) and side.value is None)
            ]
            return sides[0] if len(sides) == 1 else None
        name = ScopeCompiler._annotation_name(node)
        if isinstance(node, ast.Subscript) and name == "Optional":
            return node.slice
        return node

    @staticmethod
    def _list_item(node: ast.expr | None) -> ast.expr | None:
        """The element annotation of a ``list[...]`` parameter, if it is one."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(node, ast.Subscript) and ScopeCompiler._annotation_name(node.value) in {
            "list",
            "List",
            "Sequence",
        }:
            return node.slice
        return None

    def rest_of(self, table: str, name: str, start: int, *, numeric: bool) -> list[lua.Stat]:
        """Collect KEYS or ARGV from ``start`` onwards into a local table.

        A loop rather than ``{unpack(ARGV, start)}``, which would put every
        element on the Lua stack at once and fail past a few thousand.
        """
        self._temp += 1
        idx = f"__i{self._temp}"
        item: lua.Expr = lua.Index(lua.Global(table), lua.Name(idx))
        if numeric:
            item = lua.Call(lua.Global("tonumber"), (item,))
        target = lua.Name(name)
        slot = lua.Index(target, lua.BinOp("+", lua.UnOp("#", target), lua.Num(1)))
        return [
            lua.Local([name], [lua.Table()]),
            lua.NumericFor(
                idx,
                lua.Num(start),
                lua.UnOp("#", lua.Global(table)),
                None,
                [lua.Assign([slot], [item])],
            ),
        ]
