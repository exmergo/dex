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


# --- #1166: the package readers take the post-edit tree -------------------------


def _edit(path: str, content: str | None):
    from exmergo_dex_core.dbt_project import Edit, EditOp

    if content is None:
        return Edit(path=path, op=EditOp.DELETE, new_content=None)
    return Edit(path=path, op=EditOp.UPSERT, new_content=content)


def test_package_overlay_keeps_only_the_root_package_files():
    from exmergo_dex_core.transform.build import package_overlay

    overlay = package_overlay(
        [
            _edit("packages.yml", "packages: []\n"),
            _edit("dependencies.yml", None),
            _edit("models/packages.yml", "packages: []\n"),
            _edit("models/a.sql", "select 1\n"),
        ]
    )
    assert overlay == {"packages.yml": "packages: []\n", "dependencies.yml": None}


def test_the_readers_see_the_overlay_in_place_of_the_disk(tmp_path: Path):
    from exmergo_dex_core.transform.build import (
        declared_packages,
        has_package_shortfall,
        missing_packages,
        needs_deps,
        package_install_dir,
    )

    (tmp_path / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    (tmp_path / "packages.yml").write_text(
        "packages:\n  - local: ../a\n", encoding="utf-8"
    )
    (tmp_path / "dbt_packages" / "a").mkdir(parents=True)
    overlay = {
        "packages.yml": "packages:\n  - local: ../a\n  - git: https://x/y.git\n",
        "dbt_project.yml": "name: p\npackages-install-path: vendor\n",
    }
    assert [p.name for p in declared_packages(tmp_path)] == ["../a"]
    assert [p.name for p in declared_packages(tmp_path, overlay=overlay)] == [
        "../a",
        "https://x/y.git",
    ]
    assert package_install_dir(tmp_path, overlay=overlay) == tmp_path / "vendor"
    # Nothing installed under the overlaid path: every declaration is missing.
    assert needs_deps(tmp_path, overlay=overlay)
    assert len(missing_packages(tmp_path, overlay=overlay)) == 2
    # Without the moved install path, the git package is a count shortfall.
    assert has_package_shortfall(
        tmp_path, overlay={"packages.yml": overlay["packages.yml"]}
    )
    assert not has_package_shortfall(tmp_path)
    # A deleted spec declares nothing.
    assert declared_packages(tmp_path, overlay={"packages.yml": None}) == []


def test_a_plan_adding_a_git_package_is_refused_on_the_count(tmp_path: Path):
    from exmergo_dex_core.transform.build import (
        PlanDeclaresMissingPackagesError,
        assert_plan_packages_installed,
    )

    (tmp_path / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    (tmp_path / "packages.yml").write_text(
        "packages:\n  - local: ../a\n", encoding="utf-8"
    )
    (tmp_path / "dbt_packages" / "a").mkdir(parents=True)
    assert_plan_packages_installed(tmp_path, [])
    with pytest.raises(PlanDeclaresMissingPackagesError) as caught:
        assert_plan_packages_installed(
            tmp_path,
            [
                _edit(
                    "packages.yml",
                    "packages:\n  - local: ../a\n"
                    "  - git: https://token@github.com/o/r.git\n",
                )
            ],
        )
    message = str(caught.value)
    assert "https://github.com/o/r.git" in message
    assert "token" not in message
    assert "transform deps" in message


def test_a_plan_adding_hub_and_git_packages_names_both(tmp_path: Path):
    from exmergo_dex_core.transform.build import (
        PlanDeclaresMissingPackagesError,
        assert_plan_packages_installed,
    )

    (tmp_path / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    with pytest.raises(PlanDeclaresMissingPackagesError) as caught:
        assert_plan_packages_installed(
            tmp_path,
            [
                _edit(
                    "packages.yml",
                    "packages:\n  - package: dbt-labs/dbt_utils\n"
                    "  - git: https://github.com/o/r.git\n",
                )
            ],
        )
    assert [p.name for p in caught.value.declared_by_plan] == [
        "dbt-labs/dbt_utils",
        "https://github.com/o/r.git",
    ]


def test_an_unverifiable_addition_is_not_blamed_for_an_existing_absence(
    tmp_path: Path,
):
    from exmergo_dex_core.transform.build import (
        MissingPackagesError,
        PlanDeclaresMissingPackagesError,
        assert_plan_packages_installed,
    )

    (tmp_path / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
    (tmp_path / "packages.yml").write_text(
        "packages:\n  - local: ../a\n  - local: ../b\n", encoding="utf-8"
    )
    (tmp_path / "dbt_packages" / "a").mkdir(parents=True)
    with pytest.raises(MissingPackagesError) as caught:
        assert_plan_packages_installed(
            tmp_path,
            [
                _edit(
                    "packages.yml",
                    "packages:\n  - local: ../a\n  - local: ../b\n"
                    "  - git: https://github.com/o/r.git\n",
                )
            ],
        )
    assert not isinstance(caught.value, PlanDeclaresMissingPackagesError)
    assert "../b" in str(caught.value)
    assert "a plan installs nothing" in str(caught.value)
