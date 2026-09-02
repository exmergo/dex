# The dex-core command contract

This is the integration keystone. It is the boundary between the agent
and the engine, and it is what keeps every agent surface thin: each surface calls
the same subcommands and reads the same envelope, so no surface re-implements
logic.

## Shape of the boundary

- A surface (SKILL.md or AGENTS.md) tells the agent which subcommand to run.
- A thin PEP 723 wrapper (`skills/<skill>/scripts/run.py`) runs it via
  `uv run --no-project` against the pinned engine version, installing the connector
  extra it resolves at runtime (an explicit `--connector`, then the `connector:` in the `.dex/config.yml`
  found by walking up from the run directory to the git root, then DuckDB), so the
  pin stays connector-neutral. `uv` is therefore a prerequisite, and the wrapper
  holds the envelope contract even there: with no `uv` on `PATH` it refuses with a
  `reason: prerequisite` error envelope naming the install command, rather than
  failing the exec. It is the one refusal built by hand, because the engine that
  would otherwise build it is what is missing. Two commands need more than a
  warehouse client, and both are resolved the same way, from the command being run
  rather than installed always: `explore cluster` adds `[cluster]`, and
  `explore semantic` adds `[semantic-api]` plus, where a statement might be
  rendered locally (any mode but `list`, without `--api`), `[semantic]`. A repo
  that runs neither resolves neither scikit-learn nor MetricFlow.
- That environment is uv's own, and `--no-project` is what keeps it so. Without it
  uv discovers whatever Python project the caller is standing in, builds it, leaves
  a `.venv/` and a `uv.lock` in their repo, and puts their dependencies on the
  engine's import path: unreviewed writes into a tree dex was asked only to read,
  and an engine no longer running against the closure it pinned. The invariant is
  asserted structurally on the commands the wrapper builds, in the safety spine.
- `--warm` is the wrapper's own flag and the only one it answers itself, stripped
  before the argv reaches the engine. It resolves extras through the same path a
  real run does, installs them, prints one envelope naming what it installed, and
  exits without running a command. That is what lets a container build, a CI setup
  step, or a first-time install pay a cold resolution once instead of leaving it on
  an interactive caller's clock, and resolving through the shared path is what
  keeps it from warming an environment the next command would contradict.
- The engine prints **exactly one** sanitized JSON envelope to stdout and nothing
  else. Diagnostics go to stderr.
- Every envelope carries a constant `connection` block. On success it names the
  resolved connector, its non-secret target coordinates, and the source of that
  resolution (`flag`, `.dex/config.yml`, `environment variable`,
  `dbt profiles.yml`, or `directory-local inference`). Commands that resolve no
  warehouse keep the same shape with null/empty fields. The block is assembled
  from facts already resolved by the command and never opens a connection or
  spends merely to describe one.
- `DBT_PROFILES_DIR` locates `profiles.yml` for dbt operations and the
  last-resort credential fallback. It does **not** select dex's connector or
  override `--connector`/`--path` or `.dex/config.yml`; help and no-connector
  refusals say so explicitly.
- The agent reads the envelope and decides the next step.

State persists in the dbt project (the source of truth) and the `.dex/` cache, so
subcommands are **stateless**: the agent orchestrates multi-step flows by
re-reading them between calls. Credentials never cross this boundary, and nothing
reaches agent context except through the sanitized envelope: values cross only
from profiled, PII-cleared columns, bounded and capped by the query firewall.

## The command surface

Capabilities, not final spelling. Implemented incrementally: `demo`, `connect test`,
the `explore` group, the authoring surface (`transform`, `semantic`), and the
`maintain` group are live; `viz preview` returns a valid `not_implemented`
envelope until the Viz integration lands.

```
dex demo [path]                   -> generate a seeded local DuckDB warehouse (7 tables,
                                     29,512 rows) plus a .dex/config.yml beside it, so a
                                     first run needs no warehouse and no credentials;
                                     reports both under data.created and names what to
                                     run next under data.next_steps. Create-only and not
                                     confirmable: an existing target refuses, no
                                     directory is ever created, and an existing config
                                     at or above the target is left alone with a warning
dex connect test                  -> {capabilities, dialect, read_only: true}
dex explore inventory [--rank]    -> ranked object summary (counts, sizes; no rows). --rank caps at 30
  [--limit N] [--all]                objects by default (kept by rank); --limit widens it, --all lifts
                                     it; both no-ops without --rank
dex explore profile <objects>     -> column profiles + PII flags + candidate keys, grain, data-quality warnings.
  [--columns all]                    Verdict fields (grain, keys, data quality, row count) lead the
                                     payload, columns trails; by default columns is summarized to the
                                     ones carrying a finding, with the rest in elided_column_count.
                                     --columns all restores every column
dex explore relationships         -> inferred + declared joins with confidences + inference notes
                                     (declared covers both a relationships test and a join the
                                     semantic layer declares; `semantic_join_count` splits them)
dex explore map [--detail]        -> write/update the .dex cache, and return the map:
                                     per top-ranked object its grain, key, notable
                                     columns, PII flags and data-quality findings,
                                     plus the join edges, alongside the counts. Every
                                     cap binds in every mode and every elision is
                                     counted in notes; --detail widens the selection,
                                     never the caps. No column value ever appears
dex explore diagram               -> the cached map as a Mermaid erDiagram, in `data.mermaid`;
                                     free and connectionless (a store read, no warehouse);
                                     declared joins solid (a relationships test or a shared
                                     semantic-layer entity, the latter naming the entity in
                                     its label), inferred dotted, and a cardinality
                                     drawn only where the cache proved it; draws profiled,
                                     joined objects with their grain/key/join/PII columns
                                     (--full for every eligible object and column); every
                                     elision is counted in notes; dex writes no file
dex explore query "<SELECT ...>"  -> firewall-approved SELECTs, capped and row-major
  [more...] | --sql-file <path>      (variadic: one call answers several questions; a file takes
                                     one statement per line or semicolon-separated statements)
dex explore cluster <object>      -> k-means over a bounded sample of numeric non-PII non-key columns
                                     (a key is a unique column, a column that joins out, or one named like one);
                                     returns cluster sizes + centroids (means) + silhouette, no rows
                                     (--features to choose columns, -k to fix the cluster count)
dex explore semantic list         -> the semantic layer as the object graph it is, in one shape from either
  [--metric <m>]                     backend: semantic models, metrics (composition, the measures behind the
  [--for-dimension <d>]              number, the grains it can be queried at, and the time column a time
  [--search <t>] [--full]            grouping resolves to), dimensions, entities with one declaration per
  [--local|--api]                    semantic model, and measures. Each semantic model carries the physical
                                     relation it sits on and each element the column behind it, which is what
                                     connects a metric to the objects `explore map` describes; the hosted API
                                     exposes no relation and declares that gap. Costs no warehouse query on
                                     either backend.
                                     Three ways to narrow it, all free and all composable, and all named in
                                     the payload so a subset is never mistaken for the layer. --metric keeps
                                     those metrics and what they reach; --for-dimension is the reverse lookup,
                                     the metrics groupable by all the named tokens, which is also the cheapest
                                     way to find the metrics that share an axis; --search matches a word
                                     against every element's name and the project's own words about it, and
                                     resolves to the metrics it touches. A search term that matches nothing is
                                     named in a note rather than refusing, unlike an unknown metric name.
                                     Budgeted like `explore map`: 50 semantic models, 60 metrics, 150
                                     dimension rows, 50 entities, 60 measures, 40 groupable tokens per metric.
                                     Every cut is counted in `elided` and named in `notes`, `elided` is present
                                     with its zeros so a complete catalog says so, and --full lifts the caps.
                                     The defaults leave an ordinary layer uncut, so a cap only bites one that
                                     was already too large to read in one payload
dex explore semantic values <d>   -> one dimension's value domain: what a filter on it may be filtered to,
  [--metric <m>] [--local|--api]     capped and columnar like `explore query`. A PII-flagged dimension refuses
                                     the command rather than being screened, because the whole output is
                                     values. `scoped_to` says whether these are the column's own values or the
                                     ones present for a metric, which is the only way a dimension behind a
                                     join can be read at all
dex explore semantic query <m,m>  -> one governed metric query, capped and row-major like `explore query`.
  [--group-by <d>] [--where <f>]     --local renders the SQL with MetricFlow and executes it through dex's own
  [--order-by <c>] [--grain <g>]     connector, PII gate, read-only assertion, relation pre-check and cost
  [--limit N] [--local|--api]        handshake; --api sends it to a hosted dbt Cloud deployment, which executes
                                     server-side where dex's cost guard is structurally unavailable and every
                                     result says so
dex transform init "<name>"       -> bootstrap a dbt project skeleton; requires an explicit
                                     --connector (never defaults); refuses if a project exists;
                                     --layered-schemas routes staging/intermediate/marts to their
                                     own <layer>_<target name> schemas; warns when a target
                                     namespace already holds objects (free metadata check)
dex transform plan "<intent>"     -> proposed dbt edits as diffs (nothing applied yet)
dex transform apply [plan-id]     -> write diffs into the dbt project (a reviewable git diff);
                                     no id means the latest unapplied plan of any kind
dex transform plans               -> list stored plans, pending and applied, newest first
dex transform references <name>   -> where each name is used: model SQL, schema.yml, dbt_project.yml,
  [more...] [--kind K] [--full]      macros, semantic YAML, seed headers, installed packages. Repo-only
                                     and free on every connector; it opens no connection and needs no
                                     extra, so it is routed ahead of the dialect gate the rest of the
                                     authoring surface passes. Jinja-aware, so a var() read inside a
                                     macro counts; a reference it cannot resolve statically is reported
                                     under data.indeterminate rather than dropped. data.completeness is
                                     `complete` only once every reason to doubt it is ruled out, and
                                     data.limits names the rest. Capped, with every elision in notes;
                                     --full lifts the caps
dex transform rename <kind>       -> every edit the rename needs, as one plan: the definition, every
  <old> <new> [--edits-file <f>]     model that selects it, every schema.yml that documents or tests it,
                                     every semantic reference, and a seed header. <kind> is one of
                                     column, var, model, seed, snapshot, macro, source. A column must
                                     be named `model.column`: a bare name is refused, because a report
                                     may answer imprecisely and a rewrite may not. Scoped to the
                                     defining node and its ref() descendants. Repo-only and free.
                                     REFUSES rather than partially applying: on a reference dex could
                                     not resolve statically, on a name an installed package also
                                     defines (renaming this project's copy un-shadows the package's),
                                     on a column handed to a macro as a literal string, and on a model
                                     whose SELECT list it cannot read. A bare `select *` is not a
                                     refusal: it provably carries the column through, needs no edit,
                                     and the plan says so. --edits-file carries related hand-authored
                                     edits into the same atomic plan
dex transform remove <kind> <name>   -> the definition removed, and every read verified gone. Same kinds
  [--edits-file <f>]                 as rename, same refusals. dex authors the removal of the
                                     definition and REFUSES while any read survives, naming each with
                                     a file and a line; it never rewrites a read, because
                                     `{% if var('flag') %}` can be dropped or unguarded and only the
                                     caller knows which. Pass those read edits with --edits-file and
                                     they are validated and stored in this same plan
dex transform place <column>      -> where a derived column shared by several models belongs: the
  --targets <m,m> --expr "<sql>"     lowest model in the ref() graph that every target descends from
  [--explain]                        and that already projects the inputs the expression reads. The
                                     inputs are parsed out of --expr, so they cannot disagree with it.
                                     Defines the column there and threads it down every chain, with a
                                     schema.yml entry at the ancestor and at each target and none in
                                     between. data.reasoning names the ancestor, why it is the lowest,
                                     which targets descend from it, and the chain, because a proposal
                                     has to be arguable. Where there is no common ancestor, where the
                                     lowest one lacks an input, or where two tie, data.strategy is
                                     `per_target` and the reason is stated rather than the worse thing
                                     being done quietly. --explain returns the reasoning and stores no
                                     plan. Repo-only and free
dex transform build --target dev  -> cost preflight FIRST; runs only with --confirm and a budget;
                                     auto-runs dbt deps when packages are declared but not installed
dex transform deps                -> install/refresh dbt packages (repo-confined; no warehouse spend)
dex transform macro [name]        -> list the shipped dbt macros, or plan scaffolding one into the
                                     project's macro directory (dbt-parse-checked; apply like any plan)
dex transform test --scaffold <m> -> plan a unit_tests: skeleton for model <m>: a given block per
                                     ref()/source() input with only the columns <m> reads, typed from
                                     the exploration cache; expect: is an empty stub that fails until
                                     filled in (dbt-parse-checked; apply like any plan)
dex semantic define|update|plan   -> dbt semantic model edits as diffs (fronted by transform);
                                     validated up to and including dbt's own parser; applied with
                                     transform apply like any other plan
dex maintain snapshot             -> capture/refresh the known-good baseline in .dex/snapshot.json
dex maintain check                -> sweep every drift axis vs the snapshot; ranked report (read-only;
                                     two-phase on billed connectors: free axes now, one estimate for scans)
dex maintain schema [<objects>]   -> structural drift: columns/tables added, dropped, retyped, renamed;
                                     nullability; dangling sources; a model added, removed, or
                                     content-changed since the baseline (metadata, free everywhere)
dex maintain volume [<objects>]   -> freshness drift: row counts that collapsed, emptied, or spiked (free)
dex maintain grain [<objects>]    -> cardinality/identity drift: lost key uniqueness, changed grain, join
                                     fanout, and the grains the project declares re-verified
                                     (aggregates; gated by --confirm --budget on billed connectors)
dex maintain semantic [<objects>] -> definition drift and dangling refs (free) + categorical dimension
                                     cardinality change (a scan; gated on billed connectors)
dex maintain reconcile [<class>]  -> propose the dbt edits that reconcile detected drift, as a stored plan
                                     of diffs tagged mechanical/advisory (applied with transform apply)
dex maintain verify [<selector>]  -> is the project correct right now, no .dex/snapshot.json baseline
                                     required (unlike every subcommand above): failed/skipped build
                                     nodes (naming the failed cause, walking back through transitively
                                     skipped parents) and models with no relation in the warehouse; all
                                     free (compiled manifest + last run_results.json + cheap object
                                     metadata, never a scan). A project that fails to compile is
                                     reported first and suppresses every other check; data.suppressed
                                     names each finding class that did not run and why
dex viz preview                   -> emit the dbt semantic model to the Viz preview (not yet implemented;
                                     the Viz integration arrives later)
```

### How authored content reaches the engine

The engine has no model of its own; the agent authors the dbt file content and
hands it over via `--edits-file <path>` (or `-` for stdin), a JSON payload:

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

`kind` is one of `model_sql`, `schema_yml`, `semantic_yml` (optional on
`semantic define|update`, which imply `semantic_yml`), `packages_yml`,
`macro_sql`, `snapshot_sql`, `seed_csv`, `test_sql`, `analysis_sql`,
`project_yml`, or `profiles_yml`. Each edit also has an `op`:
`upsert` (create or update, the default, carrying `content`) or `delete` (remove
the file, no `content`). A delete is a reviewable diff pinned to the file's hash
like any other edit, and it is guarded: the plan is refused if any surviving file
still `ref()`s a deleted model (the offenders are named, with the line), so the
post-deletion project is proven free of dangling references before the plan is
stored, and, when dbt is available, the same post-deletion tree is confirmed by
dbt's parser. The guard reads the reference index behind `transform references`,
which is why it sees the two-argument `ref('package', 'model')` form, why a seed's
data rows no longer count as source, and why a reference dex could not resolve
statically *warns* rather than refusing: it may or may not name the deleted node,
and no edit the caller could make would settle it.
A rename is one plan: `delete` the old model, `create` the new, `update` the
referrers, validated together. `transform rename` generates exactly that plan, and
the rest of the rename's edits with it, so hand-assembling one is now the fallback
rather than the route. Note the two guards read the same index and answer
differently on purpose: a reference dex cannot resolve *warns* on a delete and
*refuses* on a rename. A dangling dynamic ref left by a delete is unsatisfiable,
so refusing would block a legitimate delete forever; the same reference in a
rename's path is satisfiable, because the caller can resolve it by hand and
re-run. The engine validates each edit
(model SQL must be a single read-only SELECT once jinja is stripped; YAML must
parse; semantic YAML must satisfy MetricFlow's schemas; a `packages_yml` edit
must carry a `packages:` or `dependencies:` list and targets the project-root
`packages.yml` or `dependencies.yml`; a `macro_sql` edit must hold only macro
definitions and jinja comments and must target the project's macro paths, where
no other kind may go; a `snapshot_sql` edit must hold exactly one
`{% snapshot %}` block whose `config()` names a `unique_key` and a `strategy` of
`timestamp` (with `updated_at`) or `check` (with `check_cols`), whose body is a
single read-only SELECT, and must target the project's snapshot paths; a
`seed_csv` edit must parse as CSV with a named, duplicate-free header and one
field per column on every row, must stay under 5,000 data rows and 1 MiB, must
not name columns that look like personal data, and must target the project's
seed paths; a `test_sql` edit must target the project's test paths and must be
either a generic test definition (only `{% test %}` blocks and jinja comments,
balanced) or a singular test that is a single read-only SELECT once its jinja is
stripped; an `analysis_sql` edit must target the project's analysis paths and be
a single read-only SELECT on the same terms; a `project_yml` edit targets the
project-root
`dbt_project.yml` and must keep a `name`; a `profiles_yml` edit targets the
project-root `profiles.yml` and must reference every secret via
`{{ env_var('NAME') }}`, never a literal), pins it to the sha256 of the file it
would change, computes the diffs, and stores the plan under
`.dex/plans/<plan-id>.json`. Nothing touches the dbt project until an apply.
`packages_yml` is the guarded way to declare dbt package dependencies: a
reviewable diff like any other edit, then `transform deps` (or the automatic
deps step in `transform build`) installs them. `macro_sql` is how a macro is
repaired or customized by hand; `transform macro <name>` is the scaffolding path
for the macros dex ships. `snapshot_sql` and `seed_csv` bring slowly-changing
dimension capture and small reference tables into the same flow, each confined to
its own path family (`snapshot-paths` and `seed-paths` in `dbt_project.yml`, read
with dbt's own defaults) and each gated by dbt's parser at plan time. A
`schema_yml` edit is accepted beside either one, which is where dbt expects a
seed's column types and a snapshot's tests and docs to be declared. `dbt build`
runs seeds and snapshots natively, so nothing new has to be run after an apply;
a snapshot writes a table and is priced in the cost handshake, while a seed
loads a local CSV, scans nothing, and is deliberately unpriced.

`test_sql` and `analysis_sql` do the same for the two remaining families
(`test-paths` and `analysis-paths`, again with dbt's defaults). A singular test
is an arbitrary SELECT that must return no rows, which is where most
project-specific assertions live, and generic test definitions share the same
directory, so `test_sql` reads the file to tell them apart: a `{% test %}` block
is checked structurally like a macro, anything else is checked like a model. A
singular test naming no `ref()` or `source()` warns rather than refuses, since it
runs against nothing and passes unconditionally. An analysis is held to the same
read-only SELECT even though dbt only ever compiles it, because read-only against
data describes what dex writes rather than what dbt runs. `schema_yml` is
accepted beside each. Neither kind builds a relation and nothing can `ref()`
either, so neither is a node: neither enters the drift baseline, and deleting one
raises no dangling-reference guard. `dbt build` runs a singular test natively and
prices it like any other test; an analysis is compiled by `dbt compile`, never
built, and costs nothing.

**Three different things here are called a test.** Generic tests are declared
inside a `schema.yml` (`data_tests:` on a model or a column). Unit tests are
scaffolded by `transform test --scaffold <model>` into a `unit_tests:` block,
also `schema_yml`. Singular tests and generic test *definitions* are files under
`test-paths`, and those are what `test_sql` authors.

`seed_csv` is the first kind that puts **values**, not logic, into a reviewable
diff, and a diff goes into git and stays there. So a seed's header is checked
both against the PII detector `explore` profiles warehouse columns with and
against the flags already in the `.dex/` cache. A column at or above the block
threshold is refused, and the refusal names the `pii_overrides` entry that would
clear it (never a value). The standing limit is worth knowing: dex detects PII
from names and types and never from values, everywhere, so a seed column named
`code` full of email addresses passes this gate.

`project_yml` and `profiles_yml` bring the two
project-root config files into the same plan/diff/apply flow; because they carry
project-wide settings and connection targets, they are gated by dbt's own parser
at plan time, and a `profiles_yml` edit is refused if it (or the file it would
replace) inlines a literal credential, so no secret ever reaches the diff.

- `transform init "<name>" --connector <duckdb|snowflake|bigquery|databricks|postgres>`
  bootstraps a dbt project when none exists: `dbt_project.yml`, `models/staging/`
  and `models/marts/`, and a project-local `profiles.yml` with a single `dev`
  target wired to the warehouse dex already knows (`--path`, or `duckdb.path` in
  `.dex/config.yml`). It is strictly additive (any existing dbt project is a
  refusal, so nothing is ever overwritten), and everything created is reported
  as `create` diffs. **Init never defaults the connector**: the DuckDB on-ramp a
  read-only command may take (an explicit `--path`, or a committed config that
  omits `connector:`) is wrong here, because init bakes the connector into the
  generated profile. `--connector` wins, a
  `connector:` already committed in `.dex/config.yml` is accepted (the envelope
  names which source was used), and bare init is an error listing the valid
  connectors. On success init writes `connector`, `dbt_project_dir`, and
  `dbt_target: dev` back to `.dex/config.yml`, so the choice is made once and is
  ambient for every later command. Every connector renders: DuckDB, BigQuery,
  Snowflake, Databricks, Postgres, and Redshift.

  `--layered-schemas` opts into per-layer schema routing: init additionally
  scaffolds `models/intermediate/`, the shipped `generate_schema_name` macro
  override, and a `models:` block with `+schema: staging|intermediate|marts`.
  The override composes `<custom schema>_<target name>` (layer first, target
  last), so a dev build lands in `staging_dev` / `intermediate_dev` /
  `marts_dev`; a model with no custom schema falls back to `target.schema`. On
  BigQuery the layer namespaces are sibling datasets in the profile's project;
  on Snowflake and Databricks they are sibling schemas inside the dev
  database/catalog; on Postgres and Redshift sibling schemas in the database;
  on DuckDB schemas inside the target file. The same macro is available to
  existing projects via `transform macro generate_schema_name`.

  Init also runs a content preflight over every namespace the new project
  would build into (the base dev namespace, plus each layer namespace when
  `--layered-schemas` is on): a free, metadata-only listing on every connector
  (never a query, never a warehouse wake-up). A namespace that already exists
  *with* tables or views produces a warning naming it, the object count, and up
  to five object names, because a later build would write alongside that
  content and replace same-named relations; init is the one point where the
  name is still trivial to change. It is advisory by design: init still
  succeeds, an empty or absent namespace stays silent, and when dex cannot open
  a connection the check degrades to a single note (init is credential-optional
  and stays that way). DuckDB's base namespace is exempt, since the dev target
  is the same file as the source warehouse; only the layer schemas are probed
  there.
- `transform plan` also accepts `--scaffold <table>` (repeatable): a
  deterministic staging skeleton (`stg_<table>.sql` plus per-model YAML with key
  tests and PII flags in column `meta`) generated from the `.dex/` cache.
- `transform plan` reports the **row-population consequence** of an edit to a
  model that already exists, under `data.row_attribution`. Validation proves an
  edit is well formed; this is the only plan-time check that asks whether it
  behaves the same. In scope is everything that can change which rows enter a
  model: `WHERE`, `HAVING` and `QUALIFY` predicates, a join added, removed or
  retyped between inner and left, a swapped driving relation, and `DISTINCT` or
  `GROUP BY` changes. Out of scope, and silent by construction rather than by
  filtering, are column expressions, aliases, casts and ordering, none of which
  can move a row.

  Each change is measured by applying it, alone, to the prior model and counting,
  so a delta belongs to one change rather than to the edit as a whole. That
  matters because a ticket routinely *requires* a row-population change, and a
  net figure cannot tell a requested change from a silent side effect. The
  whole-model net is reported alongside, measured on the authored model rather
  than summed from the parts; when the two disagree the changes interact and
  `interacts` says so, because isolated counterfactuals do not compose.

  **This is a warning and never a refusal**, and the plan is built and stored
  before any of it runs, so nothing here can stop a plan from existing. Naming a
  change is free and opens no connection, so it always happens. Measuring one is
  a `COUNT` aggregate over the relations the model already reads, free on DuckDB
  and spend elsewhere, so counting runs unasked only on DuckDB; on a billed
  connector it needs `--attribute-rows` and then goes through the ordinary
  estimate and `--confirm --budget` handshake, with the priced ask returned
  beside the stored plan so a confirmed re-run measures without re-planning.
  `--no-attribute-rows` turns counting off anywhere.

  Every counting statement is one `SELECT COUNT(*)` over the model, cleared by
  the query firewall like any agent-authored SQL, so the model's parents must be
  profiled and the PII policy applies (a count projects no column, so a filter
  over a flagged column is still attributable and no value crosses the envelope).
  Nothing is materialized and no relation is created. A change dex cannot isolate
  or measure reports `attributed: false` and names why: macro-generated SQL, a
  jinja statement block, a renamed or added CTE that cannot be paired with a
  prior scope, a relation absent from the cache, or a counterfactual that is not
  valid on its own because it depends on something else in the same edit.
  Authoring a model that does not exist yet produces no findings and opens
  nothing.
- `transform apply [plan-id]` re-hashes every file first. A file edited by a
  human after the plan was made is a **conflict**: nothing is written, the
  divergence is returned as diffs with `needs_confirmation`, and only an explicit
  `--confirm` overrides it. An apply is all-or-nothing across the whole plan: one
  conflict refuses the set, so the clean edits beside it do not land either. The
  stored paths are re-checked against the project format's editing surface at the
  same time, and a path outside it is refused outright rather than surfaced for
  confirmation, since nobody can accept a write into a region the format says it
  does not own. With no plan id it
  applies the latest unapplied plan of any kind (semantic plans included; `emit
  dbt` remains the semantic-scoped spelling). `transform plans` lists what is
  stored, pending and applied.
- `transform build` accepts `--target` and `--select`. The target must be `dev`
  (or the `dbt_target` named in `.dex/config.yml`); production-looking targets
  are refused outright, before the cost gate, and `--confirm` cannot override
  the refusal. On a billed connector the cost gate is priced upfront: dex runs a
  free `dbt compile` and dry-runs each compiled node through the connector's own
  estimator (the same one `explore` uses), summing the result into the
  `needs_confirmation` estimate. It is a partial floor when a cold dev target has
  not built a node's inputs yet, and degrades to no estimate (with a note) when
  dex cannot open its own connection; the ceiling and the server-side
  per-statement cap bind regardless. dbt runs with cwd pinned to the project dir
  (relative `profiles.yml` paths resolve there, never against the caller's
  shell). When
  `packages.yml` (or a `dependencies.yml` with packages) is declared and
  `dbt_packages/` is missing, `dbt deps` runs automatically first; `transform
  deps` is the explicit install/refresh. A missing dev DuckDB database is an
  actionable refusal when the project reads from sources (seed it first), and a
  warning otherwise. On failure the envelope's `errors[0]` carries the first
  real dbt message; the rest land in `warnings`, per-entry capped, deduplicated,
  with a pointer to the full log when anything was trimmed.
- `semantic define` refuses names that already exist in the project (use
  `update`); `update` refuses names that do not (use `define`); `semantic plan`
  accepts a mix and classifies per name, reporting `defined` and `updated` in
  the envelope. Names implicitly created by `create_metric: true` measures count
  as existing metrics everywhere. Beyond MetricFlow's schemas, the engine
  resolves every metric input (ratio and derived inputs must reference metrics,
  not measures) and then runs the emitted YAML through dbt's own parser against
  a throwaway copy of the project; a plan that fails parse is never stored.
  When dbt is unavailable (or the project has no time spine yet) the parse
  degrades to a warning; `--no-parse` skips it. A stored semantic plan is applied
  with `transform apply` like any other plan (no id applies the latest unapplied
  one).

Skill-to-subcommand mapping: `explore` fronts `connect`/`explore`; `transform`
fronts `transform`, `semantic`, and `viz`; `maintain` fronts the whole
`maintain` group. Within `maintain`, `snapshot` manages the baseline, `check`
plus `schema`/`volume`/`grain`/`semantic` detect drift (read-only), and
`reconcile` is the only verb that emits diffs (applied through `transform
apply`). Detection is read-only on every connector, but read-only is not free:
`schema`, `volume`, and the reference half of `semantic` read metadata and run
immediately, while `grain` and the dimension-cardinality half of `semantic` scan
the warehouse and take the `--confirm --budget` handshake on billed connectors;
`check` runs the free axes first and returns one combined estimate for the
scanning axes.

**A baseline reports its own coverage, and the axes only compare what it
covers.** `maintain snapshot` pins the exploration cache, and a cache is thin
whenever `explore map` stopped at its rank cutoff: past 50 objects it profiles
the top 25 and enters the rest as metadata alone. An object the baseline holds no
columns for has an *unknown* column set, not an empty one, so the schema axis
compares no columns for it rather than reporting every live column as added, and
the grain axis has no keys to probe. Both `snapshot` and the detectors warn and
name the objects, and `snapshot` reports `column_detail_count` beside
`dataset_count`, so partial coverage is never mistaken for a clean bill. Run
`explore map --full` before snapshotting to cover everything. The detectors also
warn when the baseline was pinned from a cache older than
`profile_freshness_hours`, judged on the capture time recorded in the baseline
rather than on which file was written last, so re-pinning cannot silence it.

**`explore semantic` queries the semantic layer; `transform` and the `semantic`
group author it, and `maintain semantic` detects drift in it.** Two backends answer
the same three subcommands through one abstraction, chosen ambiently by
`semantic.vendor` and `semantic.deployment` in `.dex/config.yml` (the released
`semantic.backend` spelling of the two is still accepted) and overridable per
command with `--local` / `--api`. Those two flags name **who executes**, not which
vendor: every catalog and every result reports it as `execution` (`dex` or
`vendor`), and that is the axis the guards read. A vendor-executed backend owns the
warehouse connection, so dex never holds a statement it could price or cap, and
every hosted result carries a warning saying exactly that.

`list` costs no warehouse query on either backend, and neither does the reverse
lookup, which inverts the dimension list each metric already carries rather than
asking the layer a second question. `values` and `query` each execute one, and both
screen every dimension a request would touch before it is sent: on `query` a
flagged dimension is refused from the grouping or the filter, and on `values` it
refuses the command outright, since there is no aggregate to fall back to when the
whole output is values. Where only the name heuristic could screen a dimension, the
result says so, so weaker screening is never mistaken for evidence. The
field-by-field catalog contract, the two backends' declared asymmetries, and what
`dimension_scope` and `scoped_to` mean are in
[`semantic-layer.md`](semantic-layer.md).

`explore relationships` and `explore map` accept `--verify`, which measures each
inferred join with one aggregate overlap probe (non-null foreign keys, orphan
count) and adjusts its confidence; the result carries `verified` and
`orphan_fraction`.

Exploration starts bare: by default the warehouse is observed as-is, and a dbt
project in the repo earns only a discovery note. `explore profile`,
`explore relationships`, and `explore map` accept `--use-project`, which folds
the project's declared definitions into the result: `relationships` tests
become declared joins at confidence 1.0 (resolved against the connection's
inventory; a declared join that matches nothing or more than one object is
reported in `notes`, never guessed, and a declared edge wins over the same
inferred one), a semantic model's primary entity overrides the heuristic grain
(disagreements and contradicted `unique` tests land in `data_quality`), and
models reachable from metric definitions rank higher alongside the configured
`ranking_hints`. The compiled manifest resolves names exactly when present;
an uncompiled project falls back to name-based resolution and says so. A
stale manifest (older than the model sources) is noted, not trusted silently.

**The project's semantic layer folds in on the same flag**, in both directions,
and neither direction costs a warehouse query.

Every entity two semantic models share is a join the layer states outright, with
the physical key named per model, so those arrive as **declared** joins at
confidence 1.0 beside the `relationships` tests, through the same endpoint
resolution and the same never-guess rule. `declared_by` on the edge names the
entity, which is the part a reader can look up with `explore semantic list` and
the only part the edge does not already carry; a `relationships` test leaves it
unset, because it declares exactly the two columns the edge already names. An edge
both channels declare is counted once. `notes` says how many came from the layer
and, separately, how many of those name-based inference did not find, which is the
case that matters: the layer routinely joins columns that share no name.

In the other direction, each object in `data.objects` carries `semantic_models`,
the models that sit on that relation. Empty is an answer: a relation nothing in the
layer reads is a different object from one several metrics are built on, and row
counts and PII flags cannot tell them apart. Every object in view is rewritten
whenever the layer was read, so a model dropped from the layer clears rather than
leaving a stale claim. A project with no compiled semantic layer contributes
neither direction and is not an error on this path; `explore semantic list` is the
command whose subject is the layer, and it is the one that refuses by name.

**`explore map` returns the map, not a receipt for it.** Alongside the counts,
`data.objects` carries each top-ranked object's row count, detected grain,
candidate key, notable columns (each with the role that earned it a place:
`grain`, `key`, `join`, or a PII flag) and data-quality findings, and `data.edges`
carries the join edges in exactly the shape `explore relationships` returns them.
It is budgeted the way `explore diagram` is budgeted: at most 25 objects kept by
rank, 12 columns per object, 40 edges, and 5 data-quality findings per object.
Every cap binds in every mode, `elided_object_count` / `elided_column_count` /
`elided_edge_count` report what each one cut, and `notes` names the count, the cap
and the way to read the rest, so a truncated answer never reads as a complete one.
`notes` is always present, so an empty list is the positive statement "nothing was
elided".

Which objects are eligible differs from `explore diagram` on purpose, and the two
commands share only the *column* selection. Every profiled object is eligible here,
including one that joins to nothing, because an isolated lookup table carrying PII
flags and an empty-table warning is exactly a finding; the diagram drops those,
since a box with no edge draws nothing. So a map can report more objects than the
diagram of the same cache draws, and the counts in the envelope always reconcile
with the objects printed beside them.

`--detail` widens what is *eligible*: every column rather than the notable ones,
and objects that were inventoried but never profiled. It lifts none of the caps.
It is deliberately not spelled `--full`, which on this command decides how much
gets scanned and therefore what the run costs; `--detail` decides only how much of
what was found comes back, and spends nothing.

**No column value crosses this envelope.** The cache holds `min_value`,
`max_value` and a value domain for the columns that earned them, and this command
does not read them: `explore profile` is where a caller asks for a value domain,
deliberately and one object at a time. PII is category and confidence, as
everywhere.

`explore map` never caps silently: past 50 objects it profiles the top
`profile_top_n` (default 25) by rank and announces the cutoff in `notes`
alongside `skipped_count` (`--full` profiles everything). Objects skipped on a
re-map carry their prior profiles forward (`carried_forward_count`), each
stamped with its own `profiled_at`. A selected object whose cached profile is
still fresh (same connector, schema unchanged, profiled within
`profile_freshness_hours`, default 24; `0` disables reuse) is reused without a
re-scan and reported as `cache_hit_count`, so it never enters the cost preflight
or the billed handshake. `--refresh` forces a full re-profile of every selected
object even when the cache is fresh, for a source that changed in a way the free
metadata check cannot see. `explore relationships` and the standalone
`explore profile <objects>` reuse fresh profiles the same way
(`cache_hit_count`), so a `profile` on a table `map` just wrote is served free;
all three accept `--refresh`.

Global flags (shared resolution path): `--connector`, `--path` (DuckDB),
`--scope`, `--project` and `--dataset` (BigQuery only), `--repo-root`,
`--confirm`, `--budget`, `--session-ceiling` and `--no-session-ceiling`.

`--session-ceiling <value>` and `--no-session-ceiling` are the two answers to the
one-time cumulative-ceiling ask below. Either one writes the answer into
`.dex/config.yml` and reports the amendment as an `update` diff; they are answers
to a question about the project, not per-command overrides, which is why they are
durable and why `--budget` is unaffected by both.

`.dex/config.yml` is found by walking up from the `--repo-root` directory (default
the shell cwd) to the enclosing git root, the way git and dbt locate their project,
so a command run from a subdirectory resolves the project's config rather than the
current directory's. The walk anchors on the config file (a subdirectory holding
only a `.dex/` cache never shadows the real config higher up) and stops at the git
root (a stray `.dex/config.yml` above the repo cannot capture the session). With no
config found anywhere and no explicit `--connector`/`--path`, the engine refuses
and names the fix rather than defaulting to DuckDB. A committed relative
`duckdb.path` resolves against the project root the config lives in, so the same
target opens from any subdirectory; a live `--path` stays relative to the shell cwd.

`--scope` is repeatable and narrows the source allowlist for one command. Each
connector reads it in its own namespace vocabulary: a `dataset` on BigQuery, a
`schema`, `database`, or `database.schema` on Snowflake, a `catalog` or
`catalog.schema` on Databricks, a `schema` on Postgres. It is never written back
to config, so `connect test --scope X` works before a connector block exists.

Two rules make it a cost control rather than a hint:

- **Scope narrows, never widens.** When `.dex/config.yml` commits a source
  allowlist, that allowlist is a cost boundary and every `--scope` entry must
  resolve inside it. A scope that reaches outside is refused.
- **A scope is honored or named in an error, never dropped.** An entry that
  names nothing refuses and lists what exists. `--project` and `--dataset` are
  BigQuery vocabulary and error on any other connector; DuckDB has no namespace
  to scope and refuses all three (its target is `--path`).

## The query firewall

`explore query` executes SQL the agent wrote; the engine generates nothing and
only refuses or bounds. A call may carry several statements, and the gate below
runs over each of them separately: batching buys call count and nothing else.
The gate, in order:

1. **Parse, don't trust.** A single read-only SELECT per statement, structurally
   checked. Writes, DDL, PRAGMA and DESCRIBE are refused (introspection goes
   through `inventory`/`profile`), and so is multi-statement input *inside one
   string*: `"select 1; select 2"` stays refused whether it arrives alone or
   alongside other arguments. Separate arguments are the supported way to ask
   several questions, because they keep the statement boundaries explicit
   instead of asking the parser to find them.
2. **Resolve against the cache, then against the connection.** Every table and
   column must exist in `.dex/cache.json`, because profiling is what makes the
   PII policy computable and the firewall cannot judge a column whose flags it
   does not have. What follows from a cache miss is not a refusal, though: the
   object is looked up in the live inventory, and if the connection has it, it is
   profiled and the query then runs. This covers the case no amount of upfront
   exploration can, a relation built since the last inventory (a model `dbt run`
   just created), which is neither profiled nor inventoried.

   Three states trigger the profile: never profiled, present but inventory-only
   (no column detail), and profiled against a column signature the warehouse has
   since changed. Age does not: a cached profile whose columns still match is
   reused however old it is, so a probe never silently becomes a re-scan.

   The scan is disclosed, never silent. The result warns, naming what was
   profiled, and `data.profiled_on_demand` lists it for a machine reader. The
   profile is a full one, same detection, same `pii_overrides`, same cache write,
   so the flags governing the query are the flags a deliberate `explore profile`
   would have produced. On a metered connector it is priced rather than implied:
   a single handshake covers profiling those objects and running the statements,
   itemized per table alongside a `(the statement itself)` entry (or one
   `(statement N)` entry each when the call carries several), and the confirmed
   call does both. A call is resolved as one set, so two statements over the same
   cold table pay for one scan rather than two. If the guard then refuses a
   statement anyway, the profile is still saved and the refusal says so, so a
   corrected query does not pay for it twice.

   An object the connection does not have is still refused, and only the
   statements that named it. The message names the connection rather than the
   cache, because no amount of profiling puts an absent object into it. A
   statement already refused is never scanned for: the objects worth profiling
   are the ones a statement that can still run will read.
   `--no-auto-profile`, or `auto_profile: false` in
   `.dex/config.yml`, restores the strict prerequisite: the original refusals
   word for word, and no connection opened to produce them.

   `explore cluster` resolves its object the same way, with one difference in
   pricing that its shape forces. Its sample statement is built from the feature
   columns, which come out of the profile, so the profile is priced first and the
   sample passes a mid-command gate afterward: a budget too small for the sample
   returns `needs_confirmation` with the profile already saved, rather than
   refusing and discarding it.
3. **Classify the projection.** Output may carry values only from profiled
   columns whose PII flag is absent or below the blocking threshold. A flag at
   confidence 0.5 or above blocks projection; the threshold is a hard-coded
   engine constant, uniform across categories, deliberately not configurable.
   Every value path from a blocking column must pass through a measuring
   aggregate (COUNT, APPROX_COUNT_DISTINCT, AVG, SUM, STDDEV, ...).
   Value-carrying aggregates (MIN, MAX, ANY_VALUE, STRING_AGG, ...) do not
   qualify, unknown functions fail closed, and `SELECT *` is refused when the
   expansion includes a blocking column. Projecting a column whose flag sits
   below the threshold (de-rated by value-shape evidence at profile time) runs,
   with an envelope warning naming the column, category, and confidence.
   Filters, join conditions, GROUP BY and ORDER BY are unrestricted: values
   flow in, not out. A column a human has reviewed as not PII is cleared by a
   `pii_overrides` entry in `.dex/config.yml` (fully qualified column, optional
   reason), which unblocks querying immediately and suppresses the flag durably
   on every later profile.
4. **Bound the result.** LIMIT is clamped (default 50 rows), long cells are cut
   (default 256 chars), the payload is byte-capped (default 16 KiB), at most 10
   statements ride in one call, and every cut is announced in `notes`. A watchdog
   interrupts queries that outlive their time budget (default 30s). All of them
   are configurable under `query:` in `.dex/config.yml`; `auto_profile` sits at
   the top level instead, because `explore cluster` honors it too. The byte cap
   is the one that follows the call rather than the statement, because it is the
   one protecting agent context: a call answering ten statements must not emit
   ten times what one statement is allowed.
5. **Record.** Every decision, allowed, refused, or failed, is appended to
   `.dex/queries.jsonl` (SQL text and counts, never result values). An allowed
   query that projected sub-threshold flagged columns records those warnings
   under `pii_warnings`, so the audit trail keeps every such projection findable.
   A decision that profiled first records the objects under `profiled_on_demand`
   (and `profile_planned` on a `needs_confirmation`), so a scan is as findable in
   the ledger as a spend. One entry per statement whatever the call carried; a
   call carrying several adds `batch_index` and `batch_size` to each and gives
   them all one timestamp, so an auditor can see that six statements were one
   authorization event rather than six.

Results are row-major (`columns`, `cells` as a list of lists, `row_count`,
`truncated`, `notes`), which is cheaper in tokens than records and keeps the
envelope sanitizer's list-of-dicts raw-row rule intact as a backstop.

A call carrying one statement puts that shape directly in `data`. A call carrying
several puts one entry per statement under `data.results`, each with the same
keys plus `index`, `status` (`ok`, `refused`, `failed`, `skipped`), `error`,
`reason`, and the source `line` when the statement came from `--sql-file`;
`statement_count`, `ok_count` and `failed_count` sit beside them. A statement
that failed makes the envelope's own status `error` while every answer stays in
`data`, the same way `transform build` reports a run that failed partway: an `ok`
envelope carrying a guard refusal would be the weaker claim, and dropping the
answers to report the refusal would discard work already paid for.

The caps follow the call rather than the statement where the difference matters.
`query.max_rows` and `query.max_cell_chars` bound each statement;
`query.max_payload_bytes` is the budget for the whole call, spent in statement
order, so several statements cannot emit several times what one is allowed; and
`query.max_statements` (default 10) bounds how many a call may carry at all.

## The envelope

Every command prints one object of this shape (`exmergo_dex_core.envelope`):

```json
{
  "status": "ok | not_implemented | error | needs_confirmation",
  "data": {},
  "cost": {
    "estimate": null,
    "ceiling": null,
    "paradigm": "bytes_scanned | compute_time | db_load | hosted | free_local | null"
  },
  "warnings": [],
  "diffs": [],
  "errors": []
}
```

Rules the envelope enforces, all of them Tier-2 eval targets:

- **Cost before spend.** `cost` is a preflight estimate. Any command that would
  spend returns `needs_confirmation` unless given `--confirm` (and a `--budget`
  on billed connectors; DuckDB is free, so the confirm handshake alone gates it).
  An estimate over the ceiling is refused outright; confirmation cannot override
  it. On billed connectors the estimate comes from free dry-runs, the confirmed
  run re-checks every statement against the budget with a server-side cap as
  backstop, actual spend is reported under `data.spend`, and every billed byte
  is appended to the `.dex/spend.jsonl` ledger, against which the optional
  `budget.session_ceiling` binds cumulatively per UTC day.
- **`data.spend` reports settled spend for every billed command**, including
  `transform build`, and always matches what the same command appended to the
  ledger. It carries the connector's unit (`bytes_billed` or `seconds_billed`)
  plus `session_spent_today`, which is what the next command's cumulative
  ceiling will start from. A failed build reports it too: dbt bills for the
  statements it ran before it stopped.
- **ClickHouse's paradigm depends on its declared deployment.** Config-free
  callers retain the backward-compatible `db_load` default; an effective
  `clickhouse.deployment: cloud` uses `compute_time`. Cloud keeps seconds as the
  binding ceiling and ledger unit, and adds approximate compute-unit-hours from
  live per-replica memory plus optional USD from the configured price. Missing
  or partial capacity refuses before billed work.
- **The spend ledger is a dependency of billing, not of every command.** A gate
  is built at every connection assembly on a billed connector, free commands
  included, but nothing reads the ledger until something needs the day's total.
  Billed admission reads it and fails closed if it cannot: the refusal is named
  (`reason: guard`), carries the cost, and says nothing ran, so re-issuing the
  same command is safe. Settlement tolerates a read failure instead, so a backend
  that goes away mid-command does not turn a command that already ran into a
  refusal. A command that cannot spend never reaches the ledger, so a store
  keeping it on a network can be unreachable without taking down a cache-served
  answer. Two fields can therefore report `null`: `data.spend.session_spent_today`
  when the ledger failed at settlement (what that command billed is still exact,
  because the warehouse said so; only the day's total is unavailable), and
  `connect test`'s `budget.session_spent_today`, which takes one guarded read
  because reporting the budget is that command's job.
- **The cumulative ceiling binds across commands that overlap in time**, not
  only across commands that follow one another. An admitted command books its
  estimate against the day's headroom before it runs and releases the unspent
  part when it settles, so a second command issued while the first is still
  running is measured against what is genuinely left, and the server-side cap
  each statement carries is bounded by that booking rather than by the whole
  ceiling. Three consequences a caller can see:
  - `cost.ceiling` on a refusal reflects headroom another command is holding, so
    two runs of the same command can be refused against different numbers.
  - `session_spent_today` counts headroom held by commands still in flight, so
    while another billed command is running it reads higher than settled spend
    and can briefly exceed `session_ceiling` without anything having overspent.
    Run commands one at a time and it is exactly settled spend.
  - A command killed outright leaves its estimate booked until the UTC rollover.
    Every softer exit, including an interrupt, releases. This errs conservative
    on purpose: the alternative is a hold that expires while its command is
    still spending.
- **A billed command with no cumulative ceiling warns.** `budget.ceiling` is
  *refused* when missing, because nothing runs unbudgeted;
  `budget.session_ceiling` is only warned about, because refusing would break
  every project that never set one. Without the warning the two are
  indistinguishable from outside, and an unset daily cap reads as one that
  bound. Config is read from `<repo_root>/.dex/config.yml` and does not inherit,
  so a second repo root has its own budget or none, and the warning says so.
- **And a project is asked for one, once.** The warning above is accurate and it
  is also the default state of every new project, so it repeated on every billed
  command, which is the condition under which warnings stop being read: several
  billed commands can run bound by their per-command caps alone, each carrying
  the same sentence, with the aggregate bounded by nothing. So the first billed
  command in a project with no recorded decision returns `needs_confirmation`
  naming a `suggested_session_ceiling` (five times that command's own estimate,
  in the connector's unit, as a starting point) and a `session_ceiling_hint`
  spelling out both answers, under its own key because a two-phase command's
  findings payload owns `hint`. Answer it with `--session-ceiling <value>` to
  set one or `--no-session-ceiling` to record that the project runs unbounded;
  either answer is written to `.dex/config.yml` and nothing asks again in that
  project. The ask is the *last* check before
  spend, so an unanswered one has run nothing, booked no headroom, and reached
  the ledger not at all, and the unconfirmed cost ask that precedes it carries
  the suggestion in `notes` so one re-run can answer both. Three cases are never
  asked: a project that already set `budget.session_ceiling` (nothing changes
  for it), one that recorded a decline, and a config-free ad-hoc read, which has
  no committed file to record an answer in and keeps the warning alone. A
  decline records a decision and loosens nothing: the warning still fires on
  every billed command, and now names the decline so a reader can tell a settled
  choice from a project that was never asked.
- **`cost.paradigm` names the connector the command ran against**, not what the
  command happened to cost. A free metadata command on BigQuery reports
  `bytes_scanned` with a null estimate, so a caller learns what a billed command
  will cost in before running one. `free_local` is DuckDB's own paradigm and a
  positive claim that the connector bills nothing; when no connector was
  resolved the field is `null` instead. A refusal carries the paradigm, the
  estimate, and the ceiling that bound it, so "was this refused over money?" is
  answerable from the structured fields without parsing the error prose.
- **`connection` names the target that answered.** Its connector, safe target
  coordinates, and resolution source are stamped once at the CLI boundary so
  individual command handlers cannot omit or reshape them. The sanitizer scans
  this block as well as `data`; credentials remain forbidden even if a future
  connector accidentally places one among its target coordinates.
- **Diffs, not silent writes.** Proposed changes appear in `diffs`; being there
  does not apply them. The user applies through their normal review and PR flow.
- **No secrets, no uncleared values.** `data` is scanned before printing
  (`envelope.sanitize`): a secret-like key or a record-shaped raw-row payload is
  a hard failure, never a silent scrub. Result values appear only in `explore
  query`'s columnar payload, and only after the query firewall has proven they
  come from profiled, PII-cleared columns. This is a release-blocking safety
  guarantee.

## Why this is the first artifact

Because every skill, test, and benchmark depends on it, the contract was locked
before the engine logic was built, and the subcommands fill in against this
fixed boundary. Exploration, authoring, and maintenance are live today; the Viz
preview is the last stub, pending the Viz integration.
