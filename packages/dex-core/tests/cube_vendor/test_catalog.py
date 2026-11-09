"""How a Cube model set maps onto dex's catalog and declarations (#910).

Columns are traced, never guessed: a member that is not one column of its cube's
relation carries none, and a relation is claimed only where the cube reads one
table as it stands.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.explore.semantic.cube import LocalCubeLayer

from .conftest import ORDERS_LAYER, cube, source, view


def _catalog(tmp_path, *cubes, views=(), connector="duckdb"):
    return source(tmp_path, *cubes, views=views, connector=connector).semantic_catalog()


def _by_name(elements):
    return {e.name: e for e in elements}


# --- columns ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "column"),
    [
        ("status", "status"),
        ("{CUBE}.status", "status"),
        ("{CUBE.raw_status}", "raw"),
        ("{raw_status}", "raw"),
        ("{TABLE}.status", "status"),
        ('{CUBE}."Status"', None),
        ("upper({CUBE}.status)", None),
        ("{customers.region}", None),
        ("{FILTER_PARAMS.orders.status.filter('x')}", None),
    ],
)
def test_a_member_carries_the_one_column_behind_it_or_none(tmp_path, sql, column):
    orders = cube(
        "orders",
        joins=[
            {
                "name": "customers",
                "sql": "{CUBE}.c = {customers}.id",
                "relationship": "many_to_one",
            }
        ],
        dimensions=[
            {"name": "raw_status", "sql": "{CUBE}.raw", "type": "string"},
            {"name": "probe", "sql": sql, "type": "string"},
        ],
    )
    customers = cube(
        "customers", dimensions=[{"name": "region", "sql": "region", "type": "string"}]
    )

    rows = _by_name(_catalog(tmp_path, orders, customers).dimensions)

    assert rows["orders.probe"].column == column


def test_a_sql_cube_traces_its_columns_through_the_projection(tmp_path):
    catalog = _catalog(tmp_path, *ORDERS_LAYER)

    customers = _by_name(catalog.semantic_models)["customers"]
    rows = _by_name(catalog.dimensions)
    assert customers.relation == "demo.main.customers"
    # `id` is `customer_key AS id` in the cube's query.
    assert rows["customers.id"].column == "customer_key"
    assert rows["customers.region"].column == "region"


def test_a_filtered_sql_cube_claims_no_relation_and_still_feeds_the_gate(tmp_path):
    filtered = cube(
        "recent",
        sql="SELECT * FROM demo.main.orders WHERE ordered_at > now() - interval 7 day",
        dimensions=[{"name": "status", "sql": "status", "type": "string"}],
    )

    catalog = _catalog(tmp_path, filtered)

    (model,) = catalog.semantic_models
    assert model.relation is None
    assert catalog.dimensions[0].column is None
    assert catalog.physical_columns["recent.status"] == ("demo.main.orders", "status")
    assert any("narrows its table (where)" in note for note in catalog.notes)


def test_a_joined_sql_cube_claims_nothing(tmp_path):
    joined = cube(
        "enriched",
        sql=(
            "SELECT o.id FROM demo.main.orders o "
            "JOIN demo.main.customers c ON o.c = c.id"
        ),
        dimensions=[{"name": "id", "sql": "id", "type": "number"}],
    )

    catalog = _catalog(tmp_path, joined)

    assert catalog.semantic_models[0].relation is None
    assert catalog.physical_columns == {}


def test_a_partial_sql_table_is_kept_for_the_gate_and_not_claimed(tmp_path):
    catalog = _catalog(
        tmp_path,
        cube(
            "orders",
            sql_table="main.orders",
            dimensions=[{"name": "s", "sql": "s", "type": "string"}],
        ),
    )

    assert catalog.semantic_models[0].relation is None
    assert catalog.physical_columns["orders.s"] == ("main.orders", "s")


def test_a_computed_member_is_screened_through_the_columns_it_reads(tmp_path):
    orders = cube(
        "orders",
        dimensions=[
            {"name": "email", "sql": "{CUBE}.email", "type": "string"},
            {"name": "contact_key", "sql": "upper({email})", "type": "string"},
        ],
    )

    catalog = _catalog(tmp_path, orders)

    assert catalog.computed_columns["orders.contact_key"] == (
        "demo.main.orders",
        "upper((email))",
    )


def test_snowflake_names_are_folded_the_way_snowflake_resolves_them(tmp_path):
    """An unquoted name is the one the warehouse folded it to, relation and
    column alike; reading the column as written made every Cube column look
    gone from a Snowflake table."""

    orders = cube(
        "orders",
        sql_table="analytics.public.orders",
        joins=[
            {
                "name": "customers",
                "sql": "{CUBE}.customer_id = {customers.id}",
                "relationship": "many_to_one",
            }
        ],
        dimensions=[
            {
                "name": "status",
                "sql": "{CUBE}.status",
                "type": "string",
                "primary_key": True,
            }
        ],
    )
    customers = cube(
        "customers",
        sql_table="analytics.public.customers",
        dimensions=[{"name": "id", "sql": "customer_key", "type": "number"}],
    )
    layer = source(tmp_path, orders, customers, connector="snowflake")

    catalog = layer.semantic_catalog()
    (declared,) = layer.declared_definitions().declared_relationships

    assert catalog.semantic_models[0].relation == "ANALYTICS.PUBLIC.ORDERS"
    assert _by_name(catalog.dimensions)["orders.status"].column == "STATUS"
    assert declared.column_pairs == [("CUSTOMER_ID", "CUSTOMER_KEY")]
    assert layer.semantic_layer().semantic_models[0].keys == [["STATUS"]]


# --- measures and metrics -----------------------------------------------------------


def test_measures_become_measures_and_metrics_with_their_kind(tmp_path):
    rolling = cube(
        "orders",
        dimensions=[{"name": "ordered_at", "sql": "ordered_at", "type": "time"}],
        measures=[
            {"name": "revenue", "sql": "amount", "type": "sum"},
            {"name": "orders", "type": "count"},
            {"name": "avg_amount", "sql": "amount", "type": "avg"},
            {"name": "aov", "sql": "{revenue} / {orders}", "type": "number"},
            {"name": "margin", "sql": "{revenue} - {cost}", "type": "number"},
            {"name": "cost", "sql": "cost", "type": "sum"},
            {"name": "net", "sql": "{margin} * 0.9", "type": "number"},
            {
                "name": "revenue_7d",
                "sql": "amount",
                "type": "sum",
                "rolling_window": {"trailing": "7 days"},
            },
        ],
    )

    catalog = _catalog(tmp_path, rolling)

    measures = _by_name(catalog.measures)
    metrics = _by_name(catalog.metrics)
    assert set(measures) == {"revenue", "orders", "avg_amount", "cost", "revenue_7d"}
    assert measures["avg_amount"].agg == "average"
    assert measures["revenue"].agg_time_dimension == "ordered_at"
    assert {name: m.type for name, m in metrics.items()} == {
        "orders.revenue": "simple",
        "orders.orders": "simple",
        "orders.avg_amount": "simple",
        "orders.aov": "ratio",
        "orders.margin": "derived",
        "orders.cost": "simple",
        "orders.net": "derived",
        "orders.revenue_7d": "cumulative",
    }
    aov = metrics["orders.aov"].composition
    assert (aov.numerator, aov.denominator) == ("orders.revenue", "orders.orders")
    assert metrics["orders.margin"].composition.input_metrics == [
        "orders.revenue",
        "orders.cost",
    ]
    # Followed through the calculated measure it reads, to what it is made of.
    assert metrics["orders.net"].input_measures == ["revenue", "cost"]
    assert metrics["orders.revenue"].queryable_granularities[:2] == ["second", "minute"]


def test_a_measure_filter_is_the_metric_filter(tmp_path):
    metric = _by_name(_catalog(tmp_path, *ORDERS_LAYER).metrics)["orders.revenue"]

    assert metric.filter == "{CUBE}.status != 'test'"


def test_a_metric_groups_by_its_cube_and_the_cubes_its_joins_reach(tmp_path):
    """Directed, as Cube's join graph is: customers declares no join back."""

    metrics = _by_name(_catalog(tmp_path, *ORDERS_LAYER).metrics)

    assert "customers.region" in metrics["orders.revenue"].dimensions
    assert "orders.status" not in metrics["customers.customer_count"].dimensions


def test_two_cubes_measure_names_do_not_collide(tmp_path):
    """Cube scopes a measure name to its cube; every cube may declare `count`."""

    a = cube(
        "a",
        dimensions=[{"name": "at", "sql": "at", "type": "time"}],
        measures=[{"name": "count", "type": "count"}],
    )
    b = cube(
        "b",
        dimensions=[{"name": "on", "sql": "on", "type": "time"}],
        measures=[{"name": "count", "type": "count"}],
    )
    layer = LocalCubeLayer(source(tmp_path, a, b))

    catalog = layer.list_definitions()
    narrowed, _ = catalog.narrowed_to(["b.count"])

    assert [(m.semantic_model, m.name) for m in narrowed.measures] == [("b", "count")]
    assert layer.time_axis_refs(["a.count"]) == ["a.at"]
    assert layer.time_axis_refs(["b.count"]) == ["b.on"]


def test_a_cube_with_two_time_dimensions_names_no_axis_and_says_why(tmp_path):
    two = cube(
        "orders",
        dimensions=[
            {"name": "ordered_at", "sql": "ordered_at", "type": "time"},
            {"name": "shipped_at", "sql": "shipped_at", "type": "time"},
        ],
        measures=[{"name": "n", "type": "count"}],
    )

    catalog = _catalog(tmp_path, two)

    assert catalog.semantic_models[0].agg_time_dimension is None
    assert catalog.metrics[0].time_axis is None
    assert any("2 time dimensions" in note for note in catalog.notes)


# --- views --------------------------------------------------------------------------


def test_a_view_exposes_its_cube_members_with_their_sources(tmp_path):
    sales = view(
        "sales",
        {
            "join_path": "orders",
            "includes": [
                "revenue",
                {"name": "status", "alias": "order_status", "title": "Order status"},
            ],
        },
        {
            "join_path": "orders.customers",
            "includes": "*",
            "excludes": ["customer_count"],
            "prefix": True,
        },
        title="Sales",
    )

    catalog = _catalog(tmp_path, *ORDERS_LAYER, views=[sales])

    model = _by_name(catalog.semantic_models)["sales"]
    rows = _by_name(r for r in catalog.dimensions if r.semantic_model == "sales")
    metrics = _by_name(m for m in catalog.metrics if m.name.startswith("sales."))
    assert (model.label, model.relation) == ("Sales", None)
    assert set(rows) == {
        "sales.order_status",
        "sales.customers_id",
        "sales.customers_region",
        "sales.customers_signed_up_at",
    }
    assert rows["sales.order_status"].label == "Order status"
    assert rows["sales.order_status"].vendor_params["source"] == "orders.status"
    assert rows["sales.order_status"].column is None
    assert catalog.physical_columns["sales.customers_region"] == (
        "demo.main.customers",
        "region",
    )
    assert set(metrics) == {"sales.revenue"}
    assert metrics["sales.revenue"].dimensions == list(rows)
    assert metrics["sales.revenue"].semantic_models == ["sales", "orders"]


# --- declarations -------------------------------------------------------------------


def test_a_one_to_many_join_is_written_child_to_parent(tmp_path):
    orders = cube(
        "orders",
        joins=[
            {
                "name": "items",
                "sql": "{CUBE}.id = {items}.order_id",
                "relationship": "has_many",
            }
        ],
    )

    (declared,) = (
        source(tmp_path, orders, cube("items"))
        .declared_definitions()
        .declared_relationships
    )

    assert (declared.model, declared.to_model) == ("items", "orders")
    assert declared.column_pairs == [("order_id", "id")]


def test_a_composite_join_keeps_every_pair_and_is_no_foreign_key(tmp_path):
    details = cube(
        "details",
        joins=[
            {
                "name": "items",
                "sql": (
                    "{CUBE}.order_id = {items}.order_id "
                    "AND {items}.line_no = {CUBE}.line_no"
                ),
                "relationship": "many_to_one",
            }
        ],
    )

    defs = source(tmp_path, details, cube("items")).declared_definitions()

    (declared,) = defs.declared_relationships
    assert declared.column_pairs == [("order_id", "order_id"), ("line_no", "line_no")]
    assert defs.foreign_keys == []


def test_a_range_join_is_declared_with_no_pairs_and_named(tmp_path):
    ranged = cube(
        "events",
        joins=[
            {
                "name": "sessions",
                "sql": (
                    "{CUBE}.at BETWEEN {sessions}.started_at AND {sessions}.ended_at"
                ),
                "relationship": "many_to_one",
            }
        ],
    )

    defs = source(tmp_path, ranged, cube("sessions")).declared_definitions()

    assert defs.declared_relationships == []
    assert any("not an equality" in note for note in defs.notes)


def test_a_primary_key_with_a_computed_member_is_left_out(tmp_path):
    keyed = cube(
        "orders",
        dimensions=[
            {"name": "id", "sql": "id", "type": "number", "primary_key": True},
            {
                "name": "k",
                "sql": "id || '-' || line",
                "type": "string",
                "primary_key": True,
            },
        ],
    )

    defs = source(tmp_path, keyed).declared_definitions()

    assert defs.declared_keys == [] and defs.declared_composite_keys == []


def test_what_has_no_home_is_named_in_the_notes(tmp_path):
    rich = cube(
        "orders",
        data_source="analytics",
        public=False,
        segments=[{"name": "paid", "sql": "{CUBE}.status = 'paid'"}],
        pre_aggregations=[{"name": "daily", "type": "rollup"}],
        dimensions=[{"name": "s", "sql": "s", "type": "string", "public": False}],
    )

    notes = _catalog(tmp_path, rich).notes

    assert any("segments, pre_aggregations" in n for n in notes)
    assert any("is not public" in n for n in notes)
    assert any("data source 'analytics'" in n for n in notes)
    assert any("s is not public" in n for n in notes)
