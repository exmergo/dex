"""maintain snapshot: what the baseline pins, its fallbacks, and its hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.storage import FilesystemStore


def test_snapshot_pins_cache_and_fingerprints_layers(maintain_repo):
    payload = maintain_repo.snapshot()
    data = payload["data"]

    assert data["baseline"]["from"] == "cache"
    assert data["baseline"]["dataset_count"] == 3
    assert data["baseline"]["relationship_count"] >= 1
    assert data["baseline"]["grain_baseline_count"] >= 2
    assert data["transform_layer"]["model_count"] == 1
    assert data["transform_layer"]["source_count"] == 2
    assert data["semantic_layer"]["semantic_model_count"] == 1
    assert data["semantic_layer"]["metric_count"] == 2
    assert "commit .dex/snapshot.json" in data["hint"]

    snap = FilesystemStore(maintain_repo.root).load_snapshot()
    identifiers = {d.identifier for d in snap.warehouse.datasets}
    assert identifiers == {
        "warehouse.main.customers",
        "warehouse.main.orders",
        "warehouse.main.stg_orders",
    }

    orders = next(
        d for d in snap.warehouse.datasets if d.identifier.endswith(".orders")
    )
    assert ["order_id"] in orders.candidate_keys
    verified = [r for r in snap.warehouse.relationships if r.verified]
    assert verified and all(r.orphan_fraction == 0.0 for r in verified)

    sources = {s.table for s in snap.transform_layer.sources}
    assert sources == {"customers", "orders"}
    assert "stg_orders" in snap.transform_layer.models
    assert "models/staging/stg_orders.sql" in snap.transform_layer.files

    model = snap.semantic_layer.semantic_models[0]
    assert model.model_ref == "stg_orders"
    assert model.categorical_dimensions == {"status": "status"}
    assert model.entities == {"order_id": "order_id"}
    assert model.measures == {"order_amount": "amount", "order_count": "order_id"}
    assert snap.transform_layer.model_sources == {"stg_orders": ["main.orders"]}
    revenue = next(m for m in snap.semantic_layer.metrics if m.name == "revenue")
    assert revenue.input_measures == ["order_amount"]


def test_snapshot_without_cache_is_metadata_only(dex, tmp_path: Path):
    duckdb = pytest.importorskip("duckdb")
    root = tmp_path / "bare"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER, label VARCHAR)")
    conn.execute("INSERT INTO items VALUES (1, 'a'), (2, 'b')")
    conn.close()
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )

    rc, payload = dex("--repo-root", str(root), "maintain", "snapshot")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["baseline"]["from"] == "metadata"
    assert payload["data"]["transform_layer"] is None
    warnings = " ".join(payload["warnings"])
    assert "metadata-only" in warnings
    assert "no dbt project" in warnings

    snap = FilesystemStore(root).load_snapshot()
    assert snap.warehouse_from == "metadata"
    items = snap.warehouse.datasets[0]
    assert items.row_count == 2
    assert {c.name for c in items.columns} == {"id", "label"}
    # Metadata-only means no uniqueness verdicts: nothing for grain to lean on.
    assert items.candidate_keys == []


def test_snapshot_recaptures_on_connector_mismatch(maintain_repo):
    store = FilesystemStore(maintain_repo.root)
    cache = store.load_cache()
    cache.provenance.connector = "bigquery"
    store.save_cache(cache)

    payload = maintain_repo.snapshot()
    assert payload["data"]["baseline"]["from"] == "metadata"
    assert any("bigquery" in w for w in payload["warnings"])


def test_snapshot_file_carries_no_string_values(maintain_repo):
    maintain_repo.snapshot()
    raw = (maintain_repo.root / ".dex" / "snapshot.json").read_text(encoding="utf-8")
    # No customer value may land in the baseline: string min/max are suppressed
    # at profile time and the snapshot pins profiles, never rows.
    assert "example.com" not in raw
    assert "name_" not in raw

    snap = json.loads(raw)
    for dataset in snap["warehouse"]["datasets"]:
        for column in dataset["columns"]:
            if "VARCHAR" in column["data_type"].upper():
                assert column["min_value"] is None
                assert column["max_value"] is None


def test_the_snapshot_hint_only_promises_git_when_the_baseline_is_a_file(
    tmp_path: Path,
):
    """The hint is advice about where the baseline landed, so it has to follow the
    backend.

    Telling a host whose snapshot is a row or a document to commit
    `.dex/snapshot.json` names a file it does not have. The half that holds
    everywhere, re-pinning after a known-good build, is what a backend dex does
    not ship should get; `snapshot_path` carries the real location either way.
    The filesystem half is asserted by the first test in this file.
    """

    import argparse

    from exmergo_dex_core import DexConfig, DexEngine, MemoryStore
    from exmergo_dex_core.maintain.commands import cmd_snapshot

    duckdb = pytest.importorskip("duckdb")
    db_path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER)")
    conn.close()

    with DexEngine(
        connector="duckdb",
        path=str(db_path),
        config=DexConfig(connector="duckdb"),
        store=MemoryStore(),
    ) as engine:
        envelope = cmd_snapshot(argparse.Namespace(), engine)

    hint = envelope.data["hint"]
    assert "commit" not in hint
    assert ".dex/snapshot.json" not in hint
    assert "re-run `maintain snapshot`" in hint
    # And the location is still reported, just not as a path anyone can commit.
    assert envelope.data["snapshot_path"] == "memory:snapshot"
