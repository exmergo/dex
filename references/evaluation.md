# Evaluation: the three-tier pyramid

dex is built eval-driven: a change does not ship unless its evals pass and it
still beats the no-skill baseline. Evaluation is three things at once: a
quality moat, a safety mechanism, and a marketing asset (published benchmark
scores).

```
TIER 3  BENCHMARKS (external, published)   few, expensive, periodic
        ADE-bench, data-eng-bench          -> marketing + north-star
TIER 2  AGENT EVALS (skill-creator)        per-skill, LLM-in-loop, CI-gated
        triggering - output-quality        -> does the skill help?
TIER 1  UNIT TESTS (dex-core, pytest)      many, deterministic, fast
        engine correctness + SAFETY        -> is the engine correct?
```

Two through-lines run up all three tiers: **uplift over baseline** (is the agent
plus dex better than the agent alone?) and **cost-efficiency** (same accuracy at a
fraction of the warehouse spend and turns).

## Tier 1: unit tests (`packages/dex-core/tests/`)

Deterministic, fast, pytest, free on DuckDB. DexEngine correctness plus the five
safety-critical assertion families, which are release blockers regardless of any
benchmark score:

1. Read-only against data; SELECT-only generation; prod-target execution refused.
2. Cost-guard binds per paradigm.
3. PII flagged as (column, category, confidence), never surfaced.
4. Propose-don't-impose: changes are diffs, hand-written files never silently
   overwritten.
5. Sanitized envelope: credentials never appear in stdout `data`, and data
   values only via `explore query`'s firewall-cleared row-major results.

The spine lives in `tests/test_safety_spine.py`. A family whose engine has not
landed yet is wired as an explicit `xfail` placeholder, so the spine is complete
before the logic is and turns green as it arrives.

Two kinds of tier-1 asset sit beside the ordinary unit tests. **Conformance
suites** are shipped rather than internal, under the `[storage-conformance]`,
`[project-conformance]` and `[semantic-conformance]` extras, so an implementation
written outside this repository is held to the same assertions the shipped ones
are. And a **reviewed fixture corpus** pins behavior a test alone would not: the
native Ossie corpus under `tests/ossie/fixtures/` pairs each document with the
verdict it must produce, so a schema upgrade is a diff of verdicts somebody read
rather than a hash somebody bumped, and a test refuses a documented claim whose
case does not exist.

## Tier 2: agent evals (skill-creator framework)

Per skill, `skills/<skill>/evals/evals.json`. Three concerns: triggering
(positive and must-not-trigger siblings, description-improver tuned), output
quality (the hard constraints as executable assertions), and uplift versus
baseline. Three skills share a description budget, so negative cases are
first-class.

## Tier 3: external benchmarks (published)

Scheduled and cost-capped, not per-commit. Two are published, each with its raw
per-task results committed. **ADE-bench** is the home benchmark (no official
leaderboard, so publish attributed numbers; semantic-model maintenance is a
confirmed gap dex contributes into). **data-eng-bench** is the larger of the two,
a 2,356-model dbt project scored by a hidden pytest suite after a cold `dbt run`.
Both publish how often dex actually fired beside the accuracy, because a score
with the tool firing on 98% of trials and the same score with it firing on 20%
are different claims. **Spider 2.0**, led by **Spider2.0-DBT**, remains the
academic north-star and has no harness here yet. Runbooks live in `benchmarks/`.

## CI gating

Tier 1 always gates (fast, free). Tier 2 gates on release (a safety regression
blocks regardless of pass-rate). Tier 3 runs on a schedule and pre-release under a
cost ceiling; a pass-rate or cost/turn regression blocks a release.

Two jobs are deliberately **advisory** rather than blocking, because both track a
moving upstream and a failure means the world changed rather than that this
change broke something. `sqlglot-canary` runs the guards and the safety spine
against the newest sqlglot, above the declared ceiling, so raising that ceiling is
a decision taken on evidence. `mermaid-syntax` renders `explore diagram` output
and parses it with the real Mermaid package from npm.

The second one is worth explaining, because it is the one place CI reaches outside
Python. dex emits Mermaid text and never renders it, so no declared dependency
pins the syntax dialect the renderer has to stay compatible with: that contract is
with a parser living in whatever tool a reader opens the diagram in. Holding it
means running the genuine parser, which is a Node package. Depending on a Python
mermaid wrapper instead would not have helped: the string builders replace none of
the renderer's actual work (the cardinality derivation and the key and PII marks),
the image renderers post the diagram to a third-party host, which is disqualifying
for a tool whose premise is that your data does not leave, and the one parser
binding needs Node installed anyway. So the dependency stays in CI, where it
belongs, and the fixture corpus is generated from the renderer rather than
committed so it cannot drift from what dex actually emits.
