# dex: driving the engine from any agent

dex is the agent-native analytics engineering toolkit. All logic lives in one
portable engine, `exmergo-dex-core`; this file tells any coding agent how to drive
it. On Claude Code the three skills (`explore`, `transform`, `maintain`)
auto-trigger and do this for you. On other agents, follow the contract below
directly. The guardrails and outputs are identical because they live in the
engine, not here.

## The loop: Explore, Transform, Maintain (ETM)

1. **Explore** an unfamiliar warehouse or DuckDB database: rank what matters,
   profile selectively, infer joins, persist a draft map.
2. **Transform** the dbt project: author and refactor dbt SQL models (staging to
   marts) with tests and docs, and author the semantic layer on top (entities,
   dimensions, measures, metrics) as dbt semantic models (MetricFlow YAML). Both
   are the same job, reviewable diffs to the dbt project.
3. **Maintain** the project as the world changes: diff the current warehouse and
   dbt against the last `.dex/` snapshot, surface schema, volume, grain, and
   definition drift, and propose the reconciling edits.

## The command contract

The engine exposes one small, stable command surface. Run a subcommand, read the
single JSON envelope it prints to stdout, decide the next step. State persists in
`.dex/`, so subcommands are stateless and you orchestrate multi-step flows.

```bash
uv run python -m exmergo_dex_core <subcommand> [flags]
# or, with the pinned wrapper a skill ships:
uv run scripts/run.py <subcommand> [flags]
```

Both forms need [`uv`](https://docs.astral.sh/uv/) on `PATH`: it is what installs
and runs the engine, and no agent host installs it for you. Without it the shell
reports `uv: command not found`, and the shipped wrapper run directly refuses with
an error envelope (`reason: prerequisite`) naming the fix. The install is one line
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, or `brew install uv`, or
`pipx install uv`); relay it to the user rather than reaching for another way to do
the work, since every guardrail below lives in the engine.

Install the engine with the connector extra you use: `exmergo-dex-core[duckdb]`
for the zero-credential on-ramp, or `[snowflake]`, `[bigquery]`, `[databricks]`,
`[postgres]`, `[redshift]`, `[clickhouse]`, or `[all]` for every optional capability at once. The shipped wrapper pins
only the engine version and selects that extra for you at runtime from the active
connector (an explicit `--connector`, then the `connector:` in the `.dex/config.yml`
found by walking up from the run directory to the git root, then DuckDB), so a
release is connector-neutral. The engine resolves the same way but does not default
silently: with no `.dex/config.yml` anywhere up the tree and no explicit
`--connector`/`--path`, it refuses and names the fix rather than reading a phantom
DuckDB target, so a command run from a subdirectory of your project resolves the
project's real config instead of a wrong default.

With no warehouse to point at yet, `demo` generates one and writes the config for
it, so `demo` then `explore map` is a working first run on a machine with no
credentials and no network.

| Subcommand | Returns |
|---|---|
| `demo [path]` | generates a seeded local DuckDB warehouse (7 tables, 29,512 rows) plus a `.dex/config.yml` beside it, so a first run needs no warehouse, no credentials, and no network; both artifacts are listed in `data.created` and `data.next_steps` names the commands worth running next. The path is positional and resolves against the working directory, defaulting to `dex_demo.duckdb`; `--path` is refused here rather than honored, since everywhere else it names the warehouse dex *reads*. Create-only, with no `--confirm` that can talk past it: an existing file at the target is a refusal (`reason: guard`), a missing parent directory is a refusal (`reason: request`), no directories are ever created, and a `.dex/config.yml` at or above the target is left untouched with a warning rather than shadowed by a second one. The data is generated from a pinned seed, so the counts quoted in the docs are the counts a user sees, and it is deliberately flawed: a key that lost uniqueness to a double-loaded batch, a key mixing two id schemes, a join whose columns share a name and none of their values, an empty table, two columns whose declared type contradicts their content, and personal data alongside two designed false positives. Needs the `[duckdb]` extra and says so by name when it is absent (`reason: prerequisite`) |
| `connect test` | capabilities, dialect, `read_only: true`; DuckDB takes `--path`, every warehouse connector takes repeatable `--scope` (a bare database on ClickHouse, whose identifiers are two-part `database.table`) (BigQuery also accepts its older `--project`/`--dataset`), never written to config; Snowflake and Databricks report the pinned warehouse and its credit or DBU rate |
| `explore inventory [--rank]` | ranked object summary (counts, sizes; no rows) |
| `explore profile <objects>` | column profiles + PII flags (column, category, confidence) + candidate keys, grain, data-quality warnings; `--use-project` lets a semantic model's declared primary entity override the heuristic grain (disagreements noted) |
| `explore relationships [--verify] [--use-project]` | inferred joins with confidences, plus notes on what inference examined; `--verify` measures each join with an aggregate overlap probe, declared and inferred alike (a composite-key join is not probed: the probe spans one column pair); `--use-project` folds in the dbt project's declared foreign keys at confidence 1.0 (a declared join wins over the same inferred edge). A measurement never revises a declared join's confidence, which stays at the 1.0 the project asserts; a declared join whose probe finds the parent largely missing is reported as a finding instead |
| `explore map [--verify] [--use-project]` | writes/updates the `.dex/` map; prints a summary; `--use-project` additionally applies declared grain and ranks metric-backing models higher |
| `explore diagram [--full]` | the `.dex/` map serialized as a Mermaid `erDiagram` under `data.mermaid`, plus an `entities` legend mapping each entity name back to its fully-qualified identifier. Free and connectionless: it reads the cache and never opens the warehouse, so it needs no credential and cannot spend. Declared joins are solid, inferred joins dotted, and a cardinality is drawn only where the cache proved it (an unverified inference never claims "exactly one"). The default draws profiled objects that participate in a join, with their grain, key, join, and PII-flagged columns; `--full` widens to every eligible object and column. An entity cap always binds and every elision is counted in `notes`. No column value ever appears; PII renders as category and confidence. dex writes no file: reproduce the string in a fenced ```mermaid block, or save it yourself |
| `explore query "<SELECT ...>" [more...]` | runs agent-authored SELECTs through the query firewall: columnar, capped results; values only from profiled columns whose PII flag is absent or below the 0.5 blocking threshold (sub-threshold projections warn in the envelope); the FROM clause may unnest JSON/array columns in the connector's native idiom (UNNEST, LATERAL FLATTEN, LATERAL VIEW EXPLODE, set-returning functions, PartiQL) when the unnested value derives from a queried table's column, with the outputs inheriting that column's flags. The positional is variadic, so a chain of questions is one call: each argument is one statement, adjudicated, executed, and ledgered on its own, and `--sql-file <path>` reads a larger batch from a file (one statement per line, or semicolon-separated). Several statements in one string is still refused, so batching never widens what a call may do. One statement returns the envelope described here; two or more return `data.results`, one entry per statement carrying this same `columns`/`types`/`cells`/`row_count`/`truncated` shape plus its own `status` (`ok`, `refused`, `failed`, `skipped`) and `error`, so a refusal on the third does not discard the first two, and the envelope's own status is `error` whenever any statement failed. `query.max_payload_bytes` is the budget for the whole call rather than for one statement, and `query.max_statements` (default 10) refuses an oversized batch. An object a statement names that the connection has but the cache cannot adjudicate (never profiled, inventoried without column detail, or profiled against a column signature the warehouse has since changed) is profiled first and the statement then runs, with a warning naming what was profiled and `data.profiled_on_demand` listing it; that profile is a full one, so the flags governing the query are the flags a deliberate `explore profile` would have produced. On a metered connector it is priced, not implied: one handshake covers the profiles and every statement together, itemized per table and per statement, and the objects a whole batch needs are scanned once rather than once per statement. An object the connection does not have refuses only the statements that named it, naming the connection rather than the cache. `--no-auto-profile` (or `auto_profile: false` in `.dex/config.yml`) restores the strict prerequisite, and on that path nothing opens a connection before the firewall has spoken |
| `explore cluster <object> [--features a,b] [-k N]` | k-means over a bounded, column-pruned, dialect-sampled scan of numeric columns; returns cluster sizes + centroids (feature means) + silhouette, never rows; auto-selects non-PII, non-key numeric features (or takes `--features`; a named PII column is opt-in, mean only); needs the `[cluster]` extra; profiles the named object on demand exactly as `explore query` does, including `--no-auto-profile`, and reports it under `data.profiled_on_demand`; billed connectors take the cost handshake, and where a profile was needed the sample scan is priced after it (the feature columns come out of that profile), so a budget too small for the sample comes back as `needs_confirmation` with the profile already saved rather than as a refusal |
| `explore semantic list [--local\|--api]` | discover the dbt semantic layer in one shape from either backend: metrics (name, type, label, description, queryable dimensions), dimensions, and entities. Local reads the compiled `target/semantic_manifest.json` (no extra); hosted introspects the dbt Cloud GraphQL API. Distinct from the top-level `semantic` group, which *authors* the layer; `explore semantic` *queries* it |
| `explore semantic query <m[,m]> [--metric <m[,m]>...] [--group-by <entity__dim[,dim]>...] [--where "<jinja>"] [--order-by <c[,c]>] [--grain <g>] [--limit N] [--local\|--api]` | run a governed metric query. Metrics are positional after the explicit `query` mode; the repeatable `--metric` spelling remains supported. Metric, `--group-by`, and `--order-by` values each accept comma-separated lists, and the flags may be repeated; `--where` never splits, because a filter clause carries its own commas. Local (`[semantic]` extra): MetricFlow `explain()` renders the SQL and dex executes it through its own connector, PII request-gate, SELECT-only assertion, relation pre-check against the connection's own inventory (a relation the connection does not have is refused before spend; a relation it has but has never profiled is queryable, with a note that PII screening fell back to the name heuristic), and cost handshake, so cost is surfaced before spend. Hosted (`[semantic-api]` extra): dbt Cloud executes server-side, so the cost guard cannot apply and every result warns so (see guardrail 4); PII is screened from the layer's metadata plus a name heuristic before the query is sent, and the service token never crosses the envelope. Either backend discloses on the result when only the name heuristic could screen a dimension, so weaker screening is never mistaken for evidence. Backend is ambient (`.dex/config.yml` `semantic.backend`), overridable with `--local` / `--api` |
| `transform init "<name>" --connector <c>` | bootstrap a dbt project skeleton (`dbt_project.yml`, `models/staging/` + `models/marts/`, a dev-only `profiles.yml`), reported as create diffs; refuses if any dbt project exists; the connector never defaults, so bare init errors (an explicit flag or a committed `connector:` in `.dex/config.yml` is required); `--layered-schemas` additionally scaffolds `models/intermediate/`, a `generate_schema_name` override, and per-folder `+schema:` config so each layer builds into its own `<layer>_<target name>` schema; init also runs a free, metadata-only content check on every namespace the project would build into and warns (never refuses) when one already holds tables or views, degrading to a note when no connection opens |
| `transform plan "<intent>" --edits-file <f>` | proposed dbt edits as diffs (nothing applied); `--scaffold <table>` adds a staging skeleton from the cache; when an edited model already exists, every authored change that can move rows (a `WHERE`/`HAVING`/`QUALIFY` predicate, a join added, removed or retyped, a swapped driving relation, a `DISTINCT` or `GROUP BY` change) is named under `data.row_attribution`, and each is measured against the prior model as a common baseline alongside the whole-model net; column expressions, aliases and ordering report nothing; measuring runs unasked only on DuckDB (free) and needs `--attribute-rows` plus the usual `--confirm --budget` on a billed connector, where the ask rides back beside the already-stored plan; a change dex cannot isolate or measure (macro-generated SQL, a jinja conditional, a renamed CTE, an unprofiled parent) reports `attributed: false` with the reason, and nothing here ever refuses a plan |
| `transform apply [plan-id]` | writes diffs into the dbt project (a reviewable git diff); a human edit since planning returns `needs_confirmation`, never an overwrite; no id applies the latest unapplied plan of any kind |
| `transform plans` | list stored plans, pending and applied, newest first |
| `transform macro [name]` | no name lists the shipped dbt macros; a name proposes scaffolding it into the project's macro directory as a plan (dbt-parse-checked, applied with `transform apply`); re-running diffs the project's copy against the shipped version |
| `transform build --target dev` | prod-looking targets refused outright; then a free dev-target preflight (refuses when `.dex/config.yml` and the rendered `profiles.yml` disagree, or when the dev database does not exist, naming the fix); then the cost preflight, priced upfront by a free `dbt compile` dry-run of each node (a partial floor when a cold dev target has not built a node's inputs yet; degrades to no estimate when dex cannot open its own connection); runs only with `--confirm` and a budget; cwd pinned to the project dir; auto-runs `dbt deps` when packages are declared but not installed |
| `transform deps` | install/refresh dbt packages (repo-confined; no warehouse spend) |
| `semantic define\|update\|plan ... --edits-file <f>` | dbt semantic model edits as diffs; validated up to and including dbt's own parser (a throwaway project copy) before the plan is stored; `plan` accepts a mix and classifies per name; degrades to a warning when dbt is absent, `--no-parse` skips; applied with `transform apply` like any other plan |
| `maintain snapshot` | capture/refresh the known-good baseline in `.dex/snapshot.json` (pins the `.dex/` map + per-layer definition fingerprints) |
| `maintain check` | sweep every drift axis vs the snapshot; ranked drift report (read-only); two-phase on billed connectors (free axes now, one estimate for the scanning axes) |
| `maintain schema [<objects>]` | structural drift: columns/tables added, dropped, retyped, renamed; nullability; dangling sources (free) |
| `maintain volume [<objects>]` | freshness drift: row counts that collapsed, emptied, or spiked (free metadata) |
| `maintain grain [<objects>]` | cardinality/identity drift: lost key uniqueness, changed grain, join fanout (scans; gated on billed connectors) |
| `maintain semantic [<objects>]` | definition drift and dangling refs (free) plus categorical dimension cardinality change (scans; gated on billed connectors) |
| `maintain reconcile [<class>]` | propose the dbt edits that reconcile detected drift, as a stored plan of diffs tagged mechanical or advisory (never applied; apply with `transform apply <plan-id>`) |
| `viz preview` | emit the dbt semantic model to the Viz preview (not yet implemented) |

Skill-to-subcommand mapping: `explore` fronts `demo`/`connect`/`explore`;
`transform` fronts `transform`, `semantic`, and `viz`; `maintain` fronts the whole
`maintain` group. Within `maintain`, detection (`check`, `schema`, `volume`,
`grain`, `semantic`) is read-only; only `reconcile` emits diffs, and applying
them is `transform apply`. Detection is read-only on every connector, but read-only
is not free: `schema`, `volume`, and the reference half of `semantic` are metadata
(free everywhere), while `grain` and the dimension-cardinality half of `semantic`
scan and go through the `--confirm --budget` handshake on billed connectors. The
engine does not care which skill fronts a subcommand.

Authored content reaches the engine through `--edits-file <path>` (or `-` for
stdin): a JSON payload of `{"edits": [{"path", "kind", "op", "content"}, ...]}`
with `kind` one of `model_sql`, `schema_yml`, `semantic_yml`, `packages_yml` (the
guarded way to author the project-root `packages.yml`/`dependencies.yml`, so
declaring a dbt package is a reviewable diff too), `macro_sql`, `project_yml`
(the project-root `dbt_project.yml`), or `profiles_yml` (the project-root
`profiles.yml`, secret-guarded so a credential never enters the diff: reference
secrets via `{{ env_var('NAME') }}`). `op` is `upsert` (create or update, the
default, carrying `content`) or `delete` (remove the file, no `content`); a
delete is a reviewable diff too, guarded so the plan is refused if any surviving
file still `ref()`s a deleted model, and a rename is one plan (delete old, create
new, update the referrers). The engine validates, diffs, and stores the plan
under `.dex/plans/`; nothing touches the dbt project until `transform apply`. See
`references/command-contract.md`.

### The envelope

Every command prints exactly one JSON object and nothing else:

```json
{ "status", "data", "cost": { "estimate", "ceiling", "paradigm" }, "warnings", "diffs", "errors" }
```

Cost is a preflight estimate surfaced **before** any spend. Any command that
would spend requires an explicit `--confirm` and a session budget: on a
metered connector (BigQuery, Snowflake, Databricks, Redshift, Postgres, and
ClickHouse)
the first call returns `needs_confirmation` with a free estimate, and the
same command is re-issued with `--confirm --budget <magnitude>` once the user
has agreed to the spend. The magnitude is paradigm-relative: **bytes** on
BigQuery (an exact free dry-run figure), **warehouse-seconds** on Snowflake
(a heuristic labeled `estimate_quality: "heuristic"`, with a credit
translation alongside) and on Databricks (a floor labeled
`estimate_quality: "low"` that sharpens itself inside the confirmed budget,
with a DBU translation alongside), **compute-seconds** on Redshift (a
heuristic with an RPU-hour translation alongside; Serverless estimates carry
the 60-second wake minimum once), **database-seconds** on Postgres (nothing
is billed in dollars; the guarded quantity is load on the operational
database, estimated free via EXPLAIN) and on ClickHouse (self-hosted, also
billing no dollars; estimated free via the non-executing EXPLAIN ESTIMATE,
which prices after primary-key pruning). On every time paradigm the budget
still binds exactly via a server-side statement timeout, except on ClickHouse,
where it binds via `max_execution_time` **and** `max_bytes_to_read`, because
time alone is checked only at block boundaries there. Actual spend comes
back under `data.spend` (`bytes_billed` or `seconds_billed`) and accumulates
in the `.dex/spend.jsonl` ledger per connector. Credentials never appear in
`data` (BigQuery authenticates via discovered Application Default
Credentials, Snowflake via a discovered `connections.toml` entry,
environment, or dbt profile, Databricks via the SDK's unified chain, Redshift
via the AWS credential chain (a pinned Serverless workgroup mints IAM
temporary database credentials) or the `REDSHIFT_*` environment, Postgres via
`pg_service.conf`, `DATABASE_URL`, the `PG*` environment, or a dbt profile,
ClickHouse via `CLICKHOUSE_URL`, the `CLICKHOUSE_*` environment, a committed
non-secret target, or a dbt profile, and
the hosted semantic layer via `DBT_SL_TOKEN` or `~/.dbt/dbt_cloud.yml`; never a
pasted key or token), and result values appear only in
`explore query`'s columnar payload after the query firewall has cleared
them.

## Guardrails (non-negotiable, enforced in the engine)

1. Sense-making, not enumeration. Never dump a schema.
2. Profile, don't exfiltrate. Understanding is built from aggregates, not raw rows.
3. Read-only against data; writes confined to the repo. DuckDB opens read-only;
   generated SQL is SELECT-only; agent-authored SQL runs only through the query
   firewall; builds run against a dev target only, never prod. `dex demo` is the
   one verb that creates a data file, and the exception is narrower than the rule
   it sits inside: it only ever creates, refusing rather than overwriting and with
   no confirmation flag that can override that, so it cannot open, inspect, or
   replace a warehouse it did not make. The generator sits on its own path and
   never reaches a connector, which is why the read-only open above has no branch
   it could take; the moment the file exists it is user data and is read like any
   other warehouse.
4. Cost-aware by connector. Nothing dex runs touches the warehouse without a
   ceiling. The source allowlist in `.dex/config.yml` is a committed cost
   boundary: `--scope` narrows it for one command and can never widen it, and a
   scope that names nothing is refused rather than dropped. The one place dex
   cannot enforce a ceiling is the hosted dbt Cloud Semantic Layer
   (`explore semantic query --api`): dbt Cloud owns the warehouse connection and
   executes the query server-side, so no dry-run estimate and no `maximum_bytes_billed`
   are possible from dex. That backend therefore runs without a `--confirm`
   handshake and instead states, explicitly and on every result, that the cost
   guard is unavailable and spend is governed by the dbt Cloud environment, not
   by dex. The local backend (`--local`) executes through dex's own connector and
   keeps the full cost-before-spend handshake. `budget.session_ceiling` binds
   across commands that overlap in time as well as across commands that follow
   one another: an admitted command books its estimate against the day's
   headroom before it runs, so issuing several billed commands at once cannot
   spend the same budget twice. If a cache backend cannot serialize that, every
   billed command says so.
5. Nothing reaches agent context except through the sanitized envelope.
   Credentials never; data values only from profiled, PII-cleared columns,
   bounded and capped.
6. PII is flagged (column, category, confidence), never surfaced, and a flag is
   never removed by evidence: value-shape statistics computed in the profiling
   scan only move its confidence, in both directions and fail-closed. The query
   firewall enforces the policy on agent SQL: any expression that would carry
   values from a column flagged at confidence 0.5 or above is refused (the
   threshold is a hard-coded engine constant); a projection of a lower-confidence
   flag runs with an envelope warning. A human clears a reviewed column durably
   with a `pii_overrides` entry in `.dex/config.yml`, never by editing the cache.
7. Persistence is git, not a service. The dbt project is the source of truth; the
   `.dex/` directory is a non-canonical cache (exploration artifacts and the
   reconcile snapshot).
8. Propose, don't impose. Every change is a reviewable diff. Human dbt edits are
   authoritative; on conflict the engine surfaces the divergence and asks.

## Where things live

- DexEngine: `packages/dex-core/` (PyPI: `exmergo-dex-core`, Apache-2.0).
- Connector and methodology notes: `references/`.
- The contract in full: `references/command-contract.md`.
- The source of truth (dbt) and `.dex/` cache: `references/canonical-model.md`.
- Where `.dex/` state lives, selecting a backend, and writing one:
  `references/storage.md`.
- Which format owns the source of truth, selecting one, and writing one:
  `references/project.md`.
