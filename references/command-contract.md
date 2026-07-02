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

Capabilities, not final spelling. Implemented incrementally: `connect test` is
real in Phase 0; the rest return a valid `not_implemented` envelope until their
phase lands.

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
dex viz preview                   -> emit the dbt semantic model to the free Viz preview
```

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
  spend returns `needs_confirmation` unless given `--confirm` and a `--budget`.
- **Diffs, not silent writes.** Proposed changes appear in `diffs`; being there
  does not apply them. The user applies through their normal review and PR flow.
- **No secrets, no uncleared values.** `data` is scanned before printing
  (`envelope.sanitize`): a secret-like key or a record-shaped raw-row payload is
  a hard failure, never a silent scrub. Result values appear only in `explore
  query`'s columnar payload, and only after the query firewall has proven they
  come from profiled, PII-cleared columns. This is a release-blocking safety
  guarantee.

## Why this is the first artifact

Because every skill, test, and benchmark depends on it, the contract is locked
before the engine logic is built. Phase 0 ships the contract, a real `connect
test`, and the sanitized envelope; Phases 1 through 3 fill in the subcommands
against this fixed boundary.
