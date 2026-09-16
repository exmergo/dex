"""maintain volume: freshness drift from free row-count metadata."""

from __future__ import annotations

from exmergo_dex_core.storage import FilesystemStore


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

    report = FilesystemStore(maintain_repo.root).load_drift()
    assert set(report.axes) == {"schema", "volume"}
    assert report.axes["schema"].findings and report.axes["volume"].findings

    # Accepting the new state means re-mapping and re-snapshotting (the
    # documented discipline); that invalidates the report, so axes measured
    # against the old baseline drop rather than lingering as stale findings.
    # --refresh forces a full re-profile: skip-if-cached would otherwise reuse
    # the still-fresh, schema-unchanged profiles (including orders' pre-DELETE
    # row count), which is precisely the volume-only change reuse cannot see.
    _rc, payload = maintain_repo.dex("explore", "map", "--refresh")
    assert payload["status"] == "ok"
    maintain_repo.snapshot()
    _rc, payload = maintain_repo.dex("maintain", "volume")
    assert payload["data"]["finding_count"] == 0
    report = FilesystemStore(maintain_repo.root).load_drift()
    assert set(report.axes) == {"volume"}


# --- what the axis could not compare, said out loud ------------------------------


def _volume_snapshot(**counts):
    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

    return Snapshot(
        created_at="2024-01-01T00:00:00",
        warehouse=WarehouseBaseline(
            datasets=[
                Dataset(
                    identifier=name,
                    row_count=count,
                    profiled_at="2024-01-01T00:00:00",
                )
                for name, count in counts.items()
            ]
        ),
    )


def test_an_object_with_no_live_row_count_is_named_rather_than_skipped_silently():
    """An absent finding reads as "checked, and nothing moved", so an axis that
    declines to check an object has to say which one.

    Reachable wherever the warehouse maintains no count: a view everywhere, and
    on BigQuery an external table too, now that the metadata zero those used to
    carry is correctly read as unknown.
    """

    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.drift import uncomparable_volume, volume_drift

    snap = _volume_snapshot(**{"p.d.ext_orders": 500, "p.d.orders": 200})
    current = [
        Dataset(identifier="p.d.ext_orders", row_count=None),
        Dataset(identifier="p.d.orders", row_count=20),
    ]

    # The comparison it can make, it still makes.
    findings = volume_drift(current, snap)
    assert [f.identifier for f in findings] == ["p.d.orders"]

    notes = uncomparable_volume(current, snap)
    assert len(notes) == 1
    assert "p.d.ext_orders" in notes[0]
    assert "p.d.orders" not in notes[0]
    assert "explore profile" in notes[0]


def test_an_object_that_never_had_a_baseline_count_is_not_worth_naming():
    """The note exists for the object a reader expects to be covered: one whose
    baseline holds a real count. Something never counted on either side has
    nothing to compare and no expectation to correct."""

    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.drift import uncomparable_volume

    snap = _volume_snapshot(**{"p.d.a_view": None})
    current = [Dataset(identifier="p.d.a_view", row_count=None)]
    assert uncomparable_volume(current, snap) == []
