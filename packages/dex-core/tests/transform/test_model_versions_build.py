"""Native dbt model versions end to end, on real dbt and DuckDB (#859, #895).

The issue's own specification: ``dim_teams`` exposes ``team_id`` and
``team_label``; v2 renames ``team_label`` to ``display_label``, and v1 stays the
default until a separate promotion. Every expectation below is written by hand
and read back from somewhere the fields under test do not come from: DuckDB's
own catalog for columns, dbt's compiled SQL for what a consumer reads, and
dbt's manifest for node ids.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = pytest.mark.live_open_adapter

STG = "select 1 as team_id, 'Alpha' as team_label\nunion all select 2, 'Beta'\n"
DIM = "select team_id, team_label from {{ ref('stg_teams') }}\n"
DIM_V2 = "select team_id, team_label as display_label from {{ ref('stg_teams') }}\n"
RPT = "select team_id from {{ ref('dim_teams') }}\n"
RPT_V2 = "select team_id, display_label from {{ ref('dim_teams', v=2) }}\n"

PLAIN_YML = """\
version: 2
models:
  - name: dim_teams
    description: One row per team.
    columns:
      - name: team_id
        tests: [unique, not_null]
      - name: team_label
"""

VERSIONED_YML = """\
version: 2
models:
  - name: dim_teams
    description: One row per team.
    latest_version: 1
    columns:
      - name: team_id
        tests: [unique, not_null]
      - name: team_label
    versions:
      - v: 1
        defined_in: dim_teams
        config:
          alias: dim_teams
      - v: 2
        columns:
          - include: all
            exclude: [team_label]
          - name: display_label
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(repo: Path, capsys, *argv: str) -> dict:
    capsys.readouterr()
    main(["--repo-root", str(repo), *argv])
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def repo(tmp_path: Path, capsys) -> Path:
    pytest.importorskip("dbt.adapters.duckdb")
    _write(
        tmp_path / ".dex/config.yml",
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        "dbt_project_dir: shop\ndbt_target: dev\n",
    )
    shop = tmp_path / "shop"
    _write(
        shop / "dbt_project.yml",
        'name: shop\nversion: "1.0"\nprofile: shop\nmodel-paths: [models]\n'
        "models:\n  +materialized: table\n",
    )
    _write(
        shop / "profiles.yml",
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
    )
    _write(shop / "models/staging/stg_teams.sql", STG)
    _write(shop / "models/marts/dim_teams.sql", DIM)
    _write(shop / "models/marts/rpt_teams.sql", RPT)
    _write(shop / "models/marts/_marts.yml", PLAIN_YML)
    built = _run(tmp_path, capsys, "transform", "build", "--target", "dev")
    assert built["status"] == "ok", built
    return tmp_path


def _plan(repo: Path, capsys, intent: str, payload: dict) -> dict:
    path = repo / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return _run(repo, capsys, "transform", "plan", intent, "--edits-file", str(path))


def _columns(repo: Path, relation: str) -> list[str]:
    import duckdb

    with duckdb.connect(str(repo / "dev.duckdb"), read_only=True) as con:
        return [r[0] for r in con.execute(f"describe {relation}").fetchall()]


def _tables(repo: Path) -> set[str]:
    import duckdb

    with duckdb.connect(str(repo / "dev.duckdb"), read_only=True) as con:
        return {
            r[0]
            for r in con.execute(
                "select table_name from information_schema.tables"
            ).fetchall()
        }


def _manifest(repo: Path) -> dict:
    found = sorted(
        (repo / "shop/target").rglob("manifest.json"), key=lambda p: p.stat().st_mtime
    )
    return json.loads(found[-1].read_text())


def _compiled(repo: Path, model: str) -> str:
    found = sorted(
        (repo / "shop/target").rglob(f"compiled/shop/models/marts/{model}.sql"),
        key=lambda p: p.stat().st_mtime,
    )
    return found[-1].read_text()


def _initial_payload(**declarations) -> dict:
    return {
        "edits": [
            {
                "path": "models/marts/dim_teams_v2.sql",
                "kind": "model_sql",
                "content": DIM_V2,
            },
            {
                "path": "models/marts/rpt_display.sql",
                "kind": "model_sql",
                "content": RPT_V2,
            },
            {
                "path": "models/marts/_marts.yml",
                "kind": "schema_yml",
                "content": VERSIONED_YML,
            },
        ],
        **declarations,
    }


def _build(repo: Path, capsys, plan_id: str) -> dict:
    return _run(
        repo,
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan",
        plan_id,
    )


def _versions_of(envelope: dict) -> dict[tuple[str, str], dict]:
    return {
        (r["model"], r["check"]): r
        for r in envelope["data"]["verification"]["model_versions"]
    }


def test_initial_versioning_then_a_separate_promotion(repo: Path, capsys):
    planned = _plan(
        repo,
        capsys,
        "v2 of dim_teams with display_label; v1 stays the default",
        _initial_payload(
            declarations=[
                {
                    "model": "dim_teams.v2",
                    "description": "One row per team, its label renamed.",
                    "grain": ["team_id"],
                    "columns": {
                        "team_id": {"role": "key"},
                        "display_label": {"role": "dimension"},
                    },
                }
            ]
        ),
    )
    assert planned["status"] == "ok", planned
    report = planned["data"]["model_versions"]["dim_teams"]
    # dbt's parse of the post-change project confirms both nodes and both refs.
    assert report["manifest"] == {"status": "confirmed"}
    assert [v["unique_id"] for v in report["versions"]] == [
        "model.shop.dim_teams.v1",
        "model.shop.dim_teams.v2",
    ]
    assert report["default"]["after"] == "dim_teams.v1"
    assert report["relation_changes"] == []

    applied = _run(repo, capsys, "transform", "apply", planned["data"]["plan_id"])
    assert applied["status"] == "ok", applied

    # A cold build: a fresh dev database holds nothing from the fixture's build.
    (repo / "dev.duckdb").unlink()
    shutil.rmtree(repo / "shop/target", ignore_errors=True)
    built = _build(repo, capsys, planned["data"]["plan_id"])
    assert built["status"] == "ok", built
    assert built["data"]["outcome"] == "validated"
    completion = built["data"]["completion"]
    assert set(completion["deliverables"]["built"]["names"]) == {
        "dim_teams.v1",
        "dim_teams.v2",
        "rpt_display",
    }
    records = _versions_of(built)
    assert {k: r["status"] for k, r in records.items()} == {
        ("dim_teams.v1", "version"): "satisfied",
        ("dim_teams.v2", "version"): "satisfied",
        ("dim_teams", "default"): "satisfied",
        ("rpt_teams", "ref"): "satisfied",
        ("rpt_display", "ref"): "satisfied",
    }

    # Ground truth from DuckDB and dbt's own artifacts.
    assert _columns(repo, "dim_teams") == ["team_id", "team_label"]
    assert _columns(repo, "dim_teams_v2") == ["team_id", "display_label"]
    assert "dim_teams_v1" not in _tables(repo)
    nodes = _manifest(repo)["nodes"]
    assert nodes["model.shop.rpt_teams"]["depends_on"]["nodes"] == [
        "model.shop.dim_teams.v1"
    ]
    assert nodes["model.shop.rpt_display"]["depends_on"]["nodes"] == [
        "model.shop.dim_teams.v2"
    ]
    assert '"dim_teams"' in _compiled(repo, "rpt_teams")
    assert '"dim_teams_v2"' in _compiled(repo, "rpt_display")
    v2 = nodes["model.shop.dim_teams.v2"]
    assert v2["config"]["meta"]["dex"]["grain"] == ["team_id"]
    assert v2["columns"]["team_id"]["meta"]["dex"]["role"] == "key"
    assert "dex" not in nodes["model.shop.dim_teams.v1"]["config"]["meta"]

    # Promotion is its own plan, and says which consumer it moves.
    promoted = yaml.safe_load((repo / "shop/models/marts/_marts.yml").read_text())
    text = (repo / "shop/models/marts/_marts.yml").read_text()
    assert promoted["models"][0]["latest_version"] == 1
    plan = _plan(
        repo,
        capsys,
        "promote dim_teams v2 to the default",
        {
            "edits": [
                {
                    "path": "models/marts/_marts.yml",
                    "kind": "schema_yml",
                    "content": text.replace("latest_version: 1", "latest_version: 2"),
                }
            ]
        },
    )
    assert plan["status"] == "ok", plan
    report = plan["data"]["model_versions"]["dim_teams"]
    assert report["default"] == {"before": "dim_teams.v1", "after": "dim_teams.v2"}
    (moved,) = [c for c in report["consumers"] if c["changed"]]
    assert (moved["model"], moved["after"]) == ("rpt_teams", "dim_teams.v2")
    assert any("(a promotion)" in w for w in plan["warnings"])
    _run(repo, capsys, "transform", "apply", plan["data"]["plan_id"])
    rebuilt = _build(repo, capsys, plan["data"]["plan_id"])
    assert rebuilt["status"] == "ok", rebuilt
    nodes = _manifest(repo)["nodes"]
    assert nodes["model.shop.rpt_teams"]["depends_on"]["nodes"] == [
        "model.shop.dim_teams.v2"
    ]
    assert '"dim_teams_v2"' in _compiled(repo, "rpt_teams")
    assert _versions_of(rebuilt)[("dim_teams", "default")]["expected"] == "dim_teams.v2"


def test_a_copy_is_warned_and_a_collision_refused(repo: Path, capsys):
    copy = _plan(
        repo,
        capsys,
        "dim_teams v2",
        {
            "edits": [
                {
                    "path": "models/marts/dim_teams_v2.sql",
                    "kind": "model_sql",
                    "content": DIM_V2,
                }
            ]
        },
    )
    assert copy["status"] == "ok", copy
    assert (
        copy["data"]["model_versions"]["independent_models"][0]["model"]
        == "dim_teams_v2"
    )
    assert any("not version 2 of 'dim_teams'" in w for w in copy["warnings"])

    collision = _initial_payload()
    collision["edits"][2]["content"] = VERSIONED_YML + "  - name: dim_teams_v2\n"
    refused = _plan(repo, capsys, "v2", collision)
    assert refused["status"] == "error"
    assert "version_file_collision" in json.dumps(refused["errors"])


def test_an_existing_versioned_model_gains_a_version_with_a_custom_file(
    repo: Path, capsys
):
    first = _plan(repo, capsys, "v2", _initial_payload())
    _run(repo, capsys, "transform", "apply", first["data"]["plan_id"])
    three = VERSIONED_YML + "      - v: 3\n        defined_in: teams_next\n"
    plan = _plan(
        repo,
        capsys,
        "a third version in teams_next.sql",
        {
            "edits": [
                {
                    "path": "models/marts/teams_next.sql",
                    "kind": "model_sql",
                    "content": DIM_V2,
                },
                {
                    "path": "models/marts/_marts.yml",
                    "kind": "schema_yml",
                    "content": three,
                },
            ],
            "declarations": [{"model": "dim_teams.v3", "grain": ["team_id"]}],
        },
    )
    assert plan["status"] == "ok", plan
    report = plan["data"]["model_versions"]["dim_teams"]
    assert report["manifest"] == {"status": "confirmed"}
    v3 = report["versions"][2]
    assert (v3["unique_id"], v3["file"]) == (
        "model.shop.dim_teams.v3",
        "models/marts/teams_next.sql",
    )
    assert report["default"] == {"before": "dim_teams.v1", "after": "dim_teams.v1"}
    _run(repo, capsys, "transform", "apply", plan["data"]["plan_id"])
    built = _build(repo, capsys, plan["data"]["plan_id"])
    assert built["status"] == "ok", built
    assert (
        "dim_teams.v3" in built["data"]["completion"]["deliverables"]["built"]["names"]
    )
    assert _columns(repo, "dim_teams_v3") == ["team_id", "display_label"]


def test_declare_and_maintain_read_each_version(repo: Path, capsys):
    first = _plan(repo, capsys, "v2", _initial_payload())
    _run(repo, capsys, "transform", "apply", first["data"]["plan_id"])
    assert _build(repo, capsys, first["data"]["plan_id"])["status"] == "ok"

    declared = _run(repo, capsys, "transform", "declare", "dim_teams.v2")
    assert declared["status"] == "ok", declared
    named = {p["model"] for p in declared["data"]["proposed"]} | {
        s["model"] for s in declared["data"]["skipped"]
    }
    assert named == {"dim_teams.v2"}
    assert all(s["reason"] != "versioned" for s in declared["data"]["skipped"])

    # A declaration on v2, baselined, then changed: drift names v2.
    plan = _plan(
        repo,
        capsys,
        "declare v2",
        {
            "declarations": [
                {"model": "dim_teams.v2", "description": "Teams, relabelled."}
            ]
        },
    )
    _run(repo, capsys, "transform", "apply", plan["data"]["plan_id"])
    snapshot = _run(repo, capsys, "maintain", "snapshot")
    assert snapshot["status"] == "ok", snapshot
    yml = repo / "shop/models/marts/_marts.yml"
    yml.write_text(yml.read_text().replace("Teams, relabelled.", "Teams, renamed."))
    drift = _run(repo, capsys, "maintain", "semantic")
    changed = [
        f for f in drift["data"]["findings"] if f["code"] == "declaration_changed"
    ]
    assert [(f["identifier"], f["data"]["field"]) for f in changed] == [
        ("dim_teams.v2", "description")
    ]
