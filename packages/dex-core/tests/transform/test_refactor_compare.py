"""Tests for schema-aware refactor comparison of two compiled SELECTs (#856)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from exmergo_dex_core.adapters.base import ColumnMeta, QueryResult
from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, PIIFlag
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.results import ConfirmationRequest
from exmergo_dex_core.transform.multiset_compare import (
    KeyedCounts,
    MultisetDiff,
    SchemaColumn,
    SchemaDiff,
    TypeMismatch,
    compare_refactors,
    compare_schemas,
)

_PII_VALUE = "secret-pii-value"


@pytest.fixture
def warehouse(tmp_path: Path):
    path = tmp_path / "compare.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("create table people (id integer, email varchar)")
    connection.execute(
        "insert into people values (1, 'ada@example.com'), (2, ?)",
        [_PII_VALUE],
    )
    connection.execute("create table unprofiled_events (id integer)")
    connection.execute("insert into unprofiled_events values (1)")
    connection.close()
    adapter = DuckDBAdapter(path)
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="main.people",
                columns=[
                    ColumnProfile(name="id", data_type="INTEGER"),
                    ColumnProfile(
                        name="email",
                        data_type="VARCHAR",
                        pii=PIIFlag(category="email", confidence=0.95),
                    ),
                ],
            )
        ]
    )
    try:
        yield adapter, cache
    finally:
        adapter.close()


class _RecordingAdapter:
    def __init__(self, inner: DuckDBAdapter):
        self._inner = inner
        self.ran: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def describe_query(self, sql: str):
        return self._inner.describe_query(sql)

    def run_query(self, sql: str, **kwargs):
        self.ran.append(sql)
        return self._inner.run_query(sql, **kwargs)


def _empty_schema() -> SchemaDiff:
    return SchemaDiff((), (), ())


def _compare(left: str, right: str, adapter, cache: DexCache, **kwargs):
    return compare_refactors(
        left,
        right,
        adapter=adapter,
        cache=cache,
        limits=QueryLimits(),
        command="transform plan",
        **kwargs,
    )


def _schemas(left: str, right: str, adapter, cache: DexCache, **kwargs):
    return compare_schemas(
        left,
        right,
        adapter=adapter,
        cache=cache,
        limits=QueryLimits(),
        command="transform plan",
        **kwargs,
    )


def test_duplicate_removal_is_not_equivalent(warehouse):
    adapter, cache = warehouse
    baseline = """
    select id, amount from (
        select 1 as id, 10 as amount
        union all
        select 1 as id, 10 as amount
    ) s
    """
    proposed = """
    select distinct id, amount from (
        select 1 as id, 10 as amount
        union all
        select 1 as id, 10 as amount
    ) s
    """
    result = _compare(baseline, proposed, adapter, cache)
    assert result.status == "different"
    assert result.reason_code == "rows_differ"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=1, only_in_left=1, only_in_right=0
    )
    assert result.keyed is None
    assert result.keyed_reason is None
    assert result.baseline_id is None
    assert result.proposed_id is None
    assert result.input_scope is None


def test_changed_measure_keeps_schema_and_row_count(warehouse):
    adapter, cache = warehouse
    baseline = "select 1 as id, 10 as amount"
    proposed = "select 1 as id, 11 as amount"
    result = _compare(
        baseline,
        proposed,
        adapter,
        cache,
        baseline_id="orders.baseline",
        proposed_id="orders.proposed",
        input_scope="dev.orders",
    )
    assert result.status == "different"
    assert result.reason_code == "rows_differ"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=1, only_in_right=1
    )
    assert result.keyed is None
    assert result.baseline_id == "orders.baseline"
    assert result.proposed_id == "orders.proposed"
    assert result.input_scope == "dev.orders"


def test_reordering_columns_and_rows_is_equivalent(warehouse):
    adapter, cache = warehouse
    baseline = "select 1 as id, 10 as amount union all select 2 as id, 20 as amount"
    proposed = "select 20 as amount, 2 as id union all select 10 as amount, 1 as id"
    result = _compare(baseline, proposed, adapter, cache)
    assert result.status == "equivalent"
    assert result.reason_code == "equivalent_on_compared_inputs"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=2, only_in_left=0, only_in_right=0
    )
    assert result.keyed is None


def test_renamed_output_matches_only_with_a_mapping(warehouse):
    adapter, cache = warehouse
    baseline = "select 1 as team_id, 10 as event_count"
    proposed = "select 1 as team, 10 as events"
    mapping = {"team_id": "team", "event_count": "events"}
    mapped = _compare(baseline, proposed, adapter, cache, column_mapping=mapping)
    assert _schemas(baseline, proposed, adapter, cache, column_mapping=mapping) == (
        _empty_schema()
    )
    assert mapped.status == "equivalent"
    assert mapped.schema == _empty_schema()
    assert mapped.multiset == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=0, only_in_right=0
    )

    unmapped = _compare(baseline, proposed, adapter, cache)
    assert unmapped.status == "different"
    assert unmapped.reason_code == "schemas_differ"
    assert unmapped.multiset is None
    assert unmapped.schema == SchemaDiff(
        only_in_left=(
            SchemaColumn("team_id", "INTEGER"),
            SchemaColumn("event_count", "INTEGER"),
        ),
        only_in_right=(
            SchemaColumn("team", "INTEGER"),
            SchemaColumn("events", "INTEGER"),
        ),
        type_mismatches=(),
    )


def test_non_executable_baseline_is_not_checked(warehouse):
    adapter, cache = warehouse
    result = _compare(
        "select * from missing_table",
        "select 1 as id",
        adapter,
        cache,
        baseline_id="baseline-build",
        proposed_id="proposed-build",
        input_scope="dev",
    )
    assert result.status == "not_checked"
    assert result.reason_code == "baseline_not_executable"
    assert result.detail == "WarehouseQueryError"
    assert result.multiset is None
    assert result.keyed is None
    assert result.schema is None
    assert "missing_table" not in repr(result)
    assert result.baseline_id == "baseline-build"
    assert result.proposed_id == "proposed-build"
    assert result.input_scope == "dev"
    seen = datetime.fromisoformat(result.compared_at)
    assert seen.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - seen) < timedelta(minutes=5)


def test_equivalent_refactor_is_limited_to_compared_inputs(warehouse):
    adapter, cache = warehouse
    baseline = """
    select t.team_id, coalesce(c.event_count, 0) as event_count
    from (select 10 as team_id union all select 11) t
    left join (select 10 as team_id, 4 as event_count) c
        on c.team_id = t.team_id
    """
    proposed = """
    with teams as (
        select 11 as team_id
        union all
        select 10 as team_id
    ),
    counts as (
        select 10 as team_id, 4 as event_count
    )
    select coalesce(counts.event_count, 0) as event_count, teams.team_id
    from teams
    left join counts on teams.team_id = counts.team_id
    """
    result = _compare(
        baseline,
        proposed,
        adapter,
        cache,
        baseline_id="team_events",
        proposed_id="team_events_cte",
        input_scope="in-memory fixture",
        key_columns=["team_id"],
    )
    assert result.status == "equivalent"
    assert result.reason_code == "equivalent_on_compared_inputs"
    assert result.baseline_id == "team_events"
    assert result.proposed_id == "team_events_cte"
    assert result.input_scope == "in-memory fixture"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=2, only_in_left=0, only_in_right=0
    )
    assert result.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=0)
    seen = datetime.fromisoformat(result.compared_at)
    assert seen.utcoffset() == timedelta(0)


def test_numeric_type_change_is_not_a_value_comparison(warehouse):
    adapter, cache = warehouse
    baseline = "select cast(1 as integer) as amount"
    proposed = "select cast(1 as bigint) as amount"
    assert _schemas(baseline, proposed, adapter, cache) == SchemaDiff(
        only_in_left=(),
        only_in_right=(),
        type_mismatches=(
            TypeMismatch(
                SchemaColumn("amount", "INTEGER"),
                SchemaColumn("amount", "BIGINT"),
            ),
        ),
    )
    result = _compare(baseline, proposed, adapter, cache)
    assert result.status == "different"
    assert result.reason_code == "schemas_differ"
    assert result.multiset is None
    assert result.keyed is None


def test_invalid_declared_key_is_not_checked(warehouse):
    adapter, cache = warehouse
    sql = "select 1 as id, 10 as amount"
    result = _compare(sql, sql, adapter, cache, key_columns=["missing"])
    assert result.status == "not_checked"
    assert result.reason_code == "invalid_key"
    assert result.schema == _empty_schema()
    assert result.multiset is None
    assert result.keyed is None
    assert result.detail == "declared key is not an output column: 'missing'"


def test_declared_key_counts_missing_added_and_changed(warehouse):
    adapter, cache = warehouse
    baseline = """
    select 1 as id, 10 as amount
    union all select 2, 5
    """
    proposed = """
    select 1 as id, 11 as amount
    union all select 3, 5
    """
    result = _compare(baseline, proposed, adapter, cache, key_columns=["id"])
    assert result.status == "different"
    assert result.reason_code == "rows_differ"
    assert result.schema == _empty_schema()
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=2, only_in_left=2, only_in_right=2
    )
    assert result.keyed == KeyedCounts(only_in_left=1, only_in_right=1, changed=1)


def test_null_key_matches_null_key(warehouse):
    adapter, cache = warehouse
    baseline = "select cast(null as integer) as id, 10 as amount"
    same = "select cast(null as integer) as id, 10 as amount"
    changed = "select cast(null as integer) as id, 11 as amount"
    matched = _compare(baseline, same, adapter, cache, key_columns=["id"])
    assert matched.status == "equivalent"
    assert matched.keyed == KeyedCounts(0, 0, 0)
    differ = _compare(baseline, changed, adapter, cache, key_columns=["id"])
    assert differ.status == "different"
    assert differ.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=1)


def test_non_unique_key_drops_only_the_keyed_verdict(warehouse):
    adapter, cache = warehouse
    baseline = "select 1 as id union all select 1 as id"
    proposed = "select 1 as id"
    result = _compare(baseline, proposed, adapter, cache, key_columns=["id"])
    assert result.status == "different"
    assert result.reason_code == "rows_differ"
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=1, only_in_left=1, only_in_right=0
    )
    assert result.keyed is None
    assert result.keyed_reason == "key_not_unique"


def test_duplicate_output_names_are_not_checked(warehouse):
    adapter, cache = warehouse
    sql = "select 1 as id, 2 as id"
    result = _compare(sql, sql, adapter, cache)
    assert result.status == "not_checked"
    assert result.reason_code == "duplicate_output_name"
    assert result.schema is None
    assert result.multiset is None
    assert result.detail == "duplicate output column name 'id' on baseline"
    with pytest.raises(ValueError, match="duplicate output column name"):
        _schemas(sql, sql, adapter, cache)


def test_empty_sql_is_refused(warehouse):
    adapter, cache = warehouse
    with pytest.raises(ValueError, match="required"):
        _compare("   ", "select 1 as id", adapter, cache)
    with pytest.raises(ValueError, match="required"):
        _schemas("select 1 as id", "  ;  ", adapter, cache)


def test_mapping_must_be_one_to_one(warehouse):
    adapter, cache = warehouse
    with pytest.raises(ValueError, match="one-to-one"):
        _compare(
            "select 1 as a, 2 as b",
            "select 1 as x, 2 as y",
            adapter,
            cache,
            column_mapping={"a": "x", "b": "x"},
        )
    with pytest.raises(ValueError, match="resolves two baseline"):
        _compare(
            "select 1 as a, 2 as b",
            "select 1 as a, 2 as b",
            adapter,
            cache,
            column_mapping={"b": "a"},
        )


def test_declared_key_follows_the_column_mapping(warehouse):
    adapter, cache = warehouse
    baseline = "select 1 as team_id, 10 as amount"
    proposed = "select 1 as team, 11 as amount"
    result = _compare(
        baseline,
        proposed,
        adapter,
        cache,
        column_mapping={"team_id": "team"},
        key_columns=["team_id"],
    )
    assert result.status == "different"
    assert result.reason_code == "rows_differ"
    assert result.keyed == KeyedCounts(only_in_left=0, only_in_right=0, changed=1)
    assert result.multiset == MultisetDiff(
        left_rows=1, right_rows=1, only_in_left=1, only_in_right=1
    )


def test_mapping_key_must_name_a_baseline_column(warehouse):
    adapter, cache = warehouse
    sql = "select 1 as id, 10 as amount"
    result = _compare(sql, sql, adapter, cache, column_mapping={"typo": "id"})
    assert result.status == "not_checked"
    assert result.reason_code == "invalid_mapping"
    assert result.multiset is None
    assert result.schema is None
    assert result.detail == (
        "column mapping names a baseline column that is not an output: 'typo'"
    )
    with pytest.raises(ValueError, match="not an output"):
        _schemas(sql, sql, adapter, cache, column_mapping={"typo": "id"})


def test_pii_column_still_compares_without_returning_values(warehouse):
    adapter, cache = warehouse
    sql = "select id, email from people"
    result = _compare(sql, sql, adapter, cache)
    assert result.status == "equivalent"
    assert result.reason_code == "equivalent_on_compared_inputs"
    assert result.multiset == MultisetDiff(
        left_rows=2, right_rows=2, only_in_left=0, only_in_right=0
    )
    assert _PII_VALUE not in repr(result)
    assert "ada@example.com" not in repr(result)


def test_unprofiled_relation_is_refused_before_run(warehouse):
    adapter, cache = warehouse
    spy = _RecordingAdapter(adapter)
    result = _compare(
        "select id from unprofiled_events",
        "select id from unprofiled_events",
        spy,
        cache,
    )
    assert result.status == "not_checked"
    assert result.reason_code == "firewall_refused"
    assert "unprofiled_events" in (result.detail or "")
    assert spy.ran == []


def test_non_select_baseline_is_refused_before_describe(warehouse):
    _adapter, cache = warehouse
    described: list[str] = []

    class _Guard:
        dialect = "duckdb"

        def describe_query(self, sql: str):
            described.append(sql)
            raise AssertionError("describe_query was called")

        def run_query(self, sql: str, **kwargs):
            raise AssertionError("run_query was called")

    result = _compare("delete from people", "select 1 as id", _Guard(), cache)
    assert result.status == "not_checked"
    assert result.reason_code == "baseline_refused"
    assert described == []
    assert "Delete" in (result.detail or "")


def test_describe_failure_leaks_no_warehouse_text(warehouse):
    adapter, cache = warehouse
    # DESCRIBE of a cast does not evaluate it on DuckDB 2. range() casts its
    # argument while planning, so the conversion is a describe failure and the
    # literal stays in the driver error, not on the result.
    result = _compare(
        "select * from range('secret@example.com')",
        "select 1 as n",
        adapter,
        cache,
    )
    assert result.status == "not_checked"
    assert result.reason_code == "baseline_not_executable"
    assert result.detail == "WarehouseQueryError"
    assert "secret" not in repr(result)


def test_execution_time_conversion_leaks_no_warehouse_text(warehouse):
    adapter, cache = warehouse
    result = _compare(
        "select cast('secret@example.com' as integer) as n",
        "select cast(1 as integer) as n",
        adapter,
        cache,
    )
    assert result.status == "not_checked"
    assert result.reason_code == "comparison_unreadable"
    assert result.detail == "WarehouseQueryError"
    assert "secret" not in repr(result)


def test_adapter_without_describe_query_does_not_run(warehouse):
    _adapter, cache = warehouse

    class _Bare:
        dialect = "duckdb"
        name = "bare"

        def run_query(self, sql: str, **kwargs):
            raise AssertionError("run_query was called")

    result = _compare("select 1 as id", "select 1 as id", _Bare(), cache)
    assert result.status == "not_checked"
    assert result.reason_code == "comparison_unsupported_on_connector"
    assert result.detail == "the bare connector does not describe query output"


def test_confirmation_request_stops_before_run(warehouse, monkeypatch):
    adapter, cache = warehouse
    spy = _RecordingAdapter(adapter)
    request = ConfirmationRequest()
    seen: dict[str, object] = {}

    def fake(command, adapter, estimate, **kwargs):
        seen["command"] = command
        seen["estimate"] = estimate
        seen["per_table"] = kwargs.get("per_table")
        return request

    monkeypatch.setattr("exmergo_dex_core.command_args.confirmation_request", fake)
    result = _compare(
        "select 1 as id, 10 as amount",
        "select 1 as id, 10 as amount",
        spy,
        cache,
        key_columns=["id"],
    )
    assert result.status == "not_checked"
    assert result.reason_code == "awaiting_confirmation"
    assert result.pending is request
    assert spy.ran == []
    assert seen["command"] == "transform plan"
    assert seen["estimate"] == 0.0
    assert seen["per_table"] == {
        "(refactor comparison)": 0.0,
        "(refactor key comparison)": 0.0,
    }


def test_unsupported_type_is_caught_before_run():
    # A warehouse reports a type it cannot group. The stub stands in for one.
    ran: list[str] = []

    class _Stub:
        dialect = "bigquery"

        def describe_query(self, sql: str):
            return [ColumnMeta("payload", "ARRAY<STRING>", True, 0)]

        def run_query(self, sql: str, **kwargs):
            ran.append(sql)
            raise AssertionError("run_query was called")

    result = _compare(
        "select 1 as payload",
        "select 1 as payload",
        _Stub(),
        DexCache(),
    )
    assert result.status == "not_checked"
    assert result.reason_code == "unsupported_type"
    assert result.detail == "payload (ARRAY<STRING>)"
    assert ran == []


def test_comparison_sql_is_exact_not_hashed(warehouse):
    adapter, cache = warehouse
    spy = _RecordingAdapter(adapter)
    result = _compare(
        "select 1 as id, 10 as amount",
        "select 1 as id, 10 as amount",
        spy,
        cache,
        key_columns=["id"],
    )
    assert result.status == "equivalent"
    assert spy.ran
    for sql in spy.ran:
        lowered = sql.lower()
        assert "hash(" not in lowered
        assert "struct_pack(" not in lowered
        assert "list_sort(" not in lowered
        assert "union all" in lowered
        assert "group by" in lowered


def test_a_folded_name_is_read_as_the_warehouse_spells_it():
    """Snowflake reports TEAM_ID for what its author wrote as team_id."""

    class _Folding:
        name = "folding"
        dialect = "duckdb"

        def describe_query(self, sql: str):
            return [
                ColumnMeta("TEAM_ID", "INT", True, 0),
                ColumnMeta("N", "INT", True, 1),
            ]

        def run_query(self, sql: str, **kwargs):
            return QueryResult(
                columns=["a", "b", "c", "d"],
                types=["int"] * 4,
                cells=[[1, 1, 0, 0]],
                truncated=False,
            )

    result = _compare(
        "select 1 as team_id, 2 as n",
        "select 1 as team_id, 2 as n",
        _Folding(),
        DexCache(),
        key_columns=["team_id"],
    )
    assert result.status == "equivalent", result.detail


def test_two_columns_differing_only_in_case_are_not_guessed_between():
    class _Both:
        name = "both"
        dialect = "duckdb"

        def describe_query(self, sql: str):
            return [ColumnMeta("Id", "INT", True, 0), ColumnMeta("ID", "INT", True, 1)]

    result = _compare(
        "select 1 as a", "select 1 as a", _Both(), DexCache(), key_columns=["id"]
    )
    assert result.reason_code == "invalid_key"
