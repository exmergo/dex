"""Cube's parameters, written into dex's dialect as literals (#913)."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.adapters import get_dialect
from exmergo_dex_core.cube.query import CubeQueryError, CubeRefusalError, bind_params


def bind(sql: str, params: list, connector: str = "duckdb") -> str:
    return bind_params(sql, params, connector, get_dialect(connector))


def test_a_placeholder_inside_quoted_text_or_a_comment_is_not_one():
    sql = (
        'SELECT \'?\' "a?", x FROM t AS "t" -- what?\n'
        "WHERE x = ? /* $1 ? */ AND y LIKE '%' || ? || '%'"
    )
    assert bind(sql, ["a", "b"]) == (
        'SELECT \'?\' "a?", x FROM t AS "t" -- what?\n'
        "WHERE x = 'a' /* $1 ? */ AND y LIKE '%' || 'b' || '%'"
    )


def test_numbered_placeholders_bind_by_number_and_may_repeat():
    sql = 'SELECT 1 FROM t AS "t" WHERE a = $2 AND b = $1 AND c = $2'
    assert bind(sql, ["x", "y"], "postgres") == (
        "SELECT 1 FROM t AS \"t\" WHERE a = 'y' AND b = 'x' AND c = 'y'"
    )


@pytest.mark.parametrize(
    ("connector", "literal"),
    [
        ("duckdb", "'it''s \\ x'"),
        ("postgres", "'it''s \\ x'"),
        ("bigquery", "'it\\'s \\\\ x'"),
        ("snowflake", "'it\\'s \\\\ x'"),
        ("redshift", "'it\\'s \\\\ x'"),
    ],
)
def test_a_value_is_quoted_by_its_dialect_s_own_rules(connector, literal):
    quote = "`" if connector == "bigquery" else '"'
    mark = "$1" if connector in {"postgres", "redshift"} else "?"
    sql = f"SELECT 1 FROM t AS {quote}t{quote} WHERE a = {mark}"  # noqa: S608
    assert bind(sql, ["it's \\ x"], connector).endswith(f"a = {literal}")


def test_a_hostile_value_stays_one_literal():
    bound = bind('SELECT 1 FROM t AS "t" WHERE a = ?', ["x'); DROP TABLE t; --"])
    assert bound == "SELECT 1 FROM t AS \"t\" WHERE a = 'x''); DROP TABLE t; --'"


def test_scalars_other_than_strings():
    assert bind(
        'SELECT 1 FROM t AS "t" WHERE a = ? AND b = ? AND c = ?', [1, True, None]
    ) == ('SELECT 1 FROM t AS "t" WHERE a = 1 AND b = TRUE AND c = NULL')


@pytest.mark.parametrize(
    ("sql", "params", "connector"),
    [
        ('SELECT 1 FROM t AS "t" WHERE a = ?', [], "duckdb"),
        ('SELECT 1 FROM t AS "t" WHERE a = ?', ["x", "y"], "duckdb"),
        ('SELECT 1 FROM t AS "t" WHERE a = $1', ["x", "y"], "postgres"),
        ('SELECT 1 FROM t AS "t" WHERE a = $3', ["x"], "postgres"),
    ],
)
def test_a_count_that_does_not_match_is_refused(sql, params, connector):
    with pytest.raises(CubeQueryError, match="will not guess"):
        bind(sql, params, connector)


@pytest.mark.parametrize(
    ("sql", "connector"),
    [
        ('SELECT 1 FROM t AS "t" WHERE a = $1', "duckdb"),
        ('SELECT 1 FROM t AS "t" WHERE a = ?', "postgres"),
        ("SELECT 1 FROM t AS `t` WHERE a = ?", "snowflake"),
        ('SELECT 1 FROM t AS "t" WHERE a = ?', "bigquery"),
    ],
)
def test_sql_rendered_for_another_warehouse_is_refused(sql, connector):
    with pytest.raises(CubeRefusalError, match=f"CUBEJS_DB_TYPE={connector}"):
        bind(sql, ["x"], connector)


def test_a_value_dex_cannot_write_is_refused():
    with pytest.raises(CubeQueryError, match="dict"):
        bind('SELECT 1 FROM t AS "t" WHERE a = ?', [{"x": 1}])
