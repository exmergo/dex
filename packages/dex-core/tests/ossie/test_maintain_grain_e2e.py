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
from exmergo_dex_core.storage import FilesystemStore


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


def _customers(
    tmp_path: Path, rows: list[tuple], *, primary_key: list[str] | None = None
) -> Path:
    """A primary key and an alternate unique key, each on a physical column."""

    duckdb = pytest.importorskip("duckdb")

    root = tmp_path
    db_path = root / "demo.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE customers (customer_id INTEGER, email VARCHAR, region VARCHAR)"
    )
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?)", rows)
    conn.close()

    handle = _field("handle")
    handle["expression"]["dialects"][0]["expression"] = "lower(email)"
    doc = {
        "version": "0.2.0.dev0",
        "name": "crm",
        "datasets": [
            {
                "name": "customers",
                "source": "demo.main.customers",
                "fields": [
                    _field("customer_id"),
                    _field("email"),
                    _field("region"),
                    handle,
                ],
                "primary_key": primary_key or ["customer_id"],
                "unique_keys": [["email"]],
            }
        ],
    }
    (root / "crm.ossie.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )

    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"duckdb:\n  path: {db_path}\n"
        "semantic:\n"
        "  vendor: ossie\n"
        "  ossie:\n"
        "    files: [crm.ossie.yaml]\n",
        encoding="utf-8",
    )
    return root


def _by_column(payload: dict, code: str) -> dict[str, dict]:
    return {f["column"]: f for f in payload["data"]["findings"] if f["code"] == code}


def test_the_fingerprint_says_which_key_is_the_grain(tmp_path: Path, capsys):
    repo = _customers(tmp_path, [(1, "a@x", "EU"), (2, "b@x", "US")])
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    snap = FilesystemStore(str(repo)).load_snapshot()
    (model,) = snap.semantic_layer.semantic_models
    assert model.keys == [["customer_id"], ["email"]]
    assert model.key_claims == ["primary", "unique"]


def test_a_key_left_out_takes_its_claim_with_it(tmp_path: Path, capsys):
    """A primary key on a computed field links no column, so it is not
    fingerprinted; the unique key after it must not inherit its claim."""

    repo = _customers(
        tmp_path, [(1, "a@x", "EU"), (2, "b@x", "US")], primary_key=["handle"]
    )
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    snap = FilesystemStore(str(repo)).load_snapshot()
    (model,) = snap.semantic_layer.semantic_models
    assert model.keys == [["email"]]
    assert model.key_claims == ["unique"]


def test_a_violated_key_is_worded_as_the_claim_it_made(tmp_path: Path, capsys):
    repo = _customers(tmp_path, [(1, "a@x", "EU"), (1, "b@x", "US"), (2, "b@x", "US")])
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    payload = _cli(repo, "maintain", "grain", capsys=capsys)

    assert payload["_exit"] == 0, payload
    found = _by_column(payload, "declared_grain_not_unique")
    assert found["customer_id"]["data"]["claim"] == "primary"
    assert (
        "declares (customer_id) as the primary key of" in found["customer_id"]["detail"]
    )
    assert found["email"]["data"]["claim"] == "unique"
    assert "declares (email) unique on" in found["email"]["detail"]
    assert "primary key" not in found["email"]["detail"]


def test_a_lapsed_unique_key_names_the_claim_it_made(tmp_path: Path, capsys):
    repo = _customers(tmp_path, [(1, "a@x", "EU"), (2, "b@x", "US"), (3, "c@x", "US")])
    _cli(repo, "explore", "map", capsys=capsys)
    _cli(repo, "maintain", "snapshot", capsys=capsys)

    import duckdb

    conn = duckdb.connect(str(repo / "demo.duckdb"))
    conn.execute("INSERT INTO customers VALUES (4, 'a@x', 'EU')")
    conn.close()

    payload = _cli(repo, "maintain", "grain", capsys=capsys)

    assert payload["_exit"] == 0, payload
    lapsed = _by_column(payload, "key_lost_uniqueness")
    assert lapsed["email"]["data"]["claim"] == "unique"
    assert "declares (email) unique on" in lapsed["email"]["detail"]
    assert "primary key" not in lapsed["email"]["detail"]
