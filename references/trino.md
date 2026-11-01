# Connector: Trino

Self-hosted Trino, Starburst Enterprise, and Trino on Amazon EMR. Namespace:
`catalog.schema.table`, **three parts, always written in full**, because dex
connects with no session catalog or schema. The cost paradigm is **database
load** in cluster-seconds: a self-hosted cluster bills no currency, but an
unbounded scan on a shared cluster slows everyone else on it and, through a
federated catalog, the database behind that catalog too. Read-only against
data, enforced in depth.

Starburst Galaxy bills differently and is not supported yet (see
[Deployment](#deployment)). Amazon Athena is Trino-derived but has its own API,
driver and bytes-scanned pricing, so it would be a separate connector.

Install with the `[trino]` extra, which brings the `trino` client and
`dbt-trino`.

## Databases through configured catalogs

Dex connects to your Trino deployment and reads the sources its catalogs expose.
Configure each source in Trino first, then include its catalog or schema in
`trino.catalogs`. These databases have official Trino connectors:

| Database | Trino connector |
| --- | --- |
| MySQL | [MySQL](https://trino.io/docs/current/connector/mysql.html) |
| Microsoft SQL Server | [SQL Server](https://trino.io/docs/current/connector/sqlserver.html) |
| MongoDB | [MongoDB](https://trino.io/docs/current/connector/mongodb.html) |
| MariaDB | [MariaDB](https://trino.io/docs/current/connector/mariadb.html) |
| Elasticsearch | [Elasticsearch](https://trino.io/docs/current/connector/elasticsearch.html) |
| Oracle | [Oracle](https://trino.io/docs/current/connector/oracle.html) |
| Redis | [Redis](https://trino.io/docs/current/connector/redis.html) |
| Cassandra | [Cassandra](https://trino.io/docs/current/connector/cassandra.html) |

See [Trino's connector catalog](https://trino.io/docs/current/connector.html)
for other sources, including Iceberg, Hive, and Delta Lake.

Use `connector: trino` and the Trino credentials under Authentication below
in Dex. Trino manages the underlying database connections. Access and available
operations depend on your Trino version, catalog configuration, type mappings,
and permissions. The [Dex integration suite](#testing) covers the catalogs named
there; it does not exercise every upstream connector.

For [dbt builds](#dbt-builds), the dev catalog must support the required writes.
Sources can live in other catalogs, with types converted in staging where the
dev catalog requires it. The supported Trino deployments and their cost model
are described under [Deployment](#deployment).

## Authentication: discover, don't ask

The engine discovers a connection at runtime and never prompts for or persists a
credential. Discovery order:

1. `TRINO_URL`: `http://user@host:port`, `https://user[:password]@host:port`,
   or `trino://user@host:port`. A catalog or schema in the URL path is ignored
   with a warning, since dex never sets one.
2. the `TRINO_*` environment (`TRINO_HOST`, `_PORT`, `_USER`, `_HTTP_SCHEME`,
   `_PASSWORD`, `_JWT_TOKEN`)
3. the committed non-secret `trino.host`/`port`/`user`/`http_scheme` config
   target, with the credential still supplied by `TRINO_PASSWORD` or
   `TRINO_JWT_TOKEN`
4. a `type: trino` output in a discovered dbt `profiles.yml`, with
   `{{ env_var(...) }}` references expanded

Three methods are supported: **none** (a cluster that trusts the user name it is
given), **password** (HTTP Basic, which is also how LDAP-backed clusters
authenticate), and **JWT**. Three are refused by name, because an agent session
and the dbt subprocess dex hands the profile to are both non-interactive:

- OAuth2 opens a browser, or waits for a sign-in at the console
- Kerberos needs a ticket cache dex does not manage
- certificate auth needs client key files dex does not manage

**A credential over plain HTTP is refused** with the fix named. Trino sends both
a password and a token as headers, so without TLS they cross the network in the
clear; dex never sets the client's `allow_insecure_auth`. The scheme defaults to
`https` whenever a credential is present or the port is 443.

Only a coarse method (`environment:dsn`, `config_target:password`,
`dbt_profile:jwt`) is ever surfaced. Users, passwords and tokens never cross the
envelope.

**Splitting dex's identity from dbt's.** dex reads and dbt writes, and they
should be different users. `TRINO_URL` is read only by dex; the profile
`transform init` renders reads `TRINO_USER` and `TRINO_PASSWORD` (or
`TRINO_JWT_TOKEN`) for dbt. So the URL carries the read-only identity and the
variables carry the one that writes the dev schema.

**Attribution.** Every connection dex opens sends `source=dex` and
`client_tags=dex`, so an operator can see dex's queries in the UI and route them
to a constrained resource group with a selector on either.

## Config

```yaml
# .dex/config.yml
connector: trino
trino:
  host: trino.internal
  port: 8443
  user: dex_ro
  http_scheme: https
  catalogs:                     # source allowlist: catalog or catalog.schema
    - lake.sales
    - pg_crm                    # empty means every catalog but `system`
  dev_catalog: lake             # where dbt dev builds write; must exist
  dev_schema: dbt_dev           # dbt creates it (default dbt_dev)
  deployment: self_hosted       # `galaxy` is accepted and refused for now
  max_full_profile_bytes: null  # opt-in BERNOULLI sampling; see Profiling
budget:
  ceiling: 60                   # per-command cluster-seconds; --budget overrides
  session_ceiling: 600          # cumulative seconds per UTC day
```

`credit_price_usd` is Galaxy's dollar translation, and is refused under
`self_hosted`, where it would be accepted and never used.

## Scoping a command

A scope is a `catalog` or a `catalog.schema`, never a table:

```
dex --scope lake.sales explore inventory
```

`--scope` narrows the committed `trino.catalogs` allowlist and can never widen
it. Resolution is free (`system.metadata.catalogs` and each catalog's
`information_schema.schemata`) and happens before anything is estimated, so a
typo is refused for nothing, with the catalogs or schemas that do exist listed.

## Identifiers are three parts, and always qualified

`lake.sales.orders`, folded to lower case, which is how Trino stores an unquoted
name. dex opens every connection with **no session catalog or schema**, and
refuses any relation that is not fully qualified (a bare name is allowed only
where the statement defines it as a CTE). That is a safety property, not a
style: the query firewall resolves an agent's partial name to a cached relation
and applies that relation's PII verdict, and with a session catalog Trino could
resolve the same spelling to a different table than the one that was checked.

## Cost model

- **Free:** `connect test`, `explore inventory`, every schema fact
  (`information_schema`, no scan), `EXPLAIN (TYPE IO)`, `maintain schema`, every
  dev-target preflight, and `dbt compile`-based build pricing.
- **Metered:** profiling, relationship verification, `explore query`,
  `explore cluster`, `maintain grain`, the cardinality half of
  `maintain semantic`, `transform build`, and the row counts behind
  `maintain volume` and `build --verify`.

**Trino keeps no free row count.** `information_schema` carries no row or byte
figures, and `SHOW STATS` is never used because its `low_value` and
`high_value` columns are raw column values, PII included. So an inventory
reports no sizes, an exact count arrives with the first profile's `COUNT(*)`,
and `maintain volume` names each object it could not compare in `warnings`
rather than reporting no drift.

**The estimate is free and executes nothing.** `EXPLAIN (TYPE IO, FORMAT JSON)`
plans the statement on the coordinator and reports each input table's estimated
bytes after column pruning and predicate pushdown. Bytes become seconds through
a deliberately conservative scan rate (200 MiB/s). A catalog with no statistics
(most JDBC catalogs, a Hive table never `ANALYZE`d) answers `NaN`;
such a table is priced at a fixed floor and named in a note. The handshake's
`estimate_basis` says `explain_io` when every table was priced from the plan and
`floor` when at least one was not, so you know which part of the quote to
distrust. A statement that cannot be planned at all, almost always a build's
test over a relation the build has not created yet, is priced at the 0.5-second
minimum and counted in a note, as every connector prices a cold dev target: the
estimate is then a lower bound, and the run-time cap still binds each statement. `estimate_quality` is `heuristic`, because the number you confirm is
seconds from a constant.

**The cap is two session properties, set through one door.** Every billed
statement runs under `query_max_run_time`, wound down to what remains of the
budget, and `query_max_scan_physical_bytes`, derived from the same scan rate so
the cap and the quote are two views of one number. Both are applied with
`SET SESSION` / `RESET SESSION` by the adapter itself, so they hold on a
connection a host supplied too, and each door states its whole set: a free
metadata read runs under a fixed 60-second wall cap and never inherits a billed
statement's tight one.

Time is the cap that always binds. The byte cap is checked on an interval of
about a second, and some catalogs report no physical input at all (`tpch`
reports none). Run time rather than execution time is capped because run time
includes queueing and planning, which is what the settlement measures.

**Settlement** is the server's own `elapsedTimeMillis` once a statement
finishes, and the client's wall clock when it fails or is cancelled (the client
stops polling stats on an error, so its last figure would be stale). A partially
fetched result is cancelled server-side, so nothing keeps running unrecorded.

There is no currency translation, because nothing is billed in one.

## Deployment

`trino.deployment` declares which Trino this is, and it is a cost decision rather
than a connection one:

- `self_hosted` (default): Trino, Starburst Enterprise, and Trino on EMR,
  guarded as `db_load` in cluster-seconds.
- `galaxy`: Starburst Galaxy, which bills credits for every second a cluster
  runs, resume and idle tail included. It is accepted by config validation so
  the committed shape is final, and refused by name on `connect`, the dev-target
  preflight, and `transform init`, with `compute_time` as the paradigm in the
  envelope, because dex does not model that uptime yet.

Deployment is a **declaration, never a sniff**. A `self_hosted` declaration
whose host, committed or discovered, ends in `.galaxy.starburst.io` is refused,
since the guard would otherwise count load on a cluster that bills uptime.

## Read-only, enforced in depth

Trino has no read-only session mode a client can hold, so `connect test`
reports `session_read_only: false` rather than claiming one. Read-only rests on:

1. **A guard on the billed door and the explain door**: one read-only SELECT,
   every relation fully qualified, and no table functions (`TABLE(...)`,
   including a federated catalog's `system.query` passthrough, which would send
   opaque SQL to the remote database), hidden `$` columns, connector metadata
   tables (`"orders$files"`, `"orders$snapshots"`), time travel
   (`FOR VERSION AS OF`, `FOR TIMESTAMP AS OF`), or `DESCRIBE`.
2. **Refusal of every non-query statement**: `CALL`, `EXECUTE`, `SET`/`RESET
   SESSION`, `USE`, `ANALYZE`, `EXPLAIN ANALYZE`, and every write. The safety
   spine pins each one, so a parser upgrade cannot quietly admit it.
3. **An adapter that issues only** `SELECT`, `EXPLAIN`, and its own
   `SET`/`RESET SESSION` for the caps. Inventory filters out `$` tables too.
4. **Server-side access control for the dex identity**, which is the layer
   nothing inside dex can vouch for, so set it up. With file-based access
   control (`access-control.name=file`), a shape that gives dex read-only access
   everywhere and dbt write access to its dev schema only:

```json
{
  "catalogs": [
    {"user": "dbt_dev", "catalog": "lake", "allow": "all"},
    {"catalog": ".*", "allow": "read-only"}
  ],
  "schemas": [
    {"user": "dbt_dev", "catalog": "lake", "schema": "dbt_dev", "owner": true}
  ],
  "tables": [
    {"user": "dbt_dev", "catalog": "lake", "schema": "dbt_dev",
     "privileges": ["SELECT", "INSERT", "DELETE", "UPDATE", "OWNERSHIP",
                    "GRANT_SELECT"]},
    {"user": "dbt_dev", "privileges": ["SELECT", "GRANT_SELECT"]},
    {"privileges": ["SELECT"]}
  ]
}
```

The rules are first-match, so the read-only catch-all goes last.
`GRANT_SELECT` for the dbt identity is not optional: Trino views run with their
owner's rights, so when dex (or anyone else) reads a dbt view, Trino checks that
the view's owner may pass on read access to everything the view reads. Without
it a staging view over a source reads fine and a mart over those staging views
is refused ("View owner 'dbt_dev' cannot create view that selects from ...").
The same intent maps onto Ranger or OPA policies. `scripts/trino/rules.json` is the
version the integration suite runs against, with an `admin` identity for
seeding.

A resource group selected by `source: dex` (or the `dex` client tag) with a
`softCpuLimit` or `hardConcurrencyLimit` is the server-side backstop on load
that dex cannot raise from inside a session.

## Profiling behavior

- Nested and special types (`array`, `map`, `row`, `json`, `varbinary`, the
  sketch types such as `hyperloglog` and `tdigest`, and the geo types) degrade
  to a non-null count before any per-column statistic is requested. Their
  spellings contain the substrings the shared type predicates look for
  (`array(varchar)` reads as a string), and a `regexp_like` over one would fail
  the whole batch.
- Fractions are computed with double literals. Trino types `1.0` as
  `decimal(2,1)` and `avg` keeps that scale, which would round every null
  fraction to one decimal place; the profiles match DuckDB's on the same data.
- `max_full_profile_bytes` samples a table whose planned scan exceeds it with
  `TABLESAMPLE BERNOULLI (pct)`, written natively because the transpiled form is
  one Trino rejects. BERNOULLI keeps each row with that probability, so it
  **still reads every split**: it reduces CPU and aggregation work, not the scan,
  and it is not repeatable. The table note says so, and a sampled profile never
  judges uniqueness.
- `explore cluster` samples the same way, with the same caveat.
- Trino enforces no keys, so every relationship dex reports here is
  name-and-shape inference, verified by an overlap probe when you ask.

The query firewall accepts Trino's own unnest idioms, `CROSS JOIN UNNEST(...)`
over an array or map column and over `CAST(json_parse(c) AS ARRAY(JSON))` or
`MAP(VARCHAR, JSON)`, with the outputs inheriting the source column's PII flags.

## dbt builds

The dbt adapter is `dbt-trino`, which ships with the `[trino]` extra. Its
`database` is the Trino catalog. `transform init` renders a dev-only profile
with no secret in it:

```yaml
type: trino
method: ldap                         # or none, or jwt
password: "{{ env_var('TRINO_PASSWORD') }}"
user: "{{ env_var('TRINO_USER', 'dex_ro') }}"
host: trino.internal
port: 8443
http_scheme: https
cert: true
database: lake                       # trino.dev_catalog
schema: dbt_dev                      # trino.dev_schema
threads: 1
session_properties:
  query_max_run_time: "{{ env_var('DEX_TRINO_QUERY_MAX_RUN_TIME', '300s') }}"
  query_max_scan_physical_bytes: "{{ env_var('DEX_TRINO_QUERY_MAX_SCAN_PHYSICAL_BYTES', '100GB') }}"
```

`cert: true` is explicit because dbt-trino otherwise hands the client
`verify=None`, which turns certificate checking off for the identity whose
password the profile sends.

**The build cap rides `session_properties`.** dbt-trino sends each entry with
every statement it issues, CTAS and MERGE included, and `transform build` fills
the two variables from the confirmed budget. The literal defaults are real caps
for a hand-run `dbt build`, because on Trino `0s` fails every statement at once
rather than meaning unlimited. A hand-written profile without those references
builds fine but cannot be capped; dex warns rather than refusing, and the build
result says the run was uncapped. `transform preflight` reports which you have.

**The dev-target preflight asks about the catalog, not the schema.** dbt-trino
creates a missing dev schema but never a catalog, and a catalog is only as
writable as its connector. So `dev_catalog` must exist, and is refused before
dbt runs when it is a read-only kind (`tpch`, `tpcds`, `jmx`, `system`,
`faker`). A `memory` catalog (lost on restart) and a `blackhole` catalog (keeps
nothing) are allowed with a warning. Whether the dbt identity may write is not
asked: Trino authorization is pluggable and exposes no grant table, so dbt finds
out.

**Scaffolded sources name their catalog.** dbt-trino resolves a source with no
`database` in the profile's, which is the dev catalog, so `transform plan
--scaffold` writes each source with `database: <catalog>` and names it
`<catalog>_<schema>` (`memory_app`), since federated catalogs often share schema
names. Two relations sharing a table name in different catalogs would both map
to `stg_<table>`; the scaffold refuses rather than repoint an existing model, and
names `transform rename model` as the way through.

**A lakehouse dev catalog cannot hold every source type.** Iceberg rejects
`char(n)`, `timestamp(p)` below microseconds, and a few others, so a staging
model that passes a federated source's column straight through fails to build
there (`Type not supported for Iceberg: char(10)`, `Timestamp precision (3) not
supported for Iceberg`). Cast in the staging model (`cast(zip as varchar)`,
`cast(paid_at as timestamp(6))`). The scaffold does not cast for you, because the
right target type depends on the dev catalog's connector: Hive, for one, rejects
`timestamp(6)` under its default precision.

`transform build --verify` reads `dev_catalog.dev_schema` for that one command.
Its row counts are priced into the build's own estimate, and since Trino keeps
no free count, every relation verified is a count query.

`transform test --mutate` prices its batch as one number and confirms it once.
Survivors are not yet compared with the model on the dev data here, so each
reports `equivalence.status: not_checked` with the reason.

With `--downstream`, the tests of the models that read the mutated one run
against each mutant too, routed through those models as ephemeral, so nothing
is materialized there either. It is opt-in here because each downstream test
recomputes the chain beneath it, and it is priced into the same single estimate:
each mutant's line grows by the downstream tests it will run.

The shipped `unpivot_json_object` macro has a Trino implementation that casts the
object to `map(varchar, json)` and unnests it, so it is top-level only and keeps
nested values as JSON, matching the other adapters.

## Semantic layer

`explore semantic query --local` renders with MetricFlow's Trino renderer and
runs through dex's own connector, firewall and cost handshake.

A native Apache Ossie layer reads expressions in the portable `ANSI_SQL`
dialect, since Ossie's dialect enum has no Trino token. A SLayer layer is
refused on Trino for now: SLayer has a Trino dialect, but a connector joins only
once a golden of its rendered SQL has been read (see
[Querying the semantic layer](semantic-layer.md)).

## Testing

Offline, deterministic, and free: `tests/fakes/trino.py` is a stateful fake of
the client surface, driving `tests/adapters/test_connect_trino.py` and the Trino
block of the safety spine.

The live suite runs against a container pair you start yourself (Trino, plus the
Postgres database its keyless Iceberg catalog keeps table pointers in), with the
`tpch`, `memory` and `iceberg` catalogs and the file-based access control above.
The same script CI runs stands it up:

```bash
scripts/setup_trino_dev.sh
DEX_TEST_TRINO_URL=http://dex_ro@127.0.0.1:8085 \
    uv run pytest tests/integration -q -m trino
scripts/setup_trino_dev.sh --down
```

A narrow suite against an Iceberg catalog on AWS Glue and S3 runs under the
protected `trino-iceberg-integration` environment, through an OIDC role that may
write only the dbt dev database. `scripts/setup_trino_iceberg_ci.sh` provisions
it; locally, `scripts/setup_trino_dev.sh --aws` attaches the same catalog from
the current AWS CLI session:

```bash
scripts/setup_trino_dev.sh --aws
DEX_TEST_TRINO_URL=http://dex_ro@127.0.0.1:8085 \
    DEX_TEST_TRINO_GLUE_SCHEMA=dex_ci \
    uv run pytest tests/integration -q -m trino_iceberg
```
