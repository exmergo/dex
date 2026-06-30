# Contributing to dex

Thanks for contributing. This guide covers local setup and the checks every
pull request must pass.

## Development setup

The engine lives in `packages/dex-core/` and is managed with
[uv](https://docs.astral.sh/uv/). From that directory, sync the runtime plus the
DuckDB on-ramp and the dev tools:

```
cd packages/dex-core
uv sync --extra duckdb --extra dev
```

Run the test suite with:

```
uv run pytest
```

## Agent evals (`evals/`)

The Tier-2 agent-eval harness lives at the repo root in `evals/`, separate from
the engine: it drives a concrete agent (Claude today) to test the skills, so it
sits with the skills it tests, not in the published `exmergo-dex-core` wheel.

It is **stdlib only by design**: no `pyproject.toml`, no `uv.lock`, no
third-party runtime dependency. Run its deterministic core tests with:

```
uvx pytest evals
```

Run a skill's live suite (needs the `claude` CLI on PATH and the plugin
installed) with `python -m evals skills/<skill>`. If a future backend needs a
real Python dependency, promote `evals/` to its own uv project at that point and
not before. See `evals/README.md` for the rationale and full usage.

## Linting and formatting (Ruff)

We use [Ruff](https://docs.astral.sh/ruff/) as both the linter and the
formatter. A single `ruff.toml` at the repo root governs the whole tree
(`packages/`, `skills/`, and `scripts/`), so run Ruff from the repo root:

```
uvx ruff check .          # lint
uvx ruff check --fix .    # lint and auto-fix
uvx ruff format .         # format in place
```

### Set up the pre-commit hook

The fastest way to stay green is to let Ruff run automatically before each
commit. Install [pre-commit](https://pre-commit.com/) once, then enable the
hook in your clone:

```
uv tool install pre-commit   # or: pipx install pre-commit
pre-commit install
```

From then on, `ruff check --fix` and `ruff format` run on your staged files at
commit time. To check every file on demand:

```
pre-commit run --all-files
```

## The linter must pass before a PR can merge

Every push to `main` and every pull request into `main` runs the **Lint**
workflow (`.github/workflows/lint.yml`), which fails the build if
`ruff check` reports any issue or if `ruff format --check` finds unformatted
code. Open PRs cannot merge until this check is green, alongside the existing
CI (tests, safety spine, and the em-dash prose check). Run Ruff or the
pre-commit hook locally before you push so the gate passes on the first try.

## Prose is em-dash free

All shipped prose in this repo avoids em dashes (an Exmergo brand rule),
enforced in CI. Before committing Markdown or text, you can check it locally:

```
python3 scripts/check_no_em_dashes.py path/to/file.md
```

## Keeping the Ruff version in sync

The pinned Ruff version appears in three places that must move together when you
bump it:

- `.github/workflows/lint.yml` (the `uvx ruff@<version>` calls)
- `.pre-commit-config.yaml` (the `rev:` tag)
- `packages/dex-core/pyproject.toml` (the `ruff==<version>` pin in the `dev` extra)
