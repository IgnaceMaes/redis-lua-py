# Contributing

## Development

```sh
uv sync --dev
uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy
```

See the [README](README.md#development) for running the suite against a live
Redis.

`src/redis_lua_py/_commands.py` is generated, not written. It is the table
command names are checked against, and it comes from the command definitions
in the Redis source. The same run writes `src/redis_lua_py/_command_stubs.py`,
the typed method per command that editors show on hover. Refresh both when a
Redis release adds commands, and commit the result:

```sh
uv run python scripts/generate_commands.py 8.10.1
```

A stale table can never block a caller: `redis.call('NEW.CMD', ...)` is
deliberately never checked.

## Commit messages

Pull requests are squash-merged, so **the PR title becomes the commit subject
on `main`**, and that subject is the only thing the release tooling reads. It
must follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: compile `pcall` into a protected call
fix: hoist names assigned in both arms of an if
docs: document the Key annotation
```

| Prefix                                    | Changelog section | Version bump |
| ----------------------------------------- | ----------------- | ------------ |
| `feat:`                                    | Added             | minor        |
| `fix:`                                     | Fixed             | patch        |
| `perf:`                                    | Performance       | patch        |
| `refactor:`                                | Changed           | patch        |
| `deps:`                                    | Dependencies      | patch        |
| `docs:`                                    | Documentation     | patch        |
| `chore:` `test:` `ci:` `build:` `style:`   | omitted           | none         |

A `!` after the prefix (`feat!:`) or a `BREAKING CHANGE:` paragraph in the PR
body marks a breaking change. While the version is below 1.0 that bumps the
minor, not the major — `0.1.0` becomes `0.2.0`, never `1.0.0`. Promoting the
project to 1.0 is a deliberate act: set `"release-as": "1.0.0"` once in
`release-please-config.json`, or land a commit with a
`Release-As: 1.0.0` footer.

The subject is copied verbatim into the changelog, so write it as a lowercase
phrase with no trailing period. `.github/workflows/pr-title.yml` enforces this
on every pull request.

## Releasing

Releases are cut by merging a pull request; nobody edits a version by hand.

1. Land your PR on `main`.
2. `.github/workflows/release.yml` runs
   [release-please](https://github.com/googleapis/release-please), which keeps
   **one open PR** titled something like `chore(main): release 0.2.0`. Each
   merge to `main` refreshes it. The PR bumps `version` in `pyproject.toml`,
   `__version__` in `src/redis_lua_py/__init__.py`, and prepends the new
   section to `CHANGELOG.md`. Review it like any other PR — the changelog body
   is editable before you merge.
3. Merge the release PR. That tags `vX.Y.Z`, publishes a GitHub release, and
   the same workflow then builds the sdist and wheel and uploads them to PyPI
   via [trusted publishing](https://docs.pypi.org/trusted-publishers/) — no API
   token is stored anywhere.

`.release-please-manifest.json` records the last released version and is
updated by the release PR. Don't edit it by hand.
