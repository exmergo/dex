"""What dex runs for each v1 connector, from what Cube Core rendered for it.

Cube renders and dex executes, so the statement that reaches the warehouse is
Cube's ``/v1/sql`` answer with its parameters written in as literals of dex's
dialect. One query with every clause dex sends (a grouped dimension, a bucketed
time dimension, an ``equals`` and an ``inDateRange`` filter, a measure filter,
an order and a limit), rendered by Cube Core v1.8.14 for each connector
(``recorded/<connector>.json``, made by ``record_cube.py``), bound by dex, and
compared exactly.

A golden changes only by hand, after someone re-records against a deliberately
bumped Cube release and reads the new output: a placeholder style, a cast, or a
time-zone idiom that moves is exactly what this is here to catch.
"""

# The goldens are Cube's statements byte for byte, long lines and the trailing
# space Cube leaves after its SELECT list included.
# ruff: noqa: E501, W291

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.adapters import get_dialect
from exmergo_dex_core.cube.query import CUBE_CONNECTORS, bind_params, check_plan
from exmergo_dex_core.guards.sql_guard import assert_select_only, referenced_relations

from .cube_cases import CONNECTORS
from .fake_cube import recorded

# Trailing whitespace inside the strings is Cube's own, kept byte for byte.
GOLDENS = {
    "duckdb": """SELECT "orders".status "orders__status", DATE_TRUNC('month', timezone('UTC', "orders".ordered_at::timestamptz)) "orders__ordered_at_month", sum("orders".total) "orders__revenue", count("orders".id) "orders__count" 
FROM  app.orders  AS "orders"
WHERE ("orders".ordered_at >= '2026-01-01T00:00:00.000Z'::timestamptz AND "orders".ordered_at <= '2026-03-31T23:59:59.999Z'::timestamptz) AND ("orders".status IN ('paid', 'shipped'))
GROUP BY 1, 2
HAVING (sum("orders".total) > '1')
ORDER BY  3  DESC
LIMIT 10""",
    "postgres": """SELECT "orders".status "orders__status", date_trunc('month', ("orders".ordered_at::timestamptz AT TIME ZONE 'UTC')) "orders__ordered_at_month", sum("orders".total) "orders__revenue", count("orders".id) "orders__count" 
FROM  app.orders  AS "orders"
WHERE ("orders".ordered_at >= '2026-01-01T00:00:00.000Z'::timestamptz AND "orders".ordered_at <= '2026-03-31T23:59:59.999Z'::timestamptz) AND ("orders".status IN ('paid', 'shipped'))
GROUP BY 1, 2
HAVING (sum("orders".total) > '1')
ORDER BY  3  DESC
LIMIT 10""",
    "bigquery": """SELECT `orders`.status `orders__status`, TIMESTAMP(DATETIME_TRUNC(TIMESTAMP(DATETIME(`orders`.ordered_at, 'UTC')), MONTH)) `orders__ordered_at_month`, sum(`orders`.total) `orders__revenue`, count(`orders`.id) `orders__count` 
FROM  app.orders  AS `orders`
WHERE (`orders`.ordered_at >= TIMESTAMP('2026-01-01T00:00:00.000000Z') AND `orders`.ordered_at <= TIMESTAMP('2026-03-31T23:59:59.999999Z')) AND (`orders`.status IN ('paid', 'shipped'))
GROUP BY 1, 2
HAVING (sum(`orders`.total) > CAST('1' AS FLOAT64))
ORDER BY  3  DESC
LIMIT 10""",
    "snowflake": """SELECT "orders".status "orders__status", date_trunc('MONTH', CONVERT_TIMEZONE('UTC', "orders".ordered_at::timestamp_tz)::timestamp_ntz) "orders__ordered_at_month", sum("orders".total) "orders__revenue", count("orders".id) "orders__count" 
FROM  app.orders  AS "orders"
WHERE ("orders".ordered_at >= '2026-01-01T00:00:00.000Z'::timestamp_tz AND "orders".ordered_at <= '2026-03-31T23:59:59.999Z'::timestamp_tz) AND ("orders".status IN ('paid', 'shipped'))
GROUP BY 1, 2
HAVING (sum("orders".total) > '1')
ORDER BY  3  DESC
LIMIT 10""",
    "redshift": """SELECT "orders".status "orders__status", date_trunc('month', ("orders".ordered_at::timestamptz AT TIME ZONE 'UTC')) "orders__ordered_at_month", sum("orders".total) "orders__revenue", count("orders".id) "orders__count" 
FROM  app.orders  AS "orders"
WHERE ("orders".ordered_at >= '2026-01-01T00:00:00.000Z'::timestamptz AND "orders".ordered_at <= '2026-03-31T23:59:59.999Z'::timestamptz) AND ("orders".status IN ('paid', 'shipped'))
GROUP BY 1, 2
HAVING (sum("orders".total) > '1')
ORDER BY  3  DESC
LIMIT 10""",
}


def bound(connector: str) -> str:
    plan = check_plan(recorded(connector)["golden"]["response"], data_source="default")
    return bind_params(plan.sql, plan.params, connector, get_dialect(connector))


def test_every_connector_cube_renders_for_has_a_golden():
    assert set(GOLDENS) == set(CUBE_CONNECTORS) == set(CONNECTORS)


@pytest.mark.parametrize("connector", sorted(GOLDENS))
def test_the_bound_statement_matches_its_golden(connector):
    assert bound(connector) == GOLDENS[connector]


@pytest.mark.parametrize("connector", sorted(GOLDENS))
def test_each_golden_passes_dex_s_guards_in_its_dialect(connector):
    dialect = get_dialect(connector)
    assert_select_only(GOLDENS[connector], dialect=dialect)
    assert referenced_relations(GOLDENS[connector], dialect=dialect) == ["app.orders"]


@pytest.mark.parametrize("connector", sorted(GOLDENS))
def test_a_golden_carries_no_placeholder_left_to_bind(connector):
    from exmergo_dex_core.cube.query import _placeholders

    assert _placeholders(GOLDENS[connector], get_dialect(connector)) == []
