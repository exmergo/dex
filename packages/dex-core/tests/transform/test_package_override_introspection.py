"""A package staging model that lists its columns is repaired through Dex (#1154).

Fivetran-style staging models list their source's columns while compiling
(`fill_staging_columns(adapter.get_columns_in_relation(...))`). The project
override the prerequisite-repair procedure recommends (#860) copies that SQL,
so planning it has to answer the column listing rather than refuse it as a
`DESCRIBE`. Real dbt on DuckDB, with the package installed from a local git
repository as in `test_cold_deps.py`: no network, and a real copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest
import yaml

from exmergo_dex_core.cli import main

pytest.importorskip("dbt.adapters.duckdb")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# Fivetran's macro, trimmed: a staging column the source lacks becomes a typed
# NULL, so the model's shape does not depend on the connector's sync.
FILL_STAGING_COLUMNS = """\
{% macro fill_staging_columns(source_columns, staging_columns) -%}
{%- set present = source_columns | map(attribute='name') | map('lower') | list -%}
{%- for column in staging_columns %}
  {% if column.name | lower in present -%}
    {{ column.name }}
  {%- else -%}
    cast(null as {{ column.datatype }}) as {{ column.name }}
  {%- endif -%}
  {%- if not loop.last %},{% endif %}
{%- endfor %}
{%- endmacro %}

{% macro get_task_columns() -%}
{{ return([
  {"name": "id", "datatype": "integer"},
  {"name": "created_at", "datatype": "timestamp"},
  {"name": "legacy_ts", "datatype": "integer"},
  {"name": "section_id", "datatype": "integer"},
]) }}
{%- endmacro %}
"""

FIELDS = """with fields as (
    select
        {{ tiny_pkg.fill_staging_columns(
            source_columns=adapter.get_columns_in_relation(ref('raw_task')),
            staging_columns=tiny_pkg.get_task_columns()
        ) }}
    from {{ ref('raw_task') }}
)
"""
# The package's own model: TIMESTAMP coalesced with an INTEGER fails to build.
BROKEN = FIELDS + (
    "select id, section_id, coalesce(created_at, legacy_ts) as created_ts\n"
    "from fields\n"
)
# The override: the same introspection, a variable the package also reads, and
# the explicit cast. The `if` is what sends it through dbt compilation.
OVERRIDE = FIELDS + (
    "select id, section_id,\n"
    "{% if var('tiny_pkg__using_legacy_ts', true) %}"
    "    coalesce(created_at, cast(cast(legacy_ts as varchar) as timestamp))"
    "{% else %}    created_at{% endif %} as created_ts\n"
    "from fields\n"
)


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


@pytest.fixture
def project(tmp_path, monkeypatch):
    package = tmp_path / "tiny_pkg"
    (package / "models").mkdir(parents=True)
    (package / "macros").mkdir()
    (package / "dbt_project.yml").write_text(
        "name: tiny_pkg\nversion: '1.0'\nconfig-version: 2\n"
        "model-paths: [models]\nmacro-paths: [macros]\n"
    )
    (package / "macros" / "staging.sql").write_text(FILL_STAGING_COLUMNS)
    (package / "models" / "pkg_task.sql").write_text(BROKEN)
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
    # No section_id: the macro has to fill it, which it can only do by knowing
    # the source's real columns.
    (root / "seeds" / "raw_task.csv").write_text(
        "id,created_at,legacy_ts\n1,2024-01-01 00:00:00,\n2,,\n3,2024-02-01 00:00:00,\n"
    )
    (root / "models" / "rpt.sql").write_text(
        "select id, section_id, created_ts from {{ ref('pkg_task') }}\n"
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


def _columns(root: Path, relation: str) -> dict[str, str]:
    connection = duckdb.connect(str(root / "dev.duckdb"), read_only=True)
    try:
        return dict(
            connection.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_name = ? order by ordinal_position",
                [relation],
            ).fetchall()
        )
    finally:
        connection.close()


def test_an_introspecting_package_override_plans_applies_and_builds(project, capsys):
    failed = _dex(capsys, "transform", "build", "--target", "dev")
    assert failed["status"] == "error"
    [entry] = failed["data"]["prerequisites"]
    assert (entry["node"], entry["owner"]) == ("pkg_task", "package")

    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    config["models"] = {"tiny_pkg": {"pkg_task": {"+enabled": False}}}
    edits = project / "edits.json"
    edits.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/pkg_task.sql",
                        "kind": "model_sql",
                        "content": OVERRIDE,
                    },
                    {
                        "path": "dbt_project.yml",
                        "kind": "project_yml",
                        "content": yaml.safe_dump(config, sort_keys=False),
                    },
                ]
            }
        )
    )
    planned = _dex(
        capsys, "transform", "plan", "Override pkg_task", "--edits-file", str(edits)
    )
    assert planned["status"] == "ok", planned["errors"]
    assert any(
        "listed the columns of" in w and "raw_task" in w for w in planned["warnings"]
    ), planned["warnings"]
    plan_id = planned["data"]["plan_id"]

    applied = _dex(capsys, "transform", "apply", plan_id)
    assert applied["status"] == "ok", applied["errors"]
    assert (project / "models" / "pkg_task.sql").read_text() == OVERRIDE

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
    assert built["data"]["outcome"] == "validated"
    assert built["data"]["verification"]["ran"] is True
    columns = _columns(project, "rpt")
    assert columns["created_ts"].startswith("TIMESTAMP")
    # The missing source column was filled from the real column list.
    assert columns["section_id"] == "INTEGER"


def test_the_override_without_its_cast_still_fails_the_build(project, capsys):
    """Planning answers the column listing; it does not make bad SQL build."""

    config = yaml.safe_load((project / "dbt_project.yml").read_text())
    config["models"] = {"tiny_pkg": {"pkg_task": {"+enabled": False}}}
    unrepaired = BROKEN.replace("select id,", "select {% if true %}id{% endif %},")
    edits = project / "edits.json"
    edits.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/pkg_task.sql",
                        "kind": "model_sql",
                        "content": unrepaired,
                    },
                    {
                        "path": "dbt_project.yml",
                        "kind": "project_yml",
                        "content": yaml.safe_dump(config, sort_keys=False),
                    },
                ]
            }
        )
    )
    # Installs the package and loads the seed; the package model fails.
    assert _dex(capsys, "transform", "build", "--target", "dev")["status"] == "error"
    planned = _dex(
        capsys, "transform", "plan", "Override pkg_task", "--edits-file", str(edits)
    )
    assert planned["status"] == "ok", planned["errors"]
    assert _dex(capsys, "transform", "apply", planned["data"]["plan_id"])["status"] == (
        "ok"
    )
    built = _dex(
        capsys,
        "transform",
        "build",
        "--target",
        "dev",
        "--for-plan",
        planned["data"]["plan_id"],
    )
    assert built["status"] == "error"
    [entry] = built["data"]["prerequisites"]
    assert (entry["node"], entry["owner"]) == ("pkg_task", "project")
