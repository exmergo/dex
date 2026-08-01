"""maintain schema: structural drift from metadata, traced to models and metrics."""

from __future__ import annotations

from exmergo_dex_core.storage import FilesystemStore


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


def test_dropped_relation_does_not_resurrect_as_a_false_table_dropped(maintain_repo):
    """#149: explore map's carry-forward must not resurrect a relation
    deleted from the warehouse; maintain snapshot then correctly excludes it
    from the baseline too, so maintain check reports no false table_dropped
    for something the operator deliberately removed (outside dex, mirroring
    a dbt run-operation)."""

    maintain_repo.snapshot()  # baseline: customers/orders/stg_orders all present

    maintain_repo.sql("DROP TABLE customers")

    # Re-map: the buggy carry-forward would resurrect `customers` here.
    rc, payload = maintain_repo.dex("explore", "map")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["dropped_count"] == 1

    maintain_repo.snapshot()  # re-pin: must not include the resurrected ghost

    _rc, payload = maintain_repo.dex("maintain", "schema")
    by_code = _by_code(payload)
    assert "table_dropped" not in by_code


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
    report = FilesystemStore(maintain_repo.root).load_drift()
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


def test_orphan_relation_is_flagged_when_model_removed(maintain_repo):
    maintain_repo.snapshot()
    (maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql").unlink()

    _rc, payload = maintain_repo.dex("maintain", "schema")
    by_code = _by_code(payload)
    orphan = by_code["orphan_relation"][0]
    assert orphan["identifier"] == "warehouse.main.stg_orders"
    assert orphan["severity"] == "medium"
    assert "DROP TABLE" in orphan["data"]["drop_statement"]
    # It already existed at snapshot time, so it must never double-report as
    # table_added, and the physical table is untouched, so never table_dropped.
    assert "table_added" not in by_code
    assert "table_dropped" not in by_code


def test_orphan_relation_not_reported_for_never_backed_table(maintain_repo):
    """A table with no baseline backing (never in transform_layer.models or
    .sources) keeps today's table_added behavior -- orphan_relation is
    strictly for identifiers backed at the baseline."""

    maintain_repo.snapshot()
    maintain_repo.sql("CREATE TABLE payments (id INTEGER, amount DOUBLE)")

    _rc, payload = maintain_repo.dex("maintain", "schema")
    by_code = _by_code(payload)
    assert by_code["table_added"][0]["identifier"] == "warehouse.main.payments"
    assert "orphan_relation" not in by_code


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


# --- unknown columns are not absent columns --------------------------------------


def test_a_thin_baseline_reports_no_columns_rather_than_all_of_them(maintain_repo):
    """The measured failure: 204 objects cached, 46 with column detail, 1,548
    false `column_added`, making a fresh baseline 4x noisier than the week-stale
    one it replaced.

    An object the baseline holds no columns for has an *unknown* column set, not
    an empty one. Diffing live columns against empty reported every column of
    every unprofiled object as newly added, which is a confident wrong answer
    where silence was correct.
    """

    maintain_repo.dex("explore", "map")
    thin = maintain_repo.strip_column_detail(".customers")
    maintain_repo.snapshot()

    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0 and payload["status"] == "ok"
    # customers has four columns, so the bug produced four findings here.
    assert payload["data"]["finding_count"] == 0
    # And the silence is reported rather than merely quiet: an axis that could
    # not compare must not look like one that compared and found nothing.
    coverage = [w for w in payload["warnings"] if "without column detail" in w]
    assert len(coverage) == 1
    assert thin[0] in coverage[0]
    assert "explore map --full" in coverage[0]


def test_a_thin_baseline_still_detects_table_level_drift(maintain_repo):
    """Skipping the column comparison must not disarm the axis wholesale:
    identity does not need a profile."""

    maintain_repo.dex("explore", "map")
    maintain_repo.strip_column_detail(".customers", ".orders", ".stg_orders")
    maintain_repo.snapshot()
    maintain_repo.sql("CREATE TABLE promos (id INTEGER, code VARCHAR)")

    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0
    codes = _by_code(payload)
    assert [f["identifier"] for f in codes["table_added"]] == ["warehouse.main.promos"]
    assert "column_added" not in codes


def test_a_covered_baseline_still_reports_column_drift(maintain_repo):
    """The regression guard on the fix: only *uncovered* objects go quiet."""

    maintain_repo.dex("explore", "map")
    maintain_repo.strip_column_detail(".customers")
    maintain_repo.snapshot()
    maintain_repo.sql("ALTER TABLE orders ADD COLUMN channel VARCHAR")
    maintain_repo.sql("ALTER TABLE customers ADD COLUMN loyalty_tier VARCHAR")

    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0
    added = _by_code(payload)["column_added"]
    # orders was profiled, so its new column is real drift. customers was not,
    # so its new column is unknowable and reporting it would be a guess.
    assert [(f["identifier"], f["column"]) for f in added] == [
        ("warehouse.main.orders", "channel")
    ]


def test_a_repin_cannot_silence_the_cache_age_warning(maintain_repo):
    """The sharpest half of the staleness bug.

    The old check compared write times, so re-pinning made the baseline the
    newer file and the warning vanished, while the baseline's contents were
    still that same old cache. The signal disappeared at the moment it became
    most needed, right after an operator was told their accept succeeded.
    """

    maintain_repo.backdate_cache("2020-01-01T02:00:00+00:00")
    maintain_repo.snapshot()

    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0
    aged = [w for w in payload["warnings"] if "captured" in w and "freshness" in w]
    assert len(aged) == 1
    # The write-time comparison is now quiet (the baseline IS newer), which is
    # exactly why the contents-age check has to be a separate signal.
    assert not [w for w in payload["warnings"] if "newer than the drift baseline" in w]


def test_a_fresh_cache_makes_no_age_claim(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "schema")
    assert rc == 0
    assert not [w for w in payload["warnings"] if "freshness" in w]
