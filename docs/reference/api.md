# API reference

Everything in `redis_lua_py.__all__`. The package is small on purpose: one
decorator, a library for Redis Functions, one annotation, two namespaces, and
the errors.

```python
from redis_lua_py import Key, Library, call, cjson, redis, script
from redis_lua_py import BoundScript, CompiledScript, LibraryFunction
```

## `script`

```python
def script(
    func: Callable[..., R] | None = None,
    /,
    *,
    name: str | None = None,
    header: bool = True,
    flags: Iterable[str] = (),
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
| `flags` | script flags for Redis 7, such as `no-writes`, written on a `#!lua` first line; see [Redis Functions](../guide/redis-functions.md#flags) |

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

Type checkers see a method per command, documented from Redis' own command
definitions, so an editor shows each command's syntax on hover. See
[In your editor](../guide/calling-redis-commands.md#in-your-editor).

## `call`

An alias of [`redis`](#redis), for modules that would rather not rename
anything.

## `cjson`

The JSON library Redis exposes to scripts: `cjson.encode` and `cjson.decode`,
which pass through under their own names.
`cjson.null` is JSON `null`, which `None` is not: a field set to `None` is
dropped from an encoded object.

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
| `param_annotations` | `tuple[str \| None, ...]` | each parameter's annotation as written, in `params` order |
| `return_annotation` | `str \| None` | the return annotation as written |
| `keyword_only` | `tuple[str, ...]` | the parameters declared after a bare `*` |
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
wraps, and leaves that script usable against any other client. A
[`LibraryFunction`](#libraryfunction) binds the same way.

## `Library`

```python
class Library(name: str)
```

A Redis Functions library. `name` is the library name Redis registers, and
takes letters, digits and underscores. See
[Redis Functions](../guide/redis-functions.md).

### `function`

```python
def function(
    self,
    func: Callable[..., R] | None = None,
    /,
    *,
    name: str | None = None,
    flags: Iterable[str] = (),
) -> LibraryFunction[R] | Callable[[Callable[..., R]], LibraryFunction[R]]
```

Compile a function into the library, under the same rules as
[`script`](#script). Usable bare or called.

| Parameter | Meaning |
| --- | --- |
| `name` | the function name Redis registers, instead of the Python name |
| `flags` | function flags, such as `no-writes`; a `no-writes` function is called with `FCALL_RO` |

### `load`

```python
def load(self, client) -> Any
```

Load the library with `FUNCTION LOAD REPLACE`. Calling a function loads the
library when the server lacks it, so this is only needed before queueing calls
in a pipeline, or to load at deploy time.

| Attribute | Type | What it is |
| --- | --- | --- |
| `name` | `str` | the library name |
| `lua` | `str` | the library source, exactly as `FUNCTION LOAD` receives it |
| `functions` | `tuple[LibraryFunction, ...]` | the functions, in the order they were added |

## `LibraryFunction`

```python
class LibraryFunction(Generic[R])
```

What `Library.function` returns. Calling it sends `FCALL`, or `FCALL_RO` for a
`no-writes` function, and loads the library first if the server does not have
it.

```python
function(client, /, *positional, **keyword) -> R              # sync client
function(client, /, *positional, **keyword) -> Awaitable[R]   # async client
```

It has the same `name`, `params`, `keys`, `args`, `variadic_key`,
`variadic_arg`, `doc` and `source` as a [`CompiledScript`](#compiledscript),
and a `bind` that works the same way, plus:

| Attribute | Type | What it is |
| --- | --- | --- |
| `library` | `Library` | the library the function belongs to |
| `lua` | `str` | the source of that whole library |
| `flags` | `tuple[str, ...]` | the function's flags |
| `read_only` | `bool` | whether it is flagged `no-writes`, and so called with `FCALL_RO` |

## `codegen`

```python
from redis_lua_py import codegen
```

Compile scripts to Lua ahead of time, for code that should not depend on this
package at runtime. See
[Shipping without the dependency](../guide/build-time.md). Every function takes
the module as a module object or a dotted name, and `out` as a path:

- ending in `.py`, for one module importing only the standard library, with a
  typed function per script, called the way the `@script` is, and its Lua as a
  string constant
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
