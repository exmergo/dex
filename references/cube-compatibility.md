# Cube compatibility

What Dex accepts from a [Cube](https://github.com/cube-js/cube) model file, what it
checks, what it makes of what it read, how it writes one, and what it does not
claim. Read it beside
[Cube models](semantic-layer.md#cube-models-vendor-cube), which describes how to
configure the layer.

## The pinned rules

Cube publishes no JSON Schema for its data model. The authoritative validator is
the schema compiler's Joi rules (`packages/cubejs-schema-compiler/src/compiler/
CubeValidator.ts`), applied after Cube's YAML compiler camelizes every key. Dex
ports those rules for the fields it reads, pinned to one Cube release.

| | |
|---|---|
| Upstream | <https://github.com/cube-js/cube> |
| Release the rules are ported from | `v1.8.14` |
| Rules | `CubeValidator.ts`, `YamlCompiler.ts` and `CubeSymbols.ts` |
| License | Apache License 2.0 |

The fingerprint records that release beside every hash, so a later port that
reads a file differently is reported as a change of reader rather than as every
cube changed.

## Required extras

| capability | extra | absent |
|---|---|---|
| `config validate`, `config get` on a `semantic.cube` block | none | always available |
| reading the layer: `explore semantic list`, `--use-project`, `maintain` | `[cube]` (the dialect engine, which every connector extra already carries) | refused by name, `reason: prerequisite` |
| authoring: `semantic cube define\|update\|plan`, and a derived cube | `[cube]`, the same extra | refused by name, `reason: prerequisite` |
| querying: `explore semantic query\|values` | `[cube]` and `[semantic-api]` (httpx, to ask a Cube server to render) | refused by name, `reason: prerequisite` |

No Cube package is installed for any of it: no Node, no `cube_dbt`, no
`apache-ossie-cube`. Only querying needs a running Cube server, and it uses that
server as a renderer: see below.

## Plain YAML is read; nothing else is guessed

Cube renders a YAML model file as a Jinja template before it compiles it,
compiles a JavaScript model with Node, and merges `extends` in its compiler. None
of that is statically readable, so each is **compiled-only**:

- a `.js` file;
- a YAML file containing `{{`, `{%` or `{#`;
- a cube or view using `extends`, element by element.

A compiled-only definition is named in the catalog's `notes` and in its
`compiled_only` list (`file`, `reason`, and `element` where only one cube or view
is affected), and nothing is translated out of it. It is never reported as
absent. A join or a reference to a cube the set does not hold is a warning rather
than a refusal while a compiled-only file is configured, because that cube may be
the one the file defines.

## What a model file is checked against

Cube publishes no JSON Schema. Its validator is the schema compiler's Joi rules
(`CubeValidator.ts`), and Dex ports them, pinned to Cube 1.8.14, for every field
it reads:

- **each element.** An identifier `name` on every cube, view, member and join.
  A cube has `sql` or `sql_table`, not both and not neither. Dimension types are
  `string`, `number`, `boolean`, `time`, `geo` and `switch`. Measure types are
  `count`, `count_distinct`, `count_distinct_approx`, `sum`, `avg`, `min`,
  `max`, `number`, `string`, `time` and `boolean` (plus `number_agg` and `rank`
  on a multi-stage measure). Checked shapes: join `relationship`, custom
  granularities, `rolling_window`, and a view's `cubes`. Keys are read in either
  spelling, `sql_table` or `sqlTable`, as Cube camelizes them, and the legacy
  relationships `belongs_to`, `has_many` and `has_one` read as `many_to_one`,
  `one_to_many` and `one_to_one`.
- **the set.** Cubes and views share one namespace, so a name declared twice
  refuses every element declaring it. Further refusals, each of which also
  refuses the element declaring it:
  - a join to a cube the set does not hold;
  - two joins to one cube without an `alias`;
  - a `{reference}` that resolves to nothing;
  - a view `join_path` hop that no declared join supports;
  - a view `includes` member the cube does not declare;
  - a view reaching one cube by two paths from one root.

  A refused cube retracts what depends on it, to a fixed point.
- **unknown keys.** Cube refuses a key its schema does not declare. Reading, Dex
  only warns (`unknown_key`): a key the port does not know may be one a newer
  Cube added, and refusing the cube would hide everything else it declares.

Every refusal names the file and the element (`cubes['orders'].joins['customers']`,
`views['sales'].cubes['orders.customers']`). One broken file leaves the rest
readable, and the catalog raises only when nothing at all could be read.

## References and lineage

A Cube `sql` is an f-string: `{CUBE}` (or `{TABLE}`) is the cube itself,
`{member}` and `{CUBE.member}` a member of it, `{cube}` and `{cube.member}`
another cube, and `{cube1.cube2.member}` a member reached through declared joins.
`\{` and `\}` are literal braces. `{FILTER_PARAMS...}`, `{SECURITY_CONTEXT...}`,
`{SQL_UTILS...}` and `{FILTER_GROUP...}` are per-query values: not errors, but an
expression reading one has no single column behind it.

A member's physical column is traced from its `sql`. `amount`,
`{CUBE}.amount`, and a reference to a member that is itself one column all
resolve. An expression, a quoted identifier, another cube's member or a
per-query value resolves to nothing. A resolved column is folded the way the
active connector folds an unquoted identifier, as the relation is: on Snowflake,
`customer_id` is the column `CUSTOMER_ID`. A cube's relation depends on how it
reads its table:

- **`sql_table`**: read with the active connector's identifier folding.
- **`sql`**: claimed only for `SELECT <columns or *> FROM <one table>` with no
  other clause, and its columns are traced through the projection
  (`customer_key AS id` makes `id`'s column `customer_key`).
- **a filtered single-table query**: keeps its table for the PII gate alone,
  with a note, because its rows are not the table's.
- **a joined query**: no relation at all.

A join's column pairs come from an equality, or a conjunction of equalities,
between a column of each side. A range or expression join is declared with no
pairs, and a note says so.

## How Cube maps onto the catalog

| Cube | catalog |
|---|---|
| cube | a semantic model, `title` as its label, on the relation its `sql_table` (or a plain single-table `sql`) names |
| view | a semantic model with no relation: the governed public surface. Each member it exposes is listed under the view (`sales.order_status`), keeps the cube member it is in `vendor_params.source`, and reaches the PII gate through that cube's column |
| dimension | a dimension keyed `cube.member`. `time` dimensions carry Cube's built-in granularities plus their own custom ones as `queryable_granularities`; every other type is `categorical`, with `[]` |
| aggregating measure (`count`, `count_distinct`, `count_distinct_approx`, `sum`, `avg`, `min`, `max`) | a measure (its type as the aggregation, `avg` as `average`) **and** a `simple` metric `cube.member` reading it. `rolling_window` makes the metric `cumulative`. The measure's `filters` are the metric's `filter` |
| calculated measure (`number`, `string`, `time`, `boolean`) | a metric only, since it aggregates nothing itself. `ratio` where its `sql` is exactly one measure divided by another, with both sides named; `derived` otherwise, with its `{member}` references as `input_metrics` and the aggregating measures beneath them as `input_measures` |
| `primary_key` | a declared key; several on one cube are one composite key in declared order, never several single ones |
| `joins[].relationship` | a declared relationship with every ordered pair, through the declaration channel |
| segments, `pre_aggregations`, `access_policy`, `hierarchies`, `folders`, `default_filters`, `refresh_key`, view groups, multi-stage settings | named in `notes`: no home in the catalog |

A metric is groupable by its own cube's dimensions and by those of every cube its
declared joins reach, in their declared direction, as Cube's join graph is
directed. A measure name is scoped to its cube in Cube (every cube may declare
`count`), so a measure is identified by its name and its `semantic_model`
together. `agg_time_dimension` and a metric's `time_axis` are the cube's time
dimension where it has exactly one; Cube declares no default, so with several,
none is named and a note says why. `public: false` and a cube on another
`data_source` are listed, with a note.

What has no home is declared in `unavailable`: Cube has **no entities**, so every
entity field is a gap and keys and joins reach `explore` through the declaration
channel instead, and a semantic model has **no `model_ref` or primary entity**.
`dimension_scope` is `declarations`.

**What is never read.** `meta` is free-form, and `mask` and access policies
govern values at query time. None of them reaches the catalog, the declarations
or the fingerprint.

## What Cube's declarations reach in `explore`

- a sole `primary_key` dimension becomes a declared unique key; several become
  one composite grain in declared order;
- a join becomes a declared relationship at confidence 1.0, written child to
  parent: the joining cube is the child, except for `one_to_many`. A
  single-pair join is also a foreign key;
- `explore map --use-project` marks each relation with the cubes on it.

## Authoring Cube model files

`semantic cube define|update|plan <intent> --edits-file <path|->` authors the
files through the same plan/apply spine as every other edit. Each edit is one
whole file of the `semantic_document` kind, keyed exactly as `semantic.cube.files`
names it, and is written byte-for-byte: comments, key spelling (`sqlTable` or
`sql_table`), quoting and line endings land as authored.

**What can be written.** Only the plain YAML files the configuration lists. Each
of these is refused by name, and nothing is stored:

| Path or content | Why |
|---|---|
| a path not in `semantic.cube.files` | the list is the write surface; the refusal names the `dex config set` that would add it |
| a `.js` model | compiled-only: a JavaScript model needs Node to compile |
| `globals.py`, `cube.py`, `cube.js` | deployment configuration, not a model |
| a configured file that is a Jinja template on disk, or holds an `extends` | compiled-only, so dex does not replace it with something it can read |
| authored content carrying Jinja, or a cube or view using `extends` | compiled-only; dex authors only what it can validate |

The compiled-only reasons are the strings the catalog writes under
`compiled_only`, so a refusal and a catalog entry about the same file agree.

**What is checked.** The whole prospective set (every configured file, with the
edits laid over it) is validated by the rules the reader runs, so a plan that
would leave a file the catalog cannot read is never stored. Authoring adds one
rule: a key Cube does not declare at its level is refused in a file the plan
writes, where the reader only warns, because dex would be writing the defect. It
adds one warning too: a cube on the one side of a join (it declares
`one_to_many`, or another cube declares `many_to_one` to it) that sums or counts
and declares no `primary_key` is `fan_out`, because Cube has nothing to
deduplicate those measures on when a query crosses the join.

**What the plan reports.** Each cube and view in an edited file is `defined`,
`updated` or `unchanged`, compared with key spelling and order normalized, so a
whole-file edit that touches one cube does not read as a change to its
neighbours. `define` refuses an existing name and a removal; `update` refuses a
new name; `plan` takes a mix and names any removed definition in a warning.

**What apply does.** `transform apply` re-validates the set against the files as
they are now and writes every edited file or none. A file changed after planning
is a conflict, and the plan has to be remade.

Nothing on this path starts Node, calls a Cube server, or opens a warehouse
connection. Authoring is the same whatever the deployment: dex writes files in
git and never writes to Cube Cloud.

## Declarations into Cube

A model declaration reaches a Cube layer through the derivation hook, in two
ways.

**A declared dbt mart derives a cube.** `transform plan` (and `transform
declare`) turn a declared mart under `transform.marts_paths` into a Cube model
file of its own, `<dir>/<mart>.yml`, where `<dir>` is the one directory the
configured YAML files share (`model/cubes` where they share none):

| Declaration | Cube |
|---|---|
| `grain` | `primary_key: true` on each grain dimension, composite included |
| any role but `measure` | a dimension, `sql: "{CUBE}.<column>"`, typed `time`, `number`, `boolean` or `string` from the SQL or the exploration cache; a `time` role is always `time` |
| `measure` + `aggregation` | a measure named after the column (`sum`, `count`, `count_distinct`, `min`, `max`, `avg`); the column is not also a dimension |
| `foreign_key` + `references` | a `many_to_one` join to the cube on the parent's table, `{CUBE}.<column> = {<parent>}.<parent column>` |
| `description` | the cube's and each member's `description` |
| `population` | `meta.dex.population`, and one sentence closing the description |
| `assumptions` | `meta.dex.assumptions` |
| `unit`, `null_rule`, `additivity` | the member's `meta.dex` |

- `sql_table` is the `database.schema` the cubes on this project's other models
  share, else the relation the compiled manifest names, else the bare table name
  (as a limitation).
- `data_source` is written only where `semantic.cube.data_source` is not
  `default`.
- **Never derived:**
  - a population as a measure filter or a segment, since the mart's SQL already
    enforces it;
  - a metric beyond a declared measure (no implicit `count`);
  - a `median` (Cube has no such type);
  - a `semi_additive` measure (Cube has no measure that sums across entities but
    not across time);
  - a second join to a cube already joined (Cube needs an alias, and dex does
    not invent one).

  Each is named under `not_carried`.
- **Never overwritten or renamed:**
  - a cube already on the mart's table makes the mart `skipped`;
  - a cube or view holding the name makes it `failed`.

Every derived cube is held to authoring's own checks and read back through the
catalog. It is *included*, stored as a plan of its own on the Cube layer beside
the dbt plan (`data.semantic_proposal.plan_id`; apply both), only where its path
is already in `semantic.cube.files`. Otherwise it is *proposed*, with the
`dex config set` that adds it.

**Declarations annotate a cube.** `semantic cube define|update|plan` take
`declarations` beside (or instead of) `edits`. Each names a cube the payload or
the layer defines, and is rendered into it field by field: grain to
`primary_key`, descriptions and the population sentence, a declared join and
measure, the rest in `meta.dex`. Only that cube's entry in its file is rewritten.
Every other cube, view and comment keeps its bytes, and the author's key spelling
is kept. A declaration naming no cube, or naming a view, is refused.

## Querying: Cube renders, Dex executes

Cube has no Python SQL renderer: its planner ships only inside a Cube server.
Every Cube server, Core or Cloud, answers `POST /cubejs-api/v1/sql` with the SQL
a query would run, without running it. Dex uses that the way it uses MetricFlow's
`explain()` and SLayer's dry run: Cube renders, Dex checks the statement and
executes it through its own connector, under the PII gate and the cost guard.
`execution` is `dex` on every result. Dex calls `/v1/meta` and `/v1/sql` and
nothing else: never `/v1/load`, and it never hands Cube a warehouse credential.
The server needs none to render, which `scripts/setup_cube_dev.sh` relies on.

### The order

1. **Connector.** DuckDB, Postgres, BigQuery, Snowflake and Redshift, each held
   to a golden of what Cube Core renders for it. Databricks, ClickHouse and
   Trino are refused by name.
2. **PII gate**, before anything is sent: every grouped member, the time
   dimension a grain buckets, and every member a `--where` filter names,
   resolved to the column behind it through the static catalog and screened on
   its profile, with the name heuristic as the floor.
3. **Grain**, against the time dimension's built-in and custom granularities.
4. **Translation** to Cube's query JSON, and the data-source check: every cube
   the query reaches must sit on `semantic.cube.data_source`.
5. **Stale renderer.** Every member the query names is compared with the
   server's `/v1/meta?extended=true`: present, the same kind, the same
   aggregation or type, the same SQL and filters, the same custom
   granularities, and, for a view member, the same cube member behind it. A
   Cube server outside dev mode does not reload an edited model file, so a
   server still serving yesterday's files is the normal state after an edit,
   and it is refused naming the member.
6. **Render** over `/v1/sql`, and the plan checks: one statement (a
   `compareDateRange` plans several), no pre-aggregation (Cube matches a
   rollup even one it never built, and the SQL then reads its table), no
   external store, no lambda query, the configured data source, and no relation
   that no cube in the repository's files sits on (which is also how a server
   whose `sql_table` differs from the repository's is caught, since `/v1/meta`
   does not carry `sql_table`).
7. **Binding.** Cube returns `[sql, params]`, every parameter a string. Each
   placeholder (`$n` on Postgres and Redshift, `?` elsewhere) becomes a literal
   of the connector's dialect, quoted by that dialect's rules. A placeholder
   style or identifier quote that belongs to another warehouse is refused: run
   the Cube server with `CUBEJS_DB_TYPE` set to Dex's connector.
8. **The governed spine**: SELECT-only, the relation pre-check against the
   connection's inventory, the cost handshake (`needs_confirmation` on a
   metered connector before any spend), Dex's connector in a UTC session (the
   setting Cube's own drivers use, so a timestamp with no zone buckets the way
   Cube means), and the columnar cap.

### The CLI dialect

| | |
|---|---|
| metrics and dimensions | `cube.member` or `view.member`, as `explore semantic list` names them |
| a time grouping | `--group-by orders.ordered_at__month` (any built-in or custom granularity), or `--group-by orders.ordered_at --grain month`. Cube has no `metric_time`, so `--grain` alone is refused |
| `--where` | Cube's JSON filter: `'{"member": "orders.status", "operator": "equals", "values": ["paid"]}'`, a list of them (ANDed), or `{"and": [...]}` / `{"or": [...]}` groups. A filter on a measure is a `HAVING`. A top-level `inDateRange` on the grouped time dimension is sent as its `dateRange`, which is what bounds a rolling window |
| `--order-by` | the query's own metrics and grouped members only, `-` for descending (`--order-by=-orders.revenue`) |
| `--limit` | sent to Cube; without one Dex sends `query.max_rows + 1`, since Cube adds `LIMIT 10000` silently |

A filter Dex cannot read (an unknown operator or key, a value that is not a
string, number or boolean, an empty group) is refused rather than screened by
half. Result columns carry the tokens the caller wrote.

`explore semantic values` is a dimension-only query rooted at the dimension's
own cube (`scoped_to` empty); `--metric` adds that measure and names it in
`scoped_to`. A PII dimension is refused before Cube is asked.

### The server and its secret

`semantic.cube.url` names the API base (`http://localhost:4000/cubejs-api` when
unset). The token is an HS256 JWT Dex mints per request from `CUBEJS_API_SECRET`,
read from the environment only, valid for 60 seconds, and never written to an
envelope or an error. A loopback host is admitted; any other host only by exact
name in `DEX_CUBE_ALLOWED_HOSTS`, over https, because the URL comes from a
committed file and the secret is yours. The token claims `dex_scopes: [meta,
sql]`; Cube enforces API scopes only through the deployment's own
`contextToApiScopes`, so on a default server the claim restricts nothing, and
what binds is that Dex calls nothing else.

### What this release does not do

- `--api` (Cube executes over `/v1/load`) and Cube Cloud are #914.
- One data source per query.
- Segments, `ungrouped`, `compareDateRange` and pre-aggregations are not sent or
  not accepted.
- Two warehouses that share a placeholder style and quoting (Postgres and
  Redshift; DuckDB and Snowflake) cannot be told apart from a statement with no
  parameters, so a server rendering for the wrong one of a pair is caught only
  once the query carries a filter. Set `CUBEJS_DB_TYPE` to match.

## What `maintain` makes of the layer

Every axis runs on a Cube layer, beside a dbt project or with none. No
`maintain` command starts a Cube server or Node: each reads the configured
model files and the metadata Dex captured through its own connection.

- **Snapshot.** `maintain snapshot` fingerprints each cube and view, each measure
  (as `cube.member`), each `primary_key` (a composite one whole) and each join
  with its pairs and arity, under `vendor: cube` and the validator release.
  Content hashes cover each element as written, keys normalized, `meta`
  excluded, so respelling `sqlTable` as `sql_table` is not a change.
- **Compiled-only definitions** (a `.js` model, a Jinja-templated file, an
  `extends`) are recorded by their content under `opaque_definitions`: a whole
  file by its bytes, an `extends` element by its own body, so editing a sibling
  cube in the same file does not mark it changed. A change is
  `compiled_only_changed`, what changed is not read, and a warning that these
  were compared by content alone, never structurally, rides every
  `maintain semantic` and `maintain check` while the layer holds one. A layer
  with one never reads clean.
- **Semantic.** A cube, view or measure added, removed or changed, with
  `definition_changed` on a cube naming the table it sits on; a dimension,
  measure, key or join column gone from its table (`dangling_reference`,
  `broken_relationship`). The scanned half re-counts every non-time dimension
  that is one column (`dimension_cardinality_changed`).
- **Grain.** Every `primary_key`, a composite one whole
  (`declared_grain_not_unique`, `key_lost_uniqueness`), and every join's
  `relationship`: a `many_to_one` join whose target's key is not unique, or a
  `one_to_one` join with either side not unique, is `join_cardinality_exceeded`.
  A `one_to_many` join is checked on its one side. Cube requires a
  `relationship`, so every join that loads has one; a join whose `sql` is not an
  equality of columns has no pairs to measure, and is named in `warnings`.
- **Schema.** Cube compares a model with the warehouse only by compiling it
  inside a Cube server, so Dex holds the declarations to its own metadata:

  | Code | When |
  |---|---|
  | `declared_relation_missing` | a cube's table was in the baseline and is gone. One neither the connection nor the baseline lists (a derived cube still pointing at a dev schema, say) is a note, not a finding |
  | `declared_column_missing` | a dimension, measure (its `sql` or a `filters[].sql`) or segment reads a column the table no longer has. Columns match case-insensitively where exactly one does |
  | `declared_type_conflict` | a `time` dimension on a column that is not temporal; a `number` dimension, or a `sum`, `avg` or `running_total` measure, on text. A warehouse type Dex cannot place never conflicts |
  | `declared_dependency_dropped` | a member, join or view include or exclude that reads something that goes, followed to a fixpoint, through `{member}` references and join paths |

  An expression Dex cannot read (a per-query value such as `FILTER_PARAMS`, a
  reference into another cube's table, a `case`, unparseable SQL) is named in
  `warnings` as unchecked, never passed.
- **Reconcile.** `maintain reconcile` cuts the removals out of the model files,
  every other byte (other cubes, views and comments) as it was, and stores them
  as a plan of their own through the `semantic cube update` route and its
  validation, under `data.semantic_plan_id`. A flow-style list of plain names
  (`includes: [revenue, status]`) is rewritten in place with the survivors in
  their own spelling; anything else that cannot be cut by its lines is advisory.
  A measure whose filter reads a dropped column goes with it rather than
  silently losing the filter. Always advisory: a cube whose table is gone (a
  vanished table is as often a rename as a retirement); a view source whose join
  path walks a removed join, and the join it walks; a `sql` cube whose own query
  selects the dropped column; and every removal in a file that already holds a
  key Cube does not declare, since `semantic cube update` refuses any edit to
  that file.

