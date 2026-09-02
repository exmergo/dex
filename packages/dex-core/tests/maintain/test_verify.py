"""maintain verify: a baseline-free sweep (#224), starting with build-status
gaps read from the compiled manifest and the last run's run_results.json,
plus a project that fails to compile (#225)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.maintain.verify import (
    build_status_findings,
    compile_check,
    missing_relation_findings,
)


def _write_artifacts(project_dir: Path, nodes: dict, results: list[dict]) -> None:
    target = project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    (target / "run_results.json").write_text(
        json.dumps({"results": results}), encoding="utf-8"
    )


# --- build_status_findings: pure, reads only artifacts already on disk ----------


def test_no_run_results_is_reported_as_a_note_not_a_finding(tmp_path: Path):
    findings, notes = build_status_findings(tmp_path)
    assert findings == []
    assert notes and "no dbt run results found" in notes[0]


def test_a_failed_node_is_reported(tmp_path: Path):
    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "error", "message": "boom"}],
    )
    findings, notes = build_status_findings(tmp_path)
    assert notes == []
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "node_failed"
    assert finding.identifier == "a"
    assert finding.severity == "high"
    assert "boom" in finding.detail


def test_one_failed_model_and_two_skipped_children_reports_all_three(tmp_path: Path):
    """#225's own acceptance bullet, verbatim: one failed model and two
    skipped children report all three findings and the causal link -- each
    child on its own, not collapsed into one."""

    _write_artifacts(
        tmp_path,
        nodes={
            "model.p.parent": {
                "name": "parent",
                "resource_type": "model",
                "depends_on": {"nodes": []},
            },
            "model.p.child_a": {
                "name": "child_a",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.parent"]},
            },
            "model.p.child_b": {
                "name": "child_b",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.parent"]},
            },
        },
        results=[
            {"unique_id": "model.p.parent", "status": "error", "message": "boom"},
            {"unique_id": "model.p.child_a", "status": "skipped"},
            {"unique_id": "model.p.child_b", "status": "skipped"},
        ],
    )
    findings, _notes = build_status_findings(tmp_path)
    assert len(findings) == 3
    by_identifier = {f.identifier: f for f in findings}
    assert set(by_identifier) == {"parent", "child_a", "child_b"}
    assert by_identifier["parent"].code == "node_failed"
    for name in ("child_a", "child_b"):
        skipped = by_identifier[name]
        assert skipped.code == "node_skipped"
        assert skipped.severity == "medium"
        assert skipped.data["caused_by"] == "parent"
        assert "'parent' failed to build" in skipped.detail


def test_a_transitively_skipped_node_traces_back_to_the_real_failure(tmp_path: Path):
    """A grandchild skipped because its parent was itself only skipped (the
    parent's own parent is the one that actually failed) still names the
    real cause, not the also-skipped intermediate node."""

    _write_artifacts(
        tmp_path,
        nodes={
            "model.p.grandparent": {"name": "grandparent", "resource_type": "model"},
            "model.p.parent": {
                "name": "parent",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.grandparent"]},
            },
            "model.p.child": {
                "name": "child",
                "resource_type": "model",
                "depends_on": {"nodes": ["model.p.parent"]},
            },
        },
        results=[
            {"unique_id": "model.p.grandparent", "status": "error", "message": "boom"},
            {"unique_id": "model.p.parent", "status": "skipped"},
            {"unique_id": "model.p.child", "status": "skipped"},
        ],
    )
    findings, _notes = build_status_findings(tmp_path)
    by_identifier = {f.identifier: f for f in findings}
    assert by_identifier["child"].data["caused_by"] == "grandparent"
    assert by_identifier["parent"].data["caused_by"] == "grandparent"


def test_a_skipped_node_with_no_identifiable_cause_is_still_reported(tmp_path: Path):
    """A selector exclusion or an upstream error dbt did not attribute still
    reports the skip, just at a lower severity than a definite causal chain."""

    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "skipped"}],
    )
    findings, _notes = build_status_findings(tmp_path)
    assert len(findings) == 1
    assert findings[0].severity == "low"
    assert findings[0].data == {}


def test_a_successful_node_reports_nothing(tmp_path: Path):
    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "success"}],
    )
    findings, notes = build_status_findings(tmp_path)
    assert findings == [] and notes == []


# --- missing_relation_findings: pure ---------------------------------------------


def test_a_model_with_no_matching_relation_is_reported():
    findings = missing_relation_findings(
        model_relations={"stg_orders": "db.main.stg_orders"},
        live_identifiers=["db.main.customers"],
        already_reported=set(),
    )
    assert len(findings) == 1
    assert findings[0].code == "no_relation"
    assert findings[0].identifier == "stg_orders"
    assert findings[0].data["relation_name"] == "db.main.stg_orders"


def test_a_model_with_a_matching_relation_is_quiet():
    findings = missing_relation_findings(
        model_relations={"stg_orders": "db.main.stg_orders"},
        live_identifiers=["db.main.stg_orders"],
        already_reported=set(),
    )
    assert findings == []


def test_a_model_already_explained_by_a_build_status_finding_is_not_repeated():
    """A node that failed or was skipped never produced a relation either;
    reporting that twice under a different code would say the same thing
    about the same node in two places."""

    findings = missing_relation_findings(
        model_relations={"stg_orders": "db.main.stg_orders"},
        live_identifiers=[],
        already_reported={"stg_orders"},
    )
    assert findings == []


# --- compile_check: wraps shadow_parse -------------------------------------------


def test_compile_check_is_quiet_when_dbt_is_unavailable(tmp_path: Path, monkeypatch):
    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": False,
            "reason": "dbt is not installed",
            "success": None,
            "messages": [],
        },
    )
    finding, notes = compile_check(tmp_path)
    assert finding is None
    assert notes == ["compile check skipped (dbt is not installed)"]


def test_compile_check_is_quiet_on_a_passing_parse(tmp_path: Path, monkeypatch):
    import exmergo_dex_core.maintain.verify as verify_mod

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
    finding, notes = compile_check(tmp_path)
    assert finding is None and notes == []


def test_compile_check_reports_the_first_parse_message(tmp_path: Path, monkeypatch):
    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": False,
            "messages": ["Compilation Error in model stg_orders: syntax error"],
        },
    )
    finding, notes = compile_check(tmp_path)
    assert notes == []
    assert finding is not None
    assert finding.code == "project_does_not_compile"
    assert finding.severity == "high"
    assert "syntax error" in finding.detail


# --- end to end through the CLI (the degraded, dbt-artifact-free default) -------
#
# `maintain_repo` is shared with the drift suites, which never invoke a real
# `dbt parse` against it (they work entirely from the `.dex/` cache), so its
# own compile-cleanliness is untested territory. The compile check is faked
# to a clean pass here so these tests exercise this module's own wiring
# (manifest/run-results reading through `engine.project_dir()`, scoping)
# rather than depending on the fixture project being dbt-parseable, which
# `compile_check` already has its own direct unit tests for above.


@pytest.fixture
def _assume_the_project_compiles(monkeypatch: pytest.MonkeyPatch):
    import exmergo_dex_core.maintain.verify as verify_mod

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


def test_verify_on_a_project_with_no_build_yet_is_clean_and_says_so(
    maintain_repo, _assume_the_project_compiles
):
    """`maintain_repo` never runs real dbt, so this is the ordinary state a
    fresh checkout is in: no baseline needed (unlike every other maintain
    subcommand), and the absence of run results is named rather than read as
    a clean bill of health."""

    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert payload["data"]["finding_count"] == 0
    assert "build_status" in payload["data"]["suppressed"]
    assert any("no dbt run results found" in w for w in payload["warnings"])


def test_verify_reports_a_failed_node_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """The manifest/run_results wiring through engine.project_dir(), not just
    the pure function tested above in isolation."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
            }
        },
        results=[
            {
                "unique_id": "model.maintain_test.stg_orders",
                "status": "error",
                "message": "boom",
            }
        ],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = payload["data"]["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "node_failed"
    assert findings[0]["identifier"] == "stg_orders"
    assert "build_status" not in payload["data"]["suppressed"]


def test_verify_reports_a_model_with_no_relation_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """The other manifest-reading path, through `ProjectDefinitions.
    model_relations` rather than `build_status_findings`'s own manifest
    read: a model the project declares that never built anything."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_missing": {
                "name": "stg_missing",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."stg_missing"',
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = payload["data"]["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "no_relation"
    assert findings[0]["identifier"] == "stg_missing"
    assert "no_relation" not in payload["data"]["suppressed"]


def test_verify_reports_a_compile_failure_first_and_suppresses_the_rest(
    maintain_repo, monkeypatch: pytest.MonkeyPatch
):
    """#225's third acceptance bullet: a project that does not compile
    reports that first, and nothing else runs against its (untrustworthy)
    manifest."""

    import exmergo_dex_core.maintain.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": True,
            "reason": None,
            "success": False,
            "messages": ["Compilation Error in model stg_broken: syntax error"],
        },
    )
    # A failed node in run_results, which should never be reached: the
    # compile failure must suppress build-status checking entirely.
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={"model.maintain_test.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.maintain_test.a", "status": "error"}],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = payload["data"]["findings"]
    assert len(findings) == 1
    assert findings[0]["code"] == "project_does_not_compile"
    assert "syntax error" in findings[0]["detail"]
    assert set(payload["data"]["suppressed"]) == {"build_status", "no_relation"}


def test_verify_scopes_to_the_named_object(maintain_repo, _assume_the_project_compiles):
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.a": {"name": "a", "resource_type": "model"},
            "model.maintain_test.b": {"name": "b", "resource_type": "model"},
        },
        results=[
            {"unique_id": "model.maintain_test.a", "status": "error"},
            {"unique_id": "model.maintain_test.b", "status": "error"},
        ],
    )
    rc, payload = maintain_repo.dex("maintain", "verify", "a")
    assert rc == 0 and payload["status"] == "ok", payload
    assert [f["identifier"] for f in payload["data"]["findings"]] == ["a"]
