"""`maintain coverage`'s strength section and the mutation record behind it
(#1281, #1284).

The hand-built manifests check how a recorded score is called against the
project; the DuckDB round trip at the bottom runs `transform test --mutate`,
reads the record back through `maintain coverage`, edits the model and sees the
record go stale.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exmergo_dex_core import dbt_tests
from exmergo_dex_core.maintain import coverage as coverage_mod
from exmergo_dex_core.storage import MemoryStore

PROJECT = "shop"
FCT = f"model.{PROJECT}.fct"
MART = f"model.{PROJECT}.mart"
BARE = f"model.{PROJECT}.bare"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _manifest() -> dict:
    def model(name, raw, reads=()):
        return {
            "resource_type": "model",
            "name": name,
            "package_name": PROJECT,
            "original_file_path": f"models/{name}.sql",
            "raw_code": raw,
            "language": "sql",
            "config": {"materialized": "table", "enabled": True},
            "columns": {"id": {"name": "id"}},
            "depends_on": {"nodes": list(reads)},
        }

    return {
        "metadata": {"project_name": PROJECT},
        "nodes": {
            FCT: model("fct", "select id from raw"),
            MART: model("mart", "select id from {{ ref('fct') }}", [FCT]),
            BARE: model("bare", "select 1 as id"),
            "test.shop.not_null_fct_id": {
                "resource_type": "test",
                "name": "not_null_fct_id",
                "package_name": PROJECT,
                "attached_node": FCT,
                "column_name": "id",
                "test_metadata": {"name": "not_null", "kwargs": {"column_name": "id"}},
                "config": {"severity": "ERROR", "enabled": True},
                "depends_on": {"nodes": [FCT]},
            },
            "test.shop.unique_mart_id": {
                "resource_type": "test",
                "name": "unique_mart_id",
                "package_name": PROJECT,
                "attached_node": MART,
                "column_name": "id",
                "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}},
                "config": {"severity": "ERROR", "enabled": True},
                "depends_on": {"nodes": [MART]},
            },
        },
        "sources": {},
        "unit_tests": {},
        "disabled": {},
    }


def _record(manifest: dict, model_id: str = FCT, **extra) -> dict:
    node = {**manifest["nodes"][model_id], "unique_id": model_id}
    record = {
        "model_id": model_id,
        "model": model_id.rsplit(".", 1)[-1],
        "score": 0.8,
        "score_with_downstream": None,
        "incremental_score": None,
        "survivors": 1,
        "complete": True,
        "not_run": 0,
        "downstream": False,
        "recorded_at": "2026-11-15T11:00:00+00:00",
        "sql_digest": dbt_tests.model_sql_digest(node),
        "tests_digest": dbt_tests.own_tests_digest(manifest, node),
        "downstream_tests": {},
    }
    record.update(extra)
    return record


def _strength(manifest: dict, records: dict) -> dict[str, dict]:
    found = coverage_mod.inventory(manifest, records=records, now=NOW, full=True)
    return {r["name"]: r.get("strength") for r in found.sections["models"]}


def test_a_matching_record_is_fresh_and_carries_its_age():
    manifest = _manifest()
    strength = _strength(manifest, {FCT: _record(manifest)})
    assert strength["fct"]["status"] == "fresh"
    assert strength["fct"]["score"] == 0.8
    assert strength["fct"]["age_seconds"] == 3600
    assert "changed" not in strength["fct"]


def test_a_model_sql_edit_makes_the_record_stale():
    manifest = _manifest()
    record = _record(manifest)
    manifest["nodes"][FCT]["raw_code"] = "select id from raw where id > 0"
    strength = _strength(manifest, {FCT: record})
    assert strength["fct"]["status"] == "stale"
    assert strength["fct"]["changed"] == ["model_sql"]
    # A stale score is still shown, never as current.
    assert strength["fct"]["score"] == 0.8


def test_a_test_change_makes_the_record_stale():
    manifest = _manifest()
    record = _record(manifest)
    test = manifest["nodes"]["test.shop.not_null_fct_id"]
    test["config"]["where"] = "id > 10"
    assert _strength(manifest, {FCT: record})["fct"]["changed"] == ["own_tests"]

    # A test added to the model is a change too.
    manifest = _manifest()
    record = _record(manifest)
    added = copy.deepcopy(manifest["nodes"]["test.shop.not_null_fct_id"])
    added["test_metadata"]["name"] = "unique"
    manifest["nodes"]["test.shop.unique_fct_id"] = added
    assert _strength(manifest, {FCT: record})["fct"]["changed"] == ["own_tests"]

    # And a disabled one.
    manifest = _manifest()
    record = _record(manifest)
    manifest["nodes"]["test.shop.not_null_fct_id"]["config"]["enabled"] = False
    assert _strength(manifest, {FCT: record})["fct"]["changed"] == ["own_tests"]


def test_a_downstream_test_that_ran_and_changed_makes_it_stale():
    manifest = _manifest()
    downstream = manifest["nodes"]["test.shop.unique_mart_id"]
    record = _record(
        manifest,
        downstream=True,
        score_with_downstream=1.0,
        downstream_tests={
            "test.shop.unique_mart_id": dbt_tests.test_digest(downstream)
        },
    )
    assert _strength(manifest, {FCT: record})["fct"]["status"] == "fresh"
    del manifest["nodes"]["test.shop.unique_mart_id"]
    assert _strength(manifest, {FCT: record})["fct"]["changed"] == ["downstream_tests"]


def test_missing_untestable_and_partial_records():
    manifest = _manifest()
    record = _record(manifest, complete=False, not_run=3)
    strength = _strength(manifest, {FCT: record})
    assert strength["fct"]["complete"] is False
    assert strength["fct"]["not_run"] == 3
    # mart has its own test and no record; bare has no test at all.
    assert strength["mart"] == {"status": "missing"}
    assert strength["bare"] == {"status": "untestable"}


def test_the_summary_counts_statuses_and_bands_never_averages():
    manifest = _manifest()
    found = coverage_mod.inventory(
        manifest, records={FCT: _record(manifest, score=0.6)}, now=NOW
    )
    strength = found.summary["models"]["strength"]
    assert strength["by_status"] == {
        "fresh": 1,
        "stale": 0,
        "missing": 1,
        "untestable": 1,
    }
    assert strength["fresh"] == {
        "numerator": 1,
        "denominator": 3,
        "of": "enabled project models",
    }
    assert strength["fresh_score_bands"]["0.5_to_0.75"] == 1
    assert "score" not in strength


def test_no_store_records_means_no_strength_at_all():
    found = coverage_mod.inventory(_manifest(), records=None)
    assert "strength" not in found.summary["models"]
    assert all("strength" not in r for r in found.sections["models"])


def test_the_record_digests_agree_between_parse_and_compile_fields():
    """A compiled manifest adds compiled_code; the digests read neither it nor
    anything else a parse does not carry."""

    manifest = _manifest()
    record = _record(manifest)
    compiled = copy.deepcopy(manifest)
    compiled["nodes"][FCT]["compiled_code"] = "select id from main.raw"
    compiled["nodes"]["test.shop.not_null_fct_id"]["compiled_code"] = "select 1"
    assert _strength(compiled, {FCT: record})["fct"]["status"] == "fresh"


def test_memory_store_round_trip_through_coverage():
    store = MemoryStore()
    manifest = _manifest()
    store.save_mutation_record(_record(manifest))
    assert _strength(manifest, store.load_mutation_records())["fct"]["status"] == (
        "fresh"
    )


# --- the round trip on real dbt and DuckDB ----------------------------------


def test_mutate_records_and_coverage_reads_it_fresh_then_stale(
    tmp_path: Path, duckdb_file: Path
):
    pytest.importorskip("dbt.adapters.duckdb")
    from tests.transform.mutation_helpers import (
        MODEL_SQL,
        _engine,
        _schema_yml,
    )
    from tests.transform.mutation_helpers import (
        measurable_project as _fixture,
    )

    project = _fixture.__wrapped__(tmp_path, duckdb_file)
    (project / "models" / "schema.yml").write_text(_schema_yml(True), "utf-8")
    engine = _engine(project)
    engine.build(target="dev")

    before = engine.coverage(["fct"])
    assert before.sections["models"][0]["strength"] == {"status": "missing"}

    result = engine.test_mutations("fct")
    assert result.record, "a run that decided mutants is recorded"
    assert Path(result.record).is_file()
    stored = engine.store.load_mutation_records()
    assert list(stored) == ["model.p.fct"]
    assert stored["model.p.fct"]["score"] == result.score
    # Counts and digests only: no SQL reaches the record.
    assert "select" not in str(stored).lower()

    fresh = engine.coverage(["fct"]).sections["models"][0]["strength"]
    assert fresh["status"] == "fresh", fresh.get("changed")
    assert fresh["score"] == result.score

    (project / "models" / "fct.sql").write_text(
        MODEL_SQL.replace("> 100", ">= 100"), "utf-8"
    )
    stale = engine.coverage(["fct"]).sections["models"][0]["strength"]
    assert stale["status"] == "stale"
    assert stale["changed"] == ["model_sql"]


def test_a_refused_mutation_run_records_nothing(tmp_path: Path, duckdb_file: Path):
    pytest.importorskip("dbt.adapters.duckdb")
    from tests.transform.mutation_helpers import _engine
    from tests.transform.mutation_helpers import (
        measurable_project as _fixture,
    )

    project = _fixture.__wrapped__(tmp_path, duckdb_file)
    engine = _engine(project)
    with pytest.raises(Exception, match="no tests"):
        engine.test_mutations("fct")
    assert engine.store.load_mutation_records() == {}


def test_a_singular_test_on_two_models_counts_once_in_the_status_summary():
    manifest = _manifest()
    manifest["nodes"]["test.shop.both"] = {
        "resource_type": "test",
        "name": "both",
        "package_name": PROJECT,
        "attached_node": None,
        "config": {"severity": "ERROR", "enabled": True},
        "depends_on": {"nodes": [FCT, MART]},
    }
    found = coverage_mod.inventory(manifest, statuses={"test.shop.both": "fail"})
    assert found.summary["tests"]["by_last_run"]["fail"] == 1
    listed = {r["name"]: r["last_run"]["fail"] for r in found.sections["models"]}
    assert listed["fct"] == 1 and listed["mart"] == 1


def _run_result(*, full: dict, branch: dict | None, branch_tests: list[str]):
    return {
        "counts": {"killed": 0, "caught_downstream": 0, "survived": 0, "not_run": 0}
        | full,
        "incremental": None
        if branch is None
        else {
            "counts": {"killed": 0, "survived": 0, "not_run": 0} | branch,
            "tests": branch_tests,
            "score": 0.0,
        },
        "score": None,
        "score_with_downstream": None,
        "equivalence": None,
    }


def test_inferred_branch_survivors_alone_record_nothing():
    """Review fix: with the budget spent before any full-build mutant ran and
    no unit test taking the is_incremental() branch, the branch's survivors are
    by construction, so the previous record stays."""

    from types import SimpleNamespace

    from exmergo_dex_core.transform.commands import _record_mutation_run

    store = MemoryStore()
    manifest = _manifest()
    node = {**manifest["nodes"][FCT], "unique_id": FCT}
    fingerprint = {"sql_digest": "a", "tests_digest": "b", "manifest": manifest}
    cap = {"limit": 20, "generated": 4, "elided": {}}
    below = SimpleNamespace(enabled=False, tests=[])

    at, _ = _record_mutation_run(
        store,
        node,
        fingerprint,
        _run_result(full={"not_run": 2}, branch={"survived": 2}, branch_tests=[]),
        cap=cap,
        unsupported=0,
        downstream=below,
        connector="duckdb",
    )
    assert at is None and store.load_mutation_records() == {}

    at, _ = _record_mutation_run(
        store,
        node,
        fingerprint,
        _run_result(full={"not_run": 2}, branch={"killed": 1}, branch_tests=["ut"]),
        cap=cap,
        unsupported=0,
        downstream=below,
        connector="duckdb",
    )
    assert at is not None
    assert store.load_mutation_records()[FCT]["complete"] is False
