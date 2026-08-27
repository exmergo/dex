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

## Running a suite (live)

```
python -m evals skills/explore               # full suite: triggering + quality + uplift
python -m evals skills/explore --triggering  # triggering only (cheaper)
python -m evals skills/explore --json        # machine-readable report
```

The default backend drives Claude Code headless, so it needs the `claude` CLI and
a workspace with the dex plugin installed. The command exits non-zero unless the
suite passes (clean triggering and no regression versus baseline), so the same
invocation works locally and as a release gate.

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
- `tests/` covers the scoring core with fake backends (no model, free, in CI).
