# data-eng-bench

data-eng-bench is Snowflake's 103-task data-engineering benchmark
([snowflake-labs/data-eng-bench](https://github.com/snowflake-labs/data-eng-bench),
released 2026-08-06). Each task hands an agent a 2,356-model dbt project on a
489 MB DuckDB warehouse and a prescriptive ticket, then scores whether a hidden
pytest suite passes after a cold `dbt run`. Scoring is binary and total: one
failed or skipped assertion is a zero.

## Setup

- 103 tasks (84 build, 19 fix), 3 easy / 47 medium / 45 hard / 8 very hard.
- Agent: Claude Code 2.1.226, one attempt per task, `reasoning_effort: xhigh`.
- Harness: Harbor 0.20.0, `-n 2`, `WebSearch` and `WebFetch` disabled.
- `dex` is supplied as the `exmergo/dex` skill plugin plus the `dex` CLI on PATH.
- Task digests match `snowflake-labs/data-eng-bench@v1.0`; nothing under `tasks/`
  was modified.

## Results

| Run | Resolved | Accuracy | dex trigger rate | Cost |
|---|---|---|---|---|
| `dex` + Claude Sonnet 5 | 59 / 103 | **57.3%** | 98% | $282.12 |

Accuracy is successful trials over total trials, which is the benchmark
leaderboard's headline metric and the quantity Snowflake's launch post calls
`Pass@1`. The 95% confidence interval is 47.6% to 66.4%.

For context, Snowflake published `Pass@1` of 56.6% for both Claude Code and their
own CoCo harness on Sonnet 5, and 69.6% to 73.8% for the Opus 5 rows. **Our result
is nominally the highest published Sonnet 5 figure and is statistically
indistinguishable from the two at 56.6%**: a 0.7 point gap on 103 tasks is less
than one task, and a 56.6% agent produces a draw at least this good about half the
time. Read it as parity, not as an improvement.

## Reading the numbers

This is a single run at `k=1`, below the leaderboard's three-trial minimum, so it
sizes a submission rather than being one. Two consequences worth stating:

- **No `Pass^3`.** That metric requires three trials on every task and cannot be
  derived from one. Our row is absent from that column rather than low in it.
- **No delta claim.** There is no control arm here, so nothing in this directory
  supports "dex is worth X points". The comparison above is against numbers run by
  someone else on a different day.

The trigger rate is reported next to the accuracy on purpose. 57.3% with dex firing
on 98% of trials and 57.3% with it firing on 15% are different claims, and only the
first is a statement about dex. dex ran via the CLI on 90% of trials and the Skill
tool on 18%, and exclusively through `explore`; `transform` and `maintain` were
never invoked.

### Assertion-level results

Because the reward is binary, a task that misses one assertion out of 219 scores the
same as one that never built a model. Each run therefore also publishes per-check
results parsed from the verifier output:

| | |
|---|---|
| Assertions passed, task-weighted | **89.7%** (95% CI 85.2% to 93.6%) |
| Assertions passed, pooled | 94.3% (2,705 of 2,867) |
| Failed tasks blocked by exactly one assertion | 19 of 44 |

Prefer the task-weighted figure when quoting one number; pooling lets a single
219-assertion suite outvote a hundred smaller ones. The interval is a cluster
bootstrap over tasks.

## What is committed

Three files per run under `experiments/`, matching the convention in
[`../ade_bench`](../ade_bench):

| File | Contents |
|---|---|
| `run_metadata.json` | provenance, arm, model, dex version, and every metric with its definition |
| `results.json` | per-task detail including the full per-check pass/fail record |
| `results.tsv` | one flat row per trial |

The Harbor trial artifacts (agent transcripts, trajectories, session recordings)
are roughly 270 MB per run and stay out of git.

**Task prompts are deliberately not included.** Every data-eng-bench `task.toml`
opens with a canary line stating the benchmark data must not appear in training
corpora, and a public repository is one. Tasks are referenced by name and digest.
This is the one place this directory departs from the `ade_bench` layout, which does
publish prompts.

`run_metadata.json` spells out each metric in a `metric_definitions` block, because
`pass_at_3` ("at least one of three attempts succeeds", a leaderboard submission
column) and `Pass^3` ("all three succeed") differ by one character and, on a
realistic board, by roughly 27 points.
