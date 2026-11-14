"""The converter's ``ExpressionDialectSuite``, ported (#1260): which Ossie
dialect a Databricks expression can carry on import."""

from __future__ import annotations

import pytest

from exmergo_dex_core.metric_views.ossie_bridge.dialect import classify

DATASETS = {"sales", "customer"}
VIEW_NAMES = {"p_region", "status"}

OSSIE = [
    "amount",
    "SUM(sales.amount)",
    "COUNT(customer.c_name)",
    "COUNT(*)",
    "COUNT(DISTINCT customer_id)",
    "SUM(CASE WHEN o_status = 'done' THEN amount ELSE 0 END)",
    "DATE_TRUNC('month', order_date)",
    "EXTRACT(YEAR FROM order_date)",
    "CAST(amount AS DECIMAL(10, 2))",
    "CAST(amount AS DOUBLE)",
    "CAST(amount AS STRING)",
    "COALESCE(a, b) + 1.5e3",
    "a IS NOT DISTINCT FROM b",
    "name ILIKE '%x%'",
    "x IN ('a', 'b') AND NOT y IS NULL",
    "amount % 2 != 0",
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount)",
    "DATEADD(day, 7, order_date)",
    "DATE '2024-01-15'",
    "TIMESTAMP_NTZ '2024-01-15 10:00:00'",
    "POSITION('a' IN s)",
    "SUBSTRING(s, 2, 3)",
    "CURRENT_DATE()",
    "a || b",
    "LOG(10, x)",
]

ANSI = [
    "CHAR_LENGTH(name)",
    "SUM(amount) FILTER (WHERE amount > 0)",
    "CORR(a, b)",
    "SUBSTRING(s FROM 2 FOR 3)",
    "TRIM(BOTH ' ' FROM s)",
    "s LIKE 'a!%' ESCAPE '!'",
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount NULLS LAST)",
]

DATABRICKS = [
    '"amount"',
    "'it''s'",
    "'a' 'b'",
    "'a\\'b'",
    "`my col`",
    "x::int",
    ":param * 2",
    "x <=> y",
    "x == y",
    "arr[0]",
    "t.*",
    "a.b.c",
    "transform(arr, x -> x + 1)",
    "name RLIKE 'a.*'",
    "INTERVAL 7 DAY",
    "1L + 2",
    "a -- comment",
    "MEASURE(revenue)",
    "schema.fn(x)",
    "SUM(x) total",
    "x IN (SELECT y FROM t)",
    "CURRENT_USER()",
    "localtime",
    "o_flags >> 3",
    "x << 2",
    "CHAR_LENGTH(s) > 0 AND d = CURRENT_DATE()",
    "CHAR_LENGTH(s) > 0 AND d = TIMESTAMP_NTZ '2024-01-15 10:00:00'",
    "UPPER(source.o_status)",
    "address.city",
    "amount * p_region",
    "UPPER(status)",
    "SUM(sales.status)",
    "customer.address.city",
    "UPPER(customer.nation.n_name)",
    "customer.fn(x)",
    "SUM(x) OVER (ORDER BY v)",
    "ROW_NUMBER() OVER (ORDER BY d NULLS LAST)",
    "SUM(x) OVER w",
    "DATEDIFF(day, start_date, end_date)",
    "DATEDIFF(end_date, start_date)",
    "LOG(x)",
    "COUNT(a, b)",
    "CEIL(x, 2)",
    "TRUNC(d, 'MM')",
    "POSITION('a', s)",
    "TRIM(s, 'x')",
    "TRIM(BOTH FROM s, 'x')",
    "REGEXP_EXTRACT(s, 'a')",
    "CAST(z AS VARCHAR(5))",
    "CAST(x AS FLOAT)",
    "CAST(x AS INT)",
    "CAST(x AS DECIMAL)",
    "CAST(d AS TIMESTAMP)",
    "TIMESTAMP '2024-01-01'",
    "",
    "(a",
    "a, b",
]


@pytest.mark.parametrize("expr", OSSIE)
def test_ossie_language_expressions_are_ossie_sql(expr):
    assert classify(expr, DATASETS, VIEW_NAMES, False) == "OSSIE_SQL_2026"


@pytest.mark.parametrize("expr", ANSI)
def test_standard_sql_outside_the_ossie_language_is_ansi_sql(expr):
    assert classify(expr, DATASETS, VIEW_NAMES, False) == "ANSI_SQL"


@pytest.mark.parametrize(
    ("expected", "expr"),
    [
        ("OSSIE_SQL_2026", "SUM(amount)"),
        (
            "OSSIE_SQL_2026",
            "SUM(sales.amount) / NULLIF(COUNT(DISTINCT customer_id), 0)",
        ),
        ("OSSIE_SQL_2026", "SUM(CASE WHEN (a > 1) THEN b END) * 2"),
        ("OSSIE_SQL_2026", "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY amount)"),
        ("ANSI_SQL", "SUM(amount) FILTER (WHERE amount > 0)"),
        ("DATABRICKS", "total_cost / row_count"),
        ("DATABRICKS", "running_revenue * 2"),
        ("DATABRICKS", "SUM(amount) + sales.amount"),
        ("DATABRICKS", "DATEADD(day, 1, MAX(order_date))"),
    ],
)
def test_a_measure_is_portable_only_when_every_name_sits_in_an_aggregate(
    expected, expr
):
    assert classify(expr, DATASETS, VIEW_NAMES, True) == expected


@pytest.mark.parametrize("expr", DATABRICKS)
def test_databricks_only_or_ambiguous_expressions_stay_databricks(expr):
    assert classify(expr, DATASETS, VIEW_NAMES, False) == "DATABRICKS"


@pytest.mark.parametrize("expr", [e for e in OSSIE + ANSI if e])
def test_every_portable_sample_parses_with_the_ossie_reader_sql_dialect(expr):
    """An expression labelled portable must also parse where dex's Ossie reader
    parses it, or the import would be refused as a syntax error it is not."""
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(expr)


@pytest.mark.parametrize("text", ["na\u00efve_col", "\u00fcn\u00efcode + 1"])
def test_a_bmp_letter_starts_an_identifier(text):
    assert classify(text, DATASETS, VIEW_NAMES, False) == "OSSIE_SQL_2026"


def test_an_astral_character_is_not_a_letter_as_in_java():
    assert classify("\U0001d465 + 1", DATASETS, VIEW_NAMES, False) == "DATABRICKS"
