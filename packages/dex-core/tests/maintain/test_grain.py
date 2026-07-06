"""maintain grain: uniqueness and fanout drift from aggregates, never raw rows."""

from __future__ import annotations


def test_clean_warehouse_reports_no_grain_drift(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "grain")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0
    assert payload["cost"]["paradigm"] == "free_local"


def test_duplicated_key_is_detected_exactly(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")

    rc, payload = maintain_repo.dex("maintain", "grain")
    assert rc == 0 and payload["status"] == "ok"
    findings = [
        f for f in payload["data"]["findings"] if f["code"] == "key_lost_uniqueness"
    ]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["identifier"] == "warehouse.main.orders"
    assert finding["column"] == "order_id"
    assert finding["severity"] == "high"
    assert finding["exact"] is True
    assert finding["data"] == {
        "distinct_count": 200,
        "row_count": 210,
        "was_grain": True,
    }
    # order_id is the semantic model's entity, so every metric on it is at risk.
    assert finding["impacted_models"] == ["stg_orders"]
    assert finding["impacted_metrics"] == ["order_volume", "revenue"]


def test_new_orphans_move_the_verified_join(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "INSERT INTO orders SELECT 200 + i, 900 + i, 1.0, 'placed', "
        "DATE '2024-03-01' FROM range(1, 6) t(i)"
    )

    _rc, payload = maintain_repo.dex("maintain", "grain")
    findings = {
        f["data"]["to_dataset"]: f
        for f in payload["data"]["findings"]
        if f["code"] == "join_orphans_increased"
    }
    # The new orders orphan both verified joins: customer_id -> customers
    # (unknown customers) and order_id -> stg_orders (not yet built there).
    finding = findings["warehouse.main.customers"]
    assert finding["identifier"] == "warehouse.main.orders"
    assert finding["column"] == "customer_id"
    assert finding["data"]["orphan_fraction_before"] == 0.0
    assert finding["data"]["orphan_fraction_after"] > 0.0
    assert finding["severity"] == "medium"


def test_dropped_key_column_is_left_to_the_schema_axis(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("ALTER TABLE customers DROP id")

    rc, payload = maintain_repo.dex("maintain", "grain", "customers")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0


def test_scope_limits_the_scan(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")

    rc, payload = maintain_repo.dex("maintain", "grain", "customers")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0


def test_metadata_only_baseline_warns_grain_has_nothing(dex, tmp_path):
    import duckdb

    root = tmp_path / "bare"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER)")
    conn.close()
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )
    dex("--repo-root", str(root), "maintain", "snapshot")

    rc, payload = dex("--repo-root", str(root), "maintain", "grain")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0
    assert any("metadata-only" in w for w in payload["warnings"])


def test_estimated_row_counts_cannot_fabricate_duplicates():
    """An adapter whose free row counts are planner estimates (Postgres
    reltuples) must not produce key_lost_uniqueness findings from the estimate
    alone: grain_drift re-reads the metadata after the distinct scan, and the
    adapter serves the exact count that scan paid for."""

    from exmergo_dex_core.adapters.base import ColumnMeta, ObjectMeta
    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.drift import GrainPlan, grain_drift

    class EstimatingAdapter:
        name = "stub"
        dialect = "duckdb"

        def __init__(self):
            self.scanned = False

        def table_metadata(self, identifier):
            # 1200 is the stale planner estimate; 1000 is the exact count the
            # distinct scan carried (COUNT(*) rides along on Postgres).
            rows = 1000 if self.scanned else 1200
            meta = ObjectMeta(
                identifier=identifier,
                object_type="table",
                schema="s",
                name="t",
                row_count=rows,
                byte_size=None,
                column_count=1,
            )
            return meta, [
                ColumnMeta(name="id", data_type="INTEGER", nullable=False, ordinal=0)
            ]

        def exact_distinct_counts(self, identifier, columns):
            self.scanned = True
            return dict.fromkeys(columns, 1000)

    dataset = Dataset(identifier="db.s.t", candidate_keys=[["id"]], grain=["id"])
    plan = GrainPlan(key_checks=[(dataset, ["id"], 1200)], fanout_pairs=[])
    findings = grain_drift(EstimatingAdapter(), plan)
    assert findings == []
