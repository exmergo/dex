"""maintain check: the all-axis sweep, ranked by blast radius."""

from __future__ import annotations

from exmergo_dex_core.cache import DexStore

from .conftest import SEMANTIC_YAML


def test_check_requires_a_snapshot(maintain_repo):
    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 1 and payload["status"] == "error"
    assert "maintain snapshot" in payload["errors"][0]


def test_clean_world_reports_every_axis_clean(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "check")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0
    assert payload["data"]["axes"] == {
        "schema": 0,
        "volume": 0,
        "grain": 0,
        "semantic": 0,
    }
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
    assert axes["schema"] == 1
    assert axes["volume"] == 1
    # Emptying most of stg_orders also orphans the verified orders -> stg_orders
    # join, so grain reports the lost key and the moved joins.
    assert axes["grain"] >= 2
    assert axes["semantic"] == 1

    findings = payload["data"]["findings"]
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

    report = DexStore(maintain_repo.root).load_drift()
    assert set(report.axes) == {"schema", "volume", "grain", "semantic"}


def test_check_warns_when_cache_outruns_snapshot(maintain_repo):
    maintain_repo.snapshot()
    _rc, payload = maintain_repo.dex("explore", "map")
    assert payload["status"] == "ok"

    _rc, payload = maintain_repo.dex("maintain", "check")
    assert any("re-run `maintain snapshot`" in w for w in payload["warnings"])


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
