"""``maintain coverage``'s last-run statuses, read from ``run_results.json`` (#1285)
and ``sources.json`` (#1347)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exmergo_dex_core.maintain import coverage_runs
from exmergo_dex_core.transform.artifact_runtime import isolated_artifact_dir

INV_A = "11111111-1111-1111-1111-111111111111"
INV_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)

NOT_NULL = "test.shop.not_null_orders_order_id.abc123"
UNIQUE = "test.shop.unique_orders_order_id.def456"
SINGULAR = "test.shop.assert_positive_amount"
UNIT = "unit_test.shop.orders.test_orders_full"
SRC_ORDERS = "source.shop.raw.orders"
SRC_CUSTOMERS = "source.shop.raw.customers"
SRC_EVENTS = "source.shop.raw.events"
SRC_PAYMENTS = "source.shop.raw.payments"


def _manifest(*extra_tests: str, extra_sources: tuple[str, ...] = ()) -> dict:
    nodes = {
        uid: {"resource_type": "test", "config": {"enabled": True}}
        for uid in (NOT_NULL, UNIQUE, SINGULAR, *extra_tests)
    }
    nodes["model.shop.orders"] = {"resource_type": "model", "config": {}}
    sources = {
        uid: {"resource_type": "source", "config": {"enabled": True}}
        for uid in (
            SRC_ORDERS,
            SRC_CUSTOMERS,
            SRC_EVENTS,
            SRC_PAYMENTS,
            *extra_sources,
        )
    }
    return {
        "nodes": nodes,
        "sources": sources,
        "unit_tests": {UNIT: {"config": {"enabled": True}}},
    }


def _write_results(
    directory: Path,
    rows: dict[str, str],
    *,
    invocation: str = INV_A,
    generated_at: str = "2026-11-15T11:00:00.000000Z",
    which: str = "build",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
            "dbt_version": "1.12.0",
            "generated_at": generated_at,
            "invocation_id": invocation,
        },
        "results": [
            {
                "unique_id": uid,
                "status": status,
                "message": "Got 3 results, configured to fail if != 0",
                "failures": 3,
                "compiled_code": "select secret_value from orders",
            }
            for uid, status in rows.items()
        ],
        "elapsed_time": 1.5,
        "args": {"which": which},
    }
    (directory / "run_results.json").write_text(json.dumps(document))


def _write_log(directory: Path, *invocations: str, text: bool = False) -> None:
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = []
    for invocation in invocations:
        if text:
            lines.append(
                f"============================== 2026-11-15 11:00:00.000000 | "
                f"{invocation} =============================="
            )
        else:
            lines.append(json.dumps({"info": {"invocation_id": invocation}}))
    (logs / "dbt.log").write_text("\n".join(lines) + "\n")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("DBT_ENGINE_TARGET_PATH", "DBT_TARGET_PATH", "DBT_LOG_PATH"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: shop\n")
    return root


def _dex_dir(project: Path) -> Path:
    directory = isolated_artifact_dir(project)
    assert directory is not None
    return directory


def test_reads_a_dex_build(project: Path) -> None:
    _write_results(
        _dex_dir(project),
        {
            NOT_NULL: "pass",
            UNIQUE: "fail",
            UNIT: "pass",
            "model.shop.orders": "success",
        },
    )
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "pass", UNIQUE: "fail", UNIT: "pass"}
    assert found.run == {
        "invocation_id": INV_A,
        "generated_at": "2026-11-15T11:00:00.000000Z",
        "command": "build",
        "artifact": "dex",
        "cold": False,
        "age_seconds": 3600,
    }
    assert found.warnings == []


def test_reads_a_plain_dbt_build_in_target(project: Path) -> None:
    _write_results(project / "target", {SINGULAR: "warn", UNIQUE: "error"})
    _write_log(project, INV_A, text=True)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {SINGULAR: "warn", UNIQUE: "error"}
    assert found.run is not None and found.run["artifact"] == "project"


def test_the_newer_of_the_two_wins(project: Path) -> None:
    _write_results(
        project / "target",
        {NOT_NULL: "fail"},
        invocation=INV_A,
        generated_at="2026-11-15T09:00:00Z",
    )
    _write_results(
        _dex_dir(project),
        {NOT_NULL: "pass"},
        invocation=INV_B,
        generated_at="2026-11-15T10:00:00Z",
    )
    _write_log(project, INV_A, INV_B)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "pass"}
    assert found.run is not None
    assert found.run["invocation_id"] == INV_B
    assert found.run["age_seconds"] == 7200


def test_a_compile_never_counts_as_a_test_run(project: Path) -> None:
    _write_results(
        project / "target", {NOT_NULL: "fail"}, generated_at="2026-11-15T09:00:00Z"
    )
    # Dex prices a build with a compile, which writes run_results too.
    _write_results(
        _dex_dir(project),
        {NOT_NULL: "success"},
        invocation=INV_B,
        generated_at="2026-11-15T10:00:00Z",
        which="compile",
    )
    _write_log(project, INV_A, INV_B)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "fail"}
    assert found.run is not None and found.run["invocation_id"] == INV_A


def test_a_test_added_after_the_run_is_absent(project: Path) -> None:
    added = "test.shop.accepted_values_orders_status.999"
    _write_results(_dex_dir(project), {NOT_NULL: "pass"})
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(added), now=NOW)

    assert added not in found.statuses
    assert coverage_runs.counts(
        [found.statuses.get(uid, "not_run") for uid in (NOT_NULL, added)]
    ) == {"pass": 1, "fail": 0, "warn": 0, "error": 0, "skipped": 0, "not_run": 1}


def test_a_skipped_test_is_skipped(project: Path) -> None:
    _write_results(_dex_dir(project), {UNIQUE: "skipped", UNIT: "skipped"})
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {UNIQUE: "skipped", UNIT: "skipped"}


def test_a_mismatched_invocation_is_not_attributed(project: Path) -> None:
    _write_results(_dex_dir(project), {NOT_NULL: "pass"}, invocation=INV_A)
    _write_log(project, INV_B)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {}
    assert found.run is not None and found.run["invocation_id"] == INV_A
    assert any(INV_A in warning for warning in found.warnings)


def test_a_rotated_log_still_confirms_the_invocation(project: Path) -> None:
    _write_results(_dex_dir(project), {NOT_NULL: "pass"}, invocation=INV_A)
    _write_log(project, INV_B)
    (project / "logs" / "dbt.log.1").write_text(
        json.dumps({"info": {"invocation_id": INV_A}}) + "\n"
    )
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "pass"}


def test_a_detached_build_log_beside_its_artifacts(project: Path) -> None:
    directory = _dex_dir(project)
    _write_results(directory, {NOT_NULL: "pass"})
    _write_log(directory, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "pass"}


def test_no_log_attributes_with_a_note(project: Path) -> None:
    _write_results(_dex_dir(project), {NOT_NULL: "pass"})
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "pass"}
    assert any("no dbt log" in note for note in found.notes)


def test_a_result_for_a_removed_test_is_dropped(project: Path) -> None:
    gone = "test.shop.not_null_orders_legacy_id.777"
    disabled = "test.shop.assert_disabled"
    manifest = _manifest(disabled)
    manifest["nodes"][disabled]["config"]["enabled"] = False
    _write_results(
        _dex_dir(project), {gone: "fail", disabled: "fail", NOT_NULL: "pass"}
    )
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, manifest, now=NOW)

    assert found.statuses == {NOT_NULL: "pass"}
    assert any("2 test result(s)" in note for note in found.notes)


def test_no_run_results_is_a_note(project: Path) -> None:
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {}
    assert found.run is None
    assert found.warnings == []
    assert found.notes and "not_run" in found.notes[0]


def test_a_cold_deps_build_is_read(project: Path) -> None:
    _write_results(
        _dex_dir(project) / "cold",
        {NOT_NULL: "fail"},
        generated_at="2026-11-15T11:30:00Z",
    )
    _write_results(_dex_dir(project), {NOT_NULL: "pass"})
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "fail"}
    assert found.run is not None and found.run["cold"] is True


def test_runtime_error_spelling_and_unknown_statuses(project: Path) -> None:
    _write_results(_dex_dir(project), {NOT_NULL: "runtime error", UNIQUE: "no-op"})
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "error"}
    assert any("does not recognize" in note for note in found.notes)


def test_nothing_from_a_result_row_but_id_and_status(project: Path) -> None:
    _write_results(_dex_dir(project), {UNIQUE: "fail"})
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    rendered = json.dumps(
        {"s": found.statuses, "r": found.run, "n": found.notes, "w": found.warnings}
    )
    for leaked in ("secret_value", "Got 3 results", "failures"):
        assert leaked not in rendered
    # Worded as one dated run, never as the current state.
    assert "recent" not in rendered and "current" not in rendered


def test_target_path_from_environment(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("DBT_TARGET_PATH", str(elsewhere))
    _write_results(elsewhere, {NOT_NULL: "pass"})
    _write_log(project, INV_A)
    found = coverage_runs.last_run(project, _manifest(), now=NOW)

    assert found.statuses == {NOT_NULL: "pass"}
    assert os.fspath(elsewhere) not in json.dumps(found.run)


def _write_sources(
    directory: Path,
    rows: dict[str, str],
    *,
    invocation: str = INV_A,
    generated_at: str = "2026-11-15T11:15:00.000000Z",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/sources/v3.json",
            "dbt_version": "1.12.0",
            "generated_at": generated_at,
            "invocation_id": invocation,
        },
        "results": [
            {
                "unique_id": uid,
                "status": status,
                "max_loaded_at": "2026-11-15T08:42:19.000000Z",
                "snapshotted_at": "2026-11-15T11:15:00.000000Z",
                "max_loaded_at_time_ago_in_s": 9161.0,
                "criteria": {
                    "warn_after": {"count": 12, "period": "hour"},
                    "error_after": {"count": 24, "period": "hour"},
                    "filter": "secret_tenant = 'vip'",
                },
                "message": "Database Error in source: secret_warehouse_detail",
                "error": "Runtime Error: secret_warehouse_detail",
            }
            for uid, status in rows.items()
        ],
        "elapsed_time": 0.8,
    }
    (directory / "sources.json").write_text(json.dumps(document))


def test_freshness_reads_dex_isolated_sources_json(project: Path) -> None:
    _write_sources(
        _dex_dir(project),
        {
            SRC_ORDERS: "pass",
            SRC_CUSTOMERS: "warn",
            SRC_EVENTS: "error",
            SRC_PAYMENTS: "runtime error",
        },
    )
    _write_log(project, INV_A)
    found = coverage_runs.freshness_run(project, _manifest(), now=NOW)

    assert found.statuses == {
        SRC_ORDERS: "pass",
        SRC_CUSTOMERS: "warn",
        SRC_EVENTS: "error",
        SRC_PAYMENTS: "error",
    }
    assert found.run == {
        "invocation_id": INV_A,
        "generated_at": "2026-11-15T11:15:00.000000Z",
        "artifact": "dex",
        "cold": False,
        "age_seconds": 2700,
    }
    assert found.warnings == []


def test_freshness_reads_project_target_and_picks_newer(project: Path) -> None:
    _write_sources(
        _dex_dir(project),
        {SRC_ORDERS: "error"},
        invocation=INV_A,
        generated_at="2026-11-15T09:00:00Z",
    )
    _write_sources(
        project / "target",
        {SRC_ORDERS: "pass"},
        invocation=INV_B,
        generated_at="2026-11-15T10:30:00Z",
    )
    _write_log(project, INV_A, INV_B)
    found = coverage_runs.freshness_run(project, _manifest(), now=NOW)

    assert found.statuses == {SRC_ORDERS: "pass"}
    assert found.run == {
        "invocation_id": INV_B,
        "generated_at": "2026-11-15T10:30:00Z",
        "artifact": "project",
        "cold": False,
        "age_seconds": 5400,
    }


def test_freshness_reads_cold_deps_and_target_env(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("DBT_TARGET_PATH", str(elsewhere))
    _write_sources(
        elsewhere / "cold",
        {SRC_ORDERS: "warn"},
        generated_at="2026-11-15T11:40:00Z",
    )
    _write_log(project, INV_A)
    found = coverage_runs.freshness_run(project, _manifest(), now=NOW)

    assert found.statuses == {SRC_ORDERS: "warn"}
    assert found.run is not None
    assert found.run["artifact"] == "project"
    assert found.run["cold"] is True
    assert os.fspath(elsewhere) not in json.dumps(found.run)


def test_freshness_drops_removed_and_disabled_sources_and_unknown_status(
    project: Path,
) -> None:
    gone = "source.shop.raw.legacy"
    disabled = "source.shop.raw.archived"
    manifest = _manifest(extra_sources=(disabled,))
    manifest["sources"][disabled]["config"]["enabled"] = False
    _write_sources(
        _dex_dir(project),
        {
            gone: "error",
            disabled: "error",
            SRC_ORDERS: "pass",
            SRC_CUSTOMERS: "no-op",
        },
    )
    _write_log(project, INV_A)
    found = coverage_runs.freshness_run(project, manifest, now=NOW)

    assert found.statuses == {SRC_ORDERS: "pass"}
    assert SRC_EVENTS not in found.statuses
    assert any("2 freshness result(s)" in note for note in found.notes)
    assert any("does not recognize" in note for note in found.notes)


def test_freshness_mismatched_invocation_warns_and_attributes_nothing(
    project: Path,
) -> None:
    _write_sources(_dex_dir(project), {SRC_ORDERS: "pass"}, invocation=INV_A)
    _write_log(project, INV_B)
    found = coverage_runs.freshness_run(project, _manifest(), now=NOW)

    assert found.statuses == {}
    assert found.run is not None and found.run["invocation_id"] == INV_A
    assert any(INV_A in warning for warning in found.warnings)


def test_no_sources_json_is_a_note_and_not_an_error(project: Path) -> None:
    found = coverage_runs.freshness_run(project, _manifest(), now=NOW)

    assert found.statuses == {}
    assert found.run is None
    assert found.warnings == []
    assert found.notes
    assert "sources.json" in found.notes[0] and "not_run" in found.notes[0]


def test_freshness_leaks_nothing_from_result_rows_and_never_says_current(
    project: Path,
) -> None:
    _write_sources(_dex_dir(project), {SRC_ORDERS: "runtime error"})
    _write_log(project, INV_A)
    found = coverage_runs.freshness_run(project, _manifest(), now=NOW)

    rendered = json.dumps(
        {"s": found.statuses, "r": found.run, "n": found.notes, "w": found.warnings}
    )
    for leaked in (
        "max_loaded_at",
        "2026-11-15T08:42:19",
        "snapshotted_at",
        "9161",
        "secret_tenant",
        "secret_warehouse_detail",
    ):
        assert leaked not in rendered
    assert "recent" not in rendered and "current" not in rendered
