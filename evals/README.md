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

## Layout

- `suite.py` loads and validates a skill's `evals.json` (stdlib dataclasses).
- `runner.py` is the deterministic scoring core: triggering, output quality, and
  uplift. It takes an agent and a judge by dependency injection.
- `claude_agent.py` is the live backend: the `AgentRunner` and `Judge` driven by
  the `claude` CLI. A non-Claude agent is a second backend behind the same two
  protocols, with no change to the core.
- `__main__.py` is the CLI.
- `tests/` covers the scoring core with fake backends (no model, free, in CI).
