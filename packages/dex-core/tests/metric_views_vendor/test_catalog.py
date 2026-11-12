"""How a Metric View maps onto dex's catalog and declaration channel (#1257)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.metric_views import lineage

from .conftest import ORDERS, source, view


@pytest.fixture
def orders(tmp_path: Path):
    """The `ORDERS` star as a source."""
    return source(tmp_path, orders=ORDERS)


def _by(items, key="name"):
    """``items`` keyed by one attribute."""
    return {getattr(item, key): item for item in items}


def test_a_view_is_one_semantic_model_on_its_source(orders):
    """The model, its relation and description, and the naming note."""
    catalog = orders.semantic_catalog()

    (model,) = catalog.semantic_models
    assert model.name == "orders"
    assert model.relation == "demo.main.orders"
    assert model.description == "One row per placed order."
    assert any("named after its file" in note for note in catalog.notes)


def test_each_field_names_the_column_behind_it_only_on_the_views_relation(orders):
    """Joined and computed fields claim no column; the PII gate still reaches theirs."""
    catalog = orders.semantic_catalog()
    rows = _by(catalog.dimensions, "definition")

    assert rows["order_id"].column == "order_id"
    assert rows["status"].column == "status"  # `source.status`
    assert rows["ordered_on"].column == "placed_at"
    # Read through joins: no column on the view's relation, and the PII gate
    # still reaches the joined table's column.
    assert rows["customer_name"].column is None
    assert rows["nation"].column is None
    assert catalog.physical_columns["orders.customer_name"] == (
        "demo.main.customers",
        "name",
    )
    assert catalog.physical_columns["orders.nation"] == ("demo.main.nations", "n_name")
    # Computed: no column, and the gate gets the expression over the source.
    assert rows["size"].column is None
    relation, expression = catalog.computed_columns["orders.size"]
    assert relation == "demo.main.orders" and "amount" in expression


def test_the_metadata_a_field_carries(orders):
    """Label, description, synonyms, the join it reads, and `time` from `format`."""
    rows = _by(orders.semantic_catalog().dimensions, "definition")

    status = rows["status"]
    assert status.name == "orders.status"
    assert status.label == "Order status"
    assert status.description == "Where the order is in fulfilment."
    assert status.vendor_params["synonyms"] == ["state"]
    assert status.type == "categorical"
    assert rows["ordered_on"].type == "time"  # its format says date
    assert rows["nation"].vendor_params["join"] == "customer.nation"
    assert all(row.queryable_granularities is None for row in rows.values())


def test_measures_and_metrics_by_shape(orders):
    """Aggregates are measures and simple metrics; compositions are ratio or derived."""
    catalog = orders.semantic_catalog()
    measures = _by(catalog.measures)
    metrics = _by(catalog.metrics)

    assert set(measures) == {"revenue", "order_count", "paid", "units"}
    assert measures["revenue"].agg == "sum" and measures["revenue"].column == "amount"
    assert measures["order_count"].agg == "count"
    assert measures["order_count"].column is None
    assert measures["units"].column is None  # on the joined table

    assert metrics["orders.revenue"].type == "simple"
    assert metrics["orders.revenue"].label == "Revenue"
    aov = metrics["orders.aov"]
    assert aov.type == "ratio"
    assert (aov.composition.numerator, aov.composition.denominator) == (
        "orders.revenue",
        "orders.order_count",
    )
    assert aov.input_measures == ["revenue", "order_count"]
    net = metrics["orders.net"]
    assert net.type == "derived"
    assert net.composition.input_metrics == ["orders.revenue"]
    running = metrics["orders.running"]
    assert running.type == "derived"
    assert running.vendor_params["window"] == [
        {"order": "ordered_on", "range": "cumulative"}
    ]
    assert "running" not in measures


def test_the_view_filter_rides_on_every_metric_and_a_measure_filter_beside_it(orders):
    """A view `filter` is on each metric, ANDed with a measure's own `FILTER`."""
    metrics = _by(orders.semantic_catalog().metrics)

    assert metrics["orders.revenue"].filter == "status != 'cancelled'"
    assert metrics["orders.paid"].filter == "(status != 'cancelled') AND (paid)"


def test_a_metric_groups_by_its_views_fields(orders):
    """Groupable tokens are the view's own fields, in order."""
    metrics = _by(orders.semantic_catalog().metrics)

    assert metrics["orders.revenue"].dimensions == [
        "orders.order_id",
        "orders.status",
        "orders.ordered_on",
        "orders.customer_name",
        "orders.nation",
        "orders.size",
    ]


def test_each_join_is_a_declared_relationship_child_to_parent(orders):
    """Including a nested join, and a `one_to_many` join turned round."""
    declared = orders.declared_definitions()
    rows = {r.name: r for r in declared.declared_relationships}

    customer = rows["orders.customer"]
    assert (customer.model, customer.to_model) == ("orders", "orders.customer")
    assert customer.relation == "demo.main.orders"
    assert customer.to_relation == "demo.main.customers"
    assert customer.column_pairs == [("customer_id", "id")]

    nation = rows["orders.customer.nation"]
    assert (nation.model, nation.to_model) == (
        "orders.customer",
        "orders.customer.nation",
    )
    assert nation.column_pairs == [("nation_key", "nation_key")]

    # one_to_many: the joined table is the many side, so it is the child.
    lines = rows["orders.lines"]
    assert (lines.model, lines.to_model) == ("orders.lines", "orders")
    assert lines.column_pairs == [("order_id", "order_id")]


def test_rely_is_a_declared_key_on_the_joined_side_and_nothing_else(orders):
    """A unique key, never primary, and only where `rely` says so."""
    declared = orders.declared_definitions()

    assert [
        (k.model, k.column, k.unique, k.primary) for k in declared.declared_keys
    ] == [("orders.customer", "id", True, False)]
    assert declared.declared_composite_keys == []
    assert declared.source_relations["orders.customer"] == "demo.main.customers"


def test_a_join_to_another_views_source_is_that_view(tmp_path: Path):
    """The join's far side, its key and its read columns land on that view."""
    customers = view("demo.main.customers", fields=[{"name": "id", "expr": "id"}])
    layer = source(tmp_path, orders=ORDERS, customers=customers)

    rows = {r.name: r for r in layer.declared_definitions().declared_relationships}
    assert rows["orders.customer"].to_model == "customers"
    keys = layer.declared_definitions().declared_keys
    assert [(k.model, k.column) for k in keys] == [("customers", "id")]
    fingerprint = {m.name: m for m in layer.semantic_layer().semantic_models}
    assert fingerprint["customers"].keys == [["id"]]
    assert "orders.customer" not in fingerprint
    # The column `orders.customer_name` reads from that table is fingerprinted
    # on the table's own model, so dropping it is still reported.
    assert fingerprint["customers"].dimensions == {
        "id": "id",
        "orders.customer_name": "name",
    }


def test_a_join_condition_that_is_not_key_pairs_declares_no_relationship(
    tmp_path: Path,
):
    """A range join is named in a note, not guessed into pairs."""
    body = view(
        joins=[
            {
                "name": "rates",
                "source": "demo.main.rates",
                "on": "source.day BETWEEN rates.valid_from AND rates.valid_to",
            }
        ],
        fields=[{"name": "x", "expr": "x"}],
    )
    layer = source(tmp_path, orders=body)

    assert layer.declared_definitions().declared_relationships == []
    assert any("not only equalities" in n for n in layer.semantic_catalog().notes)


def test_a_projection_query_source_is_its_table_and_a_filtered_one_is_not(
    tmp_path: Path,
):
    """Projection, filtered and joined query sources."""
    projected = view(
        "SELECT order_id, amount_usd AS amount FROM demo.main.orders",
        fields=[{"name": "order_id", "expr": "order_id"}],
        measures=[{"name": "revenue", "expr": "SUM(amount)"}],
    )
    filtered = view(
        "SELECT * FROM demo.main.orders WHERE NOT is_test",
        fields=[{"name": "order_id", "expr": "order_id"}],
    )
    joined = view(
        "SELECT o.* FROM demo.main.orders o JOIN demo.main.c c ON o.k = c.k",
        fields=[{"name": "order_id", "expr": "order_id"}],
    )
    layer = source(tmp_path, projected=projected, filtered=filtered, joined=joined)
    catalog = layer.semantic_catalog()
    models = _by(catalog.semantic_models)
    measures = _by(catalog.measures)

    assert models["projected"].relation == "demo.main.orders"
    assert measures["revenue"].column == "amount_usd"
    assert models["filtered"].relation is None
    assert catalog.physical_columns["filtered.order_id"] == (
        "demo.main.orders",
        "order_id",
    )
    assert models["joined"].relation is None
    assert "joined.order_id" not in catalog.physical_columns
    assert any("filters `demo.main.orders`" in n for n in catalog.notes)


def test_an_aggregate_or_having_source_claims_no_relation(tmp_path: Path):
    """An aggregate, or a HAVING with no GROUP BY, collapses the rows, so the
    view claims no relation and the note says why, as for a GROUP BY."""
    aggregated = lineage.source_relation("SELECT sum(b) AS a FROM t")
    assert aggregated.relation is None and aggregated.gate is None
    assert "an aggregate" in (aggregated.note or "")

    having = lineage.source_relation("SELECT a FROM t HAVING sum(b) > 0")
    assert having.relation is None and having.gate is None
    assert "a HAVING" in (having.note or "")

    # The issue's example: both reasons at once, both named.
    both = lineage.source_relation("SELECT sum(b) AS a FROM t HAVING sum(b) > 0")
    assert both.relation is None and both.gate is None
    assert "an aggregate" in (both.note or "") and "a HAVING" in (both.note or "")


def test_an_aggregate_or_having_source_claims_no_relation_in_the_catalog(
    tmp_path: Path,
):
    """The collapsed view is no semantic model relation, and the catalog note
    carries the reason; a windowed view keeps its table."""
    aggregated = view(
        "SELECT sum(amount) AS total FROM demo.main.orders",
        fields=[{"name": "total", "expr": "total"}],
    )
    having = view(
        "SELECT status FROM demo.main.orders HAVING sum(amount) > 0",
        fields=[{"name": "status", "expr": "status"}],
    )
    windowed = view(
        "SELECT order_id, row_number() OVER (ORDER BY placed_at) AS rn "
        "FROM demo.main.orders",
        fields=[{"name": "order_id", "expr": "order_id"}],
    )
    layer = source(
        tmp_path, aggregated=aggregated, having=having, windowed=windowed
    )
    catalog = layer.semantic_catalog()
    models = _by(catalog.semantic_models)

    assert models["aggregated"].relation is None
    assert models["having"].relation is None
    assert models["windowed"].relation == "demo.main.orders"
    notes = " ".join(catalog.notes)
    assert "an aggregate" in notes and "a HAVING" in notes


def test_a_window_function_source_keeps_its_table():
    """A window function keeps the row count, so it may keep the claim: a bare
    row_number, and an aggregate used only as a window."""
    row_number = lineage.source_relation(
        "SELECT row_number() OVER (PARTITION BY x ORDER BY y) AS rn, a FROM t"
    )
    assert row_number.relation == "t" and row_number.gate == "t"

    windowed_sum = lineage.source_relation("SELECT sum(b) OVER () AS a FROM t")
    assert windowed_sum.relation == "t" and windowed_sum.gate == "t"


def test_a_field_reading_an_earlier_field_resolves_through_it(tmp_path: Path):
    """A bare name matching an earlier field is that field."""
    body = view(
        fields=[
            {"name": "buyer", "expr": "customer_id"},
            {"name": "buyer_again", "expr": "buyer"},
        ]
    )
    rows = _by(
        source(tmp_path, orders=body).semantic_catalog().dimensions, "definition"
    )

    assert rows["buyer_again"].column == "customer_id"


@pytest.mark.parametrize(
    ("connector", "relation", "column"),
    [
        ("snowflake", "DEMO.MAIN.ORDERS", "STATUS"),
        ("databricks", "demo.main.orders", "status"),
        # BigQuery keeps case, so the names are the ones written.
        ("bigquery", "demo.MAIN.Orders", "Status"),
    ],
)
def test_names_are_folded_the_way_the_connector_folds_them(
    tmp_path: Path, connector, relation, column
):
    """Snowflake upper-cases, Databricks lower-cases, BigQuery keeps case."""
    body = view("demo.MAIN.Orders", fields=[{"name": "status", "expr": "Status"}])
    catalog = source(tmp_path, connector=connector, orders=body).semantic_catalog()

    assert catalog.semantic_models[0].relation == relation
    assert catalog.dimensions[0].column == column


def test_a_partial_source_name_claims_no_relation_and_says_why(tmp_path: Path):
    """The PII gate still matches it by suffix."""
    body = view("orders", fields=[{"name": "x", "expr": "x"}])
    catalog = source(tmp_path, orders=body).semantic_catalog()

    assert catalog.semantic_models[0].relation is None
    assert catalog.physical_columns["orders.x"] == ("orders", "x")
    assert any("not one fully qualified relation" in n for n in catalog.notes)


def test_parameters_materialization_and_partition_are_named_not_modelled(
    tmp_path: Path,
):
    """Each is carried, noted, and never a measure. Two views, since Databricks
    refuses to materialize a view that declares parameters."""
    parameterized = view(
        parameters=[{"name": "region", "data_type": "string", "default": "'EU'"}],
        fields=[{"name": "x", "expr": "x"}],
        measures=[
            {
                "name": "per_x",
                "expr": "COUNT(DISTINCT id)",
                "partition": {"include": ["x"], "outer_aggregate": "SUM"},
            }
        ],
    )
    materialized = view(
        "demo.main.daily",
        materialization={
            "mode": "relaxed",
            "materialized_views": [
                {"name": "daily", "type": "aggregated", "dimensions": ["y"]}
            ],
        },
        fields=[{"name": "y", "expr": "y"}],
    )
    catalog = source(
        tmp_path, orders=parameterized, daily=materialized
    ).semantic_catalog()
    notes = " ".join(catalog.notes)

    assert "parameters (region)" in notes
    assert "materialization" in notes and "never deploys" in notes
    assert "partition measure" in notes
    (metric,) = catalog.metrics
    assert metric.type == "derived" and metric.vendor_params["partition"][
        "include"
    ] == ["x"]
    assert catalog.measures == []


def test_the_fingerprint_keeps_each_joined_tables_columns_and_key(orders):
    """Joined tables are their own models; joins are many_to_one."""
    layer = orders.semantic_layer()
    models = {m.name: m for m in layer.semantic_models}

    assert (layer.vendor, layer.format_version) == ("databricks", "1.1")
    assert layer.vendor_release
    assert models["orders.customer"].relation == "demo.main.customers"
    assert models["orders.customer"].dimensions == {"customer_name": "name"}
    assert models["orders.customer"].keys == [["id"]]
    assert models["orders.customer.nation"].dimensions == {"nation": "n_name"}
    assert models["orders"].categorical_dimensions == {
        "order_id": "order_id",
        "status": "status",
    }
    assert {m.name for m in layer.metrics} == {
        f"orders.{m['name']}" for m in ORDERS["measures"]
    }
    assert {r.name: r.cardinality for r in layer.relationships} == {
        "orders.customer": "many_to_one",
        "orders.customer.nation": "many_to_one",
        "orders.lines": "many_to_one",
    }
    assert layer.relationships_and_keys_captured


def test_a_changed_measure_changes_its_hash_and_nothing_else(tmp_path: Path):
    """One measure edited, one metric hash moved."""
    first = {
        m.name: m.content_sha256
        for m in source(tmp_path, orders=ORDERS).semantic_layer().metrics
    }
    edited = {**ORDERS, "measures": [dict(m) for m in ORDERS["measures"]]}
    edited["measures"][0]["expr"] = "SUM(amount_net)"
    second = {
        m.name: m.content_sha256
        for m in source(tmp_path, orders=edited).semantic_layer().metrics
    }

    assert {n for n in first if first[n] != second[n]} == {"orders.revenue"}


@pytest.mark.parametrize(
    ("on", "pairs"),
    [
        ("source.a = c.b", [("a", "b")]),
        ("c.b = source.a", [("a", "b")]),
        ("source.a = c.a AND source.d = d2", [("a", "a"), ("d", "d2")]),
        ("source.a = c.b AND c.active", None),
        ("source.a = other.b", None),
    ],
)
def test_join_conditions_read_as_ordered_pairs(on, pairs):
    """Either side order; anything but key equalities names no pairs."""
    found, _ = lineage.join_pairs(on, None, "c", ("c",), (("source",),))
    assert found == pairs


def test_a_nested_join_names_its_parent_by_name_or_path():
    """`customer.c_nationkey = nation.n_nationkey` under `customer`."""
    found, _ = lineage.join_pairs(
        "customer.c_nationkey = nation.n_nationkey",
        None,
        "nation",
        ("customer", "nation"),
        (("customer",), ("customer",)),
    )
    assert found == [("c_nationkey", "n_nationkey")]


def test_an_aggregate_over_a_joined_column_is_fingerprinted_on_that_table(
    tmp_path: Path,
):
    """`SUM(lines.quantity)` reads the joined table, so dropping `quantity` there
    has to be detectable: on the join's own model, or on the model of the view
    that sits on the same table."""

    orders = view(
        joins=[
            {
                "name": "lines",
                "source": "demo.main.order_lines",
                "on": "source.order_id = lines.order_id",
                "cardinality": "one_to_many",
            },
            {
                "name": "customer",
                "source": "demo.main.customers",
                "on": "source.customer_id = customer.id",
            },
        ],
        fields=[{"name": "status", "expr": "status"}],
        measures=[
            {"name": "units", "expr": "SUM(lines.quantity)"},
            {"name": "balance", "expr": "SUM(customer.balance)"},
        ],
    )
    customers = view("demo.main.customers", fields=[{"name": "id", "expr": "id"}])
    layer = source(tmp_path, orders=orders, customers=customers).semantic_layer()
    models = {m.name: m for m in layer.semantic_models}

    assert models["orders.lines"].measures == {"units": "quantity"}
    assert models["customers"].measures == {"orders.balance": "balance"}
    assert models["orders"].measures == {"units": None, "balance": None}
