"""`transform apply` end to end: hash-checked writes, conflicts, confirmation."""

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
    payload_file = tmp_path / f"edits-{abs(hash((path, content)))}.json"
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


def test_apply_round_trip(dbt_project_dir: Path, tmp_path: Path, capsys):
    plan_id = _make_plan(
        tmp_path, capsys, "models/marts/fct_orders.sql", "select 1 as id\n"
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["models/marts/fct_orders.sql"]
    assert envelope["diffs"]
    written = dbt_project_dir / "models/marts/fct_orders.sql"
    assert written.read_text(encoding="utf-8") == "select 1 as id\n"
    # The stored plan is marked applied.
    plan_file = tmp_path / ".dex" / "plans" / f"{plan_id}.json"
    assert json.loads(plan_file.read_text())["applied_at"] is not None


def test_apply_unknown_plan_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", "p0000000000"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_apply_without_plan_id_is_a_clean_error(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 1
    assert envelope["status"] == "error"


def test_reapply_is_a_noop(dbt_project_dir: Path, tmp_path: Path, capsys):
    plan_id = _make_plan(tmp_path, capsys, "models/marts/fct_x.sql", "select 1\n")
    _run(["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == []


def test_apply_conflict_needs_confirmation_then_confirm_overrides(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    target = dbt_project_dir / "models/staging/stg_customers.sql"
    plan_id = _make_plan(
        tmp_path, capsys, "models/staging/stg_customers.sql", "select 1 as id\n"
    )
    # A human edits the file between plan and apply.
    target.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id], capsys
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert (
        envelope["data"]["conflicts"][0]["path"] == "models/staging/stg_customers.sql"
    )
    assert envelope["diffs"], "the divergence is surfaced as a diff"
    assert target.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"

    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "apply", plan_id, "--confirm"],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["written"] == ["models/staging/stg_customers.sql"]
    assert envelope["data"]["conflicts_overridden"] == [
        "models/staging/stg_customers.sql"
    ]
    assert target.read_text(encoding="utf-8") == "select 1 as id\n"
