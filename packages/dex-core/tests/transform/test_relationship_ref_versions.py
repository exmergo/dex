"""YAML-mode relationship refs must keep version labels (#1125)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.dbt_project import definitions


def _write_versioned_project(root: Path, *, to: str) -> None:
    models = root / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "dim_teams.sql").write_text("select 1 as team_id\n", encoding="utf-8")
    (models / "dim_teams_v2.sql").write_text("select 1 as team_id\n", encoding="utf-8")
    (models / "fct_games.sql").write_text(
        "select 1 as game_id, 1 as team_id\n", encoding="utf-8"
    )
    (models / "teams.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: dim_teams\n"
        "    latest_version: 2\n"
        "    versions:\n"
        "      - v: 1\n"
        "        defined_in: dim_teams\n"
        "      - v: 2\n"
        "        defined_in: dim_teams_v2\n"
        "  - name: fct_games\n"
        "    columns:\n"
        "      - name: team_id\n"
        "        tests:\n"
        "          - relationships:\n"
        f"              to: {to}\n"
        "              field: team_id\n",
        encoding="utf-8",
    )


def _write_matching_manifest(root: Path, *, to: str) -> None:
    target = root / "target"
    target.mkdir(exist_ok=True)
    nodes = {
        "model.dex_test.fct_games": {
            "resource_type": "model",
            "name": "fct_games",
            "relation_name": '"dev"."main"."fct_games"',
            "config": {"enabled": True},
        },
        "model.dex_test.dim_teams.v1": {
            "resource_type": "model",
            "name": "dim_teams",
            "relation_name": '"dev"."main"."dim_teams_v1"',
            "config": {"enabled": True},
            "version": 1,
            "latest_version": 2,
        },
        "model.dex_test.dim_teams.v2": {
            "resource_type": "model",
            "name": "dim_teams",
            "relation_name": '"dev"."main"."dim_teams_v2"',
            "config": {"enabled": True},
            "version": 2,
            "latest_version": 2,
        },
        "test.dex_test.relationships_fct_games.bbb": {
            "resource_type": "test",
            "name": "relationships_fct_games_team_id",
            "test_metadata": {
                "name": "relationships",
                "kwargs": {
                    "column_name": "team_id",
                    "to": to,
                    "field": "team_id",
                },
            },
            "attached_node": "model.dex_test.fct_games",
            "depends_on": {
                "nodes": [
                    "model.dex_test.dim_teams.v2",
                    "model.dex_test.fct_games",
                ]
            },
        },
    }
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes, "sources": {}}), encoding="utf-8"
    )


def test_yaml_relationship_ref_keeps_version_label(dbt_project_dir: Path):
    _write_versioned_project(dbt_project_dir, to="ref('dim_teams', v=1)")
    defs = definitions(dbt_project_dir)
    assert defs.relationship_source == "yaml"
    (fk,) = defs.foreign_keys
    assert (fk.model, fk.to_model, fk.to_column) == (
        "fct_games",
        "dim_teams.v1",
        "team_id",
    )


def test_yaml_relationship_bare_ref_uses_latest_version(dbt_project_dir: Path):
    _write_versioned_project(dbt_project_dir, to="ref('dim_teams')")
    (fk,) = definitions(dbt_project_dir).foreign_keys
    assert fk.to_model == "dim_teams.v2"


@pytest.mark.parametrize(
    "to",
    [
        "ref('dim_teams', v=2)",
        "ref('dim_teams', version=2)",
        'ref("dim_teams", v=2)',
    ],
)
def test_yaml_and_manifest_agree_on_versioned_relationship_edge(
    dbt_project_dir: Path, to: str
):
    _write_versioned_project(dbt_project_dir, to=to)
    yaml_fk = definitions(dbt_project_dir).foreign_keys[0]
    assert yaml_fk.to_model == "dim_teams.v2"

    _write_matching_manifest(dbt_project_dir, to=to)
    manifest_fk = definitions(dbt_project_dir).foreign_keys[0]
    assert manifest_fk.to_model == yaml_fk.to_model == "dim_teams.v2"
