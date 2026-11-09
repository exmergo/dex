"""Column contract data type alias normalization and comparison tests (#1033)."""

from __future__ import annotations

import pytest

from exmergo_dex_core.dbt_project import (
    column_contract_divergence,
    types_equal,
)


@pytest.mark.parametrize(
    ("declared", "actual"),
    [
        ("int64", "INTEGER"),
        ("int", "INTEGER"),
        ("integer", "INTEGER"),
        ("bigint", "INTEGER"),
        ("smallint", "INTEGER"),
        ("tinyint", "INTEGER"),
        ("byteint", "INTEGER"),
        ("float64", "FLOAT"),
        ("float", "FLOAT"),
        ("bool", "BOOLEAN"),
        ("boolean", "BOOLEAN"),
        ("string", "STRING"),
        ("varchar", "STRING"),
        ("text", "STRING"),
        ("numeric", "NUMERIC"),
        ("decimal", "NUMERIC"),
        ("bignumeric", "BIGNUMERIC"),
        ("bigdecimal", "BIGNUMERIC"),
        ("timestamp", "TIMESTAMP"),
        ("datetime", "DATETIME"),
        ("date", "DATE"),
        ("time", "TIME"),
        ("bytes", "BYTES"),
        ("record", "STRUCT"),
        ("struct", "STRUCT"),
    ],
)
def test_bigquery_type_aliases_match(declared: str, actual: str):
    assert types_equal(declared, actual, connector="bigquery")
    assert types_equal(actual, declared, connector="bigquery")


def test_bigquery_real_mismatches_are_reported():
    assert not types_equal("string", "INTEGER", connector="bigquery")
    assert not types_equal("int64", "FLOAT", connector="bigquery")
    assert not types_equal("bool", "TIMESTAMP", connector="bigquery")


def test_bigquery_array_type_recursion():
    assert types_equal("ARRAY<INT64>", "ARRAY<INTEGER>", connector="bigquery")
    assert types_equal("ARRAY<string>", "ARRAY<STRING>", connector="bigquery")
    assert not types_equal("ARRAY<string>", "ARRAY<INTEGER>", connector="bigquery")


@pytest.mark.parametrize(
    ("declared", "actual"),
    [
        ("int", "INTEGER"),
        ("int4", "INTEGER"),
        ("signed", "INTEGER"),
        ("bigint", "BIGINT"),
        ("int8", "BIGINT"),
        ("long", "BIGINT"),
        ("smallint", "SMALLINT"),
        ("int2", "SMALLINT"),
        ("short", "SMALLINT"),
        ("tinyint", "TINYINT"),
        ("int1", "TINYINT"),
        ("hugeint", "HUGEINT"),
        ("varchar", "VARCHAR"),
        ("char", "VARCHAR"),
        ("bpchar", "VARCHAR"),
        ("text", "VARCHAR"),
        ("string", "VARCHAR"),
        ("boolean", "BOOLEAN"),
        ("bool", "BOOLEAN"),
        ("logical", "BOOLEAN"),
        ("float", "FLOAT"),
        ("float4", "FLOAT"),
        ("real", "FLOAT"),
        ("double", "DOUBLE"),
        ("float8", "DOUBLE"),
        ("decimal", "DECIMAL"),
        ("numeric", "DECIMAL"),
        ("datetime", "TIMESTAMP"),
        ("timestamptz", "TIMESTAMPTZ"),
        ("timestamp with time zone", "TIMESTAMPTZ"),
    ],
)
def test_duckdb_type_aliases_match(declared: str, actual: str):
    assert types_equal(declared, actual, connector="duckdb")
    assert types_equal(actual, declared, connector="duckdb")


def test_duckdb_real_mismatches_are_reported():
    assert not types_equal("int", "BIGINT", connector="duckdb")
    assert not types_equal("int", "VARCHAR", connector="duckdb")
    assert not types_equal("float", "DOUBLE", connector="duckdb")


@pytest.mark.parametrize(
    ("declared", "actual", "connector"),
    [
        ("int", "integer", "postgres"),
        ("int4", "integer", "postgres"),
        ("varchar", "character varying", "postgres"),
        ("character varying", "varchar", "postgres"),
        ("timestamptz", "timestamp with time zone", "postgres"),
        ("double precision", "float8", "postgres"),
        ("int", "NUMBER", "snowflake"),
        ("integer", "FIXED", "snowflake"),
        ("varchar", "TEXT", "snowflake"),
        ("string", "VARCHAR", "snowflake"),
        ("datetime", "TIMESTAMP_NTZ", "snowflake"),
        ("long", "BIGINT", "databricks"),
        ("tinyint", "INT8", "clickhouse"),
        ("real", "FLOAT32", "clickhouse"),
        ("double precision", "FLOAT64", "clickhouse"),
        ("char", "character", "postgres"),
        ("bpchar", "char", "postgres"),
        ("int", "INTEGER", "trino"),
    ],
)
def test_other_connector_aliases_match(declared: str, actual: str, connector: str):
    assert types_equal(declared, actual, connector=connector)


def test_distinct_connector_types_do_not_match():
    # ClickHouse distinct types
    assert not types_equal("DateTime", "DateTime64", connector="clickhouse")
    assert not types_equal("Date", "Date32", connector="clickhouse")
    assert types_equal("DateTime", "DateTime", connector="clickhouse")
    assert types_equal("DateTime64", "DateTime64", connector="clickhouse")

    # Databricks distinct timestamp types
    assert not types_equal("timestamp", "timestamp_ntz", connector="databricks")
    assert types_equal("timestamp", "timestamp", connector="databricks")
    assert types_equal("timestamp_ntz", "timestamp_ntz", connector="databricks")

    # Postgres fixed-width vs variable-width character types
    assert not types_equal("char", "varchar", connector="postgres")
    assert not types_equal("character", "varchar", connector="postgres")
    assert not types_equal("bpchar", "text", connector="postgres")
    assert types_equal("char", "character", connector="postgres")
    assert types_equal("char", "bpchar", connector="postgres")


def test_parameterized_types_matching_rules():
    # One side unconstrained matches parameterized base
    assert types_equal("varchar(10)", "VARCHAR", connector="duckdb")
    assert types_equal("VARCHAR", "varchar(10)", connector="duckdb")
    assert types_equal("decimal(10, 2)", "DECIMAL", connector="duckdb")

    # Both parameterized: match if whitespace-normalized parameters match
    assert types_equal("decimal(10, 2)", "DECIMAL(10,2)", connector="duckdb")
    assert types_equal("varchar( 255 )", "VARCHAR(255)", connector="duckdb")

    # Both parameterized: mismatch if parameters differ
    assert not types_equal("varchar(10)", "varchar(20)", connector="duckdb")
    assert not types_equal("decimal(10, 2)", "decimal(10, 4)", connector="duckdb")


def test_unnormalizable_type_fallback_does_not_silently_pass():
    # Case-insensitive exact match succeeds
    assert types_equal("GEOGRAPHY", "geography", connector="bigquery")

    # Differing unknown types fail
    assert not types_equal("GEOGRAPHY", "GEOMETRY", connector="bigquery")

    # Unknown type with differing parameter syntax fails
    assert not types_equal("CUSTOM(10)", "CUSTOM", connector="bigquery")


def test_column_contract_divergence_with_connector():
    declared = {
        "id": "int64",
        "active": "bool",
        "name": "string",
        "wrong": "string",
    }
    actual = {
        "id": "INTEGER",
        "active": "BOOLEAN",
        "name": "STRING",
        "wrong": "INTEGER",
    }
    missing, extra, mismatched = column_contract_divergence(
        declared, actual, connector="bigquery"
    )
    assert missing == []
    assert extra == []
    assert mismatched == [("wrong", "string", "INTEGER")]
