"""Ossie's declared keys reach `maintain grain`/`maintain check` (#410).

Reuses the existing governed grain-verification machinery (`grain_plan`'s
declared-composite survey, already billed and confirmation-gated the same
way for every format) rather than building anything new: the only gap #410
closes is that `grain_plan` used to read declared keys exclusively from
`engine.project_format().definitions()`, which is never Ossie. A repository
with no dbt project at all and `semantic.vendor: ossie` is the case that
proves the fix, since there is nothing else that could feed the check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main


def _field(name: str) -> dict:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
    }


def _repo(tmp_path: Path, *, duplicate_line_no: bool) -> Path:
    duckdb = pytest.importorskip("duckdb")

    root = tmp_path
    db_path = root / "demo.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku VARCHAR)"
    )
    rows = [(1, 1, "A"), (1, 2, "B"), (2, 1, "C")]
    if duplicate_line_no:
        # (order_id=1, line_no=1) now appears twice: the declared composite
        # key is no longer unique.
        rows.append((1, 1, "A2"))
    conn.executemany("INSERT INTO order_items VALUES (?, ?, ?)", rows)
    conn.close()

    doc = {
        "version": "0.2.0.dev0",
        "semantic_model": [
            {
                "name": "commerce",
                "datasets": [
                    {
                        "name": "order_items",
                        "source": "demo.main.order_items",
                        "fields": [
                            _field("order_id"),
                            _field("line_no"),
                            _field("sku"),
                        ],
                        # An array of arrays, per the issue's own wording: one
                        # independent unique-key declaration, itself composite.
                        "unique_keys": [["order_id", "line_no"]],
                    }
                ],
            }
        ],
    }
    (root / "commerce.ossie.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )

    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"duckdb:\n  path: {db_path}\n"
        "semantic:\n"
        "  vendor: ossie\n"
        "  ossie:\n"
        "    files: [commerce.ossie.yaml]\n",
        encoding="utf-8",
    )
    return root


def _cli(repo: Path, *argv: str, capsys) -> dict:
    rc = main(["--repo-root", str(repo), *argv])
    payload = json.loads(capsys.readouterr().out)
    payload["_exit"] = rc
    return payload


def test_grain_flags_a_declared_composite_key_that_is_not_unique(
    tmp_path: Path, capsys
):
    repo = _repo(tmp_path, duplicate_line_no=True)
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    payload = _cli(repo, "maintain", "grain", capsys=capsys)

    assert payload["_exit"] == 0, payload
    findings = payload["data"]["findings"]
    broken = [f for f in findings if f["code"] == "declared_grain_not_unique"]
    assert len(broken) == 1
    assert broken[0]["identifier"].endswith("order_items")
    assert sorted(broken[0]["data"]["columns"]) == ["line_no", "order_id"]


def test_grain_is_silent_when_the_declared_composite_key_is_actually_unique(
    tmp_path: Path, capsys
):
    repo = _repo(tmp_path, duplicate_line_no=False)
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    payload = _cli(repo, "maintain", "grain", capsys=capsys)

    assert payload["_exit"] == 0, payload
    codes = {f["code"] for f in payload["data"]["findings"]}
    assert "declared_grain_not_unique" not in codes


def test_check_also_surfaces_the_declared_grain_finding(tmp_path: Path, capsys):
    """The same routing fix has to reach `maintain check`'s grain axis, not
    only the focused `maintain grain` detector."""

    repo = _repo(tmp_path, duplicate_line_no=True)
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    payload = _cli(repo, "maintain", "check", capsys=capsys)

    assert payload["_exit"] == 0, payload
    broken = [
        f
        for f in payload["data"]["findings"]
        if f["code"] == "declared_grain_not_unique"
    ]
    assert len(broken) == 1


def test_reconcile_proposes_no_edit_for_a_failed_declared_grain(tmp_path: Path, capsys):
    """A failed declaration is a finding, never an automatic rewrite (#410):
    Ossie has no write tier at all, and `declared_grain_not_unique` is
    advisory-only for every format regardless, so this holds twice over.

    `maintain reconcile` needs an actual dbt project to load even when every
    proposal it produces ends up advisory (it is the write tier's own
    prerequisite, unrelated to #410), so this uses a minimal empty one beside
    the Ossie document rather than the no-dbt-project fixture the other tests
    in this file use.
    """

    repo = _repo(tmp_path, duplicate_line_no=True)
    (repo / "dbt_project.yml").write_text(
        'name: ossie_grain_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models").mkdir()
    _cli(repo, "maintain", "snapshot", capsys=capsys)
    _cli(repo, "maintain", "grain", capsys=capsys)

    payload = _cli(repo, "maintain", "reconcile", capsys=capsys)

    assert payload["_exit"] == 0, payload
    proposals = [
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "declared_grain_not_unique"
    ]
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "advisory"
    assert payload["data"].get("plan_id") is None
