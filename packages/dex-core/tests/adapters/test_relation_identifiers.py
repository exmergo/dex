"""The pure relation acceptor: what counts as one relation, per connector.

It exists because a caller has to decide whether an authored string names a
relation *before* a connection exists, and the answer differs per connector. It
is deliberately not the adapters' own `_split`: that one splits an identifier
dex already resolved from the warehouse catalog, so it checks arity and nothing
else, and handed `select x from a.b.c` it reports success.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.adapters import (
    _DIALECTS,
    _RELATION_RULES,
    normalize_relation,
    parse_relation,
)

THREE_PART = ("duckdb", "bigquery", "snowflake", "databricks", "postgres", "redshift")


def test_every_shipped_connector_has_a_rule():
    """A connector missing a rule would raise from inside a project format
    reading a document, several frames from anything that names it."""

    assert set(_RELATION_RULES) == set(_DIALECTS)


def test_an_unknown_connector_raises_rather_than_guessing():
    """Matching `get_dialect`, which raises for the same reason: a silent
    fallback means every later decision is made against the wrong rules."""

    with pytest.raises(ValueError, match="unknown connector"):
        parse_relation("nope", "a.b.c")


@pytest.mark.parametrize("connector", THREE_PART)
def test_a_fully_qualified_unquoted_name_is_accepted(connector):
    assert parse_relation(connector, "db.schema.table") == ("db", "schema", "table")


def test_clickhouse_relations_are_two_parts(connector="clickhouse"):
    """The one connector whose relations are not three parts: it has no catalog
    level, and dbt-clickhouse's `schema:` *is* the ClickHouse database."""

    assert parse_relation(connector, "db.table") == ("db", "table")
    assert parse_relation(connector, "db.schema.table") is None


@pytest.mark.parametrize(
    "rejected",
    [
        "SELECT * FROM demo.main.orders",
        "demo.main.orders WHERE x = 1",
        "select x from a.b.c",
        '"demo"."main"."orders"',
        "`demo`.`main`.`orders`",
        "demo.main.orders, demo.main.customers",
        "demo. main.orders",
        "demo..orders",
        "demo.main.",
        ".main.orders",
        "orders",
        "main.orders",
        "demo.main.orders.extra",
        "",
        "demo.main.(orders)",
    ],
)
def test_anything_that_is_not_one_relation_is_rejected(rejected):
    """Conservative on purpose: every rejection here is a physical link a
    caller then does not claim, and a query accepted as a relation would reach
    the PII gate as false physical evidence."""

    assert parse_relation("duckdb", rejected) is None


def test_a_quoted_part_is_rejected_rather_than_unwrapped():
    """An unquoted `Orders` and a quoted `"Orders"` name the same relation on
    DuckDB and different relations on Snowflake, and dex has no way to tell
    which the author meant. So the non-link is correct rather than cautious."""

    assert parse_relation("snowflake", 'db.schema."Orders"') is None
    assert parse_relation("duckdb", 'db.schema."Orders"') is None


def test_a_hyphenated_bigquery_project_is_accepted():
    """Hyphens are legal in a BigQuery project id and dex's own inventory
    reports them, so rejecting one would leave every hyphenated project
    unlinkable."""

    assert parse_relation("bigquery", "my-proj.ds.tbl") == ("my-proj", "ds", "tbl")


@pytest.mark.parametrize(
    "rejected",
    ["-lead.ds.tbl", "trail-.ds.tbl", "proj.my-ds.tbl", "a - b.c.d"],
)
def test_the_hyphen_allowance_is_narrow(rejected):
    """Only the project id, and never a leading or trailing hyphen or the ` - `
    of a subtraction."""

    assert parse_relation("bigquery", rejected) is None


def test_a_hyphen_is_not_accepted_where_no_connector_allows_one():
    assert parse_relation("duckdb", "a-b.c.d") is None


@pytest.mark.parametrize(
    ("connector", "expected"),
    [
        ("snowflake", "DB.SCHEMA.ORDERS"),
        ("postgres", "db.schema.orders"),
        ("redshift", "db.schema.orders"),
        ("databricks", "db.schema.orders"),
        ("duckdb", "Db.Schema.Orders"),
        ("bigquery", "Db.Schema.Orders"),
    ],
)
def test_normalization_folds_the_way_the_connector_resolves(connector, expected):
    """So an authored `Main.Orders` matches the `main.orders` the cache and
    `explore inventory` carry."""

    assert normalize_relation(connector, "Db.Schema.Orders") == expected


def test_normalization_declines_exactly_what_the_acceptor_declines():
    assert normalize_relation("duckdb", "SELECT 1") is None


def test_a_non_string_is_declined_rather_than_raising():
    """Reached from a tier-1 read, which may not raise, so it is handed
    whatever a malformed document contained."""

    assert parse_relation("duckdb", None) is None
    assert parse_relation("duckdb", 42) is None


def test_the_acceptor_opens_nothing_and_imports_no_client():
    """Kept beside `_DIALECTS` for the reason that module states: resolving
    this must never import a client library."""

    import subprocess
    import sys

    probe = (
        "import sys;"
        "from exmergo_dex_core.adapters import parse_relation;"
        "parse_relation('duckdb', 'a.b.c');"
        "print(sorted(m for m in sys.modules if m in "
        "{'duckdb','google.cloud.bigquery','snowflake','psycopg',"
        "'clickhouse_connect'}))"
    )
    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "[]"
