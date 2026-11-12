# Snowflake Semantic View compatibility

What Dex accepts from a Snowflake
[Semantic View](https://docs.snowflake.com/en/user-guide/views-semantic/overview)
YAML file, what it checks, what it makes of what it read, and what it does not
claim. Read it beside
[Snowflake Semantic Views](semantic-layer.md#snowflake-semantic-views-vendor-snowflake),
which describes how to configure the layer.

## The pinned rules

Snowflake publishes the
[Semantic View YAML specification](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)
and its
[validation rules](https://docs.snowflake.com/en/user-guide/views-semantic/validation-rules)
as prose, and enforces them when `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` runs on
an account. Dex reads the committed YAML without one, so it restates the rules it
relies on as its own port.

The format has no `version` key. The version a fingerprint records is Dex's own:
the revision of the specification and rules this reader was written and
qualified against.

| | |
|---|---|
| Rule set (`format_version`) | `2026-11` |
| Qualified against | `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(..., verify_only => TRUE)` on a live account, over the fixture corpus in `packages/dex-core/tests/semantic_views_vendor/fixtures/` |

The fingerprint records `vendor: snowflake` and `format_version` beside every
hash. A later port that reads a file differently is reported as that change
rather than as every definition changing.

## Required extras

| capability | extra | absent |
|---|---|---|
| `config validate` and `config get` on a `semantic.snowflake` block | none | always available |
| reading the layer: `explore semantic list`, `--use-project`, `maintain` | `[semantic-views]`, the dialect engine (every connector extra already carries it) | refused by name, `reason: prerequisite` |

Reading the layer installs no Snowflake package and opens no Snowflake
connection. The `[snowflake]` extra is the warehouse connector, which only a
governed query (#1290) will need.

## The YAML document is read; the DDL is not

Each configured file is one document in Snowflake's YAML specification: the input
of `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` and the output of
`SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`. A repository file and a deployed view can
therefore be compared like for like.

- **The view is named by its `name`.** A file whose stem disagrees with it warns
  (`file_name`), and two files declaring one view (names compared ignoring case)
  refuse the second (`duplicate_view`).
- **Refused by name, as another format:**
  - a `.sql` file (`CREATE SEMANTIC VIEW ...`), in config;
  - a legacy Cortex Analyst semantic model, recognized by a table declaring
    `measures` (`legacy_semantic_model`). Snowflake accepts such a file and drops
    what a Semantic View does not read; Dex refuses rather than read part of it.

Reading `dbt_semantic_view` models and DDL is a follow-on (#1298).

## YAML as Snowflake reads it, and one difference

The reader shares Dex's strict YAML loader with the Metric View reader:

- YAML 1.2 booleans;
- a key that is not text is refused;
- a key written twice in one mapping is refused (`malformed`);
- alias expansion is capped, and a recursive alias is refused.

Snowflake itself keeps one of two duplicate keys and accepts the file. Which one
a deployment reads is not something Dex guesses, so this is a deliberate,
recorded difference (the corpus's `stricter/` set).

## What a file is checked against

Each rule below is one row of `RULES` in `semantic_views/validator.py`. Every
refusal names the file and the element (`tables['orders'].metrics['revenue']`).

A problem in one member or relationship refuses that element and keeps the rest.
A problem in a logical table's own definition (name, base table, keys) refuses
the table. A problem in what the whole view rests on refuses the view.

**Structure**

| Rule | What it checks |
|---|---|
| `root`, `name`, `tables` | one mapping, a `name`, at least one logical table |
| `table`, `base_table` | each table has a `name` and a `base_table` (`database`, `schema`, `table`, or a `definition` query) |
| `primary_key`, `unique_keys` | `{columns: [...]}`; no column named twice; a unique key that repeats the primary key, or another unique key, is refused |
| `constraints`, `range` | a `distinct_range` constraint is `{name, distinct_range: {start_column, end_column}}` over two different columns |
| `member`, `duplicate_name` | each member has a `name` and an `expr`; names are unique **within a logical table** (dimensions, time dimensions, facts and metrics together), ignoring case |
| `access_modifier` | `public_access` or `private_access` |
| `dimension_access` (warning) | `private_access` on a dimension |
| `non_additive`, `using_relationships` | each names a dimension, or a relationship, the view declares |
| `unknown_key` (warning) | a key Dex does not know; it is carried and not read |

**Relationships**

| Rule | What it checks |
|---|---|
| `relationship` | names two logical tables of the view and lists `relationship_columns` |
| `self_relationship` | a table related to itself |
| `relationship_cycle` | two tables that reach each other, directly or transitively |
| `one_to_many` | the left columns are a key and the right columns are not |
| `many_to_many` (warning) | neither side's columns are a key |
| `asof` | more than one ASOF column |
| `range` | a range pair needs `right_range`, over two different columns, matching a `distinct_range` constraint on the referenced table |

**Expressions** (parsed with sqlglot in the `snowflake` dialect)

| Rule | What it checks |
|---|---|
| `reference` | a name reads a member of its own table, a column of its base table, or a member of another table the relationships reach, never another table's base column, and never a member the reader refused (it is not read as a base column of that name instead) |
| `row_level` | a dimension or fact aggregates nothing of its own grain (its table, or one joined one-to-one), aggregates only what it reads from a finer table (so `COUNT(*)` is refused), and reads no metric at its own grain |
| `granularity` | a dimension or fact reads a finer table only through an aggregate; a metric reads a finer table's row-level member through a nested aggregate (two) |
| `metric_aggregate` | a table metric aggregates, or reads another metric, and reads no row-level value outside an aggregate (`SUM(x) + x` is refused) |
| `derived_metric` | a view-level metric reads only metrics, is no window function, and shares no name with a table metric |
| `window_reference` | nothing reads a window function metric |
| `multiple_paths` | two tables joined more than one way, or many-to-many, are not read across without `using_relationships` naming a relationship on a path between them |
| `expression_cycle` | no expression reads itself, directly or through others. Two tables whose expressions read each other are not a cycle: Snowflake accepts them |
| `unparsed_expression` (warning) | an expression sqlglot cannot read (a level-of-detail expression, for one) keeps its member, with its rules unchecked |

Granularity follows the relationships. A relationship's left table is finer than
its right table (many rows to one), unless its left columns are themselves a key
(one-to-one), and the order is transitive. A refused member retracts the members
that read it (a metric's non-additive dimension included), to a fixed point.
The search for relationship paths checks reachability first and is bounded, so
a dense relationship graph cannot stall the read.

## Where the port and the documentation disagree

The account decided, and each row below records what it said:

| The documentation says | Snowflake does | Dex |
|---|---|---|
| a view needs a dimension or a metric | accepts a view of facts alone from YAML | reads it |
| `access_modifier` is for facts and metrics; dimensions are always public | accepts and keeps `private_access` on a dimension, and writes `access_modifier` on every dimension on read-back | reads it, and warns on `private_access` |
| a relationship references a primary or unique key | refuses one-to-many; accepts many-to-many and refuses only an expression that reads across it | refuses one-to-many; warns on many-to-many |
| (not documented) | reads a range constraint as `constraints: [{name, distinct_range: {start_column, end_column}}]`, and refuses a range relationship without one | reads and checks it |
| (not documented) | keeps one of two duplicate YAML keys | refuses the file |
| (not documented) | accepts a legacy `measures` table | refuses the file by name |
| no circular references between logical tables | accepts two tables whose expressions read each other, as long as no member reads itself | reads it |

**What only the warehouse can decide.** Whether a name a table reads is one of
its base table's columns is a fact about the warehouse, so the static reader
accepts it. `maintain`'s schema checks (#1293) hold the columns to the warehouse.

## How a Semantic View maps onto the catalog

| Semantic View | Dex catalog |
|---|---|
| view | a namespace: every element is named within it |
| logical table | semantic model `view.table` on the relation its `base_table` names; a `definition` query stands for its table only where it is a plain projection of one table |
| single-column `primary_key` | primary entity `view.table` on that column; each relationship referencing it is a foreign declaration on the referencing table |
| composite `primary_key`, `unique_keys` | declared keys through the declaration channel; a primary key is also the model's declared grain |
| dimension, time dimension | dimension `view.table.member`; a time dimension is `time`, with no queryable grains |
| fact | a dimension row marked `kind: fact`, `groupable: false`: row-level, queried through the `FACTS` clause, never beside a metric |
| table metric, one aggregate | measure **and** `simple` metric `view.table.metric` |
| metric that is one metric divided by another | `ratio` |
| any other table or view-level metric | `derived`, with its expression and the metrics it reads |
| relationship, key equality | declared join with every column pair, and its arity: `many_to_one`, `one_to_one` where the left columns are a key too, `many_to_many` where the right ones are not |
| ASOF or range relationship | named in the notes; no arity, not a key join |
| `description` | description |
| `synonyms` | `vendor_params.synonyms`, which `--search` matches |
| `access_modifier: private_access` | `vendor_params.access: private` |
| `sample_values` | `vendor_params.sample_values.count` only: never surfaced, since Snowflake does not mask them |

A metric is groupable by its own table's dimensions and those of every table its
relationships reach many-to-one, except a table joined to it more than one way
(or many-to-many), unless its `using_relationships` names a relationship on the
path. A view-level metric is groupable by what all of
its inputs share.

**Why every name carries its view.** Two views routinely both declare `orders`.
Inside a view, the name is what Snowflake's own `SEMANTIC_VIEW(...)` clause takes
(`orders.order_count`), and the view is the clause's first argument. A name never
depends on which other views are configured, so adding one moves nothing.

**Columns.** A member that reads another member is followed to the column behind
it. A computed one carries no column: the PII gate parses its expression,
rewritten over the base columns behind its members.

**Carried and not modelled**, each named in the catalog's notes:
- window function metrics
- `non_additive_dimensions` (`NON ADDITIVE BY`)
- ASOF and range relationships
- `using_relationships`
- level-of-detail expressions
- table `filters` and member `labels`
- `variables`, `max_staleness`, `tags` and materializations
- the Cortex Analyst keys: `verified_queries`, `custom_instructions` and
  `module_custom_instructions`

The catalog's declared gaps:
- a display label on every kind
- a metric `filter`, since a Semantic View metric has none apart from its
  expression
- time grains and a time axis
- `model_ref` and `agg_time_dimension`
- an entity's label and description

## `query` and `values` are not available yet

A governed Semantic View query is a `SEMANTIC_VIEW(...)` clause rendered by Dex
and executed through the Snowflake connector under the cost guard (#1290). Until
then `explore semantic query` refuses by name. `explore semantic values` resolves
the token and runs the values PII gate first, so a personal dimension is refused
as PII before it is refused for want of a runtime. Profile the relations the
catalog names with `explore profile` instead.

## Deployed semantic views and the warehouse

A semantic view on the account is not a relation Dex profiles: a plain
`SELECT` on one fails. Checked live:
- `SHOW VIEWS` and `SHOW TABLES` do not list a semantic view, so the inventory,
  profiling, snapshots and `maintain grain` never reach one;
- `SHOW COLUMNS` lists its members, with `kind` `DIMENSION`, `FACT` or `METRIC`,
  and the inventory skips every row that is not a `COLUMN`;
- the dev-namespace existence check lists semantic views with
  `SHOW SEMANTIC VIEWS`, so a schema holding one is not read as empty.

## What `maintain` makes of the layer

| Axis | On Semantic Views |
|---|---|
| `snapshot` | fingerprints the view's own keys (description, `variables`, `max_staleness`, tags) as a definition named after the view, each logical table (`view.table`, with its members' columns and its primary and unique keys), each table and view-level metric, and each key-equality relationship with its ordered pairs and arity; records `vendor: snowflake` and `format_version` |
| `schema` | unchanged: no vendor schema axis yet (#1293) |
| `volume` | unchanged: it reads the warehouse, not the layer |
| `grain` | measures each primary and unique key (`declared_grain_not_unique`) and each relationship's arity (`join_cardinality_exceeded`); an ASOF or range relationship states no arity and is named unchecked |
| `semantic` | definition drift, as everywhere |
| `reconcile` | advisory only: there is no `semantic snowflake update` route yet (#1292) |

**A read-back fingerprints as its file.** `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`
changes nothing that is part of the definition, so the hash covers a canonical
form. What Snowflake does to a view on read-back:
- spells every identifier in upper case;
- sorts lists;
- writes `access_modifier: public_access` and the column's `data_type` on every
  member;
- re-types `sample_values`;
- adds empty `synonyms` and `description` to a derived metric;
- moves `custom_instructions` under `module_custom_instructions`.

The canonical form therefore:
- names every definition in lower case, and folds columns and table parts too;
- keys lists of named elements by name;
- drops the defaulted values;
- leaves out `data_type`, and `sample_values` except an `is_enum` dimension's,
  which are its complete allowed set and so are hashed (never stored);
- hashes expressions as written.

Every fixture in the corpus is held to this: its read-back fingerprints
identically to the file it came from.

## Authoring

Not yet: `semantic snowflake define|update|plan` and the derivation from a
declared mart are #1292. An edit to a view is a direct edit to its file. Read it
back with `explore semantic list`, which validates it with the same port. Never
deploy it from Dex: `SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` is outside Dex.

## The dogfood corpus

`packages/dex-core/tests/semantic_views_vendor/fixtures/` is both the offline test
corpus and what a maintainer deploys to a scratch schema with
`scripts/snowflake_semantic_views.py` (after
`scripts/setup_snowflake_semantic_views.sh`). It is split three ways:
- `valid/`: Snowflake accepts it and Dex reads it.
- `invalid/`: both refuse it.
- `stricter/`: Snowflake accepts it and Dex refuses it by design.
- `readback/`: what Snowflake returned for each deployed `valid/` view.

`verify_only` needs a running warehouse and resumes it, so each verify run bills
at least a warehouse resume.
