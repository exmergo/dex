"""Manifest paths on Windows: dbt writes the platform separator, plans write "/".

The weekly Windows canary is the only run that sees a real Windows manifest, so
these tests feed the engine one by hand and hold it to the spelling a plan uses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from exmergo_dex_core.manifest_paths import node_file, patch_file, posix
from exmergo_dex_core.transform import compiled_tree, downstream_impact, prerequisites


def test_posix_reads_a_backslash_as_a_separator():
    assert posix("models\\staging\\stg_orders.sql") == "models/staging/stg_orders.sql"
    assert posix("models/staging/stg_orders.sql") == "models/staging/stg_orders.sql"
    assert posix(None) == ""
    assert node_file({"original_file_path": "models\\a.sql"}) == "models/a.sql"
    assert node_file({}) == ""


def test_patch_file_drops_the_package_and_normalizes_the_rest():
    assert patch_file("tiny_pkg://models\\pkg_task.yml") == "models/pkg_task.yml"
    assert patch_file("models/x.yml") == "models/x.yml"
    assert patch_file(None) == ""


def test_a_package_entry_reads_with_slashes():
    node = {"patch_path": "tiny_pkg://models\\pkg_task.yml"}
    entry = prerequisites._package_entry(node, "dbt_packages")
    assert entry == "dbt_packages/tiny_pkg/models/pkg_task.yml"
    assert prerequisites._package_entry(node, None) == "tiny_pkg://models/pkg_task.yml"


def test_a_windows_manifest_fills_the_branch_with_plan_paths():
    manifest = {
        "nodes": {
            "model.p.rpt": {
                "name": "rpt",
                "package_name": "p",
                "original_file_path": "models\\marts\\rpt.sql",
                "compiled_code": "select 1 as id",
                "depends_on": {"macros": ["macro.p.helper"], "nodes": []},
            },
            "model.p.broken": {
                "name": "broken",
                "package_name": "p",
                "original_file_path": "models\\broken.sql",
            },
        },
        "macros": {
            "macro.p.helper": {
                "original_file_path": "macros\\helper.sql",
                "depends_on": {"macros": []},
            }
        },
        "disabled": {
            "model.p.off": [
                {
                    "name": "off",
                    "package_name": "p",
                    "original_file_path": "models\\off.sql",
                }
            ]
        },
    }
    branch = compiled_tree.Branch()
    # The compile worker already keys a set-aside file by "/".
    compiled_tree.read_manifest(branch, manifest, "p", {"models/broken.sql": "boom"})
    assert branch.models["rpt"].path == "models/marts/rpt.sql"
    assert branch.failed == {"broken": "boom"}
    assert branch.unmatched_failed == []
    assert branch.disabled == {"off": "models/off.sql"}
    assert compiled_tree.macro_callers(branch, "p") == {"macros/helper.sql": ["rpt"]}


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_crlf_file_at_head_reads_as_the_project_reads_it(tmp_path: Path):
    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(tmp_path), "-c", "core.autocrlf=false", *args],  # noqa: S607
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_bytes(b"select 1 as id\r\n")
    git("add", "models/a.sql")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "crlf",
    )
    text = downstream_impact._git_head(tmp_path, ".", "models/a.sql")
    assert text == "select 1 as id\n"
