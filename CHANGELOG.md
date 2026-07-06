# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The engine version is derived from the git tag and follows
[PEP 440](https://peps.python.org/pep-0440/); the plugin follows semver. A single
tag releases both in lockstep, so entries below are keyed by the engine version.

## [Unreleased]

### Added

- **PostgreSQL connector**, the operational-database connector and the first
  on the db-load paradigm. Explore, transform, and maintain run against
  Postgres with the same guardrails as the billed connectors, adapted to what
  the paradigm actually protects (no dollars are billed; the guarded quantity
  is load on a production primary, in database-seconds):
  - Connection discovery, never prompting: a `pg_service.conf` entry pinned by
    `postgres.service`, `DATABASE_URL`, the `PG*` environment (resolved
    natively by libpq, including `~/.pgpass`), the committed non-secret
    `postgres.host`/`dbname` config target, or a dbt profile. Only a coarse
    auth method is surfaced; DSNs and passwords never cross the envelope.
  - Database-seconds budgets (`--budget`, `budget.ceiling`,
    `budget.session_ceiling`) through the same strict confirm handshake as the
    billed connectors. Query estimates come from the genuinely free planner
    preflight (`EXPLAIN (FORMAT JSON)`, so index-served queries are not quoted
    as full scans) and profile estimates from relation sizes, both honestly
    labeled `estimate_quality: "heuristic"`; the budget is hard-enforced
    regardless by a per-statement server-side `statement_timeout`. Actual
    wall-clock seconds land in the `.dex/spend.jsonl` ledger as
    `billed_seconds`. Sessions connect as `application_name = 'dex'`.
  - Read-only in depth: `default_transaction_read_only = on` on every session
    (autocommit, so no idle-in-transaction holds back vacuum), the SELECT-only
    guard in the postgres dialect through one execution door, an adapter that
    issues only catalog SELECTs / EXPLAIN / session SETs, and a documented
    least-privilege role shape.
  - Profiling that is deliberately light on the primary: one cheap single-pass
    aggregate batch (counts, nulls, safe min/max); distinct counts come free
    from `pg_stats.n_distinct` (never the value-carrying statistics columns),
    and near-unique keys escalate to exact `COUNT(DISTINCT)` inside the
    confirmed budget. The escalation scan also upgrades `reltuples` estimates
    to exact row counts, so uniqueness proofs and `maintain grain` verdicts
    never fabricate duplicates from a stale planner estimate. `json`/`jsonb`,
    arrays, `bytea`, and geometric types degrade to non-null counts; tables
    above `postgres.max_full_profile_bytes` profile from `TABLESAMPLE SYSTEM`.
  - `transform init --connector postgres`: a dbt-postgres dev profile from the
    discovered connection (password only as an `env_var('PGPASSWORD')`
    reference, never a value), writing to a dedicated `dev_schema` refused as
    a source, one thread. `transform build` injects the ceiling as
    `PGOPTIONS="-c statement_timeout=<ceiling>s"` (dbt has no dry-run; the
    per-statement cap is the binding cost control) and accounts per-node
    execution time into the ledger.
  - The `[postgres]` extra now carries `dbt-postgres`.
  - Testing per the established connector template: a stateful fake connection
    (catalog + pg_stats registry, size-derived EXPLAIN costs, simulated timing,
    psycopg's real `QueryCanceled` on timeout), safety-spine extensions across
    all five families for the db-load paradigm, and an env-gated live
    integration suite (`DEX_TEST_PG_DSN`) against the seeded database from
    `scripts/postgres_seed.sql`. No cloud setup script: CI runs the suite
    against a free, keyless `postgres:16` service container, and
    `scripts/setup_postgres_dev.sh` stands up the same seeded database locally
    in Docker.

### Fixed

- `explore map` replica folding now recognizes the Snowflake and Postgres dev
  schemas (`snowflake.dev_schema`, `postgres.dev_schema`); previously only
  BigQuery's `dev_dataset` fed the fold, so a mapped Snowflake dev schema
  could inflate one real foreign key into duplicate edges.

## [0.1.4] - 2026-07-06

### Added

- **Snowflake connector**, the second billed cloud connector and the first on
  the compute-time paradigm. Explore, transform, and maintain run against
  Snowflake with the same guardrails as BigQuery, adapted to the cost
  inversion (metadata is free via SHOW commands; scans bill warehouse time):
  - Connection discovery, never prompting: a `connections.toml` entry pinned
    by `snowflake.connection_name`, the default connection, `SNOWFLAKE_*`
    environment variables (including workload-identity tokens, the keyless CI
    path), or a dbt profile. Only a coarse auth method is surfaced.
  - Warehouse-seconds budgets (`--budget`, `budget.ceiling`,
    `budget.session_ceiling`) with the credit translation shown on every cost
    surface, and dollars when `snowflake.credit_price_usd` is configured.
    Estimates are an honestly labeled heuristic (`estimate_quality:
    "heuristic"`; Snowflake has no dry-run) floored by the 60-second resume
    minimum on a suspended warehouse; the budget is hard-enforced regardless
    by a per-statement server-side `STATEMENT_TIMEOUT_IN_SECONDS`. Actual
    wall-clock seconds land in the `.dex/spend.jsonl` ledger as
    `billed_seconds`, kept separate from byte entries so paradigms never sum
    together.
  - Strict warehouse pinning: billed statements run only on
    `snowflake.warehouse`; a connection-default warehouse is never spent on.
    Every session is tagged `QUERY_TAG = 'dex'`.
  - Free-path inventory and profiling estimation from SHOW metadata; batched
    aggregate profiling with semi-structured degradation (VARIANT, OBJECT,
    ARRAY, GEOGRAPHY) and opt-in `SAMPLE SYSTEM` above
    `snowflake.max_full_profile_bytes`.
  - `transform init --connector snowflake`: a dbt-snowflake dev profile from
    the discovered connection (key-pair as a path, SSO as externalbrowser, a
    password only as an `env_var` reference, never a value), writing to a
    dedicated `dev_database.dev_schema` refused as a source, one thread, on
    the pinned warehouse. `transform build` accounts per-node execution time
    into the ledger.
  - The `[snowflake]` extra now carries `dbt-snowflake` and requires
    `snowflake-connector-python>=3.17` (workload-identity support).
  - Testing per the established billed-connector template: a stateful fake
    connection with simulated timing and real connector error types,
    safety-spine extensions across all five families for the compute-time
    paradigm, an env-gated live integration suite (`DEX_TEST_SNOWFLAKE_*`)
    against `SNOWFLAKE_SAMPLE_DATA`, and a scheduled keyless CI job
    (Snowflake workload identity federation, GitHub OIDC). One-time
    provisioning automated by `scripts/setup_snowflake_ci.sh`.

## [0.1.3] - 2026-07-05

Hardening pass from the first billed-connector dogfooding sessions (BigQuery):
the full explore, transform, and maintain loop on a real warehouse.

### Added

- `packages_yml` edit kind: author the project-root `packages.yml` (or
  `dependencies.yml`) through the normal `transform plan`/`apply` contract, so
  declaring a dbt package dependency is a reviewable, hash-pinned diff like every
  other edit instead of a hand-written file outside the guardrail. The edit must
  carry a `packages:` or `dependencies:` list; writes stay confined to the dbt
  project (arbitrary project-root files are still refused).
- `connect test --project` and `--dataset` (repeatable) for BigQuery: convenience
  overrides of the config target, applied in memory only (never written to
  `.dex/config.yml`), so a first smoke test works before a `bigquery:` block
  exists. They mirror DuckDB's `--path`.

### Changed

- `explore map` folds same-lineage duplicate relationships when a dev/replica
  dataset is mapped alongside its source. A replica's models mirror source
  entities and keys, which otherwise inflated one real foreign key into source,
  replica, and cross-dataset lookalike edges; the canonical (source-schema) edge
  is kept, the duplicates are dropped, and the summary notes how many objects
  mirror source lineage. The replica schema is recognized from
  `bigquery.dev_dataset` or structurally (a matching entity and column set in a
  second schema).
- The query firewall's PII refusal now points at an unflagged column that
  plausibly carries the same readable value (for example `inventory_items.product_name`
  when `products.name` is flagged), computed from the cache. The flag itself is
  never weakened and no value is ever surfaced; only the guidance improves.

### Fixed

- dbt subprocess path doubling: with a relative `dbt_project_dir`, `--project-dir`
  and `--profiles-dir` resolved a second time against the already-pinned cwd
  (`project/project`), which broke `transform build` and the `semantic define`
  parse gate on a clean project. The engine now passes absolute dbt CLI paths, so
  a relative project dir no longer needs a hand-edit to an absolute path.
- BigQuery cost estimates now fold in the per-query billing floor (10 MiB per
  referenced table). The dry-run estimate summed raw scanned bytes and ignored
  the floor, so on small data (and fan-out commands like `maintain check`) it read
  far below what must be approved and produced a ladder of budget rejections. The
  surfaced estimate now reflects what BigQuery will bill, so the budget the agent
  proposes clears in one step.
- `maintain` no longer reports phantom dimension-cardinality drift from an
  approximate baseline. It compares an exact current count against the snapshot's
  distinct count, which for a low-cardinality categorical dimension is a
  HyperLogLog estimate; a delta within the sketch's error band is now suppressed
  as noise (the band scales with cardinality, so a genuine new category at low
  cardinality still fires, and an exact baseline still fires on any change).

- `transform init --connector snowflake` on a workload-identity connection
  now refuses with the working alternatives named (key-pair or SSO via
  `snowflake.connection_name`) instead of rendering a profile that references
  a `SNOWFLAKE_PASSWORD` that cannot exist. Stable dbt-snowflake does not
  support workload identity yet; the engine paths (explore, maintain, query)
  are unaffected. Surfaced by the first scheduled Snowflake integration run,
  where the whole suite authenticates keylessly.
- The live `connect test` assertion that no identity crosses the envelope now
  checks identity-shaped keys and credential values instead of a raw username
  substring, which false-positived when the CI username (`DEX_CI`) was a
  substring of the scratch database and warehouse names the envelope
  legitimately reports.

## [0.1.2] - 2026-07-04

### Added

- Maintain: the drift-detection and reconcile engine, closing the ETM loop. It
  compares current reality against the `.dex/snapshot.json` baseline on four
  axes and proposes the fix.
  - `maintain snapshot` captures the baseline: it pins the `.dex/` map (so the
    grain baseline is the exact-distinct verdicts `explore map` computed) plus
    per-layer fingerprints of the dbt project's definitions (file hashes,
    source declarations, semantic models and metrics with their referenced
    columns). Fingerprinting the definitions, not the compiled manifest, keeps
    the baseline stable across dbt versions. Without a cache it captures a
    metadata-only baseline and says the grain and cardinality axes have nothing
    to diff against.
  - `maintain schema` (structural: columns and tables added, dropped, retyped,
    renamed; nullability; dangling sources) and `maintain volume` (freshness:
    row counts that collapsed, emptied, or spiked) read metadata and are free
    on every connector.
  - `maintain grain` (lost key uniqueness and increased join fanout, from exact
    distinct counts and the verified overlap probes) and the categorical
    dimension-cardinality half of `maintain semantic` scan the warehouse, so on
    a billed connector they run the same `--confirm --budget` handshake as
    `explore profile`. `maintain semantic` also does the free half: definition
    changes against the baseline, dangling references, and impact analysis
    tracing warehouse drift through to the affected models and metrics.
  - `maintain check` sweeps every axis, ranked by blast radius (severity plus
    the count of impacted models and metrics). On a billed connector it is
    two-phase: the free axes complete immediately and their findings ride along
    in the `needs_confirmation` envelope with one combined estimate for the
    scanning axes.
  - `maintain reconcile` proposes the fixing edits as a stored plan of
    reviewable diffs, each tagged `mechanical` (a schema re-scaffold of a
    dex-generated staging model) or `advisory` (a decision surfaced, at most
    backed by a visibility test). Applied with `transform apply <plan-id>`, so
    the human-edit conflict handshake is inherited; reconcile itself writes
    nothing.
  - New-categorical-value detection is a cardinality delta only: no dimension
    value is ever stored in `.dex/` or surfaced in the envelope (naming a new
    value is left to a firewalled `explore query`).
- `.dex/drift.json`: a non-canonical cache of the last detection report, so
  `reconcile` reads what `check` found instead of re-scanning; axes merge across
  focused runs but are dropped when the baseline changes.

## [0.1.1] - 2026-07-04

The first cloud connector: the full explore and transform loop runs on BigQuery
with hard cost guards, alongside the existing DuckDB path.

### Added

- BigQuery adapter (`--connector bigquery`, behind the `[bigquery]` extra):
  free API-metadata inventory (never `INFORMATION_SCHEMA`), batched aggregate
  profiling with nested-type (`STRUCT`/`ARRAY`/`JSON`) handling, metadata-only
  degradation for partition-filter-required tables, and opt-in `TABLESAMPLE`
  block sampling for very large tables (`bigquery.max_full_profile_bytes`).
- Credential discovery for BigQuery: Application Default Credentials only
  (user, service account, impersonated, or federated), never a prompted or
  pasted key; the project resolves from `.dex/config.yml`, the environment,
  the ADC default, or a dbt profile, and every failure names the fix.
- Bytes-scanned cost guards: every billed command is estimated with free
  dry-runs and returns `needs_confirmation` until re-issued with
  `--confirm --budget <bytes>`; every job carries a server-side
  `maximum_bytes_billed` cap; billed bytes land in a `.dex/spend.jsonl` ledger
  (byte counts and statement hashes, never SQL text); and
  `budget.session_ceiling` bounds cumulative spend per UTC day against that
  ledger. Over-ceiling estimates are refused outright and confirmation cannot
  override them.
- `bigquery:` config block (`project`, `datasets` allowlist supporting
  qualified `project.dataset` entries such as public datasets, `location`,
  `dev_dataset`, `max_full_profile_bytes`).
- `transform init --connector bigquery`: renders a dev-only dbt profile with
  `method: oauth` (ADC, no secrets), pointed at a dedicated dev dataset that
  is refused when it collides with a source dataset; `transform build` on
  BigQuery requires `--confirm` with a `--budget`, inherits the profile's
  per-statement `maximum_bytes_billed` cap, and records billed bytes from
  dbt's run results into the spend ledger. The `[bigquery]` extra now carries
  `dbt-bigquery`.
- Live BigQuery integration suite (`tests/integration/`), gated on
  `DEX_TEST_BQ_*` environment variables and skipped otherwise, reading public
  datasets with per-query byte ceilings; a scheduled `integration.yml`
  workflow authenticates via Workload Identity Federation (no stored keys).
- Safety-spine coverage for the billed paradigm: SELECT-only in the BigQuery
  dialect (scripting, `MERGE`, `EXPORT DATA`, `CALL` refused), the
  unconfirmed-never-executes and over-ceiling-cannot-confirm guarantees, the
  server-side cap on every job, PII firewall checks for BigQuery
  value-carrying aggregates (`ANY_VALUE`, `ARRAY_AGG`, `STRING_AGG`,
  `TO_JSON_STRING`), secret-free generated profiles, and a sanitizer-checked
  capabilities payload (principal type only, never an identity).

### Changed

- Explore envelopes on billed connectors now stamp the preflight `cost` and
  report actual spend under `data.spend`; `connect test` reports the
  connector's cost paradigm and performs a real API round-trip (a stale
  credential no longer reports a healthy connection).
- The query firewall and `explore query` now parse in the active connector's
  SQL dialect instead of assuming DuckDB.
- Relationship verification probes are authored in portable SQL and transpiled
  per connector (BigQuery lacks `FILTER (WHERE ...)`).

## [0.1.0] - 2026-07-03


Hardening pass from two same-day dogfooding sessions across the full
explore/transform/semantic loop. The theme: stop layers from vouching for
something they did not fully check, and stop assuming a repo with nothing already
in it.

### Added

- `transform deps`: install or refresh dbt packages (repo-confined, no warehouse
  spend, no cost gate). `transform build` now also runs `dbt deps` automatically
  when the project declares packages but `dbt_packages/` is missing, so a project
  with dependencies builds on the first try.
- `semantic plan`: accepts a mix of new and existing names in one payload and
  classifies per name, reporting `defined` and `updated`, so one logical change no
  longer forces separate define and update calls.
- Authoritative validation for semantic plans: beyond MetricFlow's schemas, the
  engine resolves every metric input (ratio and derived inputs must reference
  metrics, not measures) and runs the emitted YAML through dbt's own parser against
  a throwaway copy of the project before the plan is stored. A plan that cannot
  parse is never stored. When dbt is unavailable the check degrades to a warning;
  `--no-parse` skips it.
- `transform plans`: list stored plans, pending and applied, newest first.

### Changed

- `explore map` no longer caps silently: past 50 objects it profiles the top
  `profile_top_n` (default 25) by rank and states the cutoff and `skipped_count` in
  the summary. On a re-map, objects outside this run's top set carry their prior
  profiles forward (`carried_forward_count`), each stamped with its own
  `profiled_at`, so coverage accumulates across runs.
- `transform apply` with no plan id applies the latest unapplied plan of any kind
  (semantic plans included), absorbing the one behavior `emit dbt` used to add.

### Removed

- The `emit` command group is gone. `emit dbt` was a redundant spelling of
  `transform apply` for semantic plans; its only distinct behavior (default to the
  latest unapplied plan) now lives on `transform apply`, so a stored semantic plan
  is applied the same way as any model plan. `emit osi` and the dormant OSI
  exporter (`exporters/`, the pinned `osi-schema.json`, and the OSI reference docs)
  are removed with it: dex reasons over the dbt project and authors into it
  directly, and does not project the model back out into other formats. This is a
  deliberate contract break, taken while pre-1.0; update any `emit dbt` call to
  `transform apply`. The base `jsonschema` dependency, which only the OSI validator
  used directly, is dropped.

### Fixed

- False "grain unknown" verdicts: approximate distinct counts could overshoot a
  genuinely unique column and hide a real key. Profiling now escalates near-unique
  columns to an exact `COUNT(DISTINCT)` (batched, read-only, bounded per table),
  and only an exact count is allowed to confirm a key or a table's grain.
- dbt subprocesses now pin their cwd to the project dir, so a relative `path:` in
  `profiles.yml` resolves inside the project instead of silently creating a stray
  empty database at the caller's shell cwd. A missing dev DuckDB database is an
  actionable refusal when the project reads from sources, a warning otherwise.
- Build failure envelopes surface the real cause: `errors[0]` carries the first
  actual dbt message, the rest land in `warnings`, deduplicated and per-entry
  capped, with a pointer to the full log when anything was trimmed (previously the
  cause was buried under kilobytes of duplicated tracebacks).

## [0.1.0a6] - 2026-07-03

### Added

- `transform init "<name>" --connector <c>`: engine-owned dbt project bootstrap,
  so an empty repo no longer hits a wall on step one. Renders a deterministic
  skeleton (`dbt_project.yml`, `models/staging/` and `models/marts/`, a
  project-local `profiles.yml` with a single duckdb `dev` target wired to the
  known warehouse) and records `connector`, `dbt_project_dir`, and
  `dbt_target: dev` in `.dex/config.yml`, all reported as create diffs. Strictly
  additive: refuses wherever a dbt project already exists. Unlike the read-only
  commands, init never falls back to a default connector (it bakes the connector
  into the generated profile): `--connector` wins, a committed `connector:` in
  `.dex/config.yml` is accepted and attributed in the envelope, and bare init is
  an error listing the valid connectors. DuckDB is the supported connector
  today; the cloud connectors return an actionable not-yet-supported error until
  their dbt adapters ship.
- Safety-spine coverage for init: refuses over an existing project, no connector
  fall-through, the generated profile is dev-only with no prod-named target and
  no secret-like keys, and the generated project round-trips through the loader
  and a real gated `dbt build`.

### Changed

- `.dex/config.yml` writes now persist only fields that were explicitly loaded
  or assigned, so the committed file records choices instead of every engine
  default.

## [0.1.0a5] - 2026-07-03

The authoring half of the loop goes live on DuckDB. `transform plan|apply|build`,
`semantic define|update`, and `emit dbt` now do real work; `maintain`, `emit osi`,
and `viz preview` still report `not_implemented`.

### Added

- The dbt project reader/writer (`dbt_project.py`): loads `dbt_project.yml`, the
  source files under the model paths, and `target/manifest.json` when compiled;
  resolves profile targets to name and adapter type only (credentials never leave
  the engine); writes plan edits back all-or-nothing with sha256 conflict
  detection, so a human edit made after planning is surfaced as a diff and never
  overwritten.
- Transform plans: agent-authored edits arrive via `--edits-file <path|->` (JSON:
  `{"edits": [{"path", "kind", "content"}]}`), are validated per kind (model SQL
  must be a single read-only SELECT once jinja is stripped; YAML must parse;
  semantic YAML is validated against MetricFlow's schemas via
  dbt-semantic-interfaces), diffed against the current project, and stored under
  `.dex/plans/<plan-id>.json`. Plan ids are content-addressed, so re-planning the
  same change is idempotent.
- `transform plan --scaffold <table>` (repeatable): deterministic staging
  skeletons (`stg_<table>.sql`, per-model YAML, one shared sources file) from the
  `.dex/` cache, with key tests and PII flags propagated into column `meta`,
  never example values.
- `transform build`: dev-target `dbt build` as an isolated subprocess with
  `--target`/`--select`, summarized from `run_results.json` (no raw log text in
  the envelope). Prod-looking targets (`prod`, `production`, `prd`, `live`,
  `release`, `main`) are refused outright, before the cost gate, and config
  cannot whitelist them.
- The cost guard (`guards/cost_guard.py`): preflight-before-spend with a strict
  order (over-ceiling blocks even when confirmed; billed paradigms require a
  ceiling; unconfirmed commands return `needs_confirmation` with the cost).
  DuckDB is free but the confirm handshake still gates `transform build`.
- Semantic authoring: `semantic define` refuses names already in the project,
  `semantic update` requires them; `emit dbt [plan-id]` applies the semantic
  plan's YAML (latest unapplied by default) through the same conflict-checked
  write path.
- Unified-diff rendering (`diffs.py`) feeding the envelope's `diffs` field, and a
  `needs_confirmation` envelope builder.
- `dbt_project_dir` in `.dex/config.yml` to pin the dbt project when discovery
  would be ambiguous.

### Changed

- Transform logic moved into its own `transform/` package (commands over pure
  engine modules), mirroring the `explore/` layout; the pre-refactor top-level
  stubs (`transform.py`, `semantic.py`, and the explore-era orphans) are removed.
- The three transform-touching safety-spine tests (prod-target refused,
  cost-guard binds, changes-are-diffs) are now real assertions instead of
  `xfail` placeholders, joined by an apply-refuses-overwrite case.

## [0.1.0a4] - 2026-07-02

### Added

- `explore query "<SELECT ...>"`: guarded ad-hoc SQL. The agent authors the
  query; the engine's new query firewall refuses or bounds it. Values may cross
  the envelope only from profiled, PII-cleared columns (every value path from a
  flagged column must pass through a measuring aggregate such as COUNT or AVG;
  MIN/ANY_VALUE/STRING_AGG and unknown functions fail closed). Results are
  columnar and hard-capped (rows, cell width, payload bytes, wall time), with
  every cut announced. Requires the `.dex/` cache, so profiling precedes probing.
- `.dex/queries.jsonl`: an audit log of every query decision (allowed, refused,
  failed) with SQL text and counts, never result values.
- `--verify` on `explore relationships` and `explore map`: measures each
  inferred join with one aggregate overlap probe and adjusts its confidence;
  relationships now carry `verified` and `orphan_fraction`.
- A probe playbook shipped with the `explore` skill: recipes mapping common
  analyst questions to effective, firewall-friendly probe shapes.
- Configurable `query:` limits in `.dex/config.yml` (`max_rows`,
  `max_cell_chars`, `max_payload_bytes`, `timeout_seconds`).

### Changed

- The boundary guarantee is stated precisely: nothing reaches agent context
  except through the sanitized envelope; credentials never, and data values only
  from profiled, PII-cleared columns, bounded and capped. Previously the docs
  said "raw rows never cross", which the guarded query path deliberately
  refines.
- The adapter protocol gains `run_query` (bounded, watchdog-interrupted
  execution of firewall-approved SQL); DuckDB implements it, cloud stubs do not
  yet.

- PII detection catches common name and contact columns, not just exact tokens:
  bare `name` and generic `*_name` columns (with a denylist of technical
  qualifiers like `table_name`), camelCase names (`firstName`), and free-text
  fields (`comments`, `notes`, `message`, `feedback`) under a new `free_text`
  category. Every new flag suppresses min/max the same way existing categories do.
- Grain and data-quality interpretation in `explore profile` and `explore map`:
  a non-unique id column now produces an explicit fan-out warning with the
  duplicate count, a table with no candidate key reports "grain unknown", and
  `profile` populates `candidate_keys` and `grain` (previously `map`-only).
- `explore relationships` and `explore map` envelopes carry `notes` explaining
  what inference examined, so an empty relationships array is distinguishable
  from "did not try".
- `explore profile` accepts comma-separated object lists in addition to
  space-separated ones.

### Changed

- Relationship inference now recognizes camelCase foreign keys (`raceId`),
  strips warehouse-layer prefixes (`raw_`, `stg_`, `dim_`, ...) when matching
  parent tables, matches parents keyed on `<entity>Id` / `<entity>_id` (not just
  `id`), and refines confidence with distinct-count and numeric-range
  containment from the aggregates already profiled. A parent whose key is not
  unique still yields the join at reduced confidence instead of being dropped.
- The `.dex/` cache schema version is now 2 (new `free_text` PII category).
- The skill wrappers drop `VIRTUAL_ENV` from the engine subprocess environment,
  silencing uv's mismatch warning on every call.
- The `explore` skill description triggers on casual, artifact-first prompts
  ("what's in my duckdb") in addition to analyst phrasings.

## [0.1.0a3] - 2026-07-01

### Changed

- Skill wrappers pin only the engine version; the connector extra is now selected
  at runtime from the active connector (an explicit `--connector`, then
  `.dex/config.yml`, then DuckDB), so a published release is connector-neutral
  instead of hard-coded to `[duckdb]`. The release tooling verifies the version
  pin rather than a connector-specific string.

### Added

- An `all` extra on `exmergo-dex-core` that installs every connector at once, for
  users who drive more than one warehouse. The light default and the `[duckdb]`
  on-ramp are unchanged.

## [0.1.0a2] - 2026-07-01

The ETM taxonomy correction. The three motions are now Explore, Transform, and
Maintain (previously Explore, Transform, Model). Explore remains the only live
stage; Transform and Maintain report `not_implemented` until they land.

### Changed

- The tagline and third motion: **Explore. Transform. Maintain.** "Model" is
  retired as a verb because it is overloaded (dbt model, data modeling, semantic
  model, LookML, ML); the ETM acronym is preserved.
- Semantic-layer authoring folds into the `transform` skill as a first-class
  capability. There is no separate `model` skill; both dbt SQL models and dbt
  semantic models are authored as reviewable diffs to the dbt project.
- Reconcile is promoted from an unnamed cross-skill behavior to the `maintain`
  skill, now backed by a real command group: `snapshot` (baseline), `check`
  (sweep), the per-axis detectors `schema` / `grain` / `semantic`, and
  `reconcile` (propose fixing diffs). Detection is read-only; only reconcile
  emits diffs. Manual and free; continuous, governed maintenance stays the
  commercial product.
- The engine CLI renames the `model` command group to `semantic`
  (`semantic define|update`), removing the "model" overload from the surface.

## [0.1.0a1] - 2026-06-30

First public alpha. The Explore stage of the ETM loop runs end to end on DuckDB;
the rest of the loop is scaffolded and reports `not_implemented` until it lands.

### Added

- Explore on DuckDB, fully read-only: ranked inventory, selective column
  profiling, PII flagged as (column, category, confidence) with no example
  values, and inferred plus declared relationship discovery.
- The dex-core command contract and sanitized JSON stdout envelope; credentials
  and raw rows never cross the boundary.
- The dbt project as the source of truth, with a non-canonical `.dex/` cache for
  exploration artifacts and the reconcile snapshot.
- A dormant OSI exporter validated against a pinned `osi-schema.json`; no OSI is
  emitted in this release.
- The Tier-1 safety spine: read-only enforcement, SELECT-only generation,
  prod-target refusal, cost preflight before any spend, PII flagged not
  surfaced, and propose-don't-impose diffs.
- The three skills (`explore`, `transform`, `model`) with thin `uv run` wrappers
  pinned to the engine.
- The Tier-2 agent-eval harness (`evals/`): triggering, output-quality, and
  uplift-over-baseline scoring behind a swappable agent backend.
- Release pipeline: tag-derived versioning via hatch-vcs, wrapper-pin coupling
  verification, and PyPI publishing through Trusted Publishing (OIDC).

### Not yet implemented

- The Transform, Model, and Reconcile stages of the loop.
- The cloud and operational connectors (BigQuery, Snowflake, Databricks,
  PostgreSQL) and their cost paradigms.

[Unreleased]: https://github.com/exmergo/dex/compare/v0.1.0a3...HEAD
[0.1.0a3]: https://github.com/exmergo/dex/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/exmergo/dex/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/exmergo/dex/releases/tag/v0.1.0a1
