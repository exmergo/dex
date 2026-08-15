"""maintain check: the all-axis sweep, ranked by blast radius."""

from __future__ import annotations

import json

from exmergo_dex_core.storage import FilesystemStore

from .conftest import SEMANTIC_YAML


def test_check_requires_a_snapshot(maintain_repo):
    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 1 and payload["status"] == "error"
    assert "maintain snapshot" in payload["errors"][0]
    # #170: a machine-readable reason alongside the prose.
    assert payload["reason"] == "prerequisite"


def test_clean_world_reports_every_axis_clean(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0
    axes = payload["data"]["axes"]
    assert {axis: result["finding_count"] for axis, result in axes.items()} == {
        "schema": 0,
        "volume": 0,
        "grain": 0,
        "semantic": 0,
    }
    assert all(result["findings"] == [] for result in axes.values())
    assert all(result["scope"] is None for result in axes.values())
    assert payload["data"]["axes_run"] == ["grain", "schema", "semantic", "volume"]


def test_check_sweeps_all_axes_and_ranks_by_blast_radius(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "ALTER TABLE customers ADD COLUMN phone VARCHAR",
        "DELETE FROM stg_orders WHERE order_id > 20",
        "INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10",
    )
    maintain_repo.edit(
        "models/marts/orders_semantic.yml",
        SEMANTIC_YAML.replace("label: Revenue", "label: Gross revenue"),
    )

    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 0 and payload["status"] == "ok"
    axes = payload["data"]["axes"]
    assert axes["schema"]["finding_count"] == 1
    assert axes["volume"]["finding_count"] == 1
    # Emptying most of stg_orders also orphans the verified orders -> stg_orders
    # join, so grain reports the lost key and the moved joins.
    assert axes["grain"]["finding_count"] >= 2
    assert axes["semantic"]["finding_count"] == 1

    findings = payload["data"]["findings"]
    axis_findings = [
        finding for result in axes.values() for finding in result["findings"]
    ]

    assert sorted(json.dumps(finding, sort_keys=True) for finding in axis_findings) == (
        sorted(json.dumps(finding, sort_keys=True) for finding in findings)
    )
    assert all(
        result["finding_count"] == len(result["findings"]) for result in axes.values()
    )
    codes = {f["code"] for f in findings}
    assert codes == {
        "column_added",
        "row_count_changed",
        "key_lost_uniqueness",
        "join_orphans_increased",
        "definition_changed",
    }
    severities = [f["severity"] for f in findings]
    assert severities == sorted(severities, key=["high", "medium", "low"].index)
    assert "reconcile" in payload["data"]["hint"]

    report = FilesystemStore(maintain_repo.root).load_drift()
    assert set(report.axes) == {"schema", "volume", "grain", "semantic"}


def test_check_warns_when_cache_outruns_snapshot(maintain_repo):
    maintain_repo.snapshot()
    _rc, payload = maintain_repo.dex("explore", "map")
    assert payload["status"] == "ok"

    _rc, payload = maintain_repo.dex("maintain", "check")
    stale = [w for w in payload["warnings"] if "newer than the drift baseline" in w]
    assert len(stale) == 1
    # The advice has to name both commands. `maintain snapshot` alone re-pins
    # whatever the cache holds, so on a warehouse past the rank cutoff following
    # this literally is what corrupts the baseline: the cheap path and the
    # correct path are opposites, and this warning used to point at the cheap one.
    assert "explore map --full" in stale[0]
    assert "maintain snapshot" in stale[0]


def test_scope_narrows_every_axis_including_the_paid_ones(maintain_repo):
    """#115: an unscoped check's grain/cardinality estimate covers everything;
    a scoped one should price and report only the named object(s), across
    every axis, not just the free ones."""

    maintain_repo.snapshot()
    # Grain drift on customers; semantic cardinality drift on stg_orders. Two
    # unrelated tables, so scoping to one must exclude the other's findings.
    maintain_repo.sql("INSERT INTO customers SELECT * FROM customers WHERE id <= 5")
    maintain_repo.sql(
        "INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', DATE '2024-03-01')"
    )

    rc, payload = maintain_repo.dex("maintain", "check", "customers")
    assert rc == 0 and payload["status"] == "ok"
    axes = payload["data"]["axes"]
    assert axes["schema"]["finding_count"] == 0
    assert axes["semantic"]["finding_count"] == 0
    assert axes["grain"]["finding_count"] >= 1
    assert all(result["scope"] == ["customers"] for result in axes.values())
    assert len({result["run_at"] for result in axes.values()}) == 1

    codes = {f["code"] for f in payload["data"]["findings"]}
    assert "key_lost_uniqueness" in codes
    assert "dimension_cardinality_changed" not in codes

    report = FilesystemStore(maintain_repo.root).load_drift()
    assert report.axes["grain"].scope == ["customers"]
    assert report.axes["semantic"].scope == ["customers"]


def test_check_without_project_skips_semantic_with_warning(dex, tmp_path):
    import duckdb

    root = tmp_path / "bare"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE items (id INTEGER, label VARCHAR)")
    conn.execute("INSERT INTO items VALUES (1, 'a')")
    conn.close()
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )
    dex("--repo-root", str(root), "maintain", "snapshot")

    rc, payload = dex("--repo-root", str(root), "maintain", "check")
    assert rc == 0 and payload["status"] == "ok"
    assert "semantic" not in payload["data"]["axes"]
    warnings = " ".join(payload["warnings"])
    assert "semantic axis skipped" in warnings
    assert "metadata-only" in warnings
