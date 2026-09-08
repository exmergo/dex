"""Build evidence: what a run established, as distinct from whether it exited zero.

The reproduction on issue #441 shows the failure this closes: a build whose
selection matched no nodes returns ``success: True``, and so does a build of a
model the change never touched. ``success`` still means what it always did;
``outcome`` is the field that tells the two apart from a build that validated
the change.

The outcomes are driven from synthesized dbt artifacts rather than from real
runs, so every one of them is reachable and deterministic. The live half is in
the dogfood.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exmergo_dex_core.edits import EditOp
from exmergo_dex_core.transform.evidence import (
    BuildOutcome,
    build_evidence,
    plan_drift,
    required_nodes,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

MART = "select 1 as id\n"
SCHEMA = "version: 2\nmodels:\n  - name: mart\n    description: A mart.\n"


def write_run_results(project: Path, results: list[dict], **metadata) -> None:
    target = project / "target"
    target.mkdir(exist_ok=True)
    (target / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "invocation_id": "abc-123",
                    "dbt_version": "1.11.11",
                    "generated_at": datetime.now(UTC).isoformat(),
                    **metadata,
                },
                "results": results,
            }
        ),
        encoding="utf-8",
    )


def write_manifest_nodes(project: Path, nodes: dict[str, dict]) -> None:
    target = project / "target"
    target.mkdir(exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {"generated_at": datetime.now(UTC).isoformat()},
                "nodes": nodes,
            }
        ),
        encoding="utf-8",
    )


def node(name: str, status: str = "success", **extra) -> dict:
    return {"unique_id": f"model.dex_test.{name}", "status": status, **extra}


@pytest.fixture
def project(dbt_project_dir: Path) -> Path:
    (dbt_project_dir / "models" / "staging" / "mart.sql").write_text(
        MART, encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "mart.yml").write_text(
        SCHEMA, encoding="utf-8"
    )
    write_manifest_nodes(
        dbt_project_dir,
        {
            "model.dex_test.mart": {
                "name": "mart",
                "resource_type": "model",
                "relation_name": '"dev"."main"."mart"',
                "config": {"materialized": "view"},
            },
            "model.dex_test.other": {
                "name": "other",
                "resource_type": "model",
                "relation_name": '"dev"."main"."other"',
                "config": {"materialized": "table"},
            },
        },
    )
    return dbt_project_dir


@pytest.fixture
def edits() -> list[PlanEdit]:
    return [
        PlanEdit(
            path="models/staging/mart.sql",
            new_content=MART,
            op=EditOp.UPSERT,
            kind=EditKind.MODEL_SQL,
        ),
        PlanEdit(
            path="models/staging/mart.yml",
            new_content=SCHEMA,
            op=EditOp.UPSERT,
            kind=EditKind.SCHEMA_YML,
        ),
    ]


def test_a_run_that_built_the_required_nodes_is_validated(project, edits):
    write_run_results(project, [node("mart")])
    evidence = build_evidence(project, target="dev", edits=edits)

    assert evidence.outcome is BuildOutcome.VALIDATED
    assert evidence.coverage.required == ["mart"]
    assert evidence.coverage.missing == []
    assert evidence.generated == ["dev.main.mart"]
    assert evidence.nodes[0].materialization == "view"


def test_an_empty_selection_is_not_a_pass(project, edits):
    """The reproduction's sharpest case: dbt exits zero having built nothing."""

    write_run_results(project, [])
    evidence = build_evidence(
        project, target="dev", select="tag:does_not_exist", edits=edits
    )

    assert evidence.outcome is BuildOutcome.EMPTY_SELECTION
    assert evidence.selection.empty is True
    assert evidence.coverage.covered == []


def test_a_build_of_an_unrelated_model_is_not_a_pass_either(project, edits):
    write_run_results(project, [node("other")])
    evidence = build_evidence(project, target="dev", select="other", edits=edits)

    assert evidence.outcome is BuildOutcome.UNRELATED
    assert evidence.selection.empty is False
    assert evidence.coverage.covered == []
    assert evidence.coverage.missing == ["mart"]


def test_some_required_and_some_not_is_partial(project):
    edits = [
        PlanEdit(
            path=f"models/staging/{name}.sql",
            new_content=MART,
            op=EditOp.UPSERT,
            kind=EditKind.MODEL_SQL,
        )
        for name in ("mart", "other")
    ]
    write_run_results(project, [node("mart")])
    evidence = build_evidence(project, target="dev", edits=edits)

    assert evidence.outcome is BuildOutcome.PARTIAL
    assert evidence.coverage.covered == ["mart"]
    assert evidence.coverage.missing == ["other"]


def test_a_failed_node_is_failed_whatever_it_covered(project, edits):
    write_run_results(
        project, [node("mart", "error", message="Binder Error: no such column")]
    )
    evidence = build_evidence(project, target="dev", edits=edits)

    assert evidence.outcome is BuildOutcome.FAILED
    assert evidence.errors == [
        {"node": "mart", "message": "Binder Error: no such column"}
    ]
    # A node that errored materialized nothing, so it is not in `generated`.
    assert evidence.generated == []


def test_a_failing_test_is_a_failure_too(project, edits):
    write_run_results(project, [node("mart"), node("unique_mart_id", "fail")])
    assert build_evidence(project, target="dev", edits=edits).outcome is (
        BuildOutcome.FAILED
    )


def test_every_node_skipped_is_skipped_and_never_a_pass(project, edits):
    """dbt's own word for a node whose parent failed, and a caller must not read
    it as a build that succeeded."""

    write_run_results(project, [node("mart", "skipped"), node("other", "skipped")])
    assert build_evidence(project, target="dev", edits=edits).outcome is (
        BuildOutcome.SKIPPED
    )


def test_no_run_results_at_all_is_not_run(project, edits):
    """A project that failed to parse never reaches node execution."""

    evidence = build_evidence(project, target="dev", edits=edits)
    assert evidence.outcome is BuildOutcome.NOT_RUN
    assert evidence.nodes == []
    assert evidence.coverage.missing == ["mart"]


def test_a_stale_manifest_is_stale_even_when_everything_passed(project, edits):
    write_run_results(project, [node("mart")])
    write_manifest_nodes(project, {})
    (project / "target" / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "generated_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()
                },
                "nodes": {},
            }
        ),
        encoding="utf-8",
    )
    evidence = build_evidence(project, target="dev", edits=edits)
    assert evidence.outcome is BuildOutcome.STALE
    assert evidence.stale_artifacts is True


def test_a_file_the_plan_wrote_that_changed_since_makes_the_build_stale(project, edits):
    """The reachable half of staleness, and the one a host needs.

    dbt succeeded, every required node was covered, and the tree it validated is
    not the tree the plan describes.
    """

    write_run_results(project, [node("mart")])
    (project / "models" / "staging" / "mart.sql").write_text(
        MART + "\n-- somebody edited this after the plan landed\n", encoding="utf-8"
    )
    evidence = build_evidence(project, target="dev", edits=edits)

    assert evidence.outcome is BuildOutcome.STALE
    assert [d.path for d in evidence.plan_drift] == ["models/staging/mart.sql"]
    assert evidence.coverage.missing == []

    # Drift alone is enough, which is what makes this reachable from a build
    # that regenerated its own artifacts and is therefore never stale by the
    # manifest's clock.
    from exmergo_dex_core.transform.evidence import _outcome

    assert (
        _outcome(True, evidence.nodes, evidence.coverage, False, evidence.plan_drift)
        is BuildOutcome.STALE
    )


def test_a_missing_file_the_plan_wrote_is_drift_rather_than_being_skipped(
    project, edits
):
    write_run_results(project, [node("mart")])
    (project / "models" / "staging" / "mart.yml").unlink()
    (drift,) = plan_drift(edits, project)
    assert drift.path == "models/staging/mart.yml"
    assert drift.found_sha256 is None


def test_coverage_is_absent_rather_than_empty_when_no_plan_was_named(project):
    """An empty coverage block reads as "nothing required was built", which is
    the opposite of "nobody asked what was required"."""

    write_run_results(project, [node("mart")])
    evidence = build_evidence(project, target="dev")
    assert evidence.coverage is None
    assert "coverage" not in evidence.data()


def test_selection_completeness_declines_rather_than_guessing_at_dbt_selectors(
    project, edits
):
    write_run_results(project, [node("mart")])

    literal = build_evidence(project, target="dev", select="mart", edits=edits)
    assert literal.selection.complete is True

    missed = build_evidence(project, target="dev", select="other", edits=edits)
    assert missed.selection.complete is False
    assert missed.selection.unmatched == ["other"]

    # dbt's graph operators and method selectors are dbt's to evaluate, so the
    # honest answer is that dex does not know.
    for selector in ("mart+", "tag:nightly", "@mart", "path:models/staging"):
        graph = build_evidence(project, target="dev", select=selector, edits=edits)
        assert graph.selection.complete is None, selector
        assert graph.selection.unmatched == []


def test_required_nodes_says_how_each_requirement_was_derived(project):
    edits = [
        PlanEdit(
            path="models/staging/mart.sql",
            new_content=MART,
            op=EditOp.UPSERT,
            kind=EditKind.MODEL_SQL,
        ),
        PlanEdit(
            path="macros/helper.sql",
            new_content="{% macro helper() %}1{% endmacro %}",
            op=EditOp.UPSERT,
            kind=EditKind.MACRO_SQL,
        ),
        PlanEdit(
            path="models/staging/gone.sql", op=EditOp.DELETE, kind=EditKind.MODEL_SQL
        ),
    ]
    required, basis = required_nodes(edits, project)

    assert required == ["mart"]
    # A macro can affect any node, so it requires none in particular, and says so
    # rather than contributing nothing silently.
    assert any("can affect any node" in line for line in basis)
    assert any("a deletion requires no node" in line for line in basis)


def test_a_semantic_model_requires_the_dbt_model_it_sits_on(project):
    """The link that makes a semantic change checkable against a build at all."""

    semantic = (
        "version: 2\nsemantic_models:\n  - name: orders\n    model: ref('fct_orders')\n"
    )
    edits = [
        PlanEdit(
            path="models/staging/orders_semantic.yml",
            new_content=semantic,
            op=EditOp.UPSERT,
            kind=EditKind.SEMANTIC_YML,
        )
    ]
    required, _basis = required_nodes(edits, project)
    assert required == ["fct_orders"]


def test_a_test_node_is_named_rather_than_reported_as_its_hash(project, edits):
    """dbt's unique_id for a test ends in a disambiguating hash, which is not a
    name a reader can look up in the project."""

    write_manifest_nodes(
        project,
        {
            "test.dex_test.not_null_mart_id.3249b83c15": {
                "name": "not_null_mart_id",
                "resource_type": "test",
                "config": {},
            }
        },
    )
    write_run_results(
        project,
        [{"unique_id": "test.dex_test.not_null_mart_id.3249b83c15", "status": "pass"}],
    )
    evidence = build_evidence(project, target="dev")
    assert evidence.nodes[0].name == "not_null_mart_id"


def test_the_evidence_carries_the_digests_a_host_compares_against(project, edits):
    write_run_results(project, [node("mart")])
    evidence = build_evidence(
        project,
        target="dev",
        edits=edits,
        plan_digest="sha256:aaa",
        source_digest="sha256:bbb",
    )
    digests = evidence.digests
    assert digests.plan_digest == "sha256:aaa"
    assert digests.source_digest == "sha256:bbb"
    assert digests.manifest_sha256.startswith("sha256:")
    assert digests.run_results_sha256.startswith("sha256:")


def test_a_corrupt_run_results_is_not_run_rather_than_an_empty_pass(project, edits):
    (project / "target").mkdir(exist_ok=True)
    (project / "target" / "run_results.json").write_text("{not json", encoding="utf-8")
    assert build_evidence(project, target="dev", edits=edits).outcome is (
        BuildOutcome.NOT_RUN
    )
