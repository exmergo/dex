"""The deployed-definition check and the rendered `SEMANTIC_VIEW` goldens (#1290).

The corpus under `fixtures/` holds, for each fixture, the file deployed
(`valid/`) and what `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returned for it on the
dogfood account (`readback/`). What Snowflake does on the way back (upper-cased
names, `access_modifier` written out, re-inferred `data_type`, empty `synonyms`
and `description`) must never read as a difference.

The goldens are the statements dex sends, compared exactly, each parsed with the
pinned sqlglot as the governed spine parses it: read-only, and reading only the
deployed view.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.semantic_views.deployed import (
    LocateRefused,
    compare,
    locate,
    read_deployed,
)
from exmergo_dex_core.semantic_views.loader import load_files
from exmergo_dex_core.semantic_views.query import (
    QueryRefused,
    render_query,
    render_values,
)

from .conftest import FIXTURES, SHOP, source

PAIRS = sorted(
    path.stem
    for path in (FIXTURES / "readback").glob("*.yaml")
    if (FIXTURES / "valid" / path.name).exists()
)


@pytest.mark.parametrize("stem", PAIRS)
def test_snowflake_s_read_back_normalization_is_no_difference(stem: str):
    deployed = load_files(FIXTURES, [f"valid/{stem}.yaml"]).views[0]
    back = load_files(FIXTURES, [f"readback/{stem}.yaml"]).views[0]
    members = [m for t in deployed.tables for m in t.members()] + list(deployed.metrics)

    assert compare(deployed, back, members, [t.name for t in deployed.tables]) == []


def test_a_changed_expression_is_a_difference_and_reformatting_is_not():
    import yaml

    text = (FIXTURES / "valid" / "private_members.yaml").read_text()
    mine = load_files(FIXTURES, ["valid/private_members.yaml"]).views[0]

    def read(body: str):
        return read_deployed(
            "private_members", "D.S.PRIVATE_MEMBERS", lambda _: body, mine
        )

    spaced = read(text.replace("SUM(", "sum(  "))
    assert spaced.view is not None
    members = [m for t in mine.tables for m in t.members()]
    assert compare(mine, spaced.view, members, []) == []

    doc = yaml.safe_load(text)
    doc["tables"][0]["metrics"][-1]["expr"] = "SUM(lineitem.extended_price)"
    changed = read(yaml.safe_dump(doc))
    differences = compare(mine, changed.view, members, [])
    assert [(d.field) for d in differences] == ["expr"]


def test_locate_matches_by_name_ignoring_case():
    assert locate(["A.B.SALES", "A.B.OTHER"], "sales") == "A.B.SALES"
    with pytest.raises(LocateRefused, match="defined only in the repository"):
        locate(["A.B.OTHER"], "sales")
    with pytest.raises(LocateRefused, match="more than one allowlisted schema"):
        locate(["A.B.SALES", "A.C.SALES"], "sales")


def test_a_refused_read_is_coverage_not_a_crash():
    def refuse(_):
        raise RuntimeError("Insufficient privileges to operate on semantic view")

    read = read_deployed("sales", "A.B.SALES", refuse, None)
    assert read.coverage.status == "refused"
    assert "Insufficient privileges" in read.coverage.reasons[0]


# --- goldens -------------------------------------------------------------------------

GOLDENS = {
    "metric_by_dimension": (
        {"metrics": ["shop.orders.revenue"], "group_by": ["shop.customers.region"]},
        'SELECT * FROM SEMANTIC_VIEW("DB"."SCH"."SHOP" DIMENSIONS customers.region '
        'AS "c0" METRICS orders.revenue AS "c1") LIMIT 51',
    ),
    "derived": (
        {
            "metrics": ["shop.orders_per_customer"],
            "group_by": ["shop.customers.region"],
        },
        'SELECT * FROM SEMANTIC_VIEW("DB"."SCH"."SHOP" DIMENSIONS customers.region '
        'AS "c0" METRICS orders_per_customer AS "c1") LIMIT 51',
    ),
    "grain_filter_order": (
        {
            "metrics": ["shop.orders.revenue"],
            "group_by": ["shop.orders.ordered_on__month"],
            "where": ["orders.status = 'paid'"],
            "order_by": ["-shop.orders.revenue"],
            "limit": 10,
        },
        'SELECT * FROM SEMANTIC_VIEW("DB"."SCH"."SHOP" DIMENSIONS '
        "DATE_TRUNC('month', orders.ordered_on) AS \"c0\" METRICS orders.revenue "
        'AS "c1" WHERE (orders.status = \'paid\')) ORDER BY "c1" DESC LIMIT 10',
    ),
    "facts_only": (
        {"metrics": [], "group_by": ["shop.orders.amount"]},
        'SELECT * FROM SEMANTIC_VIEW("DB"."SCH"."SHOP" FACTS orders.amount) LIMIT 51',
    ),
}


@pytest.fixture
def shop(tmp_path: Path):
    src = source(tmp_path, SHOP, connector="snowflake")
    return src.loaded(), src.resolution(), src.semantic_catalog()


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_the_rendered_statement_is_the_golden(shop, name: str):
    request, expected = GOLDENS[name]
    rendered = render_query(*shop, "DB.SCH.SHOP", max_rows=50, **request)

    assert rendered.sql == expected
    _parses_as_one_read_of_the_view(rendered.sql)


def test_the_values_statements_are_the_goldens(shop):
    plain = render_values(
        *shop, "DB.SCH.SHOP", "shop.customers.region", [], max_rows=50
    )
    scoped = render_values(
        *shop,
        "DB.SCH.SHOP",
        "shop.customers.region",
        ["shop.orders.revenue"],
        max_rows=50,
    )

    assert plain.sql == (
        'SELECT * FROM SEMANTIC_VIEW("DB"."SCH"."SHOP" DIMENSIONS customers.region '
        'AS "c0") ORDER BY "c0" LIMIT 51'
    )
    assert scoped.sql == (
        'SELECT "c0" FROM SEMANTIC_VIEW("DB"."SCH"."SHOP" DIMENSIONS '
        'customers.region AS "c0" METRICS orders.revenue AS "c1") ORDER BY "c0" '
        "LIMIT 51"
    )
    for sql in (plain.sql, scoped.sql):
        _parses_as_one_read_of_the_view(sql)


def test_an_order_by_outside_the_query_is_refused(shop):
    with pytest.raises(QueryRefused, match="not a member of this query"):
        render_query(
            *shop,
            "DB.SCH.SHOP",
            max_rows=50,
            metrics=["shop.orders.revenue"],
            group_by=[],
            order_by=["shop.customers.region"],
        )


def _parses_as_one_read_of_the_view(sql: str) -> None:
    from exmergo_dex_core.guards.sql_guard import (
        assert_select_only,
        referenced_relations,
    )

    assert_select_only(sql, dialect="snowflake")
    assert [r.upper() for r in referenced_relations(sql, dialect="snowflake")] == [
        "DB.SCH.SHOP"
    ]
