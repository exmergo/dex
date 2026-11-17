"""The rendering of a governed Metric View query, held to recorded goldens (#1362).

Pure: no connection. Each request in `fixtures/governed_goldens.yaml` is
translated against the repository's files and rendered against the deployed
view's name, and the statement has to equal the recorded one exactly and survive
a parse round trip through the pinned sqlglot in the Databricks dialect, which is
the dialect the governed spine proves it read-only in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")

import sqlglot

from exmergo_dex_core.guards.sql_guard import assert_select_only, referenced_relations
from exmergo_dex_core.metric_views.query import (
    QueryRefused,
    filter_fields,
    translate_query,
    translate_values,
)

from .conftest import FIXTURES, source
from .workspace_fake import ORDERS

GOLDENS = yaml.safe_load((FIXTURES / "governed_goldens.yaml").read_text("utf-8"))
DEPLOYED = "shop.metrics.orders"


@pytest.fixture
def loaded(tmp_path: Path):
    return source(tmp_path, connector="databricks", orders=ORDERS).loaded()


def _render(loaded, case: dict) -> str:
    if "values" in case:
        plan = translate_values(loaded, case["values"], case.get("metrics", []))
    else:
        plan = translate_query(
            loaded,
            metrics=case["metrics"],
            group_by=case.get("group_by", []),
            where=case.get("where", []),
        )
    return plan.render(
        DEPLOYED,
        order_by=case.get("order_by", []),
        limit=case.get("limit"),
        max_rows=50,
    ).sql


@pytest.mark.parametrize("case", GOLDENS, ids=[c["name"] for c in GOLDENS])
def test_the_rendering_is_the_recorded_one(loaded, case):
    assert _render(loaded, case) == case["sql"]


@pytest.mark.parametrize("case", GOLDENS, ids=[c["name"] for c in GOLDENS])
def test_the_rendering_round_trips_through_the_pinned_sqlglot(case):
    parsed = sqlglot.parse_one(case["sql"], read="databricks")

    assert parsed.sql(dialect="databricks") == case["sql"]
    assert assert_select_only(case["sql"], dialect="databricks")
    assert referenced_relations(case["sql"], dialect="databricks") == [DEPLOYED]


def test_tokens_match_ignoring_case_and_alias_as_written(loaded):
    plan = translate_query(
        loaded, metrics=["ORDERS.Revenue"], group_by=["orders.Order_Status"]
    )
    rendered = plan.render(DEPLOYED, max_rows=50)

    assert "`order_status` AS `orders.Order_Status`" in rendered.sql
    assert rendered.names == {
        "orders.order_status": "orders.Order_Status",
        "orders.revenue": "ORDERS.Revenue",
    }


def test_a_repeated_token_is_selected_once(loaded):
    plan = translate_query(
        loaded,
        metrics=["orders.revenue", "orders.REVENUE"],
        group_by=["orders.priority", "orders.priority"],
    )

    assert plan.render(DEPLOYED, max_rows=50).sql.count("MEASURE(") == 1
    assert len(plan.fields) == 1


def test_filter_fields_names_each_field_as_the_catalog_spells_it(loaded):
    read = filter_fields(loaded, ["ORDER_STATUS = 'F' AND orders.nation = 'FR'"])

    assert read is not None
    fields, predicate = read
    assert [f.canonical for f in fields] == ["orders.order_status", "orders.nation"]
    assert predicate == "(`order_status` = 'F' AND `nation` = 'FR')"


@pytest.mark.parametrize(
    "clause",
    [
        "order_status IN (SELECT 1)",
        "COUNT(order_status) > 1",
        "order_status = ?",
        "order_status = ${x}",
        "{{ Dimension('x') }} = 1",
        "order_status = 'a'; DROP TABLE x",
        "SUM(1) OVER () > 0",
        "1 = 1",
    ],
)
def test_a_clause_dex_cannot_screen_is_not_read(loaded, clause):
    assert filter_fields(loaded, [clause]) is None


def test_a_name_from_another_view_or_a_join_column_is_refused(loaded):
    with pytest.raises(QueryRefused, match="no such field"):
        filter_fields(loaded, ["customer.c_name = 'x'"])


def test_a_field_reads_only_the_fields_declared_before_it(tmp_path):
    """Databricks resolves a bare name in a field against the fields before it:
    `tag: CONCAT(tag, ...)` reads the source's `tag`, and so does a field
    naming one declared after it."""

    from exmergo_dex_core.metric_views.query import field_columns

    layer = source(
        tmp_path,
        connector="databricks",
        orders={
            **ORDERS,
            "fields": [
                {"name": "first", "expr": "CONCAT(later, '-')"},
                {"name": "tag", "expr": "CONCAT(tag, customer.c_mktsegment)"},
                {"name": "later", "expr": "o_orderpriority"},
                {"name": "after", "expr": "CONCAT(later, customer.c_mktsegment)"},
            ],
            "measures": [{"name": "order_count", "expr": "COUNT(1)"}],
        },
    )
    (orders,) = layer.loaded().views

    def read(name: str) -> list[str]:
        found = field_columns(orders, orders.member(name), layer.resolution(), str)
        return [column.token for column in found or []]

    assert read("tag") == ["orders.source.tag", "orders.customer.c_mktsegment"]
    assert read("first") == ["orders.source.later"]
    assert sorted(read("after")) == [
        "orders.customer.c_mktsegment",
        "orders.source.o_orderpriority",
    ]


@pytest.mark.parametrize(
    ("source_sql", "tables"),
    [
        # A CTE inside a subquery is not visible to the query around it.
        (
            "SELECT r.* FROM restricted_data r CROSS JOIN (WITH restricted_data AS "
            "(SELECT * FROM shop.core.orders) SELECT * FROM restricted_data) x",
            ["restricted_data", "shop.core.orders"],
        ),
        # A CTE sees those before it, and the body sees them all.
        (
            "WITH a AS (SELECT * FROM t1), b AS (SELECT * FROM a) "
            "SELECT * FROM b JOIN c ON b.k = c.k",
            ["c", "t1"],
        ),
        # ... and not one declared after it.
        (
            "WITH b AS (SELECT * FROM a), a AS (SELECT * FROM t1) SELECT * FROM b",
            ["a", "t1"],
        ),
        # An outer WITH is visible inside a subquery.
        ("WITH a AS (SELECT * FROM t1) SELECT * FROM (SELECT * FROM a) s", ["t1"]),
    ],
)
def test_a_cte_name_is_read_by_lexical_scope(source_sql, tables):
    from exmergo_dex_core.metric_views.lineage import source_tables

    assert sorted(source_tables(source_sql) or []) == sorted(tables)
