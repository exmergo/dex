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
    plan = GrainPlan(
        key_checks=[(dataset, ["id"], 1200)], fanout_pairs=[], composite_checks=[]
    )
    findings = grain_drift(EstimatingAdapter(), plan)
    assert findings == []


# --- composite keys ---------------------------------------------------------------


class _ComboAdapter:
    """Stub for the composite path: serves metadata for one two-column table
    and answers combination probes with a configured count."""

    name = "stub"
    dialect = "duckdb"

    def __init__(self, rows: int, combo_count: int | None):
        self.rows = rows
        self.combo_count = combo_count
        self.combo_calls: list[list[list[str]]] = []

    def list_objects(self, *, include_views: bool = True):
        meta, _ = self.table_metadata("db.s.line_items")
        return [meta]

    def table_metadata(self, identifier):
        from exmergo_dex_core.adapters.base import ColumnMeta, ObjectMeta

        meta = ObjectMeta(
            identifier=identifier,
            object_type="table",
            schema="s",
            name="line_items",
            row_count=self.rows,
            byte_size=None,
            column_count=2,
        )
        columns = [
            ColumnMeta(name=n, data_type="INTEGER", nullable=False, ordinal=i)
            for i, n in enumerate(["order_key", "line_number"])
        ]
        return meta, columns

    def exact_distinct_counts(self, identifier, columns):
        raise AssertionError(
            f"composite members must never be checked one at a time: {columns}"
        )

    def distinct_combination_counts(self, identifier, combinations):
        self.combo_calls.append([list(c) for c in combinations])
        return {tuple(c): self.combo_count for c in combinations}


def _composite_snapshot():
    from exmergo_dex_core.cache import Dataset
    from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline

    dataset = Dataset(
        identifier="db.s.line_items",
        candidate_keys=[["order_key", "line_number"]],
        grain=["order_key", "line_number"],
        composite_keys=[["order_key", "line_number"]],
    )
    snap = Snapshot.model_construct(
        warehouse=WarehouseBaseline.model_construct(
            datasets=[dataset], relationships=[]
        )
    )
    return dataset, snap


def test_composite_grain_plans_the_combination_never_the_members():
    from exmergo_dex_core.maintain.drift import grain_plan

    _dataset, snap = _composite_snapshot()
    plan = grain_plan(_ComboAdapter(rows=1000, combo_count=1000), snap)
    assert plan.key_checks == []
    assert len(plan.composite_checks) == 1
    _ds, combos, rows = plan.composite_checks[0]
    assert combos == [["order_key", "line_number"]]
    assert rows == 1000


def test_composite_key_lost_uniqueness_is_detected():
    from exmergo_dex_core.maintain.drift import GrainPlan, grain_drift

    dataset, _snap = _composite_snapshot()
    plan = GrainPlan(
        key_checks=[],
        fanout_pairs=[],
        composite_checks=[(dataset, [["order_key", "line_number"]], 1000)],
    )
    adapter = _ComboAdapter(rows=1000, combo_count=950)
    findings = grain_drift(adapter, plan)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "key_lost_uniqueness"
    assert finding.column == "order_key, line_number"
    assert "no longer unique" in finding.detail
    assert finding.data == {
        "columns": ["order_key", "line_number"],
        "distinct_count": 950,
        "row_count": 1000,
        "was_grain": True,
    }


def test_composite_check_is_quiet_when_the_key_still_holds():
    from exmergo_dex_core.maintain.drift import GrainPlan, grain_drift

    dataset, _snap = _composite_snapshot()
    plan = GrainPlan(
        key_checks=[],
        fanout_pairs=[],
        composite_checks=[(dataset, [["order_key", "line_number"]], 1000)],
    )
    findings = grain_drift(_ComboAdapter(rows=1000, combo_count=1000), plan)
    assert findings == []


def test_adapter_without_combination_counts_skips_composite_checks():
    from exmergo_dex_core.maintain.drift import GrainPlan, grain_drift

    dataset, _snap = _composite_snapshot()
    plan = GrainPlan(
        key_checks=[],
        fanout_pairs=[],
        composite_checks=[(dataset, [["order_key", "line_number"]], 1000)],
    )
    adapter = _ComboAdapter(rows=1000, combo_count=950)
    adapter.distinct_combination_counts = None  # shadow: adapter can't probe
    assert grain_drift(adapter, plan) == []


def test_composite_grain_drift_end_to_end(dex, tmp_path):
    """A composite-grain fact table drifts: after the baseline, a duplicated
    (order_key, line_number) row must surface as one combination-level finding,
    with no per-member noise."""

    import duckdb

    root = tmp_path / "composite"
    root.mkdir()
    db_path = root / "warehouse.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE line_items AS "
        "SELECT o.range::INTEGER AS order_key, l.range::INTEGER AS line_number, "
        "(l.range % 2)::INTEGER AS quantity "
        "FROM range(1, 501) o, range(1, 5) l"
    )
    conn.close()
    (root / ".dex").mkdir()
    (root / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {db_path}\n", encoding="utf-8"
    )
    rc, _payload = dex("--repo-root", str(root), "explore", "map")
    assert rc == 0
    rc, _payload = dex("--repo-root", str(root), "maintain", "snapshot")
    assert rc == 0

    conn = duckdb.connect(str(db_path))
    conn.execute("INSERT INTO line_items VALUES (1, 1, 1)")
    conn.close()

    rc, payload = dex("--repo-root", str(root), "maintain", "grain")
    assert rc == 0 and payload["status"] == "ok"
    findings = [
        f for f in payload["data"]["findings"] if f["code"] == "key_lost_uniqueness"
    ]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["column"] == "order_key, line_number"
    assert finding["data"]["columns"] == ["order_key", "line_number"]
    assert finding["data"]["distinct_count"] == 2000
    assert finding["data"]["row_count"] == 2001


def test_grain_estimate_prices_composite_checks():
    from exmergo_dex_core.maintain.drift import GrainPlan, grain_estimate

    dataset, _snap = _composite_snapshot()
    plan = GrainPlan(
        key_checks=[],
        fanout_pairs=[],
        composite_checks=[(dataset, [["order_key", "line_number"]], 1000)],
    )
    priced: list[str] = []

    adapter = _ComboAdapter(rows=1000, combo_count=1000)
    adapter.query_estimate = lambda sql: priced.append(sql) or 7.0
    total, per_table = grain_estimate(adapter, plan)
    assert total == 7.0
    assert per_table == {"db.s.line_items": 7.0}
    assert len(priced) == 1
    assert "SELECT DISTINCT" in priced[0]
