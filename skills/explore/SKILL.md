---
name: explore
description: 'Use this to make sense of a database, warehouse, or DuckDB file: inventory and rank what is there, profile columns, detect PII, flag grain and data-quality problems, infer and verify how tables join, and answer ad-hoc data questions with guarded SQL probes, producing a draft map without dumping the schema into context. Trigger it for casual, artifact-first prompts like "what''s in my duckdb", "what''s in this database", "what data do I have", "take a look at data.duckdb", or "any PII in here", as well as analyst questions like "what is in this warehouse", "which tables matter", "what does this table contain", "how do these tables relate", "is this data any good", "profile these columns", or ad-hoc counts and distributions like "how many orders have no customer". Any mention of exploring, inspecting, querying, or understanding a .duckdb or .db file, a warehouse connection, or unfamiliar data qualifies. This is read-only sense-making and writes nothing but the .dex/ cache. Do not use it to author or change dbt models or the semantic layer (use transform) or to detect drift and reconcile a project (use maintain).'
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
   warnings (e.g. a non-unique id that will fan out on joins). Distinct counts
   are approximate for scale, but any column that looks unique within
   approximation noise is escalated to an exact COUNT(DISTINCT)
   (`distinct_count_exact: true`), so uniqueness and grain verdicts rest on
   proof; a `~` prefix in a warning marks a count that is still approximate.
4. `explore relationships` returns inferred and declared joins with confidences,
   plus notes explaining what the inference examined (so an empty list is
   meaningful). Add `--verify` to measure each inferred join with an aggregate
   overlap probe (orphan fraction, confidence adjusted).
5. `explore map` writes or updates the `.dex/` cache and prints a summary
   (`--verify` works here too). Past 50 objects it profiles only the top 25 by
   rank and says so in `notes` (with `skipped_count`); pass `--full` to profile
   everything. On a re-map, objects skipped this run keep their prior profiles
   (`carried_forward_count`), each stamped with its own `profiled_at` so
   staleness is visible instead of column detail silently vanishing.
6. `explore query "<SELECT ...>"` answers an ad-hoc question the fixed commands
   don't cover: you write the SQL, the engine's query firewall refuses or bounds
   it. Requires the `.dex/` cache (run `map` first). Results come back columnar
   and capped; a refusal names the offending column and the fix, so one rewrite
   is enough. Read `${CLAUDE_SKILL_DIR}/references/probe-playbook.md` before
   writing a probe: it maps common questions to effective probe shapes.

Rules of engagement for `query`: prefer the fixed commands when they answer the
question; one probe answers one question; batch related measures into a single
query rather than issuing many; aggregates over PII-flagged columns must be
measuring (COUNT, APPROX_COUNT_DISTINCT, AVG(LENGTH(...))), never value-carrying
(MIN, ANY_VALUE, STRING_AGG). Never fall back to raw Python or a database CLI to
run SQL; the firewall path is the only sanctioned one.

## Cloud and database targets (BigQuery, Snowflake, Databricks, Postgres, Redshift)

A remote warehouse or database replaces `--path` with connector config. Start
with `connect test --connector <name>` (or set `connector:` plus the matching
block in `.dex/config.yml`: `bigquery:` with `project` and a `datasets`
allowlist, `snowflake:` with the pinned `warehouse` and a `databases`
allowlist, `databricks:` with the pinned SQL `warehouse` and a `catalogs`
allowlist, `postgres:` with a `schemas` allowlist, `redshift:` with the
Serverless `workgroup` and a `schemas` allowlist). Credentials are
discovered, never asked for: if the envelope reports missing or expired
credentials, relay the fix it names (for BigQuery
`gcloud auth application-default login`; for Snowflake a `connections.toml`
entry or `SNOWFLAKE_*` env; for Databricks `databricks auth login` or
`DATABRICKS_*` env; for Postgres `DATABASE_URL`, `PG*` env, or a
`pg_service.conf` entry; for Redshift the AWS credential chain
(`aws configure`, `AWS_*` env) or `REDSHIFT_*` env) and never ask the user to
paste a key, token, or password.

On a metered connector, scanning commands (`profile`, `map`, `relationships`,
`query`) run a two-step handshake. The first call returns
`needs_confirmation` with an estimate in `cost.estimate` (and a per-table
breakdown where relevant): an exact dry-run byte figure on BigQuery, a
heuristic labeled `estimate_quality: "heuristic"` in warehouse-seconds on
Snowflake (credits alongside), a floor labeled `estimate_quality: "low"` in
warehouse-seconds on Databricks (DBUs alongside; it sharpens itself inside
the confirmed budget), a heuristic in compute-seconds on Redshift (RPU-hours
alongside; Serverless estimates carry the 60-second wake minimum once), and
database-seconds on Postgres (no dollars; the guarded quantity is load on
the operational database). Surface the
estimate to the user in human units, get an explicit budget from them, and
re-issue the same command with `--confirm` and `--budget <magnitude>` in the
paradigm's unit. Never invent a budget the user did not agree to, and never
retry with a raised budget on an over-ceiling refusal without asking.
Metadata is free (`connect test`, `inventory` run immediately), and OK
envelopes report actual spend under `data.spend`.

When an estimate is larger than the work deserves, narrow the scope rather than
raise the budget. `--scope` (repeatable) bounds a command to part of the
configured source allowlist, in the connector's own vocabulary: a dataset on
BigQuery, a `schema` or `database.schema` on Snowflake, a `catalog.schema` on
Databricks, a schema on Postgres or Redshift. It is free to resolve, it can only narrow what
`.dex/config.yml` already allows, and a scope that names nothing is refused with
the schemas that do exist listed. So `explore map --scope <schema>` is the first
thing to reach for on a warehouse whose full map would be expensive.

## Guardrails (enforced in the engine, not here)

- Read-only against data. The connection is opened read-only and generated SQL is
  SELECT-only. Never propose a write to source data.
- Sense-making, not enumeration. Rank and drill selectively; never paste a full
  schema into context.
- Profile, don't exfiltrate. Understanding comes from aggregates. PII is flagged,
  never surfaced, and the query firewall enforces it on your own SQL: values
  cross the envelope only from profiled, PII-cleared columns, bounded and capped.
