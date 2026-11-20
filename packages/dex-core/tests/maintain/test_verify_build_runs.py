"""``maintain verify`` judges build status by the latest dated run (#1524).

Before, build status read the ``run_results.json`` beside the newest
``manifest.json``. A ``dbt parse`` rewrites the manifest and runs nothing, so a
hand-run ``dbt test`` that failed, followed by a Dex build that passed and a
parse, paired a fresh manifest with the hand run's failure and reported it as
current. Each node is now read from the latest building run that attempted it,
and each finding names that run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from exmergo_dex_core.maintain import verify as verify_mod
from exmergo_dex_core.transform.artifact_runtime import (
    isolated_artifact_dir,
    pinned_artifacts,
)
from exmergo_dex_core.transform.node_record import RECORD, record_node_runs

HAND = "11111111-1111-1111-1111-111111111111"
DEX_FIX = "22222222-2222-2222-2222-222222222222"
DEX_SEMANTIC = "33333333-3333-3333-3333-333333333333"
FOREIGN = "44444444-4444-4444-4444-444444444444"

MODEL = "model.shop.fct_daily"
OTHER = "model.shop.dim_days"
UNIT = "unit_test.shop.fct_daily.week_on_week_null_when_either_day_is_missing"
BINDER = (
    "Runtime Error in unit_test week_on_week_null_when_either_day_is_missing: "
    "Binder Error: Set operations can only apply to expressions with the same "
    "number of result columns"
)


def _manifest() -> dict:
    return {
        "nodes": {
            MODEL: {"name": "fct_daily", "resource_type": "model"},
            OTHER: {"name": "dim_days", "resource_type": "model"},
        },
        "unit_tests": {
            UNIT: {
                "name": "week_on_week_null_when_either_day_is_missing",
                "resource_type": "unit_test",
            }
        },
    }


def _write_run(
    directory: Path,
    rows: dict[str, str],
    *,
    invocation: str,
    generated_at: str,
    which: str = "build",
    mtime: float | None = None,
    message: str = "boom",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata": {"generated_at": generated_at, "invocation_id": invocation},
        "results": [
            {"unique_id": uid, "status": status, "message": message}
            for uid, status in rows.items()
        ],
        "args": {"which": which},
    }
    path = directory / "run_results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _write_manifest(directory: Path, *, mtime: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _log(project: Path, *invocations: str) -> None:
    logs = project / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"info": {"invocation_id": i}}) for i in invocations]
    (logs / "dbt.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("DBT_ENGINE_TARGET_PATH", "DBT_TARGET_PATH", "DBT_LOG_PATH"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: shop\n", encoding="utf-8")
    return root


def _dex(project: Path) -> Path:
    directory = isolated_artifact_dir(project)
    assert directory is not None
    return directory


def _issue_sequence(project: Path) -> None:
    """The issue's steps 1 to 4, as the files they leave on disk."""

    target = project / "target"
    # 1. a hand-run `dbt test` with the broken fixture.
    _write_run(
        target,
        {UNIT: "error"},
        invocation=HAND,
        generated_at="2026-11-20T09:00:00.000000Z",
        which="test",
        mtime=1_000,
        message=BINDER,
    )
    # 2. the fix, built by Dex: the unit test passes. Each Dex build records
    # its nodes, as `transform build` does after dbt returns.
    _write_run(
        _dex(project),
        {MODEL: "success", UNIT: "pass"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T10:00:00.000000Z",
        mtime=2_000,
    )
    record_node_runs(_dex(project))
    # 3. a semantic plan's build, which does not select the unit test. It writes
    # the same run_results.json, so step 2's file is gone.
    _write_run(
        _dex(project),
        {OTHER: "success"},
        invocation=DEX_SEMANTIC,
        generated_at="2026-11-20T11:00:00.000000Z",
        mtime=3_000,
    )
    record_node_runs(_dex(project))
    _write_manifest(_dex(project), mtime=3_000)
    # 4. a `dbt parse` refreshes the project's manifest and runs nothing.
    _write_manifest(target, mtime=4_000)
    _log(project, HAND, DEX_FIX, DEX_SEMANTIC)


def test_the_issue_sequence_reports_no_stale_failure(project: Path) -> None:
    """The issue's acceptance: steps 1 to 4 report no node_failed for the unit
    test, since the Dex build in step 2 attempted it after the hand run."""

    _issue_sequence(project)

    findings, notes = verify_mod.build_status_findings(project)

    assert findings == []
    results = verify_mod.node_results(project)
    row, run = results.nodes[UNIT]
    assert row["status"] == "pass"
    assert run.invocation_id == DEX_FIX
    assert run.source == "dex_record"
    # Step 3's build attempted none of the stale nodes, and is still the newest.
    assert results.newest.invocation_id == DEX_SEMANTIC
    assert notes == []


def test_a_failure_names_the_run_it_was_read_from(project: Path) -> None:
    """The second acceptance bullet: every node_failed names its run."""

    _write_run(
        _dex(project),
        {UNIT: "error"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T10:00:00.000000Z",
        message=BINDER,
    )
    _write_manifest(_dex(project), mtime=2_000)
    _log(project, DEX_FIX)

    [finding] = verify_mod.build_status_findings(project)[0]

    assert finding.code == "node_failed"
    assert finding.identifier == "week_on_week_null_when_either_day_is_missing"
    assert finding.data["run"] == {
        "invocation_id": DEX_FIX,
        "generated_at": "2026-11-20T10:00:00.000000Z",
        "command": "build",
        "artifact": "dex",
        "cold": False,
        "source": "run_results",
        "invoked_by": "dex",
        "latest": True,
    }
    assert f"dbt build run {DEX_FIX} at 2026-11-20T10:00:00" in finding.detail
    assert "started by Dex" in finding.detail
    assert "earlier run" not in finding.detail


def test_a_newer_failing_hand_run_is_reported_and_named_as_not_dex(
    project: Path,
) -> None:
    """The other order: the hand run is the newer attempt, so its failure is
    the current one, and it is marked as a run Dex did not start."""

    _write_run(
        _dex(project),
        {UNIT: "pass"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T09:00:00.000000Z",
        mtime=1_000,
    )
    _write_manifest(_dex(project), mtime=1_000)
    _write_run(
        project / "target",
        {UNIT: "error"},
        invocation=HAND,
        generated_at="2026-11-20T10:00:00.000000Z",
        which="test",
        mtime=2_000,
        message=BINDER,
    )
    _log(project, HAND, DEX_FIX)

    findings, notes = verify_mod.build_status_findings(project)

    [finding] = findings
    assert finding.code == "node_failed"
    assert finding.data["run"]["invoked_by"] == "outside_dex"
    assert finding.data["run"]["command"] == "test"
    assert finding.data["run"]["latest"] is True
    assert "not started by Dex" in finding.detail
    assert any("newest dbt run on disk" in n and HAND in n for n in notes)


def test_a_failure_no_later_run_attempted_is_marked_as_an_earlier_run(
    project: Path,
) -> None:
    """A node an older run failed and a later partial build never attempted
    has not been shown to pass, so it is still reported, labelled with the
    run it came from and the later one that skipped it."""

    _write_run(
        _dex(project),
        {MODEL: "error"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T09:00:00.000000Z",
        mtime=1_000,
    )
    _write_run(
        _dex(project) / "cold",
        {OTHER: "success"},
        invocation=DEX_SEMANTIC,
        generated_at="2026-11-20T10:00:00.000000Z",
        mtime=2_000,
    )
    _write_manifest(_dex(project), mtime=2_000)
    _log(project, DEX_FIX, DEX_SEMANTIC)

    [finding] = verify_mod.build_status_findings(project)[0]

    assert finding.identifier == "fct_daily"
    assert finding.severity == "high"
    assert finding.data["run"]["invocation_id"] == DEX_FIX
    assert finding.data["run"]["latest"] is False
    assert "earlier run than the latest" in finding.detail
    assert DEX_SEMANTIC in finding.detail


def test_a_compile_never_supersedes_a_build(project: Path) -> None:
    """A compile (Dex prices a build with one) writes run results too, where
    success means compiled; it says nothing about whether a node built."""

    _write_run(
        project / "target",
        {MODEL: "error"},
        invocation=HAND,
        generated_at="2026-11-20T09:00:00.000000Z",
    )
    _write_run(
        _dex(project),
        {MODEL: "success"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T10:00:00.000000Z",
        which="compile",
    )
    _write_manifest(_dex(project), mtime=2_000)

    [finding] = verify_mod.build_status_findings(project)[0]
    assert finding.data["run"]["invocation_id"] == HAND


def test_a_run_no_dbt_log_records_is_ignored_with_a_warning(project: Path) -> None:
    """Copied artifacts, or another checkout's target path: the log beside the
    project names other invocations, so these results are not this project's."""

    _write_run(
        project / "target",
        {MODEL: "error"},
        invocation=FOREIGN,
        generated_at="2026-11-20T10:00:00.000000Z",
    )
    _write_manifest(project / "target", mtime=1_000)
    _log(project, HAND)

    findings, notes = verify_mod.build_status_findings(project)

    assert findings == []
    assert any(FOREIGN in n and "no dbt log" in n for n in notes)
    assert any("no dbt run results found" in n for n in notes)
    results = verify_mod.node_results(project)
    assert results.summary()["ignored"][0]["reason"] == "not_logged"


def test_a_result_for_a_node_the_manifest_lost_is_not_reported(
    project: Path,
) -> None:
    _write_run(
        project / "target",
        {"model.shop.retired": "error", MODEL: "success"},
        invocation=HAND,
        generated_at="2026-11-20T10:00:00.000000Z",
    )
    _write_manifest(project / "target", mtime=1_000)

    findings, notes = verify_mod.build_status_findings(project)

    assert findings == []
    assert any("no longer has" in n for n in notes)


def test_a_build_s_own_sweep_reads_only_its_invocation(project: Path) -> None:
    """Inside `transform build --verify` the build's directory and invocation
    are pinned: a newer hand run's failure is not this build's."""

    _write_run(
        _dex(project),
        {MODEL: "success"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T09:00:00.000000Z",
    )
    _write_manifest(_dex(project), mtime=1_000)
    _write_run(
        project / "target",
        {MODEL: "error"},
        invocation=HAND,
        generated_at="2026-11-20T10:00:00.000000Z",
    )

    with pinned_artifacts(_dex(project), DEX_FIX):
        findings, _ = verify_mod.build_status_findings(project)
        assert findings == []
        results = verify_mod.node_results(project)
        assert [run.invocation_id for run in results.runs] == [DEX_FIX]
        assert results.describe(results.runs[0])["invoked_by"] == "dex"


def test_the_summary_names_every_run_read(project: Path) -> None:
    _issue_sequence(project)

    summary = verify_mod.node_results(project).summary()

    assert [run["invocation_id"] for run in summary["runs"]] == [
        HAND,
        DEX_FIX,
        DEX_SEMANTIC,
    ]
    by_id = {run["invocation_id"]: run for run in summary["runs"]}
    assert by_id[HAND]["invoked_by"] == "outside_dex"
    assert by_id[HAND]["nodes"] == 0
    assert by_id[DEX_FIX]["nodes"] == 2
    assert by_id[DEX_FIX]["source"] == "dex_record"
    assert by_id[DEX_SEMANTIC]["latest"] is True
    assert by_id[DEX_SEMANTIC]["source"] == "run_results"
    assert summary["ignored"] == []


def test_without_the_record_the_hand_run_is_marked_as_an_earlier_run(
    project: Path,
) -> None:
    """Results from before the record existed: the unit test's only attempt on
    disk is the hand run, so its failure stays, named as that run's and as
    earlier than the newest run, which did not attempt it."""

    _issue_sequence(project)
    (_dex(project) / RECORD).unlink()

    [finding] = verify_mod.build_status_findings(project)[0]

    assert finding.code == "node_failed"
    assert finding.data["run"]["invocation_id"] == HAND
    assert finding.data["run"]["generated_at"] == "2026-11-20T09:00:00.000000Z"
    assert finding.data["run"]["latest"] is False
    assert finding.data["run"]["invoked_by"] == "outside_dex"
    assert "earlier run than the latest" in finding.detail


def test_the_record_keeps_one_entry_per_node_and_no_message(project: Path) -> None:
    _issue_sequence(project)

    document = json.loads((_dex(project) / RECORD).read_text(encoding="utf-8"))

    assert set(document["nodes"]) == {MODEL, UNIT, OTHER}
    assert document["nodes"][UNIT] == {
        "status": "pass",
        "invocation_id": DEX_FIX,
        "generated_at": "2026-11-20T10:00:00.000000Z",
        "command": "build",
    }
    assert "Binder" not in json.dumps(document)


def test_an_older_file_never_overwrites_a_newer_record(project: Path) -> None:
    _issue_sequence(project)
    _write_run(
        _dex(project),
        {UNIT: "error"},
        invocation=HAND,
        generated_at="2026-11-19T09:00:00.000000Z",
    )
    record_node_runs(_dex(project))

    document = json.loads((_dex(project) / RECORD).read_text(encoding="utf-8"))
    assert document["nodes"][UNIT]["invocation_id"] == DEX_FIX


def test_a_compile_is_never_recorded(project: Path) -> None:
    _write_run(
        _dex(project),
        {MODEL: "success"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T10:00:00.000000Z",
        which="compile",
    )
    record_node_runs(_dex(project))

    assert not (_dex(project) / RECORD).exists()


def test_a_shared_target_path_cannot_say_who_ran(project: Path) -> None:
    _write_run(
        project / "target",
        {MODEL: "error"},
        invocation=HAND,
        generated_at="2026-11-20T10:00:00.000000Z",
    )
    _write_manifest(project / "target", mtime=1_000)

    results = verify_mod.node_results(project, artifact_policy="shared")
    [finding] = verify_mod.build_status_findings(project, results=results)[0]

    assert finding.data["run"]["invoked_by"] == "unknown"
    assert "shared target path" in finding.detail


def test_passed_grain_tests_reads_the_latest_attempt(project: Path) -> None:
    """A uniqueness test that failed by hand and then passed in a later Dex
    build proves the grain; the reverse does not."""

    test_id = "test.shop.unique_fct_daily_day.abc"
    manifest = _manifest()
    manifest["nodes"][test_id] = {
        "name": "unique_fct_daily_day",
        "resource_type": "test",
        "attached_node": MODEL,
        "test_metadata": {"name": "unique", "kwargs": {"column_name": "day"}},
    }
    _write_run(
        project / "target",
        {test_id: "fail"},
        invocation=HAND,
        generated_at="2026-11-20T09:00:00.000000Z",
        which="test",
    )
    _write_run(
        _dex(project),
        {test_id: "pass"},
        invocation=DEX_FIX,
        generated_at="2026-11-20T10:00:00.000000Z",
    )
    path = project / "target" / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert "fct_daily" in verify_mod.passed_grain_tests(project)
