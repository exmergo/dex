"""``dex maintain diagnose`` (#1475): from an open incident to a cause.

Each project here is a git repository holding what a dbt build leaves behind
(a manifest naming a source, the staging model reading it and a ``unique`` test
on that model), the Elementary package's tables in a DuckDB file holding the
test's runs, and, in the same file, the source relation the loader filled.

The defect is the one ``dex demo`` already plants (#1477): a batch loaded twice,
so ``order_item_id`` is no longer unique and the test fails. Each fixture has a
twin with no defect seeded. Where a series is wanted, ``.dex/snapshot.json`` is
committed with a baseline captured before the last passing run, whose profile
proved the key unique.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.cache import ColumnProfile, Dataset
from exmergo_dex_core.cli import dispatch
from exmergo_dex_core.maintain.commit_history import git_env
from exmergo_dex_core.maintain.results import DiagnoseResult
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline
from exmergo_dex_core.results import to_envelope

from .test_incident_sources import RELEASE, SCHEMA, Result, Run, _warehouse

duckdb = pytest.importorskip("duckdb")

TEST = "test.shop.unique_stg_order_items_order_item_id.4b1a2c"
MODEL = "model.shop.stg_order_items"
SOURCE = "source.shop.raw.order_items"
RELATION = "dev.raw.order_items"


def _git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=dex",
            "-c",
            "user.email=dex@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        env=git_env(),
    )


def _source(name: str) -> dict[str, Any]:
    return {
        "unique_id": f"source.shop.raw.{name}",
        "resource_type": "source",
        "package_name": "shop",
        "source_name": "raw",
        "name": name,
        "identifier": name,
        "database": "dev",
        "schema": "raw",
    }


def _manifest(extra_sources: Sequence[str] = ()) -> dict[str, Any]:
    nodes: dict[str, Any] = {
        MODEL: {
            "unique_id": MODEL,
            "resource_type": "model",
            "package_name": "shop",
            "name": "stg_order_items",
            "database": "dev",
            "schema": "main",
            "config": {"enabled": True},
            "depends_on": {
                "nodes": [SOURCE, *(f"source.shop.raw.{s}" for s in extra_sources)]
            },
        },
        TEST: {
            "unique_id": TEST,
            "resource_type": "test",
            "package_name": "shop",
            "name": "unique_stg_order_items_order_item_id",
            "column_name": "order_item_id",
            "config": {"enabled": True, "severity": "ERROR"},
            "test_metadata": {
                "name": "unique",
                "kwargs": {"column_name": "order_item_id"},
            },
            "depends_on": {"nodes": [MODEL]},
            "attached_node": MODEL,
        },
    }
    for table in ("dbt_invocations", "elementary_test_results"):
        uid = f"model.elementary.{table}"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "model",
            "package_name": "elementary",
            "name": table,
            "database": "dev",
            "schema": SCHEMA,
            "config": {"enabled": True},
        }
    for hook in ("on-run-start", "on-run-end"):
        uid = f"operation.elementary.elementary-{hook}-0"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "operation",
            "package_name": "elementary",
        }
    return {
        "metadata": {"project_name": "shop", "adapter_type": "duckdb"},
        "nodes": nodes,
        "sources": {
            SOURCE: _source("order_items"),
            **{f"source.shop.raw.{s}": _source(s) for s in extra_sources},
        },
        "macros": {},
        "disabled": {},
    }


def _runs() -> list[Run]:
    """Four hourly runs ending in the last hour, so the incident is open."""

    now = datetime.now(UTC).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return [Run(f"inv-{n}", now - timedelta(hours=5 - n)) for n in range(1, 5)]


def _results(runs: Sequence[Run], failing: Sequence[str]) -> list[Result]:
    bad = set(failing)
    return [
        Result(
            f"r-{run.id}", run.id, test=TEST, status="fail" if run.id in bad else "pass"
        )
        for run in runs
    ]


def _project(
    tmp_path: Path,
    *,
    duplicated: bool,
    failing: Sequence[str] = ("inv-3", "inv-4"),
    snapshot_before: datetime | None = None,
    unobserved: Sequence[str] = (),
    extra_sources: Sequence[str] = (),
    refuted_before: bool = False,
) -> tuple[Path, Path, list[Run]]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: shop\nversion: '1.0'\n")
    target = root / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(_manifest(extra_sources)))
    package = root / "dbt_packages" / "elementary"
    package.mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        f"name: elementary\nversion: '{RELEASE}'\n"
    )
    _git(root, "init", "-q")

    runs = _runs()
    results = [r for r in _results(runs, failing) if r.run not in set(unobserved)]
    warehouse = _warehouse(tmp_path / "dev.duckdb", runs, results)
    con = duckdb.connect(str(warehouse))
    con.execute("CREATE SCHEMA raw")
    con.execute(
        "CREATE TABLE raw.order_items (order_item_id INTEGER, quantity INTEGER)"
    )
    con.execute("INSERT INTO raw.order_items SELECT range, 1 FROM range(1, 201)")
    if duplicated:
        # The batch loaded twice.
        con.execute("INSERT INTO raw.order_items SELECT range, 1 FROM range(151, 201)")
    for name in extra_sources:
        # Another source at another grain: the key repeats by design.
        con.execute(f"CREATE TABLE raw.{name} (order_item_id INTEGER, step INTEGER)")
        con.execute(
            f"INSERT INTO raw.{name} SELECT range % 200 + 1, range FROM range(0, 600)"  # noqa: S608
        )
    con.close()

    if snapshot_before is not None:
        snap = Snapshot(
            created_at=snapshot_before.replace(tzinfo=UTC).isoformat(),
            connector="duckdb",
            warehouse=WarehouseBaseline(
                datasets=[
                    Dataset(
                        identifier=RELATION,
                        object_type="table",
                        row_count=200,
                        profiled_at=snapshot_before.replace(tzinfo=UTC).isoformat(),
                        candidate_keys=[] if refuted_before else [["order_item_id"]],
                        columns=[
                            ColumnProfile(
                                name="order_item_id",
                                data_type="INTEGER",
                                distinct_count=150 if refuted_before else 200,
                                distinct_count_exact=True,
                                is_unique=not refuted_before,
                            ),
                            ColumnProfile(name="quantity", data_type="INTEGER"),
                        ],
                    )
                ]
            ),
        )
        dex = root / ".dex"
        dex.mkdir()
        (dex / "snapshot.json").write_text(snap.model_dump_json(indent=2))
        _git(root, "add", ".dex/snapshot.json")
        _git(root, "commit", "-q", "-m", "baseline")
    return root, warehouse, runs


def _diagnose(root: Path, warehouse: Path) -> dict[str, Any]:
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    result = engine.diagnose()
    assert isinstance(result, DiagnoseResult)
    return to_envelope(result).data


def _cause(data: dict[str, Any]) -> dict[str, Any]:
    [diagnosis] = data["diagnoses"]
    assert diagnosis["verdict"] == "cause", diagnosis
    [cause] = diagnosis["causes"]
    return cause


# --- acceptance: the defect dex demo seeds, and the same with none ----------------


def test_a_double_loaded_batch_is_named_as_the_loader_cause_with_its_evidence(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    data = _diagnose(root, warehouse)
    cause = _cause(data)

    assert cause["owner"] == "loader"
    assert cause["kind"] == "duplicate_load"
    assert cause["relation"] == RELATION
    assert cause["columns"] == ["order_item_id"]
    assert cause["check"] == "maintain check: grain axis"
    assert cause["evidence"]["distinct_count"] == 200
    assert cause["evidence"]["row_count"] == 250
    assert cause["evidence"]["duplicate_rows"] == 50
    [diagnosis] = data["diagnoses"]
    # The repair is handed to transform, never written.
    [step] = diagnosis["next_steps"]
    assert step["command"].startswith("dex transform plan ")


def test_the_same_project_with_no_defect_seeded_and_no_failure_has_nothing_to_diagnose(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=False, failing=())
    data = _diagnose(root, warehouse)

    assert data["diagnoses"] == []
    assert data["sources"]["elementary"]["status"] == "read"


def test_a_failure_with_no_defect_seeded_reports_no_cause_and_what_was_ruled_out(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=False)
    data = _diagnose(root, warehouse)
    [diagnosis] = data["diagnoses"]

    assert diagnosis["verdict"] == "no_cause"
    assert diagnosis["causes"] == []
    [ruled_out] = [c for c in diagnosis["ruled_out"] if c["owner"] == "loader"]
    assert ruled_out["kind"] == "duplicate_load"
    assert ruled_out["reason"] == "still_unique"
    assert ruled_out["check"] == "maintain check: grain axis"
    assert diagnosis["unseen"]
    assert diagnosis["next_steps"] == []


# --- acceptance: with and without a snapshot series -------------------------------


def test_with_no_series_the_cause_comes_with_the_window_nothing_recorded(
    tmp_path: Path,
):
    root, warehouse, runs = _project(tmp_path, duplicated=True)
    data = _diagnose(root, warehouse)
    cause = _cause(data)
    [diagnosis] = data["diagnoses"]

    assert data["series"]["status"] == "read"
    assert data["series"]["count"] == 0
    assert diagnosis["snapshots"]["series"] == "none"
    assert cause["finding"] == "declared_grain_not_unique"
    assert cause["evidence"]["measured_against"] == {"claim": TEST}
    [window] = diagnosis["unseen"]
    assert window["reason"] == "no_snapshot_series"
    # From the last observed pass to the first observed failure.
    assert window["from_at"].startswith(runs[1].started.isoformat()[:16])
    assert window["to_at"].startswith(runs[2].started.isoformat()[:16])


def test_with_a_series_the_cause_comes_with_the_snapshots_that_bound_it(
    tmp_path: Path,
):
    runs = _runs()
    before = runs[0].started + timedelta(minutes=30)
    root, warehouse, _ = _project(tmp_path, duplicated=True, snapshot_before=before)
    data = _diagnose(root, warehouse)
    cause = _cause(data)
    [diagnosis] = data["diagnoses"]

    assert data["series"]["count"] == 1
    bounds = diagnosis["snapshots"]
    assert bounds["series"] == "read"
    assert bounds["before"]["created_at"].startswith(before.isoformat()[:16])
    assert bounds["after"] is None
    # Measured against the bounding snapshot, as maintain check reads it.
    assert cause["finding"] == "key_lost_uniqueness"
    assert cause["evidence"]["measured_against"]["snapshot_created_at"].startswith(
        before.isoformat()[:16]
    )
    assert cause["evidence"]["volume"]["row_count_before"] == 200
    assert cause["evidence"]["volume"]["row_count_after"] == 250
    [window] = diagnosis["unseen"]
    assert window["reason"] == "between_snapshots"
    assert window["to_at"] is None


def test_with_a_series_and_no_defect_the_snapshot_bounds_a_no_cause_answer(
    tmp_path: Path,
):
    runs = _runs()
    before = runs[0].started + timedelta(minutes=30)
    root, warehouse, _ = _project(tmp_path, duplicated=False, snapshot_before=before)
    data = _diagnose(root, warehouse)
    [diagnosis] = data["diagnoses"]

    assert diagnosis["verdict"] == "no_cause"
    [ruled_out] = [c for c in diagnosis["ruled_out"] if c["owner"] == "loader"]
    assert ruled_out["finding"] == "key_lost_uniqueness"
    assert diagnosis["snapshots"]["before"] is not None


# --- acceptance: an unobserved run widens the range -------------------------------


def test_a_run_whose_result_was_not_recorded_widens_the_range(tmp_path: Path):
    # inv-2 recorded no result for the test: the range reaches back to inv-1.
    root, warehouse, _ = _project(tmp_path, duplicated=True, unobserved=("inv-2",))
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert diagnosis["range"]["after"]["run"] == "inv-1"
    assert diagnosis["range"]["through"]["run"] == "inv-3"
    assert diagnosis["range"]["unobserved_runs"] == 1
    assert diagnosis["range"]["span_seconds"] == 2 * 3600


def test_the_same_run_observed_narrows_the_range(tmp_path: Path):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert diagnosis["range"]["after"]["run"] == "inv-2"
    assert diagnosis["range"]["unobserved_runs"] == 0
    assert diagnosis["range"]["span_seconds"] == 3600


# --- the answer's other parts -----------------------------------------------------


def test_the_owners_this_release_does_not_check_are_listed_as_not_checked(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert {c["owner"] for c in diagnosis["not_checked"]} >= {
        "seed",
        "package",
        "schedule",
    }


def test_a_test_that_is_not_a_uniqueness_test_checks_no_loader_candidate(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    manifest = json.loads((root / "target" / "manifest.json").read_text())
    manifest["nodes"][TEST]["test_metadata"] = {
        "name": "not_null",
        "kwargs": {"column_name": "order_item_id"},
    }
    (root / "target" / "manifest.json").write_text(json.dumps(manifest))
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert diagnosis["verdict"] == "no_cause"
    assert not [c for c in diagnosis["ruled_out"] if c["owner"] == "loader"]


def test_no_value_from_the_data_reaches_the_envelope(tmp_path: Path):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    text = json.dumps(_diagnose(root, warehouse))

    assert "a value the test failed on" not in text


def test_the_cli_runs_the_same_diagnosis(tmp_path: Path):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    args = argparse.Namespace(
        group="maintain", subcommand="diagnose", objects=[], invocations=None
    )
    envelope = dispatch(args, engine)

    assert envelope.status == "ok", envelope.errors
    [diagnosis] = envelope.data["diagnoses"]
    assert diagnosis["verdict"] == "cause"


def test_an_invocations_count_out_of_range_is_refused_before_anything_opens(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    args = argparse.Namespace(
        group="maintain", subcommand="diagnose", objects=[], invocations=0
    )

    assert dispatch(args, engine).status == "error"


# --- the checks are priced before they scan ---------------------------------------


def test_the_checks_are_priced_as_one_handshake_before_any_probe_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.maintain import diagnose as diagnose_mod
    from exmergo_dex_core.maintain import drift as drift_mod

    root, warehouse, _ = _project(tmp_path, duplicated=True)
    events: list[tuple[str, Any]] = []
    priced = diagnose_mod.command_args.billed_handshake
    probed = drift_mod.run_batched_probes

    def handshake(command, adapter, estimate, **kwargs):
        events.append(("priced", (command, kwargs.get("axes"))))
        return priced(command, adapter, estimate, **kwargs)

    def probe(adapter, batched):
        events.append(("probed", None))
        return probed(adapter, batched)

    monkeypatch.setattr(diagnose_mod.command_args, "billed_handshake", handshake)
    monkeypatch.setattr(drift_mod, "run_batched_probes", probe)
    _diagnose(root, warehouse)

    # The incident read prices its own statements first, under this command's
    # name; the checks are the one handshake on the grain axis.
    checks = [e for e in events if e[0] == "priced" and e[1][1] == ["grain"]]
    assert checks == [("priced", ("maintain diagnose", ["grain"]))]
    first_probe = next(i for i, e in enumerate(events) if e[0] == "probed")
    assert events.index(checks[0]) < first_probe


# --- the cold review's findings (#1574) -------------------------------------------


def _loader(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        c
        for part in ("causes", "ruled_out", "not_checked")
        for c in diagnosis[part]
        if c["owner"] == "loader"
    ]


def test_a_second_source_at_another_grain_is_not_confirmed_without_a_before(
    tmp_path: Path,
):
    # The model reads a source whose key repeats by design beside the one the
    # loader filled. With no measured before, today's data cannot tell them
    # apart, so neither is named.
    root, warehouse, _ = _project(
        tmp_path, duplicated=False, extra_sources=("order_item_events",)
    )
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert diagnosis["verdict"] == "no_cause"
    reasons = {c["relation"]: c["reason"] for c in _loader(diagnosis)}
    assert reasons == {
        RELATION: "several_sources",
        "dev.raw.order_item_events": "several_sources",
    }


def test_a_before_snapshot_that_measured_the_key_not_unique_rules_the_loader_out(
    tmp_path: Path,
):
    runs = _runs()
    before = runs[0].started + timedelta(minutes=30)
    root, warehouse, _ = _project(
        tmp_path, duplicated=True, snapshot_before=before, refuted_before=True
    )
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert diagnosis["verdict"] == "no_cause"
    [loader] = _loader(diagnosis)
    assert loader["status"] == "ruled_out"
    assert loader["reason"] == "not_unique_at_before_snapshot"


def test_a_relation_nothing_counted_is_not_checked_rather_than_ruled_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A connector that keeps no row count for the relation (a BigQuery view or
    # external table): the grain axis judges nothing, so nothing is ruled out.
    import dataclasses

    root, warehouse, _ = _project(tmp_path, duplicated=True)
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    opened = engine._adapter

    def uncounted(*args, **kwargs):
        adapter = opened(*args, **kwargs)
        described = adapter.table_metadata

        def metadata(identifier, *more, **named):
            meta, columns = described(identifier, *more, **named)
            if identifier == RELATION:
                meta = dataclasses.replace(meta, row_count=None)
            return meta, columns

        monkeypatch.setattr(adapter, "table_metadata", metadata)
        return adapter

    monkeypatch.setattr(engine, "_adapter", uncounted)
    [diagnosis] = to_envelope(engine.diagnose()).data["diagnoses"]

    [loader] = _loader(diagnosis)
    assert loader["status"] == "not_checked"
    assert loader["reason"] == "uncounted"
    assert diagnosis["verdict"] == "no_cause"


def test_a_source_the_connection_does_not_list_is_named_as_such(tmp_path: Path):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    manifest = json.loads((root / "target" / "manifest.json").read_text())
    manifest["sources"][SOURCE]["identifier"] = "order_items_unbuilt"
    (root / "target" / "manifest.json").write_text(json.dumps(manifest))
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    [loader] = _loader(diagnosis)
    assert loader["status"] == "not_checked"
    assert loader["reason"] == "source_not_listed"


def test_checks_that_do_not_fit_what_the_read_left_are_offered_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.maintain import drift as drift_mod

    root, warehouse, _ = _project(tmp_path, duplicated=True)
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    opened = engine._adapter

    def gated(*args, **kwargs):
        adapter = opened(*args, **kwargs)
        adapter.cost_gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=1_000.0,
            session_ceiling=100_000_000.0,
            session_spent=0.0,
            confirmed=True,
            connector="bigquery",
            command="maintain diagnose",
        )
        return adapter

    probed: list[str] = []
    monkeypatch.setattr(engine, "_adapter", gated)
    monkeypatch.setattr(
        drift_mod, "grain_estimate", lambda adapter, plan, batched=None: (5_000.0, {})
    )
    monkeypatch.setattr(
        drift_mod,
        "run_batched_probes",
        lambda adapter, batched: probed.append("probe") or {},
    )
    data = to_envelope(engine.diagnose()).data
    [diagnosis] = data["diagnoses"]

    assert probed == []
    assert data["price"]["checks"]["offered"] is True
    [loader] = _loader(diagnosis)
    assert loader["reason"] == "scan_offered"
    assert diagnosis["verdict"] == "no_cause"


def test_an_incident_that_closed_is_listed_undiagnosed_with_why(tmp_path: Path):
    # inv-2 fails and the two passes after it close the incident.
    root, warehouse, _ = _project(tmp_path, duplicated=True, failing=("inv-2",))
    data = _diagnose(root, warehouse)

    assert data["diagnoses"] == []
    [entry] = data["not_diagnosed"]
    assert entry["state"] == "closed"
    assert entry["reason"] == "not_open"


# --- the history and the spend the box's planted pass found untested -------------


def test_the_repository_s_history_reaches_the_fold_and_rules_the_project_out(
    tmp_path: Path,
):
    # Every run deployed the same commit, which passed before it failed: read
    # from the repository's history, the break spans no commit, so the project
    # is ruled out. Without the history the range is not read at all.
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _git(root, "add", "dbt_project.yml")
    _git(root, "commit", "-q", "-m", "project")
    head = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        env=git_env(),
        text=True,
    ).stdout.strip()
    con = duckdb.connect(str(warehouse))
    con.execute("UPDATE main_elementary.dbt_invocations SET git_sha = ?", [head])
    con.close()
    [diagnosis] = _diagnose(root, warehouse)["diagnoses"]

    assert diagnosis["range"]["commits"]["status"] == "same_commit"
    [project] = [c for c in diagnosis["ruled_out"] if c["owner"] == "project"]
    assert project["reason"] == "same_commit"
    assert project["evidence"]["commit"] == head


def test_the_command_s_spend_sums_the_incident_read_and_the_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core import command_args
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    root, warehouse, _ = _project(tmp_path, duplicated=True)
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    opened = engine._adapter

    def gated(*args, **kwargs):
        adapter = opened(*args, **kwargs)
        adapter.cost_gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=10_000_000.0,
            session_ceiling=100_000_000.0,
            session_spent=0.0,
            confirmed=True,
            connector="bigquery",
            command="maintain diagnose",
        )
        return adapter

    # Each phase settles once: the incident read first, then the checks.
    settlements = iter(
        [
            {"bytes_billed": 100, "session_spent_today": 100},
            {"bytes_billed": 40, "session_spent_today": 140},
        ]
    )
    monkeypatch.setattr(engine, "_adapter", gated)
    monkeypatch.setattr(
        command_args, "settled_spend", lambda adapter: next(settlements)
    )
    data = to_envelope(engine.diagnose()).data

    assert data["spend"]["bytes_billed"] == 140
    assert data["spend"]["session_spent_today"] == 140
    assert data["price"]["incidents"]["settled"]["bytes_billed"] == 100
    assert data["price"]["checks"]["settled"]["bytes_billed"] == 40


def test_a_git_dir_in_the_environment_does_not_redirect_the_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A process started from a git hook, or under a harness that exports
    # GIT_DIR, still reads the repository that holds .dex, never that one.
    runs = _runs()
    before = runs[0].started + timedelta(minutes=30)
    root, warehouse, _ = _project(tmp_path, duplicated=True, snapshot_before=before)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    data = _diagnose(root, warehouse)

    assert data["series"]["count"] == 1
    assert _cause(data)["finding"] == "key_lost_uniqueness"


# --- the series reader on every git it meets --------------------------------------


def _commit_snapshot(root: Path, created: datetime, message: str) -> None:
    dex = root / ".dex"
    dex.mkdir(exist_ok=True)
    snap = Snapshot(created_at=created.isoformat(), connector="duckdb")
    (dex / "snapshot.json").write_text(snap.model_dump_json(indent=2))
    _git(root, "add", ".dex/snapshot.json")
    _git(root, "commit", "-q", "-m", message)


def test_a_snapshot_in_the_root_commit_is_read_and_a_removal_is_not_a_version(
    tmp_path: Path,
):
    # The root commit adds the first baseline, a later commit removes the file
    # and a third adds it again. Every version is read, the root commit's
    # included (git 2.43 drops it under a lowercase --diff-filter), and the
    # removal is counted apart from versions that did not parse.
    from exmergo_dex_core.maintain.snapshot_series import read_series

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    first = datetime(2026, 11, 1, 1, tzinfo=UTC)
    _commit_snapshot(root, first, "first baseline")
    _git(root, "rm", "-q", ".dex/snapshot.json")
    _git(root, "commit", "-q", "-m", "remove the baseline")
    second = datetime(2026, 11, 1, 3, tzinfo=UTC)
    _commit_snapshot(root, second, "second baseline")
    (root / "unrelated.txt").write_text("x\n")
    _git(root, "add", "unrelated.txt")
    _git(root, "commit", "-q", "-m", "unrelated")

    read = read_series(root / ".dex")

    assert read.series.status == "read"
    assert [p.created_at for p in read.series.points] == [first, second]
    assert read.removed == 1
    assert read.skipped == 0


def test_a_root_commit_alone_is_a_series_of_one(tmp_path: Path):
    from exmergo_dex_core.maintain.snapshot_series import read_series

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    only = datetime(2026, 11, 1, 1, tzinfo=UTC)
    _commit_snapshot(root, only, "first baseline")

    read = read_series(root / ".dex")

    assert [p.created_at for p in read.series.points] == [only]
    assert (read.removed, read.skipped) == (0, 0)


def test_a_repository_with_no_commit_yet_reads_as_no_series_not_unreadable(
    tmp_path: Path,
):
    # A .dex directory inside a repository nothing has been committed to: the
    # history is readable and holds no baseline, which is not a history that
    # could not be read.
    from exmergo_dex_core.maintain.snapshot_series import read_series

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    dex = root / ".dex"
    dex.mkdir()
    snap = Snapshot(created_at=datetime(2026, 11, 1, tzinfo=UTC).isoformat())
    (dex / "snapshot.json").write_text(snap.model_dump_json(indent=2))

    read = read_series(dex)

    assert read.series.status == "read"
    assert read.series.points == ()
    assert "no commit yet" in (read.series.reason or "")


# --- acceptance (#1478): the semantic-layer objects that read the column ----------


def _semantic_layer(
    root: Path,
    *,
    measure_expr: str = "order_item_id",
    extra: str = "",
) -> None:
    """dbt semantic YAML on the tested model: one measure on ``measure_expr``,
    a simple metric on it, and a ratio built on that metric."""

    models = root / "models"
    models.mkdir(exist_ok=True)
    (models / "semantic.yml").write_text(
        "semantic_models:\n"
        "  - name: order_items\n"
        "    model: ref('stg_order_items')\n"
        "    measures:\n"
        "      - name: order_item_count\n"
        "        agg: count_distinct\n"
        f"        expr: {measure_expr}\n"
        "      - name: quantity_total\n"
        "        agg: sum\n"
        "        expr: quantity\n"
        f"{extra}"
        "metrics:\n"
        "  - name: order_items\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: order_item_count\n"
        "  - name: units\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: quantity_total\n"
        "  - name: items_per_unit\n"
        "    type: ratio\n"
        "    type_params:\n"
        "      numerator: order_items\n"
        "      denominator: units\n"
    )


def _readers(data: dict[str, Any]) -> dict[str, Any]:
    [diagnosis] = data["diagnoses"]
    return diagnosis["semantic_readers"]


def test_a_measure_reading_the_failing_column_is_named_with_its_metrics(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(root)
    readers = _readers(_diagnose(root, warehouse))

    assert readers["status"] == "read"
    assert readers["vendor"] == "dbt"
    assert readers["tested"] == [
        {"test": TEST, "model": "stg_order_items", "columns": ["order_item_id"]}
    ]
    assert readers["readers"] == [
        {
            "semantic_model": "order_items",
            "model": "stg_order_items",
            "kind": "measure",
            "name": "order_item_count",
            "column": "order_item_id",
            # The simple metric on the measure, and the ratio built on it.
            "metrics": ["items_per_unit", "order_items"],
        }
    ]
    assert readers["unresolved"] == []


def test_the_same_measure_reading_another_column_names_nothing(tmp_path: Path):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(root, measure_expr="quantity")
    readers = _readers(_diagnose(root, warehouse))

    # Read, and nothing in it reads the column: empty, not absent.
    assert readers["status"] == "read"
    assert readers["readers"] == []
    assert readers["unresolved"] == []


def test_a_project_with_no_semantic_layer_reads_as_absent_not_empty(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    readers = _readers(_diagnose(root, warehouse))

    assert readers["status"] == "absent"
    assert readers["reason"] == "no_semantic_layer"
    assert readers["readers"] == []


def test_a_computed_expression_on_the_tested_model_is_listed_as_unresolved(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(root, measure_expr="case when quantity > 0 then order_item_id end")
    readers = _readers(_diagnose(root, warehouse))

    assert readers["status"] == "read"
    assert readers["readers"] == []
    assert readers["unresolved"] == [
        {
            "semantic_model": "order_items",
            "model": "stg_order_items",
            "kind": "measure",
            "name": "order_item_count",
            "reason": "computed_expression",
            "metrics": ["items_per_unit", "order_items"],
        }
    ]


def test_an_entity_and_a_dimension_on_the_column_are_named_without_metrics(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(
        root,
        measure_expr="quantity",
        extra=(
            "    entities:\n"
            "      - name: order_item\n"
            "        type: primary\n"
            "        expr: ORDER_ITEM_ID\n"
            "    dimensions:\n"
            "      - name: order_item_id\n"
            "        type: categorical\n"
        ),
    )
    readers = _readers(_diagnose(root, warehouse))

    assert [(r["kind"], r["name"], r["metrics"]) for r in readers["readers"]] == [
        ("dimension", "order_item_id", []),
        ("entity", "order_item", []),
    ]


def test_a_semantic_file_that_does_not_parse_reads_as_unreadable_not_absent(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    models = root / "models"
    models.mkdir()
    (models / "semantic.yml").write_text(
        "semantic_models:\n  - name: order_items\n    model: [unclosed\n"
    )
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    envelope = to_envelope(engine.diagnose())
    readers = _readers(envelope.data)

    assert readers["status"] == "unreadable"
    assert readers["reason"] == "unparsed_files"
    assert readers["unread_files"] == ["models/semantic.yml"]
    assert any("semantic_readers may miss" in w for w in envelope.warnings)


def test_a_test_on_no_column_reads_no_semantic_layer(tmp_path: Path):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(root)
    manifest = json.loads((root / "target" / "manifest.json").read_text())
    node = manifest["nodes"][TEST]
    node["test_metadata"] = {"name": "expression_is_true", "kwargs": {}}
    node.pop("column_name")
    (root / "target" / "manifest.json").write_text(json.dumps(manifest))
    readers = _readers(_diagnose(root, warehouse))

    assert readers["status"] == "not_read"
    assert readers["reason"] == "no_tested_column"


def test_naming_the_readers_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Repo-only: while the semantic layer is read, nothing may open the
    # warehouse. The incident read before it queries Elementary's tables, as
    # it always has; the layer read adds nothing to that.
    from exmergo_dex_core.maintain import diagnose as diagnose_mod

    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(root)
    real = diagnose_mod._Layer.read.__func__  # type: ignore[attr-defined]

    def guarded(cls: Any, engine: DexEngine) -> Any:
        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("the semantic-layer read opened a connection")

        saved = engine._adapter
        engine._adapter = refuse  # type: ignore[method-assign]
        try:
            return real(cls, engine)
        finally:
            engine._adapter = saved  # type: ignore[method-assign]

    monkeypatch.setattr(diagnose_mod._Layer, "read", classmethod(guarded))
    readers = _readers(_diagnose(root, warehouse))

    assert [r["name"] for r in readers["readers"]] == ["order_item_count"]


def test_a_test_that_is_not_a_uniqueness_test_reads_the_column_it_is_attached_to(
    tmp_path: Path,
):
    root, warehouse, _ = _project(tmp_path, duplicated=True)
    _semantic_layer(root)
    manifest = json.loads((root / "target" / "manifest.json").read_text())
    manifest["nodes"][TEST]["test_metadata"] = {"name": "not_null", "kwargs": {}}
    (root / "target" / "manifest.json").write_text(json.dumps(manifest))
    readers = _readers(_diagnose(root, warehouse))

    assert readers["tested"][0]["columns"] == ["order_item_id"]
    assert [r["name"] for r in readers["readers"]] == ["order_item_count"]
