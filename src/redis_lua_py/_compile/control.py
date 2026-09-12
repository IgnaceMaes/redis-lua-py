"""if, loops, try, raise and return."""

from __future__ import annotations

import ast

from .. import _lua as lua
from .analysis import (
    callee_name,
    contains_return,
    is_catch_all,
    literal_int,
    loop_has,
    offset,
    without_finally,
)
from .base import Loop
from .calls import CallCompiler


class ControlFlowCompiler(CallCompiler):
    """Compiles branches, loops, and errors raised and caught."""

    def loop_body(
        self, body: list[ast.stmt], binding: list[lua.Stat] | None = None
    ) -> list[lua.Stat]:
        """Compile a loop body, making room for continue when it uses one.

        Lua 5.1 has no continue and no goto. A body that continues runs inside
        `repeat ... until true`, where continue is a break out of that block. A
        break in the same loop sets a flag first, and the loop breaks on it.
        """
        continues = loop_has(body, ast.Continue)
        flag = None
        if continues and loop_has(body, ast.Break):
            self._temp += 1
            flag = f"__brk{self._temp}"
        self.flow.append(Loop(break_flag=flag))
        try:
            inner = self.block(body)
        finally:
            self.flow.pop()

        out = list(binding or [])
        if not continues:
            return out + inner
        if flag is not None:
            out.append(lua.Local([flag], [lua.Bool(False)]))
        out.append(lua.Repeat(inner))
        if flag is not None:
            out.append(lua.If([(lua.Name(flag), [lua.Break()])]))
        return out

    def enclosing_loop(self, node: ast.stmt, keyword: str) -> Loop:
        loop = self.flow[-1] if self.flow else None
        if loop is None:
            self.fail(
                node,
                f"'{keyword}' cannot leave a try block",
                hint="The try body runs as a function under pcall, so the loop around it "
                "is out of reach. Set a flag inside the try, and act on it after.",
            )
        return loop

    def return_stat(self, value: lua.Expr | None) -> lua.Stat:
        """A return, which inside a try body also has to leave the protected call."""
        if self.protected_depth:
            return lua.Return(lua.Values((lua.Bool(True), value or lua.Nil())))
        return lua.Return(value)

    def error_stat(self, message: lua.Expr) -> lua.Stat:
        """``error(message, 0)``.

        Level 0 keeps Lua's `user_script:12:` position out of the message, so
        the caller, or an except block, sees exactly what was raised.
        """
        if not isinstance(message, lua.Str):
            message = lua.Call(lua.Name("tostring"), (message,))
        return lua.ExprStat(lua.Call(lua.Name("error"), (message, lua.Num(0))))

    def raise_stmt(self, node: ast.Raise) -> lua.Stat:
        """``raise SomeError('message')``, which reaches the caller as an error reply."""
        if node.cause is not None:
            self.fail(
                node,
                "raise ... from ... is not supported",
                hint="An error inside a script carries only a message; raise it on its own.",
            )
        exc = node.exc
        if exc is None:
            if not self.handlers:
                self.fail(node, "a bare raise can only re-raise inside an except block")
            self.helpers.add("__errmsg")
            return self.error_stat(lua.Call(lua.Name("__errmsg"), (lua.Name(self.handlers[-1]),)))
        if isinstance(exc, ast.Call) and not exc.keywords:
            if len(exc.args) > 1:
                self.fail(exc, "an error raised in a script carries a single message")
            if exc.args:
                return self.error_stat(self.expr(exc.args[0]))
            return self.error_stat(lua.Str(callee_name(exc.func)))
        if isinstance(exc, ast.Name):
            if exc.id in self.known:
                # The name an `except ... as e` bound, which holds a message.
                return self.error_stat(self.expr(exc))
            return self.error_stat(lua.Str(exc.id))
        self.fail(exc, "only raise SomeError('message') is supported")

    def try_stmt(self, node: ast.Try) -> list[lua.Stat]:
        """try/except/else/finally, over pcall.

        The body runs as a local function under pcall, so an error inside it --
        from redis.call, a raise, or Lua itself -- lands in the except block. A
        return inside the body has to leave that function first, so it hands
        back a flag and the value, which are returned again once pcall is done.
        """
        if len(node.handlers) > 1:
            self.fail(
                node.handlers[1],
                "only one except clause is supported",
                hint="An error inside a script carries a message, but no type to tell "
                "clauses apart. Catch Exception, and branch on the message.",
            )
        handler = node.handlers[0] if node.handlers else None
        if handler is not None and handler.type is not None and not is_catch_all(handler.type):
            self.fail(
                handler.type,
                f"except {ast.unparse(handler.type)} cannot be told apart from any other error",
                hint="An error inside a script carries only a message, so every except "
                "catches everything. Write except Exception, and branch on the message.",
            )

        if node.finalbody:
            # The try/except/else runs protected as a whole, then the finally
            # block, then any error is raised again or the return goes through.
            inner: list[ast.stmt] = node.body if handler is None else [without_finally(node)]
            setup, ok, result, value, returns = self.protected(inner)
            out = [*setup, *self.block(node.finalbody)]
            self.helpers.add("__errmsg")
            reraise = self.error_stat(lua.Call(lua.Name("__errmsg"), (lua.Name(result),)))
            out.append(lua.If([(lua.UnOp("not", lua.Name(ok)), [reraise])]))
            if returns:
                out.append(lua.If([(lua.Name(result), [self.return_stat(lua.Name(value))])]))
            return out

        assert handler is not None  # a try without finally has a handler
        setup, ok, result, value, returns = self.protected(node.body)
        success: list[lua.Stat] = []
        if returns:
            success.append(lua.If([(lua.Name(result), [self.return_stat(lua.Name(value))])]))
        success += self.block(node.orelse)

        failure: list[lua.Stat] = []
        if handler.name is not None:
            self.helpers.add("__errmsg")
            message = lua.Call(lua.Name("__errmsg"), (lua.Name(result),))
            failure.append(lua.Local([handler.name], [message]))
        self.handlers.append(result)
        try:
            failure += self.block(handler.body)
        finally:
            self.handlers.pop()

        if success:
            return [*setup, lua.If([(lua.Name(ok), success)], failure)]
        return [*setup, lua.If([(lua.UnOp("not", lua.Name(ok)), failure)])]

    def protected(self, body: list[ast.stmt]) -> tuple[list[lua.Stat], str, str, str, bool]:
        """Compile a body into a local function and a pcall of it.

        Returns the statements, the names pcall's results are bound to, and
        whether the body returns, in which case a third result carries the value.
        """
        self._temp += 1
        n = self._temp
        function, ok, result, value = f"__try{n}", f"__ok{n}", f"__r{n}", f"__v{n}"
        self.flow.append(None)
        self.protected_depth += 1
        try:
            inner = self.block(body)
        finally:
            self.protected_depth -= 1
            self.flow.pop()
        returns = contains_return(body)
        names = [ok, result, value] if returns else [ok, result]
        call = lua.Call(lua.Name("pcall"), (lua.Name(function),))
        return (
            [lua.LocalFunction(function, [], inner), lua.Local(names, [call])],
            ok,
            result,
            value,
            returns,
        )

    def if_stmt(self, node: ast.If) -> lua.If:
        branches = [(self.condition(node.test), self.block(node.body))]
        orelse = node.orelse
        # Collapse `else: if ...` chains into Lua's elseif.
        while len(orelse) == 1 and isinstance(orelse[0], ast.If):
            nested = orelse[0]
            branches.append((self.condition(nested.test), self.block(nested.body)))
            orelse = nested.orelse
        return lua.If(branches, self.block(orelse))

    def for_stmt(self, node: ast.For) -> lua.Stat:
        if node.orelse:
            self.fail(node, "for/else is not supported")
        names = self.loop_names(node.target)
        self.known.update(names)
        it = node.iter

        if (
            isinstance(it, ast.Call)
            and isinstance(it.func, ast.Attribute)
            and it.func.attr in {"items", "keys", "values"}
            and not it.args
            and not self.is_namespace(it.func.value)
        ):
            return self.pairs_loop(node, names, it.func.value, it.func.attr)
        if self.kind(it) == "dict":
            # Iterating a dict walks its keys, as .keys() does.
            return self.pairs_loop(node, names, it, "keys")
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range":
            if len(names) != 1:
                self.fail(node.target, "range() yields one value per step")
            return self.range_loop(node, names[0], it)
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "enumerate":
            return self.enumerate_loop(node, names, it)

        iterable = self.expr(it)
        prefix, seq, idx = self.bind_sequence(iterable)
        element: lua.Expr = lua.Index(seq, lua.Name(idx))
        binding: list[lua.Stat]
        if len(names) == 1:
            binding = [lua.Local(names, [element])]
        else:
            # `for name, n in pairs:` unpacks each element by position.
            self._temp += 1
            item = f"__e{self._temp}"
            binding = [
                lua.Local([item], [element]),
                lua.Local(
                    names, [lua.Index(lua.Name(item), lua.Num(i + 1)) for i in range(len(names))]
                ),
            ]
        body = self.loop_body(node.body, binding)
        return lua.Block([*prefix, lua.NumericFor(idx, lua.Num(1), lua.UnOp("#", seq), None, body)])

    def loop_names(self, target: ast.expr) -> list[str]:
        if isinstance(target, ast.Name):
            names = [target.id]
        elif isinstance(target, ast.Tuple | ast.List) and all(
            isinstance(e, ast.Name) for e in target.elts
        ):
            names = [e.id for e in target.elts if isinstance(e, ast.Name)]
        else:
            self.fail(target, "a loop can only bind a name, or a flat tuple of names")
        for name in names:
            if not lua.is_identifier(name):
                self.fail(target, f"{name!r} is a reserved word in Lua")
        return names

    def bind_sequence(self, iterable: lua.Expr) -> tuple[list[lua.Stat], lua.Expr, str]:
        """Bind a sequence once, and name an index to walk it with.

        Walking by index, rather than with ipairs, keeps a call in the iterable
        from being re-evaluated on every step. A plain name is already stable,
        so it is left alone.
        """
        self._temp += 1
        idx = f"__i{self._temp}"
        if isinstance(iterable, lua.Name):
            return [], iterable, idx
        seq = f"__seq{self._temp}"
        return [lua.Local([seq], [iterable])], lua.Name(seq), idx

    def pairs_loop(self, node: ast.For, names: list[str], table: ast.expr, method: str) -> lua.Stat:
        """``d.items()``, ``d.keys()`` and ``d.values()``, over Lua's pairs().

        pairs() visits entries in no particular order, as does Lua itself;
        sort the result if the order reaches the caller.
        """
        wanted = 2 if method == "items" else 1
        if len(names) != wanted:
            shape = "a key and a value" if wanted == 2 else "one value"
            self.fail(node.target, f"{method}() yields {shape} per step")
        iterable = self.expr(table)
        if method == "values":
            self._temp += 1
            names = [f"__k{self._temp}", names[0]]
        iterator = lua.Call(lua.Name("pairs"), (iterable,))
        return lua.GenericFor(names, iterator, self.loop_body(node.body))

    def enumerate_loop(self, node: ast.For, names: list[str], call: ast.Call) -> lua.Stat:
        if call.keywords or not 1 <= len(call.args) <= 2:
            self.fail(call, "enumerate() takes an iterable and an optional start")
        if len(names) != 2:
            self.fail(node.target, "enumerate() yields an index and a value per step")
        start = literal_int(call.args[1]) if len(call.args) == 2 else 0
        if start is None:
            self.fail(call.args[1], "enumerate() start must be an integer literal")

        self.kinds[names[0]] = "num"
        prefix, seq, idx = self.bind_sequence(self.expr(call.args[0]))
        # Lua counts from 1; Python's enumerate counts from `start`.
        position: lua.Expr = lua.Name(idx)
        if start != 1:
            position = offset(position, start - 1)
        body = self.loop_body(
            node.body,
            [
                lua.Local([names[0]], [position]),
                lua.Local([names[1]], [lua.Index(seq, lua.Name(idx))]),
            ],
        )
        return lua.Block([*prefix, lua.NumericFor(idx, lua.Num(1), lua.UnOp("#", seq), None, body)])

    def range_loop(self, node: ast.For, var: str, call: ast.Call) -> lua.Stat:
        args = call.args
        if not 1 <= len(args) <= 3:
            self.fail(call, "range() takes one to three arguments")

        step: lua.Expr | None = None
        descending = False
        if len(args) == 3:
            step_value = literal_int(args[2])
            if step_value is None:
                self.fail(args[2], "range() step must be an integer literal")
            if step_value == 0:
                self.fail(args[2], "range() step cannot be zero")
            descending = step_value < 0
            step = lua.Num(step_value)

        if len(args) == 1:
            start: lua.Expr = lua.Num(0)
            stop_node = args[0]
        else:
            start = self.expr(args[0])
            stop_node = args[1]

        # Python's range excludes the stop value; Lua's numeric for includes it.
        stop = offset(self.expr(stop_node), 1 if descending else -1)
        self.kinds[var] = "num"
        return lua.NumericFor(var, start, stop, step, self.loop_body(node.body))
