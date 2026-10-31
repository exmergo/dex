# Tier-2 agent evals

This is the harness that runs the per-skill agent evals (Tier 2 of the evaluation
pyramid in `references/evaluation.md`). It answers two questions for a skill: does
it **trigger** on the right intent and stay quiet on its siblings, and does the
agent **plus the skill** beat the agent alone (uplift over baseline)?

The suites themselves live with the skills, at `skills/<skill>/evals/evals.json`.
This directory is only the runner.

## Why this lives here and not in the engine

`exmergo-dex-core` (under `packages/dex-core/`) is the portable, agent-agnostic
runtime engine that ships to PyPI. This harness is a development and CI tool that
drives a concrete agent (Claude today, others later) to test the skills, so it
deliberately lives at repo level next to the skills it tests and the Tier-3
`benchmarks/` harness. Keeping it out of the engine keeps the published wheel lean
and keeps the engine free of any dependency on a specific agent.

## Dependencies: none (by design)

This harness is **stdlib only**. The models are plain dataclasses and JSON output
is hand-built, so there is no `pyproject.toml` and no `uv.lock` here. Run the
deterministic core tests with:

```
uvx pytest evals
```

The only external thing the live backend needs is the `claude` CLI on PATH (not a
Python package). When a future non-Claude backend needs a real Python SDK
dependency (planned around the multi-agent portability work), this directory
should be promoted to its own uv project at that point, not before. Until then,
keep it dependency-free.

The metadata-quality eval (`evals/metadata/`, below) is the one exception: it
builds dbt projects on DuckDB, so it runs in the engine's own environment, whose
`[duckdb]` extra already pins DuckDB, PyYAML and dbt, and imports them only when
run. Nothing else here imports it.

## Running a suite (live)

```
python -m evals skills/explore               # full suite: triggering + quality + uplift
python -m evals skills/explore --triggering  # triggering only (cheaper)
python -m evals skills/explore --json        # machine-readable report
python -m evals skills/explore --case 7 --case 8  # only those quality cases
```

The default backend drives Claude Code headless, so it needs the `claude` CLI. The
command exits non-zero unless the suite passes (clean triggering and no regression
versus baseline), so the same invocation works locally and as a release gate.

**Which plugin a run evaluates.** The skill arm loads the dex plugin from
`--plugin-dir`, which defaults to this checkout, so a branch is evaluated with its
own skill text. Every run prints the path on stderr. Pass `--plugin-dir <path>` to
evaluate another copy. The baseline arm of the uplift check passes no plugin flag,
so it still sees whatever dex plugin the machine has installed. `--corpus` runs
use `--plugin-dir` too.

**Running a few cases.** `--case ID` (repeatable) runs only those output-quality
cases, with the skill on, and skips triggering and the uplift baseline. The ids
are the `id` fields in the suite's `evals.json`; an unknown id is an error. It
exits non-zero unless every selected case passes.

**Running from inside a Claude Code session.** A session exports `CLAUDECODE`,
`CLAUDE_CODE_SESSION_ID` and other `CLAUDE*` variables that describe it, and a
`claude -p` child that inherits them has answered as part of the parent session.
The backend therefore starts each child without any `CLAUDE*` variable except
the ones it needs to log in: `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`, and
`CLAUDE_CODE_USE_*`/`CLAUDE_CODE_SKIP_*` (provider selection). `ANTHROPIC_*`
variables pass through unchanged. Running from inside a session needs nothing
extra.

## The cross-skill triggering corpus

The per-skill `positive`/`negative` lists above have a structural blind spot: they
are written by the same person who wrote the description they test, at the same
time, so a positive is often just a paraphrase of the description, and each skill
is only ever checked with the *other* skills disabled. Neither failure mode is
visible from inside one skill's own suite.

`evals/corpus/` holds externally authored fixtures for exactly this: real prompts
from someone with no knowledge of these descriptions, run with every skill
available at once, each hand-labeled with the skill (or `none`) it should fire.
`evals/corpus/ade_bench_triggering.json` is the first one, sourced from
[dbt-labs/ade-bench](https://github.com/dbt-labs/ade-bench) (Apache-2.0); see the
file's own `source` field for the exact provenance and what was and was not
carried over.

```
python -m evals --corpus evals/corpus/ade_bench_triggering.json
python -m evals --corpus evals/corpus/ade_bench_triggering.json --json
```

One live call per prompt (not per prompt-per-skill, since every skill is available
in the same call), and the report is per-skill precision/recall plus which cases
missed. This mode always exits 0: it is a measurement, not a release gate. Expect
the first run's numbers to be low and record them as the baseline (there is no
committed baseline yet; run it and note what you get in the PR, and treat later
runs as measuring drift from that number rather than pass/fail).

## Layout

- `suite.py` loads and validates a skill's `evals.json`, and a cross-skill
  `Corpus` from `evals/corpus/*.json` (stdlib dataclasses both ways).
- `runner.py` is the deterministic scoring core: triggering, output quality,
  uplift, and the corpus's per-skill precision/recall. It takes an agent, a judge,
  or a classifier by dependency injection.
- `claude_agent.py` is the live backend: the `AgentRunner`/`Judge`/`Classifier`
  protocols driven by the `claude` CLI. A non-Claude agent is a second backend
  behind the same protocols, with no change to the core.
- `__main__.py` is the CLI.
- `tests/` covers the scoring core with fake backends, and the CLI and live
  backend with `subprocess.run` patched out (no model, free, in CI).
- `metadata/` is the metadata-quality eval (#492), described below. It is the
  one part of `evals/` with dependencies, and it imports them only when run.

## The metadata-quality eval (`evals/metadata/`)

Neither benchmark Dex runs against grades metadata: they compare the data a model
returns, never its description, its `meta` or its semantic definitions. This
suite scores the declarations an agent writes (`transform plan`'s `declarations`
payload, rendered into the model's YAML), deterministically, on small DuckDB
fixture projects built so each declaration has a known right answer.

### The fixtures

Five fixture projects, written for this suite and derived from no benchmark task,
with seven tasks phrased the way users ask:

| Fixture | Task | What it holds the agent to |
|---|---|---|
| `revenue_population` | build `daily_revenue` | the population every sibling model applies (cancelled and returned orders excluded), with `precedent` as the evidence |
| `season_to_date` | build `fct_team_week` | to-date columns declared `semi_additive`, aggregated with `max` |
| `season_to_date` | fix `team_season_record` ("numbers far too high") | `max` of a running total, every team kept for an unfinished season |
| `share` | build `product_region_share` | a share declared `unit: fraction`, `non_additive`, summing to 1 within a region |
| `composite_grain` | build `store_daily_stock` | a two-column grain, a stock balance that is `semi_additive`, a distinct count that is `non_additive` |
| `parent_zero_children` | build `account_ticket_summary` | driven from accounts with tickets left-joined, so every account survives with a count of zero and orphan tickets stay out |
| `parent_zero_children` | fix the same model ("accounts are missing") | the same, starting from a model driven from the child table |

Each fixture is `fixture.json`, `warehouse.py` (seeded, pinned by a digest in
`tests/test_metadata.py`), the starting dbt `project/`, and per task a
`task.json`, a `truth.json`, an optional `setup/` overlay (a fix task's broken
starting state) and a `reference/` overlay (a correct answer).

### What each number means

- **Build gate** (pass or fail, per task): a cold `dbt parse` and `dbt build` of
  the result, in a fresh copy with no `target/`, on a freshly generated warehouse,
  and no model with two YAML entries. A failure zeroes the task, because a
  declaration that breaks the parse costs more than every declaration it adds. It
  is not a threshold: the run exits 1 when any task fails it.
- **Completeness** (0 to 1, per task): the share of required fields present on
  each model the task asks for: a description, a grain, a population, a role for
  every column the built relation has, aggregation, additivity and unit for every
  measure, and at least one assumption with an evidence tier.
- **Correctness** (0 to 1, per task): the share of fields with a known answer that
  match it: grain (as a set), population (read through the column's domain, so
  `includes` and `excludes` that mean the same thing score the same), role,
  aggregation, additivity, unit, and the evidence tier the convention calls for.
- **Checks** (0 to 1, per task): SQL over the built relation, such as "every
  account is present" or "shares sum to 1". They catch a right declaration on
  wrong SQL.
- A task where the agent touched none of the models it was asked for scores 0 on
  every axis (`no_models`), never a vacuous 1.

The scorer reads the YAML with PyYAML rather than with the engine's reader, so a
bug in how Dex writes declarations lowers the score instead of being shared by
the thing measured and the measure. Description prose is reported, never scored
by a comparison; no LLM judge is needed for any field.

### Running it

It needs DuckDB, PyYAML and dbt, which the engine's `[duckdb]` extra pins, so it
runs in the engine's environment (and the build gate uses the dbt `transform
build` uses):

```bash
# Free: score every reference answer. This is the scorer's own self-test.
uv run --project packages/dex-core --extra duckdb python -m evals metadata --reference

# The scorer and fixture tests (also in CI):
uv run --project packages/dex-core --extra duckdb --with pytest pytest evals/tests/test_metadata.py

# Paid: one headless Claude Code session per task, then score.
uv run --project packages/dex-core --extra duckdb python -m evals metadata \
    --model claude-sonnet-5-5 --out <dir>
```

A live run gives each agent exactly the block `dex procedure --block` returns as
the workspace's `CLAUDE.md`, the working tree's plugin (`--plugin-dir`), and the
candidate engine's `dex` and `dbt` on PATH. It loads only the project's settings,
so a dex plugin installed on the machine cannot stand in for the candidate, and
it records which plugins actually loaded, the commit, the engine version, the
procedure's digest and the Claude Code version in `report.json`'s `provenance`.
Each task is capped by `--max-budget-usd` (default 5) and `--max-turns`
(default 50). `--fixture` and `--task` narrow the run; `--score-only <workspace>
--task <fixture/task>` re-scores a workspace a run left behind.

The release threshold for completeness and correctness is set from the first
baseline run, not before; the build gate is 100% or the run fails.

### Baseline

First live runs, 2026-10-31, `claude-sonnet-5-5`, Claude Code 2.1.316, one
attempt per task, on the #502/#492 working tree (procedure digest in each
`report.json`):

| Run | Build gate | Completeness | Correctness | Checks | Cost |
|---|---|---|---|---|---|
| Procedure without the declaration vocabulary | 100% | 1.00 | 0.84 | 1.00 | $0.74 |
| Procedure with it (as shipped) | 100% | 1.00 | 0.98 | 1.00 | $0.74 |

Every miss in the first run was vocabulary: units such as `count:games` or
`ratio` for `count` and `fraction`, and `aggregation: count` on a count that
rolls up with `sum`. An agent reading only `CLAUDE.md` had never seen the values,
so the procedure now lists them. The one miss left is a store's stock balance
declared `additive` rather than `semi_additive`. Seven tasks at one attempt each
size a regression check; they do not measure a difference between two runs.

Proposed release threshold, to confirm before the #503 candidate run: build gate
100% (not negotiable), completeness at least 0.95, correctness at least 0.90.
