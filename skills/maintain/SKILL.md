---
name: maintain
description: Use this to keep a dbt project correct as the warehouse and the business change: detect schema drift and semantic or metric-definition drift, diff the current warehouse and dbt against the last known-good snapshot, and propose the edits that reconcile them. Trigger it for requests like "what changed in the warehouse", "did anything drift", "is my dbt project still in sync", "reconcile my models with the source schema", or "which models are stale". It reads the .dex/ snapshot and proposes reviewable diffs; it never overwrites hand-written work. Do not use it to author new dbt models or metrics from scratch (use transform) or to explore an unfamiliar warehouse (use explore).
---

# Maintain

Keep the dbt project correct as the world underneath it moves. Maintenance is the
recurring half of the loop: warehouses drift, models go stale, and business
definitions change, so this skill detects that drift and proposes the edits that
bring dbt back in sync. It is manual and on-demand here; continuous drift
detection and automated PRs are the commercial product.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

- `reconcile` diffs the current warehouse and dbt project against the last `.dex/`
  snapshot and reports what changed (schema drift, new or dropped columns, changed
  types, and semantic or definition drift), with proposed edits as reviewable
  diffs. Nothing is applied.

Run `explore map` (the `explore` skill) first if there is no snapshot yet;
reconcile needs a prior fingerprint to diff against.

## Guardrails (enforced in the engine, not here)

- Read-only against data. Drift is computed from metadata and the `.dex/`
  snapshot, never by scanning source rows.
- Propose, don't impose. Every proposed reconciliation is a reviewable diff. Human
  dbt edits are authoritative; on conflict the engine surfaces the divergence and
  asks rather than overwriting.
- The dbt project is the source of truth; the `.dex/` snapshot is a non-canonical
  fingerprint used only to detect change.
