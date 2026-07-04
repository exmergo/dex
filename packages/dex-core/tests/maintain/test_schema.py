"""maintain schema: structural drift from metadata, traced to models and metrics."""

from __future__ import annotations

from exmergo_dex_core.cache import DexStore


def _by_code(payload: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for finding in payload["data"]["findings"]:
        grouped.setdefault(finding["code"], []).append(finding)
    return grouped


def test_clean_warehouse_reports_no_drift(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0
    assert payload["data"]["axes_run"] == ["schema"]


def test_schema_requires_a_snapshot(maintain_repo):
    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 1 and payload["status"] == "error"
    assert "maintain snapshot" in payload["errors"][0]


def test_induced_column_drift_is_detected_and_traced(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "ALTER TABLE customers ADD COLUMN phone VARCHAR",
        "ALTER TABLE customers DROP COLUMN email",
        "ALTER TABLE orders ALTER amount TYPE VARCHAR",
    )

    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0 and payload["status"] == "ok"
    by_code = _by_code(payload)

    added = by_code["column_added"][0]
    assert added["identifier"].endswith(".customers") and added["column"] == "phone"
    assert added["severity"] == "low"

    dropped = by_code["column_dropped"][0]
    assert dropped["column"] == "email" and dropped["severity"] == "high"

    # email (VARCHAR) out, phone (VARCHAR) in: metadata cannot tell a rename
    # from a drop+add, so the possibility is surfaced as a hint.
    rename = by_code["possible_rename"][0]
    assert rename["data"] == {"renamed_from": "email", "renamed_to": "phone"}

    retyped = by_code["column_retyped"][0]
    assert retyped["identifier"].endswith(".orders") and retyped["column"] == "amount"
    assert retyped["data"]["type_before"] == "DOUBLE"
    # amount feeds stg_orders, whose measure order_amount feeds revenue; the
    # count-based order_volume is untouched by an amount change.
    assert retyped["impacted_models"] == ["stg_orders"]
    assert retyped["impacted_metrics"] == ["revenue"]

    # Ranked by severity: every high finding before the low ones.
    severities = [f["severity"] for f in payload["data"]["findings"]]
    assert severities == sorted(severities, key=["high", "medium", "low"].index)

    # The report is persisted for reconcile.
    report = DexStore(maintain_repo.root).load_drift()
    assert {f.code for f in report.axes["schema"].findings} >= {
        "column_added",
        "column_dropped",
        "column_retyped",
    }


def test_dropped_source_table_is_flagged_dangling(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("DROP TABLE orders")

    _rc, payload = maintain_repo.dex("maintain", "schema")
    by_code = _by_code(payload)
    assert by_code["table_dropped"][0]["identifier"] == "warehouse.main.orders"
    dangling = by_code["dangling_source"][0]
    assert dangling["identifier"] == "main.orders"
    assert dangling["severity"] == "high"
    assert dangling["data"]["declared_in"] == "models/staging/_dex_sources.yml"


def test_new_table_is_reported_added(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("CREATE TABLE payments (id INTEGER, amount DOUBLE)")

    _rc, payload = maintain_repo.dex("maintain", "schema")
    by_code = _by_code(payload)
    assert by_code["table_added"][0]["identifier"] == "warehouse.main.payments"
    assert by_code["table_added"][0]["severity"] == "low"


def test_scope_filters_findings_to_named_objects(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "ALTER TABLE customers ADD COLUMN phone VARCHAR",
        "ALTER TABLE orders ALTER amount TYPE VARCHAR",
    )

    rc, payload = maintain_repo.dex("maintain", "schema", "customers")
    identifiers = {f["identifier"] for f in payload["data"]["findings"]}
    assert identifiers == {"warehouse.main.customers"}

    rc, payload = maintain_repo.dex("maintain", "schema", "no_such_table")
    assert rc == 1 and payload["status"] == "error"
    assert "no_such_table" in payload["errors"][0]
