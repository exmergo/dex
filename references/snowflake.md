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

A key-pair connection's private key file must be readable by its owner alone,
the rule `ssh` applies to its keys: a file with any group or other permission
bit is refused, with its path and the `chmod 600` that fixes it, before any
connection is attempted. Windows is not checked, because its mode bits are
synthesized and its ACLs, which govern access there, are not visible to this
check.

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
  suspend_after_command: false   # suspend the warehouse after a command that woke it (needs OPERATE)
budget:
  ceiling: 300                   # per-command warehouse-seconds; --budget overrides
  session_ceiling: 3600          # cumulative seconds per UTC day
```

**The warehouse pin is strict.** Billed statements run only on
`snowflake.warehouse`; a connection-level default warehouse is never spent on,
and a missing or unpinned warehouse refuses with the fix named. Pin the
smallest warehouse that works (X-Small with 60s auto-suspend is the intended
shape; `scripts/setup_snowflake_ci.sh` provisions exactly that plus a resource
monitor as the hard monthly backstop). Auto-suspend matters as much as size:
every billed command that leaves the warehouse idle is charged the idle time
until it suspends (see **Warehouse uptime** below), and Snowflake's default
for a new warehouse is 600 seconds, which bills a two-second query for ten
minutes. Where the warehouse is shared and its `AUTO_SUSPEND` cannot be
lowered, `suspend_after_command: true` gets the same effect per command (see
**Suspending after a command** below). `connect test` recommends one or the
other whenever `AUTO_SUSPEND` is longer than 60 seconds and neither is in
place; dex never changes `AUTO_SUSPEND` itself.

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

`explore query` and `explore cluster` bill an auto-profile of an object this connection has that the `.dex/` cache cannot adjudicate, priced into the same handshake as the statements: see [`cost-controls.md`](cost-controls.md). Pass `--no-auto-profile` (or set `auto_profile: false` in `.dex/config.yml`) to be refused instead.

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
also quotes, once per command, the **warehouse uptime** no statement will
report: the 60-second resume minimum when the pinned warehouse is suspended,
and the idle tail until auto-suspend either way, with a note saying which. So
a two-second probe against a cold warehouse is quoted at what the account will
actually see. The same estimator prices `transform build`: each compiled
model, snapshot, and test is estimated and summed into the build's upfront
cost, with the uptime as a line of its own, `(warehouse uptime)`.

`transform build --verify` costs only where the warehouse keeps no row
count, so a table's count is free metadata; how the counts are priced into
the build's own estimate is in
[`cost-controls.md`](cost-controls.md).

`transform test --mutate` prices the batch in warehouse-seconds, from the same
heuristic the rest of this connector uses rather than from a dry run, so the
estimate carries `estimate_quality: heuristic` and the resume minimum floors it
like any other billed command here. One estimate and one confirmation cover
every mutant; a budget that runs out partway stops the run and reports the rest
as `not_run`. Each run settles at the per-node execution seconds it reports,
ledgered under `command: "transform test"`, and the batch's warehouse uptime
settles once at the end as one more row under the same command. The stop rule
counts that uptime before admitting the next run, and it includes the time
between runs, while dbt starts and parses with the warehouse up, which no
estimate prices: a batch confirmed at exactly its estimate can stop a run
early. Nothing is materialized,
because a mutant builds as an ephemeral model, so `snowflake.dev_database` holds
exactly the objects it held before the run.

Comparing each survivor with the unmutated model on the dev data is opt-in here,
with `--check-equivalence`, and is priced in seconds into the same estimate from
the same heuristic: one line for the self-check, and one for the comparisons,
priced as if every mutant survives. Only survivors run one, so the settled
seconds are at most that line, ledgered under the same command. Without the
flag, survivors report `equivalence.status: not_checked`.

With `--downstream`, the tests of the models that read the mutated one run
against each mutant too, routed through those models as ephemeral, so nothing
is materialized there either. It is opt-in here because each downstream test
recomputes the chain beneath it, and it is priced into the same single estimate:
each mutant's line grows by the downstream tests it will run.
The heuristic prices a statement by the bytes it scans, and a downstream test
recomputes the whole chain beneath it (joins and aggregates included), so on a
large source it can settle above its line; the stop rule counts settled
seconds, so the confirmed budget still binds.


`--verify` also reads the dev schema (`snowflake.dev_database` /
`snowflake.dev_schema`) for the length of that one command, because dbt writes
the relations it is judging there and that schema is refused as a source
everywhere else. The schema need not exist yet: dbt creates it on a first
build, and dex lists it after dbt has run. How that read is bounded is under
**Read scope** in [`command-contract.md`](command-contract.md).


**The budget is hard-enforced regardless of estimate quality.** Before every
billed statement the session's `STATEMENT_TIMEOUT_IN_SECONDS` is set to the
remaining budget, less the uptime the command will still owe once its
statements are done, so a wrong heuristic cannot overrun the ceiling: Snowflake
kills the statement and dex reports the over-ceiling refusal. Every session is
tagged `QUERY_TAG = 'dex'` for attribution.

## Warehouse uptime

Snowflake bills a warehouse for being up, not for running statements: at least
60 seconds each time a suspended warehouse resumes, then per second until it has
idled for its `AUTO_SUSPEND`. Statement time alone under-reports exactly the
small commands an agent runs most (a three-second build on a warehouse with a
60-second auto-suspend bills over a minute), so a command's actual spend has two
parts, both recorded as `billed_seconds` in the ledger:

- **Statement time.** Wall-clock seconds per statement dex runs, and dbt's
  per-node execution seconds for a build.
- **Uptime no statement reported**, settled once the command's last statement
  is done, from a free `SHOW WAREHOUSES`: the time between the command's own
  statements, floored at 60 seconds when it found the warehouse suspended, plus
  the `AUTO_SUSPEND` tail when it left the warehouse idle. A build folds this
  into its own settlement row; other commands record it as one more row in
  their `reservation_id` group, with no statement hash. A warning names what
  was added and why.

The rules that decide the figure:

- **The last user pays the tail.** A command that finishes while other queries
  are still running or queued on the warehouse is charged no tail; one that
  leaves it idle is charged all of it.
- **A tail is paid once per resume cycle.** Each command records what it
  claimed of the warehouse's current resume cycle (the warehouse, its
  `resumed_on`, when its last statement ended, its `AUTO_SUSPEND`) as a
  zero-amount settlement row carrying `warehouse_claim` in the spend ledger. A
  command that then finds the warehouse still running in the same cycle is
  charged only what it adds to that claim: the gap since the previous
  command's last statement plus its own span, rather than a second tail. Ten
  commands half a minute apart settle about one tail between them, as the
  warehouse bills, instead of ten. The rule only lowers a charge, never past
  the command's own statement time, and never raises one past statement time
  plus one tail, so a warehouse other users keep running cannot make dex claim
  their uptime. The estimate quotes by the same rule. A store without readable
  ledger history charges the full tail, the safe direction.
- **A command that never woke the warehouse owes no uptime.** Snowflake answers
  a result-cache hit or a count served from metadata without resuming it.
- **`AUTO_SUSPEND` off means unbounded.** The warehouse bills until someone
  suspends it, so no tail is charged, the warning says the figure is a lower
  bound, and `connect test` warns.
- **It is a model of Snowflake's billing rules, not a meter.** Snowflake's
  suspend check runs about every 30 seconds, so a tail can run up to that much
  longer than `AUTO_SUSPEND`, most likely when the setting is not a multiple of
  30; multi-cluster scale-out and resizes are not modeled.
  `SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY` is the authority, and the
  resource monitor the backstop. dex cannot settle from that view instead: it
  reports hourly buckets that mix in other users, lags up to three hours, and is
  readable only with ACCOUNTADMIN or MONITOR USAGE.

The overhead belongs to the command, not to any statement, so it is quoted once
per command as its own `(warehouse uptime)` line, by whichever handshake shows
the caller an estimate first, and a phase whose budget cannot cover it asks
rather than running. Where the idle tail is most of the estimate, a note states
the command's own work, what `AUTO_SUSPEND` makes it bill, and what a 60-second
`AUTO_SUSPEND` or a suspend would bring it to. The estimate carries its credit
translation; the handshake itself is in [`cost-controls.md`](cost-controls.md).

### Suspending after a command

`snowflake.suspend_after_command: true` (off by default) has dex suspend the
pinned warehouse at the end of a command, so the command bills its statement
time floored at Snowflake's 60-second resume minimum instead of the whole
`AUTO_SUSPEND` tail. On a warehouse at the 600-second default that is roughly a
tenth of the per-command bill. It is opt-in because, unlike the session
parameters dex sets, it acts on a warehouse other workloads may share, and dex
only suspends one it can show was its own to suspend:

- **The command woke it.** It found the warehouse suspended, and the
  warehouse's `resumed_on` moved during the command's first statement, so the
  resume was this command's and not another session's.
- **Nothing else is using it.** `SHOW WAREHOUSES` shows nothing running or
  queued once the command's last statement is done. A warehouse that was
  already running when the command started is never suspended, whoever was
  using it.
- **It wakes on its own.** `AUTO_RESUME` is on, so the next query anyone sends
  resumes it. A warehouse with `AUTO_RESUME = FALSE` is never suspended, since
  every query after the suspend would fail until someone resumed it by hand.
- **It is the pinned warehouse.** The statement is rendered by one typed
  adapter method that can only produce `ALTER WAREHOUSE "<pinned warehouse>"
  SUSPEND`, refusing when `SHOW WAREHOUSES` spells the warehouse differently
  from the pin. No agent-authored SQL reaches it, and the query firewall goes
  on refusing every `ALTER`. Suspending stops compute and touches no data,
  schema or file, and Snowflake lets running statements finish first.

It needs `OPERATE` on the warehouse:

```sql
GRANT OPERATE ON WAREHOUSE DEX_WH TO ROLE DEX_ROLE;
```

Without the grant the suspend is refused, the command completes and warns with
the grant to add, and it settles the tail as before. The warnings about the
tail print their commands ready to copy, with the warehouse and the session's
own role filled in (the role comes from the login response, so reading it runs
no statement). While the setting is off, `connect test` prints the `dex config
set` that turns it on and the grant, so adopting it is two copied commands. An
estimate's note names only what is missing: the `dex config set` while the
setting is off, and the grant once a suspend has been refused for lacking it.
dex cannot test the grant without suspending something, so the quote counts on
a suspend only once one has worked: the first command after the setting is
turned on is quoted the tail, with a note saying why, and is settled at what
the suspend did; later ones are quoted the resume minimum, until a refusal is
recorded. `connect test` reports
`budget.warehouse.suspend_after_command` (`enabled`, `last_outcome`) and lists
`OPERATE` under `required_grants` while the setting is on. If the grant is
revoked after a suspend has worked, one command is quoted the resume minimum
and settled at the tail before the refusal it records puts the tail back in
the quote.

## Read-only, enforced in depth

- Every data statement passes the SELECT-only guard parsed in the snowflake
  dialect through one execution door (DML, DDL, `COPY INTO`, `CALL`,
  `ALTER`, and multi-statement forms all refuse).
- The adapter issues no mutating statements: SHOW commands, SELECTs, and
  session parameters only, plus `ALTER WAREHOUSE "<pinned warehouse>"
  SUSPEND` where `snowflake.suspend_after_command` opts in, which stops compute
  and changes no data. The safety spine pins that set.
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

`SHOW COLUMNS` reports a type token rather than a full type. `NUMBER(38,0)` and
`NUMBER(10,2)` both arrive as `FIXED`, and `VARCHAR(50)` as `TEXT`, so precision,
scale and length are not recorded anywhere dex can compare. Two things follow:
`maintain schema` reports no `column_retyped` for a precision, scale or length
change, and `is_integer_type` treats `FIXED` as a known, accepted false negative
rather than guessing at the scale behind it.

## Testing

Offline: a stateful fake connection (`tests/fakes/snowflake.py`) that records
statement ordering, simulates timing on an injected clock, resumes a suspended
warehouse the way the service does, and enforces the statement timeout with the
connector's real error types. Live: an env-gated
integration suite (`DEX_TEST_SNOWFLAKE_*`) reading `SNOWFLAKE_SAMPLE_DATA`
and writing only to the transient scratch database, run in CI on a schedule
via workload identity federation (no stored keys), never as a merge or
release gate.
