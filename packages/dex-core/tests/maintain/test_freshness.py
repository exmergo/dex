"""A declared freshness checked by ``maintain verify`` (#1460).

The planning half reads ``meta.dex.freshness`` off a compiled manifest; the
judging half runs against a real DuckDB file, since the statement is what is
under test: the column's newest value and the warehouse's clock, in whole epoch
seconds, read in one aggregate statement. Each other dialect's expression is
held to the shipped macro's in ``transform/test_freshness.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.maintain import declared_facts as facts_mod
from exmergo_dex_core.maintain import freshness as freshness_mod

duckdb = pytest.importorskip("duckdb")


def _manifest(project: Path, nodes: dict) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )


def _model(name: str, columns: dict[str, dict]) -> dict:
    node: dict = {
        "name": name,
        "resource_type": "model",
        "relation_name": f'"warehouse"."main"."{name}"',
        "config": {"materialized": "table", "meta": {}},
        "columns": {},
    }
    for column, column_dex in columns.items():
        node["columns"][column] = {
            "name": column,
            "config": {"meta": {"dex": column_dex}},
        }
    return node


def _fresh(max_lag: str = "6h", basis: str = "capture") -> dict:
    return {"role": "time", "freshness": {"max_lag": max_lag, "basis": basis}}


def _run(tmp_path: Path, columns: dict[str, dict], *statements: str):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    _manifest(tmp_path, {"model.p.fct": _model("fct", columns)})
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    for statement in statements:
        conn.execute(statement)
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, notes = facts_mod.plan(tmp_path)
        assert notes == []
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live)
        return facts_mod.findings(adapter, prepared)
    finally:
        adapter.close()


def _table(select: str) -> str:
    return f"create table fct as {select}"


# --- reading the declaration --------------------------------------------------------


def test_the_plan_reads_each_columns_freshness(tmp_path: Path):
    _manifest(
        tmp_path,
        {
            "model.p.fct": _model(
                "fct",
                {"loaded_at": _fresh("360m", "capture"), "amount": {"unit": "count"}},
            )
        },
    )
    [model], _ = facts_mod.plan(tmp_path)
    assert model.freshness == (
        freshness_mod.FreshnessRule("loaded_at", "6h", 21600, "capture"),
    )
    assert model.declares("freshness")


@pytest.mark.parametrize(
    "value",
    [
        {"max_lag": "6 hours", "basis": "capture"},
        {"max_lag": "6h", "basis": "loaded"},
        {"max_lag": "0h", "basis": "capture"},
        {"max_lag": 6, "basis": "capture"},
        "6h",
    ],
)
def test_a_rule_the_schema_would_refuse_is_never_guessed_into_a_check(value):
    assert freshness_mod.read_rule("t", value) is None


def test_nothing_declared_suppresses_the_class_by_name(tmp_path: Path):
    result = _run(
        tmp_path, {"share": {"unit": "fraction"}}, _table("select 0.5 as share")
    )
    assert "freshness" in result.suppressed
    assert "freshness on a time column" in result.suppressed["freshness"]


# --- judging --------------------------------------------------------------------


def test_a_column_older_than_its_lag_is_stale(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        _table("select current_timestamp - interval 7 hour as loaded_at"),
    )
    [finding] = result.findings
    assert finding.code == freshness_mod.CODE
    assert (finding.identifier, finding.column) == ("fct", "loaded_at")
    assert finding.data["stale"] == 1
    assert finding.data["lag_seconds"] >= 25200
    assert finding.data["max_lag_seconds"] == 21600
    [record] = result.freshness
    assert record["status"] == "contradicted"
    assert record["finding"] == freshness_mod.CODE
    assert "freshness" not in result.suppressed


def test_a_view_exactly_at_its_lag_passes(tmp_path: Path):
    # The view computes its value from the clock of the statement reading it,
    # so the newest value is exactly 6h behind whatever that clock says.
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        "create view fct as select current_timestamp - interval 6 hour as loaded_at",
    )
    assert result.findings == []
    [record] = result.freshness
    assert (record["status"], record["lag_seconds"]) == ("satisfied", 21600)


def test_one_second_past_the_lag_is_stale(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        "create view fct as select current_timestamp - interval 21601 second "
        "as loaded_at",
    )
    [finding] = result.findings
    assert finding.data["lag_seconds"] == 21601


def test_a_column_well_inside_its_lag_passes(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        _table("select current_timestamp - interval 5 minute as loaded_at"),
    )
    assert result.findings == []
    assert result.freshness[0]["status"] == "satisfied"


def test_an_empty_model_is_not_fresh(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        _table("select current_timestamp as loaded_at where 1 = 0"),
    )
    [finding] = result.findings
    assert finding.data["reason"] == "no_rows"
    assert finding.data["lag_seconds"] is None
    assert "no rows is not fresh" in finding.detail
    assert result.freshness[0]["reason_code"] == "no_rows"


def test_a_column_null_on_every_row_is_not_fresh(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        _table("select cast(null as timestamp) as loaded_at from range(3)"),
    )
    [finding] = result.findings
    assert finding.data["reason"] == "all_null"
    assert finding.data["rows"] == 3


def test_the_newest_value_decides_whatever_older_rows_hold(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("1d")},
        _table(
            "select timestamp '2000-01-01' as loaded_at "
            "union all select cast(null as timestamp) "
            "union all select current_timestamp - interval 1 hour"
        ),
    )
    assert result.findings == []


def test_a_capture_time_is_loud_and_a_business_time_is_not(tmp_path: Path):
    stale = _table("select timestamp '2000-01-01' as at")
    capture = _run(tmp_path / "c", {"at": _fresh("1d", "capture")}, stale)
    business = _run(tmp_path / "b", {"at": _fresh("1d", "business")}, stale)
    assert capture.findings[0].severity == "high"
    assert "loading has stopped" in capture.findings[0].detail
    assert business.findings[0].severity == "medium"
    assert "backfill" in business.findings[0].detail


def test_a_value_ahead_of_the_clock_is_fresh_and_noted(tmp_path: Path):
    result = _run(
        tmp_path,
        {"loaded_at": _fresh("6h")},
        _table("select current_timestamp + interval 1 hour as loaded_at"),
    )
    assert result.findings == []
    assert result.freshness[0]["ahead_of_clock"] is True


def test_a_date_column_reads_as_its_utc_midnight(tmp_path: Path):
    result = _run(
        tmp_path,
        {"d": _fresh("2d", "business")},
        _table("select current_date as d"),
    )
    assert result.findings == []
    assert 0 <= result.freshness[0]["lag_seconds"] <= 86400 + 60


def test_a_zoned_timestamp_is_read_as_the_instant_it_is(tmp_path: Path):
    result = _run(
        tmp_path,
        {"t": _fresh("6h")},
        _table("select cast(current_timestamp as timestamptz) as t"),
    )
    assert result.findings == []
    assert abs(result.freshness[0]["lag_seconds"]) <= 60


def test_a_text_column_is_unchecked_and_named(tmp_path: Path):
    result = _run(tmp_path, {"t": _fresh()}, _table("select '2026-01-01' as t"))
    assert result.findings == []
    [record] = result.freshness
    assert (record["status"], record["reason_code"]) == (
        "unchecked",
        freshness_mod.NOT_TEMPORAL,
    )
    assert "not a date or timestamp" in result.suppressed["freshness"]


def test_a_column_the_relation_lacks_is_unchecked_and_named(tmp_path: Path):
    result = _run(tmp_path, {"t": _fresh()}, _table("select 1 as other"))
    [record] = result.freshness
    assert (record["status"], record["reason_code"]) == (
        "unchecked",
        freshness_mod.COLUMN_MISSING,
    )


def test_two_time_columns_are_judged_apart(tmp_path: Path):
    result = _run(
        tmp_path,
        {"ordered_at": _fresh("1d", "business"), "loaded_at": _fresh("1h")},
        _table(
            "select current_timestamp - interval 2 hour as ordered_at, "
            "current_timestamp - interval 2 hour as loaded_at"
        ),
    )
    assert [f.column for f in result.findings] == ["loaded_at"]
    assert sorted((r["column"], r["status"]) for r in result.freshness) == [
        ("loaded_at", "contradicted"),
        ("ordered_at", "satisfied"),
    ]


def test_the_statement_returns_counts_and_a_lag_never_a_value(tmp_path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    _manifest(tmp_path, {"model.p.fct": _model("fct", {"t": _fresh()})})
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(_table("select current_timestamp as t"))
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live)
    finally:
        adapter.close()
    [sql] = prepared.statements()
    import sqlglot

    parsed = sqlglot.parse_one(sql, read="duckdb")
    # Every projection is an aggregate or arithmetic over aggregates.
    assert all(
        projection.find(sqlglot.exp.AggFunc) is not None
        for projection in parsed.expressions
    )


# --- the dialect expressions --------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "data_type", "kind"),
    [
        ("duckdb", "TIMESTAMP", "ntz"),
        ("duckdb", "TIMESTAMP WITH TIME ZONE", "tz"),
        ("duckdb", "DATE", "date"),
        ("postgres", "timestamp without time zone", "ntz"),
        ("postgres", "timestamp with time zone", "tz"),
        ("bigquery", "TIMESTAMP", "tz"),
        ("bigquery", "DATETIME", "ntz"),
        ("snowflake", "TIMESTAMP_NTZ", "ntz"),
        ("snowflake", "TIMESTAMP_LTZ", "tz"),
        ("snowflake", "TIMESTAMP_TZ", "tz"),
        ("databricks", "timestamp", "tz"),
        ("databricks", "timestamp_ntz", "ntz"),
        ("clickhouse", "Nullable(DateTime64(3))", "tz"),
        ("clickhouse", "Date32", "date"),
        ("trino", "timestamp(6)", "ntz"),
        ("trino", "timestamp(3) with time zone", "tz"),
        ("redshift", "timestamptz", "tz"),
        ("duckdb", "TIME", None),
        ("duckdb", "INTERVAL", None),
        ("duckdb", "VARCHAR", None),
        ("duckdb", None, "unknown"),
    ],
)
def test_a_type_is_read_as_the_kind_of_time_it_holds(dialect, data_type, kind):
    assert freshness_mod.temporal_kind(dialect, data_type) == kind


@pytest.mark.parametrize(
    "dialect",
    [
        "duckdb",
        "postgres",
        "redshift",
        "bigquery",
        "snowflake",
        "databricks",
        "clickhouse",
        "trino",
    ],
)
def test_each_dialects_statement_is_one_read_only_select(dialect):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    column = '"t"'
    for kind in ("date", "ntz", "tz", "unknown"):
        lag = freshness_mod.lag_sql(dialect, column, kind)
        sql = f'SELECT {lag} AS lag FROM "db"."s"."m"'  # noqa: S608
        assert assert_select_only(sql, dialect=dialect) == sql


def test_a_personal_data_column_keeps_its_verdict_and_withholds_its_lag(
    tmp_path: Path,
):
    # The lag is the clock minus the newest value, so it would disclose it.
    result = _run(
        tmp_path,
        {"date_of_birth": _fresh("1d", "business")},
        _table("select timestamp '1990-05-01' as date_of_birth"),
    )
    [finding] = result.findings
    assert finding.data["lag_seconds"] is None
    assert finding.data["lag_withheld"] is True
    assert "withheld" in finding.detail
    [record] = result.freshness
    assert "lag_seconds" not in record
    assert record["lag_withheld"] is True


def test_a_personal_data_column_is_judged_without_computing_its_newest_value(
    tmp_path: Path,
):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    columns = {"date_of_birth": _fresh("1d", "business")}
    _manifest(tmp_path, {"model.p.fct": _model("fct", columns)})
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(_table("select current_timestamp as date_of_birth"))
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live)
        [sql] = prepared.statements()
        result = facts_mod.findings(adapter, prepared)
    finally:
        adapter.close()
    assert "MAX(" not in sql.upper()
    assert result.findings == []
    [record] = result.freshness
    assert (record["status"], record["lag_withheld"]) == ("satisfied", True)


def test_a_person_clearing_the_column_shows_its_lag(tmp_path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    columns = {"date_of_birth": _fresh("1d", "business")}
    _manifest(tmp_path, {"model.p.fct": _model("fct", columns)})
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(_table("select timestamp '1990-05-01' as date_of_birth"))
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        cleared = {f"{live[0]}.date_of_birth".lower()}
        prepared = facts_mod.prepare(adapter, models, live, pii_overrides=cleared)
        result = facts_mod.findings(adapter, prepared, pii_overrides=cleared)
    finally:
        adapter.close()
    [finding] = result.findings
    assert finding.data["lag_seconds"] > 0


def test_an_infinite_newest_value_is_never_read_as_satisfied(tmp_path: Path):
    # A DuckDB that reads EPOCH of -infinity as NULL gives no lag, which is
    # unchecked; one that floors it to a huge number gives a stale column.
    # Either way it is not fresh.
    result = _run(
        tmp_path,
        {"t": _fresh()},
        _table("select timestamp '-infinity' as t"),
    )
    [record] = result.freshness
    assert record["status"] in {"unchecked", "contradicted"}
    if record["status"] == "unchecked":
        assert record["reason_code"] == freshness_mod.LAG_UNREADABLE
        assert result.findings == []


def test_a_lag_the_warehouse_returns_as_null_is_unchecked():
    from types import SimpleNamespace

    rule = freshness_mod.FreshnessRule("t", "6h", 21600, "capture")
    model = facts_mod.DeclaredModel(
        model="fct", unique_id="model.p.fct", relation="db.s.fct", freshness=(rule,)
    )
    probe = facts_mod._Probe(model=model, identifier="db.s.fct")
    probe.freshness = freshness_mod.Written(
        "select 1", [(0, rule, "t", "TIMESTAMP", True)]
    )
    answer = SimpleNamespace(
        columns=["dex_rows", "dex_fresh_n_0", "dex_fresh_lag_0"], cells=[[3, 3, None]]
    )
    adapter = SimpleNamespace(run_query=lambda *a, **k: answer)
    prepared = facts_mod.Prepared(dropped={"freshness": []})
    result = facts_mod.DeclaredFactsResult()
    checked = {"freshness": 0}

    freshness_mod.run_probe(adapter, probe, result, checked, prepared, None)

    assert result.findings == []
    [record] = result.freshness
    assert (record["status"], record["reason_code"]) == (
        "unchecked",
        freshness_mod.LAG_UNREADABLE,
    )
    assert checked["freshness"] == 0


# --- a daily deadline (#1486) ------------------------------------------------------
#
# The day is the runner's (frozen here through ``freshness_mod.clock``), read in
# the declared zone as Elementary's data_freshness_sla reads it; whether the
# deadline has passed is the warehouse's clock, compared in the statement.


def _deadline(
    deadline: str = "00:00",
    timezone: str = "UTC",
    basis: str = "capture",
    days: list[str] | None = None,
    max_lag: str | None = None,
) -> dict:
    freshness: dict = {"deadline": deadline, "timezone": timezone, "basis": basis}
    if days is not None:
        freshness["days"] = days
    if max_lag is not None:
        freshness["max_lag"] = max_lag
    return {"role": "time", "freshness": freshness}


def _runner_at(monkeypatch, moment) -> None:
    monkeypatch.setattr(freshness_mod, "clock", lambda: moment)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def test_the_plan_reads_a_lag_and_a_deadline_as_two_checks(tmp_path: Path):
    _manifest(
        tmp_path,
        {"model.p.fct": _model("fct", {"loaded_at": _deadline(max_lag="6h")})},
    )
    [model], _ = facts_mod.plan(tmp_path)
    assert [(r.check, r.max_lag, r.deadline) for r in model.freshness] == [
        ("max_lag", "6h", None),
        ("deadline", None, "00:00"),
    ]


def test_a_deadline_the_schema_would_refuse_is_never_guessed_into_a_check():
    bad = {"deadline": "07:00", "timezone": "Mars/Base", "basis": "capture"}
    assert freshness_mod.read_rules("t", bad) == []
    assert (
        freshness_mod.read_rule("t", {"deadline": "07:00", "basis": "capture"}) is None
    )


def test_todays_value_after_the_deadline_is_satisfied(tmp_path: Path, monkeypatch):
    from datetime import timedelta

    # Yesterday's deadline (00:00 UTC) has passed, and a value from now falls
    # on or after yesterday's midnight.
    _runner_at(monkeypatch, _now() - timedelta(days=1))
    result = _run(
        tmp_path,
        {"loaded_at": _deadline()},
        _table("select current_timestamp as loaded_at"),
    )
    assert result.findings == []
    [record] = result.freshness
    assert record["check"] == "deadline"
    assert record["status"] == "satisfied"
    assert (record["deadline"], record["timezone"]) == ("00:00", "UTC")
    assert "max_lag" not in record


def test_an_earlier_days_value_after_the_deadline_is_missed(
    tmp_path: Path, monkeypatch
):
    from datetime import timedelta

    _runner_at(monkeypatch, _now() - timedelta(days=1))
    result = _run(
        tmp_path,
        {"loaded_at": _deadline()},
        _table("select timestamp '2020-01-01 06:00' as loaded_at"),
    )
    [finding] = result.findings
    assert finding.code == "deadline_missed"
    assert finding.severity == "high"
    assert finding.data["reason"] == "not_today"
    # Counts and the day judged, never a timestamp from the column.
    assert "2020" not in finding.detail and "2020" not in json.dumps(finding.data)
    [record] = result.freshness
    assert (record["status"], record["finding"], record["reason_code"]) == (
        "contradicted",
        "deadline_missed",
        "not_today",
    )


def test_a_business_time_misses_quietly_and_says_where_the_deadline_belongs(
    tmp_path: Path, monkeypatch
):
    from datetime import timedelta

    _runner_at(monkeypatch, _now() - timedelta(days=1))
    result = _run(
        tmp_path,
        {"ordered_at": _deadline(basis="business")},
        _table("select timestamp '2020-01-01' as ordered_at"),
    )
    [finding] = result.findings
    assert finding.severity == "medium"
    assert "load time" in finding.detail


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("create table fct (loaded_at timestamp)", "no_rows"),
        ("create table fct as select cast(null as timestamp) as loaded_at", "all_null"),
    ],
)
def test_no_rows_or_no_value_after_the_deadline_is_missed(
    tmp_path: Path, monkeypatch, statement, reason
):
    from datetime import timedelta

    _runner_at(monkeypatch, _now() - timedelta(days=1))
    result = _run(tmp_path, {"loaded_at": _deadline()}, statement)
    [finding] = result.findings
    assert finding.data["reason"] == reason


def test_before_the_deadline_nothing_is_due(tmp_path: Path, monkeypatch):
    from datetime import timedelta

    # A runner two days ahead puts the deadline in the warehouse's future.
    _runner_at(monkeypatch, _now() + timedelta(days=2))
    result = _run(
        tmp_path,
        {"loaded_at": _deadline()},
        _table("select timestamp '2020-01-01' as loaded_at"),
    )
    assert result.findings == []
    [record] = result.freshness
    assert (record["status"], record["reason_code"]) == (
        "not_applicable",
        freshness_mod.BEFORE_DEADLINE,
    )
    assert "freshness" not in result.suppressed


def test_a_day_the_days_leave_out_is_not_checked(tmp_path: Path, monkeypatch):
    from datetime import UTC, datetime

    # 2026-01-05 is a Monday.
    _runner_at(monkeypatch, datetime(2026, 1, 5, 12, tzinfo=UTC))
    result = _run(
        tmp_path,
        {"loaded_at": _deadline(days=["tue", "wed"])},
        _table("select timestamp '2020-01-01' as loaded_at"),
    )
    assert result.findings == []
    [record] = result.freshness
    assert (record["status"], record["reason_code"], record["days"]) == (
        "not_applicable",
        freshness_mod.NOT_SCHEDULED,
        ["tue", "wed"],
    )
    assert record["target_date"] == "2026-01-05"


def test_a_lag_and_a_deadline_on_one_column_are_two_records(
    tmp_path: Path, monkeypatch
):
    from datetime import timedelta

    _runner_at(monkeypatch, _now() - timedelta(days=1))
    result = _run(
        tmp_path,
        {"loaded_at": _deadline(max_lag="6h")},
        _table("select timestamp '2020-01-01' as loaded_at"),
    )
    assert sorted(r["check"] for r in result.freshness) == ["deadline", "max_lag"]
    assert sorted(f.code for f in result.findings) == [
        "deadline_missed",
        "freshness_exceeded",
    ]


def test_a_personal_data_column_is_judged_by_counts_alone(tmp_path: Path, monkeypatch):
    from datetime import timedelta

    _runner_at(monkeypatch, _now() - timedelta(days=1))
    result = _run(
        tmp_path,
        {"date_of_birth": _deadline(basis="business")},
        _table("select timestamp '1990-05-01' as date_of_birth"),
    )
    [finding] = result.findings
    assert "1990" not in finding.detail and "1990" not in json.dumps(finding.data)


def test_a_deadline_on_a_missing_column_reads_unchecked(tmp_path: Path, monkeypatch):
    result = _run(
        tmp_path,
        {"loaded_at": _deadline()},
        _table("select 1 as order_id"),
    )
    [record] = result.freshness
    assert (record["check"], record["status"], record["reason_code"]) == (
        "deadline",
        "unchecked",
        freshness_mod.COLUMN_MISSING,
    )


@pytest.mark.parametrize(
    ("moment", "zone", "target", "start", "due"),
    [
        # Summer in Chicago is UTC-5: local midnight is 05:00Z, 07:00 is 12:00Z.
        (
            "2026-07-01T12:00:00+00:00",
            "America/Chicago",
            "2026-07-01",
            "05:00",
            "12:00",
        ),
        # Winter is UTC-6.
        (
            "2026-01-15T12:00:00+00:00",
            "America/Chicago",
            "2026-01-15",
            "06:00",
            "13:00",
        ),
        # 02:00Z is still the previous evening in Chicago.
        (
            "2026-07-02T02:00:00+00:00",
            "America/Chicago",
            "2026-07-01",
            "05:00",
            "12:00",
        ),
        # Tokyo is ahead: 20:00Z on the 1st is the 2nd there.
        ("2026-07-01T20:00:00+00:00", "Asia/Tokyo", "2026-07-02", "15:00", "22:00"),
    ],
)
def test_the_window_is_todays_in_the_declared_zone(moment, zone, target, start, due):
    from datetime import UTC, datetime

    rule = freshness_mod.read_rules(
        "t", {"deadline": "07:00", "timezone": zone, "basis": "capture"}
    )[0]
    window = freshness_mod.deadline_window(rule, datetime.fromisoformat(moment))

    def clock(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, UTC).strftime("%H:%M")

    assert window.target_date == target
    assert (clock(window.day_start), clock(window.deadline)) == (start, due)
    assert window.scheduled is True


@pytest.mark.parametrize(
    "dialect",
    [
        "duckdb",
        "postgres",
        "redshift",
        "bigquery",
        "snowflake",
        "databricks",
        "clickhouse",
        "trino",
    ],
)
def test_each_dialects_deadline_parts_are_one_read_only_select(dialect):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    for kind in ("date", "ntz", "tz", "unknown"):
        today = freshness_mod.today_sql(dialect, '"t"', kind, 1767225600)
        due = freshness_mod.due_sql(dialect, 1767225600)
        sql = f'SELECT COUNT(*) AS n, {today} AS today, {due} AS due FROM "db"."s"."m"'  # noqa: S608
        assert assert_select_only(sql, dialect=dialect) == sql
