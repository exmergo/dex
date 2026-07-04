"""maintain volume: freshness drift from free row-count metadata."""

from __future__ import annotations

from exmergo_dex_core.cache import DexStore


def test_clean_warehouse_reports_no_volume_drift(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "volume")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0


def test_row_count_collapse_ranks_high_and_traces_to_metrics(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "DELETE FROM orders WHERE order_id > 20",  # 200 -> 20 rows
        "DELETE FROM stg_orders",  # emptied entirely
        "INSERT INTO customers VALUES (100, 'x', 'x@example.com', DATE '2024-03-01')",
    )

    rc, payload = maintain_repo.dex("maintain", "volume")
    assert rc == 0 and payload["status"] == "ok"
    findings = {f["identifier"]: f for f in payload["data"]["findings"]}

    collapsed = findings["warehouse.main.orders"]
    assert collapsed["severity"] == "high"
    assert collapsed["data"]["row_count_before"] == 200
    assert collapsed["data"]["row_count_after"] == 20
    assert collapsed["data"]["change_fraction"] == -0.9
    assert collapsed["exact"] is True

    emptied = findings["warehouse.main.stg_orders"]
    assert emptied["severity"] == "high"
    assert "emptied" in emptied["detail"]
    # stg_orders is the built model itself, so its metrics are on the line.
    assert emptied["impacted_models"] == ["stg_orders"]
    assert emptied["impacted_metrics"] == ["order_volume", "revenue"]

    # +1 row on customers is load chatter, below the reporting threshold.
    assert "warehouse.main.customers" not in findings


def test_axis_results_merge_in_drift_json(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "DELETE FROM orders WHERE order_id > 20",
        "ALTER TABLE customers ADD COLUMN phone VARCHAR",
    )
    maintain_repo.dex("maintain", "schema")
    maintain_repo.dex("maintain", "volume")

    report = DexStore(maintain_repo.root).load_drift()
    assert set(report.axes) == {"schema", "volume"}
    assert report.axes["schema"].findings and report.axes["volume"].findings

    # Accepting the new state means re-mapping and re-snapshotting (the
    # documented discipline); that invalidates the report, so axes measured
    # against the old baseline drop rather than lingering as stale findings.
    _rc, payload = maintain_repo.dex("explore", "map")
    assert payload["status"] == "ok"
    maintain_repo.snapshot()
    _rc, payload = maintain_repo.dex("maintain", "volume")
    assert payload["data"]["finding_count"] == 0
    report = DexStore(maintain_repo.root).load_drift()
    assert set(report.axes) == {"volume"}
