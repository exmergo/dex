"""The bounded completion block that leads a build payload (#855).

The failure this closes is a reader problem, not a detector one: the 1.17.1
benchmark agents read build envelopes through `head -c` or a slice, and a
project of a few hundred staging models put every node ahead of the evidence
that mattered. So the central assertion here is about the serialized prefix
itself, not only about a field being present somewhere in the payload: the
first 4 KiB of the envelope has to say that the requested report was skipped,
and which node stopped it.

Everything is driven from synthesized dbt artifacts; the live half is the
dogfood.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.dbt_artifacts import dex_target
from tests.transform.test_build import _fake_runner_factory, _run

from exmergo_dex_core.edits import EditOp, content_hash
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.transform.completion import (
    COMPLETION_BUDGET_BYTES,
    ChangeRef,
    ComparisonState,
    summarize_completion,
)
from exmergo_dex_core.transform.evidence import (
    BuildCoverage,
    BuildEvidence,
    BuildNode,
    BuildOutcome,
    BuildSelection,
    PlanDrift,
    recompute_outcome,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, TransformPlan
from exmergo_dex_core.transform.results import BuildResult

REPORT = "rpt_team_activity"
PREREQ = "int_team_members"
PREFIX = 4096


def _uid(name: str) -> str:
    return f"model.dex_test.{name}"


def _evidence(
    nodes: list[BuildNode],
    *,
    required: list[str] | None = None,
    drift: list[PlanDrift] | None = None,
    stale: bool | None = False,
    undetermined: bool = False,
) -> BuildEvidence:
    coverage = None
    if required is not None:
        answers = {n.name for n in nodes}
        coverage = BuildCoverage(
            required=required,
            covered=[r for r in required if r in answers],
            missing=[r for r in required if r not in answers],
            undetermined=undetermined,
        )
    draft = BuildEvidence.model_construct(
        invocation={"invocation_id": "inv-1"},
        nodes=nodes,
        selection=BuildSelection(matched=[n.name for n in nodes], empty=not nodes),
        coverage=coverage,
        plan_drift=drift or [],
        stale_artifacts=stale,
    )
    return BuildEvidence(
        outcome=recompute_outcome(draft),
        invocation={"invocation_id": "inv-1"},
        nodes=nodes,
        selection=draft.selection,
        coverage=coverage,
        plan_drift=drift or [],
        stale_artifacts=stale,
    )


def _staging(count: int) -> list[BuildNode]:
    return [
        BuildNode(
            unique_id=_uid(f"stg_source_{i:03d}"),
            name=f"stg_source_{i:03d}",
            resource_type="model",
            status="success",
            execution_time=0.05,
            relation=f'"dev"."main"."stg_source_{i:03d}"',
        )
        for i in range(count)
    ]


def _blocked_nodes() -> list[BuildNode]:
    return [
        *_staging(300),
        BuildNode(
            unique_id=_uid(PREREQ),
            name=PREREQ,
            resource_type="model",
            status="error",
            message="Runtime Error in model int_team_members: Conversion Error: "
            "Could not convert string 'n/a' to INT32" + " detail" * 80,
        ),
        BuildNode(
            unique_id=_uid(REPORT),
            name=REPORT,
            resource_type="model",
            status="skipped",
        ),
    ]


GRAPH = {_uid(REPORT): [_uid(PREREQ)], _uid(PREREQ): [_uid("stg_source_001")]}


def _serialized(result: BuildResult) -> str:
    return json.dumps(to_envelope(result).model_dump(mode="json"))


def test_a_skipped_deliverable_behind_300_successes_is_in_the_first_4_kib():
    evidence = _evidence(_blocked_nodes(), required=[REPORT])
    completion = summarize_completion(
        evidence,
        {"ran": False, "reason": "not requested; re-run with --verify to sweep"},
        process={"success": False, "returncode": 1},
        change=ChangeRef(plan_id="p1", source="for_plan"),
        graph=GRAPH,
    )
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    deliverables = completion.deliverables
    assert deliverables.coverage == "known"
    assert deliverables.required == 1
    assert deliverables.skipped.names == [REPORT]
    assert deliverables.built.count == 0
    # The skip's cause is the node that failed, walked through the graph.
    assert deliverables.blocked[0].cause == PREREQ
    assert completion.blockers[0].node == PREREQ
    # The message is bounded here; the full text stays in data.evidence.
    assert len(completion.blockers[0].message or "") <= 160
    assert "predates this run" in (deliverables.note or "")
    # Not a statement that the build was clean: nothing verified anything.
    assert completion.verification.ran is False
    assert completion.obligations.checked is False

    result = BuildResult(
        success=False,
        summary={
            "target": "dev",
            "success": False,
            "returncode": 1,
            "nodes": [n.model_dump(exclude_none=True) for n in evidence.nodes],
        },
        evidence=evidence,
        completion=completion,
    )
    text = _serialized(result)
    prefix = text[:PREFIX]
    assert prefix.startswith('{"status": "ok", "data": {"completion": ')
    assert f'"skipped": {{"count": 1, "names": ["{REPORT}"]' in prefix
    assert f'"cause": "{PREREQ}"' in prefix
    # The whole block is in the prefix, and every node comes after it.
    block = json.dumps(to_envelope(result).data["completion"])
    assert block in prefix
    assert text.index('"nodes"') > text.index(block) + len(block)


def test_no_plan_says_coverage_is_unknown_rather_than_empty():
    evidence = _evidence(_staging(3))
    completion = summarize_completion(evidence, {"ran": False, "reason": "x"})
    assert completion.deliverables.coverage == "unknown"
    assert completion.deliverables.required == 0
    assert "--for-plan" in (completion.deliverables.note or "")
    # Without a plan, assurance is over what built.
    assert completion.assurance.basis == "built_models"
    assert completion.assurance.build_only.count == 3


def test_an_empty_selection_is_named_and_validates_nothing():
    evidence = _evidence([])
    completion = summarize_completion(evidence, {"ran": False, "reason": "x"})
    assert completion.outcome == "empty_selection"
    assert completion.process["node_counts"] == {}
    assert completion.deliverables.built.count == 0


def test_a_plan_whose_edits_name_no_node_is_undetermined():
    evidence = _evidence(_staging(2), required=[], undetermined=True)
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "coverage_undetermined"
    assert completion.deliverables.coverage == "undetermined"


def test_drift_and_stale_artifacts_are_stale_not_newly_verified():
    nodes = [BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success")]
    evidence = _evidence(
        nodes,
        required=[REPORT],
        drift=[PlanDrift(path=f"models/{REPORT}.sql")],
        stale=True,
    )
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "stale"
    assert completion.deliverables.stale.count == 2
    assert f"models/{REPORT}.sql" in completion.deliverables.stale.names


def test_a_missing_required_node_is_missing_not_built():
    evidence = _evidence(_staging(2), required=[REPORT, "stg_source_000"])
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "partial"
    assert completion.deliverables.missing.names == [REPORT]
    assert completion.deliverables.built.names == ["stg_source_000"]


def test_a_constant_answer_model_with_no_check_is_only_build_validated():
    nodes = [
        BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success"),
        BuildNode(unique_id=_uid("checked"), name="checked", status="success"),
        BuildNode(
            unique_id="test.dex_test.not_null_checked_id.abc123",
            name="not_null_checked_id",
            resource_type="test",
            status="pass",
        ),
    ]
    evidence = _evidence(nodes, required=[REPORT, "checked"])
    graph = {"test.dex_test.not_null_checked_id.abc123": [_uid("checked")]}
    completion = summarize_completion(evidence, None, graph=graph)
    assert completion.outcome == "validated"
    assert completion.assurance.basis == "required"
    assert completion.assurance.build_only.names == [REPORT]
    assert completion.assurance.asserted.names == ["checked"]


def test_a_satisfied_record_counts_as_asserted_and_obligations_are_counted():
    nodes = [BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success")]
    evidence = _evidence(nodes, required=[REPORT])
    verification = {
        "ran": True,
        "scope": [REPORT],
        "findings": [],
        "finding_count": 0,
        "suppressed": {"temporal": "nothing declares a history"},
        "parent_population": [{"model": REPORT, "status": "satisfied"}],
        "join_coverage": [
            {"model": REPORT, "status": "unchecked", "reason_code": "filtered_cte"},
            {"model": "other", "status": "contradicted", "finding": "join_orphans"},
        ],
    }
    completion = summarize_completion(evidence, verification)
    assert completion.assurance.asserted.names == [REPORT]
    assert completion.verification.ran is True
    assert completion.verification.finding_count == 0
    # A clean findings list is not coverage: one class was suppressed and one
    # obligation stayed unchecked, and both are in the bounded block.
    assert completion.verification.suppressed == ["temporal"]
    assert completion.obligations.checked is True
    assert completion.obligations.by_status == {
        "contradicted": 1,
        "satisfied": 1,
        "unchecked": 1,
    }
    assert completion.obligations.unchecked[0].reason_code == "filtered_cte"
    # A contradiction says what was wrong without a trip to data.verification.
    assert completion.obligations.contradicted[0].finding == "join_orphans"


def test_findings_are_counted_including_the_ones_the_cap_dropped():
    nodes = [BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success")]
    evidence = _evidence(nodes, required=[REPORT])
    findings = [
        {"code": "row_loss", "identifier": f"m{i}", "severity": "high"}
        for i in range(8)
    ]
    completion = summarize_completion(
        evidence,
        {"ran": True, "findings": findings, "suppressed": {}},
        findings_dropped=12,
    )
    assert completion.verification.finding_count == 20
    assert completion.verification.findings_dropped == 12
    assert completion.verification.by_severity == {"high": 8}
    assert len(completion.verification.top) == 5


def test_long_lists_are_capped_and_every_cut_is_counted():
    failing = [
        BuildNode(
            unique_id=_uid(f"broken_{i:02d}"),
            name=f"broken_{i:02d}",
            status="error",
            message="boom " * 60,
        )
        for i in range(40)
    ]
    evidence = _evidence(failing, required=[n.name for n in failing])
    completion = summarize_completion(evidence, None)
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert completion.deliverables.failed.count == 40
    assert len(completion.deliverables.failed.names) <= 5
    assert completion.deliverables.failed.elided == 40 - len(
        completion.deliverables.failed.names
    )
    assert completion.elided["blockers"] >= 35


def test_a_tight_budget_trims_names_and_keeps_counts():
    evidence = _evidence(_blocked_nodes(), required=[REPORT])
    completion = summarize_completion(evidence, None, graph=GRAPH, budget=1400)
    assert completion.size() <= 1400
    assert completion.deliverables.skipped.names == [REPORT]
    assert completion.deliverables.blocked[0].cause == PREREQ
    assert completion.elided


def test_recorded_comparisons_ride_in_the_block():
    nodes = [BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success")]
    completion = summarize_completion(
        _evidence(nodes, required=[REPORT]),
        None,
        comparisons=[ComparisonState(model=REPORT, status="not_compared")],
    )
    assert completion.data()["refactor_comparison"] == [
        {"model": REPORT, "status": "not_compared"}
    ]


def test_a_versioned_model_answers_to_its_file_stem():
    nodes = [
        BuildNode(
            unique_id="model.dex_test.dim_customers.v2",
            name="dim_customers.v2",
            status="success",
        )
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["dim_customers_v2"]), None
    )
    assert completion.deliverables.built.names == ["dim_customers_v2"]


def test_a_skip_behind_a_failing_data_test_names_the_test():
    """`dbt build` skips a model's children when a test on it fails, and the
    test is not their parent."""

    nodes = [
        BuildNode(unique_id=_uid(PREREQ), name=PREREQ, status="success"),
        BuildNode(
            unique_id="test.dex_test.unique_int_team_members_id.9f1",
            name="unique_int_team_members_id",
            resource_type="test",
            status="fail",
        ),
        BuildNode(unique_id=_uid(REPORT), name=REPORT, status="skipped"),
    ]
    graph = {
        _uid(REPORT): [_uid(PREREQ)],
        "test.dex_test.unique_int_team_members_id.9f1": [_uid(PREREQ)],
    }
    completion = summarize_completion(
        _evidence(nodes, required=[REPORT]), None, graph=graph
    )
    assert completion.deliverables.blocked[0].cause == "unique_int_team_members_id"
    assert completion.blockers[0].node == "unique_int_team_members_id"


def test_a_failed_check_is_not_build_only_and_a_unit_test_is_not_a_row_check():
    nodes = [
        BuildNode(unique_id=_uid("failing"), name="failing", status="success"),
        BuildNode(unique_id=_uid("unit_only"), name="unit_only", status="success"),
        BuildNode(
            unique_id="test.dex_test.not_null_failing_id.1",
            name="not_null_failing_id",
            resource_type="test",
            status="fail",
        ),
        BuildNode(
            unique_id="unit_test.dex_test.unit_only.checks_math",
            name="checks_math",
            resource_type="unit_test",
            status="pass",
        ),
    ]
    graph = {
        "test.dex_test.not_null_failing_id.1": [_uid("failing")],
        "unit_test.dex_test.unit_only.checks_math": [_uid("unit_only")],
    }
    completion = summarize_completion(
        _evidence(nodes, required=["failing", "unit_only"]), None, graph=graph
    )
    assert completion.assurance.failed_checks.names == ["failing"]
    # A unit test reads fixtures: the built rows are unchecked.
    assert completion.assurance.build_only.names == ["unit_only"]


def test_failed_checks_say_which_kind_of_check_failed():
    """A join measured against a default threshold is not a failed test: each
    failure names its kind, and a join's names its finding and the join
    (#1162)."""

    join = 'inner join "dev"."main"."handles" AS h on m.member_id = h.member_id'
    nodes = [
        BuildNode(unique_id=_uid("mart"), name="mart", status="success"),
        BuildNode(unique_id=_uid("tested"), name="tested", status="success"),
        BuildNode(
            unique_id="test.dex_test.not_null_tested_id.1",
            name="not_null_tested_id",
            resource_type="test",
            status="warn",
        ),
    ]
    verification = {
        "ran": True,
        "findings": [],
        "join_coverage": [
            {
                "model": "mart",
                "join": join,
                "status": "contradicted",
                "finding": "join_orphans",
            },
            {"model": "tested", "join": "left join x", "status": "satisfied"},
        ],
    }
    completion = summarize_completion(
        _evidence(nodes, required=["mart", "tested"]),
        verification,
        graph={"test.dex_test.not_null_tested_id.1": [_uid("tested")]},
    )
    failed = completion.assurance.failed_checks
    assert failed.names == ["mart", "tested"]
    assert completion.data()["assurance"]["failed_checks"]["checks"] == [
        {
            "model": "mart",
            "kind": "join_coverage",
            "finding": "join_orphans",
            "join": join,
        },
        {"model": "tested", "kind": "data_test", "name": "not_null_tested_id"},
    ]


def test_failed_checks_beyond_the_cap_are_counted():
    nodes = [BuildNode(unique_id=_uid("mart"), name="mart", status="success")]
    verification = {
        "ran": True,
        "findings": [],
        "join_coverage": [
            {
                "model": "mart",
                "join": f"inner join t{i} " + "on a.k = b.k and " * 20,
                "status": "contradicted",
                "finding": "join_orphans",
            }
            for i in range(8)
        ],
    }
    completion = summarize_completion(_evidence(nodes, required=["mart"]), verification)
    checks = completion.assurance.failed_checks.checks
    assert len(checks) == 5
    assert completion.elided["assurance.failed_checks.checks"] == 3
    assert all(len(c.join or "") <= 163 for c in checks)
    assert completion.size() <= COMPLETION_BUDGET_BYTES


def _three_failed_models() -> tuple[list[BuildNode], dict]:
    """``busy`` fails eight join checks; ``quiet`` and ``last`` fail one each."""

    names = ["busy", "quiet", "last"]
    nodes = [BuildNode(unique_id=_uid(n), name=n, status="success") for n in names]
    records = [
        {
            "model": "busy",
            "join": f"inner join t{i} on a.k = b.k",
            "status": "contradicted",
            "finding": "join_orphans",
        }
        for i in range(8)
    ] + [
        {
            "model": name,
            "join": "inner join u on a.k = u.k",
            "status": "contradicted",
            "finding": "join_orphans",
        }
        for name in ("quiet", "last")
    ]
    return nodes, {"ran": True, "findings": [], "join_coverage": records}


def test_capped_failed_checks_give_every_listed_model_its_kind_first():
    nodes, verification = _three_failed_models()
    completion = summarize_completion(
        _evidence(nodes, required=["busy", "quiet", "last"]), verification
    )
    failed = completion.assurance.failed_checks
    assert failed.names == ["busy", "quiet", "last"]
    assert [c.model for c in failed.checks] == [
        "busy",
        "quiet",
        "last",
        "busy",
        "busy",
    ]
    assert completion.elided["assurance.failed_checks.checks"] == 5


@pytest.mark.parametrize(("budget", "kept"), [(2000, 3), (1400, 1)])
def test_a_tight_budget_keeps_failed_checks_aligned_with_their_names(budget, kept):
    nodes, verification = _three_failed_models()
    completion = summarize_completion(
        _evidence(nodes, required=["busy", "quiet", "last"]),
        verification,
        budget=budget,
    )
    failed = completion.assurance.failed_checks
    assert completion.size() <= budget
    assert len(failed.checks) == kept
    assert [c.model for c in failed.checks] == failed.names[:kept]


def test_a_unit_test_alone_does_not_count_as_its_model_being_built():
    nodes = [
        BuildNode(
            unique_id=f"unit_test.dex_test.{REPORT}.checks_math",
            name="checks_math",
            resource_type="unit_test",
            status="pass",
        )
    ]
    completion = summarize_completion(_evidence(nodes, required=[REPORT]), None)
    assert completion.deliverables.missing.names == [REPORT]
    assert completion.deliverables.built.count == 0


def test_a_long_selector_is_cut_and_the_block_stays_in_budget():
    nodes = _staging(300)
    evidence = _evidence(nodes)
    long_selector = " ".join(n.name for n in nodes)
    completion = summarize_completion(
        evidence, None, change=ChangeRef(selector=long_selector)
    )
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert len(completion.change.selector or "") <= 203


def test_more_than_five_blocked_deliverables_are_counted():
    failing = [
        BuildNode(unique_id=_uid(f"m{i}"), name=f"m{i}", status="error")
        for i in range(8)
    ]
    completion = summarize_completion(
        _evidence(failing, required=[n.name for n in failing]), None
    )
    assert len(completion.deliverables.blocked) == 5
    assert completion.elided["deliverables.blocked"] == 3


# --- through the CLI, the failed-build path included ---------------------------


def _write_artifacts(project: Path, results: list[dict], manifest_nodes: dict) -> None:
    target = dex_target(project)
    target.mkdir(parents=True, exist_ok=True)
    compiled_at = datetime.now(UTC) + timedelta(minutes=1)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {"generated_at": compiled_at.isoformat()},
                "nodes": manifest_nodes,
            }
        ),
        encoding="utf-8",
    )
    (target / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "invocation_id": "inv-cli",
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                "elapsed_time": 12.5,
                "results": results,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def blocked_project(dbt_project_dir: Path, tmp_path: Path) -> tuple[Path, str]:
    report_sql = "select * from {{ ref('int_team_members') }}\n"
    marts = dbt_project_dir / "models" / "marts"
    marts.mkdir()
    (marts / f"{REPORT}.sql").write_text(report_sql, encoding="utf-8")
    plan = TransformPlan(
        plan_id="pblocked",
        created_at=datetime.now(UTC).isoformat(),
        intent="add the team activity report",
        project_dir="analytics",
        edits=[
            PlanEdit(
                path=f"models/marts/{REPORT}.sql",
                op=EditOp.UPSERT,
                kind=EditKind.MODEL_SQL,
                new_content=report_sql,
                old_content_hash=content_hash(report_sql),
            )
        ],
        applied_at=datetime.now(UTC).isoformat(),
    )
    plans = tmp_path / ".dex" / "plans"
    plans.mkdir(parents=True)
    (plans / "pblocked.json").write_text(plan.model_dump_json(), encoding="utf-8")
    return dbt_project_dir, "pblocked"


def test_the_cli_failure_envelope_leads_with_the_skipped_deliverable(
    blocked_project: tuple[Path, str], tmp_path: Path, capsys, monkeypatch
):
    project, plan_id = blocked_project
    staging = [f"stg_source_{i:03d}" for i in range(300)]
    manifest_nodes = {
        _uid(name): {
            "name": name,
            "resource_type": "model",
            "depends_on": {"nodes": []},
        }
        for name in staging
    }
    manifest_nodes[_uid(PREREQ)] = {
        "name": PREREQ,
        "resource_type": "model",
        "depends_on": {"nodes": [_uid("stg_source_000")]},
    }
    manifest_nodes[_uid(REPORT)] = {
        "name": REPORT,
        "resource_type": "model",
        "original_file_path": f"models/marts/{REPORT}.sql",
        "depends_on": {"nodes": [_uid(PREREQ)]},
    }
    results = [
        {"unique_id": _uid(name), "status": "success", "execution_time": 0.1}
        for name in staging
    ]
    results.append(
        {
            "unique_id": _uid(PREREQ),
            "status": "error",
            "message": "Conversion Error: Could not convert string 'n/a' to INT32",
        }
    )
    results.append({"unique_id": _uid(REPORT), "status": "skipped"})

    def write(_argv):
        _write_artifacts(project, results, manifest_nodes)

    _fake_runner_factory(monkeypatch, returncode=1, on_run=write)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--for-plan",
            plan_id,
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    data = envelope["data"]
    assert next(iter(data)) == "completion"
    completion = data["completion"]
    assert completion["outcome"] == "failed"
    assert completion["change"] == {"plan_id": plan_id, "source": "for_plan"}
    assert completion["deliverables"]["skipped"]["names"] == [REPORT]
    assert completion["deliverables"]["blocked"] == [
        {"deliverable": REPORT, "status": "skipped", "cause": PREREQ}
    ]
    assert completion["blockers"][0]["node"] == PREREQ
    assert len(json.dumps(completion)) <= COMPLETION_BUDGET_BYTES
    # What a `head -c 4096` of the real CLI line shows.
    text = json.dumps(envelope)
    assert json.dumps(completion) in text[:PREFIX]
    assert text.index('"nodes"') > text.index(json.dumps(completion))


def test_an_unsettled_identifier_conversion_is_an_obligation_on_its_model():
    """The plan's undeclared or unmeasured identifier conversions ride into the
    block for the models the change requires (#861); settled ones do not."""

    nodes = [BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success")]
    completion = summarize_completion(
        _evidence(nodes, required=[REPORT]),
        None,
        identifier_conversions={
            REPORT: [
                {"column": "product_id", "status": "undeclared"},
                {"column": "product_id", "status": "lossless"},
                {"column": "sku", "status": "unchecked", "reason_code": "counting_off"},
            ],
            "not_required": [{"column": "id", "status": "undeclared"}],
        },
    )
    assert completion.data()["obligations"]["identifier_conversions"] == [
        {
            "model": REPORT,
            "kind": "identifier_conversion",
            "status": "undeclared",
            "column": "product_id",
        },
        {
            "model": REPORT,
            "kind": "identifier_conversion",
            "status": "unchecked",
            "column": "sku",
            "reason_code": "counting_off",
        },
    ]


def test_no_unsettled_conversion_leaves_no_key():
    nodes = [BuildNode(unique_id=_uid(REPORT), name=REPORT, status="success")]
    completion = summarize_completion(
        _evidence(nodes, required=[REPORT]),
        None,
        identifier_conversions={REPORT: [{"status": "declared"}]},
    )
    assert "identifier_conversions" not in completion.data()["obligations"]


def test_an_unrecognized_status_is_not_a_built_deliverable():
    """#1105: completion agrees with the outcome and the coverage partition. A
    deliverable on a status dex does not know is listed apart, blocked, and not
    a blocker, since nothing failed."""

    nodes = [
        *_staging(2),
        BuildNode(
            unique_id=_uid(REPORT),
            name=REPORT,
            resource_type="model",
            status="partial success",
        ),
    ]
    evidence = _evidence(nodes, required=[REPORT, "stg_source_000"])
    completion = summarize_completion(evidence, None)

    assert completion.outcome == "indeterminate"
    assert completion.deliverables.unrecognized.names == [REPORT]
    assert completion.deliverables.built.names == ["stg_source_000"]
    assert completion.deliverables.failed.count == 0
    assert [(b.deliverable, b.status) for b in completion.deliverables.blocked] == [
        (REPORT, "partial success")
    ]
    assert completion.blockers == []
    # Part of a microbatch model may have been written: not "predates this run".
    assert completion.deliverables.note.startswith("reported a status dex does not")


def test_an_unrecognized_model_is_not_counted_built_without_a_plan():
    nodes = [
        *_staging(1),
        BuildNode(unique_id=_uid(REPORT), name=REPORT, status="no-op"),
    ]
    completion = summarize_completion(_evidence(nodes), None)
    assert completion.outcome == "indeterminate"
    assert REPORT not in completion.assurance.build_only.names
    assert REPORT not in completion.assurance.asserted.names


def test_an_unrecognized_deliverable_survives_the_4_kib_cut():
    nodes = [
        *_staging(300),
        BuildNode(unique_id=_uid(REPORT), name=REPORT, status="partial success"),
    ]
    evidence = _evidence(
        nodes, required=[REPORT, *(f"stg_source_{i:03d}" for i in range(300))]
    )
    completion = summarize_completion(evidence, None)
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert completion.deliverables.unrecognized.names == [REPORT]


def test_a_plan_that_only_adds_tests_establishes_coverage_when_tests_pass():
    nodes = [
        BuildNode(
            unique_id="test.dex_test.assert_orders_have_ids",
            name="assert_orders_have_ids",
            resource_type="test",
            status="pass",
        )
    ]
    evidence = _evidence(nodes, required=["assert_orders_have_ids"])
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "validated"
    assert completion.deliverables.coverage == "known"
    assert completion.deliverables.required == 1
    assert completion.deliverables.built.names == ["assert_orders_have_ids"]
    assert completion.deliverables.failed.count == 0
    assert completion.deliverables.missing.count == 0
    # Model execution and assertion evidence remain separate
    assert completion.assurance.asserted.count == 0
    assert completion.assurance.build_only.count == 0


def test_an_unrelated_model_build_does_not_satisfy_a_test_only_plan():
    nodes = [
        BuildNode(
            unique_id=_uid(REPORT),
            name=REPORT,
            resource_type="model",
            status="success",
        )
    ]
    evidence = _evidence(nodes, required=["assert_orders_have_ids"])
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "unrelated"
    assert completion.deliverables.coverage == "known"
    assert completion.deliverables.required == 1
    assert completion.deliverables.built.count == 0
    assert completion.deliverables.missing.names == ["assert_orders_have_ids"]


def test_a_failed_test_in_a_test_only_plan_is_reported_explicitly():
    nodes = [
        BuildNode(
            unique_id="test.dex_test.assert_orders_have_ids",
            name="assert_orders_have_ids",
            resource_type="test",
            status="fail",
        )
    ]
    evidence = _evidence(nodes, required=["assert_orders_have_ids"])
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "failed"
    assert completion.deliverables.failed.names == ["assert_orders_have_ids"]
    assert [(b.deliverable, b.status) for b in completion.deliverables.blocked] == [
        ("assert_orders_have_ids", "fail")
    ]
    assert completion.blockers[0].node == "assert_orders_have_ids"
    assert completion.blockers[0].status == "fail"


def test_a_skipped_test_in_a_test_only_plan_names_the_failed_ancestor():
    nodes = [
        BuildNode(
            unique_id=_uid(REPORT),
            name=REPORT,
            resource_type="model",
            status="error",
        ),
        BuildNode(
            unique_id="test.dex_test.assert_orders_have_ids",
            name="assert_orders_have_ids",
            resource_type="test",
            status="skipped",
        ),
    ]
    evidence = _evidence(nodes, required=["assert_orders_have_ids"])
    graph = {"test.dex_test.assert_orders_have_ids": [_uid(REPORT)]}
    completion = summarize_completion(evidence, None, graph=graph)
    assert completion.outcome == "failed"
    assert completion.deliverables.skipped.names == ["assert_orders_have_ids"]
    assert [
        (b.deliverable, b.status, b.cause) for b in completion.deliverables.blocked
    ] == [("assert_orders_have_ids", "skipped", REPORT)]


def test_native_unit_test_deliverable_passes_without_rebuilding_model():
    nodes = [
        BuildNode(
            unique_id="unit_test.dex_test.stg_orders.test_orders_atomic",
            name="test_orders_atomic",
            resource_type="unit_test",
            status="pass",
        )
    ]
    evidence = _evidence(nodes, required=["test_orders_atomic"])
    completion = summarize_completion(evidence, None)
    assert completion.outcome == "validated"
    assert completion.deliverables.built.names == ["test_orders_atomic"]
    assert "stg_orders" not in completion.deliverables.built.names
    assert "stg_orders" not in completion.assurance.build_only.names


def test_same_named_model_cannot_satisfy_required_test_deliverable():
    """Model node execution cannot satisfy a required test deliverable (#1316)."""
    coverage = BuildCoverage(
        required=["orders"],
        covered=[],
        missing=["orders"],
        built=[],
        not_built=["orders"],
        kinds={"orders": "test"},
    )
    nodes = [
        BuildNode(
            unique_id="model.dex_test.orders",
            name="orders",
            resource_type="model",
            status="success",
        )
    ]
    evidence = BuildEvidence(
        outcome=BuildOutcome.UNRELATED,
        nodes=nodes,
        coverage=coverage,
    )
    completion = summarize_completion(evidence, None)
    assert completion.deliverables.missing.names == ["orders"]
    assert "orders" not in completion.deliverables.built.names
    assert completion.outcome == "unrelated"


def test_model_scoped_unit_test_completion_requires_all_scoped_tests():
    """Running only one model-scoped unit test leaves the other missing (#1316)."""
    coverage = BuildCoverage(
        required=["customers.atomic", "orders.atomic"],
        covered=["orders.atomic"],
        missing=["customers.atomic"],
        built=["orders.atomic"],
        not_built=["customers.atomic"],
        kinds={"orders.atomic": "unit_test", "customers.atomic": "unit_test"},
    )
    nodes = [
        BuildNode(
            unique_id="unit_test.dex_test.orders.atomic",
            name="atomic",
            resource_type="unit_test",
            status="pass",
        )
    ]
    evidence = BuildEvidence(
        outcome=BuildOutcome.PARTIAL,
        nodes=nodes,
        coverage=coverage,
    )
    completion = summarize_completion(evidence, None)
    assert "orders.atomic" in completion.deliverables.built.names
    assert "customers.atomic" in completion.deliverables.missing.names
    assert completion.outcome == "partial"


# --- a pass that checked nothing (#1308) -----------------------------------------


def _elementary_nodes() -> tuple[list[BuildNode], dict[str, list[str]]]:
    """``only_anomaly`` has one Elementary test; ``with_builtin`` has the same
    test beside a dbt ``not_null``; ``failing_anomaly`` has one that failed."""

    tests = {
        "test.dex_test.volume_only.1": ("volume_only", "pass", "only_anomaly"),
        "test.dex_test.volume_with.2": ("volume_with", "pass", "with_builtin"),
        "test.dex_test.not_null_with.3": ("not_null_with", "pass", "with_builtin"),
        "test.dex_test.volume_failing.4": ("volume_failing", "fail", "failing_anomaly"),
    }
    nodes = [
        BuildNode(unique_id=_uid(name), name=name, status="success")
        for name in ("only_anomaly", "with_builtin", "failing_anomaly")
    ] + [
        BuildNode(unique_id=uid, name=name, resource_type="test", status=status)
        for uid, (name, status, _) in tests.items()
    ]
    graph = {uid: [_uid(model)] for uid, (_, _, model) in tests.items()}
    return nodes, graph


def test_a_model_whose_only_test_checked_nothing_is_build_only():
    nodes, graph = _elementary_nodes()
    unchecked = {
        "test.dex_test.volume_only.1": "package_disabled",
        "test.dex_test.volume_with.2": "package_disabled",
        "test.dex_test.volume_failing.4": "history_unmeasured",
    }
    completion = summarize_completion(
        _evidence(nodes, required=["only_anomaly", "with_builtin", "failing_anomaly"]),
        None,
        graph=graph,
        unchecked_tests=unchecked,
    )
    assurance = completion.assurance
    assert assurance.build_only.names == ["only_anomaly"]
    # The dbt test still checks its model, and is not listed.
    assert assurance.asserted.names == ["with_builtin"]
    # A failure read something, so it stands as one.
    assert assurance.failed_checks.names == ["failing_anomaly"]
    assert [t.model_dump() for t in assurance.unchecked_tests] == [
        {
            "test": "volume_only",
            "models": ["only_anomaly"],
            "reason_code": "package_disabled",
        },
        {
            "test": "volume_with",
            "models": ["with_builtin"],
            "reason_code": "package_disabled",
        },
    ]


def test_without_unchecked_tests_the_key_is_absent():
    nodes, graph = _elementary_nodes()
    completion = summarize_completion(
        _evidence(nodes, required=["only_anomaly"]), None, graph=graph
    )
    assert "unchecked_tests" not in completion.data()["assurance"]
    assert "only_anomaly" in completion.assurance.asserted.names


def test_unchecked_tests_beyond_the_cap_are_counted():
    nodes = [BuildNode(unique_id=_uid("m"), name="m", status="success")]
    graph: dict[str, list[str]] = {}
    unchecked: dict[str, str] = {}
    for i in range(9):
        uid = f"test.dex_test.anomaly_{i}.{i}"
        nodes.append(
            BuildNode(
                unique_id=uid, name=f"anomaly_{i}", resource_type="test", status="pass"
            )
        )
        graph[uid] = [_uid("m")]
        unchecked[uid] = "history_unmeasured"
    completion = summarize_completion(
        _evidence(nodes, required=["m"]), None, graph=graph, unchecked_tests=unchecked
    )
    assert len(completion.assurance.unchecked_tests) == 5
    assert completion.elided["assurance.unchecked_tests"] == 4
    assert completion.assurance.build_only.names == ["m"]


def test_with_a_plan_only_tests_on_a_deliverable_are_listed():
    nodes, graph = _elementary_nodes()
    unchecked = {
        "test.dex_test.volume_only.1": "history_unmeasured",
        "test.dex_test.volume_with.2": "history_unmeasured",
    }
    completion = summarize_completion(
        _evidence(nodes, required=["only_anomaly"]),
        None,
        graph=graph,
        unchecked_tests=unchecked,
    )
    assert [t.test for t in completion.assurance.unchecked_tests] == ["volume_only"]
    assert completion.assurance.build_only.names == ["only_anomaly"]


def test_a_versioned_deliverable_named_by_its_file_stem_keeps_its_tests_listed():
    """A plan requires ``dim_customers_v2`` (the file stem), the node's label
    is ``dim_customers.v2``, and the test names its model by the label."""

    uid = "model.dex_test.dim_customers.v2"
    test = "test.dex_test.volume_dim.1"
    nodes = [
        BuildNode(unique_id=uid, name="dim_customers.v2", status="success"),
        BuildNode(
            unique_id=test, name="volume_dim", resource_type="test", status="pass"
        ),
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["dim_customers_v2"]),
        None,
        graph={test: [uid]},
        unchecked_tests={test: "history_unmeasured"},
    )
    assert [t.test for t in completion.assurance.unchecked_tests] == ["volume_dim"]


def test_a_versioned_deliverable_named_by_its_file_stem_is_asserted():
    """A plan requires ``dim_customers_v2`` (the file stem), the node's label
    is ``dim_customers.v2``, and a passing test names its model by the label:
    the deliverable is ``asserted``, not ``build_only`` (#1499)."""

    uid = "model.dex_test.dim_customers.v2"
    test = "test.dex_test.not_null_dim_customers_id.1"
    nodes = [
        BuildNode(unique_id=uid, name="dim_customers.v2", status="success"),
        BuildNode(
            unique_id=test,
            name="not_null_dim_customers_id",
            resource_type="test",
            status="pass",
        ),
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["dim_customers_v2"]),
        None,
        graph={test: [uid]},
    )
    assert completion.assurance.basis == "required"
    assert completion.assurance.asserted.names == ["dim_customers_v2"]
    assert completion.assurance.build_only.count == 0


def test_a_versioned_deliverable_named_by_its_file_stem_reports_its_failed_check():
    """The same mismatch on the failing side: a failing test on the versioned
    model puts the deliverable under ``failed_checks`` with its check, not
    under ``build_only`` (#1499)."""

    uid = "model.dex_test.dim_customers.v2"
    test = "test.dex_test.not_null_dim_customers_id.1"
    nodes = [
        BuildNode(unique_id=uid, name="dim_customers.v2", status="success"),
        BuildNode(
            unique_id=test,
            name="not_null_dim_customers_id",
            resource_type="test",
            status="fail",
        ),
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["dim_customers_v2"]),
        None,
        graph={test: [uid]},
    )
    assert completion.assurance.failed_checks.names == ["dim_customers_v2"]
    assert [c.name for c in completion.assurance.failed_checks.checks] == [
        "not_null_dim_customers_id"
    ]
    assert completion.assurance.build_only.count == 0


def test_a_failed_version_does_not_also_assert_the_shared_base_name():
    """A passing ``dim_customers.v1`` and a failing ``dim_customers.v2`` both
    expand to ``dim_customers``: failure takes precedence after alias
    expansion, so the deliverable is failed, not asserted (#1499)."""

    v1 = "model.dex_test.dim_customers.v1"
    v2 = "model.dex_test.dim_customers.v2"
    pass_test = "test.dex_test.not_null_dim_customers_v1_id.1"
    fail_test = "test.dex_test.not_null_dim_customers_v2_id.2"
    nodes = [
        BuildNode(unique_id=v1, name="dim_customers.v1", status="success"),
        BuildNode(unique_id=v2, name="dim_customers.v2", status="success"),
        BuildNode(
            unique_id=pass_test,
            name="not_null_dim_customers_v1_id",
            resource_type="test",
            status="pass",
        ),
        BuildNode(
            unique_id=fail_test,
            name="not_null_dim_customers_v2_id",
            resource_type="test",
            status="fail",
        ),
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["dim_customers"]),
        None,
        graph={pass_test: [v1], fail_test: [v2]},
    )
    assert completion.assurance.failed_checks.names == ["dim_customers"]
    assert [c.name for c in completion.assurance.failed_checks.checks] == [
        "not_null_dim_customers_v2_id"
    ]
    assert completion.assurance.asserted.names == []
    assert completion.assurance.build_only.count == 0


def test_two_failing_versions_share_one_alias_and_keep_both_checks():
    """Both built versions fail: their checks accumulate under the shared
    alias instead of the first version's checks standing alone (#1499)."""

    v1 = "model.dex_test.dim_customers.v1"
    v2 = "model.dex_test.dim_customers.v2"
    fail_test_v1 = "test.dex_test.not_null_dim_customers_v1_id.1"
    fail_test_v2 = "test.dex_test.not_null_dim_customers_v2_id.2"
    nodes = [
        BuildNode(unique_id=v1, name="dim_customers.v1", status="success"),
        BuildNode(unique_id=v2, name="dim_customers.v2", status="success"),
        BuildNode(
            unique_id=fail_test_v1,
            name="not_null_dim_customers_v1_id",
            resource_type="test",
            status="fail",
        ),
        BuildNode(
            unique_id=fail_test_v2,
            name="not_null_dim_customers_v2_id",
            resource_type="test",
            status="fail",
        ),
    ]
    completion = summarize_completion(
        _evidence(nodes, required=["dim_customers"]),
        None,
        graph={fail_test_v1: [v1], fail_test_v2: [v2]},
    )
    assert completion.assurance.failed_checks.names == ["dim_customers"]
    assert [c.name for c in completion.assurance.failed_checks.checks] == [
        "not_null_dim_customers_v1_id",
        "not_null_dim_customers_v2_id",
    ]


def test_the_warning_counts_the_unchecked_tests_the_cap_cut(monkeypatch):
    from exmergo_dex_core.transform import commands

    nodes = [BuildNode(unique_id=_uid("m"), name="m", status="success")]
    graph: dict[str, list[str]] = {}
    unchecked: dict[str, str] = {}
    for i in range(9):
        uid = f"test.dex_test.anomaly_{i}.{i}"
        nodes.append(
            BuildNode(
                unique_id=uid, name=f"anomaly_{i}", resource_type="test", status="pass"
            )
        )
        graph[uid] = [_uid("m")]
        unchecked[uid] = "history_unmeasured"
    monkeypatch.setattr(commands, "_unchecked_tests", lambda inputs: unchecked)
    result = BuildResult(
        success=True, summary={"success": True}, evidence=_evidence(nodes)
    )
    commands._attach_completion(result, None, {"graph": graph})
    assert len(result.completion.assurance.unchecked_tests) == 5
    assert any(line.startswith("9 passing Elementary") for line in result.warnings)
