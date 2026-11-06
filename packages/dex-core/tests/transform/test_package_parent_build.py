"""A package model as `population.parent`, from plan to `build --verify` (#1155).

A conversation-level mart over a package's staging models, the package building
into its own schema (`+schema: inbox`), as Fivetran's packages do. Driven from
the parts table, whose conversation ids include deleted conversations, the plan
names the driver as contradicting the declared package parent, and the build's
verification counts the parents missing and the keys unexpected. Driven from the
parent, both are clean. Real dbt on DuckDB, with the package installed from a
local git repository as in `test_package_documented_override.py`.
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

PARENT = "stg_inbox__conversations"
FROM_PARTS = (
    "select p.conversation_id, count(*) as part_count\n"
    "from {{ ref('stg_inbox__conversation_parts') }} as p\n"
    "group by p.conversation_id\n"
)
FROM_PARENT = (
    "select c.conversation_id, count(p.part_id) as part_count\n"
    "from {{ ref('stg_inbox__conversations') }} as c\n"
    "left join {{ ref('stg_inbox__conversation_parts') }} as p\n"
    "  on p.conversation_id = c.conversation_id\n"
    "group by c.conversation_id\n"
)


def _git(cwd: Path, *argv: str) -> str:
    """Run git in ``cwd`` with a throwaway identity, returning stdout."""

    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *argv],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A DuckDB project reading a local git package of two staging models."""

    package = tmp_path / "tiny_inbox"
    (package / "models").mkdir(parents=True)
    (package / "dbt_project.yml").write_text(
        "name: tiny_inbox\nversion: '1.0'\nconfig-version: 2\nmodel-paths: [models]\n"
        "models:\n  tiny_inbox:\n    +materialized: table\n    +schema: inbox\n"
    )
    (package / "models" / f"{PARENT}.sql").write_text(
        "select conversation_id, state from {{ ref('raw_conversations') }}\n"
    )
    (package / "models" / "stg_inbox__conversation_parts.sql").write_text(
        "select part_id, conversation_id from {{ ref('raw_parts') }}\n"
    )
    _git(package, "init", "-q", ".")
    _git(package, "add", "-A")
    _git(package, "commit", "-qm", "init")

    root = tmp_path / "proj"
    for folder in ("models/marts", "seeds", ".dex", ".git"):
        (root / folder).mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        "name: proj\nversion: '1.0'\nconfig-version: 2\nprofile: proj\n"
        "model-paths: [models]\nseed-paths: [seeds]\n"
        "models:\n  proj:\n    +materialized: table\n"
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
    # Conversations 1-4 exist; 9 was deleted and its parts remain. 4 has none.
    (root / "seeds" / "raw_conversations.csv").write_text(
        "conversation_id,state\n1,open\n2,closed\n3,open\n4,open\n"
    )
    (root / "seeds" / "raw_parts.csv").write_text(
        "part_id,conversation_id\n1,1\n2,1\n3,2\n4,3\n5,9\n"
    )
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nduckdb:\n  path: dev.duckdb\n"
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("DEX_ALLOW_UNSERIALIZED_LEDGER", "1")
    return root


def _dex(capsys, *argv: str) -> dict:
    main(list(argv))
    return json.loads(capsys.readouterr().out)


def _plan(capsys, root: Path, sql: str) -> dict:
    payload = {
        "edits": [
            {
                "path": "models/marts/inbox__threads.sql",
                "kind": "model_sql",
                "content": sql,
            }
        ],
        "declarations": [
            {
                "model": "inbox__threads",
                "description": "One row per conversation.",
                "grain": ["conversation_id"],
                "population": {
                    "rule": "Every current conversation, with or without parts.",
                    "parent": {
                        "model": PARENT,
                        "key": {"conversation_id": "conversation_id"},
                    },
                },
                "columns": {
                    "conversation_id": {
                        "role": "key",
                        "references": f"{PARENT}.conversation_id",
                    },
                    "part_count": {
                        "role": "measure",
                        "aggregation": "sum",
                        "additivity": "additive",
                        "unit": "count",
                    },
                },
            }
        ],
    }
    path = root / "edits.json"
    path.write_text(json.dumps(payload))
    return _dex(capsys, "transform", "plan", "threads", "--edits-file", str(path))


def _parent_rows(planned: dict) -> list[dict]:
    entry = planned["data"]["declarations"]["inbox__threads"]
    return [
        c
        for c in entry.get("contradictions") or []
        if c["check"] == "population_parent"
    ]


def _build(capsys, plan_id: str) -> dict:
    return _dex(
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--verify",
        "--for-plan",
        plan_id,
    )


def test_a_package_parent_is_planned_built_and_verified(project, capsys):
    assert _dex(capsys, "transform", "deps")["status"] == "ok"
    assert _dex(capsys, "transform", "build", "--target", "dev")["status"] == "ok"
    installed = sorted(
        (p.relative_to(project).as_posix(), p.read_bytes())
        for p in (project / "dbt_packages").rglob("*")
        if p.is_file() and ".git" not in p.parts
    )

    # Driven from the parts: accepted, and the driver contradicts the parent.
    planned = _plan(capsys, project, FROM_PARTS)
    assert planned["status"] == "ok", planned["errors"]
    [found] = _parent_rows(planned)
    assert found["status"] == "contradicted"
    assert found["authored"] == "stg_inbox__conversation_parts"
    assert (
        _dex(capsys, "transform", "apply", planned["data"]["plan_id"])["status"] == "ok"
    )
    built = _build(capsys, planned["data"]["plan_id"])
    [record] = built["data"]["verification"]["parent_population"]
    assert record["parent_relation"].split(".")[-2:] == ["main_inbox", PARENT]
    assert record["status"] == "contradicted"
    assert record["measured"]["missing_parents"] == 1
    assert record["measured"]["unexpected_keys"] == 1
    codes = {f["code"] for f in built["data"]["verification"]["findings"]}
    assert {"parent_population_missing", "parent_population_unexpected"} <= codes

    # Driven from the parent: no contradiction, the parent population holds, and
    # the relationships test to the package model runs.
    planned = _plan(capsys, project, FROM_PARENT)
    assert planned["status"] == "ok", planned["errors"]
    assert _parent_rows(planned) == []
    assert (
        _dex(capsys, "transform", "apply", planned["data"]["plan_id"])["status"] == "ok"
    )
    schema = (project / "models/marts/inbox__threads.yml").read_text()
    assert f"ref('{PARENT}')" in schema
    built = _build(capsys, planned["data"]["plan_id"])
    assert built["data"]["outcome"] == "validated"
    [record] = built["data"]["verification"]["parent_population"]
    assert record["status"] == "satisfied"
    relationships = [
        n for n in built["data"]["nodes"] if "relationships" in n["unique_id"]
    ]
    assert relationships
    assert all(n["status"] == "pass" for n in relationships)

    after = sorted(
        (p.relative_to(project).as_posix(), p.read_bytes())
        for p in (project / "dbt_packages").rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    assert after == installed, "no package file is ever written"
