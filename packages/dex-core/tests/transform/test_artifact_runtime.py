"""Artifact-producer metadata and cross-runtime collision fixture (#862)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.transform.test_build_evidence import (
    node,
    write_manifest_nodes,
    write_run_results,
)

from exmergo_dex_core.transform.artifact_runtime import (
    ArtifactCollisionError,
    find_snapshot_path_collisions,
    resolve_artifact_dir,
    runtime_artifact_dir,
    sanitize_artifact_directory,
)
from exmergo_dex_core.transform.evidence import build_evidence


def test_sanitize_prefers_path_relative_to_project(tmp_path: Path):
    project = tmp_path / "proj"
    target = project / "target"
    target.mkdir(parents=True)
    assert sanitize_artifact_directory(target, project_root=project) == "target"


def test_sanitize_refuses_credential_like_segments(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    bad = project / "secrets" / "target"
    bad.mkdir(parents=True)
    with pytest.raises(ValueError, match="not publishable"):
        sanitize_artifact_directory(bad, project_root=project)


def _plant_directory_as_sql(shared: Path) -> Path:
    """Synthetic collision: snapshot compiled path is a directory, not a file."""
    snap = shared / "run" / "demo" / "snapshots" / "check_row.sql"
    snap.mkdir(parents=True)
    (snap / "nested.sql").write_text("select 1\n", encoding="utf-8")
    return snap


def test_shared_directory_collision_is_detected(tmp_path: Path):
    shared = tmp_path / "target"
    planted = _plant_directory_as_sql(shared)
    found = find_snapshot_path_collisions(shared)
    assert len(found) == 1
    assert found[0].kind == "directory_where_file_expected"
    assert found[0].path.endswith("check_row.sql")
    assert planted.is_dir()


def test_same_runtime_reuse_when_clean(tmp_path: Path):
    shared = tmp_path / "target"
    shared.mkdir()
    (shared / "run_results.json").write_text("{}", encoding="utf-8")
    chosen = resolve_artifact_dir(shared, "dbt-1.10.11")
    assert chosen == shared


def test_collision_isolates_per_runtime_without_deleting(tmp_path: Path):
    shared = tmp_path / "target"
    planted = _plant_directory_as_sql(shared)
    chosen = resolve_artifact_dir(shared, "dbt-1.10.11")
    assert chosen == runtime_artifact_dir(shared, "dbt-1.10.11")
    assert chosen == shared / "dex" / "dbt-1.10.11"
    assert chosen.is_dir()
    assert planted.is_dir()


def test_collision_can_refuse_instead_of_isolating(tmp_path: Path):
    shared = tmp_path / "target"
    _plant_directory_as_sql(shared)
    with pytest.raises(ArtifactCollisionError, match="isolated"):
        resolve_artifact_dir(shared, "dbt-1.10.11", isolate_on_collision=False)


def test_build_evidence_includes_artifact_producer(dbt_project_dir: Path):
    project = dbt_project_dir
    (project / "models" / "staging" / "mart.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    write_run_results(project, [node("mart")], adapter_version="1.10.11")
    write_manifest_nodes(
        project,
        {
            "model.dex_test.mart": {
                "name": "mart",
                "resource_type": "model",
                "relation_name": '"dev"."main"."mart"',
                "config": {"materialized": "view"},
            }
        },
    )
    manifest_path = project / "target" / "manifest.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc.setdefault("metadata", {})["project_name"] = "dex_test"
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")

    evidence = build_evidence(project, target="dev", select="mart")
    producer = evidence.invocation["artifact_producer"]
    assert producer["dbt_version"] == "1.11.11"
    assert producer["adapter_version"] == "1.10.11"
    assert producer["target"] == "dev"
    assert producer["artifact_directory"] == "target"
    assert producer["invocation_id"] == "abc-123"
    assert producer["project"] == "dex_test"
