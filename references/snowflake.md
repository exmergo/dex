# Connector: Snowflake

The first compute-time billed connector. Namespace: `database.schema.table`.
Cost paradigm: **warehouse-seconds** (credits shown alongside). Read-only
against data, enforced in depth.

## Authentication: discover, don't ask

The engine discovers a connection at runtime and never prompts for or persists
a password. Discovery order:

1. `snowflake.connection_name` in `.dex/config.yml`, naming a
   `~/.snowflake/connections.toml` entry (the store the `snow` CLI writes)
2. the default connection (`default_connection_name` in `config.toml`, or
   `SNOWFLAKE_DEFAULT_CONNECTION_NAME`)
3. `SNOWFLAKE_*` environment variables (`SNOWFLAKE_ACCOUNT` +
   `SNOWFLAKE_USER` plus a credential; this is also how CI's
   workload-identity token arrives: `SNOWFLAKE_AUTHENTICATOR=WORKLOAD_IDENTITY`,
   `SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER=OIDC`, `SNOWFLAKE_TOKEN=<jwt>`)
4. the `account` of a `type: snowflake` target in a discovered dbt
   `profiles.yml`

Key-pair, SSO (`externalbrowser`), password, token, and workload-identity
credentials all work; only the coarse method (for example
`named_connection:key_pair`) is ever surfaced. Identities, passwords, and key
material never cross the envelope. Every discovery failure names the fix.

One caveat: workload identity covers the engine (explore, maintain, query)
but not dbt builds, because stable dbt-snowflake does not support it yet.
`transform init` refuses a workload-identity connection and names the
alternatives (key-pair or SSO); the refusal lifts once dbt-snowflake ships
support.

## Config

```yaml
# .dex/config.yml
connector: snowflake
snowflake:
  connection_name: dex-ci        # a connections.toml entry; optional
  warehouse: DEX_CI_WH           # REQUIRED for anything billed; the pin is strict
  databases:                     # source allowlist; empty means all visible
    - SNOWFLAKE_SAMPLE_DATA.TPCH_SF1   # db or db.schema entries
  dev_database: DEX_CI           # where dbt dev builds write (never a source)
  dev_schema: DBT_DEV            # default DBT_DEV
  max_full_profile_bytes: null   # opt-in SAMPLE threshold for huge tables
  credit_price_usd: null         # your contract price; set it to see dollars
budget:
  ceiling: 300                   # per-command warehouse-seconds; --budget overrides
  session_ceiling: 3600          # cumulative seconds per UTC day
```

**The warehouse pin is strict.** Billed statements run only on
`snowflake.warehouse`; a connection-level default warehouse is never spent on,
and a missing or unpinned warehouse refuses with the fix named. Pin the
smallest warehouse that works (X-Small with 60s auto-suspend is the intended
shape; `scripts/setup_snowflake_ci.sh` provisions exactly that plus a resource
monitor as the hard monthly backstop).

## Scoping a command

`snowflake.databases` is the committed source allowlist, and a single database
can span four orders of magnitude in table size, so prefer `db.schema` entries
over bare `db` when the difference matters for cost.

`--scope` narrows that allowlist for one command, and is repeatable. It accepts
a bare schema (`--scope TPCH_SF1`, qualified against the databases already in
scope), a database, or a qualified `database.schema`. Resolution is free (SHOW
metadata, no warehouse) and happens before anything is estimated:

- A scope that names no database and no schema is **refused**, and the error
  lists the schemas that do exist. It is never quietly dropped, because an
  estimate that silently spans the whole allowlist is one a user could confirm
  believing it bounded a handful of tables.
- A bare schema that exists in more than one in-scope database is refused as
  ambiguous, asking for `database.schema`.
- A scope outside the committed allowlist is refused. `--scope` narrows; it
  never widens.

`--project` and `--dataset` are BigQuery vocabulary and error here.

## Preflight before a dbt build

`transform build` refuses, for free, before the cost gate:

- when `.dex/config.yml` and the rendered `profiles.yml` disagree about the dev
  database, schema, or warehouse (the profile is what dbt reads, so a config
  edit that never reached it would otherwise build against the old target), and
- when `snowflake.dev_database` does not exist. dbt creates schemas but never
  databases, so the first build would fail inside dbt's `list_schemas` macro
  with `002043: Object does not exist`. dex names the database and the
  statement that fixes it:

```sql
CREATE DATABASE IF NOT EXISTS DBT_DEV;
```

dex never creates it for you, and never rewrites your `profiles.yml`: its only
writes are reviewable diffs inside the repo.

`transform init` runs its own free preflight in the other direction: a
SHOW-only content check (cloud-services layer, no warehouse) of every schema
the new project would build into, warning when one already holds tables or
views. With `--layered-schemas` the layers build into sibling schemas of
`snowflake.dev_database` (`STAGING_DEV`, `INTERMEDIATE_DEV`, `MARTS_DEV` on
the `dev` target), and those are checked too.

## Cost model: the inversion from BigQuery

On BigQuery, metadata is free and scans bill by bytes. On Snowflake the rule
flips: metadata is cheap (SHOW commands run on the cloud-services layer with
no warehouse), while any data scan costs warehouse runtime. So dex guards
**warehouse-seconds**, not bytes.

- **Free:** `connect test`, `explore inventory`, all schema and row/byte
  metadata (SHOW commands, no `INFORMATION_SCHEMA` scans), estimation, and
  `maintain check` (drift detection reads metadata only).
- **Billed:** profiling aggregates, `explore query`, relationship verification
  probes, and `transform build`.

Budgets (`budget.ceiling`, `--budget`, `budget.session_ceiling`) are
warehouse-seconds: the number you budget is the number the server enforces.
Every cost surface also carries the translation to **credits**
(`seconds x credits_per_hour / 3600`, rate from the warehouse's size and
generation via `SHOW WAREHOUSES`; Gen2 rates are marked approximate), and to
dollars when `snowflake.credit_price_usd` is set. dex never guesses a dollar
price: no API exposes your contract rate.

**Estimates are a heuristic, honestly labeled.** Snowflake has no dry-run, so
the estimate is derived from table bytes over a conservative scan rate, and
every handshake payload carries `estimate_quality: "heuristic"`. The estimate
also floors in the **60-second resume minimum** when the pinned warehouse is
suspended (Snowflake bills at least 60 seconds per resume), so a two-second
probe against a cold warehouse is quoted at what the account will actually
see.

**The budget is hard-enforced regardless of estimate quality.** Before every
billed statement the session's `STATEMENT_TIMEOUT_IN_SECONDS` is set to the
remaining budget, so a wrong heuristic cannot overrun the ceiling: Snowflake
kills the statement and dex reports the over-ceiling refusal. Actual spend is
wall-clock seconds per statement (including any resume the statement caused),
recorded to `.dex/spend.jsonl` as `billed_seconds` and summed into the daily
session ceiling. Every session is tagged `QUERY_TAG = 'dex'` for attribution.

The handshake is the same strict two-step as every billed connector: a
scanning command without `--confirm` returns `needs_confirmation` carrying the
seconds estimate (per table where relevant) and its credit translation;
re-issue with `--confirm --budget <seconds>`. Nothing executes unconfirmed or
without a ceiling, and an estimate over the ceiling is refused outright.

## Read-only, enforced in depth

- Every data statement passes the SELECT-only guard parsed in the snowflake
  dialect through one execution door (DML, DDL, `COPY INTO`, `CALL`,
  `ALTER`, and multi-statement forms all refuse).
- The adapter issues no mutating statements: SHOW commands, SELECTs, and
  session parameters only.
- The documented grant shape is a least-privilege role: USAGE on the pinned
  warehouse, read (or `IMPORTED PRIVILEGES`) on source databases, and write
  only on the dedicated dev database (`scripts/setup_snowflake_ci.sh` creates
  this shape).
- dbt dev builds write to `dev_database.dev_schema`, which is refused as a
  source, and the generated profile pins the warehouse and one thread.

## Profiling behavior

Profiling is batched aggregates (COUNT, APPROX_COUNT_DISTINCT, min/max only
for engine-cleared safe columns), never row values. Semi-structured and
spatial columns (VARIANT, OBJECT, ARRAY, GEOGRAPHY, GEOMETRY, VECTOR) degrade
to non-null counts. Tables above `snowflake.max_full_profile_bytes` are
profiled from a block sample (`SAMPLE SYSTEM`), noted, with uniqueness not
judged. Exact distinct-count escalation spends only inside the confirmed
budget and degrades to approximate verdicts with a note when the remainder
cannot cover it.

## Testing

Offline: a stateful fake connection (`tests/fakes/snowflake.py`) that records
statement ordering, simulates timing on an injected clock, and enforces the
statement timeout with the connector's real error types. Live: an env-gated
integration suite (`DEX_TEST_SNOWFLAKE_*`) reading `SNOWFLAKE_SAMPLE_DATA`
and writing only to the transient scratch database, run in CI on a schedule
via workload identity federation (no stored keys), never as a merge or
release gate.
