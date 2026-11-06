"""A warehouse error keeps its words and loses the value it quotes (#1070).

Each connector's conversion, parse and range failures name the input they could
not handle, and that input is a row's value. These tests hold the mask to one
message per shape per connector (the value is a distinctive sentinel, so a leak
is unambiguous), to real DuckDB failures, and to the two places every surface
reaches it through: the adapters' ``warehouse_refusal`` and ``failure_text``.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.adapters.base import warehouse_refusal
from exmergo_dex_core.envelope import failure_text
from exmergo_dex_core.errors import WarehouseQueryError
from exmergo_dex_core.value_mask import VALUE_WITHHELD, mask_values

SENTINEL = "zq_sentinel_7"

# (connector, message, a fragment of the diagnosis that must survive)
SHAPES = [
    (
        "duckdb",
        f"Conversion Error: Could not convert string '{SENTINEL}' to INT32 "
        "when casting from source column account_id",
        "to INT32 when casting from source column account_id",
    ),
    (
        "duckdb",
        f'Conversion Error: invalid date field format: "{SENTINEL}", expected '
        "format is (YYYY-MM-DD)",
        "expected format is (YYYY-MM-DD)",
    ),
    (
        "duckdb",
        "Conversion Error: Type INT32 with value 1234567 can't be cast because "
        "the value is out of range for the destination type INT8",
        "destination type INT8",
    ),
    (
        "duckdb",
        "Invalid Input Error: Malformed JSON at byte 0 of input: unexpected "
        f'character.  Input: "{SENTINEL}"',
        "Malformed JSON at byte 0",
    ),
    (
        "postgres",
        f'invalid input syntax for type integer: "{SENTINEL}" [22P02]',
        "invalid input syntax for type integer:",
    ),
    (
        "postgres",
        f'value "{SENTINEL}" is out of range for type integer',
        "is out of range for type integer",
    ),
    (
        "postgres",
        f'invalid input value for enum mood: "{SENTINEL}"',
        "invalid input value for enum mood:",
    ),
    (
        "postgres",
        f'date/time field value out of range: "{SENTINEL}"',
        "date/time field value out of range:",
    ),
    (
        "redshift",
        f"Invalid digit, Value '{SENTINEL}', Pos 0, Type: Long",
        "Pos 0, Type: Long",
    ),
    (
        "bigquery",
        f"Bad int64 value: {SENTINEL}; error in writing field account_id",
        "error in writing field account_id",
    ),
    ("bigquery", f"Bad int64 value: '{SENTINEL}'", "Bad int64 value:"),
    (
        "bigquery",
        f'Could not cast literal "{SENTINEL}" to type DATE at [1:8]',
        "to type DATE at [1:8]",
    ),
    ("bigquery", f"Invalid date: '{SENTINEL}'", "Invalid date:"),
    ("bigquery", f"Invalid timestamp: '{SENTINEL}'", "Invalid timestamp:"),
    (
        "bigquery",
        f'Failed to parse input string "{SENTINEL}"',
        "Failed to parse input string",
    ),
    (
        "snowflake",
        f"Numeric value '{SENTINEL}' is not recognized",
        "is not recognized",
    ),
    (
        "snowflake",
        f"Can't parse '{SENTINEL}' as timestamp with format 'AUTO'",
        "as timestamp with format",
    ),
    (
        "databricks",
        f"[CAST_INVALID_INPUT] The value '{SENTINEL}' of the type \"STRING\" "
        'cannot be cast to "INT" because it is malformed. SQLSTATE: 22018',
        "SQLSTATE: 22018",
    ),
    (
        "clickhouse",
        f"Code: 6. DB::Exception: Cannot parse string '{SENTINEL}' as Int32: "
        "syntax error at begin of string.",
        "as Int32: syntax error at begin of string.",
    ),
    (
        "clickhouse",
        "Code: 27. DB::Exception: Cannot parse input: expected '\"' before: "
        f"'{SENTINEL}': (at row 1)",
        "(at row 1)",
    ),
    ("trino", f"Cannot cast '{SENTINEL}' to INT", "to INT"),
    ("trino", f"Cannot cast VARCHAR '{SENTINEL}' to INTEGER", "to INTEGER"),
    ("trino", f"Value cannot be cast to date: {SENTINEL}", "Value cannot be cast"),
    ("trino", f'Invalid format: "{SENTINEL}"', "Invalid format:"),
]


@pytest.mark.parametrize(
    ("connector", "message", "kept"),
    SHAPES,
    ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(SHAPES)],
)
def test_each_connector_s_conversion_error_loses_its_value(connector, message, kept):
    masked = mask_values(message)
    assert SENTINEL not in masked
    assert "1234567" not in masked
    assert VALUE_WITHHELD in masked
    assert kept in masked
    assert mask_values(masked) == masked  # idempotent


@pytest.mark.parametrize(
    "message",
    [
        'Binder Error: Referenced column "nope" not found in FROM clause!',
        "SQL compilation error: error line 1 at position 7 invalid identifier "
        "'\"id\"' [000904]",
        "Catalog Error: Table with name orders does not exist!",
        'relation "analytics.orders" does not exist',
        "Not found: Table proj:ds.orders was not found in location US",
        "",
    ],
)
def test_a_message_that_quotes_no_value_is_left_as_written(message):
    assert mask_values(message) == message


def test_a_value_with_an_inner_quote_is_masked_whole():
    """Warehouses print the value as stored: `'O'Brien'` is one value."""

    masked = mask_values("Could not convert string 'O'Brien' to INT32")
    assert masked == f"Could not convert string {VALUE_WITHHELD} to INT32"


def test_every_line_of_a_multi_line_message_is_masked():
    masked = mask_values(
        f"Conversion Error: Could not convert string '{SENTINEL}' to INT32\n"
        f'LINE 1: ... Input: "{SENTINEL}"'
    )
    assert SENTINEL not in masked


_DUCKDB_FAILURES = [
    f"select cast('{SENTINEL}' as integer)",
    f"select cast('{SENTINEL}' as date)",
    f"select cast('{SENTINEL}' as timestamp)",
    f"select cast('{SENTINEL}' as boolean)",
    f"select json_extract('{SENTINEL}', '$.a')",
    # A JSON value is printed unquoted, whole objects included (CodeRabbit).
    f"""select cast('{{"secret": "{SENTINEL}"}}'::json as integer)""",
    """select cast('{"secret": 1234567}'::json as double)""",
    f"""select cast('["{SENTINEL}"]'::json as integer)""",
    f"""select cast('"{SENTINEL}"'::json as integer)""",
    "select cast(1234567 as tinyint)",
    # No cast written at all: comparing a VARCHAR key to an INTEGER key casts
    # implicitly, and the failure quotes the key it could not convert.
    f"select * from (select '{SENTINEL}' as k) a join (select 1 as k) b on a.k = b.k",  # noqa: S608
]


@pytest.mark.parametrize("sql", _DUCKDB_FAILURES)
def test_a_real_duckdb_refusal_reaches_the_caller_masked(tmp_path, sql):
    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    path = tmp_path / "wh.duckdb"
    duckdb.connect(str(path)).close()
    adapter = DuckDBAdapter(path)
    try:
        with pytest.raises(WarehouseQueryError) as raised:
            adapter.run_query(sql, max_rows=5, timeout_seconds=30)
    finally:
        adapter.close()
    text = str(raised.value)
    assert SENTINEL not in text
    assert "1234567" not in text
    assert VALUE_WITHHELD in text


def test_the_value_is_masked_before_the_length_cap():
    """A cap that cut through the value would leave it without its closing
    quote, which is what the mask reads the value's end from."""

    message = "x" * 380 + f" Could not convert string '{SENTINEL}{'y' * 40}' to INT32"
    refusal = warehouse_refusal(message)
    assert SENTINEL not in refusal.detail
    assert refusal.detail.endswith("...")


def test_warehouse_refusal_keeps_its_code_and_stage():
    refusal = warehouse_refusal(
        f'invalid input syntax for type integer: "{SENTINEL}"', code="22P02"
    )
    assert refusal.detail == (
        f"invalid input syntax for type integer: {VALUE_WITHHELD} [22P02]"
    )


def test_failure_text_masks_a_driver_error_that_escaped_translation():
    exc = RuntimeError(f"Could not convert string '{SENTINEL}' to INT32")
    assert failure_text(exc) == (
        f"RuntimeError: Could not convert string {VALUE_WITHHELD} to INT32"
    )


def test_failure_text_detail_only_is_the_server_words():
    exc = WarehouseQueryError(f"Numeric value '{SENTINEL}' is not recognized")
    assert failure_text(exc, detail_only=True) == (
        f"Numeric value {VALUE_WITHHELD} is not recognized"
    )
    assert failure_text(exc).startswith("WarehouseQueryError: the warehouse refused")


def test_failure_text_still_redacts_secrets():
    exc = RuntimeError("could not connect: password=hunter2")
    assert "hunter2" not in failure_text(exc)


@pytest.mark.parametrize(
    "message",
    [
        "Parsing Error: The metric 'dnf_count' does not exist",
        "Binder Error: Cannot compare values of type VARCHAR and type INTEGER - "
        "an explicit cast is required on column 'k'",
    ],
)
def test_a_bare_parse_or_cast_word_does_not_mask_identifiers(message):
    """dbt's `Parsing Error` and a binder's cast hint quote names, not values."""

    assert mask_values(message) == message


def test_a_json_cast_keeps_the_column_it_was_casting_from(tmp_path):
    """The value is masked whole and the column clause after it is kept."""

    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    path = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE t AS SELECT ?::JSON AS payload", [f'{{"secret": "{SENTINEL}"}}']
    )
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        with pytest.raises(WarehouseQueryError) as raised:
            adapter.run_query(
                "select cast(payload as integer) from t",
                max_rows=5,
                timeout_seconds=30,
            )
    finally:
        adapter.close()
    detail = raised.value.detail
    assert SENTINEL not in detail
    assert detail.endswith(
        f"Failed to cast value to numerical: {VALUE_WITHHELD} "
        "when casting from source column payload"
    )
