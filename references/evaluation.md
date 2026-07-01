# Evaluation: the three-tier pyramid

dex is built eval-driven: a change does not ship unless its evals pass and it
still beats the no-skill baseline. Evaluation is three things at once: a
quality moat, a safety mechanism, and a marketing asset (published benchmark
scores).

```
TIER 3  BENCHMARKS (external, published)   few, expensive, periodic
        ADE-bench (home) - Spider 2.0      -> marketing + north-star
TIER 2  AGENT EVALS (skill-creator)        per-skill, LLM-in-loop, CI-gated
        triggering - output-quality        -> does the skill help?
TIER 1  UNIT TESTS (dex-core, pytest)      many, deterministic, fast
        engine correctness + SAFETY        -> is the engine correct?
```

Two through-lines run up all three tiers: **uplift over baseline** (is the agent
plus dex better than the agent alone?) and **cost-efficiency** (same accuracy at a
fraction of the warehouse spend and turns).

## Tier 1: unit tests (`packages/dex-core/tests/`)

Deterministic, fast, pytest, free on DuckDB. Engine correctness plus the five
safety-critical assertion families, which are release blockers regardless of any
benchmark score:

1. Read-only against data; SELECT-only generation; prod-target execution refused.
2. Cost-guard binds per paradigm.
3. PII flagged as (column, category, confidence), never surfaced.
4. Propose-don't-impose: changes are diffs, hand-written dbt never silently
   overwritten.
5. Sanitized envelope: credentials and raw rows never appear in stdout `data`.

The spine lives in `tests/test_safety_spine.py`. Families whose engine lands in a
later phase are wired as explicit `xfail` placeholders so the spine is complete
from Phase 0 and turns green as the logic arrives.

## Tier 2: agent evals (skill-creator framework)

Per skill, `skills/<skill>/evals/evals.json`. Three concerns: triggering
(positive and must-not-trigger siblings, description-improver tuned), output
quality (the hard constraints as executable assertions), and uplift versus
baseline. Three skills share a description budget, so negative cases are
first-class.

## Tier 3: external benchmarks (published)

Scheduled and cost-capped, not per-commit. **ADE-bench** is the home benchmark
(no official leaderboard, so publish attributed numbers; semantic-model
maintenance is a confirmed gap dex contributes into). **Spider 2.0** is the
academic north-star, led by **Spider2.0-DBT** (the still-hard, AE-aligned track;
the old 17-36% headline is retired). No Spider validation is claimed for Postgres
or Databricks (no coverage). Runbooks live in `benchmarks/`.

## CI gating

Tier 1 always gates (fast, free). Tier 2 gates on release (a safety regression
blocks regardless of pass-rate). Tier 3 runs on a schedule and pre-release under a
cost ceiling; a pass-rate or cost/turn regression blocks a release.
