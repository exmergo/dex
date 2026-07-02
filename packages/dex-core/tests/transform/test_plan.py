"""`transform plan` end to end: validate, diff, store; never touch the project."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _write_payload(tmp_path: Path, edits: list[dict]) -> Path:
    payload_file = tmp_path / "edits.json"
    payload_file.write_text(json.dumps({"edits": edits}), encoding="utf-8")
    return payload_file


def test_plan_from_edits_file(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/stg_orders.sql",
                "kind": "model_sql",
                "content": "select 1 as id, 2 as customer_id\n",
            },
            {
                "path": "models/staging/stg_orders.yml",
                "kind": "schema_yml",
                "content": "version: 2\nmodels:\n  - name: stg_orders\n",
            },
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add stg_orders",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    plan_id = envelope["data"]["plan_id"]
    assert envelope["data"]["edit_count"] == 2
    assert envelope["diffs"] and envelope["diffs"][0]["op"] == "create"
    assert (tmp_path / ".dex" / "plans" / f"{plan_id}.json").is_file()
    # Propose-don't-impose: planning writes nothing into the dbt project.
    assert not (dbt_project_dir / "models/staging/stg_orders.sql").exists()
    assert not (dbt_project_dir / "models/staging/stg_orders.yml").exists()


def test_plan_reads_stdin(dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch):
    payload = json.dumps(
        {
            "edits": [
                {
                    "path": "models/staging/stg_inline.sql",
                    "kind": "model_sql",
                    "content": "select 1 as id\n",
                }
            ]
        }
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "inline",
            "--edits-file",
            "-",
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"


def test_plan_is_idempotent_by_content(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/a.sql",
                "kind": "model_sql",
                "content": "select 1\n",
            }
        ],
    )
    argv = [
        "--repo-root",
        str(tmp_path),
        "transform",
        "plan",
        "same",
        "--edits-file",
        str(payload),
    ]
    _rc, first = _run(argv, capsys)
    _rc, second = _run(argv, capsys)
    assert first["data"]["plan_id"] == second["data"]["plan_id"]


def test_plan_rejects_non_select_sql(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/evil.sql",
                "kind": "model_sql",
                "content": "drop table customers",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "evil",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert not (tmp_path / ".dex" / "plans").exists()


def test_plan_accepts_jinja_wrapped_select(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    content = (
        "{{ config(materialized='view') }}\n\n"
        "with source as (\n"
        "    select * from {{ source('main', 'orders') }}\n"
        ")\n"
        "select id, customer_id from source\n"
    )
    payload = _write_payload(
        tmp_path,
        [{"path": "models/staging/stg_j.sql", "kind": "model_sql", "content": content}],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "jinja",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"


@pytest.mark.parametrize(
    "bad_path",
    ["../outside.sql", "models/../../escape.sql", "seeds/data.yml"],
)
def test_plan_rejects_paths_outside_model_paths(
    dbt_project_dir: Path, tmp_path: Path, capsys, bad_path: str
):
    payload = _write_payload(
        tmp_path, [{"path": bad_path, "kind": "model_sql", "content": "select 1\n"}]
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "x",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_plan_rejects_invalid_yaml(dbt_project_dir: Path, tmp_path: Path, capsys):
    payload = _write_payload(
        tmp_path,
        [
            {
                "path": "models/staging/bad.yml",
                "kind": "schema_yml",
                "content": "version: 2\nmodels:\n  - name: [unclosed\n",
            }
        ],
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "bad",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_plan_without_content_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "plan", "no content"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "edits-file" in envelope["errors"][0]
