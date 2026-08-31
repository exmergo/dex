---
name: transform
description: 'Use this to author and change a dbt project: bootstrap a project in a repo that has none (`transform init`), write or refactor model SQL from staging to marts, add tests and docs in schema.yml, manage dependencies, and define or update the semantic layer (dbt semantic models / MetricFlow: entities, dimensions, measures, metrics). Reach for this rather than editing model files by hand whenever the change spans more than one file or has to stay consistent with the rest of the project: it validates the edit against the real schema before writing, returns the change as a reviewable diff with a plan id, and catches the class of error that only surfaces at `dbt run`, such as wrong column names, broken refs, or a materialization that fights the project config. On a large project that check is worth more than the round trip costs. It applies to bug-fix tickets too: "this model returns wrong numbers, fix it" is a transform task. Trigger it for requests like "set up a dbt project in this repo", "build a staging model for this table", "refactor this model", "add tests to this model", "create a mart for X", "define a revenue metric", or "add a dimension to this entity". Any warehouse build is dev-target only, gated, and cost-surfaced first. If you do not yet know the source tables'' columns or grain, use explore first, then come back. To reconcile a project that has drifted out of sync with the warehouse, use maintain.'
---

# Transform

Author and refactor the dbt project: both the SQL transformations (staging to
marts, tests, docs) and the semantic layer on top (entities, dimensions,
measures, metrics). Both are the same job, writing reviewable diffs to the dbt
project, which is the source of truth. This is the building half of the loop. It
writes only to the repo, as reviewable diffs, and runs against a dev target only.

## How to drive it

```bash
uv run --no-project --script "${CLAUDE_SKILL_DIR}/scripts/run.py" <subcommand> [flags]
```

dex runs its engine through `uv`, which is a prerequisite and is not installed by
Claude Code. If the shell reports `uv: command not found`, stop and tell the user
to install it (`curl -LsSf https://astral.sh/uv/install.sh | sh`, or
`brew install uv`, or `pipx install uv`), then re-run. Never fall back to editing
the dbt project by hand instead: the validation, the diffs, and the dev-target
gating live in the engine, so any other path is unguarded.

The first command in a fresh environment installs the engine, so it can take tens
of seconds where later ones take well under a second. `--warm` pays that install up
front and exits without running anything:

```bash
uv run --no-project --script "${CLAUDE_SKILL_DIR}/scripts/run.py" --warm
```

Offer it once at setup. It is not something to run before an ordinary command.

You author the dbt file content; the engine validates it, computes the diffs,
and stores the proposal as a plan. Hand content over with `--edits-file <path>`
(or `-` to read stdin), a JSON payload:

```json
{"edits": [
  {"path": "models/staging/stg_orders.sql", "kind": "model_sql", "content": "..."},
  {"path": "models/staging/stg_orders.yml", "kind": "schema_yml", "content": "..."},
  {"path": "snapshots/snap_orders.sql", "kind": "snapshot_sql", "content": "..."},
  {"path": "seeds/country_vat.csv", "kind": "seed_csv", "content": "..."},
  {"path": "tests/assert_totals_reconcile.sql", "kind": "test_sql", "content": "..."},
  {"path": "analyses/email_skew.sql", "kind": "analysis_sql", "content": "..."},
  {"path": "models/marts/dim_orders.sql", "kind": "model_sql", "op": "delete"}
]}
```

`kind` is `model_sql`, `schema_yml`, `semantic_yml` (optional on
`semantic define|update|plan`, which imply it), `macro_sql` (a macro file under
the project's macro paths), `snapshot_sql` (a snapshot under the snapshot
paths), `seed_csv` (a seed's CSV under the seed paths), `test_sql` (a singular
test or a generic test definition under the test paths), `analysis_sql` (SQL dbt
compiles but never runs, under the analysis paths), `packages_yml`,
`project_yml` (the project-root `dbt_project.yml`), or `profiles_yml` (the
project-root `profiles.yml`). Model SQL must be a single read-only SELECT once
its jinja is stripped; semantic YAML is validated against MetricFlow's schemas,
cross-reference-checked, and (when dbt is available) parsed by dbt itself before
the plan is accepted; a macro file must hold only macro definitions and jinja
comments. A snapshot must hold exactly one `{% snapshot %}` block whose
`config()` names a `unique_key` and a `strategy` of `timestamp` (with
`updated_at`) or `check` (with `check_cols`), and whose body is a single
read-only SELECT. A seed must parse as CSV with a named, duplicate-free header
and one field per column on every row, and stays under 5,000 data rows and 1 MiB
(past that it is data rather than a lookup: load it into the warehouse and
`source()` it). A `test_sql` file is read to decide which of the two shapes
sharing the test paths it is: one holding `{% test %}` blocks is a generic test
definition and must hold only those and jinja comments, balanced; anything else
is a singular test and must be a single read-only SELECT. A singular test that
names no `ref()` or `source()` is warned about, not refused, because it runs
against nothing and passes unconditionally. An analysis must be a single
read-only SELECT too, even though dbt only compiles it. `project_yml` must keep
a `name`; `profiles_yml` must reference
every secret via `{{ env_var('NAME') }}` (a literal credential is refused so
none reaches the diff). Config kinds, snapshots and seeds are all parsed by dbt
at plan time.

Each kind is confined to its own family of paths, and filing one in the wrong
family is refused naming both fixes. `schema_yml` is the exception, accepted
beside a model, a snapshot, a seed, a test or an analysis, because that is where
dbt expects a snapshot's tests, a seed's column types, a singular test's severity
and an analysis's description declared.

**Three things here are called a test, and they are not interchangeable.**
Generic tests are declared inside a `schema.yml` (`data_tests:` on a model or a
column). Unit tests come from `transform test --scaffold <model>`, which writes a
`unit_tests:` block, also `schema_yml`. Singular tests and generic test
*definitions* are files under `test-paths`, and `test_sql` is the kind for those.

**A seed puts values, not logic, into a diff, and a diff goes into git and stays
there.** So a seed whose header names a column that looks like personal data is
refused, and the refusal names the `pii_overrides` entry in `.dex/config.yml`
that a human can add to clear it. Detection reads names and types and never
values (everywhere in dex), so it cannot see personal data hiding under a
neutral column name: do not build a seed out of warehouse rows you have not
looked at.

`dbt build` runs seeds, snapshots and singular tests natively, so `transform
build` after an apply is all it takes; there is no separate seed or test step. A
snapshot writes a table and a test runs a scanning SELECT, so both are priced in
the cost handshake; a seed scans nothing and an analysis is never built at all,
so neither is. A singular test and an analysis build no relation and nothing can
`ref()` either, so neither is a node: neither enters `maintain`'s drift baseline,
and deleting one raises no dangling-reference guard.

`op` is `upsert` (the default: create or update, carrying `content`) or
`delete` (remove the file, no `content`). A delete is a first-class reviewable
diff like any other edit, so a reclassification or refactor is one plan rather
than a plan plus a manual `rm`. Deletes are guarded: the plan is refused if any
file that survives it still `ref()`s a deleted model, naming the offenders.
Carry the edits that remove those references in the same plan (for a rename,
`delete` the old model, `create` the new one, and `update` every referrer to
point at it, all together) so the post-change project is validated as one unit.
An unconfirmed delete against a file a human edited after planning surfaces as
`needs_confirmation`, never a silent removal.

For a rename or a removal, reach for `transform rename` / `transform remove`
instead of assembling the edits yourself. They generate the whole change from the
reference graph and refuse when they cannot promise it is complete, which is the
guarantee hand-assembly cannot give you.

### Bootstrapping a project

If no dbt project exists in the repo, offer `transform init` before anything
else: `transform plan` needs a project to edit. Ask the user for the project
name and **confirm the connector with them**, then run:

```bash
uv run --no-project --script "${CLAUDE_SKILL_DIR}/scripts/run.py" transform init "<name>" --connector <c>
```

The engine renders the whole skeleton (`dbt_project.yml`, `models/staging/` and
`models/marts/`, a `profiles.yml` with a single `dev` target and no secrets) and
records `connector`, `dbt_project_dir`, and `dbt_target: dev` in
`.dex/config.yml`; do not hand-write these files yourself. Init never assumes a
connector: it errors rather than defaulting, so always pass the user's confirmed
choice (a `connector:` already committed in `.dex/config.yml` also counts).
Every connector is supported: DuckDB, BigQuery, Snowflake, Databricks,
Postgres, Redshift, and ClickHouse. DuckDB needs a warehouse path (`--path`, or the
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
reaches dbt only through the `REDSHIFT_PASSWORD` environment variable.
ClickHouse writes builds to a dedicated `clickhouse.dev_database` (default
`dbt_dev`), rendered as the profile's `schema:` because dbt-clickhouse has no
`database:` key, with the password reaching dbt only through the
`CLICKHOUSE_PASSWORD` environment variable; the rendered profile also carries
a `custom_settings` block whose `env_var` references are how `transform build`
turns the confirmed budget into a per-statement server-side cap, so do not
strip them from a profile you edit. All of them discover their connections and refuse with the fix named when none
resolves. Init refuses if any dbt project already exists.

When the user wants staging/intermediate/marts isolated in their own
datasets/schemas (a common ask when the warehouse is shared with unrelated
work), offer `--layered-schemas`: init then also scaffolds
`models/intermediate/`, a `generate_schema_name` override, and per-folder
`+schema:` config, so builds land in `staging_dev` / `intermediate_dev` /
`marts_dev` instead of one shared dev namespace. Do not hand-write that macro;
existing projects can adopt it later via `transform macro
generate_schema_name`. Note dbt warns about "unused configuration paths" until
the first model lands in each layer folder; that resolves itself.

Init also checks (free, metadata-only) whether each namespace the project
would build into already exists with content, and warns naming the namespace
and a few object names. The warning is advisory: relay it to the user and ask
whether the content is theirs (a previous dev build) or unrelated; when it is
unrelated, discard the freshly scaffolded project (nothing has been built),
point the config at a different dev namespace, and re-run init. A "could not
check" note just means no connection was reachable at init time.

### dbt SQL models

- `transform plan "<intent>" --edits-file <path|->` validates the edits and
  returns them as diffs with a plan id. Nothing is applied yet. Add
  `--scaffold <table>` (repeatable) to generate a staging skeleton
  (`stg_<table>.sql` plus per-model YAML with key tests and PII meta) from the
  `.dex/` cache instead of, or on top of, hand-authored edits.
- When you edit a model that already exists, the plan reports what your change
  does to its **row population** under `data.row_attribution`: every predicate,
  join, source and grain change is named, and each is measured on its own against
  the prior model. Read it before applying. A change you were not asked to make
  carrying a non-zero `delta` is the signal to look at: the model still compiles
  and the columns are still right, and it is now returning a different set of
  rows. It is advisory, never a refusal, because changing the filter is sometimes
  the job. On DuckDB the deltas are measured automatically; on a billed connector
  the changes are named for free and measuring them needs `--attribute-rows`
  (then the usual `--confirm --budget` once priced), so ask the user before
  spending. A change reported with `attributed: false` names why it could not be
  measured; treat that as unknown, not as zero.
- `transform apply [plan-id]` writes the plan into the dbt project (the latest
  unapplied plan when no id is given; any plan kind, semantic included). The
  result is still a reviewable git diff for the user. If a human edited a file
  after the plan was made, nothing is written: the divergence comes back as
  diffs with `needs_confirmation`, and you should re-plan against current state
  (or, only when the user says so, re-run with `--confirm`).
- `transform plans` lists stored plans (pending and applied, newest first), so
  you never need to browse `.dex/plans/` by hand.
- `transform references <name> [more...]` answers "where is this used" before you
  change it. **Reach for this whenever a change has to land in more than one
  place**: removing a project variable, renaming a column, deleting a model,
  changing what a macro returns. Editing the files you happen to have open and
  hoping that was all of them is the failure this prevents, and it is a quiet
  one, because the project still compiles with one use left behind.

  It is repo-only and free on every connector, so there is never a cost reason
  not to run it. The positional is variadic, so one call covers a whole rename.
  `--kind` narrows to `model`, `source`, `seed`, `snapshot`, `macro`, `var`,
  `column`, `metric`, `entity`, `dimension` or `measure`; leave it off when you
  are not sure what the project calls the thing, and the answer will tell you.

  Read `data.completeness` before you act on the list. When it says `incomplete`,
  `data.limits` says why and `data.indeterminate` lists the call sites dex could
  not resolve, each with a file and a line. Those are references that *may* name
  what you asked about, so open them and decide yourself; do not treat the list
  of resolved hits as exhaustive when the verdict says it is not. A bare column
  name is matched across the project (`scope: name_matched`), so qualify it as
  `model.column` when you want the lineage separated from same-named columns
  elsewhere.

  Once you know where a name is used, `transform rename` and `transform remove`
  below make the change; you do not have to carry the list into hand edits.
- `transform rename <kind> <old> <new>` generates **every** edit the rename needs
  and stores them as one plan: the definition, every model that selects the name,
  every `schema.yml` that documents or tests it, every semantic reference, and a
  seed header. Kinds are `column`, `var`, `model`, `seed`, `snapshot`, `macro`,
  `source`. Repo-only and free, like `references`.

  **Use this instead of editing the files yourself.** Retyping a rename across
  nine files and missing the tenth is the failure mode this exists for, and it is
  a quiet one: the project still compiles.

  Name a column as `model.column`. A bare name is refused, and the refusal lists
  the models that define a column of that name so you can pick. That asymmetry
  with `references` is deliberate: a report you read can afford to be imprecise
  and a rewrite cannot, because renaming a bare `id` project-wide would rewrite
  every unrelated `id` there is.

  **It refuses rather than half-applying**, and each refusal names what to fix:
  a reference dex could not resolve statically, a name an installed package also
  defines, a column handed to a macro as a literal string (dex cannot tell a
  column argument from a display label), a SELECT list it cannot read. Fix what
  it names and re-run. There is no override flag, because a completeness
  guarantee you can switch off is a suggestion. A bare `select *` is *not* a
  refusal: it carries the column through under the new name with no edit, and the
  plan's `notes` says so.

  Read `data.sites` against the `transform references` output you ran first. It
  counts occurrences per reference form in the same vocabulary, so the two
  agreeing is your evidence that nothing was dropped between reading and writing.
- `transform remove <kind> <name>` removes the **definition** and verifies every
  read is gone, refusing while any survives and naming each with a file and line.

  It never rewrites a read, and that boundary is the point rather than a gap.
  `{% if var('using_department') %}` can be deleted or unguarded, and
  `{{ var('x') }}` sitting in an expression has no value dex may invent. You are
  the one who knows. Author those edits yourself and pass them with
  `--edits-file` in the same call: they are validated and stored in the same
  plan, so the removal is still atomic.
- `transform place <column> --targets <a,b> --expr "<sql>"` answers where a
  derived column that several models need should be *defined*. It walks `ref()`
  upward from every target, takes the lowest model they all descend from that
  already projects the inputs your expression reads, defines the column there,
  and threads it down every chain. The inputs come from parsing `--expr`, so
  there is no separate list to get out of sync with it.

  **Read `data.reasoning` before you apply.** It names the ancestor, why it is
  the lowest, which targets descend from it, and the chain. You are supposed to
  be able to disagree with it; `--explain` gives you the same answer with no plan
  stored, which is the cheap way to ask.

  When `data.strategy` is `per_target` the shared definition was not available
  and the reasoning says why: no common ancestor, or the lowest one is missing an
  input, or two candidates tie. dex will not go further upstream to pull an input
  down, because that turns one placement into an unbounded rewrite of everything
  above it. The fallback duplicates the derivation in each target and those
  copies will drift, so relay the reason to the user rather than applying it on
  their behalf. Often the named fix (add the missing column to the ancestor
  first) is what they actually want.
- `transform build --target dev` runs `dbt build` against a dev target. The
  engine surfaces a cost preflight first and runs only with `--confirm` (plus a
  `--budget` on billed connectors). dbt itself has no dry-run, but the engine
  compiles the project and dry-runs each node itself, so on BigQuery the first
  unconfirmed call already returns `needs_confirmation` with `estimated_bytes`
  and a `per_table_bytes` breakdown, the same shape the scanning `explore`
  commands use. Never invent a `--budget` figure: read the reported estimate
  (`per_table_bytes` is the actionable half, since it names which node is
  driving the cost) and confirm with a `--budget` grounded in that number.
  Each statement dbt runs is capped server-side by the profile's
  `maximum_bytes_billed`, and the envelope reports billed bytes afterward.
  Production-looking targets are refused
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
  DuckDB JSON, ClickHouse raw JSON text in a String), a NULL object yields no
  rows, and a nested object's own field
  names never surface as top-level keys. For a string-typed source column
  pass the parse expression as `json_column` (`parse_json(payload)` on
  BigQuery, Snowflake, and Databricks; `json_parse(payload)` on Redshift);
  Postgres, DuckDB, and ClickHouse accept JSON-bearing text directly. Databricks needs
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
reviewable diffs inside the repo. On Postgres, Redshift, and ClickHouse, dbt creates the dev
namespace but only if the profile's user may, so the missing privilege is what
gets refused, with the `CREATE SCHEMA`/`GRANT` statement to run. On ClickHouse
that check can also come back with no verdict, because a server may not let dex
read another user's grants; it then warns instead of guessing, and the build
proceeds with dbt's own error as the backstop. On DuckDB the dev target is a database file,
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

- Writes confined to the repo, and within it to the dbt project's authored path
  families (models, macros, snapshots, seeds, tests, analyses) plus the
  project-root manifests dbt keeps there. dex never writes to source warehouse
  data.
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
