# ADE-bench

ADE-bench is dbt Labs' 75-task analytics-engineering benchmark
([dbt-labs/ade-bench](https://github.com/dbt-labs/ade-bench)). Each task hands an
agent a dbt project on DuckDB and asks it to fix a broken model, build a new one,
or extend the semantic layer, then scores whether the project's tests pass.

## Setup

- 75 tasks across 8 domains (airbnb, f1, asana, intercom, quickbooks,
  helixops_saas, analytics_engineering).
- Agent: Claude Code, one attempt per task, up to 50 episodes.
- `dex` is supplied as the `exmergo/dex` skill plugin. The baseline run has no
  plugin; everything else is identical.

## Results

| Run | Resolved | Accuracy | Cost |
|---|---|---|---|
| `dex` + Claude Sonnet 5 | 57 / 75 | **76.0%** | $35.95 |
| `dex` + Claude Fable 5 | 56 / 75 | 74.7% | $91.98 |
| `dex` + Claude Opus 4.8 | 54 / 75 | 72.0% | $43.38 |
| Claude Sonnet 5 (no `dex`) | 53 / 75 | 70.7% | $30.76 |

**`dex` + Sonnet 5 leads at 76%**, for **2.5x less than Fable 5** and **~17% less
than Opus 4.8**. With `dex`, accuracy sits in a 72-76% band across all three
models while cost ranges from $36 to $92, so the practical call is to run an
inexpensive model.

For context, dbt's published agent skills reported 58% on this benchmark with
Opus 4.6.

## Reading the numbers

These are single-run results (one attempt per task), so treat small gaps as
noise.

The raw `results.json` for every run is committed under `experiments/`.
