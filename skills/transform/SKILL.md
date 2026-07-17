---
name: transform
description: 'Use this to author and change a dbt project: bootstrap a new dbt project in a repo that has none (`transform init`), write or refactor dbt model SQL from staging to marts, add tests and docs in schema.yml, manage dependencies, and define or update the semantic layer (dbt semantic models / MetricFlow: entities, dimensions, measures, metrics). Trigger it for requests like "set up a dbt project in this repo", "build a staging model for this table", "refactor this model", "add tests to this model", "create a mart for X", "define a revenue metric", or "add a dimension to this entity". Every change is a reviewable diff to the dbt project; any warehouse build is dev-target only, gated, and cost-surfaced first. Do not use it to explore or profile a warehouse (use explore) or to detect drift and reconcile a project that has fallen out of sync (use maintain).'
---

# Transform

Author and refactor the dbt project: both the SQL transformations (staging to
marts, tests, docs) and the semantic layer on top (entities, dimensions,
measures, metrics). Both are the same job, writing reviewable diffs to the dbt
project, which is the source of truth. This is the building half of the loop. It
writes only to the repo, as reviewable diffs, and runs against a dev target only.

## How to drive it

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

You author the dbt file content; the engine validates it, computes the diffs,
and stores the proposal as a plan. Hand content over with `--edits-file <path>`
(or `-` to read stdin), a JSON payload:

```json
{"edits": [
  {"path": "models/staging/stg_orders.sql", "kind": "model_sql", "content": "..."},
  {"path": "models/staging/stg_orders.yml", "kind": "schema_yml", "content": "..."}
]}
```

`kind` is `model_sql`, `schema_yml`, `semantic_yml` (optional on
`semantic define|update|plan`, which imply it), or `macro_sql` (a macro file
under the project's macro paths). Model SQL must be a single read-only SELECT
once its jinja is stripped; semantic YAML is validated against MetricFlow's
schemas, cross-reference-checked, and (when dbt is available) parsed by dbt
itself before the plan is accepted; a macro file must hold only macro
definitions and jinja comments.

### Bootstrapping a project

If no dbt project exists in the repo, offer `transform init` before anything
else: `transform plan` needs a project to edit. Ask the user for the project
name and **confirm the connector with them**, then run:

```bash
uv run "${CLAUDE_SKILL_DIR}/scripts/run.py" transform init "<name>" --connector <c>
```

The engine renders the whole skeleton (`dbt_project.yml`, `models/staging/` and
`models/marts/`, a `profiles.yml` with a single `dev` target and no secrets) and
records `connector`, `dbt_project_dir`, and `dbt_target: dev` in
`.dex/config.yml`; do not hand-write these files yourself. Init never assumes a
connector: it errors rather than defaulting, so always pass the user's confirmed
choice (a `connector:` already committed in `.dex/config.yml` also counts).
Every connector is supported: DuckDB, BigQuery, Snowflake, Databricks,
Postgres, and Redshift. DuckDB needs a warehouse path (`--path`, or the
`duckdb.path` config). BigQuery needs a GCP project (usually
`bigquery.project` in `.dex/config.yml`; confirm it with the user) and writes
builds to a dedicated dev dataset (`bigquery.dev_dataset`, default
`dbt_dev`); auth is Application Default Credentials, so if credentials are
missing tell the user to run `gcloud auth application-default login`, never
ask for a key. Snowflake writes builds to a dedicated
`snowflake.dev_database`/`dev_schema` on the pinned warehouse; Databricks
writes builds to a dedicated `databricks.dev_catalog`/`dev_schema` on the
pinned SQL warehouse (if credentials are missing tell the user to run
`databricks auth login`, never ask for a token); Postgres writes builds to a
dedicated `postgres.dev_schema` (default `dbt_dev`), with the password
reaching dbt only through the `PGPASSWORD` environment variable. Redshift
writes builds to a dedicated `redshift.dev_schema` (default `dbt_dev`): with
a `redshift.workgroup` pinned the profile renders IAM auth (temporary
credentials from the AWS chain, nothing persisted), otherwise the password
reaches dbt only through the `REDSHIFT_PASSWORD` environment variable. All
of them discover their connections and refuse with the fix named when none
resolves. Init refuses if any dbt project already exists.

### dbt SQL models

- `transform plan "<intent>" --edits-file <path|->` validates the edits and
  returns them as diffs with a plan id. Nothing is applied yet. Add
  `--scaffold <table>` (repeatable) to generate a staging skeleton
  (`stg_<table>.sql` plus per-model YAML with key tests and PII meta) from the
  `.dex/` cache instead of, or on top of, hand-authored edits.
- `transform apply [plan-id]` writes the plan into the dbt project (the latest
  unapplied plan when no id is given; any plan kind, semantic included). The
  result is still a reviewable git diff for the user. If a human edited a file
  after the plan was made, nothing is written: the divergence comes back as
  diffs with `needs_confirmation`, and you should re-plan against current state
  (or, only when the user says so, re-run with `--confirm`).
- `transform plans` lists stored plans (pending and applied, newest first), so
  you never need to browse `.dex/plans/` by hand.
- `transform build --target dev` runs `dbt build` against a dev target. The
  engine surfaces a cost preflight first and runs only with `--confirm` (plus a
  `--budget` on billed connectors). On BigQuery there is no upfront estimate
  (dbt has no dry-run), so always get an explicit byte budget from the user and
  pass it as `--budget <bytes>`; never invent one. Each statement dbt runs is
  capped server-side by the profile's `maximum_bytes_billed`, and the envelope
  reports billed bytes afterward. Production-looking targets are refused
  outright; `--confirm` cannot override that. dbt runs with its working
  directory pinned to the project dir, so relative paths in `profiles.yml`
  resolve against the project. When the project declares packages
  (`packages.yml`) and `dbt_packages/` is missing, the engine runs `dbt deps`
  automatically before the build.
- `transform deps` installs dbt packages explicitly (also the refresh path when
  `dbt_packages/` exists but is stale). No confirmation needed: deps writes only
  inside the project and never touches the warehouse.

### Shipped macros

- `transform macro` lists the macros dex ships; `transform macro <name>`
  proposes scaffolding one into the project's macro directory as a plan,
  applied with `transform apply` like any other. The user's copy is theirs to
  edit; re-running the command diffs it back against the shipped version (a
  warning says whether it is customized or stale), and applying that plan
  overwrites deliberately.
- `unpivot_json_object` turns a JSON object column with dynamic keys (the
  NoSQL-sourced shape: a Firestore/Mongo/DynamoDB document keyed by a related
  entity's id) into one row per top-level key. Use it instead of hand-rolling
  JSON SQL; it renders a complete SELECT:

  ```sql
  select id, key as related_id, value as attrs
  from (
    {{ unpivot_json_object(relation=ref('stg_entities'),
                           json_column='attributes', passthrough=['id']) }}
  )
  ```

  The contract on every connector: one row per top-level key, `key` a plain
  string, `value` the warehouse's native semi-structured type (BigQuery JSON,
  Snowflake VARIANT, Databricks VARIANT, Postgres jsonb, Redshift SUPER,
  DuckDB JSON), a NULL object yields no rows, and a nested object's own field
  names never surface as top-level keys. For a string-typed source column
  pass the parse expression as `json_column` (`parse_json(payload)` on
  BigQuery, Snowflake, and Databricks; `json_parse(payload)` on Redshift);
  Postgres and DuckDB accept JSON-bearing text directly. Databricks needs
  VARIANT support (DBR 15.3+ or a current SQL warehouse). Two BigQuery quirks
  are absorbed by the macro, so do not "fix" them back in: a JSON path
  argument must be a compile-time literal (the macro reads values with the
  subscript operator, which accepts a computed key), and `JSON_KEYS` recurses
  into nested objects unless depth-limited (the macro pins depth 1). When a
  planned model calls the macro and the project lacks it, the plan warns and
  names the scaffold command; scaffold it rather than inlining a copy.

### Preparing the dev target

Before the cost gate, and for free, `transform build` refuses two things and
names the fix for each. Neither costs anything to check, so both surface on the
unconfirmed call rather than after a budget has been agreed.

**Config that has drifted from the profile.** `transform init` renders
`.dex/config.yml` into the project's `profiles.yml`, and dbt reads only the
profile from then on. If a later config edit never reached it (a retargeted
`dev_database`, a different warehouse), the build refuses and names both values
and both files. Edit one to match the other. The engine never rewrites
`profiles.yml`, which you may legitimately have hand-edited.

**A dev target that does not exist.** On Snowflake, dbt creates schemas but never
databases, so a missing `dev_database` is refused with the `CREATE DATABASE`
statement to run; dex will not create it for you, because its only writes are
reviewable diffs inside the repo. On Postgres and Redshift, dbt creates the dev
schema but only if the profile's user may, so the missing privilege is what gets
refused, with the `CREATE SCHEMA`/`GRANT` statement to run. On DuckDB the dev target is a database file,
and dbt would happily create an empty one, then fail every `source()` relation
with a confusing catalog error. The convention there: copy the shared source
warehouse to the dev target path (for example
`cp shared/f1.duckdb <project>/dev.duckdb`), or point the dev target at an
existing file. Projects without sources just get a warning and an empty
database, which is fine for model-only builds.

### The semantic layer

- `semantic define ... --edits-file <path|->` and `semantic update ...` author
  and evolve the dbt semantic models (entities, dimensions, measures, metrics)
  as plans. `define` refuses names that already exist (use `update`); `update`
  refuses names that do not (use `define`). For one logical change that mixes
  both (evolve existing metrics and add the helpers they depend on), use
  `semantic plan ...`: it accepts mixed intent and classifies each name, and the
  envelope reports the split as `defined` and `updated`.
- Plan-time validation is layered so a plan that validates will build:
  MetricFlow's schemas check the shape; the engine resolves every metric input
  (ratio and derived metrics reference **metrics**, not measures; a measure only
  becomes a metric via `create_metric: true`, and the error names that fix); and
  finally the emitted YAML is run through **dbt's own parser** against a
  throwaway copy of the project. A plan that fails parse is refused, not stored.
  If dbt is not installed the parse degrades to a warning; `--no-parse` skips it
  explicitly.
- A semantic plan is applied like any other: `transform apply [plan-id]` writes
  its YAML into the dbt project (no id applies the latest unapplied plan).
- dbt cannot parse semantic models in a project without a MetricFlow **time
  spine**; the engine warns when one is missing and defers the parse gate until
  one exists. Author it like any other model (a day-grain date model plus YAML
  with a `time_spine:` config) in the same or a separate plan.
- `viz preview` is not yet implemented (it returns `not_implemented`); the Viz
  integration arrives later.

## Guardrails (enforced in the engine, not here)

- Writes confined to the repo, and within it to the dbt project's model paths.
  dex never writes to source warehouse data.
- Dev-target only. Prod-target execution is never initiated by dex.
- Cost surfaced before any spend. A build that would spend requires explicit
  confirmation and a session budget.
- Propose, don't impose. Human edits to dbt (SQL and semantic YAML) are
  authoritative; on conflict the engine surfaces a diff and asks rather than
  overwriting.
- PII flags propagate from the cache into emitted dbt (model and column `meta`),
  never example values. Stamping is presence-based at any confidence; only a
  column cleared by a human `pii_overrides` entry in `.dex/config.yml` is
  scaffolded without the meta.
