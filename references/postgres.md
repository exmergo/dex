# Connector: PostgreSQL

The operational database AEs pull from. Namespace: `database.schema.table`
(one connection, one database; the first component is always the connected
database). Cost paradigm: **database load**, expressed as database-seconds.
Read-only against data, enforced in depth.

## Authentication: discover, don't ask

The engine discovers a connection at runtime and never prompts for or
persists a password. Discovery order:

1. `postgres.service` in `.dex/config.yml`, naming a `pg_service.conf` entry
   (`PGSERVICEFILE`, else `~/.pg_service.conf`)
2. `DATABASE_URL` (the twelve-factor URL most app stacks already export)
3. the `PG*` environment (`PGHOST`/`PGDATABASE`/`PGSERVICE` and friends),
   resolved natively by libpq, including `~/.pgpass`
4. the committed non-secret `postgres.host`/`port`/`dbname`/`user` config
   target (password still supplied by `PGPASSWORD` or `~/.pgpass`)
5. the `host` of a `type: postgres` target in a discovered dbt `profiles.yml`

Only a coarse method (for example `database_url:password` or
`environment:external`) is ever surfaced; the credential kind is `password`
when one is inline and `external` for everything else (`.pgpass`, peer or
trust auth, SSL client certificates), deliberately coarse. DSNs, identities,
and passwords never cross the envelope (the sanitizer additionally strips
`user:pass@host` URL fragments). Every discovery failure names all the fixes.

## Config

```yaml
# .dex/config.yml
connector: postgres
postgres:
  service: analytics             # a pg_service.conf entry; optional
  schemas:                       # source allowlist; empty means all visible
    - app
  dev_schema: dbt_dev            # where dbt dev builds write (never a source)
  max_full_profile_bytes: null   # opt-in TABLESAMPLE threshold for huge tables
budget:
  ceiling: 60                    # per-command database-seconds; --budget overrides
  session_ceiling: 600           # cumulative seconds per UTC day
```

Point dex at a **read-only role** (or better, a read replica) rather than a
superuser on the primary; the documented grant shape is USAGE on source
schemas plus SELECT on their tables, with write access only on the dedicated
dev schema for the dbt role. `scripts/postgres_seed.sql` shows the shape
(`dex_ro` and `dbt_dev` roles).

## Cost model: load, not dollars

Postgres bills nothing, but dex is usually pointed at a production OLTP
primary, and an unbounded scan there is a real cost: it evicts cache, holds
back vacuum, and steals cycles from the application. So the guarded quantity
is **database-seconds**, gated through the same strict handshake as every
metered connector.

- **Free:** `connect test`, `explore inventory`, all schema facts
  (`pg_catalog` lookups, no table scans), `pg_stats` reads, `EXPLAIN`
  estimation, and the free axes of `maintain check` (schema and volume read
  catalog metadata only).
- **Metered:** profiling aggregates, `explore query`, relationship
  verification probes, distinct-count escalations, and `transform build`.

Budgets (`budget.ceiling`, `--budget`, `budget.session_ceiling`) are
database-seconds: the number you budget is the number the server enforces.

**Estimates are a heuristic, honestly labeled.** Query estimates come from
the genuinely free planner preflight (`EXPLAIN (FORMAT JSON)`), so an
index-served lookup is not quoted as a full scan; plan cost translates to
seconds through a deliberately conservative scan rate, and profile estimates
come from relation sizes over the same rate. Every handshake payload carries
`estimate_quality: "heuristic"`.

**The budget is hard-enforced regardless of estimate quality.** Before every
metered statement the session's `statement_timeout` is set to the remaining
budget, so a wrong heuristic cannot overrun the ceiling: Postgres kills the
statement and dex reports the over-ceiling refusal. Actual spend is
wall-clock seconds per statement (a killed statement still bills what ran),
recorded to `.dex/spend.jsonl` as `billed_seconds` and summed into the daily
session ceiling. Every session connects as `application_name = 'dex'` for
attribution in `pg_stat_activity`.

The handshake is the same strict two-step as every metered connector: a
scanning command without `--confirm` returns `needs_confirmation` carrying
the seconds estimate (per table where relevant); re-issue with
`--confirm --budget <seconds>`. Nothing executes unconfirmed or without a
ceiling, and an estimate over the ceiling is refused outright.

## Read-only, enforced in depth

- The session sets `default_transaction_read_only = on` before any statement,
  so the server itself refuses writes (SQLSTATE 25006) even if every other
  guard were bypassed. Connections are autocommit, so no idle-in-transaction
  session ever holds back vacuum on the primary.
- Every data statement passes the SELECT-only guard parsed in the postgres
  dialect through one execution door (DML, DDL, `COPY`, and multi-statement
  forms all refuse).
- The adapter issues no mutating statements: catalog SELECTs, `EXPLAIN`, and
  session `SET`s only.
- The documented grant shape is a least-privilege read-only role; dbt dev
  builds write to `dev_schema` (default `dbt_dev`), which is refused as a
  source, and the generated profile renders one thread and an `env_var`
  password reference, never a value.

## Profiling behavior

Profiling is deliberately light on the primary. The metered batch is one
cheap single-pass scan per 50 columns: `COUNT(*)`, per-column non-null
counts, and min/max only for engine-cleared safe columns. **Distinct counts
come free from the planner's own statistics** (`pg_stats.n_distinct`, scaled
by the exact row count when negative); the value-carrying statistics columns
(`most_common_vals`, `histogram_bounds`) are never read. A statistics
estimate is never a uniqueness verdict: near-unique keys escalate to an exact
`COUNT(DISTINCT)` inside the confirmed budget through the standard escalation
flow, and that scan also upgrades the row count from the `reltuples` estimate
to an exact figure, so uniqueness proofs and grain verdicts compare against
real rows.

Freshness caveats, surfaced as data-quality notes rather than silently
thinner numbers: a never-analyzed table has no row estimate and no distinct
estimates (the note names `ANALYZE` as the fix), and `maintain volume` on
unprofiled tables compares planner estimates whose accuracy tracks
autovacuum's ANALYZE cadence. `json`, `jsonb`, arrays, `bytea`, `xml`,
`tsvector`, and the geometric and PostGIS types degrade to non-null counts.
Tables above `postgres.max_full_profile_bytes` are profiled from a block
sample (`TABLESAMPLE SYSTEM`), noted, with uniqueness not judged.

## dbt builds

`transform init` renders a `type: postgres` dev profile from the discovered
connection: host, port, user, and dbname from whichever source won discovery,
`schema` pinned to the dedicated dev schema, one thread, and
`password: "{{ env_var('PGPASSWORD', '') }}"` (the empty default keeps
`~/.pgpass` and peer auth working when no variable is set). dbt has no
dry-run, so `transform build` cannot be estimated upfront; the ceiling and
confirmation still bind, and the ceiling is injected into the dbt subprocess
as `PGOPTIONS="-c statement_timeout=<ceiling>s"`, the per-statement
server-side cap (the `maximum_bytes_billed` analogue). Actual per-node
execution time is summed into `billed_seconds` and recorded to the ledger.

## Testing

Offline: a stateful fake connection (`tests/fakes/postgres.py`) that records
statement ordering, serves catalog and `pg_stats` lookups from a table
registry, answers `EXPLAIN` with size-derived plan costs, simulates timing on
an injected clock, and enforces `statement_timeout` with psycopg's real
`QueryCanceled`. Live: an env-gated integration suite (`DEX_TEST_PG_DSN`,
plus `DEX_TEST_PG_DEV_PASSWORD` for builds) against the seeded database from
`scripts/postgres_seed.sql`; `scripts/setup_postgres_dev.sh` stands up the
same thing locally in Docker, and CI runs the suite against a free, keyless
`postgres:16` service container (kept in the integration workflow for
pattern parity, never a merge gate).
