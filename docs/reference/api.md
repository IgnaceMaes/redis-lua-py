# API reference

Everything in `redis_lua_py.__all__`. The package is small on purpose: one
decorator, one annotation, two namespaces, and the errors.

```python
from redis_lua_py import Key, call, cjson, redis, script
from redis_lua_py import BoundScript, CompiledScript
```

## `script`

```python
def script(
    func: Callable[..., R] | None = None,
    /,
    *,
    name: str | None = None,
    header: bool = True,
) -> CompiledScript[R] | Callable[[Callable[..., R]], CompiledScript[R]]
```

Compile a function into a Redis Lua script. Usable bare or called:

```python
@script
def touch(key: Key) -> int: ...


@script(name="touch_v2", header=False)
def touch(key: Key) -> int: ...
```

| Parameter | Meaning |
| --- | --- |
| `name` | overrides the name in the generated header and in errors |
| `header` | `False` drops the provenance comment entirely, for anyone who wants the script body and nothing else |

Parameters annotated [`Key`](#key) become `KEYS`, in declaration order; every
other parameter becomes `ARGV`. See
[Keys and arguments](../guide/keys-and-arguments.md).

The return annotation describes what the *caller* gets back, and is carried
through to the call: `-> int` makes the script a `CompiledScript[int]`. The
compiler itself does not read it. See
[What the caller gets](../guide/return-values.md).

Define scripts at module level, where they compile once at import.

Raises [`UnsupportedSyntax`](errors.md#unsupportedsyntax) at decoration time,
pointing at the line at fault, if the body strays outside
[the supported subset](supported-subset.md).

## `Key`

```python
class Key(str)
```

Marks a parameter as a Redis key. A `str` subclass, so it is inert at runtime
and the annotation is the whole of it.

Getting this right matters: Redis Cluster routes a script by its declared keys,
so a key passed as an argument will be invisible to the router.

## `redis`

The script namespace. `redis.incr(key)` becomes `redis.call('INCR', key)`;
underscores split into subcommand tokens, so `redis.script_load(x)` becomes
`redis.call('SCRIPT', 'LOAD', x)`.

Names are checked at compile time against Redis' own command table;
`redis.call(...)` itself is never checked and is the escape hatch. See
[Calling Redis commands](../guide/calling-redis-commands.md).

The compiler identifies the namespace by value rather than by the name it is
imported under, so every alias works and nothing is reserved.

## `call`

An alias of [`redis`](#redis), for modules that would rather not rename
anything.

## `cjson`

The JSON library Redis exposes to scripts: `cjson.encode` and `cjson.decode`,
which pass through under their own names.

## `CompiledScript`

```python
class CompiledScript(Generic[R])
```

What `@script` returns. Calling it runs `EVALSHA` and falls back to `EVAL` the
first time, or whenever the server has dropped the script from its cache.

```python
script(client, /, *positional, **keyword) -> R              # sync client
script(client, /, *positional, **keyword) -> Awaitable[R]   # async client
```

| Attribute | Type | What it is |
| --- | --- | --- |
| `name` | `str` | the script's name, in the header and in errors |
| `lua` | `str` | the full Lua source, exactly as sent to Redis |
| `params` | `tuple[str, ...]` | every parameter, in declaration order |
| `keys` | `tuple[str, ...]` | the parameters annotated `Key` or `list[Key]`, in `KEYS` order |
| `args` | `tuple[str, ...]` | everything else, in `ARGV` order |
| `variadic_key` | `str \| None` | the `list[Key]` parameter, which fills the rest of `KEYS` |
| `variadic_arg` | `str \| None` | the list parameter that fills the rest of `ARGV` |
| `doc` | `str \| None` | the function's docstring |
| `source` | `str` | where it was defined, repo-relative |

### `bind`

```python
def bind(self, client) -> BoundScript[R]              # sync client
def bind(self, client) -> BoundScript[Awaitable[R]]   # async client
```

Attach a client, so calls do not have to pass one. See
[Binding a client](../guide/binding-a-client.md).

## `BoundScript`

```python
class BoundScript(Generic[T])
```

A script with its client already attached, produced by
[`bind`](#bind). The type parameter is what a call returns: the script's own
return type for a sync client, an awaitable of it for an async one.

```python
bound(*positional, **keyword) -> T
```

Exposes `name`, `lua`, `params`, `keys`, `args` and `doc` from the script it
wraps, and leaves that script usable against any other client.

## `codegen`

```python
from redis_lua_py import codegen
```

Compile scripts to Lua ahead of time, for code that should not depend on this
package at runtime. See
[Shipping without the dependency](../guide/build-time.md). Every function takes
the module as a module object or a dotted name, and `out` as a path:

- ending in `.py`, for one module holding every script as a string constant
  and importing nothing
- anything else, for a directory with one `.lua` file per script

### `codegen.generate`

```python
def generate(module: ModuleType | str, out: str | Path) -> list[Path]
```

Write the scripts under `out`, and return the paths that changed. A file that
already holds the right content is left alone. In a directory, a `.lua` file
generated earlier for a script that no longer exists is removed.

### `codegen.check`

```python
def check(module: ModuleType | str, out: str | Path) -> None
```

Raise [`StaleLuaError`](errors.md#staleluaerror), with a diff, unless `out`
holds exactly what `generate` would write. Writes nothing.

### `codegen.render`

```python
def render(module: ModuleType | str, out: str | Path) -> dict[Path, str]
```

The files `generate` would write, and what each would hold.

### `codegen.collect`

```python
def collect(module: ModuleType | str) -> dict[str, CompiledScript[Any]]
```

Every script a module holds, by the name it is bound to, in definition order.
A script bound to two names is collected once, under the first.

### The command line

```bash
python -m redis_lua_py generate MODULE --out PATH [--check]
```

`generate` or, with `--check`, `check`, exiting 1 on an out-of-date output, a
module that cannot be imported, or a script that does not compile. Installing
the package also provides the same command as `redis-lua-py`.

## Errors and warnings

`CompileError`, `UnsupportedSyntax`, `ScriptArgumentError`, `StaleLuaError`,
`RedisLuaError`, `RedisLuaWarning` and `NilTruncationWarning` have
[their own page](errors.md).
