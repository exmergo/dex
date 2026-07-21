"""maintain reconcile: findings -> proposals (mechanical vs advisory) -> a plan
applied through `transform apply`, never written by reconcile itself."""

from __future__ import annotations


def _proposals_by_axis(payload: dict) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for proposal in payload["data"]["proposals"]:
        grouped.setdefault(proposal["axis"], []).append(proposal)
    return grouped


def test_drift_added_column_honors_pii_override():
    """A drift-added column gets a name-based flag at base confidence (no
    aggregates exist yet, so it blocks until the next profile); an override
    clears it with the audit recorded."""

    from exmergo_dex_core.cache import ColumnProfile, Dataset
    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.maintain.reconcile import _patched_dataset

    base = Dataset(
        identifier="db.main.orders",
        columns=[ColumnProfile(name="id", data_type="INTEGER")],
    )
    finding = DriftFinding(
        axis="schema",
        code="column_added",
        identifier="db.main.orders",
        column="customer_name",
        detail="column customer_name added",
        data={"data_type": "VARCHAR"},
    )

    plain = _patched_dataset(base, [finding], set())
    added = next(c for c in plain.columns if c.name == "customer_name")
    assert added.pii is not None and added.pii.confidence == 0.6

    cleared = _patched_dataset(base, [finding], {"db.main.orders.customer_name"})
    added = next(c for c in cleared.columns if c.name == "customer_name")
    assert added.pii is None
    assert added.pii_overridden is not None


def test_drift_added_column_honors_pattern_pii_override():
    """A pattern-form override (column_name + scope) reaches drift-added
    columns the same way an exact override does: this is the path
    `maintain/commands.py` feeds through `pii_override_paths()`."""

    from exmergo_dex_core.cache import ColumnProfile, Dataset
    from exmergo_dex_core.config import PIIOverride, pii_override_paths
    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.maintain.reconcile import _patched_dataset

    base = Dataset(
        identifier="db.raw_orders_qa",
        columns=[ColumnProfile(name="id", data_type="INTEGER")],
    )
    finding = DriftFinding(
        axis="schema",
        code="column_added",
        identifier="db.raw_orders_qa",
        column="customer_name",
        detail="column customer_name added",
        data={"data_type": "VARCHAR"},
    )
    matcher = pii_override_paths(
        [PIIOverride(column_name="customer_name", scope="db.raw_*")]
    )

    cleared = _patched_dataset(base, [finding], matcher)
    added = next(c for c in cleared.columns if c.name == "customer_name")
    assert added.pii is None
    assert added.pii_overridden is not None


def test_reconcile_needs_a_drift_report(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert rc == 1 and payload["status"] == "error"
    assert "maintain check" in payload["errors"][0]


def test_no_drift_reconciles_to_nothing(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.dex("maintain", "check")
    rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["proposal_count"] == 0
    assert "no drift" in payload["data"]["hint"]


def test_schema_drift_is_mechanical_and_rescaffolds(maintain_repo):
    # Give reconcile a dex-scaffolded staging model to rebuild.
    rc, payload = maintain_repo.dex(
        "transform", "plan", "--scaffold", "orders", "scaffold stg_orders"
    )
    assert payload["status"] == "ok"
    maintain_repo.dex("transform", "apply", payload["data"]["plan_id"])
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    maintain_repo.sql(
        "ALTER TABLE orders ADD COLUMN discount DOUBLE",
        "ALTER TABLE orders DROP status",
    )
    maintain_repo.dex("maintain", "schema")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    assert rc == 0 and payload["status"] == "ok"
    proposal = _proposals_by_axis(payload)["schema"][0]
    assert proposal["kind"] == "mechanical"
    assert "models/staging/stg_orders.sql" in proposal["paths"]

    # The re-scaffolded model reflects the drift: discount in, status out.
    sql_diff = next(
        d for d in payload["diffs"] if d["path"] == "models/staging/stg_orders.sql"
    )
    assert "discount" in sql_diff["unified"]
    assert any(
        line.startswith("-") and "status" in line
        for line in sql_diff["unified"].splitlines()
    )

    # Reconcile proposes; it does not write. The model on disk is untouched
    # until transform apply runs.
    assert (
        "discount"
        not in (
            maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql"
        ).read_text()
    )

    plan_id = payload["data"]["plan_id"]
    assert f"transform apply {plan_id}" in payload["data"]["hint"]
    rc, applied = maintain_repo.dex("transform", "apply", plan_id)
    assert applied["status"] == "ok"
    assert (
        "discount"
        in (
            maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql"
        ).read_text()
    )


def test_grain_drift_is_advisory_with_a_visibility_test(maintain_repo):
    # A staging model whose key carries no unique test (a common omission):
    # the break would pass builds silently until reconcile proposes the test.
    maintain_repo.edit(
        "models/staging/stg_orders.yml",
        "version: 2\n"
        "models:\n"
        "  - name: stg_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        tests: [not_null]\n",
    )
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    maintain_repo.dex("maintain", "grain")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok"
    proposal = next(
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "key_lost_uniqueness"
    )
    assert proposal["kind"] == "advisory"
    assert "decide" in proposal["action"]
    # The advisory is backed by a unique test that makes the break visible in
    # builds, but the dedup decision stays with the human.
    yml_diff = next(
        d for d in payload["diffs"] if d["path"] == "models/staging/stg_orders.yml"
    )
    assert "unique" in yml_diff["unified"]


def test_grain_drift_adds_no_test_when_one_already_alerts(maintain_repo):
    # The fixture's stg_orders.yml already tests order_id for uniqueness, so a
    # broken key already fails builds; reconcile does not add a redundant test.
    maintain_repo.snapshot()
    maintain_repo.sql("INSERT INTO orders SELECT * FROM orders WHERE order_id <= 10")
    maintain_repo.dex("maintain", "grain")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "grain")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["mechanical_count"] == 0
    assert payload["diffs"] == []


def test_semantic_and_volume_drift_are_advisory_only(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "DELETE FROM orders WHERE order_id > 20",
        "INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', NULL)",
    )
    maintain_repo.dex("maintain", "check")

    rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["mechanical_count"] == 0
    assert payload["data"]["advisory_count"] >= 2
    # Nothing to apply: no plan is minted when every proposal is advisory
    # without a backing test edit.
    assert "plan_id" not in payload["data"]
    assert payload["diffs"] == []

    by_axis = _proposals_by_axis(payload)
    assert all(p["kind"] == "advisory" for p in by_axis["volume"])
    assert all(p["kind"] == "advisory" for p in by_axis["semantic"])


def test_reconcile_conflict_surfaces_at_apply_not_reconcile(maintain_repo):
    _rc, payload = maintain_repo.dex(
        "transform", "plan", "--scaffold", "orders", "scaffold stg_orders"
    )
    maintain_repo.dex("transform", "apply", payload["data"]["plan_id"])
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    maintain_repo.sql("ALTER TABLE orders ADD COLUMN discount DOUBLE")
    maintain_repo.dex("maintain", "schema")
    _rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    plan_id = payload["data"]["plan_id"]

    # A human edits the model after reconcile planned against it.
    maintain_repo.edit(
        "models/staging/stg_orders.sql", "select 1 as id -- hand-tuned\n"
    )
    _rc, apply1 = maintain_repo.dex("transform", "apply", plan_id)
    assert apply1["status"] == "needs_confirmation"
    assert apply1["data"]["conflicts"]
    assert (
        "hand-tuned"
        in (
            maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql"
        ).read_text()
    )

    _rc, apply2 = maintain_repo.dex("transform", "apply", plan_id, "--confirm")
    assert apply2["status"] == "ok"
    assert apply2["data"]["conflicts_overridden"]


def test_reconcile_warns_on_stale_drift_report(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("ALTER TABLE customers ADD COLUMN phone VARCHAR")
    maintain_repo.dex("maintain", "schema")
    # Re-snapshot after the check: the report now predates the baseline.
    maintain_repo.dex("explore", "map")
    maintain_repo.snapshot()

    _rc, payload = maintain_repo.dex("maintain", "reconcile")
    assert any("older snapshot" in w for w in payload["warnings"])


def test_dropped_source_reconcile_is_advisory_when_no_scaffold(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("DROP TABLE orders")
    maintain_repo.dex("maintain", "schema")

    rc, payload = maintain_repo.dex("maintain", "reconcile", "schema")
    assert rc == 0 and payload["status"] == "ok"
    # stg_orders exists but was hand-written (not the dex scaffold shape); the
    # table_dropped / dangling_source findings are advisory regardless.
    assert all(p["kind"] == "advisory" for p in payload["data"]["proposals"])
    codes = {p["finding_code"] for p in payload["data"]["proposals"]}
    assert "dangling_source" in codes or "table_dropped" in codes
