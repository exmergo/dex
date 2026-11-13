# Databricks Metric View compatibility

What Dex accepts from a Databricks Unity Catalog
[Metric View](https://docs.databricks.com/aws/en/uc-semantics/metric-views/yaml-reference)
file, what it checks, what it makes of what it read, and what it does not claim.
Read it beside
[Databricks Metric Views](semantic-layer.md#databricks-metric-views-vendor-databricks),
which describes how to configure the layer.

## The pinned rules

Databricks publishes the Metric View YAML specification as prose and enforces it
when `CREATE VIEW ... WITH METRICS` runs in a workspace. Dex reads the committed
YAML without one, so it restates the rules it relies on as its own port, pinned
to one specification version. The catalog mapping is ported from the `databricks`
spoke of [apache/ossie-converters](https://github.com/apache/ossie-converters),
which converts Metric View YAML to and from Apache Ossie. A Metric View and its
Ossie rendering therefore agree on what each construct means.

| | |
|---|---|
| Specification | Metric View YAML `1.1` |
| Mapping ported from | apache/ossie-converters, `databricks/java/README.md` at `82006ba2` |
| Converter fixtures held to | `ossie_fixtureA`, `ossie_fixtureB`, `ossie_tpcds` (Metric View side) |
| License of the ported fixtures | Apache License 2.0 |

The fingerprint records `format_version: 1.1` and the converter commit
(`vendor_release`) beside every hash. A later port that reads a file differently,
or a move to another specification version, is reported as that change rather
than as every view changing.

## Required extras

| capability | extra | absent |
|---|---|---|
| `config validate` and `config get` on a `semantic.databricks` block | none | always available |
| reading the layer: `explore semantic list`, `--use-project`, `maintain` | `[metric-views]`, the dialect engine (every connector extra already carries it) | refused by name, `reason: prerequisite` |

Reading the layer installs no Databricks package and opens no workspace
connection. The `[databricks]` extra is the warehouse connector. Only a governed
query (#964) needs it.

## The YAML body is read; the DDL is not

Each configured file holds one Metric View YAML body: the document that
`CREATE VIEW <name> WITH METRICS LANGUAGE YAML AS $$ ... $$` holds between its
delimiters. This is also the format the Ossie converter reads and writes.

- **A view is named after its file's stem**, because the YAML carries no name.
  `views/orders.yaml` is the view `orders`. The name it is deployed under in Unity
  Catalog is not read.
- **Two files with one stem are refused** in configuration, case ignored. They
  would name one view twice.
- **A `.sql` file is refused by name.** The DDL bundles deployment settings
  (`OR REPLACE`, `TBLPROPERTIES`, column aliases, `COPY *`) with the definition.
  Dex reads definitions and never deploys, so it does not guess the YAML out of a
  statement.

Dex never runs `CREATE VIEW`, deploys a materialization, or changes a Unity
Catalog privilege. Reading the layer writes no file.

## What a file is checked against

YAML is read the way Databricks reads it:

- **YAML 1.2 booleans.** A join's bare `on:` key stays the text `on`, where YAML
  1.1 would read it as `true`.
- **Duplicate keys.** A key written twice in one mapping is refused.

**The version.** `version` is required and must be `1.1`. `0.1`, or any other
version, refuses the view, and the refusal names the re-save that upgrades it
(`ALTER VIEW ... AS $$...$$`). Copy Unity Catalog comments into `comment` first,
since the upgrade wipes them.

**What refuses the whole view**, because Databricks would refuse to create it:

- a missing or empty `source`;
- neither `fields` nor `measures`;
- malformed `parameters`: each needs a `name` and a `data_type`, and once one
  parameter has a `default`, every later one needs one too;
- malformed `materialization`: `mode: relaxed`, a non-empty `materialized_views`
  list with a `name` and a `type` on each, at most one `unaggregated` entry with
  no dimensions or measures, and `dimensions` or `measures` on each `aggregated`
  one;
- `materialization` on a view that declares `parameters`, which Databricks
  binds per query, so there is no one result to maintain.

**What refuses one element**, leaving the rest of the view:

- **Field or measure.** A non-empty `expr`, and an explicit `name` that is not
  empty. Names are unique across fields and measures, case ignored, and the later
  of two is refused.
- **`display_name`.** At most 255 characters.
- **`synonyms`.** At most 10 entries of at most 255 characters each.
- **`format`.** Checked by `type` (`number`, `currency`, `percentage`, `byte`,
  `date`, `date_time`) with that type's keys:
  - a currency format needs an ISO-4217 `currency_code`;
  - a `date` format needs `date_format`, and a `date_time` format needs both
    `date_format` and `time_format` (Databricks refuses either without, checked
    with `CREATE VIEW ... WITH METRICS` in #1258);
  - `decimal_places` of type `max` or `exact` needs `places` from 0 to 10;
  - `date_format` and `time_format` cannot both be none.
- **Measure `window`.**
  - `order` and `range` are required.
  - `range` is `current`, `cumulative`, `all`, or
    `trailing|leading <n> [<unit>] [inclusive|exclusive]`.
  - `semiadditive` is `first` or `last`.
  - `offset` is a signed integer or `<n> day|month|year[s]`.
  - The number in a `range` or an `offset` may be an integer parameter the view
    declares, by its bare name (`trailing days_back day`). A name that is no
    integer parameter of the view is refused.
- **Measure `partition`.** `include` is a list of field names, and
  `outer_aggregate` is one of `SUM`, `AVG`, `MIN`, `MAX`, `COUNT` or `MEDIAN`. A
  measure has a `window` or a `partition`, never both.
- **Join.** A `name` that is an identifier and is not `source`, and a `source`.
  - It needs `on` or `using`. With both, `on` wins, as in Databricks, and Dex
    warns.
  - `cardinality` is `many_to_one` (the default) or `one_to_many`, and every join
    below a join keeps that join's cardinality.
  - `rely` takes `at_most_one_match: true|false`.
  - Sibling joins have distinct names. A refused join takes the joins below it
    with it.
- **Expressions.** These are read with the Databricks SQL dialect:
  - every `MEASURE(name)` names a measure of the view defined **before** it,
    which also rules out a cycle. A refused measure retracts the measures that
    read it, to a fixed point;
  - no field reads a `one_to_many` join. Only a measure can aggregate over one;
  - a nested join is read through its whole path: `orders.customer.c_name`,
    never `customer.c_name`.

**What only warns**, while reading:

- **An unknown key**, at any level, `rely` included. Databricks refuses it on
  create, but the rest of the definition is still readable. Authoring refuses it
  in a file the plan writes (see [Authoring](#authoring)).
- **`on` beside `using`.**
- **`fields` and its older spelling `dimensions` both written.** Databricks
  creates the view from `dimensions` and drops `fields`, and so does the Ossie
  converter. Dex reads `dimensions` too and names what it dropped. Authoring
  refuses it in a file the plan writes.
- **A field reading a later field's name, or a `filter` reading a field's name.**
  A field reads only the fields before it, and a `filter` reads source columns
  only, so Databricks reads such a name as a source column and refuses the view
  if there is none. Whether there is one is the warehouse's to say, so Dex warns
  rather than refuses.
- **A field or measure with no `name`.** It is named by the rule Databricks
  applies on DBR 19 and later. A plain column keeps its final identifier,
  including inside `CAST`; any other expression keeps its text with the
  whitespace removed (`SUM(a+b)`).
- **A wildcard** (`source.*`, `<join>.* EXCEPT (...)`). Databricks expands it from
  the warehouse schema when the view is created. Dex does not expand it, and the
  members it would add are not listed. A wildcard carrying a `name` or other
  metadata is refused.
- **An expression sqlglot cannot parse.** Its column is not resolved.

Every finding names the file and the element (`measures['revenue']`,
`joins['customer.nation']`, `fields['status'].format`). One broken file leaves the
rest readable, and the catalog raises only when nothing at all could be read.

These rules were held to Databricks itself: each refusal above, and each
reading Dex makes of `fields` beside `dimensions`, forward references and nested
paths, was checked against `CREATE VIEW ... WITH METRICS` on a serverless SQL
warehouse. Where they differ, the difference is deliberate:

- **Version `0.1`.** Databricks still creates it; Dex reads `1.1` only.
- **An unknown key.** Databricks refuses the view; Dex warns while reading, and
  refuses it in a file a plan writes.
- **A duplicate name.** Databricks refuses the view; Dex refuses the later
  member and keeps the rest while reading. A plan that writes a duplicate name is
  refused, since any error in the set refuses the plan.

## References and lineage

A column is addressed the way the specification addresses it:

- In a field or measure, `source.<col>` or a bare `<col>` reads the view's
  source. `<join>.<col>` reads a join, and a nested join is read through its
  whole path (`customer.nation.n_name`).
- A bare name that matches an earlier field is that field, not a source column of
  the same name. A field reads only the fields before it.
- In a join's `on`, `source.` is the view's source, the join's own name (or a
  bare column) is the joined table, and a nested join names its parent by the
  parent's name.

A member's physical column is traced from its `expr` only where the expression
is a single column. A computed field resolves to no column, and the PII gate is
given its SQL over the one table it reads, so every column it touches is
screened. A resolved name is folded the way the active connector folds an
unquoted identifier: on Snowflake, `customer_id` is `CUSTOMER_ID`.

A source or a join's source depends on how it is written:

- **A table name** is the relation, read with the active connector's folding. A
  name that is not fully qualified (Databricks completes it from the session's
  catalog and schema) claims no relation, with a note. The PII gate still matches
  it by suffix.
- **A query** is a relation only as `SELECT <columns or *> FROM <one table>` with
  no other clause, and its columns are traced through the projection.
- **A filtered single-table query** keeps its table for the PII gate alone, with
  a note, because its rows are not the table's.
- **A joined, grouped or otherwise shaped query** has no relation.

A join's key pairs come from `using`, or from `on` when it is an equality or a
conjunction of equalities between a parent column and a joined column. Any other
condition, such as a range join, declares no pairs, and a note says so.

## How a Metric View maps onto the catalog

| Metric View | catalog |
|---|---|
| view (one file) | one semantic model named after the file, on its source's relation. `comment` is its description |
| field (or `dimensions` entry) | a dimension `view.field`. `display_name` is the label and `comment` the description. `synonyms`, `format`, the `expr` and the join it reads ride in `vendor_params`. Its `column` is claimed only on the view's own relation; a joined field's column reaches the PII gate through `physical_columns` |
| field type | `time` only where `format.type` is `date` or `date_time`, `categorical` otherwise. Metric Views type nothing else |
| measure that is one aggregate (`SUM`, `COUNT`, `COUNT(DISTINCT)`, `AVG`, `MIN`, `MAX`, `MEDIAN`, `APPROX_COUNT_DISTINCT`) | a measure **and** a `simple` metric. `FILTER (WHERE ...)` becomes the metric's filter |
| `MEASURE(a) / MEASURE(b)` | a `ratio` metric with both sides |
| any other composition | a `derived` metric with its input metrics and the aggregating measures beneath them |
| `window` or `partition` measure | a `derived` metric with the spec carried verbatim in `vendor_params`, and a note that it is not modelled |
| view `filter` | carried on every metric of the view, because it applies to every query |
| `joins` (nested) | one declared relationship per join with key pairs, written child to parent: the table it hangs off for `many_to_one`, the joined table for `one_to_many` |
| `rely.at_most_one_match: true` | a declared unique key on the joined side's join columns, composite where there are several. It is not a primary key and not a grain claim |
| a joined table that is another configured view's source | that view, so the views form one graph |
| entities | a declared gap in `unavailable`, never synthesized |
| `parameters`, `materialization`, wildcards | named in `notes`, carried and not modelled |
| groupability | a metric groups by its own view's fields. `dimension_scope: declarations` |

`unavailable` declares what a Metric View has no field for:

- a view's `label`, `model_ref`, `agg_time_dimension` and `primary_entity`;
- a metric's `time_axis` and `queryable_granularities`;
- a dimension's `queryable_granularities`;
- a measure's `agg_time_dimension`;
- every entity field.

A Metric View has no time grains: a time bucket is a field's own SQL
(`DATE_TRUNC('MONTH', o_orderdate)`).

## What the declarations reach in `explore`

`explore relationships|map|profile --use-project` read the declaration channel:

- **Joins.** Each join with key pairs is a declared edge at confidence 1.0,
  `declared_by: databricks relationship '<view>.<join>'`. `--verify` measures it.
- **`rely` keys.** Each `rely.at_most_one_match` key is a declared unique key on
  the joined relation. Profiling that finds duplicates says so against "the
  Databricks Metric View layer". Databricks does not check `rely`, and a wrong one
  returns wrong numbers, which is why Dex measures it rather than trusting it.
- **Exposure.** `explore map --use-project` marks each relation a view sits on
  with that view under `semantic_models`.

A Metric View has no metadata block a Dex declaration (`meta.dex`) could live in,
so `declaration` is never carried from this layer.

## `query` and `values` are not available yet

A governed Metric View query is SQL over the view with `MEASURE()`, executed by
Dex through the Databricks connector under the cost guard. That is #964. Until
then:

- `explore semantic query` refuses by name.
- `explore semantic values` resolves the token and runs the values PII gate
  first, so a personal field is refused as PII before it is refused for want of a
  runtime.

## What `maintain` makes of the layer

`maintain snapshot` fingerprints the layer:

- **Each view** is one semantic model. Its fields and aggregating measures on the
  source are mapped to their columns.
- **Each joined table** that is not another view's source is one semantic model
  of its own (`view.join.path`). It holds the fields that read it and its `rely`
  key.
- **Each measure** is one metric. **Each join with key pairs** is one relationship
  oriented child to parent, so its arity is `many_to_one`.

The layer-neutral axes then read the fingerprint:

- `maintain semantic` reports definition drift: a view, measure or join added,
  removed or changed. It also reports a column a field reads that the warehouse
  no longer has.
- `maintain grain` measures each `rely` key (`declared_grain_not_unique`) and
  each join's declared arity. A contradicted `rely` key is worded as the view's
  claim (`rely.at_most_one_match` on a join to the table), not as the project's
  grain: Databricks trusts the flag and returns wrong numbers when it is false.
- `maintain semantic` reports a view re-saved in a spec version Dex does not read
  as `semantic_format_changed` for that file, naming it and both versions: the
  fingerprint records each file's declared `version` under `definition_versions`,
  so the other views are still compared by content.

`maintain schema` holds each view to the warehouse metadata Dex captured (#1259),
with no workspace connection. Each finding names the view, the element and the
column:

| Code | When |
|---|---|
| `declared_column_missing` | a field, measure, `filter`, join `on`/`using` or query `source` reads a column its table no longer has |
| `declared_relation_missing` | the view's `source` (the whole view) or a join's `source` is gone since the baseline |
| `declared_type_conflict` | a `date`/`date_time` format on a column that is not temporal, a number, currency, percentage or byte format on text, or `SUM`/`AVG` over text |
| `declared_dependency_dropped` | a field, measure (through `MEASURE()` too), window, or nested join loses an input to another removal |

A relation neither the connection nor the baseline lists, and an expression Dex
cannot read, are named in `warnings` as unchecked rather than passed.

`maintain reconcile` cuts each removal out of the view file and stores it as a
`semantic databricks update` plan (`data.semantic_plan_id`), validated by the
reader before it is stored, so a reconciled view reads back. A removed field,
measure or join is cut by the span it occupies (a nested join goes with its
parent), a contradicted `format` is cut from its member, and every other byte is
kept. These stay advisory and are never written:

- a view or a join whose `source` is gone: a vanished table is as often a rename;
- a `filter` reading a dropped column: the filter is the view's population;
- a query `source` reading a dropped column: no member removal repairs it;
- a removal a `materialization` lists, or one that would leave the view no field
  and no measure;
- a file already holding a key Databricks refuses.

A Metric View already deployed in the connector's source scope is not a
relation Dex reads: Databricks refuses a plain `SELECT` of its measure columns,
so the Databricks connector leaves Unity Catalog objects of type `METRIC_VIEW`
out of its inventory. The view's YAML in the repository is what Dex reads.

The Ossie bridge is #1260.

## Authoring

`semantic databricks define|update|plan --edits-file <f>` (#1258) authors the
configured files through a stored plan that `transform apply` writes. Dex never
deploys: nothing runs `CREATE VIEW ... WITH METRICS`, and no authoring path opens
a workspace connection.

- **The write surface** is exactly `semantic.databricks.files`. A path off it, a
  `.sql` file, another suffix and a stem another file already names are refused by
  name, the first with the `dex config set` that adopts it.
- **Edits are whole files, written byte-for-byte**, all or none. A file changed
  after planning is a conflict. The set apply would leave is validated again.
- **The whole prospective set is validated** by the rules above. Every error
  anywhere in the set refuses the plan, and so do two warnings in a file the plan
  writes: an unknown key, and `fields` beside `dimensions`.
- **YAML 1.2, never a 1.1 round trip.** A key that is not text is refused while
  reading; a `true:` key names the `on:` rewrite a YAML 1.1 writer makes. Dex's
  own writers (derivation and annotation) read and write YAML 1.2, so `on:` is
  written plain. A file whose YAML aliases expand past 100,000 values, or refer to
  themselves, is refused too.
- **Derivation** from a declared dbt mart, and **annotation** with a
  `declarations` payload, follow the mapping in the transform skill's
  `declarations.md`. A Metric View has no metadata key, so the population is
  carried only as the last sentence of the view's `comment`, which the catalog
  withholds, and the NULL rule, additivity, assumptions and grain are named as not
  carried. A population is never written as a `filter`.
