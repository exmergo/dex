"""An override of a package model the package documents carries no declaration (#1161).

dbt applies a package's YAML entry for a model the project disables to the
project's override of it: the package's own lookup misses its disabled node and
falls back to a lookup in any package. A project entry for the same name is then
a second entry, and dbt's parse refuses the pair. Dex says so before dbt runs,
naming both files and the route, and stops asking for a declaration it would
have to refuse. Real dbt on DuckDB, with the package installed from a local git
repository as in `test_package_override_introspection.py`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytest.importorskip("dbt.adapters.duckdb")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

PACKAGE_ENTRY = "dbt_packages/tiny_pkg/models/pkg_task.yml"
PACKAGE_DESCRIPTION = "One row per task, from the vendor."

# The package's own model: a TIMESTAMP coalesced with an INTEGER fails to build.
BROKEN = (
    "select id, coalesce(created_at, legacy_ts) as created_ts\n"
    "from {{ ref('raw_task') }}\n"
)
OVERRIDE = (
    "select id, coalesce(created_at, cast(cast(legacy_ts as varchar) as timestamp))"
    " as created_ts\nfrom {{ ref('raw_task') }}\n"
)
DECLARATION = {
    "model": "pkg_task",
    "description": "One row per task.",
    "grain": ["id"],
    "columns": {"id": {"role": "key"}},
}


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            *argv,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_project(tmp_path: Path, *, documented: bool) -> Path:
    package = tmp_path / "tiny_pkg"
    (package / "models").mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        "name: tiny_pkg\nversion: '1.0'\nconfig-version: 2\nmodel-paths: [models]\n"
    )
    (package / "models" / "pkg_task.sql").write_text(BROKEN)
    if documented:
        (package / "models" / "pkg_task.yml").write_text(
            "version: 2\nmodels:\n  - name: pkg_task\n"
            f"    description: {PACKAGE_DESCRIPTION}\n"
            "    columns:\n      - name: id\n        tests: [not_null]\n"
        )
    _git(package, "init", "-q", ".")
    _git(package, "add", "-A")
    _git(package, "commit", "-qm", "init")

    root = tmp_path / "proj"
    (root / "models").mkdir(parents=True)
    (root / "seeds").mkdir()
    (root / ".dex").mkdir()
    (root / ".git").mkdir()
    (root / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": "proj",
                "version": "1.0",
                "config-version": 2,
                "profile": "proj",
                "model-paths": ["models"],
                "seed-paths": ["seeds"],
                "seeds": {
                    "proj": {
                        "raw_task": {
                            "+column_types": {
                                "id": "integer",
                                "created_at": "timestamp",
                                "legacy_ts": "integer",
                            }
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    (root / "profiles.yml").write_text(
        "proj:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ./dev.duckdb\n"
    )
    (root / "packages.yml").write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "git": f"file://{package}",
                        "revision": _git(package, "rev-parse", "HEAD"),
                    }
                ]
            }
        )
    )
    (root / "seeds" / "raw_task.csv").write_text(
        "id,created_at,legacy_ts\n1,2024-01-01 00:00:00,\n2,,\n3,2024-02-01 00:00:00,\n"
    )
    (root / "models" / "rpt.sql").write_text(
        "select id, created_ts from {{ ref('pkg_task') }}\n"
    )
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nduckdb:\n  path: dev.duckdb\n"
    )
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = _make_project(tmp_path, documented=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("DEX_ALLOW_UNSERIALIZED_LEDGER", "1")
    return root


@pytest.fixture
def undocumented(tmp_path, monkeypatch):
    root = _make_project(tmp_path, documented=False)
    monkeypatch.chdir(root)
    monkeypatch.setenv("DEX_ALLOW_UNSERIALIZED_LEDGER", "1")
    return root


def _dex(capsys, *argv: str) -> dict:
    main(list(argv))
    return json.loads(capsys.readouterr().out)


def _override_edits(root: Path, **extra) -> Path:
    config = yaml.safe_load((root / "dbt_project.yml").read_text())
    config["models"] = {"tiny_pkg": {"pkg_task": {"+enabled": False}}}
    payload = {
        "edits": [
            {"path": "models/pkg_task.sql", "kind": "model_sql", "content": OVERRIDE},
            {
                "path": "dbt_project.yml",
                "kind": "project_yml",
                "content": yaml.safe_dump(config, sort_keys=False),
            },
            *extra.pop("edits", []),
        ],
        **extra,
    }
    path = root / "edits.json"
    path.write_text(json.dumps(payload))
    return path


def _plan(capsys, edits: Path) -> dict:
    return _dex(
        capsys, "transform", "plan", "Override pkg_task", "--edits-file", str(edits)
    )


def test_the_failed_build_names_the_package_entry_on_the_override_route(
    project, capsys
):
    failed = _dex(capsys, "transform", "build", "--target", "dev")
    assert failed["status"] == "error"
    [entry] = failed["data"]["prerequisites"]
    [override] = [o for o in entry["options"] if o["kind"] == "project_override"]
    assert override["supported"] is True
    assert override["package_entry"] == PACKAGE_ENTRY
    assert PACKAGE_ENTRY in override["how"]
    assert "no YAML entry or declaration of its own" in override["how"]


def test_declaring_the_override_is_refused_naming_both_files(project, capsys):
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    planned = _plan(capsys, _override_edits(project, declarations=[DECLARATION]))
    assert planned["status"] == "error"
    [message] = planned["errors"]
    assert PACKAGE_ENTRY in message
    assert "models/pkg_task.yml" in message
    assert "declare the models that read it" in message
    # Dex's words, before dbt runs: no parse message stands in for it.
    assert "dbt parse failed" not in message
    assert not (planned.get("data") or {}).get("plan_id")


def test_a_hand_written_entry_for_the_override_is_refused_the_same_way(project, capsys):
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    entry = {
        "path": "models/overrides.yml",
        "kind": "schema_yml",
        "content": "version: 2\nmodels:\n  - name: pkg_task\n    description: Mine.\n",
    }
    planned = _plan(capsys, _override_edits(project, edits=[entry]))
    assert planned["status"] == "error"
    [message] = planned["errors"]
    assert PACKAGE_ENTRY in message
    assert "models/overrides.yml" in message


def test_the_undeclared_override_plans_builds_and_keeps_the_package_entry(
    project, capsys
):
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    planned = _plan(capsys, _override_edits(project))
    assert planned["status"] == "ok", planned["errors"]
    assert not any(
        "'pkg_task'" in w and "declares nothing" in w for w in planned["warnings"]
    ), planned["warnings"]
    assert not any(w.startswith("'pkg_task' declares no") for w in planned["warnings"])
    drafted = planned["data"]["declarations"]["pkg_task"]
    assert drafted["missing"] == []
    assert drafted["package_documented"] == {
        "package": "tiny_pkg",
        "package_entry": PACKAGE_ENTRY,
    }
    plan_id = planned["data"]["plan_id"]
    assert _dex(capsys, "transform", "apply", plan_id)["status"] == "ok"

    built = _dex(
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--cold-deps",
        "--for-plan",
        plan_id,
        "--verify",
    )
    assert built["status"] == "ok", built["errors"]
    manifest = json.loads(
        (Path(built["data"]["artifact_directory"]) / "manifest.json").read_text()
    )
    node = manifest["nodes"]["model.proj.pkg_task"]
    # dbt gave the package's entry to the project's override.
    assert node["description"] == PACKAGE_DESCRIPTION
    assert node["patch_path"] == "tiny_pkg://models/pkg_task.yml"

    reader = project / "reader.json"
    reader.write_text(
        json.dumps(
            {
                "edits": [],
                "declarations": [
                    {
                        "model": "rpt",
                        "description": "One row per task.",
                        "grain": ["id"],
                        "columns": {"id": {"role": "key"}},
                    }
                ],
            }
        )
    )
    declared = _dex(
        capsys, "transform", "plan", "Declare rpt", "--edits-file", str(reader)
    )
    assert declared["status"] == "ok", declared["errors"]

    skipped = _dex(capsys, "transform", "declare", "pkg_task")
    assert skipped["status"] == "ok", skipped["errors"]
    [entry] = skipped["data"]["skipped"]
    assert entry["reason"] == "package_documented"
    assert PACKAGE_ENTRY in entry["detail"]


def test_an_override_the_package_does_not_document_still_takes_a_declaration(
    undocumented, capsys
):
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    planned = _plan(capsys, _override_edits(undocumented, declarations=[DECLARATION]))
    assert planned["status"] == "ok", planned["errors"]
    assert "models/pkg_task.yml" in planned["data"]["paths"]
    assert "package_documented" not in planned["data"]["declarations"]["pkg_task"]
