"""Statements, and the finished compiler."""

from __future__ import annotations

import ast

from .. import _lua as lua
from .control import ControlFlowCompiler


class Compiler(ControlFlowCompiler):
    """Compiles one function body to Lua. Each helper it defines gets a child."""

    def block(self, body: list[ast.stmt]) -> list[lua.Stat]:
        out: list[lua.Stat] = []
        reachable = True
        for stmt in body:
            # Everything is still compiled, so a mistake is reported wherever it
            # sits. But Lua refuses to load a statement after `return`, and
            # Python would never run one, so the unreachable tail is dropped.
            compiled = self.stmt(stmt)
            if reachable:
                out.extend(compiled)
            if isinstance(stmt, ast.Return | ast.Break | ast.Continue | ast.Raise):
                reachable = False
        return out

    def stmt(self, node: ast.stmt) -> list[lua.Stat]:
        match node:
            case ast.Pass():
                return []
            case ast.Expr(value=ast.Constant(value=str())):
                return []  # a stray string literal, e.g. a docstring
            case ast.Expr(value=ast.Call() as inner):
                return self.call_statement(inner)
            case ast.Expr():
                self.fail(node, "this expression has no effect in Lua")
            case ast.Assign(targets=targets, value=value):
                if len(targets) != 1:
                    self.fail(node, "chained assignment (a = b = c) is not supported")
                return self.assign(node, targets[0], value)
            case ast.AnnAssign(target=target, value=value):
                if value is None:
                    self.fail(node, "a bare annotation declares nothing in Lua")
                return self.assign(node, target, value)
            case ast.AugAssign(target=target, op=op, value=value):
                synthetic = ast.BinOp(left=target, op=op, right=value)
                ast.copy_location(synthetic, node)
                return self.assign(node, target, synthetic, augmented=True)
            case ast.Return(value=None):
                return [self.return_stat(None)]
            case ast.Return(value=ast.expr() as value):
                rendered = self.expr(value)
                self.check_reply(value, rendered)
                return [self.return_stat(rendered)]
            case ast.If():
                return [self.if_stmt(node)]
            case ast.For():
                return [self.for_stmt(node)]
            case ast.While(test=test, orelse=orelse, body=body):
                if orelse:
                    self.fail(node, "while/else is not supported")
                return [lua.While(self.condition(test), self.loop_body(body))]
            case ast.Break():
                loop = self.enclosing_loop(node, "break")
                if loop.break_flag is not None:
                    flag = lua.Name(loop.break_flag)
                    return [lua.Assign([flag], [lua.Bool(True)]), lua.Break()]
                return [lua.Break()]
            case ast.Continue():
                self.enclosing_loop(node, "continue")
                # The body of a loop that continues sits in `repeat ... until
                # true`, so leaving that block is exactly Python's continue.
                return [lua.Break()]
            case ast.Assert(test=test, msg=msg):
                message = lua.Str("AssertionError") if msg is None else self.expr(msg)
                failed = lua.UnOp("not", self.condition(test))
                return [lua.If([(failed, [self.error_stat(message)])])]
            case ast.Raise():
                return [self.raise_stmt(node)]
            case ast.Try():
                return self.try_stmt(node)
            case ast.FunctionDef():
                return [self.nested_function(node)]
            case ast.AsyncFunctionDef() | ast.ClassDef():
                self.fail(node, "a script cannot define async functions or classes")
            case ast.Import() | ast.ImportFrom():
                self.fail(node, "a script cannot import anything")
            case ast.With() | ast.AsyncWith():
                self.fail(node, "'with' is not supported")
            case ast.Global() | ast.Nonlocal():
                self.fail(node, "'global' and 'nonlocal' are not supported")
            case _:
                self.fail(node, f"{type(node).__name__} statements are not supported")

    def check_reply(self, node: ast.expr, rendered: lua.Expr) -> None:
        """Refuse a returned table that is known to hold a nil.

        Redis converts a returned Lua array by walking it until the first nil
        and stopping there, so the reply is silently cut short rather than
        carrying a null in the middle. A literal nil in the table is never what
        the author meant, and it is invisible at the call site: the caller gets
        a shorter list, not a null.
        """
        if not isinstance(node, ast.List | ast.Tuple) or not isinstance(rendered, lua.Table):
            return
        for element, compiled in zip(node.elts, rendered.array, strict=True):
            if isinstance(compiled, lua.Nil):
                self.fail(
                    element,
                    "a nil in a returned table truncates the reply at that point",
                    hint="Redis stops converting the array at the first nil, so the caller "
                    "sees a shorter list rather than a null. Return a placeholder value "
                    "such as 0 or '' instead.",
                )

    def call_statement(self, node: ast.Call) -> list[lua.Stat]:
        # `items.append(x)` is the one method call worth special-casing: it is
        # how you build a return value, and Lua spells it t[#t + 1] = x.
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.known
        ):
            if self.kind(node.func.value) == "dict":
                self.fail(node, "a dict has no append() method")
            if len(node.args) != 1:
                self.fail(node, "append() takes exactly one argument")
            target = self.expr(node.func.value)
            slot = lua.Index(target, lua.BinOp("+", lua.UnOp("#", target), lua.Num(1)))
            return [lua.Assign([slot], [self.expr(node.args[0])])]
        return [lua.ExprStat(self.call(node))]

    def assign(
        self, node: ast.stmt, target: ast.expr, value: ast.expr, *, augmented: bool = False
    ) -> list[lua.Stat]:
        if isinstance(target, ast.Tuple | ast.List):
            return self.unpack_assign(node, target.elts, value)
        rhs = self.expr(value)
        match target:
            case ast.Name(id=name):
                if not lua.is_identifier(name):
                    self.fail(target, f"{name!r} is a reserved word in Lua")
                if not augmented and id(node) in self.simple_assigns:
                    if isinstance(rhs, lua.Function):
                        # A local function, unlike a local holding one, is in
                        # scope in its own body, so a named lambda can recurse.
                        return [lua.LocalFunction(name, list(rhs.params), list(rhs.body))]
                    return [lua.Local([name], [rhs])]
                return [lua.Assign([lua.Name(name)], [rhs])]
            case ast.Subscript():
                return [lua.Assign([self.subscript_target(target)], [rhs])]
            case _:
                self.fail(target, "unsupported assignment target")

    def unpack_assign(
        self, node: ast.stmt, elts: list[ast.expr], value: ast.expr
    ) -> list[lua.Stat]:
        """``a, b = ...``: Lua's multiple assignment, which also evaluates first.

        A tuple on the right maps one to one. Anything else is bound once and
        indexed, so ``head, tail = redis.lrange(k, 0, 1)`` runs the command once.
        """
        targets: list[lua.Expr] = []
        for element in elts:
            if isinstance(element, ast.Name):
                if not lua.is_identifier(element.id):
                    self.fail(element, f"{element.id!r} is a reserved word in Lua")
                targets.append(lua.Name(element.id))
            elif isinstance(element, ast.Subscript):
                targets.append(self.subscript_target(element))
            else:
                self.fail(element, "only names and subscripts can be unpacked into")

        prefix: list[lua.Stat] = []
        if isinstance(value, ast.Tuple | ast.List) and not any(
            isinstance(v, ast.Starred) for v in value.elts
        ):
            if len(value.elts) != len(elts):
                self.fail(node, f"cannot unpack {len(value.elts)} values into {len(elts)} targets")
            values = [self.expr(v) for v in value.elts]
        else:
            self._temp += 1
            bound = f"__t{self._temp}"
            prefix.append(lua.Local([bound], [self.expr(value)]))
            values = [lua.Index(lua.Name(bound), lua.Num(i + 1)) for i in range(len(elts))]

        if id(node) in self.simple_assigns:
            names = [e.id for e in elts if isinstance(e, ast.Name)]
            return [*prefix, lua.Local(names, values)]
        return [*prefix, lua.Assign(targets, values)]

    def nested_function(self, node: ast.FunctionDef) -> lua.Stat:
        """Compile a helper defined in the body to a Lua local function."""
        if not any(stmt is node for stmt in self.func.body):
            self.fail(
                node,
                "a helper function must be defined at the top level of the script body",
                hint="Move the def out of the if or loop it is in.",
            )
        if node.decorator_list:
            self.fail(node, "a helper function cannot be decorated")
        sig = node.args
        if sig.vararg or sig.kwarg or sig.posonlyargs or sig.kwonlyargs or sig.defaults:
            self.fail(
                node,
                "a helper function takes plain positional parameters only",
                hint="Calls inside a script are positional, so defaults, *args and "
                "keyword-only parameters have nothing to bind to.",
            )
        for name in [node.name, *(a.arg for a in sig.args)]:
            if not lua.is_identifier(name):
                self.fail(node, f"{name!r} is a reserved word in Lua")
        params = [a.arg for a in sig.args]

        child = self.scope_of(node, params, name=node.name)
        child.collect_assigned()
        body = node.body[1:] if ast.get_docstring(node) is not None else node.body
        statements = child.block(body)
        if child.hoisted:
            statements.insert(0, lua.Local(sorted(set(child.hoisted)), []))
        self.close_over(node, child)
        return lua.LocalFunction(node.name, params, statements)

    def lambda_expr(self, node: ast.Lambda) -> lua.Expr:
        """``lambda x: ...``, as an anonymous Lua function returning the expression.

        Lua closes over a name by reference, as Python does, so a lambda sees
        what the names around it hold when it runs, not when it was written.
        """
        sig = node.args
        if sig.vararg or sig.kwarg or sig.posonlyargs or sig.kwonlyargs or sig.defaults:
            self.fail(
                node,
                "a lambda takes plain positional parameters only",
                hint="Calls inside a script are positional, so defaults, *args and "
                "keyword-only parameters have nothing to bind to.",
            )
        params = [a.arg for a in sig.args]
        for name in params:
            if not lua.is_identifier(name):
                self.fail(node, f"{name!r} is a reserved word in Lua")
        # A compiler holds a function: here, one that returns the lambda's body.
        function = ast.FunctionDef(
            name="lambda", args=sig, body=[ast.Return(node.body)], decorator_list=[]
        )
        child = self.scope_of(ast.copy_location(function, node), params, name=None)
        value = child.expr(node.body)
        self.close_over(node, child)
        return lua.Function(tuple(params), (lua.Return(value),))

    def scope_of(self, node: ast.FunctionDef, params: list[str], *, name: str | None) -> Compiler:
        """A compiler of its own for a function defined in the body.

        The names the function assigns are then local to it, as they would be
        in Python, while it can still read the names of the script around it,
        call the script's helpers, and call itself.
        """
        child = type(self)(
            node,
            filename=self.filename,
            first_lineno=self.first_lineno,
            lines=self.lines,
            globalns=self.globalns,
        )
        own = set() if name is None else {name}
        child.params = params
        child.known = self.known | set(params) | own
        child.local_functions = self.local_functions | own
        child.callable_params = self.callable_params | set(params)
        # What the script's names hold is known inside the function too, except
        # for its parameters, which say nothing about their type.
        child.kinds = {k: v for k, v in self.kinds.items() if k not in params}
        child.declared = {k: v for k, v in self.declared.items() if k not in params}
        child.values = {k: list(v) for k, v in self.values.items() if k not in params}
        child.opaque = self.opaque - set(params)
        child._temp = self._temp
        return child

    def close_over(self, node: ast.FunctionDef | ast.Lambda, child: Compiler) -> None:
        """Take in what a function defined in the body needs from the script around it."""
        self._temp = child._temp
        self.helpers |= child.helpers

        bound = {
            arg.arg
            for inner in ast.walk(node)
            if isinstance(inner, ast.FunctionDef | ast.Lambda)
            for arg in inner.args.args
        }
        free = [
            read
            for read in ast.walk(node)
            if isinstance(read, ast.Name)
            and isinstance(read.ctx, ast.Load)
            and read.id not in bound
            and read.id not in child.assigned
        ]
        for read in free:
            if read.id in self.loop_targets and read.id not in child.loop_targets:
                self.fail(
                    read,
                    f"a function defined in the body cannot read the loop variable {read.id!r}",
                    hint="Lua gives each step of a loop a variable of its own, which a "
                    "function keeps, and which does not exist after the loop; Python has "
                    "one for the whole script. Pass the value in as a parameter.",
                )

        # Python resolves a free name when the function runs; Lua binds it where
        # the function is written. A script name first assigned after the
        # function is declared up front instead, so the function sees it too.
        for name in sorted({read.id for read in free}):
            first = self.first_assignment.get(name)
            if first is None or id(first) not in self.simple_assigns or first.lineno <= node.lineno:
                continue
            self.simple_assigns.discard(id(first))
            self.hoisted.extend(self.first_names[id(first)])
