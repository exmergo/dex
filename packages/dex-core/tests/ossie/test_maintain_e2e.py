"""Ossie reaching maintain's tier 2 through the actual command surface (#409).

A repository with no dbt project at all, `semantic.vendor: ossie`, and a real
DuckDB warehouse: `maintain snapshot` has to succeed and persist Ossie's own
fingerprint (keys, a composite-shaped relationship, `relationships_and_keys_
captured`), and `maintain check`/`maintain semantic` have to detect a broken
relationship once the warehouse changes under it. This is the standalone case
`_semantic_layer`'s independent-degrade design exists for: the transform half
(no dbt project) has nothing to answer with, and the semantic half still does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.storage import FilesystemStore


@pytest.fixture
def ossie_repo(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")

    root = tmp_path
    db_path = root / "demo.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE orders (order_id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE customers (customer_id INTEGER, country_code VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'US'), (2, 'EU')")
    conn.close()

    doc = {
        "version": "0.2.0.dev0",
        "name": "commerce",
        "datasets": [
            {
                "name": "orders",
                "source": "demo.main.orders",
                "fields": [
                    {
                        "name": "order_id",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "order_id",
                                }
                            ]
                        },
                    },
                    {
                        "name": "customer_id",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "customer_id",
                                }
                            ]
                        },
                    },
                ],
                "primary_key": ["order_id"],
            },
            {
                "name": "customers",
                "source": "demo.main.customers",
                "fields": [
                    {
                        "name": "customer_id",
                        "expression": {
                            "dialects": [
                                {
                                    "dialect": "ANSI_SQL",
                                    "expression": "customer_id",
                                }
                            ]
                        },
                    }
                ],
                "primary_key": ["customer_id"],
            },
        ],
        "relationships": [
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
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


def test_snapshot_succeeds_with_no_dbt_project_at_all(ossie_repo: Path, capsys):
    payload = _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)

    assert payload["_exit"] == 0, payload
    assert payload["status"] == "ok", payload
    assert payload["data"]["semantic_layer"] is not None
    assert payload["data"]["semantic_layer"]["semantic_model_count"] == 2


def test_the_persisted_snapshot_carries_ossie_keys_and_relationships(
    ossie_repo: Path, capsys
):
    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)

    snap = FilesystemStore(str(ossie_repo)).load_snapshot()
    assert snap is not None
    assert snap.semantic_layer is not None
    assert snap.semantic_layer.relationships_and_keys_captured is True

    by_name = {m.name: m for m in snap.semantic_layer.semantic_models}
    assert by_name["commerce.orders"].keys == [["order_id"]]
    assert by_name["commerce.orders"].relation == "demo.main.orders"

    (relationship,) = snap.semantic_layer.relationships
    assert relationship.model == "commerce.orders"
    assert relationship.to_model == "commerce.customers"
    assert relationship.column_pairs == [("customer_id", "customer_id")]


def test_check_flags_a_relationship_broken_by_a_dropped_column(
    ossie_repo: Path, capsys
):
    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)

    import duckdb

    conn = duckdb.connect(str(ossie_repo / "demo.duckdb"))
    conn.execute("ALTER TABLE orders DROP COLUMN customer_id")
    conn.close()

    payload = _cli(ossie_repo, "maintain", "check", capsys=capsys)

    assert payload["_exit"] == 0, payload
    findings = payload["data"]["findings"]
    broken = [f for f in findings if f["code"] == "broken_relationship"]
    assert len(broken) == 1
    assert broken[0]["data"]["relationship"] == "orders_to_customers"
    assert broken[0]["data"]["missing_pairs"] == [["customer_id", "customer_id"]]


def test_check_runs_schema_and_volume_findings_with_no_transform_project(
    ossie_repo: Path, capsys
):
    """The transform half degrades independently (#409): a repository with no
    dbt project at all still gets the free axes that do not need one."""

    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)

    import duckdb

    conn = duckdb.connect(str(ossie_repo / "demo.duckdb"))
    conn.execute("CREATE TABLE new_table (id INTEGER)")
    conn.close()

    payload = _cli(ossie_repo, "maintain", "check", capsys=capsys)

    assert payload["_exit"] == 0, payload
    added = [
        f
        for f in payload["data"]["findings"]
        if f["code"] == "table_added" and f["identifier"].endswith("new_table")
    ]
    assert len(added) == 1


# --- the pinned schema as the layer's format (#955) -------------------------

OLD_PIN = "27aab111647b1e8d2229a2413e4682e459f43edc62e0eaadd497317754089e42"


def test_the_snapshot_records_the_pinned_schema_as_the_format(ossie_repo: Path, capsys):
    from exmergo_dex_core.ossie.loader import SCHEMA_SHA256

    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)

    snap = FilesystemStore(str(ossie_repo)).load_snapshot()
    assert snap.semantic_layer.format_version == SCHEMA_SHA256


def test_a_baseline_under_the_previous_pin_reports_a_format_change(
    ossie_repo: Path, capsys
):
    """An upgrade of the pin is one finding, never a layer of changed datasets."""

    from exmergo_dex_core.ossie.loader import SCHEMA_SHA256

    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)
    path = ossie_repo / ".dex" / "snapshot.json"
    stored = json.loads(path.read_text())
    stored["semantic_layer"]["format_version"] = OLD_PIN
    for entry in stored["semantic_layer"]["semantic_models"]:
        entry["content_sha256"] = "0" * 64
    path.write_text(json.dumps(stored))

    payload = _cli(ossie_repo, "maintain", "semantic", capsys=capsys)

    assert payload["_exit"] == 0, payload
    codes = [f["code"] for f in payload["data"]["findings"]]
    assert codes == ["semantic_format_changed"], codes
    finding = payload["data"]["findings"][0]
    assert finding["data"]["format_version_before"] == OLD_PIN
    assert finding["data"]["format_version_after"] == SCHEMA_SHA256


def test_a_baseline_that_recorded_no_format_is_unrecorded_not_a_change(
    ossie_repo: Path, capsys
):
    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)
    path = ossie_repo / ".dex" / "snapshot.json"
    stored = json.loads(path.read_text())
    stored["semantic_layer"]["format_version"] = None
    path.write_text(json.dumps(stored))

    payload = _cli(ossie_repo, "maintain", "semantic", capsys=capsys)

    assert payload["_exit"] == 0, payload
    assert [f["code"] for f in payload["data"]["findings"]] == []


# --- keys and relationships through fields (#956) ----------------------------


def _rename_the_join_field(repo: Path) -> None:
    path = repo / "commerce.ossie.yaml"
    doc = yaml.safe_load(path.read_text())
    orders = doc["datasets"][0]
    orders["fields"][1]["name"] = "buyer"
    doc["relationships"][0]["from_columns"] = ["buyer"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_a_dropped_column_behind_a_renamed_field_breaks_the_relationship(
    ossie_repo: Path, capsys
):
    """The relationship names the field `buyer`; what drops is its column."""

    _rename_the_join_field(ossie_repo)
    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)

    import duckdb

    conn = duckdb.connect(str(ossie_repo / "demo.duckdb"))
    conn.execute("ALTER TABLE orders DROP COLUMN customer_id")
    conn.close()

    payload = _cli(ossie_repo, "maintain", "check", capsys=capsys)

    broken = [
        f for f in payload["data"]["findings"] if f["code"] == "broken_relationship"
    ]
    assert len(broken) == 1, payload["data"]["findings"]
    assert broken[0]["data"]["missing_pairs"] == [["customer_id", "customer_id"]]


def test_renaming_a_relationship_field_is_drift_on_the_relationship(
    ossie_repo: Path, capsys
):
    """The fingerprint covers the fields named as well as the columns behind them."""

    _cli(ossie_repo, "maintain", "snapshot", capsys=capsys)
    _rename_the_join_field(ossie_repo)

    payload = _cli(ossie_repo, "maintain", "semantic", capsys=capsys)

    changed = [
        f["data"]
        for f in payload["data"]["findings"]
        if f["code"] == "definition_changed"
    ]
    assert {"kind": "relationship", "name": "orders_to_customers"} in [
        {k: d.get(k) for k in ("kind", "name")} for d in changed
    ], payload["data"]["findings"]
