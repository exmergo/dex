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
    )


def _manifest() -> dict[str, Any]:
    nodes: dict[str, Any] = {
        MODEL: {
            "unique_id": MODEL,
            "resource_type": "model",
            "package_name": "shop",
            "name": "stg_order_items",
            "database": "dev",
            "schema": "main",
            "config": {"enabled": True},
            "depends_on": {"nodes": [SOURCE]},
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
            SOURCE: {
                "unique_id": SOURCE,
                "resource_type": "source",
                "package_name": "shop",
                "source_name": "raw",
                "name": "order_items",
                "identifier": "order_items",
                "database": "dev",
                "schema": "raw",
            }
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
) -> tuple[Path, Path, list[Run]]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: shop\nversion: '1.0'\n")
    target = root / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(_manifest()))
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
                        candidate_keys=[["order_item_id"]],
                        columns=[
                            ColumnProfile(
                                name="order_item_id",
                                data_type="INTEGER",
                                distinct_count=200,
                                distinct_count_exact=True,
                                is_unique=True,
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
