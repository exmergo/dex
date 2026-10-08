# Maintain axes: what each command detects, holds, and costs

Lookup detail for the maintain subcommands. What you must and must not do is in
`SKILL.md`; this file says what a finding means and where it comes from.

## The baseline: `maintain snapshot`

`maintain snapshot` pins the current `.dex/cache.json`, so the grain baseline is
the exact-distinct verdicts `explore map` already computed, plus per-layer
fingerprints of the dbt project and of the semantic layer.

- **The two project layers are fingerprinted independently.** The transform
  layer comes from the dbt project; the semantic layer comes from whichever vendor
  `semantic.vendor` names, which may be dbt's own or a native format such as
  Apache Ossie. A repository with a semantic layer and no dbt project still gets
  a baseline and runs every free axis: `transform_layer` comes back null. The
  warning that no project was fingerprinted is reserved for the case where
  neither layer answered.
- A native semantic layer contributes its definitions per dataset and per
  metric, each with a content hash, the relation behind it, the column each field
  resolves to, its declared keys in the arity they were written, and its
  relationships with every ordered column pair. Whether that side was captured is
  itself recorded, so a baseline written before it reports the relationship axis
  as unchecked rather than clean.
- Without a cache it captures a metadata-only baseline and says so. It warns
  when the cache it pinned is thin (objects without column detail) or older than
  the profile freshness window, because either makes "accept current state" only
  partly true.
- Past 50 objects, `explore map` profiles the top 25 by rank and enters the rest
  as metadata alone. The snapshot envelope reports `column_detail_count` against
  `dataset_count` plus a warning naming what it could not cover.
- `--project-only` refreshes the transform and semantic fingerprints without
  opening the warehouse, carrying the existing `warehouse` block,
  `warehouse_from`, and `cache_updated_at` forward. It requires an existing
  snapshot and refuses connection-target flags (`--connector`, `--path`,
  `--scope`, `--project`, `--dataset`).

## `maintain check`

Sweeps every axis against the snapshot and returns one report ranked by blast
radius. Read-only. `check` warns when the baseline looks stale.

## `maintain schema [<objects>]`: structural drift

Source columns and tables added, dropped, retyped, or renamed; nullability
changes; declared sources the warehouse no longer honors; a model added,
removed, or content-changed since the baseline. Free.

## `maintain volume [<objects>]`: freshness drift

Row counts that collapsed, spiked, or went to zero: the "is the data still
flowing?" axis, distinct from "did the shape change?". Free metadata. An object
the warehouse keeps no count for (a view anywhere, an external table on
BigQuery) is named in `warnings` as not compared.

## `maintain grain [<objects>]`: grain drift

A key that now has duplicates, a changed row-per-entity cardinality, or an
increased join fanout. It also re-verifies the grains the repository
*declares*: a dbt model-level `unique_combination_of_columns`, and a semantic
layer's own key declarations. A multi-column declaration is measured as one
complete composite, never one column at a time. Aggregates only.

- A native semantic layer's keys reach this axis and nothing else reaches them,
  since such a layer is never the transformation project. They take the
  identical billed handshake on a metered warehouse.
- The composites it re-probes are the ranked, artifact-suppressed set
  `explore profile` reported.
- Below `maintain.grain_min_rows` rows (default 100), a uniqueness-regression
  finding is damped to `low`, and the damping is named in the finding's `data`
  (`severity_floor_applied`, `grain_min_rows`).
- Finding codes: `key_lost_uniqueness` (proven unique at baseline, not now) and
  `declared_grain_not_unique` (declared, never held).

## `maintain semantic [<objects>]`: definition drift

Definitions changed, added, or removed against the baseline; a source relation
that is gone; a dimension, entity, measure, or declared key naming a column that
is gone; a relationship whose endpoint or column pairs no longer resolve, which
is `high` because a join nothing can resolve is a broken layer; and categorical
dimensions whose set of values widened or narrowed underneath their metrics.

The cardinality half needs a semantic model that names a transformation model.
Ossie names none, so on an Ossie layer this command is free and offers no scan.

## `maintain verify [<selector>]`: is the project right now

Needs no baseline. Two classes of finding:

- **Build status**: nodes that failed; nodes skipped because a parent failed
  (naming the one that actually failed, through a chain of skipped parents);
  nodes that warned rather than failed (`node_warned`, ranked low, because a
  project running relationship tests at `severity: warn` has warnings by design);
  and models the project declares that built no relation.
- **Row population**: `row_loss` where a model holds materially fewer rows than
  its **driving parent** (the relation in its FROM clause, followed through the
  CTE chain, as distinct from anything it joins) and nothing in its SQL accounts
  for the shortfall; `row_fanout` where it holds materially more. Each names the
  join and its key and states both counts. A model with a `WHERE`, `GROUP BY`,
  `DISTINCT`, `QUALIFY`, `LIMIT`, a semi or anti join, or a set operation is never
  reported for loss, and an incremental model is skipped outright.

A project that does not compile is reported first and suppresses everything
else. `data.suppressed` names every class that did not run and why.

## `maintain reconcile [<class>]`

Proposes the dbt edits that bring the project back in sync, as reviewable diffs,
optionally scoped to `schema`, `volume`, `grain`, or `semantic`. It composes every
layer's declarations first, so a grain the semantic layer already declares is not
proposed as though nothing declared it.

- `mechanical`: on a dex-scaffolded staging model it re-scaffolds the model from
  the drifted source; on a project format that places a declaration but authors
  no staging model, it edits the drifted columns into that declaration.
- `advisory`: the decision surfaced, at most backed by a test edit. It declines
  that test where the test would be wrong: if a model declares a composite grain
  covering the column, no column-level `unique` is proposed on it, and the warning
  names the combination.
- Type changes: nothing dex writes declares a type, and the type it holds is the
  connector's own spelling (Snowflake reports `NUMBER(38,0)` and `NUMBER(10,2)`
  both as `FIXED`), so the proposal names both spellings.

## What each axis costs

| Axis | Cost |
|---|---|
| `schema`, `volume`, semantic definitions | free metadata, everywhere |
| `grain`, semantic dimension cardinality | scans; billed handshake on a metered connector |
| `verify` build status | free (artifacts on disk) |
| `verify` row counts | free object metadata, except a view, which keeps no count: those are batched into one aggregate-only statement, priced, and returned in `data.offer` |

On DuckDB there is no gate, so `verify` measures every count and its findings
come back `exact`.
