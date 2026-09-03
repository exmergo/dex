# Connector: ClickHouse

The analytical database, self-hosted or ClickHouse Cloud. Namespace:
`database.table`, **two parts**, because ClickHouse has no catalog level above
the database. The cost paradigm is deployment-dependent: self-hosted uses
**database load** in database-seconds; Cloud uses **compute time** in
compute-seconds with compute-unit-hours alongside. Read-only against data,
enforced in depth.

## Authentication: discover, don't ask

The engine discovers a connection at runtime and never prompts for or persists a
password. Discovery order:

1. `CLICKHOUSE_URL` (or `CLICKHOUSE_DSN`), the connection URL most stacks
   already export
2. the `CLICKHOUSE_*` environment (`CLICKHOUSE_HOST`, `_PORT`, `_USER`,
   `_PASSWORD`, `_DATABASE`, `_SECURE`)
3. the committed non-secret `clickhouse.host`/`port`/`database`/`user` config
   target, with the password still supplied by `CLICKHOUSE_PASSWORD`
4. the `host` of a `type: clickhouse` target in a discovered dbt `profiles.yml`,
   with `{{ env_var(...) }}` references expanded

There is no `pg_service.conf` analogue to pin, which is why this chain is one
shorter than the Postgres one.

Only a coarse method (for example `environment:dsn` or `config_target:password`)
is ever surfaced. DSNs, identities, and passwords never cross the envelope; the
sanitizer additionally strips `user:pass@host` URL fragments. Every discovery
failure names all the fixes.

**Splitting dex's identity from dbt's.** dex reads and dbt writes, and they are
usually different users. The shape that keeps them apart is `CLICKHOUSE_URL` for
dex (carrying the read-only user) and `CLICKHOUSE_PASSWORD` for dbt (which is
what the rendered profile's `env_var` reference resolves at dbt runtime). This
mirrors Postgres's `DATABASE_URL` plus `PGPASSWORD`.

## Config

```yaml
# .dex/config.yml
connector: clickhouse
clickhouse:
  host: clickhouse.internal
  port: 8123
  database: app                  # the database bare table references resolve in
  user: dex_ro
  databases:                     # source allowlist; empty means all visible
    - app
  dev_database: dbt_dev          # where dbt dev builds write (never a source)
  deployment: self_hosted        # `cloud` selects compute-time guarding
  compute_unit_price_usd: null   # Cloud only; actual regional/contract CU price
  max_full_profile_bytes: null   # opt-in SAMPLE threshold; see Profiling
budget:
  ceiling: 60                    # per-command seconds; --budget overrides
  session_ceiling: 600           # cumulative seconds per UTC day
```

Point dex at a **read-only user** rather than `default`. The documented grant
shape is SELECT on the source databases plus SELECT on `system.tables`,
`system.columns` and `system.databases`, with write access only on the dedicated
dev database for the dbt user. `scripts/clickhouse_seed.sql` is the shared,
parameterized data fixture; `scripts/clickhouse_local_users.sql` shows the local
`dex_ro` and `dbt_dev` grant shape.

Granting `system.grants` and `system.role_grants` as well is optional and only
affects the dev-target preflight: without them dex cannot read the dbt user's
privileges and says so rather than guessing (see [dbt builds](#dbt-builds)).

## Scoping a command

A ClickHouse scope is a bare database, because there is no catalog above it to
qualify with and a dotted entry would name a table:

```
dex --scope app explore inventory
```

`--scope` narrows the committed `clickhouse.databases` allowlist and can never
widen it. Resolution is free (one `system.databases` read) and happens before
anything is estimated, so a typo is refused for nothing with the databases that
do exist listed.

## Identifiers are two parts

`app.customers`, not `something.app.customers`. ClickHouse has no catalog level,
and dbt-clickhouse's `schema:` **is** the ClickHouse database (there is no
`database:` key in its profile at all). dex keeps the engine's own shape rather
than synthesizing a third component, so every identifier in the inventory, the
cache, and every drift finding is one you can paste into `clickhouse-client`.

## Cost model

Self-hosted ClickHouse bills nothing, but a scan is real load on a server that is
usually shared and usually serving something latency-sensitive. So the guarded
quantity is **database-seconds**, gated through the same strict handshake as
every metered connector.

- **Free:** `connect test`, `explore inventory`, all schema and size facts
  (`system.tables` and `system.columns`, no scans), `EXPLAIN ESTIMATE`,
  `maintain schema`, `maintain volume`, every dev-target preflight, and
  `dbt compile`-based build pricing.
- **Metered:** profiling, relationship verification, `explore query`,
  `explore cluster`, `maintain grain`, the cardinality half of
  `maintain semantic`, and `transform build`.

Three things make this connector's lifecycle unusually good, and they are worth
knowing because they are what the estimate quality label does *not* say.

**The estimate is free and executes nothing.** `EXPLAIN ESTIMATE` reports the
rows and marks a statement would read after primary-key and partition pruning,
so a filtered probe on a huge table is not quoted as a full scan. It covers the
MergeTree family only, so Log, Memory, Distributed and view relations fall back
to `system.tables` sizes. The handshake payload carries `estimate_basis`
(`explain_estimate`, `system_tables`, or `floor`) so you can tell which priced a
given command. A statement answered entirely from part metadata (a bare
`count()`) legitimately estimates nothing and takes the fallback.

`estimate_quality` is **heuristic**, deliberately, even though the row count is
exact: the number you confirm is seconds, and seconds come from a throughput
constant. The exact part rides one field down rather than inflating the label.

**The cap is layered, because time alone does not bind.**
`max_execution_time` is checked at block boundaries and a single fast block can
overshoot it, so every billed statement also carries `max_bytes_to_read`, derived
by inverting the same throughput constant the estimate used. Both overflow modes
are set to `throw` explicitly, so a server default of `break` could never turn a
cap into a silently truncated result: a refusal is recoverable, a wrong answer is
not.

`max_result_rows` is deliberately **not** set. With `throw` it refuses the
one-extra-row fetch that detects truncation, and with `break` it was measured not
to bind at all; a setting that either breaks a legitimate query or does nothing is
worse than no setting. The result is already bounded twice: the query firewall
clamps `LIMIT` into the statement, and the adapter slices client-side.

**Settlement is free and exact.** Every response carries the
`X-ClickHouse-Summary` header, so elapsed nanoseconds, rows read and bytes read
come back with the result. No second query, and no `system.query_log` poll (which
flushes on a delay). The seconds in the ledger are what the *server* spent, not
what the client waited, which is a genuine accuracy improvement over the other
db-load connector.

For self-hosted there is no currency translation, because nothing is billed in
one. Rows and bytes actually read are reported as table notes rather than in the
spend summary, so nothing there carries a magnitude in a unit the ceiling is not
in.

Cloud uses the same free estimates, caps, and exact statement-seconds
settlement. Before any billed work, dex reads `CGroupMemoryTotal` from every
replica through `clusterAllReplicas('default', system.asynchronous_metrics)` and
cross-checks the number of answers against `system.clusters`. It derives:

```
compute_units_per_hour = total_memory_gib / 8
compute_unit_hours = seconds * compute_units_per_hour / 3600
```

The capacity read is cached for one command and fails closed when it is denied,
empty, malformed, partial, or inconsistent. dex never substitutes a configured
replica count, a guessed tier, or a public price. When
`compute_unit_price_usd` is present, estimates and settled spend also carry a USD
translation using that supplied rate.

These fields are explicitly approximate, not an invoice. dex attributes the
server-reported time of each statement at the capacity observed for that
command; Cloud bills active capacity per minute and can include wake and idle
time outside the statement.

The dedicated AWS us-east-1 Scale test service is configured with the actual
compute rate of $0.29846 per 8-GiB compute unit-hour. Its $25.30/TB-month storage
rate is documented operational context only: storage persists independently of
a statement, so dex does not mislabel it as query spend or fold it into the
per-command confirmation.

## Deployment

`clickhouse.deployment` declares which ClickHouse this is, and it is a cost
decision rather than a connection one:

- `self_hosted` (default) bills no currency, so dex guards it in
  database-seconds.
- `cloud` is guarded in compute-seconds. Live allocated memory translates the
  same binding seconds into approximate compute-unit-hours and, when configured,
  USD.

`compute_unit_price_usd` is refused under `self_hosted`, where it would be
accepted and ignored.

Deployment is a **declaration, never a sniff**. At connect dex additionally
checks the server's own `cloud_mode` setting in both directions and refuses when
the declaration does not match reality. A declared Cloud endpoint proceeds only
after that corroboration and the live capacity proof.

For Cloud, `connect test` adds `compute.replica_count`, `total_memory_gib`,
`compute_units_per_hour`, the metadata source, and `approximate: true`. Its
budget adds `ceiling_compute_unit_hours`; confirmation adds
`estimated_compute_unit_hours`, `compute_unit_rate`, and optionally
`estimated_usd`; settlement adds `compute_unit_hours_billed` and optionally
`usd_billed`. The binding ceiling and ledger remain seconds.

## Read-only, enforced in depth

- `readonly = 2` and `allow_ddl = 0` sent as settings on **every statement**,
  free or billed, so a host that supplies its own client cannot lose them.
  `2` rather than `1` because `1` also forbids changing settings, which would
  block dex from sending its own per-statement caps.
- SELECT-only generation through one execution door, with SQLGlot-parsed refusal
  of DDL, DML, `OPTIMIZE`, multi-statement, and scripting in the ClickHouse
  dialect.
- An adapter that issues no mutating statement of any kind.
- A documented least-privilege user (SELECT only on the source databases).

**The honest limit.** `readonly = 2` by definition permits a session to change
its own settings, so dex's cap is self-imposed exactly as Postgres's
`SET statement_timeout` is. The unraisable form is a server-side settings
constraint on the dex user:

```xml
<profiles><dex_ro><constraints>
  <max_execution_time><max>300</max></max_execution_time>
</constraints></dex_ro></profiles>
```

`readonly = 2` also permits `CREATE TEMPORARY TABLE`. That is session-local and
cannot touch source data, the SELECT-only guard refuses it as DDL anyway, and a
least-privilege user has no grant for it.

`connect test` reports `session_read_only` from the server's own `readonly`
setting rather than assuming it.

## Profiling behavior

- Nullability comes from the **type**, not a column flag: ClickHouse has no
  `is_nullable`, so `Nullable(...)` and `LowCardinality(...)` are unwrapped in
  either nesting order before any type reasoning happens.
- Distinct counts come from `uniq` (a HyperLogLog sketch) in the same pass as the
  null counts, and are never a uniqueness verdict on their own. Near-unique
  columns escalate to an exact `uniqExact` inside the confirmed budget.
- `Array`, `Map`, `Tuple`, `Nested`, `JSON` and the geo types degrade to a
  non-null count: distinct counts and extremes are not meaningful on them.
- **`ORDER BY` is a sort key, not a uniqueness constraint.**
  `system.columns.is_in_primary_key` is free and raises the prior on a candidate
  key, but it never declares one. ClickHouse has no foreign keys at all, so every
  relationship dex reports here is name-and-shape inference.
- Tables on `ReplacingMergeTree`, `CollapsingMergeTree` and friends carry a note
  saying so, because rows sharing the sorting key are kept until a background
  merge collapses them: a duplicate count there describes the stored parts rather
  than the modeled grain, and querying with `FINAL` shows the collapsed view.
- `max_full_profile_bytes` is honored only where the table declared a sampling
  expression in its MergeTree key, which most do not. Where it cannot be honored
  it is **refused out loud** with a note, rather than silently producing a full
  scan you believed was sampled, or an `ORDER BY rand()` that reads everything
  and then sorts it.
- `explore cluster` samples with `ORDER BY rand() LIMIT n` for the same reason,
  and the note says so: it is a full scan bounded by the budget.

## dbt builds

The dbt adapter is `dbt-clickhouse`, which ships with the `[clickhouse]` extra.
Its `schema:` is the ClickHouse database and it has no `database:` key, so
`transform init` renders `schema: <dev_database>` and nothing else.

**The dev-target preflight asks a privilege question, not an existence one.**
dbt-clickhouse issues `CREATE DATABASE IF NOT EXISTS` in its own `create_schema`
macro, so a missing dev database is not fatal; the privilege to create it is.
This lands with Postgres rather than with Snowflake and Databricks, where the
missing object is the whole problem.

Where it differs from Postgres is what happens when the answer is unknown.
Postgres will answer a privilege question about any role; ClickHouse shows
another user's grants only to a caller holding the right access. So the preflight
has three outcomes rather than two:

| What dex could read | What it does |
|---|---|
| nothing | says so, and does not preflight grants |
| direct grants, but not role membership | may **clear** a target, never refuses one, since a privilege held through an invisible role looks identical to a missing one |
| grants and role membership | may clear or refuse |

A refusal names the statement that fixes it:

```
GRANT CREATE DATABASE, CREATE TABLE, INSERT, SELECT ON dbt_dev.* TO dbt_dev;
```

**The build cap rides the profile.** `transform init` renders a `custom_settings`
block whose values are `env_var` references, and `transform build` sets those
variables to the confirmed budget, so the ceiling becomes a per-statement
server-side cap:

```yaml
custom_settings:
  max_execution_time: "{{ env_var('DEX_CLICKHOUSE_MAX_EXECUTION_TIME', '300') }}"
  timeout_overflow_mode: throw
  max_bytes_to_read: "{{ env_var('DEX_CLICKHOUSE_MAX_BYTES_TO_READ', '100000000000') }}"
  read_overflow_mode: throw
```

The literal defaults are real caps rather than `0`, because in ClickHouse `0`
means *no limit*: a zero default would render a profile that silently removes the
backstop the moment anyone runs dbt by hand.

A hand-written profile without those references is perfectly valid and builds
fine; what it cannot do is be capped. dex **warns** in that case rather than
refusing, and the build result says the run was uncapped rather than claiming a
cap it never injected.

The shipped `unpivot_json_object` macro has a ClickHouse implementation built on
`ARRAY JOIN` over `JSONExtractKeysAndValuesRaw`, since ClickHouse has no lateral
join. It is top-level-only and keeps each value as raw JSON text, matching the
other adapters.

## Semantic layer

`explore semantic query --local` does not work on ClickHouse: MetricFlow ships no
ClickHouse renderer. This is declared as an inert capability with a named refusal
listing the connectors that do work, rather than degrading to another dialect's
renderer, which would produce SQL that runs and returns wrong numbers.

## Testing

Offline, deterministic, and free: `tests/fakes/clickhouse.py` is a stateful fake
of the client surface, driving `tests/adapters/test_connect_clickhouse.py` and
the ClickHouse block of the safety spine.

The live suite runs against a container you start yourself, so there is nothing
to provision and nothing to spend. The same script CI runs stands it up locally:

```bash
scripts/setup_clickhouse_dev.sh
DEX_TEST_CH_DSN=clickhouse://dex_ro:dex_ro@localhost:8124/app \
    DEX_TEST_CH_DEV_PASSWORD=dbt_dev \
    uv run pytest tests/integration -q -m clickhouse
scripts/setup_clickhouse_dev.sh --down
```

The narrow Cloud suite is repository-gated and serialized with the other live
cloud jobs. Its protected `clickhouse-cloud-integration` environment is created
by `scripts/setup_clickhouse_cloud_ci.sh`. Before opening SQL, both CI and the
local runner call the same read-only usage preflight; it refuses once the exact
service reaches 2 compute CHC in the current UTC day. The post-run report marks
unlocked usage records provisional because Cloud metering can lag.

Setup, rotation, local execution, troubleshooting, and bounded teardown are in
`scripts/clickhouse_cloud/README.md`. To run locally after loading the protected
environment values:

```bash
scripts/clickhouse_cloud/run_integration.sh
```

The seed is deliberately flawed in ways that make two silent hazards observable:
40 of 5,000 `order_items` rows point at products that do not exist (so an orphan
probe that reports zero has lost `join_use_nulls`), and `events.occurred_at` has
exactly three consecutive days missing from a 90-day span (so a continuity check
that reports no gap has the window function wrong).
