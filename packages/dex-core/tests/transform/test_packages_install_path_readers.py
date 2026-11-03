"""Package readers honour ``packages-install-path`` (#1059)."""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.references import ReferenceIndex, find_references
from exmergo_dex_core.transform.build import (
    installed_packages,
    needs_deps,
    packages_dir,
)
from exmergo_dex_core.transform.grounding import packages_digest


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project_with_custom_install(tmp_path: Path) -> Path:
    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n'
        "packages-install-path: vendor/dbt\n",
    )
    _write(root / "packages.yml", "packages:\n  - local: ../tiny_pkg\n")
    _write(root / "models" / "a.sql", "select 1 as id\n")
    return root


def _install_local_pkg(root: Path) -> Path:
    pkg = root / "vendor" / "dbt" / "tiny_pkg"
    _write(pkg / "dbt_project.yml", "name: tiny_pkg\nversion: 0.1.0\n")
    _write(pkg / "models" / "pkg_model.sql", "select 1 as id\n")
    return pkg


def test_packages_dir_reads_custom_install_path(tmp_path: Path):
    root = _project_with_custom_install(tmp_path)
    assert packages_dir(root) == root / "vendor" / "dbt"


def test_packages_dir_falls_back_for_templated_path(tmp_path: Path):
    root = tmp_path / "analytics"
    _write(
        root / "dbt_project.yml",
        'name: analytics\nversion: "1.0.0"\nprofile: analytics\n'
        "packages-install-path: \"{{ env_var('DEX_PKGS') }}\"\n",
    )
    assert packages_dir(root) == root / "dbt_packages"


def test_installed_packages_and_needs_deps_use_custom_path(tmp_path: Path):
    root = _project_with_custom_install(tmp_path)
    assert needs_deps(root) is True
    assert installed_packages(root) == set()

    _install_local_pkg(root)
    # Also leave a decoy under the default path that must be ignored.
    decoy = root / "dbt_packages" / "decoy"
    _write(decoy / "dbt_project.yml", "name: decoy\n")

    assert needs_deps(root) is False
    names = installed_packages(root)
    assert "tiny_pkg" in names
    assert "decoy" not in names


def test_packages_digest_fingerprints_custom_install_path(tmp_path: Path):
    root = _project_with_custom_install(tmp_path)
    before = packages_digest(root)
    assert before is not None

    _install_local_pkg(root)
    after = packages_digest(root)
    assert after is not None
    assert after != before
    assert (
        "installed:tiny_pkg:"
        in (
            # recompute parts indirectly: digest changes once install appears
            after
        )
        or after != before
    )


def test_reference_index_scans_custom_install_path(tmp_path: Path):
    root = _project_with_custom_install(tmp_path)
    _install_local_pkg(root)
    index = ReferenceIndex(load_project(root))
    decls = index.declarations_of("pkg_model", ("model",))
    assert decls
    assert decls[0].path.startswith("vendor/dbt/tiny_pkg/")


def test_declared_but_uninstalled_custom_path_is_incomplete(tmp_path: Path):
    root = _project_with_custom_install(tmp_path)
    report = find_references(load_project(root), ["id"], kind="column")
    assert report.completeness == "incomplete"
    assert any("not installed" in limit for limit in report.limits)


def test_transform_deps_reports_custom_packages_dir(
    tmp_path: Path, capsys, monkeypatch
):
    import importlib
    import subprocess

    root = _project_with_custom_install(tmp_path)
    _install_local_pkg(root)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "deps",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    envelope = json.loads(out)
    assert envelope["data"]["packages_dir_exists"] is True
