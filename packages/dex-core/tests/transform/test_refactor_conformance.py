"""The shared refactor-comparison cases on DuckDB, and the generated SQL (#899)."""

from __future__ import annotations

import pytest

from exmergo_dex_core.adapters.base import ColumnMeta
from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.transform import multiset_compare
from exmergo_dex_core.transform.multiset_compare import (
    _base_type,
    _exact_count,
    _keyed_sql,
    _multiset_sql,
    _project,
    compare_refactors,
)

from .refactor_conformance import CASES, assert_case, run_case


@pytest.fixture
def adapter(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    path = tmp_path / "w.duckdb"
    duckdb.connect(str(path)).close()
    opened = DuckDBAdapter(str(path))
    yield opened
    opened.close()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_shared_case_on_duckdb(case, adapter):
    assert_case(case, run_case(case, adapter))


@pytest.mark.parametrize(
    "dialect",
    [
        "duckdb",
        "bigquery",
        "snowflake",
        "databricks",
        "redshift",
        "postgres",
        "clickhouse",
        "trino",
    ],
)
def test_no_connector_gets_a_hash_or_a_sample(dialect):
    left = _project("select 1 as a", ["a"], ["a"], dialect)
    right = _project("select 1 as a", ["a"], ["a"], dialect)
    for sql in (
        _multiset_sql(left, right, ["a"], dialect),
        _keyed_sql(left, right, ["a"], ["a"], dialect),
    ):
        lowered = sql.lower()
        for word in ("hash", "md5", "sha", "approx", "sample", "fingerprint", "rand"):
            assert word not in lowered, (dialect, word)
        assert "full outer join" not in lowered
        assert "union all" in lowered and "group by" in lowered


@pytest.mark.parametrize(
    ("dialect", "type_name"),
    [
        ("bigquery", "ARRAY<STRING>"),
        ("bigquery", "STRUCT"),
        ("bigquery", "JSON"),
        ("bigquery", "GEOGRAPHY"),
        ("snowflake", "VARIANT"),
        ("snowflake", "OBJECT"),
        ("snowflake", "ARRAY"),
        ("redshift", "super"),
        ("databricks", "map<string,int>"),
        ("postgres", "json"),
        ("postgres", "point"),
        ("clickhouse", "Map(String, Int32)"),
        ("clickhouse", "Nullable(JSON)"),
        ("trino", "map(varchar, integer)"),
        ("trino", "json"),
    ],
)
def test_an_ungroupable_type_stops_before_anything_runs(dialect, type_name):
    ran: list[str] = []

    class _Stub:
        name = dialect

        def __init__(self):
            self.dialect = dialect

        def describe_query(self, sql):
            return [ColumnMeta("payload", type_name, True, 0)]

        def run_query(self, sql, **kwargs):
            ran.append(sql)
            raise AssertionError("run_query was called")

    result = compare_refactors(
        "select 1 as payload",
        "select 1 as payload",
        adapter=_Stub(),
        cache=DexCache(),
        limits=QueryLimits(),
        command="transform compare",
    )
    assert result.reason_code == "unsupported_type"
    assert "payload" in result.detail
    assert ran == []


def test_jsonb_is_not_refused_as_json():
    assert _base_type("jsonb") == "JSONB"
    assert "JSONB" not in multiset_compare._UNGROUPABLE_TYPES["postgres"]


def test_a_nullable_wrapper_is_read_through():
    assert _base_type("LowCardinality(Nullable(String))") == "STRING"


@pytest.mark.parametrize(
    ("value", "count"),
    [
        (3, 3),
        (3.0, 3),
        ("3", 3),
        (-1, None),
        (3.5, None),
        (True, None),
        (2.0**60, None),
        ("x", None),
        (None, None),
    ],
)
def test_a_count_is_read_exactly_or_not_at_all(value, count):
    assert _exact_count(value) == count


def test_a_paid_describe_is_priced_before_it_runs(monkeypatch):
    """A connector whose schema read wakes compute asks first and touches nothing."""

    from exmergo_dex_core.results import ConfirmationRequest

    touched: list[str] = []

    class _Paid:
        name = "paid"
        dialect = "redshift"
        describe_query_free = False

        def query_estimate(self, sql):
            return 5.0

        def describe_query(self, sql):
            touched.append("describe")
            raise AssertionError("describe ran before the handshake")

        def run_query(self, sql, **kwargs):
            touched.append("run")
            raise AssertionError("run ran before the handshake")

    seen: dict = {}

    def fake(command, adapter, estimate, **kwargs):
        seen["estimate"] = estimate
        return ConfirmationRequest()

    monkeypatch.setattr(multiset_compare.command_args, "confirmation_request", fake)
    result = compare_refactors(
        "select 1 as id",
        "select 1 as id",
        adapter=_Paid(),
        cache=DexCache(),
        limits=QueryLimits(),
        command="transform compare",
        key_columns=["id"],
    )
    assert result.reason_code == "awaiting_confirmation"
    assert result.pending is not None
    assert touched == []
    assert seen["estimate"] == 20.0  # two sides, two statements


def test_a_guard_refusal_is_not_reported_as_unreadable(monkeypatch):
    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    class _Spent:
        name = "spent"
        dialect = "duckdb"

        def describe_query(self, sql):
            return [ColumnMeta("id", "INTEGER", True, 0)]

        def run_query(self, sql, **kwargs):
            raise OverCeilingError("over the ceiling")

    with pytest.raises(OverCeilingError):
        compare_refactors(
            "select 1 as id",
            "select 1 as id",
            adapter=_Spent(),
            cache=DexCache(),
            limits=QueryLimits(),
            command="transform compare",
        )


@pytest.mark.parametrize(
    "dialect",
    [
        "bigquery",
        "snowflake",
        "databricks",
        "redshift",
        "postgres",
        "clickhouse",
        "trino",
    ],
)
def test_a_pii_column_is_compared_in_every_dialect(dialect):
    """The statement projects counts, so the firewall has no value to refuse."""

    from exmergo_dex_core.adapters.base import QueryResult
    from exmergo_dex_core.cache import ColumnProfile, Dataset, PIIFlag

    ran: list[str] = []

    class _Stub:
        name = dialect

        def __init__(self):
            self.dialect = dialect

        def describe_query(self, sql):
            return [ColumnMeta("email", "text", True, 0)]

        def run_query(self, sql, **kwargs):
            ran.append(sql)
            return QueryResult(
                columns=["a", "b", "c", "d"],
                types=["int"] * 4,
                cells=[[2, 2, 0, 0]],
                truncated=False,
            )

    cache = DexCache(
        datasets=[
            Dataset(
                identifier="db.app.people",
                columns=[
                    ColumnProfile(
                        name="email",
                        data_type="text",
                        pii=PIIFlag(category="email", confidence=0.95),
                    )
                ],
            )
        ]
    )
    sql = "select email from db.app.people"
    result = compare_refactors(
        sql,
        sql,
        adapter=_Stub(),
        cache=cache,
        limits=QueryLimits(),
        command="transform compare",
    )
    assert result.status == "equivalent", (result.reason_code, result.detail)
    assert len(ran) == 1
