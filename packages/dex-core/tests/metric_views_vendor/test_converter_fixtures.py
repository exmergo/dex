"""The ported mapping, held to the converter's own fixtures.

The `databricks` spoke of apache/ossie-converters renders each of these Metric
Views as an Ossie document. dex ports that construct table, so its catalog has to
read each construct the way the Ossie rendering states it: the fact source as the
view's relation, each join as a relationship with its key columns, a `rely` join
as a unique key on the joined dataset, `display_name` as the label, `comment` as
the description, `synonyms` carried, and the Metric-View-only keys the converter
stashes in `custom_extensions[DATABRICKS]` (`filter`, `format`, `partition`)
carried rather than dropped.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
from exmergo_dex_core.semantic_source import SemanticSourceContext

from .conftest import FIXTURES


def _layer(tmp_path: Path, fixture: str, connector: str = "databricks"):
    """The converter fixture ``fixture`` as the one configured file."""
    target = tmp_path / "views" / f"{fixture}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / f"converter_{fixture}.yaml", target)
    return MetricViewSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(tmp_path),
            connector=connector,
            options={"files": [f"views/{fixture}.yaml"]},
        )
    )


@pytest.mark.parametrize("fixture", ["fixtureA", "fixtureB", "tpcds"])
def test_every_converter_fixture_reads_with_no_error(tmp_path, fixture):
    """Each reads as one view, with no refusal."""
    layer = _layer(tmp_path, fixture)

    assert layer.loaded().errors == []
    assert len(layer.loaded().views) == 1


def test_fixture_a_star_schema(tmp_path):
    """Ossie: datasets `orders` (fact) and `customer`, relationship
    `orders_to_customer` on o_custkey -> c_custkey; label and synonyms on
    o_orderdate; synonyms on total_revenue."""

    layer = _layer(tmp_path, "fixtureA")
    catalog = layer.semantic_catalog()
    declared = layer.declared_definitions()

    (model,) = catalog.semantic_models
    assert model.relation == "samples.tpch.orders"
    assert model.description == "Sales orders with customer attributes"
    (join,) = declared.declared_relationships
    assert (join.relation, join.to_relation, join.column_pairs) == (
        "samples.tpch.orders",
        "samples.tpch.customer",
        [("o_custkey", "c_custkey")],
    )
    dimensions = {d.definition: d for d in catalog.dimensions}
    assert dimensions["o_orderdate"].label == "Order Date"
    assert dimensions["o_orderdate"].vendor_params["synonyms"] == ["order date", "date"]
    assert dimensions["o_orderkey"].description == "Order identifier"
    assert catalog.physical_columns["fixtureA.c_name"] == (
        "samples.tpch.customer",
        "c_name",
    )
    metrics = {m.name: m for m in catalog.metrics}
    assert metrics["fixtureA.total_revenue"].vendor_params["synonyms"] == [
        "revenue",
        "total revenue",
        "sales",
    ]
    measures = {m.name: m for m in catalog.measures}
    assert measures["order_count"].agg == "count"
    assert measures["total_revenue"].column == "o_totalprice"


def test_fixture_b_carries_what_the_converter_stashes(tmp_path):
    """Ossie: `unique_keys: [[o_orderkey]]` recovered on `orders` from `rely`;
    the model `filter`, the dimension and measure `format`, and the measure
    `partition` live in the DATABRICKS stash."""

    layer = _layer(tmp_path, "fixtureB")
    catalog = layer.semantic_catalog()
    declared = layer.declared_definitions()

    assert [(k.relation, k.column, k.unique) for k in declared.declared_keys] == [
        ("samples.tpch.orders", "o_orderkey", True)
    ]
    metrics = {m.name: m for m in catalog.metrics}
    assert metrics["fixtureB.revenue"].filter == "l_returnflag = 'N'"
    assert metrics["fixtureB.revenue"].vendor_params["format"]["currency_code"] == "USD"
    order_count = metrics["fixtureB.order_count"]
    assert order_count.vendor_params["partition"] == {
        "include": ["line_number"],
        "outer_aggregate": "SUM",
    }
    assert order_count.type == "derived"
    line = {d.definition: d for d in catalog.dimensions}["line_number"]
    assert line.vendor_params["format"]["type"] == "number"
    assert any("older spelling of `fields`" in n for n in catalog.notes)


def test_tpcds_three_dimension_tables(tmp_path):
    """Ossie: three dimension datasets, each keyed by its join column through
    `rely`; a stashed `filter`; labels and synonyms."""

    layer = _layer(tmp_path, "tpcds")
    declared = layer.declared_definitions()
    catalog = layer.semantic_catalog()

    assert sorted((k.relation, k.column) for k in declared.declared_keys) == [
        ("tpcds.public.customer", "c_customer_sk"),
        ("tpcds.public.date_dim", "d_date_sk"),
        ("tpcds.public.item", "i_item_sk"),
    ]
    assert {r.to_relation for r in declared.declared_relationships} == {
        "tpcds.public.customer",
        "tpcds.public.date_dim",
        "tpcds.public.item",
    }
    year = {d.definition: d for d in catalog.dimensions}["sold_year"]
    assert (year.label, year.vendor_params["synonyms"]) == ("Year", ["year", "yr"])
    assert all(m.filter == "ss_net_profit > 0" for m in catalog.metrics)
