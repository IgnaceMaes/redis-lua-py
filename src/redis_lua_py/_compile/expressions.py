"""Operators, literals, subscripts, strings and conditions."""

from __future__ import annotations

import ast
import re

from .. import _lua as lua
from .analysis import is_cheap, is_none, literal_int, offset
from .scope import ScopeCompiler
from .tables import BIN_OPS, COMPARE_OPS


class ExpressionCompiler(ScopeCompiler):
    """Compiles expressions, and expressions used for their truth value."""

    def expr(self, node: ast.expr) -> lua.Expr:
        match node:
            case ast.Constant(value=None):
                return lua.Nil()
            case ast.Constant(value=bool() as v):
                return lua.Bool(v)
            case ast.Constant(value=int() | float() as v):
                return lua.Num(v)
            case ast.Constant(value=str() as v):
                return lua.Str(v)
            case ast.Constant(value=bytes() as v):
                return lua.Bytes(v)
            case ast.Name(id=name):
                if name not in self.known:
                    return self.module_constant(node, name)
                return lua.Name(name)
            case ast.BinOp():
                return self.binop(node)
            case ast.UnaryOp(op=ast.USub(), operand=operand):
                return lua.UnOp("-", self.expr(operand))
            case ast.UnaryOp(op=ast.UAdd(), operand=operand):
                return self.expr(operand)
            case ast.UnaryOp(op=ast.Not()):
                return self.condition(node)
            case ast.Compare():
                return self.compare(node)
            case ast.BoolOp():
                return self.boolop_value(node)
            case ast.IfExp():
                return self.ifexp(node)
            case ast.Call():
                return self.call(node)
            case ast.Subscript():
                return self.subscript(node)
            case ast.List(elts=elts) | ast.Tuple(elts=elts):
                return lua.Table(array=tuple(self.expr(e) for e in elts))
            case ast.Dict(keys=keys, values=values):
                pairs = []
                for key_node, value_node in zip(keys, values, strict=True):
                    if key_node is None:
                        self.fail(node, "dict unpacking (**) is not supported")
                    pairs.append((self.expr(key_node), self.expr(value_node)))
                return lua.Table(hash=tuple(pairs))
            case ast.JoinedStr(values=values):
                return self.fstring(node, values)
            case ast.Attribute():
                return self.module_attribute(node)
            case _:
                self.fail(node, f"{type(node).__name__} expressions are not supported")

    def binop(self, node: ast.BinOp) -> lua.Expr:
        left_kind, right_kind = self.kind(node.left), self.kind(node.right)
        if isinstance(node.op, ast.Add) and "str" in (left_kind, right_kind):
            # `+` on a string concatenates in Python; Lua spells that `..`.
            return lua.BinOp("..", self.expr(node.left), self.expr(node.right))
        if isinstance(node.op, ast.Mod) and left_kind == "str":
            return self.percent_format(node)
        if isinstance(node.op, ast.Mult) and "str" in (left_kind, right_kind):
            text, count = (node.left, node.right) if left_kind == "str" else (node.right, node.left)
            return lua.Call(lua.Name("string.rep"), (self.expr(text), self.expr(count)))

        left, right = self.expr(node.left), self.expr(node.right)
        if isinstance(node.op, ast.FloorDiv):
            return lua.Call(lua.Name("math.floor"), (lua.BinOp("/", left, right),))
        op = BIN_OPS.get(type(node.op))
        if op is None:
            self.fail(node, f"the {type(node.op).__name__} operator is not supported")
        return lua.BinOp(op, left, right)

    def percent_format(self, node: ast.BinOp) -> lua.Expr:
        """``"%s: %d" % (name, n)``, which Lua's string.format reads the same way."""
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            for match in re.finditer(r"%[-+ #0]*\d*(?:\.\d+)?(.)", node.left.value):
                if match.group(1) not in "diouxXeEfgGcs%":
                    self.fail(
                        node.left,
                        f"the %{match.group(1)} conversion has no Lua counterpart",
                        hint="string.format supports d, i, o, u, x, X, e, E, f, g, G, c and s.",
                    )
        if isinstance(node.right, ast.Dict):
            self.fail(node.right, "%-formatting from a mapping is not supported")
        values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        args = (self.expr(node.left), *(self.expr(v) for v in values))
        return lua.Call(lua.Name("string.format"), args)

    def compare(self, node: ast.Compare) -> lua.Expr:
        if len(node.ops) != 1:
            self.fail(
                node,
                "chained comparisons are not supported",
                hint="Split 'a < b < c' into 'a < b and b < c'.",
            )
        op_node, right_node = node.ops[0], node.comparators[0]
        if isinstance(op_node, ast.In | ast.NotIn):
            return self.membership(node.left, right_node, negate=isinstance(op_node, ast.NotIn))
        left = self.expr(node.left)

        if isinstance(op_node, ast.Is | ast.IsNot):
            if not is_none(right_node):
                self.fail(node, "'is' is only supported against None")
            return self.none_check(left, negate=isinstance(op_node, ast.IsNot))

        # `== None` means the same as `is None` to anyone reading it, and
        # compiling it to `== nil` would never match the false Redis sends.
        if isinstance(op_node, ast.Eq | ast.NotEq) and (is_none(right_node) or is_none(node.left)):
            operand = self.expr(right_node) if is_none(node.left) else left
            return self.none_check(operand, negate=isinstance(op_node, ast.NotEq))

        op = COMPARE_OPS.get(type(op_node))
        if op is None:
            self.fail(node, f"the {type(op_node).__name__} comparison is not supported")
        return lua.BinOp(op, left, self.expr(right_node))

    def membership(self, item: ast.expr, container: ast.expr, *, negate: bool) -> lua.Expr:
        """``x in c``: one of a literal set of choices, a substring, or a runtime check."""
        if isinstance(container, ast.List | ast.Tuple | ast.Set) and not any(
            isinstance(e, ast.Starred) for e in container.elts
        ):
            if not container.elts:
                return lua.Bool(negate)
            if is_cheap(item):
                needle = self.expr(item)
                op, join = ("~=", "and") if negate else ("==", "or")
                result: lua.Expr = lua.BinOp(op, needle, self.expr(container.elts[0]))
                for element in container.elts[1:]:
                    result = lua.BinOp(join, result, lua.BinOp(op, needle, self.expr(element)))
                return result
            haystack: lua.Expr = lua.Table(array=tuple(self.expr(e) for e in container.elts))
        elif self.kind(container) == "str":
            find = lua.Call(
                lua.Name("string.find"),
                (self.expr(container), self.expr(item), lua.Num(1), lua.Bool(True)),
            )
            return lua.BinOp("==" if negate else "~=", find, lua.Nil())
        else:
            haystack = self.expr(container)
        self.helpers.add("__contains")
        check: lua.Expr = lua.Call(lua.Name("__contains"), (haystack, self.expr(item)))
        return lua.UnOp("not", check) if negate else check

    def none_check(self, operand: lua.Expr, *, negate: bool) -> lua.Expr:
        # Not `== nil`: Redis reports a missing value to Lua as false.
        self.helpers.add("__isnil")
        check: lua.Expr = lua.Call(lua.Name("__isnil"), (operand,))
        return lua.UnOp("not", check) if negate else check

    def fstring(self, node: ast.expr, values: list[ast.expr]) -> lua.Expr:
        parts: list[lua.Expr] = []
        for value in values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(lua.Str(value.value))
            elif isinstance(value, ast.FormattedValue):
                if value.conversion not in (-1, 115):
                    self.fail(
                        value,
                        "!r and !a conversions are not supported in f-strings",
                        hint="Use !s, or no conversion; a value has no repr inside a script.",
                    )
                inner = self.expr(value.value)
                if value.format_spec is None:
                    parts.append(lua.Call(lua.Name("tostring"), (inner,)))
                else:
                    spec = lua.Str(self.printf_spec(value))
                    parts.append(lua.Call(lua.Name("string.format"), (spec, inner)))
            else:  # pragma: no cover - JoinedStr only holds these two kinds
                self.fail(value, "unsupported f-string component")
        if not parts:
            return lua.Str("")
        # Lua's .. is right-associative; folding the same way avoids emitting a
        # nest of parentheses that mean nothing.
        result = parts[-1]
        for part in reversed(parts[:-1]):
            result = lua.BinOp("..", part, result)
        return result

    def printf_spec(self, value: ast.FormattedValue) -> str:
        """Turn a Python format spec into the printf directive string.format takes."""
        spec_node = value.format_spec
        if not (
            isinstance(spec_node, ast.JoinedStr)
            and all(isinstance(part, ast.Constant) for part in spec_node.values)
        ):
            self.fail(value, "a format spec computed at runtime is not supported")
        spec = "".join(
            str(part.value) for part in spec_node.values if isinstance(part, ast.Constant)
        )
        match = re.fullmatch(r"([-+ #0]*)(\d*)(?:\.(\d+))?([deEfgGxXos]?)", spec)
        if match is None:
            self.fail(
                value,
                f"the format spec {spec!r} has no string.format counterpart",
                hint="Sign, zero padding, width, precision and the d, e, f, g, x, o and s "
                "types are supported; fill, alignment and grouping are not.",
            )
        # Python's `-` sign is its default, where printf's `-` left-aligns.
        flags, width, precision, conversion = match.groups()
        flags = flags.replace("-", "")
        if not conversion:
            if precision:
                conversion = "g"
            else:
                kind = self.kind(value.value)
                if width and kind is None:
                    self.fail(
                        value,
                        "a width without a type needs to know if the value is a string",
                        hint="Python aligns a string left and a number right. Add a type, "
                        "such as :10s or :10d, to say which.",
                    )
                conversion = "s"
                if kind == "str":
                    flags += "-"
        elif conversion == "s":
            flags += "-"
        return f"%{flags}{width}{'.' + precision if precision else ''}{conversion}"

    def subscript(self, node: ast.Subscript) -> lua.Expr:
        if isinstance(node.slice, ast.Slice):
            return self.slice(node, node.slice)
        position = literal_int(node.slice)

        if self.kind(node.value) == "str":
            # A Lua string cannot be indexed; one character is a substring.
            # string.sub counts back from the end for a negative position.
            at: lua.Expr = (
                lua.Num(position)
                if position is not None and position < 0
                else self.index(node.slice)
            )
            return lua.Call(lua.Name("string.sub"), (self.expr(node.value), at, at))

        if position is not None and position < 0:
            if not isinstance(node.value, ast.Name):
                self.fail(
                    node,
                    "a negative index needs a name to count back from",
                    hint="Bind the table to a name first, then index that name.",
                )
            obj = self.expr(node.value)
            last: lua.Expr = lua.UnOp("#", obj)
            # xs[-1] is xs[#xs], and xs[-k] is xs[#xs - k + 1].
            return lua.Index(obj, last if position == -1 else offset(last, position + 1))
        return lua.Index(self.expr(node.value), self.index(node.slice))

    def slice(self, node: ast.Subscript, part: ast.Slice) -> lua.Expr:
        """``v[i:j]``, for a string or a list, through the __slice helper."""
        if part.step is not None and literal_int(part.step) != 1:
            self.fail(
                part,
                "a slice with a step is not supported",
                hint="Loop over range(start, stop, step) and collect what you need.",
            )
        lower = lua.Nil() if part.lower is None else self.expr(part.lower)
        upper = lua.Nil() if part.upper is None else self.expr(part.upper)
        self.helpers.add("__slice")
        return lua.Call(lua.Name("__slice"), (self.expr(node.value), lower, upper))

    def subscript_target(self, node: ast.Subscript) -> lua.Expr:
        target = self.subscript(node)
        if not isinstance(target, lua.Index):
            self.fail(
                node,
                "this subscript cannot be assigned to",
                hint="A slice, and a character of a string, can be read but not assigned.",
            )
        return target

    def index(self, node: ast.expr) -> lua.Expr:
        """Translate a Python subscript to a Lua one.

        A position counts from 0 in Python and from 1 in Lua, while a dict key
        is used as it is. A literal says which it is; so does a value whose type
        is known. Anything else is decided at runtime, by the __key helper.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return lua.Str(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if node.value < 0:
                self.fail(
                    node,
                    "a negative index is only supported when indexing a name",
                    hint="Bind the table to a name, then write name[-1].",
                )
            return lua.Num(node.value + 1)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            self.fail(
                node,
                "a negative index is only supported as a literal",
                hint="Count back explicitly: t[len(t) - n].",
            )
        kind = self.kind(node)
        if kind == "str":
            return self.expr(node)
        if kind == "num":
            return lua.BinOp("+", self.expr(node), lua.Num(1))
        self.helpers.add("__key")
        return lua.Call(lua.Name("__key"), (self.expr(node),))

    def boolop_value(self, node: ast.BoolOp) -> lua.Expr:
        """``a or b`` used for its value, with Python's truthiness and laziness.

        Lua's own and/or test Lua truthiness, where 0 and '' are true, so the
        idiomatic ``tonumber(x) or 0`` would mean something else. A right side
        that is a name or a literal goes through a helper; anything else is
        wrapped in a function, so it only runs when Python would run it.
        """
        is_or = isinstance(node.op, ast.Or)
        self.helpers.add("__truthy")
        result = self.expr(node.values[0])
        for value in node.values[1:]:
            right = self.expr(value)
            if is_cheap(value):
                if is_or:
                    self.helpers.add("__or")
                else:
                    self.helpers.add("__and")
                result = lua.Call(lua.Name("__or" if is_or else "__and"), (result, right))
                continue
            self._temp += 1
            operand = f"__v{self._temp}"
            test: lua.Expr = lua.Call(lua.Name("__truthy"), (lua.Name(operand),))
            if not is_or:
                test = lua.UnOp("not", test)
            body = (lua.If([(test, [lua.Return(lua.Name(operand))])]), lua.Return(right))
            result = lua.Call(lua.Function((operand,), body), (result,))
        return result

    def ifexp(self, node: ast.IfExp) -> lua.Expr:
        """``a if c else b``, which Lua 5.1 has no expression for.

        ``c and a or b`` is exactly right when ``a`` cannot be false or nil,
        which a literal cannot; otherwise the branches go in a function.
        """
        test = self.condition(node.test)
        then, otherwise = self.expr(node.body), self.expr(node.orelse)
        if isinstance(then, lua.Num | lua.Str | lua.Bytes | lua.Table) or then == lua.Bool(True):
            return lua.BinOp("or", lua.BinOp("and", test, then), otherwise)
        body = (lua.If([(test, [lua.Return(then)])]), lua.Return(otherwise))
        return lua.Call(lua.Function((), body), ())

    def condition(self, node: ast.expr) -> lua.Expr:
        """Compile an expression used for its truth value.

        Lua treats 0 and '' as true, so anything that is not already a boolean
        gets routed through the __truthy helper.
        """
        match node:
            case ast.Compare():
                return self.compare(node)
            case ast.BoolOp(op=op, values=values):
                lua_op = "and" if isinstance(op, ast.And) else "or"
                result = self.condition(values[0])
                for value in values[1:]:
                    result = lua.BinOp(lua_op, result, self.condition(value))
                return result
            case ast.UnaryOp(op=ast.Not(), operand=operand):
                return lua.UnOp("not", self.condition(operand))
            case ast.Constant(value=bool() as v):
                return lua.Bool(v)
            case _:
                self.helpers.add("__truthy")
                return lua.Call(lua.Name("__truthy"), (self.expr(node),))
