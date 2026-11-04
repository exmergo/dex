"""Plan-id resolution shared by `transform apply/export/ground/classify` (#1103).

An explicitly empty plan id (`apply ""`) is refused, and omitting the id
reports the picked latest-unapplied plan in `data.resolved_plan` and in a
`warnings` line, so a caller can see what Dex chose.
"""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _make_plan(
    tmp_path: Path, capsys, path: str, content: str, intent: str = "test plan"
) -> str:
    payload_file = tmp_path / f"edits-{abs(hash((path, content, intent)))}.json"
    payload_file.write_text(
        json.dumps(
            {"edits": [{"path": path, "kind": "model_sql", "content": content}]}
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            intent,
            "--edits-file",
            str(payload_file),
        ],
        capsys,
    )
    assert rc == 0 and envelope["status"] == "ok"
    return envelope["data"]["plan_id"]


def test_export_empty_plan_id_is_refused(dbt_project_dir: Path, tmp_path: Path, capsys):
    _make_plan(tmp_path, capsys, "models/marts/fct_orders.sql", "select 1 as id\n")
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "export", ""], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert envelope["reason"] == "request"
    assert "empty plan id" in envelope["errors"][0]


def test_export_without_id_reports_the_resolved_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    plan_id = _make_plan(
        tmp_path, capsys, "models/marts/fct_orders.sql", "select 1 as id\n", "export me"
    )
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "export"], capsys)
    assert rc == 0, envelope
    resolved = envelope["data"]["resolved_plan"]
    assert resolved["plan_id"] == plan_id
    assert resolved["intent"] == "export me"
    assert resolved["created_at"]
    assert resolved["resolved_from"] == "latest_unapplied"
    assert any(plan_id in w for w in envelope["warnings"])


def test_export_with_explicit_id_reports_no_resolved_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    plan_id = _make_plan(
        tmp_path, capsys, "models/marts/fct_orders.sql", "select 1 as id\n"
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "export", plan_id], capsys
    )
    assert rc == 0, envelope
    assert "resolved_plan" not in envelope["data"]
    assert not envelope["warnings"]


def test_classify_empty_plan_id_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _make_plan(tmp_path, capsys, "models/marts/fct_orders.sql", "select 1 as id\n")
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "classify", ""], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert envelope["reason"] == "request"
    assert "empty plan id" in envelope["errors"][0]


def test_classify_without_id_reports_the_resolved_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    plan_id = _make_plan(
        tmp_path,
        capsys,
        "models/marts/fct_orders.sql",
        "select 1 as id\n",
        "classify me",
    )
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "classify"], capsys)
    assert rc == 0, envelope
    resolved = envelope["data"]["resolved_plan"]
    assert resolved["plan_id"] == plan_id
    assert resolved["intent"] == "classify me"
    assert resolved["resolved_from"] == "latest_unapplied"
    assert any(plan_id in w for w in envelope["warnings"])
