"""What a plan introduced, apart from what was there before (#1230).

The classification itself, the sweep history it reads, the plan's reach in the
manifest's lineage, and what the completion block makes of it: an introduced
high-severity finding, an uncompared rewrite whose baseline was ready, and an
undeclared population change each keep a build that validated from reading as
a finished change.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from exmergo_dex_core.maintain.drift import DriftFinding
from exmergo_dex_core.transform import finding_attribution as attr
from exmergo_dex_core.transform.completion import (
    COMPLETION_BUDGET_BYTES,
    BuildNode,
    ComparisonState,
    summarize_completion,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.results import BuildResult

from .test_build_completion import _evidence, _uid


def _finding(identifier: str, code: str = "join_no_match", **extra) -> DriftFinding:
    return DriftFinding(
        axis=extra.pop("axis", "join_contract"),
        code=code,
        identifier=identifier,
        severity=extra.pop("severity", "high"),
        detail="d",
        **extra,
    )


def _baseline(**overrides) -> attr.Baseline:
    fields = {
        "recorded_at": "2026-11-01T00:00:00+00:00",
        "invocation_id": "inv-0",
        "scope": frozenset({"stg_orders", "fct_orders", "dim_users"}),
        "suppressed": frozenset(),
        "fingerprints": frozenset(),
    }
    fields.update(overrides)
    return attr.Baseline(**fields)


# --- fingerprints and the history ---------------------------------------------


def test_a_fingerprint_names_the_check_and_where_it_fired_not_its_counts():
    one = _finding("fct_orders", data={"join": "o join u", "orphan_fraction": 0.4})
    two = _finding("fct_orders", data={"join": "o join u", "orphan_fraction": 0.9})
    other_join = _finding("fct_orders", data={"join": "o join p"})
    assert attr.fingerprint(one) == attr.fingerprint(two)
    assert attr.fingerprint(one) != attr.fingerprint(other_join)
    assert attr.fingerprint(one) == attr.fingerprint(one.model_dump(mode="json"))


def test_the_baseline_is_the_last_sweep_before_the_plan_was_applied(tmp_path: Path):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for hours, fp in ((0, "a"), (1, "b"), (3, "c")):
        attr.record_sweep(
            tmp_path,
            "dev",
            scope=["m"],
            suppressed=[],
            fingerprints=[fp],
            invocation_id=f"inv-{hours}",
            now=start + timedelta(hours=hours),
        )
    attr.record_sweep(
        tmp_path,
        "ci",
        scope=["m"],
        suppressed=[],
        fingerprints=["other target"],
        now=start + timedelta(hours=2),
    )
    applied = (start + timedelta(hours=2)).isoformat()
    baseline = attr.load_baseline(tmp_path, "dev", before=applied)
    assert baseline is not None
    assert baseline.fingerprints == {"b"}
    assert baseline.invocation_id == "inv-1"
    # An unapplied plan's tree was last swept as the latest sweep.
    assert attr.load_baseline(tmp_path, "dev", before=None).fingerprints == {"c"}
    assert attr.load_baseline(tmp_path, "dev", before=start.isoformat()) is None
    assert attr.load_baseline(tmp_path, "prod", before=None) is None


def test_the_history_keeps_the_latest_sweeps_and_survives_a_bad_file(tmp_path: Path):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for minute in range(attr.MAX_SWEEPS + 3):
        attr.record_sweep(
            tmp_path,
            "dev",
            scope=[],
            suppressed=[],
            fingerprints=[str(minute)],
            now=start + timedelta(minutes=minute),
        )
    document = json.loads((tmp_path / attr.HISTORY).read_text())
    assert len(document["sweeps"]) == attr.MAX_SWEEPS
    assert document["sweeps"][0]["fingerprints"] == ["3"]

    (tmp_path / attr.HISTORY).write_text("{not json")
    assert attr.load_baseline(tmp_path, "dev", before=None) is None
    # A write over an unreadable file starts a fresh history.
    attr.record_sweep(tmp_path, "dev", scope=[], suppressed=[], fingerprints=["x"])
    assert attr.load_baseline(tmp_path, "dev", before=None).fingerprints == {"x"}


# --- the plan's reach ----------------------------------------------------------


def _manifest() -> dict:
    def node(name: str, path: str, parents: list[str], **extra) -> tuple[str, dict]:
        return f"model.p.{name}", {
            "name": name,
            "resource_type": "model",
            "original_file_path": path,
            "depends_on": {"nodes": parents},
            **extra,
        }

    nodes = dict(
        [
            node("stg_orders", "models/stg_orders.sql", []),
            node(
                "int_orders",
                "models/int_orders.sql",
                ["model.p.stg_orders"],
                patch_path="p://models/schema.yml",
            ),
            node("fct_orders", "models/fct_orders.sql", ["model.p.int_orders"]),
            node("dim_users", "models/dim_users.sql", []),
        ]
    )
    return {"nodes": nodes}


def _edit(path: str, kind: EditKind = EditKind.MODEL_SQL, **extra) -> PlanEdit:
    return PlanEdit(path=path, kind=kind, new_content="select 1", **extra)


def test_the_reach_is_the_edited_models_and_their_descendants():
    reach = attr.plan_reach(
        [
            _edit("models/int_orders.sql"),
            _edit("models/stg_orders.sql", old_content_hash="abc"),
        ],
        _manifest(),
    )
    assert reach.reached == {"stg_orders", "int_orders", "fct_orders"}
    # A model edit with no preimage is a model the plan created.
    assert reach.created == {"int_orders"}
    assert reach.edited == {"stg_orders", "int_orders"}


def test_a_schema_yml_reaches_the_models_it_documents():
    reach = attr.plan_reach(
        [_edit("models/schema.yml", EditKind.SCHEMA_YML, old_content_hash="h")],
        _manifest(),
    )
    assert reach.reached == {"int_orders", "fct_orders"}


def test_a_macro_or_project_edit_can_reach_anything():
    for kind, path in (
        (EditKind.MACRO_SQL, "macros/m.sql"),
        (EditKind.PROJECT_YML, "dbt_project.yml"),
    ):
        reach = attr.plan_reach([_edit(path, kind, old_content_hash="h")], _manifest())
        assert reach.reached is None


# --- the classification --------------------------------------------------------


def test_classification_rules_in_order():
    reach = attr.plan_reach(
        [
            _edit("models/int_orders.sql"),
            _edit("models/stg_orders.sql", old_content_hash="h"),
        ],
        _manifest(),
    )
    seen = _finding("fct_orders", code="row_loss", axis="row_population")
    baseline = _baseline(fingerprints=frozenset({attr.fingerprint(seen)}))

    def judged(finding, base=baseline):
        result = attr.classify(finding, base, reach)
        return result.attribution, result.basis

    assert judged(seen) == ("existing", "baseline")
    assert judged(_finding("int_orders")) == ("introduced", "new_model")
    # dim_users is outside everything the plan reaches.
    assert judged(_finding("dim_users")) == ("existing", "lineage")
    assert judged(_finding("fct_orders")) == ("introduced", "baseline")
    suppressed = _baseline(suppressed=frozenset({"join_contract"}))
    assert judged(_finding("fct_orders"), suppressed) == (
        "unattributed",
        "baseline_suppressed",
    )
    assert judged(_finding("fct_orders"), None) == ("unattributed", "no_baseline")
    # The baseline never looked at stg_orders.
    unswept = _baseline(scope=frozenset({"fct_orders"}))
    assert judged(_finding("stg_orders"), unswept) == ("unattributed", "no_baseline")


def test_attribute_orders_introduced_first_and_keeps_each_class_ranking():
    reach = attr.plan_reach([_edit("models/int_orders.sql")], _manifest())
    findings = [
        _finding("dim_users", code="a"),
        _finding("fct_orders", code="b"),
        _finding("int_orders", code="c"),
        _finding("dim_users", code="d"),
    ]
    ordered, judged = attr.attribute(findings, None, reach)
    assert [f.code for f in ordered] == ["c", "b", "a", "d"]
    assert [j.attribution for j in judged] == [
        "introduced",
        "unattributed",
        "existing",
        "existing",
    ]


# --- what the completion block makes of it -------------------------------------


def _nodes(*names: str) -> list[BuildNode]:
    return [BuildNode(unique_id=_uid(n), name=n, status="success") for n in names]


def _verification(findings: list[dict], introduced: list[dict]) -> dict:
    counts = {"introduced": 0, "unattributed": 0, "existing": 0}
    for finding in findings:
        counts[finding["attribution"]] += 1
    return {
        "ran": True,
        "findings": findings,
        "finding_count": len(findings),
        "suppressed": {},
        "introduced": introduced,
        "attribution": {"ran": True, "counts": counts, "baseline": None},
    }


def _f(identifier: str, attribution: str, severity: str = "high", code: str = "x"):
    return {
        "axis": "join_contract",
        "code": code,
        "identifier": identifier,
        "severity": severity,
        "attribution": attribution,
    }


def test_an_introduced_high_finding_leads_and_the_outcome_is_not_validated():
    inherited = [_f(f"old_{i}", "existing") for i in range(20)]
    new = _f("int_orders", "introduced", code="join_no_match")
    verification = _verification(
        [new, *inherited],
        [{"code": "join_no_match", "identifier": "int_orders", "severity": "high"}],
    )
    completion = summarize_completion(
        _evidence(_nodes("int_orders", "fct_orders"), required=["int_orders"]),
        verification,
    )
    assert completion.outcome == "built_unverified"
    (reason,) = completion.unverified
    assert (reason.model, reason.reason, reason.detail) == (
        "int_orders",
        "introduced_high_finding",
        "join_no_match",
    )
    assert completion.verification.introduced[0].identifier == "int_orders"
    assert completion.verification.top[0].attribution == "introduced"
    assert completion.verification.by_attribution == {
        "existing": 20,
        "introduced": 1,
        "unattributed": 0,
    }
    # Leads the block: outcome first, then why.
    keys = list(completion.data())
    assert keys[:2] == ["outcome", "unverified"]


def test_inherited_findings_alone_leave_the_build_validated():
    verification = _verification([_f("dim_users", "existing")], [])
    completion = summarize_completion(
        _evidence(_nodes("int_orders"), required=["int_orders"]), verification
    )
    assert completion.outcome == "validated"
    assert completion.unverified is None


def test_an_unattributed_high_finding_counts_only_on_an_edited_model():
    on_edit = _verification([_f("int_orders", "unattributed")], [])
    downstream = _verification([_f("fct_orders", "unattributed")], [])
    evidence = _evidence(_nodes("int_orders", "fct_orders"), required=["int_orders"])
    flagged = summarize_completion(evidence, on_edit)
    assert [u.reason for u in flagged.unverified] == ["unattributed_high_finding"]
    assert summarize_completion(evidence, downstream).outcome == "validated"


def test_an_uncompared_rewrite_is_unfinished_only_where_the_baseline_was_ready():
    evidence = _evidence(_nodes("fct_orders"), required=["fct_orders"])

    def outcome(**state):
        return summarize_completion(
            evidence,
            None,
            comparisons=[
                ComparisonState(model="fct_orders", status="not_compared", **state)
            ],
        )

    ready = outcome(baseline_ready=True)
    assert ready.outcome == "built_unverified"
    assert [u.reason for u in ready.unverified] == ["comparison_not_run"]
    assert outcome(baseline_ready=False).outcome == "validated"
    # A plan stored before readiness was recorded says nothing either way.
    assert outcome().outcome == "validated"


def test_a_compared_rewrite_is_not_unfinished():
    completion = summarize_completion(
        _evidence(_nodes("fct_orders"), required=["fct_orders"]),
        None,
        comparisons=[
            ComparisonState(
                model="fct_orders", status="equivalent", baseline_ready=True
            )
        ],
    )
    assert completion.outcome == "validated"


def test_a_failed_build_stays_failed():
    nodes = [BuildNode(unique_id=_uid("fct_orders"), name="fct_orders", status="error")]
    completion = summarize_completion(
        _evidence(nodes, required=["fct_orders"]),
        None,
        comparisons=[
            ComparisonState(
                model="fct_orders", status="not_compared", baseline_ready=True
            )
        ],
    )
    assert completion.outcome == "failed"


def test_no_plan_means_nothing_can_be_unfinished():
    verification = _verification(
        [_f("int_orders", "introduced")],
        [{"code": "x", "identifier": "int_orders", "severity": "high"}],
    )
    completion = summarize_completion(_evidence(_nodes("int_orders")), verification)
    assert completion.outcome == "validated"


def test_data_outcome_reads_the_completion_outcome_and_evidence_keeps_its_own():
    evidence = _evidence(_nodes("fct_orders"), required=["fct_orders"])
    completion = summarize_completion(
        evidence,
        None,
        comparisons=[
            ComparisonState(
                model="fct_orders", status="not_compared", baseline_ready=True
            )
        ],
    )
    result = BuildResult(
        success=True,
        summary={"success": True},
        evidence=evidence,
        completion=completion,
    )
    data = result.data()
    assert data["outcome"] == "built_unverified"
    assert data["evidence"]["outcome"] == "validated"


def test_many_reasons_with_long_names_still_fit_the_bound():
    names = [f"model_{i}_" + "x" * 300 for i in range(12)]
    findings = [_f(n, "introduced") for n in names]
    verification = _verification(
        findings,
        [{"code": "x", "identifier": n, "severity": "high"} for n in names],
    )
    completion = summarize_completion(
        _evidence(_nodes(*names), required=names), verification
    )
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert completion.outcome == "built_unverified"
    assert completion.unverified, completion.elided
    assert completion.elided["unverified"] >= 1


def test_a_declared_fact_is_held_to_its_own_class_not_the_whole_axis():
    """A sweep routinely suppresses `unit` ("nothing declares a unit") while it
    runs the rest; that silence says nothing about a population delta."""

    reach = attr.plan_reach(
        [_edit("models/fct_orders.sql", old_content_hash="h")], _manifest()
    )
    baseline = _baseline(suppressed=frozenset({"unit", "additivity", "population"}))

    def judged(code: str):
        finding = _finding("fct_orders", code=code, axis="declaration")
        result = attr.classify(finding, baseline, reach)
        return result.attribution, result.basis

    assert judged("population_delta") == ("introduced", "baseline")
    assert judged("parent_population_missing") == ("introduced", "baseline")
    assert judged("unit_out_of_range") == ("unattributed", "baseline_suppressed")
    assert judged("population_contradicted") == (
        "unattributed",
        "baseline_suppressed",
    )
    assert judged("something_new") == ("unattributed", "baseline_suppressed")


# --- review follow-ups ----------------------------------------------------------


def test_a_check_the_baseline_left_unchecked_on_the_model_is_not_evidence():
    """The baseline swept the model, but could not probe this join."""

    payload = {
        "join_coverage": [
            {"model": "fct_orders", "status": "unchecked", "join": "o join u"},
            {"model": "fct_orders", "status": "satisfied", "join": "o join p"},
        ],
        "column_contract": {"unchecked": [{"model": "dim_users"}]},
    }
    keys = attr.unchecked_keys(payload)
    assert keys == ["join_contract|fct_orders|o join u", "build|dim_users|"]
    baseline = _baseline(unchecked=frozenset(keys))
    reach = None

    def judged(finding):
        result = attr.classify(finding, baseline, reach)
        return result.attribution, result.basis

    unprobed = _finding("fct_orders", data={"join": "o join u"})
    probed = _finding("fct_orders", data={"join": "o join p"})
    column = _finding("dim_users", code="column_missing", axis="build")
    failed = _finding("dim_users", code="node_failed", axis="build")
    assert judged(unprobed) == ("unattributed", "baseline_suppressed")
    assert judged(probed) == ("introduced", "baseline")
    assert judged(column) == ("unattributed", "baseline_suppressed")
    assert judged(failed) == ("introduced", "baseline")


def test_a_schema_yml_that_declares_a_source_reaches_its_consumers():
    manifest = _manifest()
    manifest["sources"] = {
        "source.p.raw.orders": {
            "name": "orders",
            "resource_type": "source",
            "original_file_path": "models/sources.yml",
        }
    }
    manifest["nodes"]["model.p.stg_orders"]["depends_on"]["nodes"] = [
        "source.p.raw.orders"
    ]
    reach = attr.plan_reach(
        [_edit("models/sources.yml", EditKind.SCHEMA_YML, old_content_hash="h")],
        manifest,
    )
    assert reach.reached is not None
    assert {"stg_orders", "int_orders", "fct_orders"} <= reach.reached
    assert "dim_users" not in reach.reached


def test_an_unapplied_plan_is_judged_against_sweeps_before_it_was_made(
    tmp_path: Path, monkeypatch
):
    from types import SimpleNamespace

    from exmergo_dex_core.transform import commands

    start = datetime(2026, 11, 1, tzinfo=UTC)
    attr.record_sweep(
        tmp_path, "dev", scope=[], suppressed=[], fingerprints=["before"], now=start
    )
    attr.record_sweep(
        tmp_path,
        "dev",
        scope=[],
        suppressed=[],
        fingerprints=["the plan's own build"],
        now=start + timedelta(hours=2),
    )
    plan = SimpleNamespace(
        applied_at=None, created_at=(start + timedelta(hours=1)).isoformat()
    )
    engine = SimpleNamespace(
        require_full_store=lambda what: SimpleNamespace(load_plan=lambda pid: plan)
    )
    monkeypatch.setattr(commands, "_plan_edits", lambda *a: [])
    given = commands._attribution_input(engine, "p1", None, tmp_path, "dev")
    assert given.baseline.fingerprints == {"before"}


def test_apply_records_whether_the_baseline_is_ready_now(monkeypatch):
    from types import SimpleNamespace

    from exmergo_dex_core.transform import commands, refactor_compare

    saved = []
    stored = SimpleNamespace(
        refactor_models={"fct_orders": "unattributed", "dim_users": "none"},
        refactor_baseline_ready={"fct_orders": False, "dim_users": False},
        edit_target="project",
    )
    store = SimpleNamespace(save_plan=saved.append)
    engine = SimpleNamespace(project_dir=lambda: Path())
    # The repair since planning built fct_orders' parent; dim_users' still fails.
    monkeypatch.setattr(
        refactor_compare,
        "_blocked_parents",
        lambda project, model, exclude: [] if model == "fct_orders" else ["x"],
    )
    commands._refresh_baseline_ready(engine, store, stored)
    assert stored.refactor_baseline_ready == {"fct_orders": True, "dim_users": False}
    assert saved == [stored]


def test_a_comparison_elsewhere_excuses_a_rewrite_only_when_it_held():
    evidence = _evidence(_nodes("int_a", "fct_b"), required=["int_a", "fct_b"])

    def reasons(other: str):
        completion = summarize_completion(
            evidence,
            None,
            comparisons=[
                ComparisonState(
                    model="int_a", status="not_compared", baseline_ready=True
                ),
                ComparisonState(model="fct_b", status=other, baseline_ready=True),
            ],
        )
        return {(u.model, u.reason) for u in completion.unverified or []}

    assert reasons("equivalent") == set()
    assert ("int_a", "comparison_not_run") in reasons("not_checked")
    assert reasons("different") == {
        ("int_a", "comparison_not_run"),
        ("fct_b", "comparison_contradicted"),
    }


def test_an_unattributed_high_finding_past_the_cap_still_counts():
    verification = _verification([_f("other", "introduced", severity="low")], [])
    verification["attribution"]["unattributed_high"] = [
        {"code": "join_no_match", "identifier": "int_orders"}
    ]
    completion = summarize_completion(
        _evidence(_nodes("int_orders"), required=["int_orders"]), verification
    )
    assert [(u.model, u.reason) for u in completion.unverified] == [
        ("int_orders", "unattributed_high_finding")
    ]


def test_a_plan_never_takes_its_own_sweep_as_its_baseline(tmp_path: Path):
    """A pre-apply build of a checkout already holding the edits records a sweep
    tagged with the plan; after apply it must not become the baseline."""

    start = datetime(2026, 11, 1, tzinfo=UTC)
    attr.record_sweep(
        tmp_path, "dev", scope=[], suppressed=[], fingerprints=["clean"], now=start
    )
    attr.record_sweep(
        tmp_path,
        "dev",
        scope=[],
        suppressed=[],
        fingerprints=["the plan's"],
        plan_id="p1",
        now=start + timedelta(hours=1),
    )
    applied = (start + timedelta(hours=2)).isoformat()
    assert attr.load_baseline(
        tmp_path, "dev", before=applied, plan_id="p1"
    ).fingerprints == {"clean"}
    # Another plan may use it: that sweep is the tree it was applied onto.
    assert attr.load_baseline(
        tmp_path, "dev", before=applied, plan_id="p2"
    ).fingerprints == {"the plan's"}


def test_a_sweep_recorded_without_per_model_coverage_introduces_nothing(
    tmp_path: Path,
):
    history = {
        "schema_version": 1,
        "sweeps": [
            {
                "target": "dev",
                "recorded_at": "2026-11-01T00:00:00+00:00",
                "scope": ["dim_users"],
                "suppressed": [],
                "fingerprints": [],
            }
        ],
    }
    (tmp_path / attr.HISTORY).write_text(json.dumps(history))
    baseline = attr.load_baseline(tmp_path, "dev", before=None)
    assert baseline is not None and baseline.coverage_known is False
    column = _finding("dim_users", code="column_missing", axis="build")
    result = attr.classify(column, baseline, None)
    assert (result.attribution, result.basis) == (
        "unattributed",
        "baseline_suppressed",
    )


def test_every_unattributed_high_finding_reaches_completion():
    """More than fifty of them on other models must not hide one on the plan's."""

    findings = [_finding(f"other_{i}") for i in range(60)] + [_finding("int_orders")]
    judged = [attr.Attribution("unattributed", "no_baseline")] * len(findings)
    refs = attr.unattributed_high_refs(findings, judged)
    assert len(refs) == 61
    verification = _verification([], [])
    verification["attribution"]["unattributed_high"] = refs
    completion = summarize_completion(
        _evidence(_nodes("int_orders"), required=["int_orders"]), verification
    )
    assert [(u.model, u.reason) for u in completion.unverified] == [
        ("int_orders", "unattributed_high_finding")
    ]
