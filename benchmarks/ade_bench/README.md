# ADE-bench: 80% with Dex + Claude Sonnet 5.5

**Dex solves 60 of 75 tasks, compared with 55 without Dex using the same Claude
Sonnet 5.5 model: +6.7 percentage points and 25% fewer failures.** Net task passes
improve across six of seven projects.

[ADE-bench](https://github.com/dbt-labs/ade-bench) is dbt Labs' analytics-engineering
benchmark. These runs evaluate 75 tasks on DuckDB and dbt, including repairing
models, building new ones, refactoring projects and testing their logic. A task
passes when its benchmark grading checks pass, not merely when the project builds.

## Same model, with and without Dex

| Configuration | Resolved | Accuracy | Failed | Reported usage cost |
|---|---:|---:|---:|---:|
| **Dex 1.17.4 + Claude Sonnet 5.5** | **60 / 75** | **80.0%** | **15** | $11.91 |
| Claude Sonnet 5.5 without Dex | 55 / 75 | 73.3% | 20 | $4.95 |

All 75 task IDs and user prompts match. Each configuration completed **one scored
attempt per task**. Nine baseline failures become passes with Dex, four move the
other way, and 51 pass in both: **five additional tasks solved overall**.

- [Dex run: per-task results](experiments/exmergo-sonnet55-dex1174-default-guided/results.json), [configuration](experiments/exmergo-sonnet55-dex1174-default-guided/manifest.json), [completion summary](experiments/exmergo-sonnet55-dex1174-default-guided/summary.json).
- [No-Dex run: per-task results](experiments/2026-10-01__11-35-05__no-dex-sonnet5.5/results.json), [configuration](experiments/2026-10-01__11-35-05__no-dex-sonnet5.5/run_metadata.json), [TSV](experiments/2026-10-01__11-35-05__no-dex-sonnet5.5/results.tsv).

## What improves

- **Preserve valid identifiers while fixing a build.** On
  `analytics_engineering007.medium`, the baseline filtered out hash-format product
  IDs to make joins work, then failed the product-dimension equality check. The
  Dex run preserved those IDs and compared join keys as text. The task passed.
- **Introduce native dbt model versions.** On `helixops_saas009.base`, the Dex run
  declared v1 and v2 of `dim_accounts`, renamed the requested column in v2, and
  kept v1 as the default. The baseline failed the versioning check; Dex passed
  all four task checks.
- **Retire a feature completely.** On `quickbooks002.base`, the baseline removed
  the flag's conditionals but made department behavior permanent. The Dex run
  used a retirement contract to remove the department joins and outputs. All
  three previously failing output-equality checks passed.
- **Test the rolling-window boundary.** On `airbnb012.base`, the Dex run wrote
  native unit tests covering the date when the oldest day leaves a 28-day
  window. Both the test-existence and broken-model-detection checks passed;
  both failed without Dex.

These are task-level grading outcomes. Dex's own verification can also identify
unfinished work beyond the scored outputs; for example, the QuickBooks retirement
passed its task checks while the broader dev build reported partial completion.

## Results by project

| Project | Without Dex | With Dex | Net passes |
|---|---:|---:|---:|
| Airbnb | 11 / 14 | **12 / 14** | +1 |
| Analytics engineering | 7 / 10 | **8 / 10** | +1 |
| Asana | 3 / 6 | 2 / 6 | −1 |
| Formula 1 | 13 / 16 | **14 / 16** | +1 |
| HelixOps SaaS | 19 / 22 | **20 / 22** | +1 |
| Intercom | 0 / 3 | **1 / 3** | +1 |
| QuickBooks | 2 / 4 | **3 / 4** | +1 |
| **Total** | **55 / 75** | **60 / 75** | **+5** |

## Configuration and measurement

Both runs use `claude-sonnet-5-5` through Claude Code, with a 50-turn/episode limit.
The no-plugin run was recorded on **1 October 2026**; the Dex run on **5 November
2026**. This compares the complete guided Dex configuration with the no-plugin
configuration: Dex adds its tools, skills and a
[general workflow prompt](experiments/exmergo-sonnet55-dex1174-default-guided/prompt.txt)
to inspect relevant data, plan edits, apply them and verify the result.

The Dex run pins **Dex 1.17.4**, **Claude Code 2.1.310**, **dbt 1.10.11**,
**dbt-duckdb 1.9.3**, and **DuckDB 1.3.0**, with default reasoning effort. It used
two workers; the baseline used four. The baseline metadata does not pin its
Claude Code or dependency versions. Scores describe these two configurations;
they do not isolate the engine from its workflow instructions.

The Dex run completed all 75 tasks with **zero execution errors and no retries**.
Its separate smoke diagnostic is excluded. The plugin was loaded on all 75 tasks,
and **74 of 75 (98.7%) executed a task-relevant Dex operation**, with at least one
successful operation on each. [Execution telemetry](experiments/exmergo-sonnet55-dex1174-default-guided/dex-usage.json)
records 572 operations, including 101 plans, 77 applies and 91 builds.

Costs above are reported usage amounts from the result files, excluding
infrastructure and the smoke diagnostic. Dex averaged **$0.159 per task** versus
**$0.066** without it. Median recorded agent runtime was **42.3 seconds** versus
**28.0 seconds**. The improvement measured here is task correctness.

## Earlier runs: Claude Sonnet 5, Fable 5 and Opus 4.8

These earlier results remain available alongside the new same-model comparison.

| Run | Resolved | Accuracy | Reported cost | Raw results |
|---|---:|---:|---:|---|
| Dex + Claude Sonnet 5 | 57 / 75 | 76.0% | $35.95 | [JSON](experiments/2026-07-06__23-00-20__dex_claude_sonnet_5/results.json) |
| Dex + Claude Fable 5 | 56 / 75 | 74.7% | $91.98 | [JSON](experiments/2026-07-07__08-30-07__dex_claude_fable_5/results.json) |
| Dex + Claude Opus 4.8 | 54 / 75 | 72.0% | $43.38 | [JSON](experiments/2026-07-07__11-07-44__dex_claude_opus_4-8/results.json) |
| Claude Sonnet 5 without Dex | 53 / 75 | 70.7% | $30.76 | [JSON](experiments/2026-07-07__13-08-11__none_claude_sonnet_5/results.json) |

## What is committed

`experiments/` publishes each run's original top-level result files and provenance.
The guided Dex runner records its configuration in `manifest.json`, completion in
`summary.json`, its prompt in `prompt.txt`, and operation telemetry in
`dex-usage.json` / `dex-usage.csv`. The original runner records configuration in
`run_metadata.json` and also exports `results.tsv`. These files are copied without
changing task grades or selecting attempts.

Per-task transcripts, recordings, intermediate analysis and supervisor files stay
out of Git.
