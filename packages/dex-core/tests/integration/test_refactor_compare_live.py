"""The refactor comparison's shared cases on every live warehouse (#899).

Literal statements only, so a case reads no table. What each connector bills is
its statement minimum, under a small confirmed budget.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from transform.refactor_conformance import CASES, assert_case, run_case

from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.config import DexConfig, QueryLimits
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.guards.cost_guard import CostGuardError
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform.multiset_compare import compare_refactors

from .conftest import integration_budget

_BUDGET = {
    "bigquery": 1_000_000_000,
    "snowflake": 120,
    "databricks": 120,
    "redshift": 120,
    "postgres": 120,
    "clickhouse": 120,
    "trino": 120,
}

_CONNECTORS = [
    pytest.param(name, marks=[pytest.mark.integration, getattr(pytest.mark, name)])
    for name in _BUDGET
]

#: A value of a type the connector cannot group, per connector, and the type it
#: reports.
_UNGROUPABLE = {
    "bigquery": "select [1, 2] as payload",
    "snowflake": "select parse_json('{\"a\": 1}') as payload",
    "databricks": "select map('a', 1) as payload",
    "redshift": "select json_parse('{\"a\": 1}') as payload",
    "postgres": "select '{\"a\": 1}'::json as payload",
    "clickhouse": "select map('a', 1) as payload",
    "trino": "select map(array['a'], array[1]) as payload",
}


#: NaN, -0.0 and 0.0 per connector. Measured on every connector: NaN groups with
#: NaN, and -0.0 groups with 0.0, so a change of zero's sign is not a difference.
_FLOATS = {
    "bigquery": ("cast('nan' as float64)", "cast('-0.0' as float64)", "0.0"),
    "snowflake": ("'NaN'::float", "-0.0::float", "0.0::float"),
    "databricks": ("double('NaN')", "cast('-0.0' as double)", "cast(0.0 as double)"),
    "redshift": ("'NaN'::float8", "'-0.0'::float8", "0.0::float8"),
    "postgres": ("'NaN'::float8", "'-0.0'::float8", "0.0::float8"),
    "clickhouse": ("toFloat64('nan')", "toFloat64('-0.0')", "toFloat64('0.0')"),
    "trino": ("nan()", "-0e0", "0e0"),
}


def _block(connector: str) -> dict:
    env = os.environ
    if connector == "bigquery":
        return {
            "project": env["DEX_TEST_BQ_PROJECT"],
            "datasets": [env["DEX_TEST_BQ_DATASET"]],
        }
    if connector == "snowflake":
        return {
            "warehouse": env.get("DEX_TEST_SNOWFLAKE_WAREHOUSE", "DEX_CI_WH"),
            "connection_name": env.get("DEX_TEST_SNOWFLAKE_CONNECTION"),
            "suspend_after_command": True,
        }
    if connector == "databricks":
        return {"warehouse": env["DEX_TEST_DATABRICKS_WAREHOUSE"]}
    if connector == "redshift":
        return {
            "workgroup": env.get("DEX_TEST_REDSHIFT_WORKGROUP"),
            "dbname": env.get("DEX_TEST_REDSHIFT_DATABASE"),
        }
    return {}


@pytest.fixture
def engine_for(tmp_path: Path, monkeypatch):
    opened: list[DexEngine] = []

    def build(
        connector: str, *, confirmed: bool = True, budget: float | None = None
    ) -> DexEngine:
        if connector == "postgres":
            monkeypatch.setenv("DATABASE_URL", os.environ["DEX_TEST_PG_DSN"])
        if connector == "clickhouse":
            monkeypatch.setenv("CLICKHOUSE_URL", os.environ["DEX_TEST_CH_DSN"])
        if connector == "trino":
            monkeypatch.setenv("TRINO_URL", os.environ["DEX_TEST_TRINO_URL"])
        config = DexConfig.model_validate(
            {
                "connector": connector,
                connector: _block(connector),
                "budget": integration_budget(),
            }
        )
        engine = DexEngine(
            connector=connector,
            repo_root=str(tmp_path),
            store=FilesystemStore(tmp_path),
            config=config,
            confirmed=confirmed,
            budget=_BUDGET[connector] if budget is None else budget,
        )
        opened.append(engine)
        return engine

    yield build
    for engine in opened:
        engine.close()


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_every_shared_case(connector, engine_for):
    # A case is one command, with the gate and the budget one command gets.
    engine = engine_for(connector)
    failures = []
    for case in CASES:
        try:
            adapter = engine._adapter("transform compare")
            assert_case(case, run_case(case, adapter))
        except AssertionError as exc:
            failures.append(f"{case.name}: {exc}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_describe_reports_a_known_statement_in_order(connector, engine_for):
    adapter = engine_for(connector)._adapter("transform compare")
    columns = adapter.describe_query("select 1 as first_col, 'x' as second_col")
    assert [column.name.lower() for column in columns] == ["first_col", "second_col"]


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_an_ungroupable_type_stops_before_it_runs(connector, engine_for):
    adapter = engine_for(connector)._adapter("transform compare")
    sql = _UNGROUPABLE[connector]
    result = compare_refactors(
        sql,
        sql,
        adapter=adapter,
        cache=DexCache(),
        limits=QueryLimits(),
        command="transform compare",
    )
    assert result.status == "not_checked"
    assert result.reason_code == "unsupported_type", result.detail


_METERED = [
    pytest.param(name, marks=[pytest.mark.integration, getattr(pytest.mark, name)])
    for name in _BUDGET
]


@pytest.mark.parametrize("connector", _METERED)
def test_an_unconfirmed_run_returns_a_priced_request(connector, engine_for):
    adapter = engine_for(connector, confirmed=False)._adapter("transform compare")
    ran: list[str] = []
    real = adapter.run_query
    adapter.run_query = lambda sql, **kw: (ran.append(sql), real(sql, **kw))[1]
    result = compare_refactors(
        "select 1 as id",
        "select 1 as id",
        adapter=adapter,
        cache=DexCache(),
        limits=QueryLimits(),
        command="transform compare",
    )
    assert result.reason_code == "awaiting_confirmation", result.detail
    assert result.pending is not None
    assert ran == []


@pytest.mark.parametrize("connector", _METERED)
def test_a_budget_too_small_runs_nothing(connector, engine_for):
    adapter = engine_for(connector, budget=0.000001)._adapter("transform compare")
    ran: list[str] = []
    real = adapter.run_query
    adapter.run_query = lambda sql, **kw: (ran.append(sql), real(sql, **kw))[1]
    try:
        result = compare_refactors(
            "select 1 as id",
            "select 1 as id",
            adapter=adapter,
            cache=DexCache(),
            limits=QueryLimits(),
            command="transform compare",
        )
    except CostGuardError:
        assert ran == []
        return
    assert result.status == "not_checked", result.reason_code
    assert ran == []


@pytest.mark.parametrize("connector", _CONNECTORS)
def test_how_each_connector_groups_floats(connector, engine_for):
    engine = engine_for(connector)
    nan, negative_zero, zero = _FLOATS[connector]

    def status(left: str, right: str) -> str:
        return compare_refactors(
            f"select {left} as v",
            f"select {right} as v",
            adapter=engine._adapter("transform compare"),
            cache=DexCache(),
            limits=QueryLimits(),
            command="transform compare",
        ).status

    assert status(nan, nan) == "equivalent"
    assert status(negative_zero, zero) == "equivalent"
    assert status(nan, zero) == "different"
