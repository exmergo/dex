"""Reading a model's output columns through its stars (#643)."""

from __future__ import annotations

import pytest

from exmergo_dex_core.transform.plans import select_columns
from exmergo_dex_core.transform.select_list import (
    RelationColumns,
    resolve_select_list,
)

pytest.importorskip("sqlglot")


@pytest.mark.parametrize(
    ("sql", "columns"),
    [
        ("select a, b as c from t", {"a", "c"}),
        (
            "with final as (select a, b from {{ ref('x') }}) select * from final",
            {"a", "b"},
        ),
        (
            "with s as (select a from {{ source('r', 't') }}), "
            "r as (select * from s), final as (select * from r) select * from final",
            {"a"},
        ),
        ("select * from (select a from t) q", {"a"}),
        (
            "with p as (select 1 as a), q as (select 2 as b) "
            "select * from p join q on true",
            {"a", "b"},
        ),
        (
            "with p as (select 1 as a), q as (select 2 as b) "
            "select p.*, q.b as c from p join q on true",
            {"a", "c"},
        ),
        (
            "with p as (select 1 as a), q as (select 2 as b) "
            "select * from p semi join q on true",
            {"a"},
        ),
        (
            "with p as (select 1 as a), q as (select 2 as b) "
            "select * from p anti join q on true",
            {"a"},
        ),
        ("select * exclude (a) from (select 1 as a, 2 as b)", {"b"}),
        ("select * replace (a + 1 as a) from (select 1 as a, 2 as b)", {"a", "b"}),
        ("select {{ m('x') }} as total from t", {"total"}),
        (
            "{{ config(materialized='table') }}\n"
            "with final as (select a from t) select * from final",
            {"a"},
        ),
        ("WITH Final AS (SELECT A FROM t) SELECT * FROM FINAL", {"a"}),
    ],
)
def test_the_columns_are_read_through_stars(sql, columns):
    resolved = resolve_select_list(sql)
    assert resolved.columns == columns, resolved.reason
    assert select_columns(sql) == columns


def test_bigquery_except_is_read_in_the_bigquery_dialect():
    sql = "select * except (a) from (select 1 as a, 2 as b)"
    assert resolve_select_list(sql, dialect="bigquery").columns == {"b"}


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("select * from {{ ref('x') }}", "ref('x')"),
        ("select * from {{ source('r', 't') }}", "source('r', 't')"),
        ("select * from raw_customers", "raw_customers"),
        ("select x.* from {{ ref('x') }} x", "ref('x')"),
        ("select {{ m() }} from t", "macro standing in for a column"),
        ("select * from {{ m() }}", "macro standing in for a relation"),
        ("select count(*) from t", "unaliased expression"),
        ("select max(a) from t", "unaliased expression"),
        ("select a from x union all select a from y", "set operation"),
        (
            "with u as (select a from x union all select a from y) select * from u",
            "set operation",
        ),
        ("select * rename (a as b) from (select 1 as a)", "RENAME"),
        ("select * from (select 1 as a) p cross join unnest([1])", "table function"),
        ("select * from (values (1)) v", "table function"),
        ("select z.* from (select 1 as a) p", "`z.*` naming nothing"),
        ("select from where", "did not parse"),
        ("{{ only_a_macro() }}", ""),
    ],
)
def test_what_cannot_be_read_says_which_case_it_was(sql, reason):
    resolved = resolve_select_list(sql)
    assert resolved.columns is None
    assert resolved.reason
    assert reason in resolved.reason


def test_a_recursive_cte_is_named_rather_than_followed_forever():
    sql = "with a as (select * from a) select * from a"
    resolved = resolve_select_list(sql)
    assert resolved.columns is None
    assert "recursive CTE" in resolved.reason


def test_a_cte_shadowing_a_relation_name_is_not_read_as_the_relation():
    sql = "with orders as (select a from {{ ref('orders') }}) select * from orders"
    asked = []

    def catalog(relation):
        asked.append(relation)
        return RelationColumns(reason="unused")

    assert resolve_select_list(sql, relation_columns=catalog).columns == {"a"}
    assert asked == []


def test_a_star_over_a_relation_asks_the_catalog_and_reports_what_it_read():
    def catalog(relation):
        assert relation.kind == "source"
        assert relation.parts == ("r", "t")
        return RelationColumns(["A", "b"], read_from=["main.t"])

    resolved = resolve_select_list(
        "select * from {{ source('r', 't') }}", relation_columns=catalog
    )
    assert resolved.columns == {"a", "b"}
    assert resolved.read_from == ["main.t"]


def test_the_catalogs_own_reason_is_the_answer():
    resolved = resolve_select_list(
        "select * from {{ ref('x') }}",
        relation_columns=lambda _relation: RelationColumns(reason="no profile of x"),
    )
    assert resolved.reason == "no profile of x"
