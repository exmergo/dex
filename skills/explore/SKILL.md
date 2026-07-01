---
name: explore
description: Use this to make sense of an unfamiliar data warehouse or DuckDB database: inventory and rank what is there, profile columns, detect PII, and infer how tables join, producing a draft map of the warehouse without dumping the schema into context. Trigger it for questions like "what is in this warehouse", "which tables matter", "what does this table contain", "how do these tables relate", or "profile these columns". This is read-only sense-making and writes nothing but the .dex/ cache. Do not use it to author or change dbt models or the semantic layer (use transform) or to detect drift and reconcile a project (use maintain).
---

# Explore

Make sense of a warehouse or a local DuckDB database the way an analytics
engineer does: rank what matters, drill selectively, and persist a draft map.
This is the flagship, fully read-only skill. It absorbs profiling and
relationship inference as capabilities; they are not separate skills.

## How to drive it

Run the engine through the wrapper. It prints one sanitized JSON envelope and
nothing else; read the envelope and decide the next step.

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

Subcommands, in the usual order:

1. `connect test --path <file.duckdb>` confirms a read-only connection and
   reports capabilities.
2. `explore inventory --rank` returns a ranked object summary (counts and sizes,
   never rows).
3. `explore profile <objects>` returns column profiles and PII flags recorded as
   (column, category, confidence), never example values.
4. `explore relationships` returns inferred and declared joins.
5. `explore map` writes or updates the `.dex/` cache and prints a summary.

## Guardrails (enforced in the engine, not here)

- Read-only against data. The connection is opened read-only and generated SQL is
  SELECT-only. Never propose a write to source data.
- Sense-making, not enumeration. Rank and drill selectively; never paste a full
  schema into context.
- Profile, don't exfiltrate. Understanding comes from aggregates. Raw rows never
  cross the envelope, and PII is flagged, never surfaced.
