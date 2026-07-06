"""`dbt deps`: auto-run inside build when packages are missing, plus the
explicit `transform deps` refresh."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _fake_runner(monkeypatch, *, returncode: int, stdout: str = ""):
    """Replace _default_runner with a recorder returning a canned dbt result."""

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[dict] = []

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            calls.append({"argv": argv, "cwd": cwd})
            return subprocess.CompletedProcess(
                args=argv, returncode=returncode, stdout=stdout, stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    return calls


def _declare_packages(project: Path) -> None:
    (project / "packages.yml").write_text(
        "packages:\n  - local: ../pkg\n", encoding="utf-8"
    )


def test_build_runs_deps_before_build_when_packages_missing(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _declare_packages(dbt_project_dir)
    calls = _fake_runner(monkeypatch, returncode=0)
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
    assert [c["argv"][1] for c in calls] == ["deps", "build"]
    assert all(Path(c["cwd"]) == dbt_project_dir for c in calls)
    assert envelope["data"]["deps"] == {"ran": True, "success": True}


def test_build_skips_deps_when_dbt_packages_present(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _declare_packages(dbt_project_dir)
    installed = dbt_project_dir / "dbt_packages" / "pkg"
    installed.mkdir(parents=True)
    (installed / "dbt_project.yml").write_text("name: pkg\n", encoding="utf-8")
    calls = _fake_runner(monkeypatch, returncode=0)
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
    assert [c["argv"][1] for c in calls] == ["build"]
    assert "deps" not in envelope["data"]


def test_build_skips_deps_without_packages_yml(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    calls = _fake_runner(monkeypatch, returncode=0)
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
    assert [c["argv"][1] for c in calls] == ["build"]


def test_deps_failure_shortcircuits_build_with_actionable_error(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _declare_packages(dbt_project_dir)
    line = json.dumps(
        {"info": {"level": "error", "msg": "Package ../pkg was not found"}}
    )
    calls = _fake_runner(monkeypatch, returncode=1, stdout=line)
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
    assert rc == 1
    assert len(calls) == 1, "build must not run after a deps failure"
    assert envelope["errors"][0] == "dbt deps failed: Package ../pkg was not found"


def test_transform_deps_reports_noop_without_packages(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def exploded(timeout: float, cwd):
        def run(argv: list[str]):
            raise AssertionError(f"dbt was invoked with no packages declared: {argv}")

        return run

    monkeypatch.setattr(build_module, "_default_runner", exploded)
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "deps"], capsys)
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["ran"] is False
    assert "packages.yml" in envelope["data"]["reason"]


def test_transform_deps_installs_local_package_for_real(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("dbt.cli.main")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "dbt_project.yml").write_text(
        'name: tiny_pkg\nversion: "1.0.0"\n', encoding="utf-8"
    )
    # The local package is itself a dbt project, so discovery sees two; pin the
    # real one the way the error message tells the agent to.
    dex_dir = tmp_path / ".dex"
    dex_dir.mkdir()
    (dex_dir / "config.yml").write_text(
        f"dbt_project_dir: {dbt_project_dir.name}\n", encoding="utf-8"
    )
    _declare_packages(dbt_project_dir)
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "deps"], capsys)
    assert rc == 0, envelope
    assert envelope["data"]["ran"] is True
    assert envelope["data"]["success"] is True
    assert envelope["data"]["packages_dir_exists"] is True
    assert (dbt_project_dir / "dbt_packages").is_dir()
