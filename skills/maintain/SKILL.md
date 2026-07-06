---
name: maintain
description: Use this to keep a dbt project correct as the warehouse and the business change. It detects drift on four axes and proposes the fix: schema drift (source columns and tables added, dropped, retyped, or renamed), volume drift (a row count that collapsed, a table that emptied, a load that half-failed), grain drift (a key that lost uniqueness, a changed row-per-entity cardinality, an increased join fanout), and semantic drift (a metric, measure, dimension, or entity definition that no longer matches, new categorical values, dangling semantic references). Trigger it for requests like "what changed in the warehouse", "did anything drift", "is my dbt project still in sync", "my primary key has duplicates now", "the row count dropped", "did the load run", "the data stopped flowing", "the revenue metric definition changed", "reconcile my models with the source schema", or "which models are stale". It reads the .dex/ snapshot and proposes reviewable diffs; it never overwrites hand-written work. Do not use it to author new dbt models or metrics from scratch (use transform) or to explore an unfamiliar warehouse (use explore).
---

# Maintain

Keep the dbt project correct as the world underneath it moves. Maintenance is the
recurring half of the loop: warehouses drift, loads half-fail, models go stale,
keys stop being unique, and business definitions change. This skill compares a
known-good baseline against current reality, classifies what drifted, and proposes
the reconciling edit. It is manual and on-demand here; continuous drift detection
and automated PRs are the commercial product.

## The model: baseline, detect, reconcile

Drift is measured against a **baseline** (the `.dex/snapshot.json` fingerprint of
the warehouse map and the project's per-layer definitions). Detection is
read-only; only reconcile proposes edits.

**Snapshot discipline matters.** A snapshot is only as trustworthy as the moment
it froze. Take one right after a known-good build (`maintain snapshot`), and
**commit `.dex/snapshot.json` like a lockfile** so the whole team diffs against
the same reference. Snapshot a state that is already drifted and `check` will
mask the very drift you care about. When you accept a change as the new normal
(re-run `explore map` first, then `maintain snapshot`); `check` warns when the
baseline looks stale.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

- `maintain snapshot` captures or refreshes the baseline. Run it after a clean
  explore or transform session so later runs have a known-good reference. It pins
  the current `.dex/cache.json` (so the grain baseline is the exact-distinct
  verdicts `explore map` already computed) plus per-layer fingerprints of the dbt
  project. Without a cache it captures a metadata-only baseline and says so.
- `maintain check` is the everyday entry point: it sweeps every axis and returns
  a report ranked by blast radius. Read-only.
- `maintain schema [<objects>]` detects **structural drift**: source columns and
  tables added, dropped, retyped, or renamed; nullability changes; declared
  sources the warehouse no longer honors.
- `maintain volume [<objects>]` detects **freshness drift**: row counts that
  collapsed, spiked, or went to zero. This is the "is the data still flowing
  correctly?" axis, distinct from "did the shape change?".
- `maintain grain [<objects>]` detects **grain drift**: a declared primary or
  unique key that now has duplicates, a changed row-per-entity cardinality, or an
  increased join fanout. Uses aggregates, never raw rows.
- `maintain semantic [<objects>]` detects **definition drift**: metric, measure,
  dimension, or entity definitions that changed against the baseline; semantic
  references that no longer resolve to a model or column; and categorical
  dimensions whose set of values widened or narrowed underneath their metrics.
- `maintain reconcile [<class>]` proposes the dbt edits that bring the project
  back in sync, as reviewable diffs. Optionally scope it to one class (`schema`,
  `volume`, `grain`, or `semantic`).

The usual flow: `check` to triage, a focused detector to understand one axis in
depth, then `reconcile` to get the proposed fix.

## Per-axis cost: what is free and what scans

Detection is read-only, but read-only is not the same as free on a metered
connector (BigQuery, Snowflake, Postgres). The axes split:

- **Schema, volume, and the reference/definition half of semantic are free**
  everywhere: they read metadata and the snapshot, and run immediately.
- **Grain and the dimension-cardinality half of semantic scan the warehouse**, so
  on a metered connector they run the two-step handshake. The first call returns
  `needs_confirmation` with an estimate in `cost.estimate` (and a per-table
  breakdown). Surface it to the user in human units, get an explicit budget, and
  re-issue the same command with `--confirm --budget <magnitude>` in the
  paradigm's unit (bytes on BigQuery, warehouse-seconds on Snowflake,
  database-seconds on Postgres). Never invent a budget the user did not agree
  to, and never retry with a raised budget on an over-ceiling refusal without
  asking.
- **`check` is two-phase on a metered connector**: the free axes complete
  immediately and their findings ride along in the `needs_confirmation` envelope,
  with one combined estimate for the scanning axes. Confirm to complete the sweep.

On DuckDB everything is free and local, so nothing prompts.

## Reconcile proposals are mechanical or advisory

Reconcile tags every proposal by `kind`, because the fix differs sharply by axis:

- **`mechanical`**: schema drift on a dex-scaffolded staging model re-scaffolds
  the model from the drifted source. High-confidence, but still a reviewable diff:
  read it for hand-written logic the scaffold cannot know about.
- **`advisory`**: grain, volume, and semantic drift are decisions, not auto-fixes
  (dex cannot dedup your warehouse or decide whether a new `'refunded'` status
  belongs in a metric). The proposal is the decision surfaced, at most backed by a
  test edit that makes the break visible in builds.

When reconcile produces edits it stores them as a plan and prints a `plan_id`.
Apply them with `transform apply <plan-id>` (the one apply door): a human edit made
since detection surfaces as a conflict, never a silent overwrite.

## Guardrails (enforced in the engine, not here)

- Read-only against data. Schema, volume, and semantic references are computed from
  metadata and the snapshot; grain and dimension-cardinality use aggregates only.
  Raw rows and dimension values never cross the envelope.
- Propose, don't impose. Reconciliation is always a reviewable diff, applied
  through `transform apply`. Human dbt edits are authoritative; on conflict the
  engine surfaces the divergence and asks rather than overwriting.
- The dbt project is the source of truth; the `.dex/` snapshot is a non-canonical
  fingerprint used only to detect change.
