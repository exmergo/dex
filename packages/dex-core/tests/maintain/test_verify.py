"""maintain verify: a baseline-free sweep (#224), starting with build-status
gaps read from the compiled manifest and the last run's run_results.json,
plus a project that fails to compile (#225)."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from exmergo_dex_core.maintain import verify as verify_mod
from exmergo_dex_core.maintain.verify import (
    build_status_findings,
    column_contract_findings,
    column_contract_plan,
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


# --- column_contract_plan / column_contract_findings: pure (#230) --------------


class _ColumnAdapter:
    """Just ``table_metadata``, keyed by identifier."""

    def __init__(self, columns_by_identifier: dict[str, list[tuple[str, str]]]):
        self._columns = columns_by_identifier

    def table_metadata(self, identifier: str):
        cols = [
            types.SimpleNamespace(name=name, data_type=data_type)
            for name, data_type in self._columns[identifier]
        ]
        return types.SimpleNamespace(identifier=identifier), cols


def _node_with_columns(name: str, columns: dict[str, str | None]) -> dict:
    return {
        "name": name,
        "resource_type": "model",
        "columns": {
            col_name: ({"data_type": data_type} if data_type else {})
            for col_name, data_type in columns.items()
        },
    }


def test_no_manifest_is_a_note_not_a_finding(tmp_path: Path):
    declared, undeclared, notes = column_contract_plan(tmp_path)
    assert declared == {}
    assert undeclared == 0
    assert notes and "no compiled manifest found" in notes[0]


def test_a_model_with_no_columns_key_counts_as_undeclared(tmp_path: Path):
    _write_artifacts(
        tmp_path,
        nodes={
            "model.p.a": {"name": "a", "resource_type": "model"},
            "model.p.b": _node_with_columns("b", {"id": "INTEGER"}),
        },
        results=[],
    )
    declared, undeclared, notes = column_contract_plan(tmp_path)
    assert notes == []
    assert undeclared == 1
    assert set(declared) == {"b"}
    assert declared["b"] == {"id": "INTEGER"}


def test_a_column_with_no_declared_type_maps_to_none(tmp_path: Path):
    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": _node_with_columns("a", {"id": None})},
        results=[],
    )
    declared, _undeclared, _notes = column_contract_plan(tmp_path)
    assert declared["a"] == {"id": None}


def test_a_missing_declared_column_is_reported_high_severity():
    findings, notes = column_contract_findings(
        _ColumnAdapter({"db.main.a": [("id", "INTEGER")]}),
        declared_by_model={"a": {"id": "INTEGER", "email": "VARCHAR"}},
        model_relations={"a": "db.main.a"},
        live_identifiers=["db.main.a"],
        undeclared=0,
    )
    assert notes == []
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "column_missing"
    assert finding.identifier == "a"
    assert finding.severity == "high"
    assert "email" in finding.detail
    assert finding.data == {"model": "a", "columns": ["email"]}


def test_an_undeclared_column_is_reported_low_severity():
    findings, _notes = column_contract_findings(
        _ColumnAdapter({"db.main.a": [("id", "INTEGER"), ("extra", "VARCHAR")]}),
        declared_by_model={"a": {"id": "INTEGER"}},
        model_relations={"a": "db.main.a"},
        live_identifiers=["db.main.a"],
        undeclared=0,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "column_undeclared"
    assert finding.severity == "low"
    assert "extra" in finding.detail


def test_a_declared_type_that_disagrees_is_a_medium_severity_mismatch():
    findings, _notes = column_contract_findings(
        _ColumnAdapter({"db.main.a": [("id", "VARCHAR")]}),
        declared_by_model={"a": {"id": "INTEGER"}},
        model_relations={"a": "db.main.a"},
        live_identifiers=["db.main.a"],
        undeclared=0,
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "column_type_mismatch"
    assert finding.severity == "medium"
    assert finding.data["mismatches"] == [
        {"column": "id", "declared": "INTEGER", "actual": "VARCHAR"}
    ]


def test_a_matching_contract_reports_nothing():
    findings, _notes = column_contract_findings(
        _ColumnAdapter({"db.main.a": [("id", "INTEGER")]}),
        declared_by_model={"a": {"id": "INTEGER"}},
        model_relations={"a": "db.main.a"},
        live_identifiers=["db.main.a"],
        undeclared=0,
    )
    assert findings == []


def test_undeclared_models_are_named_once_at_the_summary_level_not_per_model():
    findings, notes = column_contract_findings(
        _ColumnAdapter({"db.main.a": [("id", "INTEGER")]}),
        declared_by_model={"a": {"id": "INTEGER"}},
        model_relations={"a": "db.main.a"},
        live_identifiers=["db.main.a"],
        undeclared=3,
    )
    assert findings == []
    assert len(notes) == 1
    assert "3 model(s)" in notes[0]


def test_a_model_with_no_matching_relation_is_named_in_notes_not_a_finding():
    findings, notes = column_contract_findings(
        _ColumnAdapter({}),
        declared_by_model={"a": {"id": "INTEGER"}},
        model_relations={"a": "db.main.a"},
        live_identifiers=[],
        undeclared=0,
    )
    assert findings == []
    assert notes and "a" in notes[0]


# --- grain_plan / grain_findings: pure (#229) -----------------------------------


class _GrainAdapter:
    """A fake adapter exposing exactly the grain-check surface: schema-only
    metadata, exact distinct counts (single-column and combination), and
    approximate column aggregates."""

    def __init__(
        self,
        *,
        row_counts: dict[str, int],
        columns: dict[str, list[str]],
        exact: dict[str, dict[str, int]] | None = None,
        combos: dict[str, dict[tuple[str, ...], int]] | None = None,
        approx: dict[str, dict[str, tuple[int, float | None]]] | None = None,
    ):
        self._row_counts = row_counts
        self._columns = columns
        self._exact = exact or {}
        self._combos = combos or {}
        self._approx = approx or {}

    def table_metadata(self, identifier: str):
        cols = [types.SimpleNamespace(name=name) for name in self._columns[identifier]]
        meta = types.SimpleNamespace(
            identifier=identifier, row_count=self._row_counts[identifier]
        )
        return meta, cols

    def exact_distinct_counts(self, identifier: str, columns: list[str]):
        return {
            col: count
            for col, count in self._exact.get(identifier, {}).items()
            if col in columns
        }

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ):
        wanted = {tuple(combo) for combo in combinations}
        return {
            combo: count
            for combo, count in self._combos.get(identifier, {}).items()
            if combo in wanted
        }

    def column_aggregates(self, identifier: str, columns):
        result = []
        for col in columns:
            entry = self._approx.get(identifier, {}).get(col.name)
            if entry is None:
                continue
            distinct_count, null_fraction = entry
            result.append(
                types.SimpleNamespace(
                    name=col.name,
                    distinct_count=distinct_count,
                    null_fraction=null_fraction,
                )
            )
        return result


def _defs(*, keys=(), composites=(), primary=None):
    from exmergo_dex_core.dbt_project import (
        DeclaredCompositeKey,
        DeclaredKey,
        ProjectDefinitions,
    )

    return ProjectDefinitions(
        present=True,
        declared_keys=[
            DeclaredKey(model=model, column=column, unique=unique, source="manifest")
            for model, column, unique in keys
        ],
        declared_composite_keys=[
            DeclaredCompositeKey(model=model, columns=list(columns), source="manifest")
            for model, columns in composites
        ],
        primary_entities=dict(primary or {}),
    )


def test_grain_plan_prefers_a_declared_unique_test_over_a_primary_entity():
    defs = _defs(
        keys=[("a", "id", True)],
        primary=[("a", "other_col")],
    )
    declared = verify_mod.grain_plan(defs)
    assert len(declared["a"]) == 1
    assert declared["a"][0].source == "unique_test"
    assert declared["a"][0].columns == ["id"]


def test_grain_plan_falls_back_to_a_composite_when_no_single_column_test_exists():
    defs = _defs(composites=[("a", ["order_id", "line_no"])], primary=[("a", "id")])
    declared = verify_mod.grain_plan(defs)
    assert len(declared["a"]) == 1
    assert declared["a"][0].source == "unique_test_composite"
    assert declared["a"][0].columns == ["order_id", "line_no"]


def test_grain_plan_falls_back_to_the_primary_entity_when_nothing_is_declared():
    defs = _defs(primary=[("a", "order_id")])
    declared = verify_mod.grain_plan(defs)
    assert declared["a"] == [
        verify_mod.GrainCandidate(columns=["order_id"], source="primary_entity")
    ]


def test_grain_plan_checks_every_independently_declared_unique_column():
    defs = _defs(keys=[("a", "id", True), ("a", "email", True), ("a", "note", False)])
    declared = verify_mod.grain_plan(defs)
    assert {c.columns[0] for c in declared["a"]} == {"id", "email"}


def test_grain_plan_scope_is_lowercase_like_column_contract_plan():
    defs = _defs(keys=[("a", "id", True), ("b", "id", True)])
    declared = verify_mod.grain_plan(defs, scope={"a"})
    assert set(declared) == {"a"}


def test_grain_plan_reports_nothing_for_a_model_with_no_declaration():
    defs = _defs(keys=[("a", "id", True)])
    declared = verify_mod.grain_plan(defs)
    assert "b" not in declared


def test_a_declared_unique_key_with_duplicates_is_reported():
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 200},
        columns={"db.main.a": ["id"]},
        exact={"db.main.a": {"id": 199}},
    )
    findings, notes = verify_mod.grain_findings(
        adapter,
        {"a": [verify_mod.GrainCandidate(columns=["id"], source="unique_test")]},
        {"a": "db.main.a"},
        ["db.main.a"],
        {"a"},
    )
    assert notes == []
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "grain_broken"
    assert finding.identifier == "a"
    assert finding.exact is True
    assert finding.data["duplicate_count"] == 1
    assert finding.severity == "high"


def test_a_declared_composite_with_duplicates_is_reported():
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 10},
        columns={"db.main.a": ["order_id", "line_no"]},
        combos={"db.main.a": {("order_id", "line_no"): 8}},
    )
    findings, notes = verify_mod.grain_findings(
        adapter,
        {
            "a": [
                verify_mod.GrainCandidate(
                    columns=["order_id", "line_no"], source="unique_test_composite"
                )
            ]
        },
        {"a": "db.main.a"},
        ["db.main.a"],
        {"a"},
    )
    assert notes == []
    assert len(findings) == 1
    assert findings[0].data["duplicate_count"] == 2
    assert findings[0].data["columns"] == ["order_id", "line_no"]


def test_a_proven_unique_declared_grain_reports_nothing():
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 10},
        columns={"db.main.a": ["id"]},
        exact={"db.main.a": {"id": 10}},
    )
    findings, notes = verify_mod.grain_findings(
        adapter,
        {"a": [verify_mod.GrainCandidate(columns=["id"], source="unique_test")]},
        {"a": "db.main.a"},
        ["db.main.a"],
        {"a"},
    )
    assert findings == []
    assert notes == []


def test_a_composite_the_heuristic_cannot_find_is_reported_unknown_not_broken():
    # No declared source at all, and no id-shaped column exists: the model's
    # true grain may well be a composite, but the free naming heuristic never
    # invents one, so this must come back as "unknown", never "broken".
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 10},
        columns={"db.main.a": ["status", "amount"]},
    )
    findings, notes = verify_mod.grain_findings(
        adapter, {}, {"a": "db.main.a"}, ["db.main.a"], {"a"}
    )
    assert findings == []
    assert any("could not be determined" in n and "a" in n for n in notes)


def test_the_heuristic_escalates_a_near_unique_id_shaped_column_to_an_exact_check():
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 100},
        columns={"db.main.a": ["order_id", "status"]},
        approx={"db.main.a": {"order_id": (99, 0.0)}},
        exact={"db.main.a": {"order_id": 98}},
    )
    findings, notes = verify_mod.grain_findings(
        adapter, {}, {"a": "db.main.a"}, ["db.main.a"], {"a"}
    )
    assert notes == []
    assert len(findings) == 1
    finding = findings[0]
    assert finding.exact is True
    assert finding.data["duplicate_count"] == 2
    assert finding.data["source"] == "heuristic"


def test_the_heuristic_reports_a_clearly_broken_grain_from_the_approximate_count():
    # Far below near-unique, so this is reported straight from the
    # approximate count rather than paying for an exact scan to confirm
    # what is already obvious -- honestly marked exact=False.
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 100},
        columns={"db.main.a": ["order_id"]},
        approx={"db.main.a": {"order_id": (40, 0.0)}},
    )
    findings, notes = verify_mod.grain_findings(
        adapter, {}, {"a": "db.main.a"}, ["db.main.a"], {"a"}
    )
    assert notes == []
    assert len(findings) == 1
    finding = findings[0]
    assert finding.exact is False
    assert finding.data["duplicate_count"] == 60
    assert "approximately" in finding.detail


def test_the_heuristic_skips_a_column_with_nulls_as_a_key_candidate():
    adapter = _GrainAdapter(
        row_counts={"db.main.a": 100},
        columns={"db.main.a": ["order_id"]},
        approx={"db.main.a": {"order_id": (95, 0.1)}},
    )
    findings, notes = verify_mod.grain_findings(
        adapter, {}, {"a": "db.main.a"}, ["db.main.a"], {"a"}
    )
    assert findings == []
    assert any("could not be determined" in n for n in notes)


def test_grain_is_not_checked_for_an_ambiguous_relation_match():
    adapter = _GrainAdapter(row_counts={}, columns={})
    findings, notes = verify_mod.grain_findings(
        adapter, {}, {"a": "db.main.a"}, [], {"a"}
    )
    assert findings == []
    assert any("was not checked" in n and "a" in n for n in notes)


def test_a_model_with_no_relation_is_silently_skipped():
    # missing_relation_findings already reports this; grain adds nothing.
    adapter = _GrainAdapter(row_counts={}, columns={})
    findings, notes = verify_mod.grain_findings(adapter, {}, {}, [], {"a"})
    assert findings == []
    assert notes == []


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


def test_verify_reports_a_missing_declared_column_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """#230: schema.yml declares a column the real, built ``stg_orders``
    relation does not have. Free on every connector: no ``--confirm`` needed,
    both sides are metadata."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": _node_with_columns(
                "stg_orders",
                {
                    "order_id": "INTEGER",
                    "customer_id": "INTEGER",
                    "amount": "DOUBLE",
                    "status": "VARCHAR",
                    "ordered_at": "DATE",
                    "region": "VARCHAR",
                },
            )
            | {"relation_name": '"warehouse"."main"."stg_orders"'}
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "column_missing"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert "region" in findings[0]["detail"]
    assert "column_contract" not in payload["data"]["suppressed"]


def test_verify_scoped_by_object_name_keeps_the_column_finding(
    maintain_repo, _assume_the_project_compiles
):
    """`maintain verify <object>` narrows the column contract to the object
    named, both ways: the named model's finding survives, and a request for
    an unrelated name checks nothing (no finding, no "unchecked" note about
    a model that was never in scope to begin with)."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": _node_with_columns(
                "stg_orders",
                {
                    "order_id": "INTEGER",
                    "customer_id": "INTEGER",
                    "amount": "DOUBLE",
                    "status": "VARCHAR",
                    "ordered_at": "DATE",
                    "region": "VARCHAR",
                },
            )
            | {"relation_name": '"warehouse"."main"."stg_orders"'}
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify", "stg_orders")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "column_missing"]
    assert len(findings) == 1
    assert findings[0]["identifier"] == "stg_orders"

    rc, payload = maintain_repo.dex("maintain", "verify", "customers")
    assert rc == 0 and payload["status"] == "ok", payload
    assert payload["data"]["findings"] == []
    assert not any("stg_orders" in w for w in payload["warnings"])


def test_verify_reports_an_undeclared_column_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """#230's other direction, at lower severity: the built relation has
    columns schema.yml never declared."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": _node_with_columns(
                "stg_orders", {"order_id": "INTEGER", "customer_id": "INTEGER"}
            )
            | {"relation_name": '"warehouse"."main"."stg_orders"'}
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [
        f for f in payload["data"]["findings"] if f["code"] == "column_undeclared"
    ]
    assert len(findings) == 1
    assert findings[0]["severity"] == "low"
    for name in ("amount", "status", "ordered_at"):
        assert name in findings[0]["detail"]


def test_verify_reports_no_columns_declared_once_at_summary_level(
    maintain_repo, _assume_the_project_compiles
):
    """#230's third acceptance bullet: a model with no schema.yml entry
    reports nothing per model, and the gap is named once, not per model."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."stg_orders"',
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert [
        f
        for f in payload["data"]["findings"]
        if f["code"] in ("column_missing", "column_undeclared", "column_type_mismatch")
    ] == []
    assert any("declare no columns" in w for w in payload["warnings"])


def _unique_test_node(model_unique_id: str, column: str) -> dict:
    """A compiled ``unique`` test node, the shape ``_declared_from_manifest``
    reads: ``attached_node`` names the model this test belongs to."""

    return {
        "resource_type": "test",
        "test_metadata": {"name": "unique", "kwargs": {"column_name": column}},
        "attached_node": model_unique_id,
    }


def test_verify_reports_a_broken_grain_from_the_declared_primary_entity(
    maintain_repo, _assume_the_project_compiles
):
    """#229: `stg_orders` has no unique test in this manifest, so its
    semantic layer's declared primary entity (`order_id`, read straight from
    the project's own real semantic YAML, since there is no compiled
    semantic manifest here) is what stands in as the intended grain."""

    maintain_repo.sql("UPDATE stg_orders SET order_id = 1 WHERE order_id = 2")
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."stg_orders"',
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "grain_broken"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["identifier"] == "stg_orders"
    assert finding["data"]["source"] == "primary_entity"
    assert finding["data"]["duplicate_count"] == 1
    assert finding["exact"] is True


def test_verify_reports_a_broken_grain_from_a_declared_unique_test(
    maintain_repo, _assume_the_project_compiles
):
    """#229's first acceptance bullet: a declared unique key with duplicates
    is reported with a count. A unique test in the manifest outranks the
    semantic layer's primary entity for the same model and column."""

    maintain_repo.sql("UPDATE stg_orders SET order_id = 1 WHERE order_id = 2")
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."stg_orders"',
            },
            "test.maintain_test.unique_stg_orders_order_id": _unique_test_node(
                "model.maintain_test.stg_orders", "order_id"
            ),
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "grain_broken"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["data"]["source"] == "unique_test"
    assert finding["data"]["duplicate_count"] == 1
    assert finding["severity"] == "high"


def test_verify_reports_nothing_for_a_proven_unique_grain_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """#229's third acceptance bullet: a model with a proven unique grain
    reports nothing, even though a real distinct-count scan ran."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.stg_orders": {
                "name": "stg_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."stg_orders"',
            },
            "test.maintain_test.unique_stg_orders_order_id": _unique_test_node(
                "model.maintain_test.stg_orders", "order_id"
            ),
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert [f for f in payload["data"]["findings"] if f["code"] == "grain_broken"] == []
    assert "grain" not in payload["data"]["suppressed"]


def test_verify_finds_a_broken_grain_via_the_naming_heuristic_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """#229's proposal: a model with no declared unique test and no semantic
    entity still gets checked, via the free ``id``-shaped naming guess, using
    the real ``customers`` table (a genuine warehouse object with no dbt
    model wrapping it in this manifest)."""

    maintain_repo.sql("UPDATE customers SET id = 1 WHERE id = 2")
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.dim_customers": {
                "name": "dim_customers",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."customers"',
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "grain_broken"]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["identifier"] == "dim_customers"
    assert finding["data"]["source"] == "heuristic"
    assert finding["data"]["duplicate_count"] == 1


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
    assert set(payload["data"]["suppressed"]) == {
        "build_status",
        "no_relation",
        "row_population",
        "column_contract",
        "grain",
    }


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


# --- row population: a model against the relation it is built from (#226) ------
#
# Pure, like the build-status half above: a manifest on disk in, findings out,
# with the counts passed in rather than measured, so every threshold and every
# suppression is asserted without a warehouse in the way.

WAREHOUSE = "wh.main"


def _model(name: str, sql: str, **extra) -> dict:
    return {
        "name": name,
        "resource_type": "model",
        "relation_name": f'"wh"."main"."{name}"',
        "compiled_code": sql,
        **extra,
    }


def _plan(tmp_path: Path, nodes: dict, sources: dict | None = None):
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes, "sources": sources or {}}), encoding="utf-8"
    )
    return verify_mod.row_population_plan(tmp_path, "duckdb")


def _judge(checks, counts: dict[str, int], counted: set[str] | None = None):
    keyed = {f"{WAREHOUSE}.{name}": rows for name, rows in counts.items()}
    measured = set(keyed) if counted is None else {f"{WAREHOUSE}.{n}" for n in counted}
    return verify_mod.row_population_findings(checks, keyed, measured)


def test_an_inner_join_that_loses_a_fifth_of_the_rows_is_reported(tmp_path: Path):
    """#226's first acceptance bullet, in the shape a dbt model actually
    compiles to: the driving parent is behind two CTEs, and the join that
    dropped the rows is named."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
            "m.2": _model("stg_customers", 'select * from "wh"."main"."customers"'),
            "m.3": _model(
                "fct_orders",
                'with source as (select * from "wh"."main"."stg_orders"), '
                "joined as (select s.order_id, c.name from source s inner join "
                '"wh"."main"."stg_customers" c on s.customer_id = c.customer_id) '
                "select * from joined",
            ),
        },
    )
    assert notes == []
    findings, _ = _judge(
        checks,
        {
            "orders": 200,
            "customers": 40,
            "stg_orders": 200,
            "stg_customers": 40,
            "fct_orders": 160,
        },
    )
    assert [f.code for f in findings] == ["row_loss"]
    finding = findings[0]
    assert finding.identifier == "fct_orders"
    assert finding.severity == "medium"
    assert finding.data["driving_parent"] == "stg_orders"
    assert "inner join to 'stg_customers'" in finding.detail


def test_the_finding_states_both_counts_so_a_reader_can_judge_it(tmp_path: Path):
    """#226's fourth acceptance bullet. The threshold is dex's opinion; the two
    counts are the evidence, and they have to be in the sentence."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
            "m.2": _model(
                "fct_orders",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."stg_customers" c on o.customer_id = c.id',
            ),
        },
    )
    findings, _ = _judge(
        checks,
        {"orders": 200, "stg_orders": 200, "fct_orders": 160, "stg_customers": 40},
    )
    assert "160 rows" in findings[0].detail and "200" in findings[0].detail
    assert findings[0].data["row_count"] == 160
    assert findings[0].data["parent_row_count"] == 200
    assert findings[0].data["change_fraction"] == -0.2


def test_a_group_by_model_with_fewer_rows_is_not_reported(tmp_path: Path):
    """#226's second acceptance bullet, and the one that decides whether the
    detector survives contact with a real project: an aggregate is *supposed*
    to hold fewer rows than its parent."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "daily",
                'select d, count(*) from "wh"."main"."stg_orders" group by d',
            )
        },
    )
    assert checks[0].reducers == ("a GROUP BY",)
    findings, _ = _judge(checks, {"stg_orders": 200, "daily": 12})
    assert findings == []


@pytest.mark.parametrize(
    ("sql", "reducer"),
    [
        ('select * from "wh"."main"."stg_orders" where status = 1', "a WHERE filter"),
        ('select distinct id from "wh"."main"."stg_orders"', "a DISTINCT"),
        ('select * from "wh"."main"."stg_orders" limit 10', "a LIMIT"),
        (
            'select * from "wh"."main"."stg_orders" qualify '
            "row_number() over (partition by id order by d) = 1",
            "a QUALIFY filter",
        ),
    ],
)
def test_every_clause_that_explains_a_shortfall_silences_the_finding(
    tmp_path: Path, sql: str, reducer: str
):
    checks, _ = _plan(tmp_path, {"m.1": _model("narrowed", sql)})
    assert reducer in checks[0].reducers
    findings, _ = _judge(checks, {"stg_orders": 200, "narrowed": 20})
    assert findings == []


def test_a_model_that_fans_out_is_reported_with_its_join_key(tmp_path: Path):
    """#226's third acceptance bullet: the key is what the reader acts on, so
    it is named rather than left to be found by opening the file."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "order_items",
                'select o.order_id, i.sku from "wh"."main"."stg_orders" o '
                'left join "wh"."main"."stg_items" i on o.order_id = i.order_id',
            )
        },
    )
    findings, _ = _judge(
        checks, {"stg_orders": 200, "stg_items": 500, "order_items": 500}
    )
    assert [f.code for f in findings] == ["row_fanout"]
    assert findings[0].severity == "high"
    assert "o.order_id = i.order_id" in findings[0].detail
    assert findings[0].data["join_keys"] == ["o.order_id = i.order_id"]


def test_a_union_is_not_mistaken_for_fanout(tmp_path: Path):
    """A set operation has more rows than either side by construction, and no
    single driving parent to compare against, so it is declared rather than
    reported."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": _model(
                "everything",
                'select id from "wh"."main"."stg_orders" union all '
                'select id from "wh"."main"."stg_items"',
            )
        },
    )
    assert checks == []
    assert "set operation" in notes[0]


def test_an_unnest_is_not_mistaken_for_fanout(tmp_path: Path):
    """A construct that turns one row into several is doing its job."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "exploded",
                'select o.id, t.tag from "wh"."main"."stg_orders" o '
                "cross join unnest(o.tags) as t(tag)",
            )
        },
    )
    assert checks[0].multiplies is True
    findings, _ = _judge(checks, {"stg_orders": 200, "exploded": 900})
    assert findings == []


def test_row_growth_with_no_join_at_all_is_not_reported(tmp_path: Path):
    """Fanout is a claim about a join. Without one, dex has no mechanism to
    name and says nothing rather than guessing."""

    checks, _ = _plan(
        tmp_path, {"m.1": _model("copy", 'select * from "wh"."main"."stg_orders"')}
    )
    findings, _ = _judge(checks, {"stg_orders": 200, "copy": 400})
    assert findings == []


def test_a_difference_inside_the_reporting_band_is_chatter(tmp_path: Path):
    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    findings, _ = _judge(checks, {"stg_orders": 200, "d": 40, "fct": 195})
    assert findings == []


def test_losing_most_of_the_rows_ranks_high(tmp_path: Path):
    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    findings, _ = _judge(checks, {"stg_orders": 200, "d": 40, "fct": 40})
    assert findings[0].severity == "high"


def test_an_incremental_model_is_skipped_and_the_note_says_why(tmp_path: Path):
    """The largest false-positive source there is: an incremental model holds
    what previous runs loaded, so it has no reason to match its parent."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": _model(
                "events",
                'select * from "wh"."main"."raw_events"',
                config={"materialized": "incremental"},
            )
        },
    )
    assert checks == []
    assert "incremental" in notes[0] and "events" in notes[0]


def test_an_ephemeral_model_contributes_nothing(tmp_path: Path):
    """It compiles with no relation of its own, so there is no count to
    compare; dbt inlines it into whatever reads it."""

    checks, notes = _plan(
        tmp_path,
        {
            "m.1": {
                "name": "helper",
                "resource_type": "model",
                "relation_name": None,
                "compiled_code": 'select * from "wh"."main"."orders"',
            }
        },
    )
    assert checks == [] and notes == []


def test_unparseable_compiled_sql_becomes_a_note_not_an_exception(tmp_path: Path):
    checks, notes = _plan(tmp_path, {"m.1": _model("broken", "select from from")})
    assert checks == []
    assert "broken" in notes[0]


def test_a_parent_with_no_row_count_is_named_rather_than_dropped(tmp_path: Path):
    """#172's inertness requirement applied here: an absent finding must not be
    indistinguishable from a clean one."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    findings, notes = _judge(checks, {"fct": 40})
    assert findings == []
    assert "fct" in notes[0] and "stg_orders" in notes[0]


def test_a_verdict_on_a_catalog_estimate_is_not_reported_as_exact(tmp_path: Path):
    """A catalog row count is an estimate, and the honesty flag is how a reader
    tells a proof from a strong signal, exactly as the volume axis does."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."stg_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    counts = {"stg_orders": 200, "d": 40, "fct": 100}
    estimated, _ = _judge(checks, counts, counted=set())
    measured, _ = _judge(checks, counts, counted=set(counts))
    assert estimated[0].exact is False
    assert measured[0].exact is True


def test_a_driving_parent_outside_the_project_still_counts(tmp_path: Path):
    """A model reading a relation the project never declares (a table loaded by
    something else) has a driving parent like any other; only the name dex can
    call it changes."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model(
                "fct",
                'select o.id from "wh"."main"."external_orders" o inner join '
                '"wh"."main"."d" c on o.cid = c.id',
            )
        },
    )
    assert checks[0].parent == "wh.main.external_orders"
    findings, _ = _judge(checks, {"external_orders": 200, "d": 40, "fct": 100})
    assert findings[0].code == "row_loss"


def test_verify_reports_row_loss_end_to_end(
    maintain_repo, _assume_the_project_compiles
):
    """The whole path against a real warehouse: manifest on disk, counts read
    from DuckDB, finding out. `fct_orders` holds 160 of `orders`' 200 rows and
    its SQL says it should hold all of them."""

    maintain_repo.sql(
        "CREATE TABLE fct_orders AS SELECT o.*, c.name FROM orders o "
        "JOIN customers c ON o.customer_id = c.id WHERE o.order_id <= 160"
    )
    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.fct_orders": {
                "name": "fct_orders",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."fct_orders"',
                "compiled_code": (
                    "select o.*, c.name from "
                    '"warehouse"."main"."orders" o inner join '
                    '"warehouse"."main"."customers" c on o.customer_id = c.id'
                ),
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    findings = [f for f in payload["data"]["findings"] if f["code"] == "row_loss"]
    assert len(findings) == 1, payload["data"]["findings"]
    assert findings[0]["identifier"] == "fct_orders"
    assert findings[0]["data"]["row_count"] == 160
    assert findings[0]["data"]["parent_row_count"] == 200
    assert findings[0]["data"]["driving_parent"] == "warehouse.main.orders"
    # DuckDB bills nothing, so the counts were measured rather than estimated
    # and the verdict is a proof.
    assert findings[0]["exact"] is True
    assert payload["cost"]["paradigm"] == "free_local"
    assert "row_population" not in payload["data"]["suppressed"]


def test_verify_says_so_when_no_model_can_be_lined_up(
    maintain_repo, _assume_the_project_compiles
):
    """A manifest with nothing comparable in it must not read as a clean bill
    of health for row population."""

    _write_artifacts(
        maintain_repo.project_dir,
        nodes={
            "model.maintain_test.events": {
                "name": "events",
                "resource_type": "model",
                "relation_name": '"warehouse"."main"."events"',
                "compiled_code": 'select * from "warehouse"."main"."orders"',
                "config": {"materialized": "incremental"},
            }
        },
        results=[],
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert "row_population" in payload["data"]["suppressed"]
    assert any("incremental" in w for w in payload["warnings"])


def test_a_join_is_named_by_the_model_it_reads_not_the_cte_alias(tmp_path: Path):
    """What a compiled dbt model actually looks like: the join in the final
    select reads a CTE called `lines`, which reads `stg_order_items`. Reporting
    `lines` would hand the reader an alias they cannot look up."""

    checks, _ = _plan(
        tmp_path,
        {
            "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
            "m.2": _model("stg_order_items", 'select * from "wh"."main"."items"'),
            "m.3": _model(
                "fct_order_lines",
                'with orders as (select * from "wh"."main"."stg_orders"), '
                'lines as (select * from "wh"."main"."stg_order_items"), '
                "final as (select orders.order_id, lines.sku from orders "
                "left join lines on orders.order_id = lines.order_id) "
                "select * from final",
            ),
        },
    )
    fanned = next(c for c in checks if c.model == "fct_order_lines")
    assert fanned.joins == ("left join to 'stg_order_items'",)
    # The key keeps the aliases the SQL uses, because that is the text the
    # reader will find when they open the file.
    assert fanned.join_keys == ("orders.order_id = lines.order_id",)


def test_a_project_that_was_never_compiled_says_so(
    maintain_repo, _assume_the_project_compiles
):
    """No manifest is not a clean project. The reason names the command that
    would produce one, the way the missing-run-results note does."""

    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert "no compiled manifest" in payload["data"]["suppressed"]["row_population"]
    assert any("dbt compile" in w for w in payload["warnings"])


# --- seams the build-time sweep shares with this one --------------------------


def test_a_warning_node_is_reported_rather_than_dropped(tmp_path: Path):
    """`warn` is neither a failure nor a skip, so it fell through both branches
    and was reported nowhere. A project that runs relationship tests at
    `severity: warn` over documented gaps has warnings by design; what it needs
    is a list to compare against last run's, not silence."""

    _write_artifacts(
        tmp_path,
        nodes={
            "test.p.relationships_orders_customer.abc123": {
                "name": "relationships_orders_customer",
                "resource_type": "test",
            }
        },
        results=[
            {
                "unique_id": "test.p.relationships_orders_customer.abc123",
                "status": "warn",
                "message": "got 12 results, configured to warn if != 0",
            }
        ],
    )
    findings, notes = build_status_findings(tmp_path)
    assert notes == []
    assert len(findings) == 1
    assert findings[0].code == "node_warned"
    assert findings[0].severity == "low"
    assert findings[0].identifier == "relationships_orders_customer"
    assert "12 results" in findings[0].detail


def test_a_passing_node_is_still_reported_nowhere(tmp_path: Path):
    """The other half of the same rule: a clean run has no findings at all."""

    _write_artifacts(
        tmp_path,
        nodes={"model.p.a": {"name": "a", "resource_type": "model"}},
        results=[{"unique_id": "model.p.a", "status": "success"}],
    )
    findings, _ = build_status_findings(tmp_path)
    assert findings == []


def test_the_plan_can_be_scoped_to_one_build_s_models(tmp_path: Path):
    """A caller verifying one build has a selection; the whole project is a
    different and more expensive question. The notes narrow with the scope,
    because a note about a model this caller never asked about reads as a gap
    in the answer it did ask for."""

    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    nodes = {
        "m.1": _model("stg_orders", 'select * from "wh"."main"."orders"'),
        "m.2": _model(
            "fct_orders",
            'select * from "wh"."main"."stg_orders"',
        ),
        "m.3": _model(
            "incremental_events",
            'select * from "wh"."main"."events"',
            config={"materialized": "incremental"},
        ),
    }
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes, "sources": {}}), encoding="utf-8"
    )

    checks, notes = verify_mod.row_population_plan(
        tmp_path, "duckdb", scope={"fct_orders"}
    )
    assert [c.model for c in checks] == ["fct_orders"]
    # The incremental model is outside the scope, so its skip is not this
    # caller's business either.
    assert notes == []

    everything, all_notes = verify_mod.row_population_plan(tmp_path, "duckdb")
    assert {c.model for c in everything} == {"stg_orders", "fct_orders"}
    assert any("incremental_events" in n for n in all_notes)


def test_the_row_count_handshake_can_be_injected(tmp_path: Path):
    """The gate primitive differs by caller and nothing else does: a standalone
    sweep prices a whole command, a sweep folded onto a build prices a phase
    against the reservation that build already holds."""

    class _Adapter:
        name = "fake"
        dialect = "duckdb"
        cost_gate = object()  # billed: the catalog answers where it can

        def query_estimate(self, sql):
            return 4096.0

    asked: list[tuple[float, int]] = []

    def handshake(estimate, relation_count):
        asked.append((estimate, relation_count))
        return None  # admitted: proceed to count

    class _Meta:
        def __init__(self, identifier, row_count=None):
            self.identifier = identifier
            self.row_count = row_count

    adapter = _Adapter()
    adapter.run_query = lambda sql, **kw: types.SimpleNamespace(
        columns=["dex_rows_0"], cells=[[7]]
    )
    measured = verify_mod.relation_counts(
        adapter,
        ["wh.main.a_view"],
        [_Meta("wh.main.a_view", None)],
        timeout_seconds=5.0,
        handshake=handshake,
    )
    assert asked == [(4096.0, 1)]
    assert measured.counts == {"wh.main.a_view": 7}
    assert measured.counted == {"wh.main.a_view"}
    assert measured.offer is None


def test_the_injected_handshake_can_defer_the_counts(tmp_path: Path):
    """A handshake that returns a request means nothing runs and the relations
    it would have counted are named as deferred, not as absent."""

    class _Adapter:
        name = "fake"
        dialect = "duckdb"
        cost_gate = object()

        def query_estimate(self, sql):
            return 4096.0

        def run_query(self, sql, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("a deferred count ran anyway")

    class _Meta:
        def __init__(self, identifier, row_count=None):
            self.identifier = identifier
            self.row_count = row_count

    sentinel = object()
    measured = verify_mod.relation_counts(
        _Adapter(),
        ["wh.main.a_view"],
        [_Meta("wh.main.a_view", None)],
        timeout_seconds=5.0,
        handshake=lambda estimate, count: sentinel,
    )
    assert measured.offer is sentinel
    assert measured.deferred == {"wh.main.a_view"}
    assert measured.counts == {}
