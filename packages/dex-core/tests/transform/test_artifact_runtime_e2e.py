"""Cross-runtime artifact isolation, exercised without two dbt installs (#862).

The supported pair is one shared ``target/`` and a second runtime key. Process
behavior is a fake ``dbt build`` argv recorder. Directory-vs-file collisions
are planted on disk, which is the failure incompatible dbt versions leave
behind.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.transform.test_build_evidence import (
    node,
    write_manifest_nodes,
    write_run_results,
)

from exmergo_dex_core.transform.artifact_runtime import (
    ForeignInvocationError,
    IncompatibleArtifactsError,
    assert_same_invocation,
    dbt_runtime_key,
    find_snapshot_path_collisions,
    prepare_artifact_dir,
    readable_artifact_dir,
    resolve_build_artifact_dir,
    shared_target_dir,
)
from exmergo_dex_core.transform.build import build
from exmergo_dex_core.transform.evidence import BuildOutcome, build_evidence

_RUNTIME_B = "dbt-1.10.11"


def _plant_directory_as_sql(shared: Path) -> Path:
    """Snapshot compiled SQL left as a directory by the other runtime."""

    snap = shared / "run" / "demo" / "snapshots" / "check_row.sql"
    snap.mkdir(parents=True)
    (snap / "nested.sql").write_text("select 1\n", encoding="utf-8")
    return snap


def _write_success(directory: Path, invocation_id: str = "abc-123") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "invocation_id": invocation_id,
                    "dbt_version": "1.11.11",
                    "adapter_version": "1.10.11",
                    "generated_at": datetime.now(UTC).isoformat(),
                },
                "results": [node("mart")],
                "elapsed_time": 0.2,
            }
        ),
        encoding="utf-8",
    )
    compiled_at = datetime.now(UTC) + timedelta(minutes=1)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "dex_test",
                    "generated_at": compiled_at.isoformat(),
                },
                "nodes": {
                    "model.dex_test.mart": {
                        "name": "mart",
                        "resource_type": "model",
                        "relation_name": '"dev"."main"."mart"',
                        "config": {"materialized": "view"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_cross_runtime_handoff_reads_the_isolated_directory(dbt_project_dir: Path):
    shared = dbt_project_dir / "target"
    planted = _plant_directory_as_sql(shared)
    isolated = prepare_artifact_dir(dbt_project_dir, _RUNTIME_B)
    assert isolated == shared / "dex" / _RUNTIME_B
    _write_success(isolated)

    evidence = build_evidence(
        dbt_project_dir, target="dev", artifact_directory=isolated
    )

    assert evidence.outcome is BuildOutcome.VALIDATED
    producer = evidence.invocation["artifact_producer"]
    assert producer["artifact_directory"] == f"target/dex/{_RUNTIME_B}"
    assert producer["invocation_id"] == "abc-123"
    assert planted.is_dir()
    assert (planted / "nested.sql").is_file()


def test_same_runtime_reuses_a_clean_shared_target(tmp_path: Path):
    project = tmp_path / "proj"
    shared = project / "target"
    shared.mkdir(parents=True)
    (shared / "manifest.json").write_text("{}\n", encoding="utf-8")

    chosen = prepare_artifact_dir(project, _RUNTIME_B)

    assert chosen == shared
    assert not (shared / "dex").exists()
    assert (shared / "manifest.json").is_file()


def test_absent_artifacts_are_not_a_successful_completion(dbt_project_dir: Path):
    # The shared target would validate on its own. The empty directory is what
    # has to win, or this passes while ``artifact_directory`` is ignored.
    write_run_results(dbt_project_dir, [node("mart")])
    write_manifest_nodes(
        dbt_project_dir,
        {
            "model.dex_test.mart": {
                "name": "mart",
                "resource_type": "model",
                "relation_name": '"dev"."main"."mart"',
                "config": {"materialized": "view"},
            }
        },
    )
    empty = dbt_project_dir / "empty-artifacts"
    empty.mkdir()

    evidence = build_evidence(dbt_project_dir, target="dev", artifact_directory=empty)

    assert evidence.outcome is BuildOutcome.NOT_RUN


def test_interrupted_shared_build_is_not_this_runtime_success(dbt_project_dir: Path):
    shared = dbt_project_dir / "target"
    planted = _plant_directory_as_sql(shared)
    write_run_results(dbt_project_dir, [node("mart")], invocation_id="interrupted-run")
    write_manifest_nodes(
        dbt_project_dir,
        {
            "model.dex_test.mart": {
                "name": "mart",
                "resource_type": "model",
                "relation_name": '"dev"."main"."mart"',
                "config": {"materialized": "view"},
            }
        },
    )

    isolated = prepare_artifact_dir(dbt_project_dir, "dbt-1.12.6")
    evidence = build_evidence(
        dbt_project_dir, target="dev", artifact_directory=isolated
    )
    shared_evidence = build_evidence(
        dbt_project_dir, target="dev", artifact_directory=shared
    )

    assert isolated != shared
    assert evidence.outcome is BuildOutcome.NOT_RUN
    assert shared_evidence.outcome is BuildOutcome.VALIDATED
    assert shared_evidence.invocation["invocation_id"] == "interrupted-run"
    assert planted.is_dir()
    assert (shared / "run_results.json").is_file()


def test_build_passes_isolated_target_path_when_the_shared_dir_collides(
    dbt_project_dir: Path,
):
    shared = dbt_project_dir / "target"
    planted = _plant_directory_as_sql(shared)
    (shared / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"invocation_id": "stale-shared"},
                "results": [node("mart")],
                "elapsed_time": 4.0,
            }
        ),
        encoding="utf-8",
    )
    isolated = shared / "dex" / _RUNTIME_B
    isolated.mkdir(parents=True)
    (isolated / "run_results.json").write_text(
        json.dumps(
            {
                "metadata": {"invocation_id": "stale-isolated"},
                "results": [node("mart", status="error")],
                "elapsed_time": 9.0,
            }
        ),
        encoding="utf-8",
    )
    (isolated / "manifest.json").write_text("{}\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        seen["isolated_results"] = (isolated / "run_results.json").exists()
        seen["shared_results"] = (shared / "run_results.json").is_file()
        seen["isolated_manifest"] = (isolated / "manifest.json").is_file()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=runner,
        runtime_key=_RUNTIME_B,
    )

    argv = seen["argv"]
    assert isinstance(argv, list)
    assert Path(argv[argv.index("--target-path") + 1]) == isolated
    assert seen["isolated_results"] is False
    assert seen["shared_results"] is True
    assert seen["isolated_manifest"] is True
    assert planted.is_dir()
    assert summary["success"] is True
    assert summary["nodes"] == []
    assert "elapsed_seconds" not in summary
    assert summary["artifact_directory"] == f"target/dex/{_RUNTIME_B}"


def test_explicit_artifact_directory_is_passed_as_target_path(
    dbt_project_dir: Path,
):
    custom = dbt_project_dir / "ci-artifacts"
    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=runner,
        artifact_directory=custom,
    )

    argv = seen["argv"]
    assert Path(argv[argv.index("--target-path") + 1]) == custom
    assert summary["artifact_directory"] == "ci-artifacts"
    assert summary["nodes"] == []


def test_default_build_writes_the_isolated_runtime_directory(dbt_project_dir: Path):
    """#1014: with no directory named, a build isolates by default."""

    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(dbt_project_dir, target="dev", confirmed=True, runner=runner)

    key = dbt_runtime_key()
    argv = seen["argv"]
    written = Path(argv[argv.index("--target-path") + 1])
    assert written == dbt_project_dir / "target" / "dex" / key
    assert summary["artifact_directory"] == f"target/dex/{key}"


def test_shared_policy_writes_the_project_target_path(dbt_project_dir: Path):
    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    summary, _cost = build(
        dbt_project_dir,
        target="dev",
        confirmed=True,
        runner=runner,
        artifact_policy="shared",
    )

    argv = seen["argv"]
    assert Path(argv[argv.index("--target-path") + 1]) == dbt_project_dir / "target"
    assert summary["artifact_directory"] == "target"


def test_shared_policy_refuses_a_collision_before_running(dbt_project_dir: Path):
    planted = _plant_directory_as_sql(dbt_project_dir / "target")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(IncompatibleArtifactsError, match="dbt clean"):
        build(
            dbt_project_dir,
            target="dev",
            confirmed=True,
            runner=runner,
            artifact_policy="shared",
        )
    assert calls == []
    assert (planted / "nested.sql").is_file()


def test_shared_policy_refuses_a_manifest_from_another_dbt_minor(
    dbt_project_dir: Path,
):
    shared = dbt_project_dir / "target"
    shared.mkdir()
    (shared / "manifest.json").write_text(
        json.dumps({"metadata": {"dbt_version": "1.10.11"}}), encoding="utf-8"
    )

    with pytest.raises(IncompatibleArtifactsError, match=r"1\.10\.11"):
        resolve_build_artifact_dir(dbt_project_dir, "shared", runtime_key="dbt-1.12.5")
    # The same minor is the same runtime family and is reused.
    assert (
        resolve_build_artifact_dir(dbt_project_dir, "shared", runtime_key="dbt-1.10.2")
        == shared
    )
    assert (shared / "manifest.json").is_file()


def test_the_isolated_directory_never_collides_with_another_runtime(
    dbt_project_dir: Path,
):
    """The #862 handoff: Dex's isolated build leaves the shared target alone,
    so the other runtime finds no directory where it expects a file."""

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        out = Path(argv[argv.index("--target-path") + 1])
        # What dbt 1.12 leaves behind for a snapshot.
        (out / "run" / "dex_test" / "snapshots" / "check_row.sql").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    build(dbt_project_dir, target="dev", confirmed=True, runner=runner)

    assert find_snapshot_path_collisions(dbt_project_dir / "target") == []
    assert not (dbt_project_dir / "target" / "run").exists()


def test_a_run_results_from_another_invocation_is_not_a_success(
    dbt_project_dir: Path,
):
    """dbt's JSON log names this run; results naming another one are foreign."""

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        out = Path(argv[argv.index("--target-path") + 1])
        _write_success(out, invocation_id="someone-else")
        log = json.dumps({"info": {"invocation_id": "this-run", "name": "MainReport"}})
        return subprocess.CompletedProcess(argv, 0, stdout=log + "\n", stderr="")

    summary, _cost = build(dbt_project_dir, target="dev", confirmed=True, runner=runner)

    assert summary["success"] is False
    assert summary["nodes"] == []
    assert "someone-else" in summary["messages"][0]


def test_readers_take_the_newest_of_dex_and_the_project_target_path(
    dbt_project_dir: Path, monkeypatch
):
    import os

    from exmergo_dex_core.dbt_project import manifest_file

    shared = dbt_project_dir / "target"
    isolated = shared / "dex" / "dbt-9.9.9"
    for directory in (shared, isolated):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
    os.utime(shared / "manifest.json", (1_000, 1_000))
    monkeypatch.setattr(
        "exmergo_dex_core.transform.artifact_runtime.dbt_runtime_key",
        lambda: "dbt-9.9.9",
    )

    assert manifest_file(dbt_project_dir) == isolated / "manifest.json"
    assert readable_artifact_dir(dbt_project_dir) == isolated

    # The user's own `dbt parse` after Dex's build is the newer answer.
    os.utime(shared / "manifest.json", None)
    os.utime(isolated / "manifest.json", (1_000, 1_000))
    assert manifest_file(dbt_project_dir) == shared / "manifest.json"


def test_target_path_resolves_like_dbt(dbt_project_dir: Path, monkeypatch):
    yml = dbt_project_dir / "dbt_project.yml"
    yml.write_text(yml.read_text() + "\ntarget-path: build_out\n", encoding="utf-8")
    assert shared_target_dir(dbt_project_dir) == dbt_project_dir / "build_out"

    monkeypatch.setenv("DBT_TARGET_PATH", "from_env")
    assert shared_target_dir(dbt_project_dir) == dbt_project_dir / "from_env"
    monkeypatch.delenv("DBT_TARGET_PATH")

    yml.write_text(
        yml.read_text().replace("build_out", "\"{{ env_var('X') }}\""),
        encoding="utf-8",
    )
    assert shared_target_dir(dbt_project_dir) == dbt_project_dir / "target"


def test_a_configured_target_path_isolates_beneath_it(dbt_project_dir: Path):
    yml = dbt_project_dir / "dbt_project.yml"
    yml.write_text(yml.read_text() + "\ntarget-path: build_out\n", encoding="utf-8")
    seen: dict[str, list[str]] = {}

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    build(dbt_project_dir, target="dev", confirmed=True, runner=runner)

    argv = seen["argv"]
    written = Path(argv[argv.index("--target-path") + 1])
    assert written.parent.parent == dbt_project_dir / "build_out"


def test_foreign_invocation_cannot_be_a_successful_completion(
    dbt_project_dir: Path,
):
    write_run_results(dbt_project_dir, [node("mart")], invocation_id="invocation-a")

    with pytest.raises(ForeignInvocationError, match="invocation-b"):
        build_evidence(
            dbt_project_dir,
            target="dev",
            expected_invocation_id="invocation-b",
        )

    evidence = build_evidence(
        dbt_project_dir,
        target="dev",
        expected_invocation_id="invocation-a",
    )
    assert evidence.outcome is BuildOutcome.VALIDATED
    assert evidence.invocation["invocation_id"] == "invocation-a"


def test_assert_same_invocation_allows_a_missing_side():
    assert_same_invocation(None, None)
    assert_same_invocation("invocation-a", None)
    assert_same_invocation(None, "invocation-a")
    assert_same_invocation("invocation-a", "invocation-a")
    with pytest.raises(ForeignInvocationError, match="invocation-b"):
        assert_same_invocation("invocation-a", "invocation-b")


def test_the_shared_policy_refusal_is_a_guard(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """Through the CLI: refused before dbt runs, as `reason: guard`."""

    import yaml

    from exmergo_dex_core.cli import main

    _plant_directory_as_sql(dbt_project_dir / "target")
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "duckdb",
                "duckdb": {"path": str(tmp_path / "dev.duckdb")},
                "dbt_project_dir": dbt_project_dir.name,
                "transform": {"artifacts": "shared"},
            }
        )
    )
    main(["--repo-root", str(tmp_path), "transform", "build", "--target", "dev"])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["reason"] == "guard"
    assert "dbt clean" in envelope["errors"][0]


def test_dbt_1_12_engine_spelling_of_the_target_path_wins(
    dbt_project_dir: Path, monkeypatch
):
    """dbt 1.12 reads DBT_ENGINE_TARGET_PATH before DBT_TARGET_PATH."""

    monkeypatch.setenv("DBT_TARGET_PATH", "old_name")
    monkeypatch.setenv("DBT_ENGINE_TARGET_PATH", "engine_name")
    assert shared_target_dir(dbt_project_dir) == dbt_project_dir / "engine_name"


def test_a_relative_project_path_reads_its_semantic_manifest(
    dbt_project_dir: Path, monkeypatch
):
    """The resolved path is absolute, so `project / path` cannot double it."""

    import os

    from exmergo_dex_core.dbt_project import _read_semantic_manifest

    target = dbt_project_dir / "target"
    target.mkdir()
    (target / "semantic_manifest.json").write_text(
        json.dumps({"semantic_models": [{"name": "orders"}]}), encoding="utf-8"
    )
    monkeypatch.chdir(dbt_project_dir.parent)
    relative = Path(os.path.relpath(dbt_project_dir))
    read = _read_semantic_manifest(relative)
    assert read is not None
    assert read[0]["semantic_models"][0]["name"] == "orders"


def test_isolated_dbt_children_drop_the_engine_spelling_too(monkeypatch):
    from exmergo_dex_core.transform.build import _child_environ

    monkeypatch.setenv("DBT_ENGINE_FULL_REFRESH", "true")
    monkeypatch.setenv("DBT_ENGINE_TARGET_PATH", "/elsewhere")
    monkeypatch.setenv("DBT_FULL_REFRESH", "true")
    env = _child_environ(isolated=True)
    assert "DBT_ENGINE_FULL_REFRESH" not in env
    assert "DBT_ENGINE_TARGET_PATH" not in env
    assert "DBT_FULL_REFRESH" not in env


def test_foreign_run_results_yield_no_evidence_through_the_cli(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The summary refuses foreign results, and the evidence must not then
    report them as this build's `validated` outcome."""

    import importlib

    import yaml

    from exmergo_dex_core.cli import main

    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def fake(timeout, cwd, env=None):
        def run(argv):
            if argv[1] == "build":
                out = Path(argv[argv.index("--target-path") + 1])
                _write_success(out, invocation_id="someone-else")
            log = json.dumps({"info": {"invocation_id": "this-run", "name": "Main"}})
            return subprocess.CompletedProcess(argv, 0, stdout=log + "\n", stderr="")

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "duckdb",
                "duckdb": {"path": str(tmp_path / "dev.duckdb")},
                "dbt_project_dir": dbt_project_dir.name,
            }
        )
    )
    main(["--repo-root", str(tmp_path), "transform", "build", "--target", "dev"])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "error"
    assert envelope["data"]["success"] is False
    assert envelope["data"].get("outcome") != "validated"
    assert "evidence" not in envelope["data"] or envelope["data"]["evidence"] is None
    assert "logged_invocation_id" not in envelope["data"]
