"""Declared facts checked against the built relation (#496).

The planning half is pure (a compiled manifest in, a list of what each model
declares out). The judging half runs against a real DuckDB file, since the
statements are the thing under test: a unit's MIN and MAX, a population
filter's count, the cumulative detector's lag-and-compare, and a declared
composite grain's distinct count, all aggregate-only. Billed connectors are
covered with a stub that prices and records, never executes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.dbt_project import declared_grain
from exmergo_dex_core.maintain import declared_facts as facts_mod
from exmergo_dex_core.maintain import verify as verify_mod
from exmergo_dex_core.project_definitions import (
    DeclaredCompositeKey,
    DeclaredKey,
    ProjectDefinitions,
)

duckdb = pytest.importorskip("duckdb")


def _manifest(project: Path, nodes: dict) -> None:
    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )


def _model(
    name: str,
    *,
    dex: dict | None = None,
    columns: dict[str, dict] | None = None,
    materialized: str = "table",
    relation: str | None = None,
) -> dict:
    node: dict = {
        "name": name,
        "resource_type": "model",
        "relation_name": relation or f'"warehouse"."main"."{name}"',
        "config": {"materialized": materialized, "meta": {}},
        "columns": {},
    }
    if dex is not None:
        node["config"]["meta"]["dex"] = dex
    for column, column_dex in (columns or {}).items():
        node["columns"][column] = {
            "name": column,
            "config": {"meta": {"dex": column_dex}},
        }
    return node


def _warehouse(tmp_path: Path, *statements: str) -> Path:
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    for statement in statements:
        conn.execute(statement)
    conn.close()
    return path


def _adapter(path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    return DuckDBAdapter(path)


def _run(tmp_path: Path, nodes: dict, *statements: str, check_grain: bool = False):
    _manifest(tmp_path, nodes)
    adapter = _adapter(_warehouse(tmp_path, *statements))
    try:
        models, notes = facts_mod.plan(tmp_path)
        assert notes == []
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live, check_grain=check_grain)
        return facts_mod.findings(adapter, prepared)
    finally:
        adapter.close()


def _codes(result) -> list[str]:
    return sorted(f.code for f in result.findings)


# --- reading what the project declares --------------------------------------------


def test_the_plan_reads_model_and_column_meta_from_the_manifest(tmp_path: Path):
    _manifest(
        tmp_path,
        {
            "model.p.fct": _model(
                "fct",
                dex={
                    "grain": ["a", "b"],
                    "population": {
                        "filters": [{"column": "status", "excludes": ["x"]}]
                    },
                },
                columns={
                    "share": {"unit": "fraction"},
                    "amount": {"additivity": "additive", "unit": "currency:EUR"},
                    "placed_at": {"role": "time"},
                },
            ),
            "model.p.plain": _model("plain"),
        },
    )
    models, notes = facts_mod.plan(tmp_path)
    assert notes == []
    assert [m.model for m in models] == ["fct"]
    model = models[0]
    assert model.grain == ("a", "b")
    assert model.units == (("share", "fraction"),)
    assert model.additive == ("amount",)
    assert model.filters[0].excludes == ("x",)
    assert dict(model.roles) == {"placed_at": "time"}
    assert model.relation == "warehouse.main.fct"


def test_a_currency_or_count_unit_has_no_range_to_check(tmp_path: Path):
    _manifest(
        tmp_path,
        {
            "model.p.fct": _model(
                "fct", columns={"a": {"unit": "currency:EUR"}, "b": {"unit": "count"}}
            )
        },
    )
    assert facts_mod.plan(tmp_path) == ([], [])


def test_the_plan_scopes_by_unique_id_or_lowered_name(tmp_path: Path):
    dex = {"grain": ["id"]}
    _manifest(
        tmp_path,
        {"model.p.a": _model("a", dex=dex), "model.p.b": _model("b", dex=dex)},
    )
    by_id, _ = facts_mod.plan(tmp_path, scope={"model.p.a"})
    by_name, _ = facts_mod.plan(tmp_path, scope={"b"})
    assert [m.model for m in by_id] == ["a"]
    assert [m.model for m in by_name] == ["b"]


def test_an_ephemeral_model_has_no_relation_and_is_left_out(tmp_path: Path):
    node = _model("eph", dex={"grain": ["id"]}, materialized="ephemeral")
    node["relation_name"] = None
    _manifest(tmp_path, {"model.p.eph": node})
    assert facts_mod.plan(tmp_path) == ([], [])


def test_no_manifest_is_named_rather_than_read_as_nothing_declared(tmp_path: Path):
    models, notes = facts_mod.plan(tmp_path)
    assert models == []
    assert "no compiled manifest" in notes[0]


def test_a_declared_grain_is_read_from_yaml_and_manifest_alike():
    assert declared_grain({"config": {"meta": {"dex": {"grain": ["a", "b"]}}}}) == [
        "a",
        "b",
    ]
    assert declared_grain({"meta": {"dex": {"grain": ["a"]}}}) == ["a"]
    assert declared_grain({"config": {"meta": {"dex": {"grain": []}}}}) is None
    assert declared_grain({"config": {"meta": {"dex": {"grain": "a"}}}}) is None


# --- grain: a declaration is the fourth source, ranked first ----------------------


def _defs(**fields) -> ProjectDefinitions:
    return ProjectDefinitions(present=True, **fields)


def test_a_declared_grain_outranks_every_other_source():
    definitions = _defs(
        declared_grains={"fct": ["order_id", "line"]},
        declared_keys=[
            DeclaredKey(model="fct", column="order_id", unique=True, source="manifest")
        ],
        declared_composite_keys=[
            DeclaredCompositeKey(model="fct", columns=["a", "b"], source="manifest")
        ],
        primary_entities={"fct": "order_id"},
    )
    plan = verify_mod.grain_plan(definitions)
    assert plan == {
        "fct": [
            verify_mod.GrainCandidate(
                columns=["order_id", "line"], source="declaration"
            )
        ]
    }


def test_a_model_without_a_declaration_keeps_the_existing_order():
    definitions = _defs(
        declared_grains={"other": ["id"]},
        declared_keys=[
            DeclaredKey(model="fct", column="order_id", unique=True, source="manifest")
        ],
    )
    plan = verify_mod.grain_plan(definitions)
    assert plan["fct"][0].source == "unique_test"
    assert plan["other"][0].source == "declaration"


def test_a_composite_grain_is_checked_with_no_dbt_utils_test(
    maintain_repo, monkeypatch
):
    """The point of the fourth source: a project with no
    `unique_combination_of_columns` still has its composite grain checked."""

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )
    maintain_repo.sql(
        "UPDATE stg_orders SET status = 'paid', ordered_at = DATE '2024-02-01'"
    )
    node = _model(
        "stg_orders",
        dex={"grain": ["status", "ordered_at"]},
        relation='"warehouse"."main"."stg_orders"',
    )
    target = maintain_repo.project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": {"model.maintain_test.stg_orders": node}}),
        encoding="utf-8",
    )
    (target / "run_results.json").write_text('{"results": []}', encoding="utf-8")
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    grain = [f for f in payload["data"]["findings"] if f["code"] == "grain_broken"]
    assert len(grain) == 1
    assert grain[0]["data"]["source"] == "declaration"
    assert grain[0]["data"]["columns"] == ["status", "ordered_at"]
    assert grain[0]["data"]["duplicate_count"] == 199
    assert "declares one row per status and ordered_at" in grain[0]["detail"]
    # Nothing else is declared, and the envelope says so rather than going quiet.
    suppressed = payload["data"]["suppressed"]
    assert "fraction or percent" in suppressed["unit"]
    assert "additive" in suppressed["additivity"]
    assert "population filters" in suppressed["population"]


# --- unit ---------------------------------------------------------------------------


_SHARES = (
    "CREATE TABLE shares AS SELECT i AS id, i / 10.0 AS as_fraction, "
    "i * 10.0 AS as_percent, i / 100.0 AS small_percent FROM range(1, 11) t(i)"
)


def test_a_fraction_above_one_is_out_of_range_and_states_the_range(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.shares": _model(
                "shares", columns={"as_percent": {"unit": "fraction"}}
            )
        },
        _SHARES,
    )
    assert _codes(result) == ["unit_out_of_range"]
    finding = result.findings[0]
    assert finding.severity == "medium"
    assert finding.column == "as_percent"
    assert finding.data["min"] == 10.0
    assert finding.data["max"] == 100.0
    assert "from 10 to 100" in finding.detail
    assert "unit" not in result.suppressed


def test_a_fraction_within_range_is_checked_and_clean(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.shares": _model(
                "shares", columns={"as_fraction": {"unit": "fraction"}}
            )
        },
        _SHARES,
    )
    assert result.findings == []
    assert "unit" not in result.suppressed


def test_a_percent_held_entirely_within_zero_to_one_is_a_question(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.shares": _model(
                "shares",
                columns={
                    "small_percent": {"unit": "percent"},
                    "as_percent": {"unit": "percent"},
                },
            )
        },
        _SHARES,
    )
    assert [f.column for f in result.findings] == ["small_percent"]
    assert "rather than proof" in result.findings[0].detail


def test_an_all_null_unit_column_is_unchecked_not_clean(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.t": _model("t", columns={"share": {"unit": "fraction"}})},
        "CREATE TABLE t AS SELECT i AS id, CAST(NULL AS DOUBLE) AS share "
        "FROM range(3) t(i)",
    )
    assert result.findings == []
    assert any("no non-NULL" in note for note in result.notes)
    assert "no declaration could be checked" in result.suppressed["unit"]


def test_an_empty_relation_is_unchecked_not_clean(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.t": _model(
                "t",
                dex={"population": {"filters": [{"column": "s", "excludes": ["x"]}]}},
                columns={"share": {"unit": "fraction"}},
            )
        },
        "CREATE TABLE t (s VARCHAR, share DOUBLE)",
    )
    assert result.findings == []
    assert any("has no rows" in note for note in result.notes)
    assert "has no rows" in result.suppressed["unit"]
    assert "has no rows" in result.suppressed["population"]


def test_a_unit_on_a_text_column_is_named_and_never_compared(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.t": _model("t", columns={"share": {"unit": "fraction"}})},
        "CREATE TABLE t AS SELECT '5' AS share",
    )
    assert result.findings == []
    assert any("not a number" in note for note in result.notes)
    assert "unit" in result.suppressed


def test_a_personal_data_column_has_its_range_withheld(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.t": _model("t", columns={"phone": {"unit": "fraction"}})},
        "CREATE TABLE t AS SELECT 5 AS phone",
    )
    assert _codes(result) == ["unit_out_of_range"]
    finding = result.findings[0]
    assert finding.data["values_withheld"] is True
    assert "min" not in finding.data and "max" not in finding.data
    assert "5" not in finding.detail


# --- population ---------------------------------------------------------------------


_ORDERS = (
    "CREATE TABLE orders AS SELECT i AS order_id, i % 10 AS customer_id, "
    "(['paid', 'cancelled', 'shipped', NULL])[(i % 4) + 1] AS status, "
    "i * 1.5 AS amount FROM range(100) t(i)"
)


def _population(**rule) -> dict:
    return {"population": {"filters": [{"column": "status", **rule}]}}


def test_an_excluded_value_still_present_is_a_count_not_rows(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.orders": _model("orders", dex=_population(excludes=["cancelled"]))},
        _ORDERS,
    )
    assert _codes(result) == ["population_contradicted"]
    finding = result.findings[0]
    assert finding.severity == "high"
    assert finding.data["contradicting_rows"] == 25
    assert finding.data["row_count"] == 100
    assert "25 of its 100 row(s)" in finding.detail


def test_an_includes_list_counts_every_other_value_and_null(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.orders": _model(
                "orders", dex=_population(includes=["paid", "shipped"])
            )
        },
        _ORDERS,
    )
    finding = result.findings[0]
    assert finding.data["contradicting_rows"] == 50
    assert finding.data["null_rows"] == 25
    assert "25 of them NULL" in finding.detail


def test_a_population_the_sql_implements_is_checked_and_clean(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.paid": _model("paid", dex=_population(excludes=["cancelled"]))},
        _ORDERS,
        "CREATE TABLE paid AS SELECT * FROM orders WHERE status <> 'cancelled'",
    )
    assert result.findings == []
    assert "population" not in result.suppressed


def test_a_numeric_filter_against_a_text_column_is_compared_as_text(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.t": _model(
                "t",
                dex={"population": {"filters": [{"column": "code", "excludes": [7]}]}},
            )
        },
        "CREATE TABLE t AS SELECT CAST(i AS VARCHAR) AS code FROM range(10) t(i)",
    )
    assert result.findings[0].data["contradicting_rows"] == 1


def test_a_text_filter_against_a_numeric_column_is_compared_as_text(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.t": _model(
                "t",
                dex={
                    "population": {"filters": [{"column": "code", "excludes": ["7"]}]}
                },
            )
        },
        "CREATE TABLE t AS SELECT i AS code FROM range(10) t(i)",
    )
    assert result.findings[0].data["contradicting_rows"] == 1


def test_a_value_with_a_quote_is_escaped_rather_than_breaking_the_statement(
    tmp_path: Path,
):
    result = _run(
        tmp_path,
        {"model.p.t": _model("t", dex=_population(excludes=["o'brien"]))},
        "CREATE TABLE t AS SELECT 'o''brien' AS status",
    )
    assert result.findings[0].data["contradicting_rows"] == 1


def test_a_population_column_aggregated_away_is_suppressed_not_passed(tmp_path: Path):
    """A rollup that groups the status away cannot be checked for it, and the
    check is not run against a guessed surrogate either."""

    result = _run(
        tmp_path,
        {"model.p.daily": _model("daily", dex=_population(excludes=["cancelled"]))},
        _ORDERS,
        "CREATE TABLE daily AS SELECT customer_id, sum(amount) AS amount "
        "FROM orders WHERE status <> 'cancelled' GROUP BY 1",
    )
    assert result.findings == []
    assert any("aggregates away" in note for note in result.notes)
    assert "no declaration could be checked" in result.suppressed["population"]


def test_a_declared_column_matches_the_relations_spelling(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.t": _model("t", dex=_population(excludes=["x"]))},
        "CREATE TABLE t AS SELECT 'x' AS \"STATUS\"",
    )
    assert result.findings[0].data["contradicting_rows"] == 1


# --- additivity ----------------------------------------------------------------------


_RUNNING = (
    "CREATE TABLE running AS SELECT i AS order_id, i % 5 AS customer_id, "
    "TIMESTAMP '2026-01-01' + to_days(i) AS placed_at, "
    "(i % 7) * 1.0 AS amount, "
    "sum(i) OVER (PARTITION BY i % 5 ORDER BY i) AS running_total "
    "FROM range(200) t(i)"
)


def _additive(*columns: str, **extra) -> dict:
    found = {c: {"additivity": "additive"} for c in columns}
    found.update(extra)
    return found


def test_an_additive_running_total_is_contradicted(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.running": _model(
                "running",
                dex={"grain": ["order_id"]},
                columns=_additive(
                    "running_total",
                    "amount",
                    customer_id={"role": "foreign_key"},
                    placed_at={"role": "time"},
                ),
            )
        },
        _RUNNING,
    )
    assert _codes(result) == ["additivity_contradicted"]
    finding = result.findings[0]
    assert finding.column == "running_total"
    assert finding.data["entity"] == "customer_id"
    assert finding.data["ordered_by"] == "placed_at"
    assert "semi_additive" in finding.detail
    assert "additivity" not in result.suppressed


def test_a_composite_grain_supplies_the_entity_and_the_time(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.running": _model(
                "running",
                dex={"grain": ["customer_id", "placed_at"]},
                columns=_additive("running_total"),
            )
        },
        _RUNNING,
    )
    assert result.findings[0].data["entity"] == "customer_id"


def test_too_few_observations_is_unchecked_not_clean(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.running": _model(
                "running",
                columns=_additive(
                    "running_total",
                    customer_id={"role": "foreign_key"},
                    placed_at={"role": "time"},
                ),
            )
        },
        _RUNNING.replace("range(200)", "range(10)"),
    )
    assert result.findings == []
    assert any("needs 20" in note for note in result.notes)
    assert "too few observations" in result.suppressed["additivity"]


def test_no_entity_or_time_to_order_by_is_named(tmp_path: Path):
    result = _run(
        tmp_path,
        {"model.p.t": _model("t", columns=_additive("amount"))},
        "CREATE TABLE t AS SELECT i * 1.0 AS amount FROM range(50) t(i)",
    )
    assert result.findings == []
    assert any("to order it by" in note for note in result.notes)
    assert "to order it by" in result.suppressed["additivity"]


def test_the_missing_ordering_columns_read_without_a_stray_article(tmp_path: Path):
    both = _run(
        tmp_path,
        {"model.p.t": _model("t", columns=_additive("amount"))},
        "CREATE TABLE t AS SELECT i * 1.0 AS amount FROM range(50) t(i)",
    )
    reason = both.suppressed["additivity"]
    assert "there is no entity and no time column to order it by" in reason
    assert "no an " not in reason


def test_a_missing_entity_alone_is_named_without_an_article(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.t": _model(
                "t", columns=_additive("amount", placed_at={"role": "time"})
            )
        },
        "CREATE TABLE t AS SELECT i * 1.0 AS amount, "
        "TIMESTAMP '2026-01-01' + to_days(i) AS placed_at FROM range(50) t(i)",
    )
    reason = result.suppressed["additivity"]
    assert "there is no entity to order it by" in reason
    assert "no an " not in reason


def test_models_sharing_a_suppression_reason_are_listed_once(tmp_path: Path):
    result = _run(
        tmp_path,
        {
            "model.p.a": _model("a", columns=_additive("amount")),
            "model.p.b": _model("b", columns=_additive("amount")),
        },
        "CREATE TABLE a AS SELECT i * 1.0 AS amount FROM range(50) t(i)",
        "CREATE TABLE b AS SELECT i * 1.0 AS amount FROM range(50) t(i)",
    )
    reason = result.suppressed["additivity"]
    assert reason.startswith("no declaration could be checked (2 models: ")
    assert "a" in reason.split("):")[0] and "b" in reason.split("):")[0]
    assert reason.count("to order it by") == 1


def test_suppression_reasons_that_differ_stay_separate():
    reason = facts_mod._suppression_reason(["a: x", "b: y", "c: x"])
    assert reason == "no declaration could be checked: a, c: x; b: y"


def test_a_single_models_suppression_reason_reads_as_before():
    assert (
        facts_mod._suppression_reason(["a: x"])
        == "no declaration could be checked: a: x"
    )
    assert facts_mod._suppression_reason(["no declared column could be measured"]) == (
        "no declaration could be checked: no declared column could be measured"
    )


# --- the build path's grain, and nothing declared at all --------------------------


def test_the_build_path_checks_a_composite_grain_in_the_same_statement(
    tmp_path: Path,
):
    result = _run(
        tmp_path,
        {"model.p.lines": _model("lines", dex={"grain": ["order_id", "product_id"]})},
        "CREATE TABLE lines AS SELECT i % 10 AS order_id, i % 3 AS product_id "
        "FROM range(40) t(i)",
        check_grain=True,
    )
    assert _codes(result) == ["grain_broken"]
    finding = result.findings[0]
    assert finding.data["source"] == "declaration"
    assert finding.data["duplicate_count"] == 10
    assert finding.severity == "low"  # below grain_min_rows


def test_a_single_grain_a_unique_test_covers_is_left_to_that_test(tmp_path: Path):
    nodes = {
        "model.p.t": _model("t", dex={"grain": ["id"]}),
        "test.p.unique_t_id": {
            "resource_type": "test",
            "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}},
            "attached_node": "model.p.t",
        },
    }
    _manifest(tmp_path, nodes)
    adapter = _adapter(
        _warehouse(tmp_path, "CREATE TABLE t AS SELECT 1 AS id UNION ALL SELECT 1")
    )
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live, check_grain=True)
    finally:
        adapter.close()
    assert prepared.statements() == []


def test_nothing_declared_suppresses_every_class_by_name():
    result = facts_mod.findings(object(), facts_mod.prepare(object(), [], []))
    assert set(result.suppressed) == set(facts_mod.CLASSES)
    assert all("no model in scope" in r for r in result.suppressed.values())


def test_a_relation_the_warehouse_does_not_have_is_named(tmp_path: Path):
    _manifest(
        tmp_path,
        {"model.p.gone": _model("gone", columns={"s": {"unit": "fraction"}})},
    )
    models, _ = facts_mod.plan(tmp_path)
    prepared = facts_mod.prepare(object(), models, ["warehouse.main.other"])
    result = facts_mod.findings(object(), prepared)
    assert "no single relation" in result.suppressed["unit"]


def test_every_statement_is_a_single_read_only_select(tmp_path: Path):
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    _manifest(
        tmp_path,
        {
            "model.p.running": _model(
                "running",
                dex={
                    "grain": ["customer_id", "placed_at"],
                    **_population(excludes=["x"]),
                },
                columns=_additive("running_total", amount={"unit": "fraction"}),
            )
        },
    )
    adapter = _adapter(
        _warehouse(tmp_path, _RUNNING.replace("AS amount", "AS amount, 'a' AS status"))
    )
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live, check_grain=True)
    finally:
        adapter.close()
    statements = prepared.statements()
    assert len(statements) == 2
    for sql in statements:
        assert assert_select_only(sql, dialect="duckdb") == sql
        # Aggregates only: nothing in the statement projects a bare column.
        assert "SELECT COUNT(*) AS dex_rows" in sql or "COUNT(CASE WHEN" in sql


# --- billed connectors: priced, offered, never run unasked ------------------------


class _BilledStub:
    """Prices every statement and refuses to run one; nothing here may execute
    before the handshake has said yes."""

    name = "bigquery"
    dialect = "bigquery"
    cost_gate = object()

    def __init__(self):
        self.priced: list[str] = []

    def query_estimate(self, sql: str) -> float:
        self.priced.append(sql)
        return 1_000.0

    def run_query(self, *args, **kwargs):  # pragma: no cover - must not be reached
        pytest.fail("a declared-fact statement ran before it was authorized")


def _billed_plan(tmp_path: Path):
    _manifest(
        tmp_path,
        {
            "model.p.fct": _model(
                "fct",
                dex=_population(excludes=["cancelled"]),
                columns={"share": {"unit": "fraction"}},
            )
        },
    )
    models, _ = facts_mod.plan(tmp_path)
    return models


def test_a_billed_connector_prices_and_offers_rather_than_running(tmp_path: Path):
    adapter = _BilledStub()
    prepared = facts_mod.prepare(adapter, _billed_plan(tmp_path), None)
    asked: list[tuple[float, int]] = []

    def handshake(estimate, count):
        asked.append((estimate, count))
        return "the-offer"

    result = facts_mod.findings(adapter, prepared, handshake=handshake)
    assert asked == [(1_000.0, 1)]
    assert result.offer == "the-offer"
    assert result.findings == []
    assert "offered above" in result.suppressed["unit"]
    assert "offered above" in result.suppressed["population"]
    # Additivity was never declared: its reason stays "nothing declared".
    assert "declares a column additive" in result.suppressed["additivity"]


def test_a_free_connector_prices_nothing(tmp_path: Path):
    class _Free:
        dialect = "duckdb"

    prepared = facts_mod.prepare(_Free(), _billed_plan(tmp_path), None)
    assert facts_mod.cost(_Free(), prepared) == (0.0, 1)


def test_the_statements_are_written_in_the_connectors_dialect(tmp_path: Path):
    adapter = _BilledStub()
    prepared = facts_mod.prepare(adapter, _billed_plan(tmp_path), None)
    (sql,) = prepared.statements()
    assert "`warehouse`.`main`.`fct`" in sql


def test_the_combined_handshake_prices_all_three_axes_in_one_ask(monkeypatch):
    import exmergo_dex_core.command_args as command_args_mod
    from exmergo_dex_core.maintain.commands import _combined_scan_handshake

    calls = []
    monkeypatch.setattr(
        command_args_mod,
        "confirmation_request",
        lambda command, adapter, estimate, **kwargs: (
            calls.append((estimate, kwargs)) or "offer"
        ),
    )
    row_ask, join_ask, facts_ask = _combined_scan_handshake(
        object(), lambda: (5.0, 2), lambda: (7.0, 3)
    )
    assert row_ask(10.0, 1) == "offer"
    assert join_ask(5.0, 2) == "offer"
    assert facts_ask(7.0, 3) == "offer"
    assert len(calls) == 1
    estimate, kwargs = calls[0]
    assert estimate == 22.0
    assert kwargs["per_table"] == {
        "(row counts)": 10.0,
        "(join overlap probes)": 5.0,
        "(declared facts)": 7.0,
    }
    assert kwargs["axes"] == ["row_population", "join_contract", "declared_facts"]
    assert "3 declared-fact check(s)" in kwargs["notes"][0]


def test_facts_alone_are_priced_once_when_nothing_else_asks(monkeypatch):
    import exmergo_dex_core.command_args as command_args_mod
    from exmergo_dex_core.maintain.commands import _combined_scan_handshake

    calls = []
    monkeypatch.setattr(
        command_args_mod,
        "confirmation_request",
        lambda command, adapter, estimate, **kwargs: (
            calls.append((estimate, kwargs)) or "offer"
        ),
    )
    lazy = []
    _row, _join, facts_ask = _combined_scan_handshake(
        object(), lambda: (5.0, 2), lambda: lazy.append(1) or (7.0, 3)
    )
    assert facts_ask(7.0, 3) == "offer"
    estimate, kwargs = calls[0]
    assert estimate == 7.0
    assert kwargs["axes"] == ["declared_facts"]
    assert lazy == []


def test_maintain_verify_reports_a_contradicted_population_end_to_end(
    maintain_repo, monkeypatch
):
    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": True,
            "messages": [],
        },
    )
    node = _model(
        "stg_orders",
        dex=_population(excludes=["returned"]),
        columns={"amount": {"unit": "percent"}},
        relation='"warehouse"."main"."stg_orders"',
    )
    target = maintain_repo.project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": {"model.maintain_test.stg_orders": node}}),
        encoding="utf-8",
    )
    (target / "run_results.json").write_text('{"results": []}', encoding="utf-8")
    rc, payload = maintain_repo.dex("maintain", "verify", "stg_orders")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = {f["code"]: f for f in payload["data"]["findings"]}
    assert findings["population_contradicted"]["data"]["contradicting_rows"] == 40
    assert "unit_out_of_range" not in findings  # amounts run well past 1
    suppressed = payload["data"]["suppressed"]
    assert "population" not in suppressed and "unit" not in suppressed
    assert "additive" in suppressed["additivity"]


def test_one_refused_statement_leaves_the_other_checks_standing(tmp_path: Path):
    """The additivity probe failing (a driver error, not a typed refusal) costs
    the additivity class its verdict and nothing else."""

    import exmergo_dex_core.explore.cumulative as cumulative_mod

    def refuse(*args, **kwargs):
        raise RuntimeError("Aggregate function with name 'lag' does not exist")

    original = cumulative_mod.measure_fractions
    cumulative_mod.measure_fractions = refuse
    try:
        result = _run(
            tmp_path,
            {
                "model.p.running": _model(
                    "running",
                    dex={"grain": ["customer_id", "placed_at"]},
                    columns=_additive("running_total", amount={"unit": "percent"}),
                )
            },
            _RUNNING,
        )
    finally:
        cumulative_mod.measure_fractions = original
    assert "unit" not in result.suppressed
    assert "does not exist" in result.suppressed["additivity"]
    assert any("could not be measured" in note for note in result.notes)


def test_an_additivity_finding_names_columns_as_declared(tmp_path: Path):
    """Snowflake folds unquoted names to upper case; the finding still speaks
    the declaration's spelling, as the unit and population findings do."""

    result = _run(
        tmp_path,
        {
            "model.p.running": _model(
                "running",
                dex={"grain": ["customer_id", "placed_at"]},
                columns=_additive("running_total"),
            )
        },
        _RUNNING.replace("AS running_total", 'AS "RUNNING_TOTAL"')
        .replace("AS customer_id", 'AS "CUSTOMER_ID"')
        .replace("AS placed_at", 'AS "PLACED_AT"'),
    )
    finding = result.findings[0]
    assert finding.column == "running_total"
    assert finding.data["entity"] == "customer_id"
    assert finding.data["ordered_by"] == "placed_at"
