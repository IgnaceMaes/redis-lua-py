# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries
are written by [release-please](https://github.com/googleapis/release-please)
from the conventional commit subjects on `main`; see
[CONTRIBUTING.md](CONTRIBUTING.md) for how a release is cut.

## [0.8.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.7.0...v0.8.0) (2026-09-13)


### ⚠ BREAKING CHANGES

* a bool parameter is a boolean in the body rather than "1" or "0", and integer conversion of a non-number raises instead of giving nil.

### Fixed

* give bool arguments, int() and known booleans their python meaning ([#24](https://github.com/IgnaceMaes/redis-lua-py/issues/24)) ([27b3de8](https://github.com/IgnaceMaes/redis-lua-py/commit/27b3de8e7c12aea9076b940e6a7f369b054876f3))

## [0.7.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.6.0...v0.7.0) (2026-09-12)


### ⚠ BREAKING CHANGES

* a + b where neither side is known to be a number or a string now joins two strings instead of adding them as numbers, as Python does, so wrap a side in int to add. Integer keys of a dict the compiler knows to be one are no longer shifted by one, and a lambda or helper function that reads a loop variable is refused.

### Added

* compile lambdas, chained comparisons and slice steps, and tell dicts from lists ([6024009](https://github.com/IgnaceMaes/redis-lua-py/commit/6024009eaefeb20ccb743225c609799aa332f7c2))
* support coredis clients ([#22](https://github.com/IgnaceMaes/redis-lua-py/issues/22)) ([3d07a89](https://github.com/IgnaceMaes/redis-lua-py/commit/3d07a89f5b4c55eddca3a1f3bae5f45b41fd4156))


### Changed

* split the compiler into a package of modules ([#20](https://github.com/IgnaceMaes/redis-lua-py/issues/20)) ([5f67766](https://github.com/IgnaceMaes/redis-lua-py/commit/5f67766f015c038086894f3b61fa76d6f9deef12))

## [0.6.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.5.1...v0.6.0) (2026-09-12)


### Added

* generate typed functions that call scripts the way `@script` does ([#18](https://github.com/IgnaceMaes/redis-lua-py/issues/18)) ([8f602f7](https://github.com/IgnaceMaes/redis-lua-py/commit/8f602f7bdf4afaa22c83c63e215ef5d965fbc584))

## [0.5.1](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.5.0...v0.5.1) (2026-09-12)


### Fixed

* load redis functions libraries on redis-py 4.2 ([#16](https://github.com/IgnaceMaes/redis-lua-py/issues/16)) ([85c2ba2](https://github.com/IgnaceMaes/redis-lua-py/commit/85c2ba20056224af7b2c9720176f799cf54b163f))

## [0.5.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.4.0...v0.5.0) (2026-09-12)


### Added

* compile continue, try/except/else/finally, raise and assert ([#11](https://github.com/IgnaceMaes/redis-lua-py/issues/11)) ([aeca575](https://github.com/IgnaceMaes/redis-lua-py/commit/aeca5757fffe42e88b49b18fa4d3fe91897fad2e))
* compile membership, slicing, string methods and formatting, and key variables ([#13](https://github.com/IgnaceMaes/redis-lua-py/issues/13)) ([4880c81](https://github.com/IgnaceMaes/redis-lua-py/commit/4880c8156d849755259dea9a8c29cf69392ecd7e))
* compile redis functions libraries, and script flags ([#15](https://github.com/IgnaceMaes/redis-lua-py/issues/15)) ([5980f03](https://github.com/IgnaceMaes/redis-lua-py/commit/5980f0317acbe9a7a96ffaff029f2d76143b7e32))
* generate lua ahead of time for code that ships without redis-lua-py ([#14](https://github.com/IgnaceMaes/redis-lua-py/issues/14)) ([83d6b9d](https://github.com/IgnaceMaes/redis-lua-py/commit/83d6b9da0b540cefc0be9ec51fbead54c4d0fade))

## [0.4.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.3.0...v0.4.0) (2026-09-12)


### Added

* compile variable keys, splat calls, and/or values, helper functions and dict loops ([#10](https://github.com/IgnaceMaes/redis-lua-py/issues/10)) ([271f3f6](https://github.com/IgnaceMaes/redis-lua-py/commit/271f3f6be23d78246d91cde5de4fec05cee28832))


### Fixed

* stop silently mistranslating reassigned parameters, infinity, == None and set_repl ([#8](https://github.com/IgnaceMaes/redis-lua-py/issues/8)) ([930c382](https://github.com/IgnaceMaes/redis-lua-py/commit/930c38283f366fd9042c4cc20cdf7eeb34e2d8a5))

## [0.3.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.2.1...v0.3.0) (2026-09-12)


### Added

* support python 3.10 and redis-py 4.2 ([#6](https://github.com/IgnaceMaes/redis-lua-py/issues/6)) ([6ce6249](https://github.com/IgnaceMaes/redis-lua-py/commit/6ce6249d219756a598e83f18f3bf44e8a58722eb))

## [0.2.1](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.2.0...v0.2.1) (2026-09-12)


### Documentation

* add a documentation site built with zensical ([#4](https://github.com/IgnaceMaes/redis-lua-py/issues/4)) ([f877f97](https://github.com/IgnaceMaes/redis-lua-py/commit/f877f97c0b7c8126ab62ef2324aff99106434ee4))

## [0.2.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.1.0...v0.2.0) (2026-09-12)

Everything raised by the first adoption report of 0.1.0
([#2](https://github.com/IgnaceMaes/redis-lua-py/pull/2),
[0ef4ed2](https://github.com/IgnaceMaes/redis-lua-py/commit/0ef4ed23dd3cef8616ab5132d7a9089e38bb518a)).

### ⚠ BREAKING CHANGES

- A command name Redis does not have is now refused at import rather than
  compiled, and a literal `None` inside a returned table is refused. Both
  previously compiled to Lua that failed, or silently truncated, at runtime.
- The generated header's source path is now relative to the project root,
  which changes the SHA of every script — once.

### Added

- Command names are checked at compile time against a table generated from the
  Redis source (`scripts/generate_commands.py`, tracking Redis 8.10), so a name
  Redis does not have is refused with a caret and a suggestion rather than
  raising the first time its branch runs.
- The redis-py spellings that name exactly one command are translated instead
  of refused: `redis.delete(k)` compiles to `redis.call('DEL', k)`.
- Container commands are checked down to the subcommand, and a hyphenated one
  is reached through its underscores: `redis.client_no_evict("on")` compiles to
  `redis.call('CLIENT', 'NO-EVICT', 'on')`.
- Module-level `int`, `float`, `str`, `bytes` and `bool` constants are folded
  into a script body, including through a dotted name such as an `IntEnum`
  member or a settings attribute. A body no longer has to repeat a number its
  own module already names.
- The return annotation is carried to the caller: a script is a
  `CompiledScript[R]`, `bind()` gives a `BoundScript`, and an async client
  yields `Awaitable[R]` rather than `Any`.
- `@script(header=False)` drops the provenance comment, for anyone who wants
  the script body and nothing else.
- `NilTruncationWarning` (and `RedisLuaWarning`) are raised when a name that is
  not assigned on every path is returned inside a table, where it would
  truncate the reply.

### Fixed

- The generated header recorded an absolute source path. Since the header is
  part of the body, and the body is what `EVALSHA` hashes, the same script had
  a different SHA on a laptop, in CI and in a container, and put build-machine
  paths on the Redis server. The path is now relative to the project root.
- A `bytes` literal in a body was decoded with `surrogateescape`, which could
  not survive the script being sent to Redis as text. Bytes literals are now
  emitted as numeric escapes, so they arrive exactly.
- A NUL in a Lua string literal was written `\0`, which Lua reads together
  with a following digit as a different byte. It is now `\000`.
- `redis.sort_ro(...)` and the other `_RO` variants split into two tokens,
  making the `RO` a stray argument. Their underscore is part of the wire name
  and is now kept.

### Documentation

- Binary values through `ARGV` are now a stated guarantee with a test that
  round-trips non-UTF-8 bytes through `ARGV`, a stored value and a returned
  `GETRANGE`.
- A returned table is truncated at the first genuine `nil` -- but a command
  with nothing to return hands Lua `false`, which becomes a null *element* and
  does not truncate. Both are written down, in "Where Lua differs from Python".
- "Testing your scripts": snapshot `.lua` in a golden test, and run behaviour
  against `fakeredis[lua]` without a server.
- Scripts belong at module level, where they compile once at import.
- `float` round-trips through Lua's `%.14g` number formatting; `bool` encodes
  to `"1"` / `"0"`; `bytes` is a passthrough.

## 0.1.0 (2026-09-12)

First release.

### Added

- `@script`, which compiles a Python function to Redis Lua at import time.
- `Key` annotation marking a parameter as `KEYS` rather than `ARGV`, so that
  Redis Cluster can route the script correctly.
- `int` and `float` annotations wrap their argument in `tonumber`, since `ARGV`
  always arrives as a string.
- One script object drives both sync and async redis-py clients, deferring to
  redis-py for `EVALSHA`, script caching and the `NOSCRIPT` reload.
- `redis.*` / `call.*` command calls, with underscores splitting into
  subcommand tokens, plus `cjson.encode` / `cjson.decode`.
- The generated Lua is exposed on `.lua` for review and golden testing.
- `UnsupportedSyntax` reports the file, line and column of anything outside the
  supported subset, with a hint for what to write instead.
- The command namespace is resolved by value rather than by name, so it works
  under any alias (`from redis_lua_py import redis as r`). A receiver that
  turns out to be redis-py itself is refused with an explanation, instead of
  being compiled against the client library.
- `script.bind(client)` returns a callable with the client attached, for code
  that would otherwise repeat it at every call site.

### Semantics closed between Python and Lua

- Truthiness: `0`, `''` and empty tables are falsy, as in Python.
- Missing values: a Redis command returning nothing gives Lua `false`, not
  `nil`, so `is None` accepts both and evaluates its operand once.
- Indexing: Python's 0-based indices are translated to Lua's 1-based tables.
- Scope: a name first assigned inside a block is hoisted, because Lua's `local`
  is block-scoped where Python's assignment is function-scoped.
