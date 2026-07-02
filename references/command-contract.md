# The dex-core command contract

This is the integration keystone. It is the boundary between the agent
and the engine, and it is what keeps every agent surface thin: each surface calls
the same subcommands and reads the same envelope, so no surface re-implements
logic.

## Shape of the boundary

- A surface (SKILL.md or AGENTS.md) tells the agent which subcommand to run.
- A thin PEP 723 wrapper (`skills/<skill>/scripts/run.py`) runs it via `uv run`
  against the pinned engine version, installing the connector extra it resolves at
  runtime (an explicit `--connector`, then `.dex/config.yml`, then DuckDB), so the
  pin stays connector-neutral.
- The engine prints **exactly one** sanitized JSON envelope to stdout and nothing
  else. Diagnostics go to stderr.
- The agent reads the envelope and decides the next step.

State persists in the dbt project (the source of truth) and the `.dex/` cache, so
subcommands are **stateless**: the agent orchestrates multi-step flows by
re-reading them between calls. Credentials never cross this boundary, and nothing
reaches agent context except through the sanitized envelope: values cross only
from profiled, PII-cleared columns, bounded and capped by the query firewall.

## The command surface

Capabilities, not final spelling. Implemented incrementally: `connect test`, the
`explore` group, and the authoring surface (`transform`, `semantic`, `emit dbt`)
are live; the `maintain` group, `emit osi`, and `viz preview` return a valid
`not_implemented` envelope until they land.

```
dex connect test                  -> {capabilities, dialect, read_only: true}
dex explore inventory [--rank]    -> ranked object summary (counts, sizes; no rows)
dex explore profile <objects>     -> column profiles + PII flags + candidate keys, grain, data-quality warnings
dex explore relationships         -> inferred + declared joins with confidences + inference notes
dex explore map                   -> write/update the .dex cache; print a summary
dex explore query "<SELECT ...>"  -> run one agent-authored SELECT through the query firewall
dex transform plan "<intent>"     -> proposed dbt edits as diffs (nothing applied yet)
dex transform apply <plan-id>     -> write diffs into the dbt project (a reviewable git diff)
dex transform build --target dev  -> cost preflight FIRST; runs only with --confirm and a budget
dex semantic define|update ...    -> dbt semantic model edits as diffs (fronted by transform)
dex emit dbt                      -> write/refresh dbt semantic YAML from the semantic edits
dex maintain snapshot             -> capture/refresh the known-good baseline in .dex/snapshot.json
dex maintain check                -> sweep every drift axis vs the snapshot; ranked drift report (read-only)
dex maintain schema [<objects>]   -> structural drift: columns/tables added, dropped, retyped, renamed; nullability
dex maintain grain [<objects>]    -> cardinality/identity drift: lost key uniqueness, changed grain, join fanout
dex maintain semantic [<objects>] -> definition drift: metric/measure/dimension/entity defs, new values, dangling refs
dex maintain reconcile [<class>]  -> propose the dbt edits that reconcile detected drift, as diffs (never applied)
dex viz preview                   -> emit the dbt semantic model to the Viz preview (not yet implemented;
                                     the Viz integration arrives later)
```

### How authored content reaches the engine

The engine has no model of its own; the agent authors the dbt file content and
hands it over via `--edits-file <path>` (or `-` for stdin), a JSON payload:

```json
{"edits": [
  {"path": "models/staging/stg_orders.sql", "kind": "model_sql", "content": "..."},
  {"path": "models/staging/stg_orders.yml", "kind": "schema_yml", "content": "..."}
]}
```

`kind` is one of `model_sql`, `schema_yml`, `semantic_yml` (optional on
`semantic define|update`, which imply `semantic_yml`). The engine validates each
edit (model SQL must be a single read-only SELECT once jinja is stripped; YAML
must parse; semantic YAML must satisfy MetricFlow's schemas), pins it to the
sha256 of the file it would change, computes the diffs, and stores the plan under
`.dex/plans/<plan-id>.json`. Nothing touches the dbt project until an apply.

- `transform plan` also accepts `--scaffold <table>` (repeatable): a
  deterministic staging skeleton (`stg_<table>.sql` plus per-model YAML with key
  tests and PII flags in column `meta`) generated from the `.dex/` cache.
- `transform apply <plan-id>` re-hashes every file first. A file edited by a
  human after the plan was made is a **conflict**: nothing is written, the
  divergence is returned as diffs with `needs_confirmation`, and only an explicit
  `--confirm` overrides it. A clean apply is all-or-nothing.
- `transform build` accepts `--target` and `--select`. The target must be `dev`
  (or the `dbt_target` named in `.dex/config.yml`); production-looking targets
  are refused outright, before the cost gate, and `--confirm` cannot override
  the refusal.
- `semantic define` refuses names that already exist in the project (use
  `update`); `update` refuses names that do not (use `define`). `emit dbt`
  applies a semantic plan (the latest unapplied one, or an explicit plan id).

Skill-to-subcommand mapping: `explore` fronts `connect`/`explore`; `transform`
fronts `transform`, `semantic`, `emit`, and `viz`; `maintain` fronts the whole
`maintain` group. Within `maintain`, `snapshot` manages the baseline, `check`
plus `schema`/`grain`/`semantic` detect drift (read-only), and `reconcile` is the
only verb that emits diffs.

`explore relationships` and `explore map` accept `--verify`, which measures each
inferred join with one aggregate overlap probe (non-null foreign keys, orphan
count) and adjusts its confidence; the result carries `verified` and
`orphan_fraction`.

Global flags (shared resolution path): `--connector`, `--path` (DuckDB),
`--repo-root`, `--confirm`, `--budget`.

## The query firewall

`explore query` executes SQL the agent wrote; the engine generates nothing and
only refuses or bounds. The gate, in order:

1. **Parse, don't trust.** A single read-only SELECT, structurally checked.
   Writes, DDL, multi-statement input, PRAGMA and DESCRIBE are refused
   (introspection goes through `inventory`/`profile`).
2. **Resolve against the cache.** Every table and column must exist in
   `.dex/cache.json`; no cache or an unprofiled object refuses with the fix
   ("run `explore map` first"). Profiling is what makes the PII policy
   computable, so probing requires it.
3. **Classify the projection.** Output may carry values only from profiled,
   unflagged columns. Every value path from a PII-flagged column must pass
   through a measuring aggregate (COUNT, APPROX_COUNT_DISTINCT, AVG, SUM,
   STDDEV, ...). Value-carrying aggregates (MIN, MAX, ANY_VALUE, STRING_AGG,
   ...) do not qualify, unknown functions fail closed, and `SELECT *` is refused
   when the expansion includes a flagged column. Filters, join conditions,
   GROUP BY and ORDER BY are unrestricted: values flow in, not out.
4. **Bound the result.** LIMIT is clamped (default 50 rows), long cells are cut
   (default 256 chars), the payload is byte-capped (default 16 KiB), and every
   cut is announced in `notes`. A watchdog interrupts queries that outlive
   their time budget (default 30s). All four are configurable under `query:` in
   `.dex/config.yml`.
5. **Record.** Every decision, allowed, refused, or failed, is appended to
   `.dex/queries.jsonl` (SQL text and counts, never result values).

Results are columnar (`columns`, `cells` as a list of lists, `row_count`,
`truncated`, `notes`), which is cheaper in tokens than records and keeps the
envelope sanitizer's list-of-dicts raw-row rule intact as a backstop.

## The envelope

Every command prints one object of this shape (`exmergo_dex_core.envelope`):

```json
{
  "status": "ok | not_implemented | error | needs_confirmation",
  "data": {},
  "cost": { "estimate": null, "ceiling": null, "paradigm": "free_local" },
  "warnings": [],
  "diffs": [],
  "errors": []
}
```

Rules the envelope enforces, all of them Tier-2 eval targets:

- **Cost before spend.** `cost` is a preflight estimate. Any command that would
  spend returns `needs_confirmation` unless given `--confirm` (and a `--budget`
  on billed connectors; DuckDB is free, so the confirm handshake alone gates it).
  An estimate over the ceiling is refused outright; confirmation cannot override
  it.
- **Diffs, not silent writes.** Proposed changes appear in `diffs`; being there
  does not apply them. The user applies through their normal review and PR flow.
- **No secrets, no uncleared values.** `data` is scanned before printing
  (`envelope.sanitize`): a secret-like key or a record-shaped raw-row payload is
  a hard failure, never a silent scrub. Result values appear only in `explore
  query`'s columnar payload, and only after the query firewall has proven they
  come from profiled, PII-cleared columns. This is a release-blocking safety
  guarantee.

## Why this is the first artifact

Because every skill, test, and benchmark depends on it, the contract was locked
before the engine logic was built, and the subcommands fill in against this
fixed boundary. Exploration and authoring are live today; maintain is the next
group to land.
