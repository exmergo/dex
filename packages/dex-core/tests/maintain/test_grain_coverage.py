"""`maintain verify`'s ``grain_coverage`` (#1223): one record per declared key.

A ``grain_broken`` finding says a key failed; nothing used to say a key held.
A caller wanting a verdict per declared key had to read "passed" out of that
silence, and read the reasons a key went unmeasured out of prose notes. These
tests pin the record a caller reads instead: every declared key appears, with
``proven``, ``broken``, ``not_applicable``, ``unchecked`` or ``not_planned``,
and a reason code wherever it was not judged.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from exmergo_dex_core.errors import WarehouseQueryError
from exmergo_dex_core.maintain import verify as verify_mod
from exmergo_dex_core.maintain.results import VerifyResult
from exmergo_dex_core.project_definitions import (
    DeclaredCompositeKey,
    DeclaredKey,
    ProjectDefinitions,
)

Candidate = verify_mod.GrainCandidate


class _Adapter:
    """The grain-check surface only: metadata, exact single-column counts and
    exact combination counts."""

    def __init__(
        self,
        *,
        rows: dict[str, int | None],
        columns: dict[str, list[str]],
        exact: dict[str, dict[str, int]] | None = None,
        combos: dict[str, dict[tuple[str, ...], int]] | None = None,
    ):
        self._rows = rows
        self._columns = columns
        self._exact = exact or {}
        self._combos = combos or {}

    def table_metadata(self, identifier: str):
        meta = types.SimpleNamespace(
            identifier=identifier, row_count=self._rows[identifier]
        )
        return meta, [types.SimpleNamespace(name=n) for n in self._columns[identifier]]

    def exact_distinct_counts(self, identifier: str, columns: list[str]):
        known = self._exact.get(identifier, {})
        return {c: known[c] for c in columns if c in known}

    def distinct_combination_counts(self, identifier: str, combinations):
        known = self._combos.get(identifier, {})
        return {tuple(c): known[tuple(c)] for c in combinations if tuple(c) in known}


def _records(adapter, declared, relations, live, models=None):
    records: list[dict] = []
    findings, notes = verify_mod.grain_findings(
        adapter,
        declared,
        relations,
        live,
        set(models if models is not None else relations),
        records=records,
    )
    return findings, notes, records


def _verdicts(records: list[dict]) -> list[tuple]:
    return [
        (r["model"], r["columns"], r["source"], r["status"], r["reason"])
        for r in records
    ]


# --- the grain axis, per candidate ---------------------------------------------


def test_every_unique_column_on_a_model_gets_its_own_record():
    """`proven` used to keep only the first unique candidate per model; the
    records keep every one, so a second key that held is stated too."""

    adapter = _Adapter(
        rows={"db.main.a": 10},
        columns={"db.main.a": ["id", "email", "code"]},
        exact={"db.main.a": {"id": 10, "email": 10, "code": 7}},
    )
    findings, _, records = _records(
        adapter,
        {
            "a": [
                Candidate(columns=["id"], source="unique_test"),
                Candidate(columns=["email"], source="unique_test"),
                Candidate(columns=["code"], source="unique_test"),
            ]
        },
        {"a": "db.main.a"},
        ["db.main.a"],
    )
    assert _verdicts(records) == [
        ("a", ["id"], "unique_test", "proven", None),
        ("a", ["email"], "unique_test", "proven", None),
        ("a", ["code"], "unique_test", "broken", None),
    ]
    assert records[0]["measured"] == {"rows": 10, "distinct": 10}
    assert records[2]["measured"] == {"rows": 10, "distinct": 7}
    assert [f.data["columns"] for f in findings] == [["code"]]


def test_a_composite_record_carries_every_column():
    adapter = _Adapter(
        rows={"db.main.a": 10},
        columns={"db.main.a": ["day", "page"]},
        combos={"db.main.a": {("day", "page"): 10}},
    )
    _, _, records = _records(
        adapter,
        {"a": [Candidate(columns=["day", "page"], source="unique_test_composite")]},
        {"a": "db.main.a"},
        ["db.main.a"],
    )
    assert _verdicts(records) == [
        ("a", ["day", "page"], "unique_test_composite", "proven", None)
    ]


def test_an_empty_relation_is_not_applicable_not_proven():
    adapter = _Adapter(
        rows={"db.main.a": 0},
        columns={"db.main.a": ["id"]},
        exact={"db.main.a": {"id": 0}},
    )
    _, _, records = _records(
        adapter,
        {"a": [Candidate(columns=["id"], source="unique_test")]},
        {"a": "db.main.a"},
        ["db.main.a"],
    )
    assert _verdicts(records) == [
        ("a", ["id"], "unique_test", "not_applicable", "empty_relation")
    ]


def test_each_note_reason_has_a_code_on_the_record():
    """The six reasons that reached a caller only as prose each have a code."""

    class _Refusing(_Adapter):
        def exact_distinct_counts(self, identifier, columns):
            if identifier == "db.main.refused":
                raise WarehouseQueryError("relation is locked: secret-value-42")
            return super().exact_distinct_counts(identifier, columns)

    adapter = _Refusing(
        rows={
            "db.main.declined": 10,
            "db.main.uncounted": None,
            "db.main.missing": 10,
            "db.main.refused": 10,
            "db.one.twin": 10,
            "db.two.twin": 10,
        },
        columns={
            "db.main.declined": ["id"],
            "db.main.uncounted": ["id"],
            "db.main.missing": ["id"],
            "db.main.refused": ["id"],
            "db.one.twin": ["id"],
            "db.two.twin": ["id"],
        },
        exact={"db.main.uncounted": {"id": 10}},
    )
    unique = [Candidate(columns=["id"], source="unique_test")]
    _, notes, records = _records(
        adapter,
        {
            "declined": unique,
            "uncounted": unique,
            "missing": [Candidate(columns=["idd"], source="unique_test")],
            "refused": unique,
            "twin": unique,
            "gone": unique,
            "unbuilt": unique,
        },
        {
            "declined": "db.main.declined",
            "uncounted": "db.main.uncounted",
            "missing": "db.main.missing",
            "refused": "db.main.refused",
            "twin": "twin",
            "gone": "db.main.gone",
        },
        [
            "db.main.declined",
            "db.main.uncounted",
            "db.main.missing",
            "db.main.refused",
            "db.one.twin",
            "db.two.twin",
        ],
    )
    by_model = {r["model"]: (r["status"], r["reason"]) for r in records}
    assert by_model == {
        "declined": ("unchecked", "scan"),
        "uncounted": ("unchecked", "no_count"),
        "missing": ("unchecked", "missing_columns"),
        "refused": ("unchecked", "refused"),
        "twin": ("unchecked", "ambiguous"),
        "gone": ("unchecked", "no_relation"),
        "unbuilt": ("unchecked", "no_relation"),
    }
    # The warehouse's words stay in the notes, never on the record.
    assert any("secret-value-42" in n for n in notes)
    assert "secret-value-42" not in json.dumps(records)


def test_the_naming_guess_writes_no_record():
    """Only a declared key gets a record: a guess is not a key the project
    states."""

    adapter = _Adapter(rows={"db.main.a": 10}, columns={"db.main.a": ["status"]})
    _, _, records = _records(adapter, {}, {"a": "db.main.a"}, ["db.main.a"])
    assert records == []


# --- what the priority rule leaves out ------------------------------------------


def _defs(**fields) -> ProjectDefinitions:
    return ProjectDefinitions(present=True, **fields)


def test_grain_plan_checks_every_composite_a_model_declares():
    """A second composite on the same model was dropped by the plan, and the
    record would have had to call it `priority`, which it was not."""

    definitions = _defs(
        declared_composite_keys=[
            DeclaredCompositeKey(model="a", columns=["x", "y"], source="manifest"),
            DeclaredCompositeKey(model="a", columns=["x", "z"], source="manifest"),
        ]
    )
    assert verify_mod.grain_plan(definitions) == {
        "a": [
            Candidate(columns=["x", "y"], source="unique_test_composite"),
            Candidate(columns=["x", "z"], source="unique_test_composite"),
        ]
    }


def test_a_key_the_priority_rule_left_out_is_not_planned():
    definitions = _defs(
        declared_keys=[
            DeclaredKey(model="a", column="id", unique=True, source="manifest")
        ],
        declared_composite_keys=[
            DeclaredCompositeKey(model="a", columns=["x", "y"], source="manifest")
        ],
        primary_entities={"a": "other"},
    )
    records = verify_mod.not_planned_grain_records(
        verify_mod.declared_grain_candidates(definitions),
        verify_mod.grain_plan(definitions),
    )
    assert _verdicts(records) == [
        ("a", ["x", "y"], "unique_test_composite", "not_planned", "priority"),
        ("a", ["other"], "primary_entity", "not_planned", "priority"),
    ]


def test_a_left_out_key_on_the_same_columns_as_a_planned_one_is_judged_by_it():
    """A unique test on the column the primary entity names is one key under
    two sources; it is judged once, under the source that ranked first."""

    definitions = _defs(
        declared_grains={"a": ["ID"]},
        declared_keys=[
            DeclaredKey(model="a", column="id", unique=True, source="manifest")
        ],
        primary_entities={"a": "id"},
    )
    records = verify_mod.not_planned_grain_records(
        verify_mod.declared_grain_candidates(definitions),
        verify_mod.grain_plan(definitions),
    )
    assert records == []


def test_the_field_is_absent_when_the_grain_class_did_not_run():
    result = VerifyResult(suppressed={"grain": "warehouse unreachable"})
    assert "grain_coverage" not in result.data()
    assert VerifyResult(grain_coverage=[]).data()["grain_coverage"] == []


# --- end to end ------------------------------------------------------------------


@pytest.fixture
def _compiles(monkeypatch: pytest.MonkeyPatch):
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


def _artifacts(project_dir: Path, nodes: dict) -> None:
    target = project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    (target / "run_results.json").write_text('{"results": []}', encoding="utf-8")


def _model(name: str, *, grain: list[str] | None = None) -> dict:
    node: dict = {
        "name": name,
        "resource_type": "model",
        "relation_name": f'"warehouse"."main"."{name}"',
        "config": {"materialized": "table", "meta": {}},
        "columns": {},
    }
    if grain is not None:
        node["config"]["meta"]["dex"] = {"grain": grain}
    return node


def _unique(model: str, column: str) -> dict:
    return {
        "resource_type": "test",
        "test_metadata": {"name": "unique", "kwargs": {"column_name": column}},
        "attached_node": f"model.maintain_test.{model}",
    }


def _combination(model: str, columns: list[str]) -> dict:
    return {
        "resource_type": "test",
        "test_metadata": {
            "name": "unique_combination_of_columns",
            "kwargs": {"combination_of_columns": columns},
        },
        "attached_node": f"model.maintain_test.{model}",
    }


def test_verify_states_every_declared_key_on_a_model(maintain_repo, _compiles):
    """The issue's fixture: a model declaring single and composite keys. The
    key that held is `proven`, the one that did not is `broken` beside its
    finding, the composite the priority rule left out is `not_planned`, and
    the semantic primary entity on `order_id` is judged by the unique test on
    the same column rather than listed twice."""

    _artifacts(
        maintain_repo.project_dir,
        {
            "model.maintain_test.stg_orders": _model("stg_orders"),
            "test.maintain_test.u1": _unique("stg_orders", "order_id"),
            "test.maintain_test.u2": _unique("stg_orders", "customer_id"),
            "test.maintain_test.c1": _combination(
                "stg_orders", ["customer_id", "ordered_at"]
            ),
        },
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    coverage = payload["data"]["grain_coverage"]
    assert _verdicts(coverage) == [
        ("stg_orders", ["customer_id"], "unique_test", "broken", None),
        ("stg_orders", ["order_id"], "unique_test", "proven", None),
        (
            "stg_orders",
            ["customer_id", "ordered_at"],
            "unique_test_composite",
            "not_planned",
            "priority",
        ),
    ]
    assert coverage[0]["measured"] == {"rows": 200, "distinct": 40}
    assert coverage[1]["measured"] == {"rows": 200, "distinct": 200}
    broken = [f for f in payload["data"]["findings"] if f["code"] == "grain_broken"]
    assert [f["data"]["columns"] for f in broken] == [["customer_id"]]


def test_a_declared_grain_counted_with_the_declared_facts_has_a_record(
    maintain_repo, _compiles
):
    """A `config.meta.dex.grain` is counted in the declared-fact statement,
    not by the grain axis; its record comes from there, with the same shape."""

    maintain_repo.sql(
        "CREATE TABLE dim_customer AS SELECT id, id % 4 AS region FROM customers",
        "CREATE TABLE empty_dim AS SELECT id FROM customers WHERE false",
    )
    _artifacts(
        maintain_repo.project_dir,
        {
            "model.maintain_test.stg_orders": _model("stg_orders", grain=["order_id"]),
            "test.maintain_test.u1": _unique("stg_orders", "order_id"),
            "model.maintain_test.dim_customer": _model(
                "dim_customer", grain=["region"]
            ),
            "model.maintain_test.empty_dim": _model("empty_dim", grain=["id"]),
            "model.maintain_test.dim_missing": _model("dim_missing", grain=["nope"]),
        },
    )
    maintain_repo.sql("CREATE TABLE dim_missing AS SELECT 1 AS id")
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert _verdicts(payload["data"]["grain_coverage"]) == [
        ("dim_customer", ["region"], "declaration", "broken", None),
        ("dim_missing", ["nope"], "declaration", "unchecked", "missing_columns"),
        ("empty_dim", ["id"], "declaration", "not_applicable", "empty_relation"),
        ("stg_orders", ["order_id"], "declaration", "proven", None),
    ]


def test_a_model_with_no_relation_is_unchecked_not_silent(maintain_repo, _compiles):
    """Neither model has a relation to read: `never_built` is not in the
    warehouse, and the fixture's semantic layer declares a primary entity on
    `stg_orders`, which this manifest does not list."""

    _artifacts(
        maintain_repo.project_dir,
        {
            "model.maintain_test.never_built": _model("never_built"),
            "test.maintain_test.u1": _unique("never_built", "id"),
        },
    )
    rc, payload = maintain_repo.dex("maintain", "verify")
    assert rc == 0 and payload["status"] == "ok", payload
    assert _verdicts(payload["data"]["grain_coverage"]) == [
        ("never_built", ["id"], "unique_test", "unchecked", "no_relation"),
        ("stg_orders", ["order_id"], "primary_entity", "unchecked", "no_relation"),
    ]
    assert any(f["code"] == "no_relation" for f in payload["data"]["findings"])


def test_a_scoped_verify_reports_only_the_selected_models_keys(
    maintain_repo, _compiles
):
    _artifacts(
        maintain_repo.project_dir,
        {
            "model.maintain_test.stg_orders": _model("stg_orders"),
            "test.maintain_test.u1": _unique("stg_orders", "order_id"),
            "model.maintain_test.never_built": _model("never_built"),
            "test.maintain_test.u2": _unique("never_built", "id"),
        },
    )
    rc, payload = maintain_repo.dex("maintain", "verify", "stg_orders")
    assert rc == 0 and payload["status"] == "ok", payload
    assert {r["model"] for r in payload["data"]["grain_coverage"]} == {"stg_orders"}


def test_a_declared_grain_behind_an_offered_scan_is_unchecked(tmp_path: Path):
    """On a metered connector the declared-fact statement waits on an offer;
    its grain is `scan_offered`, never proven by default."""

    duckdb = pytest.importorskip("duckdb")
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.maintain import declared_facts as facts_mod

    _artifacts(tmp_path, {"model.p.dim": _model("dim", grain=["id"])})
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE dim AS SELECT 1 AS id")
    conn.close()
    adapter = DuckDBAdapter(path)
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(
            adapter, models, live, check_grain=True, tested_grain=True
        )
        result = facts_mod.findings(
            adapter, prepared, handshake=lambda estimate, count: object()
        )
    finally:
        adapter.close()
    assert _verdicts(result.grain_coverage) == [
        ("dim", ["id"], "declaration", "unchecked", "scan_offered")
    ]
