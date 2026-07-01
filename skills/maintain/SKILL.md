---
name: maintain
description: Use this to keep a dbt project correct as the warehouse and the business change. It detects drift on three axes and proposes the fix: schema drift (source columns and tables added, dropped, retyped, or renamed), grain drift (a key that lost uniqueness, a changed row-per-entity cardinality, an increased join fanout), and semantic drift (a metric, measure, dimension, or entity definition that no longer matches, new categorical values, dangling semantic references). Trigger it for requests like "what changed in the warehouse", "did anything drift", "is my dbt project still in sync", "my primary key has duplicates now", "the revenue metric definition changed", "reconcile my models with the source schema", or "which models are stale". It reads the .dex/ snapshot and proposes reviewable diffs; it never overwrites hand-written work. Do not use it to author new dbt models or metrics from scratch (use transform) or to explore an unfamiliar warehouse (use explore).
---

# Maintain

Keep the dbt project correct as the world underneath it moves. Maintenance is the
recurring half of the loop: warehouses drift, models go stale, keys stop being
unique, and business definitions change. This skill compares a known-good baseline
against current reality, classifies what drifted, and proposes the reconciling
edit. It is manual and on-demand here; continuous drift detection and automated
PRs are the commercial product.

## The model: baseline, detect, reconcile

Drift is measured against a **baseline** (the `.dex/snapshot.json` fingerprint of
the warehouse schema, the dbt manifest, and the declared grain and semantic
assumptions). Detection is read-only; only reconcile proposes edits.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

- `maintain snapshot` captures or refreshes the baseline. Run it after a clean
  explore or transform session so later runs have a known-good reference to diff
  against. Everything else needs a snapshot to exist.
- `maintain check` is the everyday entry point: it sweeps every drift axis against
  the snapshot and returns a categorized report, ranked by blast radius. Read-only.
- `maintain schema [<objects>]` detects **structural drift**: source columns and
  tables added, dropped, retyped, or renamed; nullability changes; sources that no
  longer match the warehouse.
- `maintain grain [<objects>]` detects **grain drift**: a declared primary or
  unique key that now has duplicates, a changed row-per-entity cardinality, or an
  increased join fanout. Uses aggregates, never raw rows.
- `maintain semantic [<objects>]` detects **definition drift**: a metric, measure,
  dimension, or entity whose definition no longer matches the baseline; new
  categorical dimension values; and semantic references that no longer resolve to a
  model or column.
- `maintain reconcile [<class>]` proposes the dbt edits that bring the project back
  in sync with detected drift, as reviewable diffs. Nothing is applied. Optionally
  scope it to one class (`schema`, `grain`, or `semantic`).

The usual flow: `check` to triage, a focused detector to understand one axis in
depth, then `reconcile` to get the proposed fix as a diff.

## Guardrails (enforced in the engine, not here)

- Read-only against data. Structural and semantic drift are computed from metadata
  and the snapshot; grain drift uses aggregates only. Raw rows never cross the
  envelope.
- Propose, don't impose. Reconciliation is always a reviewable diff. Human dbt
  edits are authoritative; on conflict the engine surfaces the divergence and asks
  rather than overwriting.
- The dbt project is the source of truth; the `.dex/` snapshot is a non-canonical
  fingerprint used only to detect change.
