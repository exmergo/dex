"""What a plan did to the models below its edits (#1231), piece by piece.

Selection from the manifest, the pre-plan sources, the key a model is measured
by, the two statements (run on DuckDB, NULL keys and a changed key type
included), the judgment, the cost threshold, and what the completion block and
the attribution make of it. The real-dbt fixture is in
``test_downstream_impact_e2e.py``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from exmergo_dex_core.dbt_project import content_hash
from exmergo_dex_core.maintain.drift import DriftFinding
from exmergo_dex_core.transform import downstream_impact as impact
from exmergo_dex_core.transform import finding_attribution as attr
from exmergo_dex_core.transform.completion import (
    COMPLETION_BUDGET_BYTES,
    summarize_completion,
)
from exmergo_dex_core.transform.plans import (
    EditKind,
    PlanEdit,
    TransformPlan,
    _model_preimages,
)

from .test_build_completion import _evidence
from .test_finding_attribution import _nodes, _verification

# --- selection -------------------------------------------------------------------


def _node(name: str, path: str, parents=(), **extra) -> dict:
    return {
        "name": name,
        "resource_type": "model",
        "package_name": "p",
        "original_file_path": path,
        "depends_on": {"nodes": [f"model.p.{p}" for p in parents]},
        "config": extra.pop("config", {}),
        **extra,
    }


def _manifest(*nodes: dict) -> dict:
    by_id = {f"model.p.{n['name']}": n for n in nodes}
    children: dict[str, list[str]] = {uid: [] for uid in by_id}
    for uid, node in by_id.items():
        for parent in node["depends_on"]["nodes"]:
            children.setdefault(parent, []).append(uid)
    return {"metadata": {"project_name": "p"}, "nodes": by_id, "child_map": children}


def _edit(path: str, *, old: str | None = "x", kind=EditKind.MODEL_SQL, op=None):
    edit = PlanEdit(path=path, new_content="select 1", kind=kind)
    edit.old_content_hash = content_hash(old) if old is not None else None
    if op is not None:
        edit.op = op
    return edit


_CHAIN = _manifest(
    _node("stg_products", "models/stg_products.sql"),
    _node("int_products", "models/int_products.sql", ["stg_products"]),
    _node("dim_products", "models/marts/dim_products.sql", ["int_products"]),
    _node("unrelated", "models/unrelated.sql"),
    _node("py_model", "models/py_model.py", ["stg_products"], language="python"),
    _node("below_py", "models/below_py.sql", ["py_model"]),
)


def test_selection_is_the_edited_model_and_everything_below_it_nearest_first():
    found = impact.select_targets([_edit("models/stg_products.sql")], _CHAIN)
    assert [(t.label, t.role, t.distance) for t in found.targets] == [
        ("stg_products", "edited", 0),
        ("int_products", "descendant", 1),
        ("dim_products", "descendant", 2),
    ]
    assert found.targets[2].upstream == ["stg_products"]
    # A Python model cannot be inlined, so it and what is below it are named.
    assert [(c["model"], c["reason_code"]) for c in found.cut] == [
        ("py_model", "not_inlinable")
    ]
    assert found.inline == {
        "models/stg_products.sql",
        "models/int_products.sql",
        "models/marts/dim_products.sql",
    }


def test_the_cap_keeps_the_nearest_models_and_names_the_rest():
    found = impact.select_targets([_edit("models/stg_products.sql")], _CHAIN, cap=2)
    assert [t.label for t in found.targets] == ["stg_products", "int_products"]
    assert ("dim_products", "cap") in {
        (c["model"], c["reason_code"]) for c in found.cut
    }
    # What is cut is not inlined: only paths to measured models are.
    assert "models/marts/dim_products.sql" not in found.inline


def test_another_edited_model_below_is_measured_as_edited_not_as_a_descendant():
    found = impact.select_targets(
        [_edit("models/stg_products.sql"), _edit("models/int_products.sql")], _CHAIN
    )
    roles = {t.label: t.role for t in found.targets}
    assert roles == {
        "stg_products": "edited",
        "int_products": "edited",
        "dim_products": "descendant",
    }
    dim = next(t for t in found.targets if t.label == "dim_products")
    assert dim.upstream == ["int_products", "stg_products"]


@pytest.mark.parametrize(
    ("kind", "path", "code"),
    [
        (EditKind.MACRO_SQL, "macros/m.sql", "global_edit"),
        (EditKind.PROJECT_YML, "dbt_project.yml", "global_edit"),
        (EditKind.SEED_CSV, "seeds/s.csv", "inputs_unsynchronized"),
    ],
)
def test_edits_the_baseline_cannot_isolate_measure_nothing(kind, path, code):
    found = impact.select_targets(
        [_edit("models/stg_products.sql"), _edit(path, kind=kind)], _CHAIN
    )
    assert found.reason_code == code
    assert not found.targets


def test_a_plan_that_only_creates_models_has_nothing_to_compare():
    found = impact.select_targets([_edit("models/new.sql", old=None)], _CHAIN)
    assert found.reason_code == "no_rewritten_model"


# --- the pre-plan sources ----------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True)  # noqa: S603, S607


def test_recorded_sources_win_and_git_head_fills_in_when_its_hash_matches(tmp_path):
    repo = tmp_path
    _git(repo, "init", "-q")
    (repo / "analytics" / "models").mkdir(parents=True)
    old = "select 1 as id\n"
    (repo / "analytics" / "models" / "a.sql").write_text(old, encoding="utf-8")
    (repo / "analytics" / "models" / "b.sql").write_text("select 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-qm",
        "i",
    )
    edits = [_edit("models/a.sql", old=old), _edit("models/b.sql", old="different")]
    found, missing, origin = impact.baseline_sources(edits, None, repo, "analytics")
    assert found == {"models/a.sql": old}
    # HEAD holds b.sql, but not the preimage the plan recorded.
    assert missing == ["models/b.sql"]
    assert origin == "git"

    found, missing, origin = impact.baseline_sources(
        edits, {"models/b.sql": "different"}, repo, "analytics"
    )
    assert found["models/b.sql"] == "different"
    assert not missing
    assert origin == "mixed"


def test_apply_records_each_replaced_model_outside_the_digest(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text("select 1\n", encoding="utf-8")
    plan = TransformPlan(
        plan_id="p1",
        created_at="2026-01-01T00:00:00+00:00",
        intent="i",
        project_dir=".",
        edits=[
            _edit("models/a.sql", old="select 1\n"),
            _edit("models/new.sql", old=None),
            _edit("models/schema.yml", kind=EditKind.SCHEMA_YML),
        ],
    )
    assert _model_preimages(plan, tmp_path) == {"models/a.sql": "select 1\n"}

    from exmergo_dex_core.transform.portable import plan_digest, plan_document

    before = plan_digest(plan_document(plan))
    plan.baseline_sources = {"models/a.sql": "select 1\n"}
    assert plan_digest(plan_document(plan)) == before


# --- the key ----------------------------------------------------------------------


def test_the_key_is_the_declared_grain_then_a_unique_test_then_the_sql():
    declared = {"config": {"meta": {"dex": {"grain": ["a", "b"]}}}}
    assert impact.model_key("model.p.m", declared, {}, "select 1", "duckdb") == (
        ["a", "b"],
        "declaration",
    )
    tests = {
        "nodes": {
            "test.p.unique_m_id": {
                "resource_type": "test",
                "attached_node": "model.p.m",
                "column_name": "id",
                "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}},
            }
        }
    }
    assert impact.model_key("model.p.m", {}, tests, "select 1", "duckdb") == (
        ["id"],
        "unique_test",
    )
    grouped = "select s.product_id as pid, max(x) as x from s group by s.product_id"
    assert impact.model_key("model.p.m", {}, {}, grouped, "duckdb") == (
        ["pid"],
        "drafted",
    )
    assert impact.model_key(
        "model.p.m", {}, {}, "select distinct a, b from t", "duckdb"
    ) == (["a", "b"], "drafted")
    # A group by an expression proves nothing this can name.
    assert impact.model_key(
        "model.p.m",
        {},
        {},
        "select lower(a) as a, count(*) as n from t group by lower(a)",
        "duckdb",
    ) == ([], None)


# --- the statements -----------------------------------------------------------------


def _counts(baseline: str, proposed: str, key: list[str]) -> list:
    sql = impact.counts_sql(baseline, proposed, key, "duckdb")
    return list(duckdb.sql(sql).fetchall()[0])


def test_counts_match_keys_across_a_type_change_and_group_null_keys():
    baseline = "select * from (values ('1'), ('2'), ('2'), ('abc'), (null)) as t(id)"
    proposed = "select * from (values (1), (2), (3), (null)) as t(id)"
    rows_l, rows_r, keys_l, keys_r, kept, removed, added, dup_l, dup_r = _counts(
        baseline, proposed, ["id"]
    )
    assert (rows_l, rows_r) == (5, 4)
    assert (keys_l, keys_r) == (4, 4)  # NULL is one key on each side
    assert (kept, removed, added) == (3, 1, 1)  # '1','2',NULL kept; 'abc' out; 3 in
    assert (dup_l, dup_r) == (1, 0)


def test_with_no_key_only_rows_are_counted():
    assert _counts("select 1 as a union all select 2", "select 1 as a", []) == [2, 1]


def test_columns_count_the_kept_keys_each_column_changed_on():
    baseline = (
        "select * from (values (1, 10, 'a'), (2, 20, 'b'), (3, 30, null)) t(id, n, s)"
    )
    proposed = (
        "select * from (values (1, 10, 'a'), (2, 21, 'b'), (3, 30, 'c')) t(id, n, s)"
    )
    sql = impact.columns_sql(
        baseline, proposed, [("id", "id")], [("n", "n"), ("s", "s")], "duckdb"
    )
    assert list(duckdb.sql(sql).fetchall()[0]) == [3, 1, 1]


@pytest.mark.parametrize("dialect", ["bigquery", "snowflake", "postgres", "databricks"])
def test_the_statements_parse_in_each_dialect(dialect):
    import sqlglot

    counts = impact.counts_sql("select 1 as id", "select 1 as id", ["id"], dialect)
    columns = impact.columns_sql(
        "select 1 as id, 2 as n",
        "select 1 as id, 2 as n",
        [("id", "id")],
        [("n", "n")],
        dialect,
    )
    for sql in (counts, columns):
        assert len(sqlglot.parse(sql, dialect=dialect)) == 1


# --- judging -----------------------------------------------------------------------


def _compiled(role: str = "descendant", settled=None) -> impact.Compiled:
    return impact.Compiled(
        target=impact.Target(
            unique_id="model.p.dim",
            label="dim_products",
            path="models/marts/dim_products.sql",
            role=role,
            distance=1,
            upstream=["stg_products"],
        ),
        baseline_sql="b",
        proposed_sql="p",
        key=["product_id"],
        key_source="drafted",
        settled_by=settled,
    )


def _record(removed=0, added=0, before=45, after=40, role="descendant") -> dict:
    return {
        "model": "dim_products",
        "role": role,
        "upstream": ["stg_products"],
        "key": ["product_id"],
        "key_source": "drafted",
        "rows": {"baseline": before, "proposed": after, "delta": after - before},
        "keys": {"removed": removed, "added": added, "kept": 40},
        "population": "moved",
    }


def test_a_descendant_that_lost_keys_is_a_high_finding_naming_the_edit():
    finding = impact.judge(_record(removed=5), _compiled())
    assert finding is not None
    assert (finding.axis, finding.code, finding.severity) == (
        "downstream_impact",
        "downstream_population_changed",
        "high",
    )
    assert finding.detail.startswith("dim_products: 5 key(s) removed on product_id")
    assert "caused by stg_products" in finding.detail
    assert finding.data["upstream"] == ["stg_products"]


def test_keys_only_added_are_medium_and_the_edited_model_is_not_judged():
    gained = impact.judge(_record(added=3, before=40, after=43), _compiled())
    assert gained is not None and gained.severity == "medium"
    edited = _record(removed=5, role="edited")
    assert impact.judge(edited, _compiled(role="edited")) is None


def test_a_settled_or_kept_population_is_no_finding():
    settled = {**_record(removed=5), "settled_by": {"column": "product_id"}}
    assert impact.judge(settled, _compiled()) is None
    kept = {**_record(), "population": "kept"}
    assert impact.judge(kept, _compiled()) is None


def test_a_settlement_needs_a_key_column_at_request_or_contract_tier():
    def node(evidence, column="product_id"):
        return {
            "config": {
                "meta": {
                    "dex": {"assumptions": [{"evidence": evidence, "column": column}]}
                }
            }
        }

    assert impact._settlement(node("request"), ["product_id"]) == {
        "column": "product_id",
        "evidence": "request",
    }
    assert impact._settlement(node("contract"), ["product_id"]) is not None
    assert impact._settlement(node("default"), ["product_id"]) is None
    assert impact._settlement(node("request", "other"), ["product_id"]) is None


def test_records_sort_by_size_measured_first():
    small = {"model": "a", "status": "measured", "size": 2_000_000}
    big = {"model": "b", "status": "measured", "size": 5_000_000}
    unchecked = {"model": "c", "status": "unchecked"}
    unchanged = {"model": "d", "status": "measured", "size": 0}
    assert [
        r["model"] for r in impact.sort_records([unchecked, small, unchanged, big])
    ] == [
        "b",
        "a",
        "d",
        "c",
    ]


# --- cost --------------------------------------------------------------------------


@dataclass
class _Estimating:
    dialect: str = "bigquery"
    each: float = 10.0

    def query_estimate(self, sql: str) -> float:
        return self.each


def test_a_model_above_its_share_of_the_ceiling_is_unchecked_cost():
    prepared = impact.Prepared(
        selection=impact.Selection(), compiled=[_compiled()], marts=["models/marts/"]
    )
    # A mart reads both sides twice: 2 x 10 = 20, above 25% of 40.
    total = impact.price(prepared, _Estimating(), ceiling=40)
    assert total == 0
    assert not prepared.compiled
    assert prepared.unchecked[0]["reason_code"] == "cost"

    prepared = impact.Prepared(
        selection=impact.Selection(), compiled=[_compiled()], marts=["models/marts/"]
    )
    assert impact.price(prepared, _Estimating(), ceiling=1000) == 20
    assert prepared.estimates == {"dim_products": 20}


def test_a_phase_that_does_not_fit_is_offered_and_its_models_unchecked():
    prepared = impact.Prepared(
        selection=impact.Selection(), compiled=[_compiled()], marts=[]
    )
    offer = SimpleNamespace(phase="downstream_impact")
    asked = []

    def handshake(estimate, count):
        asked.append((estimate, count))
        return offer

    result = impact.run(
        prepared,
        _Estimating(),
        timeout_seconds=1,
        handshake=handshake,
        ceiling=None,
        billed=True,
    )
    assert asked == [(10.0, 1)]
    assert result.offer is offer
    assert [r["reason_code"] for r in result.records] == ["cost"]
    assert result.payload()["unchecked"] == 1


def test_nothing_measurable_says_why():
    prepared = impact.Prepared(
        selection=impact.Selection(),
        reason_code="no_baseline_source",
        reason="no pre-plan SQL",
    )
    payload = impact.unmeasured(prepared).payload()
    assert payload["ran"] is False
    assert payload["reason_code"] == "no_baseline_source"
    assert payload["models"] == []


# --- the completion block and attribution --------------------------------------------


def _impact_verification(records: list[dict]) -> dict:
    verification = _verification([], [])
    measured = [r for r in records if r.get("status") == "measured"]
    verification["impact"] = {
        "ran": True,
        "measured": len(measured),
        "changed": sum(1 for r in measured if r.get("change") == "changed"),
        "unchecked": len(records) - len(measured),
        "models": records,
    }
    return verification


def _measured(name: str, *, finding: bool, removed: int = 5, **extra) -> dict:
    return {
        "model": name,
        "role": "descendant",
        "status": "measured",
        "change": "changed",
        "population": "moved",
        "upstream": ["stg_products"],
        "rows": {"baseline": 45, "proposed": 45 - removed, "delta": -removed},
        "keys": {"removed": removed, "added": 0, "kept": 40},
        **({"finding": "downstream_population_changed"} if finding else {}),
        **extra,
    }


def test_an_unasked_move_below_the_edits_keeps_the_build_from_validating():
    verification = _impact_verification(
        [
            _measured("dim_products", finding=True),
            {
                "model": "fct",
                "role": "descendant",
                "status": "unchecked",
                "reason_code": "cost",
            },
        ]
    )
    completion = summarize_completion(
        _evidence(_nodes("stg_products", "dim_products"), required=["stg_products"]),
        verification,
    )
    assert completion.outcome == "built_unverified"
    assert [(u.model, u.reason, u.detail) for u in completion.unverified] == [
        ("dim_products", "downstream_population_changed", "45 -> 40")
    ]
    summary = completion.impact
    assert (summary.measured, summary.changed, summary.unchecked) == (1, 1, 1)
    assert summary.top[0].model == "dim_products"
    assert summary.top[0].keys_removed == 5
    assert summary.unchecked_reasons == {"cost": 1}


def test_a_settled_move_is_listed_and_does_not_hold_the_outcome():
    verification = _impact_verification(
        [_measured("dim_products", finding=False, settled_by={"column": "product_id"})]
    )
    completion = summarize_completion(
        _evidence(_nodes("stg_products", "dim_products"), required=["stg_products"]),
        verification,
    )
    assert completion.outcome == "validated"
    assert completion.impact.top[0].settled is True


def test_the_largest_move_survives_the_byte_budget():
    records = [
        _measured(
            f"model_with_a_rather_long_name_{i:02d}", finding=True, removed=40 - i
        )
        for i in range(30)
    ]
    completion = summarize_completion(
        _evidence(_nodes("stg_products"), required=["stg_products"]),
        _impact_verification(records),
        budget=COMPLETION_BUDGET_BYTES,
    )
    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert completion.impact.top[0].model == "model_with_a_rather_long_name_00"
    assert completion.elided.get("impact.top", 0) >= 25
    assert completion.impact.changed == 30


def test_an_impact_finding_is_introduced_by_measurement():
    finding = DriftFinding(
        axis="downstream_impact",
        code="downstream_population_changed",
        identifier="dim_products",
        detail="x",
    )
    baseline = attr.Baseline(
        recorded_at="t",
        invocation_id=None,
        scope=frozenset({"dim_products"}),
        suppressed=frozenset(),
        fingerprints=frozenset({attr.fingerprint(finding)}),
    )
    judged = attr.classify(finding, baseline, None)
    assert (judged.attribution, judged.basis) == ("introduced", "measured")


# --- the plan-time pointer ----------------------------------------------------------


def test_change_scope_names_the_unedited_models_below_each_rewrite(tmp_path):
    from exmergo_dex_core.dbt_project import load
    from exmergo_dex_core.transform import change_scope

    root = tmp_path / "p"
    (root / "models").mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    files = {
        "stg.sql": "select 1 as id",
        "int.sql": "select * from {{ ref('stg') }}",
        "dim.sql": "select * from {{ ref('int') }}",
        "other.sql": "select 2 as id",
    }
    for name, sql in files.items():
        (root / "models" / name).write_text(sql, encoding="utf-8")
    report = {"models": [{"model": "stg"}]}
    found = change_scope.downstream(
        report,
        load(root),
        [PlanEdit(path="models/stg.sql", new_content="x", kind=EditKind.MODEL_SQL)],
    )
    assert found["models"] == {"stg": {"count": 2, "models": ["dim", "int"]}}
    assert found["count"] == 2
    # An edited model is the plan's own, not a model it moves untouched.
    edits = [
        PlanEdit(path="models/stg.sql", new_content="x", kind=EditKind.MODEL_SQL),
        PlanEdit(path="models/int.sql", new_content="x", kind=EditKind.MODEL_SQL),
    ]
    found = change_scope.downstream(report, load(root), edits)
    assert found["models"]["stg"]["models"] == ["dim"]
    assert (
        change_scope.downstream({"models": [{"model": "other"}]}, load(root), [])
        is None
    )


def test_git_is_not_required(tmp_path: Path):
    assert impact._git_head(tmp_path / "missing", ".", "models/a.sql") is None


def test_a_billed_build_folds_the_impact_into_its_one_estimate(monkeypatch, tmp_path):
    """The upfront pricing reaches the impact with a usable artifact pin: the
    one the sweep's own pricing uses is spent once its block exits, and
    re-entering it once failed the whole estimate (found dogfooding)."""

    import importlib

    build_mod = importlib.import_module("exmergo_dex_core.transform.build")
    commands = importlib.import_module("exmergo_dex_core.transform.commands")
    verify_mod = importlib.import_module("exmergo_dex_core.transform.verify")

    monkeypatch.setattr(build_mod, "needs_deps", lambda project: False)
    monkeypatch.setattr(build_mod, "_build_env", lambda *a, **k: {})
    monkeypatch.setattr(
        build_mod, "compile_estimate", lambda *a, **k: (100.0, {"m": 100.0}, [])
    )
    monkeypatch.setattr(build_mod, "compiled_model_ids", lambda *a, **k: set())
    for name in ("price_verification", "price_declared_facts", "price_join_contract"):
        monkeypatch.setattr(verify_mod, name, lambda *a, **k: (0.0, []))
    seen = []

    def price_impact(engine, adapter, project, given, manifest_path):
        seen.append(manifest_path)
        return 42.0, []

    monkeypatch.setattr(verify_mod, "price_impact", price_impact)
    adapter = SimpleNamespace(paradigm=None, dialect="bigquery")
    engine = SimpleNamespace(
        connector="bigquery",
        budget=None,
        config=SimpleNamespace(
            connector="bigquery",
            clickhouse=None,
            budget=SimpleNamespace(ceiling=1000.0),
            guards=SimpleNamespace(approved_functions=[]),
        ),
        _adapter=lambda *a, **k: adapter,
    )
    estimate, per_node, notes, _bounds, _adapter = commands._price_build(
        engine,
        tmp_path,
        "dev",
        None,
        verify=True,
        artifact_directory=tmp_path,
        impact=object(),
    )
    assert notes == []
    assert estimate == 142.0
    assert per_node["(downstream impact)"] == 42.0
    assert seen == [tmp_path / "manifest.json"]
