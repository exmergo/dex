"""A drafted parent measured against the built relation (#1228).

The statement is the declared-parent statement (#857) plus one count, the rows
whose child columns are all NULL or zero, so the judging is what is new: which
counts ask for a decision, which tier of assumption settles it, and that a
drafted parent never lands in ``parent_population`` or ``suppressed``. Real
DuckDB relations, with the manifest written by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.maintain import declared_facts as facts_mod

duckdb = pytest.importorskip("duckdb")

TABLES = ("CREATE TABLE parents AS SELECT * FROM (VALUES (1), (2), (3), (4)) t(pid)",)


def _manifest(project: Path) -> None:
    def model(name: str) -> dict:
        return {
            "name": name,
            "resource_type": "model",
            "relation_name": f'"warehouse"."main"."{name}"',
            "config": {"materialized": "table", "meta": {}},
            "columns": {},
        }

    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {"nodes": {"model.p.parents": model("parents"), "model.p.m": model("m")}}
        ),
        encoding="utf-8",
    )


def _rule(**fields) -> facts_mod.ParentRule:
    return facts_mod.ParentRule(
        model="parents",
        relation=None,
        key=(("pid", "pid"),),
        drafted=True,
        evidence="unique_key",
        supported_by=("unique_key",),
        detail="pid is unique on parents (a unique test)",
        **fields,
    )


def _run(tmp_path: Path, output: str, rule: facts_mod.ParentRule):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    _manifest(tmp_path)
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    for statement in (*TABLES, output):
        conn.execute(statement)
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, notes = facts_mod.plan(tmp_path, drafted={"m": rule})
        assert notes == []
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live)
        return prepared, facts_mod.findings(adapter, prepared)
    finally:
        adapter.close()


def _only(result) -> dict:
    [record] = result.population_delta
    return record


def test_orphans_and_missing_parents_ask_with_a_high_finding(tmp_path: Path):
    _prepared, result = _run(
        tmp_path,
        "CREATE TABLE m AS SELECT * FROM (VALUES (1, 2), (2, 1), (9, 1)) t(pid, n)",
        _rule(empty_columns=("n",)),
    )
    record = _only(result)
    assert record["status"] == "needs_decision"
    assert record["drafted"]["parent"] == "parents"
    assert record["drafted"]["evidence"] == "unique_key"
    assert "declared" not in record
    measured = record["measured"]
    assert (measured["orphan_keys"], measured["missing_parents"]) == (1, 2)
    assert measured["empty_rows"] == 0
    assert record["asks"] == ["orphan_keys", "missing_parents"]
    [finding] = result.findings
    assert (finding.code, finding.severity) == ("population_delta", "high")
    # A drafted parent is never a declared one, and never suppressed.
    assert result.parent_coverage == []
    assert "parent_population" in result.suppressed
    assert "no model in scope declares" in result.suppressed["parent_population"]
    assert "population_delta" not in result.suppressed


def test_every_key_an_orphan_says_the_join_is_suspect(tmp_path: Path):
    _prepared, result = _run(
        tmp_path,
        "CREATE TABLE m AS SELECT * FROM (VALUES (7, 1), (8, 1)) t(pid, n)",
        _rule(),
    )
    [finding] = result.findings
    assert "the join or this parent is suspect" in finding.detail
    assert "no reason to drive the model from the child table" in finding.detail


def test_parents_kept_with_no_children_ask_above_the_threshold(tmp_path: Path):
    prepared, result = _run(
        tmp_path,
        "CREATE TABLE m AS SELECT * FROM "
        "(VALUES (1, 2, 'a'), (2, 0, NULL), (3, NULL, NULL), (4, 1, 'b')) "
        "t(pid, n, last_label)",
        _rule(empty_columns=("n", "last_label")),
    )
    record = _only(result)
    assert record["status"] == "needs_decision"
    assert record["measured"]["empty_rows"] == 2
    assert record["measured"]["empty_share"] == 0.5
    assert record["asks"] == ["empty_rows"]
    [finding] = result.findings
    assert finding.severity == "low"
    # A number reads as empty at zero, text only at NULL.
    [sql] = prepared.statements()
    assert '("n" IS NULL OR "n" = 0)' in sql
    assert '"last_label" IS NULL' in sql
    assert '"last_label" = 0' not in sql


def test_a_population_that_matches_its_parent_is_satisfied(tmp_path: Path):
    _prepared, result = _run(
        tmp_path,
        "CREATE TABLE m AS SELECT * FROM "
        "(VALUES (1, 2), (2, 1), (3, 1), (4, 5)) t(pid, n)",
        _rule(empty_columns=("n",)),
    )
    assert _only(result)["status"] == "satisfied"
    assert result.findings == []


def test_an_assumption_at_a_settling_tier_settles_without_a_finding(tmp_path: Path):
    settled = (("chosen", "only conversations with parts"), ("evidence", "request"))
    _prepared, result = _run(
        tmp_path,
        "CREATE TABLE m AS SELECT * FROM (VALUES (1, 2), (2, 1)) t(pid, n)",
        _rule(settled_by=settled),
    )
    record = _only(result)
    assert record["status"] == "settled"
    assert record["asks"] == ["missing_parents"]
    assert record["settled_by"] == dict(settled)
    assert result.findings == []


def test_an_empty_parent_and_output_have_nothing_to_compare(tmp_path: Path):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    _manifest(tmp_path)
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE parents (pid INTEGER)")
    conn.execute("CREATE TABLE m (pid INTEGER, n INTEGER)")
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, _ = facts_mod.plan(tmp_path, drafted={"m": _rule()})
        live = [meta.identifier for meta in adapter.list_objects()]
        result = facts_mod.findings(adapter, facts_mod.prepare(adapter, models, live))
    finally:
        adapter.close()
    record = _only(result)
    assert (record["status"], record["reason_code"]) == (
        "not_applicable",
        "empty_input",
    )


def test_a_parent_that_was_not_built_is_unchecked_on_its_own_record(tmp_path: Path):
    _prepared, result = _run(
        tmp_path,
        "CREATE TABLE m AS SELECT * FROM (VALUES (1, 2)) t(pid, n)",
        facts_mod.ParentRule(
            model="nowhere", relation=None, key=(("pid", "pid"),), drafted=True
        ),
    )
    record = _only(result)
    assert (record["status"], record["reason_code"]) == (
        "unchecked",
        "no_such_relation",
    )
    assert "population_delta" not in result.suppressed
    assert "parent_population" not in result.suppressed or (
        "no model in scope declares" in result.suppressed["parent_population"]
    )


def test_a_declared_parent_always_wins_over_a_draft(tmp_path: Path):
    _manifest(tmp_path)
    manifest = json.loads((tmp_path / "target" / "manifest.json").read_text())
    manifest["nodes"]["model.p.m"]["config"]["meta"]["dex"] = {
        "population": {"parent": {"model": "parents", "key": {"pid": "pid"}}}
    }
    (tmp_path / "target" / "manifest.json").write_text(json.dumps(manifest))
    models, _ = facts_mod.plan(tmp_path, drafted={"m": _rule(empty_columns=("n",))})
    [model] = [m for m in models if m.model == "m"]
    assert model.parent is not None and model.parent.drafted is False
    assert model.declares("parent_population")
