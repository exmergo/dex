# Connector: Databricks

The lakehouse connector, compute-time billed over Unity Catalog. Namespace:
`catalog.schema.table` (three-level). Cost paradigm: **warehouse-seconds**
(DBUs shown alongside). Read-only against data, enforced in depth.

## Authentication: discover, don't ask

The engine discovers a connection at runtime through the Databricks SDK's
unified auth chain and never prompts for or persists a token. Discovery order:

1. `databricks.profile` in `.dex/config.yml`, naming a `~/.databrickscfg`
   entry (the store `databricks auth login` writes)
2. `DATABRICKS_*` environment variables (`DATABRICKS_HOST` plus a credential:
   `DATABRICKS_TOKEN`, OAuth M2M client variables, or the GitHub OIDC
   federation CI exchanges into a token)
3. the default `~/.databrickscfg` profile (a `[DEFAULT]` section, or the
   newer CLI's `default_profile` pointer)
4. the `host` of a `type: databricks` target in a discovered dbt
   `profiles.yml`

The CLI's OAuth cache, PATs, OAuth M2M, and workload identity federation all
arrive through the same SDK door; only the coarse method (for example
`default_profile:oauth_user`) is ever surfaced. Identities and token values
never cross the envelope. Every discovery failure names the fix.

## Config

```yaml
# .dex/config.yml
connector: databricks
databricks:
  profile: null                  # a ~/.databrickscfg entry; optional
  warehouse: abc123def456        # REQUIRED for anything billed; ID or http_path
  catalogs:                      # source allowlist; empty means all visible
    - samples.tpch               # catalog or catalog.schema entries
  dev_catalog: dex_ci            # where dbt dev builds write (never a source)
  dev_schema: dbt_dev            # default dbt_dev
  max_full_profile_bytes: null   # opt-in TABLESAMPLE threshold for huge tables
  dbu_price_usd: null            # your contract price; set it to see dollars
budget:
  ceiling: 300                   # per-command warehouse-seconds; --budget overrides
  session_ceiling: 3600          # cumulative seconds per UTC day
```

**The warehouse pin is strict.** Billed statements run only on
`databricks.warehouse` (a SQL warehouse ID or its
`/sql/1.0/warehouses/...` HTTP path); an unpinned or unresolvable warehouse
refuses with the fix named. Pin the smallest warehouse that works (a 2X-Small
serverless with the minimum auto-stop is the intended shape;
`scripts/setup_databricks_ci.sh` provisions exactly that).

`transform init` content-checks every schema the new project would build into
through Unity Catalog REST only, so the pinned warehouse is never woken: the
base `dev_catalog.dev_schema`, plus the sibling layer schemas (`staging_dev`,
`intermediate_dev`, `marts_dev` on the `dev` target) when `--layered-schemas`
is on. A schema that already holds tables or views is warned about, never
refused.

## Cost model: two clients, one guarded quantity

Metadata is free and scans cost warehouse runtime, so dex guards
**warehouse-seconds**, not bytes, and splits its clients to keep the free
paths provably free:

- **Free (SDK REST, no SQL session):** `connect test`, `explore inventory`,
  all catalog/schema/table/column metadata, warehouse facts, estimation, and
  `maintain check` (drift detection reads metadata only). The SQL session is
  opened lazily by the first billed statement, because opening one lands on
  the warehouse and can wake it; free commands leave a STOPPED warehouse
  stopped.
- **Billed (SQL session on the pinned warehouse):** profiling aggregates,
  `explore query`, relationship verification probes, DESCRIBE DETAIL size
  probes, and `transform build`.

Budgets (`budget.ceiling`, `--budget`, `budget.session_ceiling`) are
warehouse-seconds: the number you budget is the number the server enforces.
Every cost surface also carries the translation to **DBUs**
(`seconds x dbu_per_hour / 3600`, rate from the warehouse size, marked
approximate: published rates move and differ by type and cloud), and to
dollars when `databricks.dbu_price_usd` is set. dex never guesses a dollar
price: it varies by cloud, tier, and contract.

**Estimates start as an honest floor and refine in-budget.** Databricks has
no dry-run and Unity Catalog exposes no free row or byte counts, so the first
handshake quotes a conservative per-statement floor, labeled
`estimate_quality: "low"`, plus a startup floor when the pinned warehouse is
not running (seconds for serverless, minutes for classic). Once you confirm a
budget, the adapter runs an engine-built `DESCRIBE DETAIL` per table, charged
inside the confirmed budget, never before it, to learn `sizeInBytes`: that
sharpens later estimates, drives the sampling decision, and is noted per
table when unavailable (some shared tables refuse it). The same estimator
prices `transform build`: each compiled model, snapshot, and test is estimated
and summed into the build's upfront cost, labeled `estimate_quality: "low"`
like any other Databricks estimate.

**The budget is hard-enforced regardless of estimate quality.** Before every
billed statement the session's `STATEMENT_TIMEOUT` is set to the remaining
budget, so a weak floor cannot overrun the ceiling: Databricks kills the
statement and dex reports the over-ceiling refusal. Actual spend is
wall-clock seconds per statement (including any wake the statement caused),
recorded to `.dex/spend.jsonl` as `billed_seconds` and summed into the daily
session ceiling. Connections identify themselves with a `dex` user-agent for
attribution.

The handshake is the same strict two-step as every billed connector: a
scanning command without `--confirm` returns `needs_confirmation` carrying
the seconds estimate (per table where relevant) and its DBU translation;
re-issue with `--confirm --budget <seconds>`. Nothing executes unconfirmed or
without a ceiling, and an estimate over the ceiling is refused outright.

## Read-only, enforced in depth

- Every data statement passes the SELECT-only guard parsed in the databricks
  dialect through one execution door (DML, DDL, `MERGE`, `COPY INTO`,
  `OPTIMIZE`, `VACUUM`, and multi-statement forms all refuse).
- The adapter issues no mutating calls: Unity Catalog REST reads, `SET
  STATEMENT_TIMEOUT`, `DESCRIBE DETAIL`, and guarded SELECTs only.
- The documented grant shape is least-privilege: `CAN USE` on the pinned
  warehouse, `USE CATALOG` + `USE SCHEMA` + `SELECT` on source catalogs, and
  write only on the dedicated dev catalog
  (`scripts/setup_databricks_ci.sh` creates this shape).
- dbt dev builds write to `dev_catalog.dev_schema`, which is refused as a
  source, and the generated profile pins the warehouse's HTTP path and one
  thread. A user OAuth connection renders dbt's own OAuth flow; a token
  connection renders a `DATABRICKS_TOKEN` env reference, never a value.

## Profiling behavior

Profiling is batched aggregates (COUNT, APPROX_COUNT_DISTINCT, min/max only
for engine-cleared safe columns), never row values. Nested and
semi-structured columns (ARRAY, MAP, STRUCT, VARIANT, BINARY, GEOGRAPHY,
GEOMETRY) degrade to non-null counts. Tables above
`databricks.max_full_profile_bytes` are profiled from a sample
(`TABLESAMPLE`), noted, with uniqueness not judged; the threshold binds once
DESCRIBE DETAIL has learned a size. Exact distinct-count escalation spends
only inside the confirmed budget and degrades to approximate verdicts with a
note when the remainder cannot cover it.

## Testing

Offline: a stateful fake pair (`tests/fakes/databricks.py`): a Unity Catalog
workspace client serving metadata and a DBAPI connection that records
statement ordering, simulates timing on an injected clock, and enforces
STATEMENT_TIMEOUT with the connector's real error types, behind a counting
factory that proves free paths never open a SQL session. Live: an env-gated
integration suite (`DEX_TEST_DATABRICKS_*`) reading the samples catalog and
writing only to the scratch catalog, run in CI on a schedule via an OIDC
federation policy (no stored keys), never as a merge or release gate.
