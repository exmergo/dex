# A native Ossie layer end to end

One [Apache Ossie](https://github.com/apache/ossie) (incubating) document, one
local warehouse, and every command dex has for a native semantic layer, in the
order you would actually run them. Nothing here needs a dbt project, a
credential, a cloud account, or a network connection, which is the point: a
semantic layer is a thing a repository can have on its own.

Read [Querying the semantic layer](semantic-layer.md) for the reference
treatment of the commands and [Apache Ossie
compatibility](ossie-compatibility.md) for the row-by-row statement of what dex
accepts and what it declines to claim. This page is the sequence.

```
pip install "exmergo-dex-core[duckdb,ossie]"
```

`[duckdb]` is the warehouse and `[ossie]` is the document validator. They are
independent: reading, validating, fingerprinting, and authoring a native layer
all happen without a warehouse, and a warehouse install carries no Ossie
support unless you ask for it.

## 1. A warehouse to point at

```
dex demo
dex explore inventory
```

`dex demo` writes `dex_demo.duckdb` (7 tables, 29,512 rows) and a
`.dex/config.yml` beside it, refusing rather than overwriting anything that is
already there. The data is generated from a pinned seed and is deliberately
flawed, which is what makes the rest of this page interesting.

Run `explore inventory` before writing a line of the document. On DuckDB the
catalog name comes from the filename, so the identifiers are
`dex_demo.main.orders` and not `demo.main.orders`, and a document written
against a guess will parse cleanly and link to nothing. The identifiers it
reports are:

```
dex_demo.main.customers
dex_demo.main.order_items
dex_demo.main.orders
dex_demo.main.products
dex_demo.main.returns
dex_demo.main.warehouse_locations
dex_demo.main.web_events
```

## 2. The document

Save this as `semantics/commerce.ossie.yaml`. It is written to exercise every
branch of the read path at once, so it is denser than a real first document
would be.

```yaml
version: "0.2.0.dev0"
semantic_model:
  - name: commerce
    description: Orders, the customers who place them, and the products they buy.
    ai_context:
      instructions: >-
        Revenue is gross and excludes returns. Do not present it as net.
      synonyms: [sales, retail]
    datasets:
      - name: orders
        source: dex_demo.main.orders
        description: One row per placed order.
        primary_key: [order_id]
        fields:
          - name: order_id
            datatype: Integer
            label: Order ID
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_id
          - name: customer_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: customer_id
          - name: status
            datatype: String
            description: Order lifecycle state.
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: status
                - dialect: SNOWFLAKE
                  expression: status
                - dialect: MDX
                  expression: "[Order].[Status]"
          - name: order_total_eur
            datatype: Decimal
            description: Order total converted at a fixed rate.
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_total * 0.92
          - name: quoted_status
            datatype: String
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: '"status"'

      - name: customers
        source: dex_demo.main.customers
        description: One row per registered customer.
        primary_key: [customer_id]
        ai_context: Contains personal data; treat email and full_name carefully.
        fields:
          - name: customer_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: customer_id
          - name: email
            datatype: String
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: email
          - name: country_code
            datatype: String
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: country_code
          - name: signup_date
            datatype: Date
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: signup_date

      - name: order_items
        source: dex_demo.main.order_items
        description: One row per line on an order.
        primary_key: [order_id, product_id]
        fields:
          - name: order_item_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_item_id
          - name: order_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_id
          - name: product_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: product_id
          - name: quantity
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: quantity

      - name: returns
        source: dex_demo.main.returns
        description: One row per returned order line.
        primary_key: [return_id]
        fields:
          - name: return_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: return_id
          - name: order_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_id

      - name: web_events
        source: dex_demo.main.web_events
        description: One row per page view.
        fields:
          - name: customer_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: customer_id
          - name: page_path
            datatype: String
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: page_path

      - name: recent_orders
        source: "SELECT * FROM dex_demo.main.orders WHERE status = 'paid'"
        description: A rolling window, expressed as a query rather than a table.
        fields:
          - name: order_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: order_id

    relationships:
      - name: orders_to_customers
        from: orders
        to: customers
        from_columns: [customer_id]
        to_columns: [customer_id]
      - name: web_events_to_customers
        from: web_events
        to: customers
        from_columns: [customer_id]
        to_columns: [customer_id]
      - name: returns_to_order_items
        from: returns
        to: order_items
        from_columns: [order_id, return_id]
        to_columns: [order_id, order_item_id]

    metrics:
      - name: revenue
        datatype: Decimal
        description: Sum of gross order totals.
        ai_context:
          instructions: Gross of returns. Pair with order_count for an average.
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: SUM(orders.order_total_eur)
            - dialect: SNOWFLAKE
              expression: SUM(orders.order_total_eur)::NUMBER(38,2)
            - dialect: MAQL
              expression: "SELECT SUM({fact/order_total})"
      - name: order_count
        datatype: Integer
        description: How many orders were placed.
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(*)
```

What each part is there to demonstrate:

| in the document | what it exercises |
|---|---|
| `orders.status` in `ANSI_SQL`, `SNOWFLAKE`, and `MDX` | one field, several dialects, one of them not SQL |
| `orders.order_total_eur` | a computed field, which carries no physical column |
| `orders.quoted_status` | a quoted identifier, which also carries none |
| `order_items.primary_key` | a composite grain that reaches exploration |
| `recent_orders.source` | a query-backed dataset, which is opaque |
| `returns_to_order_items` | a composite relationship, two ordered column pairs |
| `web_events_to_customers` | a declaration the warehouse will contradict |
| `revenue` and `order_count` | metric lineage that resolves, and lineage that does not |

## 3. Configuring it

Ossie is selected on the semantic axis. It is never the transformation project,
so there is no `project:` block here at all:

```yaml
connector: duckdb
duckdb:
  path: dex_demo.duckdb
semantic:
  vendor: ossie
  ossie:
    files:
      - semantics/commerce.ossie.yaml
```

Beside a dbt project the only difference is that the project axis is also
populated, and the two do not interact:

```yaml
connector: duckdb
project:
  format: dbt
semantic:
  vendor: ossie
  ossie:
    files:
      - semantics/commerce.ossie.yaml
```

`semantic.ossie.files` is the whole of the layer's coordinates. There is no
discovery step and no glob, because a document dex was not told to read is a
document nobody reviewed. Paths are relative to the repository root and confined
to it: an absolute path, a `..` that walks out, and a symlink resolving out are
each refused.

## 4. Reading the layer

```
dex explore semantic list
```

Four fields say which layer answered, and they are worth reading before
anything else in the payload:

```json
"backend": "local", "vendor": "ossie", "deployment": "local", "execution": "dex"
```

`dimension_scope` is `declarations`, meaning one row per dimension the document
declares rather than one row per token a query could group by. The layer comes
back as six semantic models (one per dataset, namespaced
`<semantic model>.<dataset>`), eighteen dimensions, two metrics, and:

```json
"entities": [], "measures": []
```

Those two are empty because the format has nowhere to put them, and the
`unavailable` block says so rather than leaving you to infer it:

```json
"unavailable": {
  "metrics": ["dimensions", "input_measures", "composition", "filter",
              "time_axis", "queryable_granularities", "label"],
  "entities": ["name", "type", "label", "description", "roles"],
  "measures": ["name", "agg", "expr", "agg_time_dimension", "label",
               "description", "semantic_model", "column"]
}
```

Read that before concluding anything from an empty list. A metric here reports
no groupable dimensions because Ossie states no metric-to-dimension
relationship at all, which is a different fact from a metric that can be
grouped by nothing.

Two more things the catalog is careful about. `commerce.revenue` carries
`semantic_models: ["commerce.orders"]`, because its expression names
`orders.order_total_eur` and that reference resolves. `commerce.order_count` is
`SUM`-free and names nothing, so its lineage is empty rather than every dataset
in the model:

> metric 'order_count' names no dataset field dex could resolve, so its lineage
> is empty. Ossie states no metric-to-dataset reference, and naming every
> dataset in the semantic model would be a claim the document does not make.

## 5. What did not link, and why

Every dimension carries the physical `column` behind it, and four of them carry
none. That is a decision, not a failure, and each one says which decision:

> field 'orders.order_total_eur' is computed rather than a bare column
> (ANSI_SQL: order_total * 0.92), so it carries no physical column. A column
> guessed out of an expression makes the PII gate screen the wrong one and
> report it as evidence

> field 'orders.quoted_status' is a quoted identifier ("status"), so it carries
> no physical column. An unquoted identifier is folded the way the warehouse
> folds it while a quoted one is exact, so the two need not name the same column
> and dex cannot tell which was meant

> dataset 'commerce.recent_orders' has a source dex cannot read as one relation
> on duckdb, so it carries no physical link. Ossie documents a source as a table
> reference or a query without a portable way to tell them apart, and a query
> read as a relation would reach the PII gate as false evidence

The fourth is the one to notice, because it is a check dex ran and reported
rather than a check it passed:

> MAQL, MDX expressions are preserved as written and were not checked: they are
> not SQL, so dex neither parses them nor reads a physical column out of one

Both non-SQL expressions are still in the payload, verbatim, under the element's
`vendor_params.dialects`. Nothing the document says is dropped. What changes is
that dex will not read a column out of them, and says so instead of staying
quiet.

The document also warns, and a warning is not a partial refusal. The layer is
read in full and the warning rides along:

> relationship 'returns_to_order_items' joins to_columns ['order_id',
> 'order_item_id'] on dataset 'order_items', which do not cover its primary key
> or any of its unique keys. The join may fan out

## 6. The governed route when `query` and `values` refuse

Ossie is catalog-first. `list` answers; `query`, `values`, and
`--for-dimension` refuse, and `--api` refuses too:

```
dex explore semantic query revenue
dex explore semantic values customers__country_code
```

> Apache Ossie specifies interchange metadata, not a portable query runtime: it
> defines no filter grammar, no join planning, and no execution semantics, so
> dex has nothing to render a governed statement from.

That is a property of the format rather than a gap in this implementation, and
the refusal names the alternative rather than leaving you stuck. A dimension
carries its `semantic_model`, that model carries its `relation`, and the
relation is an ordinary warehouse object:

```
dex explore profile dex_demo.main.customers
dex explore query "select country_code, count(*) as customers
                   from dex_demo.main.customers
                   group by country_code order by customers desc limit 5"
```

```json
"shape": "columnar",
"columns": ["country_code", "customers"],
"cells": [["IT", 173], ["BE", 163], ["DE", 152], ["ES", 149], ["SE", 145]]
```

This route is governed, not a way around the guards. It runs through the query
firewall and, on a metered warehouse, through the cost handshake. Profiling
flagged `customers.email` at confidence 0.95, so:

```
dex explore query "select email from dex_demo.main.customers limit 5"
```

> query refused: the projection would carry values from PII-flagged column(s):
> customers.email (email). Use a measuring aggregate over them (COUNT, COUNTIF,
> APPROX_COUNT_DISTINCT, AVG(LENGTH(...))), or drop them from the output.

The flag reached that column because the field `customers.email` is a bare
identifier on a relation dex can address. A field that carries no column is
screened by the name heuristic instead, and the payload says which happened.

## 7. What the declarations reach in exploration

```
dex explore map --use-project
dex explore relationships --use-project --verify
dex explore diagram
```

Three things arrive from the document.

**Source annotations.** Each relation is marked with the semantic models sitting
on it, and empty is an answer: `dex_demo.main.products` and
`dex_demo.main.warehouse_locations` come back with `semantic_models: []`,
which is what separates a load-bearing table from a merely large one.

**The declared grain.** `order_items` has a composite `primary_key` in the
document, and it overrides the heuristic, saying so rather than replacing it
silently:

> grain order_id, product_id comes from the project's declared composite key
> (heuristic suggested unit_price, order_id)

**The declared joins**, at confidence 1.0, with the composite kept whole:

```json
{"from_dataset": "dex_demo.main.returns",  "from_columns": ["order_id", "return_id"],
 "to_dataset":   "dex_demo.main.order_items", "to_columns": ["order_id", "order_item_id"],
 "kind": "declared", "confidence": 1.0,
 "declared_by": "ossie relationship 'returns_to_order_items'"}
```

Both pairs survive into `explore diagram`, which labels the edge
`order_id = order_id, return_id = order_item_id`. No first-column proxy is ever
emitted or measured, because a composite measured one column at a time can
report a healthy join three times over while no complete tuple matches.

`--verify` is where the document meets the data. `orders_to_customers` verifies
with no orphans. `web_events_to_customers` does not:

> dex_demo.main.web_events.customer_id -> dex_demo.main.customers.customer_id is
> declared as a foreign key but 100% of values have no match in the parent; the
> project and the warehouse disagree

Its confidence stays at the 1.0 the document asserts. A measurement never
revises a declaration; it reports the disagreement and leaves the decision to
you. The composite edge is measured as a complete tuple and comes back with no
orphan fraction at all, because `returns` is the demo's half-failed load and has
no rows: an honest absence of evidence rather than a clean bill of health.

## 8. A baseline, and drift against it

```
dex maintain snapshot
```

```json
"transform_layer": null,
"semantic_layer": {"semantic_model_count": 6, "metric_count": 2}
```

`transform_layer` is null because there is no transformation project here and
Ossie declares no build step, so a transform baseline over it would be a
baseline of nothing. The two layers are fingerprinted independently, which is
exactly why a repository with a semantic layer and nothing else still gets one.

The semantic side records each dataset and metric with a content hash, the
relation behind it, the column each field resolves to, the declared keys in the
arity they were written, and every relationship with its ordered column pairs.
It also records that it captured them, so a baseline written before that was
possible reports the relationship axis as unchecked rather than as clean.

Now break something. Point `web_events.page_path` at a column that does not
exist, and change one side of the composite relationship:

```
dex maintain semantic
```

> [high] broken_relationship: relationship 'returns_to_order_items' from
> 'commerce.returns' to 'commerce.order_items' names column pair(s) that no
> longer resolve: [['return_id', 'line_item_id']]

> [high] dangling_reference: dimension 'page_path' on semantic model
> 'commerce.web_events' references column 'page_url', which is gone from
> dex_demo.main.web_events

Two more findings at `low` record that the definitions themselves changed. All
of it is free: this axis reads the documents and the cache and opens no
warehouse connection. The command's paid half, dimension cardinality, needs a
semantic model that names a transformation model, and Ossie names none, so
`data.offer` is null and there is nothing to confirm.

`maintain grain` is where the declaration is measured:

```
dex maintain grain
```

> [high] (order_id, product_id) on dex_demo.main.order_items is no longer
> unique: 13952 distinct combinations over 14000 rows (~48 duplicate rows);
> joins on it will fan out

The declared composite reached the grain channel, was measured as a composite,
and the warehouse disagreed with it. It is a finding, never an automatic
rewrite. On DuckDB this is free; on a metered warehouse it goes through the same
`--confirm --budget` handshake every scanning command does.

`maintain reconcile` has nothing to author here. A native semantic layer is not
an editable transformation project, so its proposals are advisory and no plan is
stored. Authoring is the next section, and it is a different command.

## 9. Authoring into the layer

```
dex semantic ossie update "add customer lifetime value" --edits-file edits.json
```

The payload is the ordinary dex edits shape, and `kind` may be omitted because
this command implies `semantic_document`:

```json
{"edits": [{"path": "semantics/commerce.ossie.yaml", "content": "<the whole document>"}]}
```

Edits are whole documents. dex overlays what you supplied onto the other
configured files, validates the complete prospective layer, and only then stores
a plan. It also checks the references against the exploration cache without
opening a connection, and it distinguishes what it verified from what it
declined to:

> cache validation skipped 1 query-backed, quoted, or otherwise opaque dataset
> source(s); dex does not guess a physical relation

> cache validation skipped 2 computed or non-SQL field expression(s); dex
> validates only a direct physical column reference

> cache validation checked 5 direct relation(s) and 29 physical column
> reference(s) using stored evidence only; no warehouse connection was opened

Those are notes. A reference the cache can positively contradict is a refusal,
and no plan is stored:

> the prospective Ossie semantic layer has references contradicted by the
> exploration cache, so no plan was stored: dataset[5] 'suppliers' names source
> 'dex_demo.main.suppliers', but the cached inventory for namespace
> 'dex_demo.main' does not contain it

The namespace guards are the difference between the three modes. `define`
refuses a semantic-model name the layer already has, `update` refuses one it
does not, and `plan` accepts both and reports each under `defined` or `updated`:

> semantic ossie define found names already defined: commerce; use
> `semantic ossie update` or `semantic ossie plan`

Nothing is written until you apply:

```
dex transform apply p1cd6a7ce57
```

If the file changed after the plan was made, the whole apply refuses rather than
writing part of it:

```json
"conflicts": [{"path": "semantics/commerce.ossie.yaml",
               "expected_sha256": "0c5a833d...", "found_sha256": "789230894..."}]
```

> these files changed after the plan was made (human edits are authoritative);
> re-plan against current state, or re-run with `--confirm` to overwrite
> deliberately

On a clean apply the accepted bytes are written exactly as authored. dex does
not parse and re-serialize the document, so comments, key order, quoting, and
whitespace all survive, and configured documents the payload did not mention are
untouched. The comment above the new field is still there afterwards, and
`explore semantic list` now reports `customers__lifetime_value` beside the
others.

## 10. What this page did not prove

**Catalog-first is not a roadmap item.** Ossie specifies interchange metadata
and no portable query runtime. The refusals in section 6 are the format's shape.
Upstream has an active working group on a query language and a reference engine;
when that lands it becomes a declared runtime with its own governed contract,
not a loosening of these commands.

**Validation is not an execution guarantee.** There is no external validator
here the way `dbt parse` is for dbt: there is no published Ossie package and no
runtime to load a document into. A document that passes is schema-valid and
internally consistent. Whether any engine can execute it is a separate question
dex has no basis to answer, and no dex surface says otherwise.

**dbt was never involved.** No dbt project, no MetricFlow, no manifest, at any
step above. The two axes are independent in both directions: dbt does not depend
on Ossie, Ossie does not depend on MetricFlow, and a repository may have either,
both, or one of each.

**dex reads Ossie; it does not convert it.** There is no exporter to or from
another semantic format, and dex says nothing about what another tool would make
of a document it read.

The schema dex validates against is pinned by content rather than by version
string, and upgrading it is a reviewed procedure rather than a version bump. The
pin, the upstream commit, the known deltas, and the upgrade steps are in
[Apache Ossie compatibility](ossie-compatibility.md).
