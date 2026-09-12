# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries
are written by [release-please](https://github.com/googleapis/release-please)
from the conventional commit subjects on `main`; see
[CONTRIBUTING.md](CONTRIBUTING.md) for how a release is cut.

## [0.2.0](https://github.com/IgnaceMaes/redis-lua-py/compare/v0.1.0...v0.2.0) (2026-09-12)


### ⚠ BREAKING CHANGES

* a command name Redis does not have is now refused at import rather than compiled, and a literal `None` inside a returned table is refused. Both previously compiled to Lua that failed, or silently truncated, at runtime. The header's source path is now repo-relative, which changes the SHA of every script — once.

### Added

* check command names, fold module constants, and type the caller's side ([#2](https://github.com/IgnaceMaes/redis-lua-py/issues/2)) ([0ef4ed2](https://github.com/IgnaceMaes/redis-lua-py/commit/0ef4ed23dd3cef8616ab5132d7a9089e38bb518a))

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
