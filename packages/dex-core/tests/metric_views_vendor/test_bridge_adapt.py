"""What dex adds around the ported converter (#1260): each proposal passes dex's
own validator for its target, and each change is a notice of origin ``dex``."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from exmergo_dex_core.metric_views.loader import dump_yaml
from exmergo_dex_core.metric_views.ossie_bridge import (
    metric_view_to_ossie,
    ossie_to_metric_view,
)
from exmergo_dex_core.metric_views.ossie_bridge.adapt import (
    export_document,
    import_view,
    is_key_only,
)
from exmergo_dex_core.metric_views.ossie_bridge.common import parse_yaml
from exmergo_dex_core.yaml_items import dump_items

from .conftest import FIXTURES

FIXTURE_NAMES = ["fixtureA", "fixtureB", "tpcds"]


def _view(fixture: str) -> dict[str, Any]:
    return parse_yaml(
        (FIXTURES / f"converter_{fixture}.yaml").read_text(encoding="utf-8")
    )


def _ossie(fixture: str) -> dict[str, Any]:
    return parse_yaml(
        (FIXTURES / f"converter_{fixture}_ossie.yaml").read_text(encoding="utf-8")
    )


def _dex_notices(conversion: Any) -> list[str]:
    return [n.render() for n in conversion.notices if n.origin == "dex"]


def _ossie_problems(document: dict[str, Any]) -> list[str]:
    pytest.importorskip("jsonschema")
    pytest.importorskip("sqlglot")
    from exmergo_dex_core.ossie.loader import validate_document_contents

    result = validate_document_contents({"m.ossie.yaml": dump_items(document)})
    return [d.render() for d in result.diagnostics if d.severity == "error"]


def _metric_view_problems(view: dict[str, Any], name: str = "v") -> list[str]:
    pytest.importorskip("sqlglot")
    from exmergo_dex_core.metric_views.authoring import refusals_for

    path = f"views/{name}.yaml"
    validated, refusals = refusals_for({path: dump_yaml(view)}, {path})
    problems = [d.render() for d in refusals]
    if validated.view(name) is None and not problems:
        problems.append("did not read back")
    return problems


# --- (a) key fields on import ---------------------------------------------------------


def _strip_key_only(model: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(model)
    for dataset in out["datasets"]:
        fields = [f for f in dataset.get("fields", []) if not is_key_only(f)]
        if fields:
            dataset["fields"] = fields
        else:
            dataset.pop("fields", None)
    return out


def test_the_import_fixture_through_dex_is_the_converters_plus_key_fields():
    converted = import_view(_view("fixtureB"))
    expected = _ossie("fixtureB")
    # The one metric reading a column a field now exposes names that field.
    expected["metrics"][1]["expression"]["dialects"][0]["expression"] = (
        "COUNT(DISTINCT lineitem.l_orderkey)"
    )
    assert _strip_key_only(converted.value) == expected
    added = {
        (ds["name"], f["name"])
        for ds in converted.value["datasets"]
        for f in ds.get("fields", [])
        if is_key_only(f)
    }
    assert added == {("lineitem", "l_orderkey"), ("orders", "o_orderkey")}
    key_field = next(
        f for f in converted.value["datasets"][1]["fields"] if f["name"] == "o_orderkey"
    )
    assert key_field["custom_extensions"] == [
        {"vendor_name": "DATABRICKS", "data": '{"_v": 1, "key_only": true}'}
    ]
    assert _dex_notices(converted)[-1] == (
        "[metric 'order_count'] names the fields exposing its columns, as an Ossie "
        "model metric names fields: 'l_orderkey' -> 'lineitem.l_orderkey'"
    )
    assert _dex_notices(converted)[:2] == [
        "[dataset 'lineitem'] added key_only fields 'l_orderkey', since an Ossie key "
        "or relationship names fields and no field exposed these columns; dex's "
        "export leaves them out of the Metric View",
        "[dataset 'orders'] added key_only fields 'o_orderkey', since an Ossie key or "
        "relationship names fields and no field exposed these columns; dex's export "
        "leaves them out of the Metric View",
    ]


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_every_imported_fixture_passes_dexs_ossie_validation(fixture):
    raw = metric_view_to_ossie(dump_yaml(_view(fixture))).value
    assert any("undeclared" in p for p in _ossie_problems(raw)), (
        "the converter's own output is expected to name columns, not fields"
    )
    assert _ossie_problems(import_view(_view(fixture)).value) == []


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_every_metric_view_round_trips_exactly_through_dex(fixture):
    view = _view(fixture)
    imported = import_view(view)
    back = export_document(parse_yaml(dump_items(imported.value)))
    assert back.value == view
    assert _dex_notices(back) == []


def test_a_field_that_exposes_the_key_column_is_named_instead_of_a_new_one():
    view = {
        "version": "1.1",
        "source": "c.s.orders",
        "joins": [
            {
                "name": "customer",
                "source": "c.s.customer",
                "on": "source.o_custkey = customer.c_custkey",
            }
        ],
        "dimensions": [
            {"name": "customer_key", "expr": "o_custkey"},
            {"name": "c_name", "expr": "customer.c_name"},
        ],
    }
    converted = import_view(view)
    (rel,) = converted.value["relationships"]
    assert rel["from_columns"] == ["customer_key"]
    assert rel["to_columns"] == ["c_custkey"]
    assert (
        "[relationship 'orders_to_customer'] names the fields exposing its columns, "
        "since an Ossie key or relationship names fields: 'orders.customer_key' for "
        "column 'o_custkey'" in _dex_notices(converted)
    )
    assert _ossie_problems(converted.value) == []
    assert export_document(converted.value).value == view


def test_an_added_key_field_takes_a_free_name():
    view = {
        "version": "1.1",
        "source": "c.s.orders",
        "joins": [{"name": "customer", "source": "c.s.customer", "using": ["k"]}],
        "dimensions": [{"name": "k", "expr": "UPPER(other)"}],
    }
    converted = import_view(view)
    orders = converted.value["datasets"][0]
    assert [f["name"] for f in orders["fields"]] == ["k", "k_key"]
    assert converted.value["relationships"][0]["from_columns"] == ["k_key"]
    assert _ossie_problems(converted.value) == []
    assert export_document(converted.value).value == view


def test_two_joins_the_converter_names_alike_are_told_apart():
    view = {
        "version": "1.1",
        "source": "c.s.a_to",
        "joins": [
            {"name": "b", "source": "c.s.b", "using": ["k"]},
            {
                "name": "a",
                "source": "c.s.a",
                "using": ["j"],
                "joins": [{"name": "to_b", "source": "c.s.to_b", "using": ["m"]}],
            },
        ],
        "measures": [{"name": "n", "expr": "COUNT(*)"}],
    }
    names = [r["name"] for r in import_view(view).value["relationships"]]
    assert names == ["a_to_to_b", "a_to_to_a", "a_to_to_b_2"]
    assert _ossie_problems(import_view(view).value) == []


# --- (b) fields to columns on export --------------------------------------------------


def test_a_relationship_naming_fields_reads_their_columns():
    document = {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "c.s.orders",
                "fields": [{"name": "customer_id", "expression": {"sql": "o_custkey"}}],
            },
            {
                "name": "customer",
                "source": "c.s.customer",
                "primary_key": ["id"],
                "fields": [{"name": "id", "expression": {"sql": "c_custkey"}}],
            },
        ],
        "relationships": [
            {
                "name": "oc",
                "from": "orders",
                "to": "customer",
                "from_columns": ["customer_id"],
                "to_columns": ["id"],
            }
        ],
    }
    converted = export_document(document)
    (join,) = converted.value["joins"]
    assert join["on"] == "source.o_custkey = customer.c_custkey"
    assert join["rely"] == {"at_most_one_match": True}
    assert (
        "[relationship 'oc'] fields of dataset 'orders' resolved to their columns: "
        "'customer_id' -> 'o_custkey'" in _dex_notices(converted)
    )
    assert _metric_view_problems(converted.value) == []


def test_a_model_metric_reads_the_columns_its_fields_name():
    document = {
        "version": "0.2.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "items",
                "source": "c.s.items",
                "fields": [
                    {"name": "qty", "expression": {"sql": "quantity"}},
                    {"name": "price", "expression": {"sql": "unit_price"}},
                    {"name": "order_ref", "expression": {"sql": "order_id"}},
                ],
            },
            {
                "name": "orders",
                "source": "c.s.orders",
                "primary_key": ["order_key"],
                "fields": [
                    {"name": "order_key", "expression": {"sql": "order_id"}},
                    {"name": "status_code", "expression": {"sql": "upper(status)"}},
                ],
            },
        ],
        "relationships": [
            {
                "name": "io",
                "from": "items",
                "to": "orders",
                "from_columns": ["order_ref"],
                "to_columns": ["order_key"],
            }
        ],
        "metrics": [
            {"name": "revenue", "expression": {"sql": "SUM(items.qty * items.price)"}},
            {
                "name": "orders_n",
                "expression": {"sql": "COUNT(DISTINCT orders.order_key)"},
            },
            {
                "name": "codes",
                "expression": {"sql": "COUNT(DISTINCT orders.status_code)"},
            },
            {"name": "label", "expression": {"sql": "MAX('items.qty')"}},
        ],
    }
    converted = export_document(document)
    measures = {m["name"]: m["expr"] for m in converted.value["measures"]}
    assert measures == {
        "revenue": "SUM(quantity * unit_price)",
        "orders_n": "COUNT(DISTINCT orders.order_id)",
        "codes": "COUNT(DISTINCT orders.status_code)",
        "label": "MAX('items.qty')",
    }
    notices = _dex_notices(converted)
    assert (
        "[metric 'revenue'] fields resolved to their columns: 'items.qty' -> "
        "'quantity', 'items.price' -> 'unit_price'" in notices
    )
    assert any(
        n.startswith("[metric 'codes'] reads 'orders.status_code'") for n in notices
    )
    assert _metric_view_problems(converted.value, "shop") == []


# --- (c) expression.sql ---------------------------------------------------------------


def test_expression_sql_is_read_where_the_converter_would_drop_the_field():
    document = {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [
            {
                "name": "t",
                "source": "c.s.t",
                "fields": [{"name": "region", "expression": {"sql": "region"}}],
            }
        ],
    }
    from exmergo_dex_core.metric_views.ossie_bridge import ConversionError

    with pytest.raises(ConversionError):
        ossie_to_metric_view(dump_items(document))
    converted = export_document(document)
    assert converted.value["dimensions"] == [{"name": "region", "expr": "region"}]
    assert _dex_notices(converted) == [
        "[model] read expression.sql as the OSSIE_SQL_2026 expression of 'region', "
        "which the converter would have dropped (it reads only expression.dialects)"
    ]


def test_expression_sql_is_authoritative_over_an_ossie_sql_entry():
    document = {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [
            {
                "name": "t",
                "source": "c.s.t",
                "fields": [
                    {
                        "name": "region",
                        "expression": {
                            "sql": "upper(region)",
                            "dialects": [
                                {"dialect": "OSSIE_SQL_2026", "expression": "region"}
                            ],
                        },
                    }
                ],
            }
        ],
    }
    assert export_document(document).value["dimensions"][0]["expr"] == "upper(region)"


def test_a_databricks_entry_still_wins_over_expression_sql():
    document = {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [
            {
                "name": "t",
                "source": "c.s.t",
                "fields": [
                    {
                        "name": "region",
                        "expression": {
                            "sql": "region",
                            "dialects": [
                                {"dialect": "DATABRICKS", "expression": "dbx"}
                            ],
                        },
                    }
                ],
            }
        ],
    }
    converted = export_document(document)
    assert converted.value["dimensions"][0]["expr"] == "dbx"
    assert _dex_notices(converted) == []


# --- (d) dataset-scoped metrics (#957) ------------------------------------------------


def _dataset_metric_document() -> dict[str, Any]:
    return {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "orders",
                "source": "c.s.orders",
                "fields": [
                    {"name": "amount", "expression": {"sql": "o_totalprice"}},
                    {"name": "status_code", "expression": {"sql": "upper(o_status)"}},
                    {"name": "customer_id", "expression": {"sql": "o_custkey"}},
                ],
                "metrics": [
                    {"name": "revenue", "expression": {"sql": "SUM(amount)"}},
                    {
                        "name": "codes",
                        "expression": {"sql": "COUNT(DISTINCT status_code)"},
                    },
                ],
            },
            {
                "name": "customer",
                "source": "c.s.customer",
                "primary_key": ["id"],
                "fields": [{"name": "id", "expression": {"sql": "c_custkey"}}],
                "metrics": [{"name": "customers", "expression": {"sql": "COUNT(id)"}}],
            },
        ],
        "relationships": [
            {
                "name": "oc",
                "from": "orders",
                "to": "customer",
                "from_columns": ["customer_id"],
                "to_columns": ["id"],
            }
        ],
        "metrics": [{"name": "orders_n", "expression": {"sql": "COUNT(*)"}}],
    }


def test_a_fact_dataset_metric_exports_as_a_measure_and_a_joined_one_is_named():
    converted = export_document(_dataset_metric_document())
    measures = {m["name"]: m["expr"] for m in converted.value["measures"]}
    assert measures == {"revenue": "SUM(o_totalprice)", "orders_n": "COUNT(*)"}
    notices = _dex_notices(converted)
    assert (
        "[metric 'revenue'] a metric of the fact dataset 'orders', exported as a "
        "measure" in notices
    )
    assert any(
        n.startswith("[metric 'codes'] reads field 'status_code'") for n in notices
    )
    assert any(
        n.startswith("[metric 'customers'] a metric of joined dataset 'customer'")
        for n in notices
    )
    assert _metric_view_problems(converted.value, "sales") == []


def test_an_imported_metric_names_the_fields_its_columns_are_exposed_by():
    view = {
        "version": "1.1",
        "source": "c.s.orders",
        "joins": [
            {
                "name": "customer",
                "source": "c.s.customer",
                "on": "source.o_custkey = customer.c_custkey",
            }
        ],
        "dimensions": [
            {"name": "amount", "expr": "o_totalprice"},
            {"name": "date", "expr": "o_orderdate"},
            {"name": "segment", "expr": "customer.c_mktsegment"},
        ],
        "measures": [
            {"name": "revenue", "expr": "SUM(o_totalprice)"},
            {"name": "segments", "expr": "COUNT(DISTINCT customer.c_mktsegment)"},
            {
                "name": "since",
                "expr": "MIN(o_orderdate) FILTER (WHERE status = 'o_totalprice')",
            },
            {"name": "per_segment", "expr": "revenue / segments"},
            {
                "name": "typed",
                "expr": "COUNT(IF(o_orderdate > DATE '2024-01-01', 1, NULL))",
            },
        ],
    }
    converted = import_view(view)
    exprs = {
        m["name"]: m["expression"]["dialects"][0]["expression"]
        for m in converted.value["metrics"]
    }
    assert exprs == {
        "revenue": "SUM(orders.amount)",
        "segments": "COUNT(DISTINCT customer.segment)",
        "since": "MIN(orders.date) FILTER (WHERE status = 'o_totalprice')",
        "per_segment": "revenue / segments",
        "typed": "COUNT(IF(orders.date > DATE '2024-01-01', 1, NULL))",
    }
    assert _ossie_problems(converted.value) == []
    assert export_document(converted.value).value == view


def test_dataset_metrics_on_import_are_opt_in():
    view = _view("fixtureA")
    default = import_view(view)
    assert "metrics" not in default.value["datasets"][0]
    scoped = import_view(view, dataset_metrics=True)
    orders = scoped.value["datasets"][0]
    assert [m["name"] for m in orders["metrics"]] == ["total_revenue", "order_count"]
    assert "metrics" not in scoped.value
    assert _ossie_problems(scoped.value) == []
    assert export_document(scoped.value).value == view


def test_a_measure_reading_a_joined_dataset_stays_model_scoped():
    view = {
        "version": "1.1",
        "source": "c.s.orders",
        "joins": [{"name": "customer", "source": "c.s.customer", "using": ["k"]}],
        "measures": [
            {"name": "local", "expr": "SUM(amount)"},
            {"name": "remote", "expr": "SUM(customer.balance)"},
            {"name": "ratio", "expr": "MEASURE(local) / COUNT(*)"},
        ],
    }
    scoped = import_view(view, dataset_metrics=True).value
    assert [m["name"] for m in scoped["datasets"][0]["metrics"]] == ["local"]
    assert [m["name"] for m in scoped["metrics"]] == ["remote", "ratio"]


def test_a_dex_export_of_every_ossie_fixture_passes_dexs_metric_view_rules():
    for fixture in FIXTURE_NAMES:
        converted = export_document(_ossie(fixture))
        assert _metric_view_problems(converted.value, fixture) == [], fixture


# --- review fixes ---------------------------------------------------------------------


def test_a_dataset_metric_reading_a_key_only_field_reads_its_column():
    """The key-only field is resolved before it is left out of the export."""
    view = {
        "version": "1.1",
        "source": "c.s.orders",
        "joins": [{"name": "customer", "source": "c.s.customer", "using": ["k"]}],
        "dimensions": [{"name": "k", "expr": "UPPER(other)"}],
        "measures": [{"name": "keys", "expr": "COUNT(k)"}],
    }
    scoped = import_view(view, dataset_metrics=True).value
    orders = scoped["datasets"][0]
    assert orders["metrics"][0]["expression"]["dialects"][0]["expression"] == (
        "COUNT(k_key)"
    )
    assert export_document(scoped).value["measures"] == [
        {"name": "keys", "expr": "COUNT(k)"}
    ]


def _chained_fields() -> dict[str, Any]:
    return {
        "version": "0.2.0.dev0",
        "name": "m",
        "datasets": [
            {
                "name": "orders",
                "source": "c.s.orders",
                "fields": [
                    {"name": "a", "expression": {"sql": "b"}},
                    {"name": "b", "expression": {"sql": "c"}},
                    {"name": "year", "expression": {"sql": "sale_year"}},
                ],
                "metrics": [
                    {"name": "chained", "expression": {"sql": "SUM(a)"}},
                    {
                        "name": "latest",
                        "expression": {"sql": "MAX(EXTRACT(YEAR FROM created))"},
                    },
                    {
                        "name": "later",
                        "expression": {"sql": "MAX(DATEADD(year, 1, d))"},
                    },
                ],
            }
        ],
        "metrics": [{"name": "model_chained", "expression": {"sql": "SUM(orders.a)"}}],
    }


def test_a_column_written_in_is_never_read_again_as_a_field():
    measures = {
        m["name"]: m["expr"]
        for m in export_document(_chained_fields()).value["measures"]
    }
    assert measures["chained"] == "SUM(b)"
    assert measures["model_chained"] == "SUM(b)"


def test_a_date_part_is_never_read_as_a_field_of_that_name():
    measures = {
        m["name"]: m["expr"]
        for m in export_document(_chained_fields()).value["measures"]
    }
    assert measures["latest"] == "MAX(EXTRACT(YEAR FROM created))"
    assert measures["later"] == "MAX(DATEADD(year, 1, d))"
