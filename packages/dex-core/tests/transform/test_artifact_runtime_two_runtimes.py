"""Two real dbt runtimes on one project (#862, #1014), on DuckDB.

Dex builds with its own dbt (1.12), then a second, older runtime snapshots the
same project. With Dex's isolated artifacts the second runtime finds nothing of
Dex's in ``target/`` and succeeds. The shared policy is the control: it
reproduces the collision (1.12 leaves a snapshot's compiled ``.sql`` path as a
directory, 1.10 opens it as a file), and then Dex refuses to write over the
target path the other runtime left.

Skipped unless ``DEX_TEST_DBT_SECOND_RUNTIME`` names the second runtime's
``dbt`` command, for example::

    DEX_TEST_DBT_SECOND_RUNTIME="uvx --quiet --python 3.12 \\
        --from dbt-core~=1.10.0 --with dbt-duckdb~=1.10.0 dbt" \\
        uv run pytest tests/transform/test_artifact_runtime_two_runtimes.py -q

Free: DuckDB, local. The tested pair is the one the command names; a claim
about any other pair needs its own run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

SECOND = os.environ.get("DEX_TEST_DBT_SECOND_RUNTIME")
pytestmark = pytest.mark.skipif(
    not SECOND, reason="set DEX_TEST_DBT_SECOND_RUNTIME to a second dbt runtime"
)

SNAPSHOT = """{% snapshot users_snapshot %}
{{ config(unique_key='id', strategy='check', check_cols='all') }}
select * from {{ ref('users') }}
{% endsnapshot %}
"""


def _dex(root: Path, capsys, *args: str) -> dict:
    main(["--repo-root", str(root), *args])
    return json.loads(capsys.readouterr().out)


def _second_runtime_snapshot(project: Path) -> subprocess.CompletedProcess[str]:
    argv = [
        *shlex.split(SECOND or ""),
        "snapshot",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--target",
        "dev",
    ]
    return subprocess.run(argv, capture_output=True, text=True, cwd=project)  # noqa: S603


@pytest.fixture
def repo(dbt_project_dir: Path, tmp_path: Path) -> Path:
    project = dbt_project_dir
    (project / "seeds").mkdir()
    (project / "seeds" / "users.csv").write_text("id,name\n1,ada\n2,grace\n")
    (project / "snapshots").mkdir()
    (project / "snapshots" / "users_snapshot.sql").write_text(SNAPSHOT)
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "duckdb",
                "duckdb": {"path": str(tmp_path / "dev.duckdb")},
                "dbt_project_dir": project.name,
                "dbt_target": "dev",
            }
        )
    )
    return tmp_path


def test_a_second_runtime_snapshots_after_an_isolated_dex_build(repo, capsys):
    project = repo / "analytics"
    envelope = _dex(repo, capsys, "transform", "build", "--target", "dev", "--confirm")
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["artifact_directory"].startswith("target/dex/dbt-")
    assert not (project / "target" / "run").exists()

    completed = _second_runtime_snapshot(project)
    assert completed.returncode == 0, completed.stdout[-2000:]


def test_the_shared_policy_reproduces_the_collision_and_then_refuses(repo, capsys):
    """The control: without isolation the two runtimes collide."""

    project = repo / "analytics"
    config = yaml.safe_load((repo / ".dex" / "config.yml").read_text())
    config["transform"] = {"artifacts": "shared"}
    (repo / ".dex" / "config.yml").write_text(yaml.safe_dump(config))

    envelope = _dex(repo, capsys, "transform", "build", "--target", "dev", "--confirm")
    assert envelope["status"] == "ok", envelope["errors"]
    assert envelope["data"]["artifact_directory"] == "target"

    completed = _second_runtime_snapshot(project)
    assert completed.returncode != 0
    assert "Is a directory" in completed.stdout + completed.stderr

    envelope = _dex(repo, capsys, "transform", "build", "--target", "dev", "--confirm")
    assert envelope["status"] == "error"
    assert "dbt clean" in envelope["errors"][0]
    assert (project / "target" / "run").exists()
