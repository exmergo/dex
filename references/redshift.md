# Connector: Amazon Redshift

The AWS warehouse, Serverless-first. Namespace: `database.schema.table` (one
connection, one database; the first component is always the connected
database). Cost paradigm: **compute-seconds**, translated to RPU-hours on
Serverless. Read-only against data, enforced in depth. Provisioned clusters
ride the same SQL and metadata paths with seconds-only accounting (node-hours
bill flat, so there is nothing to translate); the cost gate is designed for
Serverless.

## Authentication: discover, don't ask

The engine discovers a connection at runtime and never prompts for or
persists a password. Discovery order:

1. `redshift.workgroup` in `.dex/config.yml`: the Serverless pin. The AWS
   default credential chain (`aws configure`, `AWS_*` env, SSO, an assumed
   role, instance metadata; `redshift.aws_profile` pins a named profile)
   resolves the endpoint, port, and database from the control plane, and the
   driver mints IAM **temporary database credentials** (`GetCredentials`) at
   connect time. Nothing durable exists to leak.
2. `redshift.cluster_identifier` plus `dbname`/`user`: the provisioned IAM
   analogue (`GetClusterCredentials`).
3. the `REDSHIFT_*` environment (`REDSHIFT_HOST`, `REDSHIFT_DATABASE`,
   `REDSHIFT_USER`, password via `REDSHIFT_PASSWORD`)
4. the committed non-secret `redshift.host`/`port`/`dbname`/`user` config
   target (password still supplied by `REDSHIFT_PASSWORD`)
5. the `host` of a `type: redshift` target in a discovered dbt `profiles.yml`

Only a coarse method is ever surfaced (for example `iam_serverless:profile`
or `config_target:password`); identities, keys, and passwords never cross the
envelope. Every discovery failure names all the fixes, including the IAM
permissions the workgroup path needs (`redshift-serverless:GetWorkgroup`,
`GetCredentials`, and `GetNamespace` for database discovery).

## Config

```yaml
# .dex/config.yml
connector: redshift
redshift:
  workgroup: dex-wg              # the Serverless pin; IAM auth + RPU facts
  aws_profile: null              # a named ~/.aws profile; default chain if unset
  region: null                   # when the chain's default region is wrong
  schemas:                       # source allowlist; empty means all visible
    - shop
  dev_schema: dbt_dev            # where dbt dev builds write (never a source)
  rpu_price_usd: null            # your region's RPU-hour price; set it to see dollars
budget:
  ceiling: 300                   # per-command compute-seconds; --budget overrides
  session_ceiling: 3600          # cumulative seconds per UTC day
```

Point dex at a read-only database user (or let IAM mint one); the documented
grant shape is USAGE on source schemas plus SELECT on their tables and on
`svv_table_info` (an IAM-minted user starts without it, and without it table
sizes are unknown and estimates degrade to minimums), with write access only
on the dedicated dev schema for the dbt user.

## Scoping a command

`redshift.schemas` is the committed source allowlist. `--scope` narrows it
for one command and is repeatable; a Redshift scope is a bare `<schema>` in
the connected database (dbt refuses cross-database references, so there is
nothing to qualify). Resolution is a catalog lookup and happens before
anything is estimated: a scope that names no schema is refused with the
schemas that do exist listed, never quietly dropped, and `--scope` narrows
the committed allowlist, never widens it. `--project` and `--dataset` are
BigQuery vocabulary and error here.

## Cost model: compute-seconds, honestly floored

Redshift Serverless charges RPU-hours whenever compute is active, with a
**60-second minimum** each time an idle workgroup wakes, and AWS treats every
incoming query as billable activity. Two consequences dex states plainly
rather than hides:

- **Metadata is cheap, not free.** Inventory, `connect test`, estimation,
  and the free axes of `maintain check` read only catalogs
  (`pg_catalog`, `SVV_TABLE_INFO`, `SVV_COLUMNS`) and are not gated, but
  touching an idle workgroup can incur the wake minimum; `connect test`
  carries that warning on Serverless.
- **Estimates carry the wake floor once per command.** There is no API that
  says whether compute is warm (and probing would itself bill), so every
  estimate on Serverless includes the 60-second minimum exactly once,
  labeled an upper bound that actuals waive when compute was already active.

**Metered:** profiling aggregates, `explore query`, relationship
verification probes, distinct-count escalations, and `transform build`.

Budgets (`budget.ceiling`, `--budget`, `budget.session_ceiling`) are
compute-seconds: the number you budget is the number the server enforces.
Every cost surface also carries the translation to **RPU-hours**
(`seconds x base_capacity / 3600`, base capacity from the workgroup via the
control plane, labeled approximate because Serverless can scale above it)
and to dollars when `redshift.rpu_price_usd` is set. dex never guesses a
dollar price: the RPU-hour rate varies by region and agreement.

**Estimates are a heuristic, honestly labeled.** Redshift has no dry-run,
and its EXPLAIN costs are relative units with no honest translation to
seconds, so estimates come from table bytes (`SVV_TABLE_INFO`) over a
conservative capacity-scaled scan rate; every handshake payload carries
`estimate_quality: "heuristic"`.

**The budget is hard-enforced regardless of estimate quality.** Before every
metered statement the session's `statement_timeout` is set to the remaining
budget, so a wrong heuristic cannot overrun the ceiling: Redshift kills the
statement and dex reports the over-ceiling refusal. Actual spend is
wall-clock seconds per statement (a killed statement still bills what ran),
recorded to `.dex/spend.jsonl` as `billed_seconds` and summed into the daily
session ceiling. Every session connects as `application_name = 'dex'`
(`SYS_CONNECTION_LOG`) and sets `query_group = 'dex'` for attribution.

The handshake is the same strict two-step as every metered connector: a
scanning command without `--confirm` returns `needs_confirmation` carrying
the seconds estimate (per table where relevant) and its RPU translation;
re-issue with `--confirm --budget <seconds>`. Nothing executes unconfirmed
or without a ceiling, and an estimate over the ceiling is refused outright.

## Read-only, enforced in depth

- Every data statement passes the SELECT-only guard parsed in the redshift
  dialect through one execution door (DML, DDL, `COPY`, `UNLOAD`, and
  multi-statement forms all refuse).
- The adapter issues no mutating statements: catalog SELECTs and session
  `SET`s only.
- The session attempts `default_transaction_read_only = on`; whether Redshift
  honors it is probed rather than assumed, and `connect test` reports the
  truth as `session_read_only` (the guard and grants hold either way).
- The documented grant shape is a least-privilege read-only user; dbt dev
  builds write to `dev_schema` (default `dbt_dev`), which is refused as a
  source, and the generated profile renders one thread and IAM or an
  `env_var` password reference, never a value.

## Profiling behavior

Profiling is batched aggregates in single passes: `COUNT(*)`, per-column
non-null counts, `HLL(...)` approximate distincts (Redshift refuses more
than three `APPROXIMATE COUNT(DISTINCT)` expressions per statement, so the
equivalent HLL aggregate keeps a wide batch a single pass), and min/max only
for engine-cleared safe columns; never row values. Redshift keeps no usable
planner distincts, so approximate distincts ride the billed batch, and an
approximation is never a uniqueness verdict: near-unique keys escalate to an
exact `COUNT(DISTINCT)` inside the confirmed budget, and that scan also
upgrades the `SVV_TABLE_INFO` row estimate to an exact figure. When no single
column proves unique, the composite-key probe (a bounded batch of exact
distinct-combination counts) spends inside the same confirmed budget, and
like the escalation it carries the pending Serverless wake minimum when it
bills first; when the remaining budget cannot cover it, the probe skips with
a note and the grain stays unknown. `SUPER`,
`VARBYTE`, `GEOMETRY`, `GEOGRAPHY`, and `HLLSKETCH` columns degrade to
non-null counts. There is **no sampled-profiling threshold**: Redshift has
no TABLESAMPLE, so a sampling knob would be a lie; the budget is the only
bound. Empty tables are inventoried from `pg_class` even though
`SVV_TABLE_INFO` omits them.

## dbt builds

`transform init` renders a `type: redshift` dev profile from whichever
source won discovery: IAM discovery renders `method: iam` (temporary
credentials minted by dbt-redshift at runtime, nothing persisted), password
discovery renders `password: "{{ env_var('REDSHIFT_PASSWORD') }}"` (a Jinja
reference, not a value), both with `schema` pinned to the dedicated dev
schema and one thread. Before the cost gate, and for free, `transform build`
refuses config that has drifted from the rendered profile, and a dev schema
the profile's user lacks the privilege to create or write (the Postgres
question: dbt creates the schema, but only if the user may). dbt has no
dry-run and dex cannot inject a per-build server cap through dbt-redshift,
so the honest layering is: per-node execution time summed into
`billed_seconds` on the ledger, a durable `ALTER USER dbt_dev SET
statement_timeout` applied by the setup script, and a workgroup usage limit
(RPU-hours per day) as the account-level backstop. Keep one dev schema per
building user: dbt swaps a rebuilt relation in place, and a relation another
user owns breaks the swap with a bare `relation already exists` (verified
live), so two identities sharing `dbt_dev` will trip over each other's
views.

## Testing

Offline: a stateful fake connection (`tests/fakes/redshift.py`) that records
statement ordering, serves the catalog census (including SVV_TABLE_INFO's
empty-table omission), simulates timing on an injected clock, and enforces
`statement_timeout` with redshift_connector's real error shape. Live: an
env-gated integration suite (`DEX_TEST_REDSHIFT_*`) against a seeded
Serverless workgroup, run in CI on a schedule via GitHub OIDC role assumption
(no stored keys), never as a merge gate; `scripts/setup_redshift_ci.sh`
provisions the workgroup, users, and the usage-limit backstop.
