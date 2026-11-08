"""Completion obligations for the evidence a green build does not carry.

Two of them, both about a change meant to move rows:

- ``incremental_replay`` (#1130): an incremental model is two programs, and a
  ``--full-refresh`` runs only the full one. The obligation is satisfied by a
  unit test that overrides ``is_incremental`` to true and passed in the build.
- ``comparison`` (#1157): a model the plan rewrote, held to the
  ``transform compare`` verdict the plan recorded. An intended change is
  compared with ``--expect``; one that went in uncompared stays open.

Neither changes ``outcome``: obligations are listed, never a status.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.dbt_artifacts import dex_target
from tests.transform.test_build import _fake_runner_factory, _run
from tests.transform.test_build_completion import _evidence, _uid

from exmergo_dex_core.edits import EditOp, content_hash
from exmergo_dex_core.transform.completion import (
    COMPLETION_BUDGET_BYTES,
    ComparisonState,
    summarize_completion,
)
from exmergo_dex_core.transform.evidence import BuildNode
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, TransformPlan

FCT = "fct_scans"
TEST_UID = f"unit_test.dex_test.{FCT}.test_{FCT}_incremental"


def _replay(**facts):
    return {FCT: {"tests": [TEST_UID], "full_refreshed": False, **facts}}


def _nodes(test_status: str | None = None) -> list[BuildNode]:
    nodes = [BuildNode(unique_id=_uid(FCT), name=FCT, status="success")]
    if test_status is not None:
        nodes.append(
            BuildNode(
                unique_id=TEST_UID, name=f"test_{FCT}_incremental", status=test_status
            )
        )
    return nodes


def _obligations(nodes, **kwargs):
    completion = summarize_completion(_evidence(nodes, required=[FCT]), None, **kwargs)
    return completion, completion.data()["obligations"]


# --- incremental_replay (#1130) ----------------------------------------------


def test_a_passing_incremental_unit_test_satisfies_the_replay():
    completion, obligations = _obligations(_nodes("pass"), incremental_replay=_replay())
    assert obligations["incremental_replay"] == [
        {"model": FCT, "kind": "incremental_replay", "status": "satisfied"}
    ]
    assert completion.outcome == "validated"


def test_a_full_refresh_alone_leaves_the_replay_unchecked():
    completion, obligations = _obligations(
        _nodes(), incremental_replay=_replay(full_refreshed=True)
    )
    assert obligations["incremental_replay"] == [
        {
            "model": FCT,
            "kind": "incremental_replay",
            "status": "unchecked",
            "reason_code": "full_refresh_only",
        }
    ]
    # Listed, never a status: the outcome is still dbt's and the coverage's.
    assert completion.outcome == "validated"


def test_no_incremental_test_in_the_build_is_named():
    _, obligations = _obligations(_nodes(), incremental_replay=_replay(tests=[]))
    assert obligations["incremental_replay"][0]["reason_code"] == (
        "no_incremental_test"
    )


def test_a_full_refresh_with_a_passing_test_is_satisfied():
    _, obligations = _obligations(
        _nodes("pass"), incremental_replay=_replay(full_refreshed=True)
    )
    assert obligations["incremental_replay"][0]["status"] == "satisfied"


def test_a_failing_incremental_test_contradicts_the_replay():
    _, obligations = _obligations(_nodes("fail"), incremental_replay=_replay())
    assert obligations["incremental_replay"] == [
        {
            "model": FCT,
            "kind": "incremental_replay",
            "status": "contradicted",
            "finding": "incremental_test_failed",
        }
    ]


@pytest.mark.parametrize("failed", ["fail", "error", "runtime error", "failed"])
def test_a_failing_test_beside_a_passing_one_contradicts_the_replay(failed):
    other = f"unit_test.dex_test.{FCT}.test_{FCT}_incremental_late"
    nodes = [
        *_nodes("pass"),
        BuildNode(unique_id=other, name=f"test_{FCT}_incremental_late", status=failed),
    ]
    _, obligations = _obligations(
        nodes, incremental_replay=_replay(tests=[TEST_UID, other])
    )
    assert obligations["incremental_replay"][0]["status"] == "contradicted"


def test_a_plan_touching_no_incremental_model_carries_no_replay_key():
    _, obligations = _obligations(_nodes(), incremental_replay=None)
    assert "incremental_replay" not in obligations
    _, obligations = _obligations(_nodes(), incremental_replay={})
    assert "incremental_replay" not in obligations


def test_an_incremental_model_the_change_does_not_require_is_left_out():
    _, obligations = _obligations(
        _nodes(), incremental_replay={"fct_other": {"tests": [], "answers": []}}
    )
    assert "incremental_replay" not in obligations


def test_a_required_name_matches_any_of_the_models_answers():
    nodes = [BuildNode(unique_id=_uid(FCT), name=FCT, status="success")]
    completion = summarize_completion(
        _evidence(nodes, required=["fct_scans_v2"]),
        None,
        incremental_replay={
            "fct_scans.v2": {"tests": [], "answers": ["fct_scans.v2", "fct_scans_v2"]}
        },
    )
    assert [o.model for o in completion.obligations.incremental_replay] == [
        "fct_scans.v2"
    ]


def test_many_replay_obligations_fit_the_bound_and_count_the_cut():
    names = [f"fct_incremental_model_with_a_long_name_{i:03d}" for i in range(400)]
    nodes = [BuildNode(unique_id=_uid(n), name=n, status="success") for n in names]
    completion = summarize_completion(
        _evidence(nodes, required=names),
        None,
        incremental_replay={n: {"tests": [], "full_refreshed": True} for n in names},
    )
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert len(completion.obligations.incremental_replay) >= 1
    assert completion.elided["obligations.incremental_replay"] == 400 - len(
        completion.obligations.incremental_replay
    )


# --- comparison (#1157) -------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "status", "finding", "reason"),
    [
        ("equivalent", "satisfied", None, None),
        ("as_expected", "satisfied", None, None),
        ("different", "contradicted", "different", None),
        ("unexpected", "contradicted", "unexpected_change", None),
        ("not_compared", "unchecked", None, "not_compared"),
    ],
)
def test_a_recorded_verdict_is_the_comparison_obligation(
    verdict, status, finding, reason
):
    _, obligations = _obligations(
        _nodes(), comparisons=[ComparisonState(model=FCT, status=verdict)]
    )
    expected = {"model": FCT, "kind": "comparison", "status": status}
    if finding:
        expected["finding"] = finding
    if reason:
        expected["reason_code"] = reason
    assert obligations["comparison"] == [expected]


def test_a_not_checked_verdict_keeps_its_reason():
    _, obligations = _obligations(
        _nodes(),
        comparisons=[
            ComparisonState(
                model=FCT, status="not_checked", reason_code="inputs_unsynchronized"
            )
        ],
    )
    assert obligations["comparison"][0]["status"] == "unchecked"
    assert obligations["comparison"][0]["reason_code"] == "inputs_unsynchronized"


def test_an_uncompared_rewrite_beside_a_compared_one_says_so():
    nodes = [
        BuildNode(unique_id=_uid(n), name=n, status="success")
        for n in ("int_scans", FCT)
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["int_scans", FCT]),
        None,
        comparisons=[
            ComparisonState(model="int_scans", status="not_compared"),
            ComparisonState(model=FCT, status="equivalent"),
        ],
    )
    by_model = {o.model: o for o in completion.obligations.comparison}
    assert by_model["int_scans"].reason_code == "compared_elsewhere"
    assert by_model[FCT].status == "satisfied"


def test_no_recorded_comparisons_leaves_no_comparison_key():
    _, obligations = _obligations(_nodes(), comparisons=None)
    assert "comparison" not in obligations


# --- through the CLI ------------------------------------------------------------

_SQL = "select * from {{ source('feed', 'scans') }}\n"


def _manifest(with_test: bool) -> dict:
    manifest = {
        "metadata": {
            "generated_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        },
        "nodes": {
            _uid(FCT): {
                "unique_id": _uid(FCT),
                "name": FCT,
                "resource_type": "model",
                "original_file_path": f"models/marts/{FCT}.sql",
                "config": {"materialized": "incremental"},
                "depends_on": {"nodes": []},
            }
        },
        "unit_tests": {},
    }
    if with_test:
        manifest["unit_tests"][TEST_UID] = {
            "name": f"test_{FCT}_incremental",
            "model": FCT,
            "depends_on": {"nodes": [_uid(FCT)]},
            "overrides": {"macros": {"is_incremental": True}},
        }
    return manifest


def _write(directory: Path, with_test: bool, test_status: str = "pass") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(_manifest(with_test)), encoding="utf-8"
    )
    results = [{"unique_id": _uid(FCT), "status": "success"}]
    if with_test:
        results.append({"unique_id": TEST_UID, "status": test_status})
    (directory / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "invocation_id": "inv-replay",
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                "elapsed_time": 1.0,
                "results": results,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def incremental_plan(dbt_project_dir: Path, tmp_path: Path) -> tuple[Path, str]:
    marts = dbt_project_dir / "models" / "marts"
    marts.mkdir()
    (marts / f"{FCT}.sql").write_text(_SQL, encoding="utf-8")
    plan = TransformPlan(
        plan_id="preplay",
        created_at=datetime.now(UTC).isoformat(),
        intent="pick up scans delivered on the watermark",
        project_dir="analytics",
        edits=[
            PlanEdit(
                path=f"models/marts/{FCT}.sql",
                op=EditOp.UPSERT,
                kind=EditKind.MODEL_SQL,
                new_content=_SQL,
                old_content_hash=content_hash(_SQL),
            )
        ],
        applied_at=datetime.now(UTC).isoformat(),
        refactor_models={FCT: "attributed"},
    )
    plans = tmp_path / ".dex" / "plans"
    plans.mkdir(parents=True)
    (plans / "preplay.json").write_text(plan.model_dump_json(), encoding="utf-8")
    return dbt_project_dir, "preplay"


def _build(
    tmp_path, capsys, monkeypatch, project, *extra, with_test=False, status="pass"
):
    def write(argv):
        if "--target-path" in argv:
            _write(Path(argv[argv.index("--target-path") + 1]), with_test, status)
        _write(dex_target(project), with_test, status)

    _fake_runner_factory(monkeypatch, on_run=write)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--for-plan",
            "preplay",
            *extra,
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    return envelope["data"]["completion"]


def test_a_full_refresh_build_of_an_edited_incremental_model_is_unchecked(
    incremental_plan, tmp_path, capsys, monkeypatch
):
    project, _ = incremental_plan
    completion = _build(tmp_path, capsys, monkeypatch, project, "--full-refresh")
    assert completion["outcome"] == "validated"
    assert completion["obligations"]["incremental_replay"] == [
        {
            "model": FCT,
            "kind": "incremental_replay",
            "status": "unchecked",
            "reason_code": "full_refresh_only",
        }
    ]
    # The rewrite went in with no comparison recorded.
    assert completion["obligations"]["comparison"] == [
        {
            "model": FCT,
            "kind": "comparison",
            "status": "unchecked",
            "reason_code": "not_compared",
        }
    ]


def test_the_same_build_with_a_passing_incremental_test_is_satisfied(
    incremental_plan, tmp_path, capsys, monkeypatch
):
    project, _ = incremental_plan
    completion = _build(
        tmp_path, capsys, monkeypatch, project, "--full-refresh", with_test=True
    )
    assert completion["obligations"]["incremental_replay"] == [
        {"model": FCT, "kind": "incremental_replay", "status": "satisfied"}
    ]


def test_a_plain_build_with_no_incremental_test_names_that(
    incremental_plan, tmp_path, capsys, monkeypatch
):
    project, _ = incremental_plan
    completion = _build(tmp_path, capsys, monkeypatch, project)
    assert completion["obligations"]["incremental_replay"][0]["reason_code"] == (
        "no_incremental_test"
    )
