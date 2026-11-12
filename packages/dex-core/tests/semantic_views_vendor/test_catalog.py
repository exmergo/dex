"""How a Semantic View maps onto the catalog, the declarations and the
fingerprint (#1291)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.semantic_views.catalog import (
    definitions,
    resolution,
    semantic_catalog,
)
from exmergo_dex_core.semantic_views.dependency import SEMANTIC_VIEW_FORMAT_VERSION
from exmergo_dex_core.semantic_views.loader import load_files
from exmergo_dex_core.semantic_views.snapshot import semantic_layer

from .conftest import FIXTURES, SHOP, relationship, sv, table, write


def _read(root: Path, *views, connector: str = "duckdb"):
    names = [write(root, f"views/{v['name']}.yaml", v) for v in views]
    loaded = load_files(root, names)
    assert loaded.errors == [], loaded.notes()
    return loaded, resolution(loaded, connector)


@pytest.fixture
def shop(tmp_path: Path):
    """The `SHOP` view, read on DuckDB."""
    loaded, resolved = _read(tmp_path, SHOP)
    return loaded, resolved, semantic_catalog(loaded, resolved)


def test_every_name_is_qualified_by_its_view(shop) -> None:
    """A view is a namespace: `view.table` and `view.table.member`, whichever
    other views are configured."""
    _, _, catalog = shop

    assert {m.name for m in catalog.semantic_models} == {
        "shop.orders",
        "shop.customers",
        "shop.lines",
    }
    assert "shop.orders.status" in {d.name for d in catalog.dimensions}
    assert "shop.orders.revenue" in {m.name for m in catalog.metrics}
    assert "shop.orders_per_customer" in {m.name for m in catalog.metrics}


def test_a_logical_table_sits_on_its_base_table(shop) -> None:
    _, _, catalog = shop
    models = {m.name: m for m in catalog.semantic_models}

    assert models["shop.orders"].relation == "demo.main.orders"
    assert models["shop.orders"].description == "One row per placed order."
    assert models["shop.orders"].primary_entity == "shop.orders"
    assert models["shop.lines"].primary_entity is None  # a composite key


def test_entities_are_mapped_from_primary_keys_and_relationships(shop) -> None:
    """A primary entity per single-column key, foreign where it is referenced."""
    _, _, catalog = shop
    roles = {
        e.name: {(r.semantic_model, r.type, r.column) for r in e.roles}
        for e in catalog.entities
    }

    assert roles == {
        "shop.orders": {
            ("shop.orders", "primary", "order_id"),
            ("shop.lines", "foreign", "order_id"),
        },
        "shop.customers": {
            ("shop.customers", "primary", "customer_id"),
            ("shop.orders", "foreign", "customer_id"),
        },
    }


def test_a_fact_is_a_dimension_row_that_is_never_grouped(shop) -> None:
    _, _, catalog = shop
    rows = {d.name: d for d in catalog.dimensions}
    amount = rows["shop.orders.amount"]

    assert amount.vendor_params["kind"] == "fact"
    assert amount.vendor_params["groupable"] is False
    assert amount.column == "amount"
    assert rows["shop.orders.discount"].vendor_params["access"] == "private"
    for metric in catalog.metrics:
        assert "shop.orders.amount" not in metric.dimensions


def test_time_dimensions_are_time_with_no_grains(shop) -> None:
    _, _, catalog = shop
    ordered = next(d for d in catalog.dimensions if d.name == "shop.orders.ordered_on")

    assert ordered.type == "time"
    assert ordered.column == "placed_at"
    assert ordered.queryable_granularities is None


def test_a_member_reading_a_member_is_followed_to_its_column(shop) -> None:
    _, _, catalog = shop
    rows = {d.name: d for d in catalog.dimensions}

    assert rows["shop.orders.status_label"].column == "status"
    assert rows["shop.orders.size"].column is None
    relation, expression = catalog.computed_columns["shop.orders.size"]
    assert relation == "demo.main.orders"
    assert "orders." not in expression.lower() and "amount" in expression.lower()
    assert catalog.physical_columns["shop.orders.status"] == (
        "demo.main.orders",
        "status",
    )


def test_sample_values_are_counted_and_never_surfaced(shop) -> None:
    """Snowflake does not mask them, so their content never leaves the file."""
    _, _, catalog = shop
    status = next(d for d in catalog.dimensions if d.name == "shop.orders.status")

    assert status.vendor_params["sample_values"] == {"count": 2}
    assert "paid" not in json.dumps(
        catalog.to_data()
        if hasattr(catalog, "to_data")
        else [d.vendor_params for d in catalog.dimensions]
    )


def test_metrics_map_by_shape(shop) -> None:
    """One aggregate is a measure and a simple metric; one metric over another a
    ratio; anything else derived."""
    _, _, catalog = shop
    metrics = {m.name: m for m in catalog.metrics}

    assert metrics["shop.orders.revenue"].type == "simple"
    assert metrics["shop.orders.revenue"].vendor_params["synonyms"] == ["sales"]
    aov = metrics["shop.orders.aov"]
    assert aov.type == "ratio"
    assert (aov.composition.numerator, aov.composition.denominator) == (
        "shop.orders.revenue",
        "shop.orders.order_count",
    )
    per = metrics["shop.orders_per_customer"]
    assert per.type == "ratio"
    assert per.composition.denominator == "shop.customers.customer_count"
    doubled = metrics["shop.revenue_x2"]
    assert doubled.type == "derived"
    assert doubled.composition.input_metrics == ["shop.orders.revenue"]
    assert doubled.input_measures == ["revenue"]
    assert metrics["shop.orders.net"].vendor_params["access"] == "private"
    measures = {(m.semantic_model, m.name): m for m in catalog.measures}
    assert measures[("shop.orders", "revenue")].agg == "sum"
    assert measures[("shop.orders", "revenue")].column == "amount"
    assert measures[("shop.orders", "order_count")].agg == "count"


def test_a_metric_groups_by_its_table_and_the_tables_it_reaches(shop) -> None:
    """Many-to-one only: an order metric never groups by a line's dimension."""
    _, _, catalog = shop
    metrics = {m.name: m for m in catalog.metrics}

    assert set(metrics["shop.lines.units"].dimensions) == {
        "shop.orders.status",
        "shop.orders.status_label",
        "shop.orders.size",
        "shop.orders.ordered_on",
        "shop.customers.region",
        "shop.customers.email",
    }
    assert set(metrics["shop.customers.customer_count"].dimensions) == {
        "shop.customers.region",
        "shop.customers.email",
    }
    # A derived metric groups by what all of its inputs share.
    assert set(metrics["shop.orders_per_customer"].dimensions) == {
        "shop.customers.region",
        "shop.customers.email",
    }


def test_snowflake_folds_unquoted_names_to_upper_case(tmp_path: Path) -> None:
    """The column behind a member is spelled as the warehouse spells it."""
    loaded, resolved = _read(tmp_path, SHOP, connector="snowflake")
    catalog = semantic_catalog(loaded, resolved)
    models = {m.name: m for m in catalog.semantic_models}

    assert models["shop.orders"].relation == "DEMO.MAIN.ORDERS"
    assert catalog.physical_columns["shop.orders.status"] == (
        "DEMO.MAIN.ORDERS",
        "STATUS",
    )


def test_a_definition_query_stands_for_one_table_only(tmp_path: Path) -> None:
    """A plain projection names its table; a filtered one claims none, and says."""
    plain = table(
        "plain", "demo.main.unused", ["id"], dimensions=[{"name": "x", "expr": "x"}]
    )
    plain["base_table"] = {"definition": "SELECT id, x FROM demo.main.events"}
    filtered = table(
        "filtered", "demo.main.unused", ["id"], dimensions=[{"name": "y", "expr": "y"}]
    )
    filtered["base_table"] = {
        "definition": "SELECT id, y FROM demo.main.events WHERE y > 0"
    }
    loaded, resolved = _read(tmp_path, sv("defs", plain, filtered))
    catalog = semantic_catalog(loaded, resolved)
    models = {m.name: m.relation for m in catalog.semantic_models}

    assert models == {"defs.plain": "demo.main.events", "defs.filtered": None}
    assert any("a WHERE" in note for note in catalog.notes)


def test_what_the_catalog_cannot_model_is_named(tmp_path: Path) -> None:
    """Cortex-only keys, windows, non-additive dimensions, ASOF and range."""
    files = [
        "valid/cortex_metadata.yaml",
        "valid/window_metric.yaml",
        "valid/non_additive.yaml",
        "valid/asof_range.yaml",
    ]
    loaded = load_files(FIXTURES, files)
    notes = " ".join(semantic_catalog(loaded, resolution(loaded, "snowflake")).notes)

    for said in (
        "verified_queries",
        "custom_instructions",
        "window function",
        "NON ADDITIVE BY",
        "asof",
        "range",
        "filter(s)",
    ):
        assert said in notes, said


def test_declarations_carry_keys_and_joins(shop) -> None:
    loaded, resolved, _ = shop
    defs = definitions(loaded, resolved)

    assert {(k.model, k.column, k.primary) for k in defs.declared_keys} == {
        ("shop.orders", "order_id", True),
        ("shop.customers", "customer_id", True),
        ("shop.customers", "email", False),
    }
    (composite,) = defs.declared_composite_keys
    assert (composite.model, composite.columns, composite.claim) == (
        "shop.lines",
        ["order_id", "line_no"],
        "primary",
    )
    assert {
        (r.model, r.to_model, tuple(map(tuple, r.column_pairs)))
        for r in defs.declared_relationships
    } == {
        ("shop.orders", "shop.customers", (("customer_id", "customer_id"),)),
        ("shop.lines", "shop.orders", (("order_id", "order_id"),)),
    }
    assert defs.model_declarations["shop.orders"].grain == ["order_id"]
    assert defs.source_relations["shop.lines"] == "demo.main.order_lines"


def test_the_fingerprint_records_vendor_format_keys_and_arity(shop) -> None:
    loaded, resolved, _ = shop
    layer = semantic_layer(loaded, resolved)

    assert (layer.vendor, layer.format_version) == (
        "snowflake",
        SEMANTIC_VIEW_FORMAT_VERSION,
    )
    assert layer.relationship_cardinality_captured
    models = {m.name: m for m in layer.semantic_models}
    assert models["shop.lines"].keys == [["order_id", "line_no"]]
    assert models["shop.customers"].keys == [["customer_id"], ["email"]]
    assert models["shop.orders"].dimensions["size"] is None
    assert models["shop.orders"].measures == {
        "revenue": "amount",
        "order_count": "order_id",
        "net": None,
    }
    assert {r.name: r.cardinality for r in layer.relationships} == {
        "shop.orders_customers": "many_to_one",
        "shop.lines_orders": "many_to_one",
    }
    assert {m.name for m in layer.metrics} >= {"shop.orders.aov", "shop.revenue_x2"}


def test_arity_is_one_to_one_many_to_many_or_unstated(tmp_path: Path) -> None:
    """From the keys on each side; ASOF and range state none."""
    files = [
        "valid/unique_keys.yaml",
        "valid/many_to_many_relationship.yaml",
        "valid/asof_range.yaml",
    ]
    loaded = load_files(FIXTURES, files)
    layer = semantic_layer(loaded, resolution(loaded, "snowflake"))
    arity = {r.name: r.cardinality for r in layer.relationships}

    assert arity["unique_keys.notes_by_name"] == "many_to_one"
    assert arity["many_to_many_relationship.orders_customer"] == "many_to_many"
    assert arity["asof_range.latest_customer_order"] is None
    assert arity["asof_range.price_band"] is None
    assert arity["asof_range.lines_to_orders"] == "many_to_one"


def test_a_definition_change_moves_only_its_own_hash(tmp_path: Path) -> None:
    """Rewriting one metric changes that metric and its table, nothing else."""
    import copy

    before_loaded, before_resolved = _read(tmp_path / "a", SHOP)
    changed = copy.deepcopy(SHOP)
    changed["tables"][0]["metrics"][0]["expr"] = "SUM(orders.amount) * 1.0"
    after_loaded, after_resolved = _read(tmp_path / "b", changed)
    before = semantic_layer(before_loaded, before_resolved)
    after = semantic_layer(after_loaded, after_resolved)

    def hashes(layer):
        return {
            **{m.name: m.content_sha256 for m in layer.semantic_models},
            **{m.name: m.content_sha256 for m in layer.metrics},
        }

    moved = {k for k, v in hashes(after).items() if hashes(before)[k] != v}
    assert moved == {"shop.orders", "shop.orders.revenue"}


def test_a_count_over_two_columns_names_no_column(tmp_path: Path) -> None:
    loaded, resolved = _read(
        tmp_path,
        sv(
            "pairs",
            table(
                "orders",
                "demo.main.orders",
                ["order_id"],
                metrics=[{"name": "pairs", "expr": "COUNT(order_id, status)"}],
            ),
        ),
    )
    (measure,) = semantic_catalog(loaded, resolved).measures

    assert (measure.agg, measure.column) == ("count", None)


@pytest.mark.parametrize(
    ("definition", "said"),
    [
        ("SELECT COUNT(*) AS n FROM demo.main.events", "aggregate"),
        ("SELECT id AS customer_key FROM demo.main.events", "renames or computes"),
    ],
)
def test_a_definition_that_changes_rows_or_columns_claims_no_relation(
    tmp_path: Path, definition: str, said: str
) -> None:
    query = table("q", "demo.main.unused", ["customer_key"])
    query["base_table"] = {"definition": definition}
    query["dimensions"] = [{"name": "k", "expr": "customer_key"}]
    loaded, resolved = _read(tmp_path, sv("defs", query))
    catalog = semantic_catalog(loaded, resolved)

    assert catalog.semantic_models[0].relation is None
    assert any(said in note for note in catalog.notes)
    assert "defs.q" not in definitions(loaded, resolved).source_relations


def test_an_ambiguously_joined_table_is_not_groupable_without_a_path(
    tmp_path: Path,
) -> None:
    """Two relationships from lines to orders: a metric on lines groups by an
    order dimension only where `using_relationships` names one of them."""

    orders = table(
        "orders",
        "demo.main.orders",
        ["order_id"],
        dimensions=[{"name": "status", "expr": "status"}],
    )
    lines = table(
        "lines",
        "demo.main.order_lines",
        ["line_id"],
        metrics=[
            {"name": "n", "expr": "COUNT(line_id)"},
            {
                "name": "n_by_order",
                "expr": "COUNT(line_id)",
                "using_relationships": ["by_order"],
            },
        ],
    )
    view = sv(
        "twice",
        orders,
        lines,
        relationships=[
            relationship("by_order", "lines", "orders", ("order_id", "order_id")),
            relationship("by_return", "lines", "orders", ("return_id", "order_id")),
        ],
    )
    loaded, resolved = _read(tmp_path, view)
    metrics = {m.name: m for m in semantic_catalog(loaded, resolved).metrics}

    assert "twice.orders.status" not in metrics["twice.lines.n"].dimensions
    assert "twice.orders.status" in metrics["twice.lines.n_by_order"].dimensions


def test_an_enum_and_a_views_own_keys_are_fingerprinted(tmp_path: Path) -> None:
    """An enum's values are its allowed set, and a view's `variables` change
    what it answers; ordinary sample values move nothing."""
    import copy

    base = copy.deepcopy(SHOP)
    base["tables"][0]["dimensions"][0]["is_enum"] = True
    base["variables"] = [
        {"name": "region", "data_type": "VARCHAR", "default_value": "EU"}
    ]

    def prints(body, where):
        loaded, resolved = _read(tmp_path / where, body)
        layer = semantic_layer(loaded, resolved)
        return {m.name: m.content_sha256 for m in layer.semantic_models}

    before = prints(base, "a")
    enum = copy.deepcopy(base)
    enum["tables"][0]["dimensions"][0]["sample_values"] = ["paid", "void"]
    variables = copy.deepcopy(base)
    variables["variables"][0]["default_value"] = "US"
    sample = copy.deepcopy(SHOP)
    sample["tables"][0]["dimensions"][0]["sample_values"] = ["paid", "void"]

    assert prints(enum, "b")["shop.orders"] != before["shop.orders"]
    assert prints(variables, "c")["shop"] != before["shop"]
    assert prints(sample, "d")["shop.orders"] == prints(SHOP, "e")["shop.orders"]
