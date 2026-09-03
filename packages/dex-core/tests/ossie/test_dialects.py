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


def test_the_partition_covers_the_schema_enum_and_never_overlaps():
    """The two halves are exhaustive over the enum and disjoint.

    A dialect in neither is one dex would silently ignore; a dialect in both is
    one it would parse and preserve at once. Read off the bundled schema rather
    than from a second hand-written list, so an upgrade that adds a dialect
    fails here rather than being quietly dropped.
    """

    import json

    from exmergo_dex_core.ossie.loader import schema_bytes

    declared = set(json.loads(schema_bytes())["$defs"]["Dialect"]["enum"])

    assert set(SQL_DIALECTS) | set(NON_SQL_DIALECTS) == declared
    assert not set(SQL_DIALECTS) & set(NON_SQL_DIALECTS)


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
    """Four of dex's seven connectors have no token in Ossie's enum.

    They fall to the portable dialect rather than to something close, because
    DuckDB is not Databricks and a wrong dialect is a worse answer than a
    general one.
    """

    for connector in ("duckdb", "postgres", "redshift", "clickhouse"):
        assert connector_dialect(connector) is None
        _, dialect, _ = select_expression(
            expression(SNOWFLAKE="b", ANSI_SQL="a"), connector
        )
        assert dialect == PORTABLE_DIALECT


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
