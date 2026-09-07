---
name: maintain
description: 'Use this to keep a dbt project and its semantic layer correct as the warehouse and the business change, including a semantic layer that is native Apache Ossie documents rather than dbt. It detects drift on four axes and proposes the fix: schema drift (source columns and tables added, dropped, retyped, or renamed), volume drift (a row count that collapsed, a table that emptied, a load that half-failed), grain drift (a key that lost uniqueness, a changed row-per-entity cardinality, an increased join fanout), and semantic drift (a metric, measure, dimension, or entity definition that no longer matches, new categorical values, dangling semantic references). Reach for this when something that used to work has started failing or producing different numbers and the cause is more likely upstream than in the code you just wrote: a test that began failing with no code change, a dashboard whose numbers moved, a model that is suddenly empty or duplicated. Trigger it for requests like "what changed in the warehouse", "did anything drift", "is my dbt project still in sync", "my primary key has duplicates now", "the row count dropped", "did the load run", "the data stopped flowing", "the revenue metric definition changed", "reconcile my models with the source schema", "which models are stale", "did my Ossie semantic layer drift", or "is this relationship still valid". It reads the .dex/ snapshot and proposes reviewable diffs; it never overwrites hand-written work. To author new models or metrics from scratch, use transform. To learn an unfamiliar warehouse for the first time, use explore.'
---

# Maintain

Keep the repository correct as the world underneath it moves, on both of its
axes: the dbt project and the semantic layer. Maintenance is the recurring half
of the loop: warehouses drift, loads half-fail, models go stale, keys stop being
unique, and business definitions change. This skill compares a known-good
baseline against current reality, classifies what drifted, and proposes the
reconciling edit. It is manual and on-demand here; continuous drift detection and
automated PRs are the commercial product.

## The model: baseline, detect, reconcile

Drift is measured against a **baseline** (the `.dex/snapshot.json` fingerprint of
the warehouse map and the repository's per-layer definitions). Detection is
read-only; only reconcile proposes edits.

**The two project layers are fingerprinted independently.** The transform layer
comes from the dbt project; the semantic layer comes from whichever vendor
`semantic.vendor` names, which may be dbt's own or a native format such as
Apache Ossie. A repository with a semantic layer and no dbt project at all still
gets a baseline and still runs every free axis: `transform_layer` comes back
null, and the warning that names why no project was fingerprinted is reserved for
the case where neither layer answered, since that is the one you could otherwise
mistake for a clean read.

**Snapshot discipline matters.** A snapshot is only as trustworthy as the moment
it froze. Take one right after a known-good build (`maintain snapshot`), and
**commit `.dex/snapshot.json` like a lockfile** so the whole team diffs against
the same reference. Snapshot a state that is already drifted and `check` will
mask the very drift you care about. When you accept a change as the new normal
(re-run `explore map` first, then `maintain snapshot`); `check` warns when the
baseline looks stale.

**On a warehouse past the rank cutoff, use `explore map --full` before
snapshotting.** Past 50 objects `explore map` profiles the top 25 by rank and
enters the rest as metadata alone, and the baseline can only compare columns for
objects it has columns for. Snapshotting a partial map is still valid, and the
envelope reports `column_detail_count` against `dataset_count` plus a warning
naming what it could not cover, so the gap is visible rather than silently
mistaken for a clean bill.

## How to drive it

```bash
uv run --no-project --script "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

dex runs its engine through `uv`, which is a prerequisite and is not installed by
Claude Code. If the shell reports `uv: command not found`, stop and tell the user
to install it (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or
`brew install uv`, or `pipx install uv`), then re-run. Never fall back to diffing
the warehouse against the project by hand instead: the drift axes and the baseline
comparison live in the engine, so any other path is guesswork.

The first command in a fresh environment installs the engine, so it can take tens
of seconds where later ones take well under a second. `--warm` pays that install up
front and exits without running anything:

```bash
uv run --no-project --script "${CLAUDE_SKILL_DIR}/scripts/run.py" --warm
```

Offer it once at setup. It is not something to run before an ordinary command.

- `maintain snapshot` captures or refreshes the baseline. Run it after a clean
  explore or transform session so later runs have a known-good reference. It pins
  the current `.dex/cache.json` (so the grain baseline is the exact-distinct
  verdicts `explore map` already computed) plus per-layer fingerprints of the dbt
  project and of the semantic layer. A native semantic layer contributes its
  definitions per dataset and per metric, each with a content hash, the relation
  behind it, the column each field resolves to, its declared keys in the arity
  they were written, and its relationships with every ordered column pair;
  whether that side was captured is itself recorded, so a baseline written before
  it reports the relationship axis as unchecked rather than clean.
  Without a cache it captures a metadata-only baseline and says so. It
  also warns when the cache it pinned is thin (objects without column detail) or
  older than the profile freshness window, because either makes an "accept
  current state" only partly true.
- `maintain snapshot --project-only` is for a project-only refactor, such as
  moved model files or a dbt project rename. It refreshes transform and semantic
  fingerprints without opening the warehouse, carrying the previous warehouse
  evidence and original capture time forward instead. It requires an existing
  snapshot and refuses connection-target flags.
- `maintain check` is the everyday entry point: it sweeps every axis and returns
  a report ranked by blast radius. Read-only.
- `maintain schema [<objects>]` detects **structural drift**: source columns and
  tables added, dropped, retyped, or renamed; nullability changes; declared
  sources the warehouse no longer honors.
- `maintain volume [<objects>]` detects **freshness drift**: row counts that
  collapsed, spiked, or went to zero. This is the "is the data still flowing
  correctly?" axis, distinct from "did the shape change?".
- `maintain grain [<objects>]` detects **grain drift**: a key that now has
  duplicates, a changed row-per-entity cardinality, or an increased join fanout.
  It also re-verifies the grains the repository *declares*, which measurement on
  its own can miss: a dbt model-level `unique_combination_of_columns`, and a
  semantic layer's own key declarations. A multi-column declaration is measured
  as one complete composite and never one column at a time. Uses aggregates,
  never raw rows.

  A native semantic layer's keys reach this axis and nothing else reaches it for
  them, since such a layer is never the transformation project. They go through
  the identical billed handshake on a metered warehouse: nothing here is cheaper
  or less gated because the declaration came from a document rather than from
  dbt.

  Two findings come out of the uniqueness checks and the difference is the
  baseline. `key_lost_uniqueness` is a key that was proven unique and is not any
  more: something changed in the data. `declared_grain_not_unique` is a declared
  combination that does not hold, and nothing changed at all: the project asserts
  a grain the data never had, so the fix is to the declaration (widen it, dedup
  upstream, or drop the claim) rather than to the data.
- `maintain semantic [<objects>]` detects **definition drift**: definitions that
  changed, were added, or were removed against the baseline; a source relation
  that is gone; a dimension, entity, measure, or declared key naming a column
  that is gone; a relationship whose endpoint or column pairs no longer resolve,
  which is `high` because a join nothing can resolve is a broken layer rather
  than a stale one; and categorical dimensions whose set of values widened or
  narrowed underneath their metrics.

  Read `unavailable` on the layer before hunting for an element kind. A native
  Ossie layer has no measures and no entities at all, so their absence is the
  format rather than drift. Its cardinality half also never fires, because that
  check needs a semantic model naming a transformation model and Ossie names
  none: on such a layer this command is free and offers no scan.
- `maintain verify [<selector>]` answers a different question from every command
  above it: not "what changed since the baseline" but **"is this project right
  now"**, and it needs no baseline at all, so it works on a project that was
  never correct and on one somebody else just built. Two classes of finding.
  Build status: nodes that failed, nodes skipped because a parent failed (naming
  the one that actually failed), and models the project declares that built no
  relation. Row population: `row_loss` where a model holds materially fewer rows
  than its **driving parent** (the relation in its FROM clause, followed through
  the CTE chain, as distinct from anything it joins) and nothing in its SQL
  accounts for the shortfall, and `row_fanout` where it holds materially more,
  each naming the join and its key and stating both counts.

  Row population is conservative on purpose. A model with a `WHERE`, `GROUP BY`,
  `DISTINCT`, `QUALIFY`, `LIMIT`, a semi or anti join, or a set operation was
  written to hold a different number of rows and is never reported for loss; an
  incremental model is skipped outright. So a quiet answer here is weaker
  evidence than a finding, and the `warnings` say which models could not be
  lined up at all.

  A project that does not compile is reported first and suppresses everything
  else, since a manifest a broken project could not have produced is not
  evidence. Read `data.suppressed` before reading an empty `data.findings` as a
  clean bill of health.
- `maintain reconcile [<class>]` proposes the dbt edits that bring the project
  back in sync, as reviewable diffs. Optionally scope it to one class (`schema`,
  `volume`, `grain`, or `semantic`). It composes every layer's declarations
  first, so a grain the semantic layer already declares is not proposed as though
  nothing declared it. Where there is no editable dbt project it has nothing to
  author: every proposal is advisory and no plan is stored. Authoring into a
  native semantic layer is `semantic ossie` in the transform skill, never this
  command.

The usual flow: `check` to triage, a focused detector to understand one axis in
depth, then `reconcile` to get the proposed fix. With no baseline, or on a
project whose numbers were never right, start at `verify` instead: it is the one
command here that does not need a snapshot, and it answers "is this right"
rather than "what moved".

## Per-axis cost: what is free and what scans

Detection is read-only, but read-only is not the same as free on a metered
connector (BigQuery, Snowflake, Databricks, Postgres, Redshift, ClickHouse).
The axes split:

- **Schema, volume, and the reference/definition half of semantic are free**
  everywhere: they read metadata and the snapshot, and run immediately.
- **Grain and the dimension-cardinality half of semantic scan the warehouse**, so
  on a metered connector they run the two-step handshake. Asked for directly,
  `maintain grain` returns `needs_confirmation` with an estimate in
  `cost.estimate` (and a per-table breakdown). Surface it to the user in human
  units, get an explicit budget, and re-issue the same command with
  `--confirm --budget <magnitude>` in the paradigm's unit (bytes on BigQuery,
  warehouse-seconds on Snowflake and Databricks, compute-seconds on Redshift,
  database-seconds on Postgres and
  ClickHouse). Never invent a budget the user did not agree
  to, and never retry with a raised budget on an over-ceiling refusal without
  asking. An over-ceiling
  refusal carries a calibration line from `.dex/spend.jsonl` (what this
  connector's recent commands billed as a fraction of estimate, or a sentence
  saying there is too little history to say): relay it, and note that the
  ceiling binds on the estimate, so a budget set at that fraction of the
  estimate is refused again.
- **`verify` is free except for the counts a warehouse does not keep.** Its
  build-status findings read artifacts on disk, and its row counts come from
  object metadata. A view has no stored row count anywhere, and a view is dbt's
  default materialization, so on a metered connector those counts are batched
  into one aggregate-only statement, priced, and returned in `data.offer` beside
  findings that are already final. On DuckDB there is no gate, so every count is
  measured rather than estimated and the findings come back `exact`.
- **`check`, `semantic` and `verify` answer first and offer second.** Their free axes
  complete on every call, so the envelope is `ok` and the findings in it are
  final. The price of the scanning axes sits in `data.offer`, with `axes` naming
  what it would add; `data.axes_run` names what already ran. Confirming is a
  choice, not a required next step: quote the estimate, say which axes are still
  dark, and let the user decide. A triage pass that stops at the free axes is a
  complete piece of work, not an abandoned one.
- **Read `warnings` on these responses, always.** They carry the reasons the
  baseline may no longer describe the warehouse (a cache newer than the
  snapshot, a baseline pinned from a stale cache), which bound every finding
  above them. A stale baseline is often the most important line in the response
  and it is never in `findings`.

On DuckDB everything is free and local, so nothing prompts.

A `needs_confirmation` envelope carrying `suggested_session_ceiling` is the
project's one-time ask for a *cumulative* daily cap, separate from the
per-command `--budget`. Surface it, get the user's answer, and add
`--session-ceiling <value>` or `--no-session-ceiling` to the same re-issue; it is
written to `.dex/config.yml` once and never asked again. Never answer it for
them.

## Reconcile proposals are mechanical or advisory

Reconcile tags every proposal by `kind`, because the fix differs sharply by axis:

- **`mechanical`**: schema drift reconciles in one of two shapes. On a
  dex-scaffolded staging model it re-scaffolds the model from the drifted source;
  on a project format that places a declaration but authors no staging model, it
  edits the drifted columns into that declaration and says so. High-confidence, but
  still a reviewable diff: read it for hand-written logic the scaffold cannot know
  about.
- **`advisory`**: grain, volume, and semantic drift are decisions, not auto-fixes
  (dex cannot dedup your warehouse or decide whether a new `'refunded'` status
  belongs in a metric). The proposal is the decision surfaced, at most backed by a
  test edit that makes the break visible in builds. It declines that test where
  the test would be wrong: if your model declares a composite grain covering the
  column, no column-level `unique` is proposed on it, and the warning names the
  combination so you can tell "re-baseline, this is still the grain" from
  "something relied on that column alone".

**A type change is advisory on every format.** Nothing dex writes declares a type,
and the type it holds is the connector's own spelling rather than a canonical one
(Snowflake reports `NUMBER(38,0)` and `NUMBER(10,2)` both as `FIXED`), so the
proposal names both spellings and the edit is yours. One consequence to know: on
ClickHouse nullability is part of the type, so a column that starts accepting nulls
is reported as a retype and gets advice where other connectors get an edit.

When reconcile produces edits it stores them as a plan and prints a `plan_id`.
Apply them with `transform apply <plan-id>` (the one apply door): a human edit made
since detection surfaces as a conflict, never a silent overwrite.

## Guardrails (enforced in the engine, not here)

- Read-only against data. Schema, volume, and semantic references are computed from
  metadata and the snapshot; grain and dimension-cardinality use aggregates only.
  Raw rows and dimension values never cross the envelope.
- Propose, don't impose. Reconciliation is always a reviewable diff, applied
  through `transform apply`. Human edits to the project and to the semantic layer
  are authoritative; on conflict the engine surfaces the divergence and asks
  rather than overwriting.
- The repository is the source of truth, on both axes; the `.dex/` snapshot is a
  non-canonical fingerprint used only to detect change.
