---
name: explore
description: Use this to make sense of a database, warehouse, or DuckDB file: inventory and rank what is there, profile columns, detect PII, flag grain and data-quality problems, and infer how tables join, producing a draft map without dumping the schema into context. Trigger it for casual, artifact-first prompts like "what's in my duckdb", "what's in this database", "what data do I have", "take a look at data.duckdb", or "any PII in here", as well as analyst questions like "what is in this warehouse", "which tables matter", "what does this table contain", "how do these tables relate", "is this data any good", or "profile these columns". Any mention of exploring, inspecting, or understanding a .duckdb or .db file, a warehouse connection, or unfamiliar data qualifies. This is read-only sense-making and writes nothing but the .dex/ cache. Do not use it to author or change dbt models or the semantic layer (use transform) or to detect drift and reconcile a project (use maintain).
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
3. `explore profile <objects>` (space- or comma-separated) returns column
   profiles, PII flags recorded as (column, category, confidence) and never
   example values, plus candidate keys, the likely grain, and data-quality
   warnings (e.g. a non-unique id that will fan out on joins).
4. `explore relationships` returns inferred and declared joins with confidences,
   plus notes explaining what the inference examined (so an empty list is
   meaningful).
5. `explore map` writes or updates the `.dex/` cache and prints a summary.

## Guardrails (enforced in the engine, not here)

- Read-only against data. The connection is opened read-only and generated SQL is
  SELECT-only. Never propose a write to source data.
- Sense-making, not enumeration. Rank and drill selectively; never paste a full
  schema into context.
- Profile, don't exfiltrate. Understanding comes from aggregates. Raw rows never
  cross the envelope, and PII is flagged, never surfaced.
