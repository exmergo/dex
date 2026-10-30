"""Dialect selection: deterministic, and never a non-SQL language."""

from __future__ import annotations

import pytest

from exmergo_dex_core.ossie.dialects import (
    NON_SQL_DIALECTS,
    PORTABLE_DIALECT,
    SQL_DIALECTS,
    connector_dialect,
    select_expression,
    sqlglot_dialect,
)

from .conftest import expression


def test_dialect_is_an_open_string_and_the_partition_never_overlaps():
    """`Dialect` is any non-empty string upstream, so there is no enum to cover.

    What dex reads is the closed SQL half; everything else, registered or not,
    is opaque. The two halves are disjoint, since a dialect in both is one dex
    would parse and preserve at once.
    """

    import json

    from exmergo_dex_core.ossie.loader import schema_bytes

    dialect = json.loads(schema_bytes())["$defs"]["Dialect"]

    assert dialect["type"] == "string"
    assert "enum" not in dialect
    assert "OSSIE_SQL_2026" in SQL_DIALECTS
    assert not set(SQL_DIALECTS) & set(NON_SQL_DIALECTS)


def test_ossie_sql_2026_is_the_portable_dialect_and_top_level_sql_is_it():
    chosen, dialect, declared = select_expression(
        {"sql": "a", "dialects": [{"dialect": "OSSIE_SQL_2026", "expression": "b"}]},
        "duckdb",
    )

    # Upstream: the top-level `sql` is authoritative over an OSSIE_SQL_2026 entry.
    assert (chosen, dialect) == ("a", "OSSIE_SQL_2026")
    assert declared == {"OSSIE_SQL_2026": "a"}
    assert PORTABLE_DIALECT == "OSSIE_SQL_2026"


def test_the_connector_dialect_still_outranks_the_portable_form():
    chosen, dialect, _ = select_expression(
        {"sql": "a", "dialects": [{"dialect": "SNOWFLAKE", "expression": "s"}]},
        "snowflake",
    )

    assert (chosen, dialect) == ("s", "SNOWFLAKE")


def test_ossie_sql_2026_outranks_ansi_sql():
    chosen, dialect, _ = select_expression(
        expression(ANSI_SQL="a", OSSIE_SQL_2026="o"), "duckdb"
    )

    assert (chosen, dialect) == ("o", "OSSIE_SQL_2026")


def test_an_unregistered_token_is_preserved_and_never_read():
    chosen, dialect, declared = select_expression(expression(MY_ENGINE="x"), "duckdb")

    assert (chosen, dialect) == (None, None)
    assert declared == {"MY_ENGINE": "x"}


def test_every_sql_dialect_maps_to_the_parser_and_no_other_does():
    for name in SQL_DIALECTS:
        sqlglot_dialect(name)
    for name in NON_SQL_DIALECTS:
        with pytest.raises(KeyError):
            sqlglot_dialect(name)


def test_the_active_connector_dialect_wins():
    chosen, dialect, _ = select_expression(
        expression(ANSI_SQL="a", SNOWFLAKE="b"), "snowflake"
    )

    assert (chosen, dialect) == ("b", "SNOWFLAKE")


def test_a_connector_with_no_ossie_token_falls_to_the_portable_dialect():
    """Five of dex's eight connectors have no token in Ossie's enum.

    They fall to the portable dialect rather than to something close, because
    DuckDB is not Databricks and a wrong dialect is a worse answer than a
    general one.
    """

    for connector in ("duckdb", "postgres", "redshift", "clickhouse", "trino"):
        assert connector_dialect(connector) is None
        _, dialect, _ = select_expression(
            expression(SNOWFLAKE="b", ANSI_SQL="a"), connector
        )
        assert dialect == "ANSI_SQL"


def test_the_first_declared_sql_dialect_is_the_last_resort():
    chosen, dialect, _ = select_expression(
        expression(BIGQUERY="b", DATABRICKS="d"), "duckdb"
    )

    assert (chosen, dialect) == ("b", "BIGQUERY")


def test_selection_never_falls_through_to_a_non_sql_language():
    """The rule the whole partition exists for.

    A fallback that could select MDX hands a physical-column reader a string it
    cannot read, and MAQL is worse: a bare token there looks like a column
    identifier and means something else.
    """

    chosen, dialect, declared = select_expression(
        expression(MDX="[a].[b]", MAQL="SELECT x"), "duckdb"
    )

    assert chosen is None and dialect is None
    assert declared == {"MDX": "[a].[b]", "MAQL": "SELECT x"}


def test_every_declared_dialect_is_returned_even_when_unselected():
    """Nothing is dropped: the document is the source of truth."""

    _, _, declared = select_expression(
        expression(ANSI_SQL="a", SNOWFLAKE="b", MDX="c"), "duckdb"
    )

    assert declared == {"ANSI_SQL": "a", "SNOWFLAKE": "b", "MDX": "c"}


def test_selection_is_deterministic_for_one_document_and_connector():
    """A catalog that varied between runs could not be cached or compared."""

    declared = expression(BIGQUERY="b", ANSI_SQL="a", SNOWFLAKE="s")

    assert {select_expression(declared, "snowflake")[1] for _ in range(20)} == {
        "SNOWFLAKE"
    }


@pytest.mark.parametrize("malformed", [None, "text", 42, {"dialects": "x"}, {}])
def test_a_malformed_expression_yields_nothing_rather_than_raising(malformed):
    """Tier 1 may not raise, so this is reached with unvalidated input."""

    assert select_expression(malformed, "duckdb") == (None, None, {})


def test_a_repeated_dialect_keeps_the_first():
    _, _, declared = select_expression(
        {
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": "first"},
                {"dialect": "ANSI_SQL", "expression": "second"},
            ]
        },
        "duckdb",
    )

    assert declared == {"ANSI_SQL": "first"}
