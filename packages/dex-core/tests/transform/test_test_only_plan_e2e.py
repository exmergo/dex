"""DuckDB end to end cases for test-only plans (#1316).

Tests coverage establishment for plans that only add singular tests or native
unit tests, including passing, unrelated, skipped, and failing executions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

pytestmark = pytest.mark.live_open_adapter


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _edits_file(tmp_path: Path, *entries: dict) -> Path:
    payload = tmp_path / "edits.json"
    payload.write_text(json.dumps({"edits": list(entries)}), encoding="utf-8")
    return payload


def test_passing_singular_test_only_plan_e2e(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("duckdb")

    _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    payload = _edits_file(
        tmp_path,
        {
            "path": "tests/assert_no_negative_ids.sql",
            "kind": "test_sql",
            "content": "select id from {{ ref('stg_customers') }} where id < 0\n",
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "assert no negative ids",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    plan_id = envelope["data"]["plan_id"]
    rc, _ = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--for-plan",
            plan_id,
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    completion = envelope["data"]["completion"]
    assert completion["outcome"] == "validated"
    deliverables = completion["deliverables"]
    assert deliverables["coverage"] == "known"
    assert deliverables["required"] == 1
    assert deliverables["built"]["names"] == ["assert_no_negative_ids"]
    assert deliverables["failed"]["count"] == 0
    assert deliverables["skipped"]["count"] == 0
    assert deliverables["missing"]["count"] == 0
    assert completion["assurance"]["asserted"]["count"] == 0
    assert completion["assurance"]["build_only"]["count"] == 0


def test_unrelated_model_build_does_not_satisfy_test_only_plan_e2e(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("duckdb")

    (dbt_project_dir / "models" / "staging" / "unrelated_model.sql").write_text(
        "select 1 as x\n", encoding="utf-8"
    )
    _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    payload = _edits_file(
        tmp_path,
        {
            "path": "tests/assert_no_negative_ids.sql",
            "kind": "test_sql",
            "content": "select id from {{ ref('stg_customers') }} where id < 0\n",
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "assert no negative ids",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    plan_id = envelope["data"]["plan_id"]
    rc, _ = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--select",
            "unrelated_model",
            "--for-plan",
            plan_id,
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    completion = envelope["data"]["completion"]
    assert completion["outcome"] == "unrelated"
    deliverables = completion["deliverables"]
    assert deliverables["coverage"] == "known"
    assert deliverables["required"] == 1
    assert deliverables["built"]["count"] == 0
    assert deliverables["missing"]["names"] == ["assert_no_negative_ids"]


def test_failing_test_only_plan_e2e(dbt_project_dir: Path, tmp_path: Path, capsys):
    pytest.importorskip("duckdb")

    _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    payload = _edits_file(
        tmp_path,
        {
            "path": "tests/assert_always_fails.sql",
            "kind": "test_sql",
            "content": "select id from {{ ref('stg_customers') }} where id = 1\n",
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "failing test",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    plan_id = envelope["data"]["plan_id"]
    rc, _ = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--for-plan",
            plan_id,
        ],
        capsys,
    )
    assert rc != 0
    completion = envelope["data"]["completion"]
    assert completion["outcome"] == "failed"
    deliverables = completion["deliverables"]
    assert deliverables["coverage"] == "known"
    assert deliverables["required"] == 1
    assert deliverables["failed"]["names"] == ["assert_always_fails"]
    assert deliverables["built"]["count"] == 0
    assert deliverables["blocked"][0]["deliverable"] == "assert_always_fails"
    assert deliverables["blocked"][0]["status"] == "fail"


def test_skipped_test_only_plan_e2e(dbt_project_dir: Path, tmp_path: Path, capsys):
    pytest.importorskip("duckdb")

    (dbt_project_dir / "models" / "staging" / "stg_customers.sql").write_text(
        "select * from non_existent_source_table\n", encoding="utf-8"
    )
    payload = _edits_file(
        tmp_path,
        {
            "path": "tests/assert_on_broken.sql",
            "kind": "test_sql",
            "content": "select id from {{ ref('stg_customers') }} where id < 0\n",
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "test on broken",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    plan_id = envelope["data"]["plan_id"]
    rc, _ = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--for-plan",
            plan_id,
        ],
        capsys,
    )
    assert rc != 0
    completion = envelope["data"]["completion"]
    assert completion["outcome"] == "failed"
    deliverables = completion["deliverables"]
    assert deliverables["coverage"] == "known"
    assert deliverables["required"] == 1
    assert deliverables["skipped"]["names"] == ["assert_on_broken"]
    assert deliverables["built"]["count"] == 0
    assert deliverables["blocked"][0]["deliverable"] == "assert_on_broken"
    assert deliverables["blocked"][0]["status"] == "skipped"
    assert deliverables["blocked"][0]["cause"] == "stg_customers"


def test_passing_unit_test_only_plan_e2e(dbt_project_dir: Path, tmp_path: Path, capsys):
    pytest.importorskip("duckdb")

    _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    unit_test_yaml = (
        "version: 2\n"
        "unit_tests:\n"
        "  - name: test_customers_atomic\n"
        "    model: stg_customers\n"
        "    given:\n"
        "      - input: ref('stg_customers')\n"
        "        rows:\n"
        "          - {id: 1, email: 'a@example.com'}\n"
        "    expect:\n"
        "      rows:\n"
        "        - {id: 1, email: 'a@example.com'}\n"
    )
    payload = _edits_file(
        tmp_path,
        {
            "path": "models/staging/test_stg_customers.yml",
            "kind": "schema_yml",
            "content": unit_test_yaml,
        },
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add unit test",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    plan_id = envelope["data"]["plan_id"]
    rc, _ = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--for-plan",
            plan_id,
        ],
        capsys,
    )
    assert rc == 0, envelope.get("errors")
    completion = envelope["data"]["completion"]
    assert completion["outcome"] == "validated"
    deliverables = completion["deliverables"]
    assert deliverables["coverage"] == "known"
    assert deliverables["required"] == 1
    assert deliverables["built"]["names"] == ["test_customers_atomic"]
    assert deliverables["failed"]["count"] == 0
    assert deliverables["skipped"]["count"] == 0
    assert deliverables["missing"]["count"] == 0
