"""`transform build --full-refresh`: rebuild an incremental model whose logic
changed (#1015).

An incremental run only adds or merges rows, so a fixed model keeps what the
old logic wrote until it is rebuilt. These tests pin the flag through every
layer that has to agree on it: the argv dbt runs, the compile that prices it,
the summary that names what was rebuilt, and the evidence that records it.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from tests.transform.test_build import (
    _fake_runner_factory,
    _install_fake_pricing,
    _run,
    bigquery_project_dir,  # noqa: F401 -- a fixture, used by name
)

from exmergo_dex_core.transform.build import (
    ProdTargetRefusedError,
    build,
    compile_estimate,
    full_refresh_nodes,
)

_NODES = {
    "model.dex_test.fct_events": {
        "resource_type": "model",
        "name": "fct_events",
        "config": {"materialized": "incremental"},
        "compiled_code": "select 1 as id",
    },
    "model.dex_test.fct_frozen": {
        "resource_type": "model",
        "name": "fct_frozen",
        "config": {"materialized": "incremental", "full_refresh": False},
        "compiled_code": "select 1 as id",
    },
    "model.dex_test.dim_users": {
        "resource_type": "model",
        "name": "dim_users",
        "config": {"materialized": "table"},
        "compiled_code": "select 1 as id",
    },
    "seed.dex_test.countries": {
        "resource_type": "seed",
        "name": "countries",
        "config": {"materialized": "seed"},
    },
    "snapshot.dex_test.users_snapshot": {
        "resource_type": "snapshot",
        "name": "users_snapshot",
        "config": {"materialized": "snapshot"},
        "compiled_code": "select 1 as id",
    },
}


def _write_artifacts(directory: Path, nodes: dict = _NODES) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )
    (directory / "run_results.json").write_text(
        json.dumps(
            {
                "results": [{"unique_id": uid, "status": "success"} for uid in nodes],
                "elapsed_time": 1.0,
            }
        ),
        encoding="utf-8",
    )


def _dbt(seen: list[list[str]]):
    """A fake dbt that writes the artifacts of a run where it was told to."""

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        _write_artifacts(Path(argv[argv.index("--target-path") + 1]))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return runner


def test_full_refresh_is_passed_to_dbt_build_after_the_selection(
    dbt_project_dir: Path,
):
    seen: list[list[str]] = []
    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=_dbt(seen),
        select="fct_events",
        full_refresh=True,
    )

    argv = seen[-1]
    assert argv[1] == "build"
    assert argv[-3:] == ["--select", "fct_events", "--full-refresh"]
    assert summary["success"] is True


def test_a_build_without_the_flag_never_full_refreshes(dbt_project_dir: Path):
    seen: list[list[str]] = []
    summary, _cost = build(
        dbt_project_dir, target="dev", confirmed=True, runner=_dbt(seen)
    )

    assert all("--full-refresh" not in argv for argv in seen)
    assert "full_refresh" not in summary


def test_a_full_refresh_is_still_dev_target_only(dbt_project_dir: Path):
    seen: list[list[str]] = []
    with pytest.raises(ProdTargetRefusedError):
        build(
            dbt_project_dir,
            target="prod",
            confirmed=True,
            runner=_dbt(seen),
            full_refresh=True,
        )
    assert seen == []


def test_the_summary_names_what_was_rebuilt_and_what_dbt_skipped(
    dbt_project_dir: Path,
):
    seen: list[list[str]] = []
    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=_dbt(seen),
        select="fct_events fct_frozen countries dim_users users_snapshot",
        full_refresh=True,
    )

    assert summary["full_refresh"] == {
        "requested": True,
        "models": ["fct_events"],
        "seeds": ["countries"],
        # Its own `full_refresh: false` makes dbt ignore the flag, so the rows
        # it keeps must not read as rebuilt.
        "not_refreshed": ["fct_frozen"],
        "success": True,
    }
    assert not any(
        "--full-refresh with no --select" in n for n in summary.get("notes", [])
    )


def test_an_unselected_full_refresh_warns_with_the_count(dbt_project_dir: Path):
    seen: list[list[str]] = []
    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=_dbt(seen),
        full_refresh=True,
    )

    note = next(n for n in summary["notes"] if "--full-refresh with no --select" in n)
    assert "1 incremental model and 1 seed" in note


def test_a_requested_full_refresh_skips_the_stale_seed_pre_step(
    dbt_project_dir: Path,
):
    """`dbt build --full-refresh` already rebuilds every seed it selects."""

    asked: list[list[str]] = []

    def relation_columns(names: list[str]) -> dict[str, list[str]]:
        asked.append(names)
        return {}

    seen: list[list[str]] = []
    build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=_dbt(seen),
        relation_columns=relation_columns,
        full_refresh=True,
    )

    assert asked == []
    assert [argv[1] for argv in seen] == ["build"]


def test_a_failed_full_refresh_reports_the_build_failure(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The seed pre-step's 'dbt seed --full-refresh failed' prefix is not
    borrowed for a requested refresh: the build is what failed."""

    _fake_runner_factory(monkeypatch, returncode=1, stdout="")
    _rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--full-refresh",
            "--confirm",
        ],
        capsys,
    )
    assert envelope["status"] == "error"
    assert envelope["errors"][0].startswith("dbt build failed")
    assert envelope["data"]["full_refresh"]["requested"] is True
    assert envelope["data"]["full_refresh"]["success"] is False


def test_the_cli_flag_reaches_dbt_and_the_evidence(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    calls = _fake_runner_factory(
        monkeypatch,
        on_run=lambda argv: _write_artifacts(
            Path(argv[argv.index("--target-path") + 1])
        ),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--select",
            "fct_events",
            "--full-refresh",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    builds = [c["argv"] for c in calls if c["argv"][1] == "build"]
    assert builds and "--full-refresh" in builds[-1]
    data = envelope["data"]
    assert data["full_refresh"]["models"] == ["fct_events"]
    assert data["evidence"]["invocation"]["full_refresh"] is True


def test_an_ordinary_build_records_no_full_refresh_in_its_evidence(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _fake_runner_factory(
        monkeypatch,
        on_run=lambda argv: _write_artifacts(
            Path(argv[argv.index("--target-path") + 1])
        ),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["evidence"]["invocation"]["full_refresh"] is False


class _Estimator:
    dialect = "duckdb"

    def query_estimate(self, sql: str) -> float:
        return 10.0


def test_compile_estimate_compiles_the_full_rebuild(dbt_project_dir: Path):
    """`is_incremental()` renders false under `--full-refresh`, so the priced
    SQL is the full SELECT the build will run, not the batch."""

    seen: list[list[str]] = []
    artifacts = dbt_project_dir / "target" / "dex" / "dbt-test"
    total, per_node, notes = compile_estimate(
        dbt_project_dir,
        _Estimator(),
        target="dev",
        runner=_dbt(seen),
        artifact_directory=artifacts,
        full_refresh=True,
    )

    assert seen[0][1] == "compile"
    assert "--full-refresh" in seen[0]
    assert Path(seen[0][seen[0].index("--target-path") + 1]) == artifacts
    assert any("priced as a full refresh" in n for n in notes)
    assert any("--full-refresh with no --select" in n for n in notes)
    assert total == sum(per_node.values())


def test_compile_estimate_without_the_flag_prices_the_batch(dbt_project_dir: Path):
    seen: list[list[str]] = []
    _total, _per_node, notes = compile_estimate(
        dbt_project_dir,
        _Estimator(),
        target="dev",
        runner=_dbt(seen),
        artifact_directory=dbt_project_dir / "target" / "dex" / "dbt-test",
    )
    assert "--full-refresh" not in seen[0]
    assert not any("full refresh" in n for n in notes)


def test_full_refresh_nodes_reads_nothing_from_missing_artifacts(tmp_path: Path):
    assert full_refresh_nodes(tmp_path / "absent") == {
        "models": [],
        "seeds": [],
        "not_refreshed": [],
    }


def test_billed_full_refresh_prices_without_upper_bounds(
    bigquery_project_dir: Path,  # noqa: F811 -- the fixture imported above
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    """A full refresh has no watermark filter left to prune, so no line is an
    upper bound, and the compile that priced it was told it is a refresh."""

    from exmergo_dex_core.envelope import Paradigm

    adapter = _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=6_000_000_000.0,
        per_node={"stg_events": 6_000_000_000.0},
        confirmed=False,
        ceiling=10_000_000_000,
    )
    adapter.prunes_dynamic_bounds_at_run_time = True
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    priced: dict = {}

    def fake_estimate(project, adp, *, target, select=None, **kw):
        priced.update(kw)
        return 6_000_000_000.0, {"stg_events": 6_000_000_000.0}, []

    monkeypatch.setattr(build_module, "compile_estimate", fake_estimate)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--target",
            "dev",
            "--select",
            "stg_events",
            "--full-refresh",
            "--budget",
            "10000000000",
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert priced["full_refresh"] is True
    assert "upper_bounds" not in envelope["data"]
