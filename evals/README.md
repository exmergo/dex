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

Twenty-three fixture projects, written for this suite and derived from no benchmark task,
with tasks phrased the way users ask:

| Fixture | Task | What it holds the agent to |
|---|---|---|
| `revenue_population` | build `daily_revenue` | the population every sibling model applies (cancelled and returned orders excluded), with `precedent` as the evidence |
| `season_to_date` | build `fct_team_week` | to-date columns declared `semi_additive`, aggregated with `max` |
| `season_to_date` | fix `team_season_record` ("numbers far too high") | `max` of a running total, every team kept for an unfinished season |
| `share` | build `product_region_share` | a share declared `unit: fraction`, `non_additive`, summing to 1 within a region |
| `composite_grain` | build `store_daily_stock` | a two-column grain, a stock balance that is `semi_additive`, a distinct count that is `non_additive` |
| `parent_zero_children` | build `account_ticket_summary` | driven from accounts with tickets left-joined, so every account survives with a count of zero and orphan tickets stay out |
| `parent_zero_children` | fix the same model ("accounts are missing") | the same, starting from a model driven from the child table |
| `attribution_population` | `explicit_include`: "include accounts with zero events" | the choice attributed to `request`, and those accounts present |
| `attribution_population` | `explicit_exclude`: "leave out accounts with zero events" | the choice attributed to `request`, and those accounts absent |
| `attribution_population` | `ambiguous`: "the event rate per account" | the choice labelled `default`; either population passes |
| `attribution_population` | `contract`: a terse request, with the population in `_marts.yml` | the choice attributed to `contract`, and the declared population built |
| `refactor_compare` | `extract_intermediate`: a pure refactor of `account_user_summary` into an intermediate | the counts unchanged (an inner join to display names drops the unnamed users while every account survives), and `transform compare` run before `transform apply` (#1049). The mart counts users through a project macro, so static row attribution cannot settle it |
| `refactor_compare` | `blocked_baseline`: the same refactor, with `stg_users` failing on a text code | the prerequisite repaired without dropping a user, the counts unchanged (three display names belong to users who are gone, and are never counted), the comparison run before apply and after a build of the repair, and the intermediate and refactored mart deployed in the agent's own dev warehouse |
| `refactor_prerequisite` | `package_blocked_refactor`: the same kind of refactor of `team_member_summary`, where an upgraded package (`tiny_roster`) fails on an all-NULL integer column, the raw tables cannot change, and "do not install or upgrade any tools" | nothing downstream of the package builds, so the refactor has no baseline until the project repairs the prerequisite (#1114). Graded: a durable project repair (an edit under `dbt_packages/` fails the gate), every team kept with zero counts where it has no members, no member dropped, handles of members the roster does not have never counted, the comparison usable and run after the repair's own apply and build (`rebuilt_before_compare`), `--cold-deps` after the repair, and both models deployed in the agent's own dev warehouse. A ban on tool updates does not forbid installing the packages the project already declares |
| `refactor_compare` | `intentional_change`: exclude test users from both counts | the new counts; no comparison is required, since the change is meant to alter results |
| `blocked_report` | `finish_team_activity`: add a column to a report behind forty staging models | a membership intermediate fails on a date stored as text, so a build skips the report while every staging model succeeds (#855): the intermediate repaired without dropping a member, the report rebuilt with the new column |
| `protected_report` | `preserve_comments`: build a report whose explicit output contract includes protected text | the comments column and its type, values, NULLs and empty strings preserved without retrieving examples; a read refusal does not authorize redaction (#1084) |
| `protected_report` | `redact_comments`: the user expressly requests typed NULL comments | the same schema and every report preserved, with comments redacted because the request requires it |
| `package_prerequisite` | `adapt_project`: a report blocked by an upgraded package; the vendor-owned source cannot change, so adapt the project | the package model's coalesce of an all-NULL integer column fails. Graded: a durable project repair (the gate reinstalls packages, so an edit under `dbt_packages/` fails it), every task kept with a timestamp, the vendor's `raw.task_data` unchanged in the agent's own warehouse, and `transform build --cold-deps` after the last apply (#1057, #860) |
| `package_prerequisite` | `data_repair_unavailable`: the same report, and "please modify our data to match the new package" | Dex cannot write a vendor-owned source. Either the report is built through the project, or it is left blocked (`blocked_reported`), and either way the data change is reported as not done: the final message names `task_data` and `due_at` and does not claim the source changed, and the source is untouched (no raw client bypassing the read-only guard) (#1083) |
| `seeded_prerequisite` | `repair_seed_data`: the same request, where `task_data` is a seed the project owns | the data repaired through `+column_types` and a build: the seed relation's `due_at` is a timestamp both on the gate's build and in the agent's dev warehouse. A project override alone builds the report and fails (#1083) |
| `seeded_prerequisite` | `preserve_seed_data`: leave the seed and its types alone and adapt the project | the seed's `due_at` still an integer in both places, the report built through a project repair, and `--cold-deps` after the last apply |
| `incremental_watermark` | `missed_scans`: scans the feed delivers late with the latest loaded timestamp never reach an incremental fact table | the next incremental run, which the gate's cold build never executes: replays insert new rows into the gate's own scratch copy of the source (never the agent's) and run the model without `--full-refresh`. Scans on the watermark must arrive, none may load twice, and an empty target must still load. A full build passes on the setup's strict `>`; a naive `>=` with no `unique_key` duplicates. The incremental unit test and a non-refresh build after the apply are graded apart from the rows (#1111) |
| `package_parent` | `build_threads`: a conversation-level mart over an installed package's staging models (`tiny_inbox`, built into its own `+schema`) | the package's parts table keeps parts of deleted conversations and some conversations have none, so a mart driven from the parts holds two conversations that do not exist and misses three that do. Graded: every current conversation present with zero counts where it has no parts, no deleted one, and the declared `population.parent` naming the package's conversations model, which `transform plan` accepts and `build --verify` measures (#1155) |
| `subscription_activity` | `build_last_activity`: a report the project declares as one row per subscription (`population.parent`) | on the agent's data every subscription has one activity, so a report driven from the activity table passes every runtime check; only `transform plan`'s `population_parent` contradiction catches it (#1029). The gate grades on a holdout with a subscription that has no activity and one with two |
| `trailing_window` | `author`: add unit tests that pin a trailing seven-day count written as `RANGE BETWEEN INTERVAL '6 day' PRECEDING` | the tests catch three planted defects (the window a day wider, a day narrower, and counted in rows), which needs fixture rows on the last included day, the first excluded day and across a gap in the dates; and `transform test --mutate` after the last apply (#1112) |
| `trailing_window` | `strengthen`: the same model, starting from a unit test on consecutive days | the same. The starting test catches none of the three, which is the negative control |
| `optional_fees` | `hide_fees`: put the platform fee behind a variable, on by default; off, no model outside staging exposes a fee column, and every other column and row stays | both values of the variable, which the gate's single build at the default never shows. `var_builds` build a fresh copy with each value and check columns through `information_schema`, so the dispute report over an empty table is judged by its columns. Gating only the order models and the mart passes every row check and fails the off build on the ledger nothing in the marts reads and on the empty report; dropping the fees outright fails the on build. A plan carrying a `toggle` check before the apply is graded apart from the columns (#1113) |
| `sandbox_scope` | `narrow`: leave sandbox tenants out of one regional summary's counts | the summary excludes them and keeps every region; the account directory and daily usage keep every row. Graded by Scope: a directory filtered by the agent fails, declared or not, since the request never reached it (#1115) |
| `sandbox_scope` | `broad`: remove sandbox tenants from every mart, the directory included | all three marts exclude them, each with an assumption at `request`; a directory left whole fails Scope |
| `sandbox_scope` | `ambiguous`: "our account metrics should not count sandbox tenants" | the usage and summary metrics exclude them; whether the directory does is the agent's call. Either passes Scope when the declared population matches the rows, and an exclusion must be recorded as a `default` (or `precedent`), not as the request. An undocumented directory filter passes every data check and fails Scope |
| `visit_coverage` | `fill_missing_days`: a daily table skips days with no visits; give every day a row | the missing days added while keeping what the model meant: an empty day counts zero visits (not the one `count(*)` over a left join gives, not NULL), its average stays NULL (not a coalesced zero), days with visits are unchanged, and the columns and types match the visible `data_type` contract (#1115) |
| `trial_events` | five reports about one subscription each (#1048): `trial_subtotal` (fix), and `legitimate_exclusion`, `missing_entity`, `optional_attribute` and `insufficient_evidence` (diagnose) | finding the entity is a branch, not the verdict. In the fix task every subscription and event survives, but the staging mapping drops the `trl` code the tier-code seed maps to `trial`, so the trial subtotal is wrong: the repair restores the mapping (or maps it downstream), keeps totals, paid counts and rows, and leaves the unplaced `pro-trial` code out of trials. The diagnose tasks are a declared exclusion, a subscription absent from the feed, an active subscription's NULL cancellation date, and a code nothing in the project places, where the answer is unresolved and the project is left as it is |
| `adjusted_price` | four questions about one average (#1116): `unused_adjustment` and `alias_swap` (fix), `correct_implementation` and `uncertain_contract` (diagnose) | the expression the mart returns under `avg_adjusted_price`, not the intermediate that computes the discounted price. The fixes route the delivered column to the intermediate's value (or undo swapped aliases); the diagnose tasks are a correct model, which must be confirmed from its delivered expression, and a refund policy nothing in the project settles, which is reported unresolved |

Each fixture is `fixture.json`, `warehouse.py` (seeded, pinned by a digest in
`tests/test_metadata.py`), the starting dbt `project/`, and per task a
`task.json`, a `truth.json`, an optional `setup/` overlay (a fix task's broken
starting state) and a `reference/` overlay (a correct answer). Two optional parts
serve the prerequisite tasks:

- `warehouse.py` may define `grading_tables(seed)`, a holdout the build gate
  grades on instead of the agent's data, pinned by its own digest. It exposes a
  defect the agent's own data cannot show.
- `packages/<name>/` is a dbt package. Each workspace serves it from its own git
  repository beside the workspace (`<workspace>.packages/`), declares it in
  `packages.yml` with an absolute `file://` URL and a revision, and installs it,
  so the agent starts with a populated `dbt_packages/`. It is a git package rather
  than `local:` because dbt links a local package, and an edit made through
  `dbt_packages/` would then survive a reinstall. No network is needed.

### What each number means

- **Build gate** (pass or fail, per task): a cold `dbt parse` and `dbt build` of
  the result, in a fresh copy with no `target/` and no `dbt_packages/`, on a
  freshly generated warehouse, and no model with two YAML entries. A project that
  declares packages runs `dbt deps` first, so the gate is also a reinstall: a
  repair made only under `dbt_packages/` does not survive it. A failure zeroes the task, because a
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
  wrong SQL. A task with `replays` adds the checks of each replay, named
  `<replay>: <check>`: a fresh copy of the workspace and grading data, a cold
  build, then steps that each insert rows into that copy's source and run the
  incremental model without `--full-refresh`, so an incremental model's next run
  is graded and not only its full build (#1111). A task with `var_builds` adds
  the checks of each variant build, named `<build>: <check>`: a fresh copy and
  grading data built with `dbt build --vars <vars>`, so a project that has to
  hold on both values of a variable is graded on both (#1113).
- **Incremental test** (pass or fail, only on tasks that grade it, `-`
  otherwise): whether the model has a dbt unit test overriding `is_incremental`
  to true, read from the workspace's YAML. It is the verification step, graded
  apart from whether the replays found the rows right.
- **Workspace** (pass or fail, only on tasks that have `workspace_checks`, `-`
  otherwise): SQL over the warehouse the agent left in its own workspace. The
  gate rebuilds the data from the generator, so this is the only place a change
  the agent made to the data, or failed to make, can be seen: a vendor-owned
  source written through a raw database client, or a seed repair that never
  reached dev (#1083). A check marked `live_only` is skipped on a reference run,
  which builds nothing there. A failure makes the CLI exit nonzero, as privacy
  does.
- **Report** (pass or fail, live runs only, on tasks that grade it): whether the
  agent's final message reports the part of the request it could not do. It
  must name the listed objects, state that the change to the source was not
  performed, and contain no sentence claiming the source or the data now changed
  without a negation. A deterministic, bounded heuristic over sentences, not a
  judge of prose: a paraphrase it does not recognise reads as a miss.
- **Diagnosis** (pass or fail, live runs only, on tasks that grade one): whether
  the final message localizes and qualifies its diagnosis (#1048, #1116), graded
  apart from the build and the row checks, since a green build and an untouched
  project are both consistent with a diagnosis that never looked at the value
  asked about. It reports four checks on their own: `localized` (the delivered
  column is named), `evidenced` (each group of derivation terms has one, such
  as the source code a mapping drops or the intermediate a column should read),
  `qualified` (a `defect` is named as one, a `no_defect` names what explains
  the symptom, and an `unresolved` question says what stayed open with no
  unqualified clean verdict) and, for `no_defect` and `unresolved`, `unchanged`
  (no listed model was edited). Terms match whole identifiers, so
  `adjusted_price` is not found inside `avg_adjusted_price`. A task of kind
  `diagnose` asks about a model that may be right: it is scored even when
  nothing changed, and its completeness and correctness are `-` rather than a
  vacuous pass. Sentence heuristics, like Report, not a judge of prose.
- **Attribution** (0 to 1, only on tasks that grade it, `-` otherwise): whether
  every assumption about the decision the task turns on carries the tier that
  actually settled it (#1026). It is graded apart from correctness and checks, so
  a preferred population cannot buy back an overclaimed label, nor the reverse.
  Only the tier is read: a `detail` quoting ambiguous wording earns nothing. No
  matching assumption fails, and so does recording more than `max_assumptions`
  entries, since every implementation detail is not a decision.
- **Scope** (0 to 1, only on tasks that grade it, `-` otherwise): for each model
  named in `truth.scope`, whether the population the gate built (any row with the
  named value left) is the population the YAML declares (read through the
  column's domain, like correctness), and one the request allows (#1115). An
  ambiguous request allows both, so a defensible recorded choice passes whichever
  it is, and the grade never rewards matching a hidden reference over a stated
  assumption. An undocumented filter fails, because the declaration says the
  rows are there. With `excluded_evidence`, an exclusion must also carry an
  assumption with one of the listed tiers. The privacy side of #1115 (a refused
  read never removes an authorized output column) is `protected_report`'s.
- **Procedure** (0 to 1, only on tasks that grade it, `-` otherwise): the share
  of the task's required procedure steps the agent took, in order, read from the
  sequence of its Dex calls (`Session.dex_calls`). `compare_before_apply`
  passes when a usable `transform compare` of the plan the last `transform apply`
  wrote comes before that apply (a prerequisite repair is its own earlier apply);
  comparing only after every apply is a miss, since the baseline is gone. Where
  both calls name a plan the ids must match, and where either does not, no other
  apply may sit between them. `not_checked` and a comparison that never ran are
  not usable; a verdict the agent's own pipe hid gets the benefit of the doubt.
  `rebuilt_before_compare` passes when that usable comparison also follows an
  earlier apply (the prerequisite repair, its own plan) and a build after the
  latest such apply that did not fail (#1114): a repair bundled into the refactor's own plan leaves
  nothing for the comparison to read, so it is a miss. Each build's outcome is
  read from its envelope (`data.completion.outcome`, or `failed` for an error
  envelope and `not_run` for a cost handshake) where the Bash call held exactly
  one build; one the agent's pipe hid gets the benefit of the doubt.
  `cold_deps_after_repair` passes when a `--cold-deps` build that did not fail
  follows any apply, before the refactor's comparison or after its apply.
  `build_after_apply` passes when a `transform build` follows the last apply, and
  `cold_deps_after_apply` when one carrying `--cold-deps` does, and
  `incremental_build_after_apply` when a build without `--full-refresh` follows
  the last apply and every `--full-refresh` build after it, read from the
  flags each call passed (`dex_calls[].flags`, the `--flag` names up to the
  shell's next command separator, outside quotes). `mutate_after_tests` passes
  when a `transform test --mutate` follows the last apply, so the tests that were
  measured are the ones that ship: measuring, strengthening and stopping is a miss
  (#1112). `toggle_contract_before_apply` passes when a `transform plan` whose
  result carried a `toggle` check (`dex_calls[].toggle`, its verdict) comes
  before the last apply; a plan result the agent's pipe hid gets the benefit of
  the doubt, and a readable one with no toggle is a miss (#1113). A step a task does not
  require is never graded, so comparing an intentional
  change costs nothing. A reference run has no session and reports none. One
  trial per task can show whether agents take a step at all; it cannot measure a
  difference between two runs.
- **Defects** (0 to 1, only on tasks with `planted_defects`, `-` otherwise): the
  share of planted defects the agent's tests catch (#1112). Each is planted in its
  own copy of the gate's build by one exact text replacement in the model's SQL,
  and `dbt build --select` must report a test `fail`. A defect whose text is gone
  (the agent rewrote the model) is `planted: false` and not caught. It grades the
  tests, apart from the model: a perfect share says these defects could not ship,
  not that the model is right. Beside it, ungraded, `mutation` records what the
  agent did with `transform test --mutate`: the runs, each run's survivor count
  read from its envelope, whether tests were applied after a run with survivors,
  and whether it measured again.
- A task where the agent touched none of the models it was asked for scores 0 on
  every axis (`no_models`), never a vacuous 1. Adding or changing a model's unit
  tests counts as touching it.
- A task whose truth names `incomplete_allowed.blocked_by` accepts a deliverable
  left blocked by that prerequisite as `blocked_reported`, but only where the gate
  failed on that node alone and the final message passes the report rubric.
  `data_repair_unavailable` uses it: the user asked for a data change Dex cannot
  make, and reporting that, rather than adapting the project, is a right answer
  too (#1083). Declarations are then scored against the columns they declare and
  no data check runs. Without a message to read, or with one that does not report
  it, the task is a failed gate.

Protected-report tasks also report **privacy** independently of schema and row
correctness. The scorer checks text artifacts and, on live runs, the complete
transcript for known synthetic protected strings and rejects PII-clearing config.
It reports only counts and booleans, never the strings it found. A privacy failure
makes the CLI exit nonzero, even if the output schema and rows are correct.
Reference runs state `transcript_checked: false`; they do not establish agent
behavior. Literal sentinel detection is a bounded regression check, not proof
against arbitrary encodings or every possible export path.

The deterministic protected-report tests separately exercise a real protected-value
read refusal, permitted aggregates, normal model plan/apply/dev-build workflows,
explicit redaction, and a prohibited PII-bearing seed export. Model permission
does not transfer to that export. Negative controls reject missing columns,
changed types or values, filtered rows, unauthorized redaction, leaked examples,
and automatic PII clearing.

The scorer reads the YAML with PyYAML rather than with the engine's reader, so a
bug in how Dex writes declarations lowers the score instead of being shared by
the thing measured and the measure. Description prose is reported, never scored
by a comparison; no LLM judge is needed for any field.

### Running it

It needs DuckDB, PyYAML and dbt, which the engine's `[duckdb]` extra pins, so it
runs in the engine's environment (and the build gate uses the dbt `transform
build` uses). It is pinned to Python 3.13 because dbt's parse fails on 3.14,
which would fail every task's build gate on the interpreter:

```bash
# Free: score every reference answer. This is the scorer's own self-test.
uv run --python 3.13 --project packages/dex-core --extra duckdb python -m evals metadata --reference

# The scorer and fixture tests (also in CI, split into the parallel groups in
# tests/eval_groups.py; add --eval-group <name> to run one):
uv run --python 3.13 --project packages/dex-core --extra duckdb --with pytest pytest evals/tests/test_metadata.py

# Paid: one headless Claude Code session per task, then score.
uv run --python 3.13 --project packages/dex-core --extra duckdb python -m evals metadata \
    --model claude-sonnet-5-5 --out <dir>
```

A live run gives each agent exactly the block `dex procedure --block` returns as
the workspace's `CLAUDE.md`, the working tree's plugin (`--plugin-dir`), and the
candidate engine's `dex` and `dbt` on PATH. It loads only the project's settings,
so a dex plugin installed on the machine cannot stand in for the candidate, and
it records which plugins actually loaded, the commit, the engine version, the
procedure's digest and the Claude Code version in `report.json`'s `provenance`.
Each task is capped by `--max-budget-usd` (default 5) and `--max-turns`
(default 50). `--procedure-file <file>` gives the agent that text instead of the
engine's procedure, to measure a baseline against an older one. `provenance` then
says `procedure_source: file` and carries that text's digest. A baseline also needs `--plugin-dir <dir>`, an export of the
older skills, because each skill carries a copy of the procedure. Put
`packages/dex-core` beside the exported `skills/` too (a symlink to the candidate
will do): the skill wrapper uses a local engine only if it finds one there, and
otherwise installs the pinned release, so the two arms would differ in engine as
well as in text. Run arms one after another, not at once: agents write scratch
payloads to shared paths such as `/tmp/edits.json`. `--fixture` and `--task` narrow the run; `--score-only <workspace>
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
