"""`maintain` under `semantic.vendor: snowflake` (#1293).

The views held to the warehouse schema dex captured (the vendor schema axis),
`maintain reconcile` cutting the removals out of the view files through
`semantic snowflake update`, the reconciled view read back by the reader, the
fingerprint's drift and baselines, and a declared key the data contradicts
worded as the Semantic View's claim. Nothing opens a Snowflake connection or
deploys.

The command tests run on a real DuckDB warehouse, so they skip without one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cli import main
from exmergo_dex_core.semantic_views.model import FACT, METRIC, TIME_DIMENSION, Member
from exmergo_dex_core.semantic_views.removal import apply_removal
from exmergo_dex_core.semantic_views.schema import needed_kind

from .conftest import repo, sv, table

HEADER = "# sales, kept by hand\n"

SALES_YAML = """\
# sales, kept by hand
name: sales
description: Orders and the customers who placed them.
tables:
  - name: orders
    base_table:
      database: demo
      schema: main
      table: orders
    primary_key:
      columns: [order_id]
    dimensions:
      - name: status  # where the order is
        expr: status
      - name: status_label
        expr: orders.status
      - name: note
        expr: note
        data_type: VARCHAR
      - name: customer_region
        expr: customers.region
    time_dimensions:
      # the day an order was placed
      - name: placed_on
        expr: placed_at
        data_type: DATE
    facts:
      - name: amount
        expr: amount
    metrics:
      - name: revenue
        expr: SUM(orders.amount)
      - name: order_count
        expr: COUNT(order_id)
  - name: customers
    base_table:
      database: demo
      schema: main
      table: customers
    primary_key:
      columns: [customer_key]
    unique_keys:
      - columns: [email]
    dimensions:
      - name: region
        expr: region
    metrics:
      - name: customer_count
        expr: COUNT(customer_key)
relationships:
  - name: orders_customers
    left_table: orders
    right_table: customers
    relationship_columns:
      - left_column: customer_id
        right_column: customer_key
metrics:
  - name: aov
    expr: orders.revenue / orders.order_count
"""


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _run_ok(root: Path, *argv: str) -> bool:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--repo-root", str(root), *argv])
    return rc == 0


def _sql(root: Path, *statements: str) -> None:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(root / "demo.duckdb"))
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


def _layer(root: Path, text: str = SALES_YAML, *, snapshot: bool = True) -> Path:
    """A repository configured for one hand-kept view, mapped and baselined."""

    root = repo(root, sv("sales", table("orders", "demo.main.orders")))
    (root / "views" / "sales.yaml").write_text(text, encoding="utf-8")
    config = root / ".dex" / "config.yml"
    config.write_text(
        config.read_text() + "duckdb:\n  path: demo.duckdb\n", encoding="utf-8"
    )
    if snapshot:
        assert _run_ok(root, "explore", "map")
        assert _run_ok(root, "maintain", "snapshot")
    return root


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    """Orders and customers, the hand-kept view over them, and a baseline pinned
    with every column present."""

    _sql(
        tmp_path,
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "status VARCHAR, note VARCHAR, amount DOUBLE, placed_at DATE)",
        "INSERT INTO orders VALUES (1, 10, 'paid', 'a', 5, '2026-01-01'), "
        "(2, 20, 'open', 'b', 3, '2026-01-02')",
        "CREATE TABLE customers (customer_key INTEGER, region VARCHAR, email VARCHAR)",
        "INSERT INTO customers VALUES (10, 'EU', 'a@x'), (20, 'US', 'b@x')",
    )
    return _layer(tmp_path)


def _drift(capsys, root: Path, *statements: str) -> tuple[list[dict], dict]:
    """Apply ``statements``, refresh the map, and return the schema findings dex
    computed for the view, with the envelope."""

    _sql(root, *statements)
    assert _run_ok(root, "explore", "map", "--refresh")
    rc, envelope = _run(capsys, root, "maintain", "schema")
    assert rc == 0, envelope["errors"]
    found = [
        f for f in envelope["data"]["findings"] if f["code"].startswith("declared")
    ]
    return found, envelope


def _by_code(findings: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for finding in findings:
        grouped.setdefault(finding["code"], []).append(finding)
    return grouped


# --- what a member needs of its column ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "warehouse_type", "agg", "needs"),
    [
        (TIME_DIMENSION, "VARCHAR", None, ("temporal", "a time dimension")),
        (TIME_DIMENSION, "TIMESTAMP_NTZ", None, None),
        (TIME_DIMENSION, "DATE", None, None),
        (METRIC, "VARCHAR", "sum", ("numeric", "SUM")),
        (METRIC, "TEXT", "average", ("numeric", "AVG")),
        (METRIC, "VARCHAR", "count", None),
        (METRIC, "VARCHAR", "max", None),
        (METRIC, "NUMBER(38,0)", "sum", None),
        (FACT, "VARCHAR", None, None),
        (TIME_DIMENSION, "GEOGRAPHY", None, None),
    ],
)
def test_what_a_member_needs_of_its_column(kind, warehouse_type, agg, needs):
    member = Member(kind=kind, name="m", expr="c", table="t")
    assert needed_kind(member, warehouse_type, agg) == needs


# --- the schema axis ------------------------------------------------------------


def test_nothing_drifted_is_nothing_reported(warehouse, capsys):
    found, _ = _drift(capsys, warehouse)
    assert found == []


def test_a_dropped_column_names_the_view_the_element_and_the_column(warehouse, capsys):
    found, _ = _drift(capsys, warehouse, "ALTER TABLE orders DROP COLUMN status")

    grouped = _by_code(found)
    # `status_label` reads the dimension `status`, so it goes with it.
    (cascade,) = grouped["declared_dependency_dropped"]
    assert "tables['orders'].dimensions['status_label']" in " ".join(
        cascade["data"]["reasons"]
    )
    missing = grouped["declared_column_missing"]
    elements = {f["data"]["element"] for f in missing}
    assert elements == {"tables['orders'].dimensions['status']"}
    (status,) = [f for f in missing if f["data"]["declared_member"] == "status"]
    assert status["identifier"] == "demo.main.orders"
    assert status["column"] == "status"
    assert status["data"]["semantic_model"] == "sales"
    assert status["data"]["model_file"] == "views/sales.yaml"
    assert status["severity"] == "high"


def test_a_fact_dropped_takes_the_metrics_that_read_it(warehouse, capsys):
    """`revenue` reads the fact `amount`, and `aov` reads `revenue`."""

    found, _ = _drift(capsys, warehouse, "ALTER TABLE orders DROP COLUMN amount")

    grouped = _by_code(found)
    (missing,) = grouped["declared_column_missing"]
    assert missing["data"]["element"] == "tables['orders'].facts['amount']"
    (cascade,) = grouped["declared_dependency_dropped"]
    reasons = " ".join(cascade["data"]["reasons"])
    for element in ("tables['orders'].metrics['revenue']", "metrics['aov']"):
        assert element in reasons
    removed = {(m["table"], m["name"]) for m in cascade["data"]["removal"]["members"]}
    assert removed == {
        ("orders", "amount"),
        ("orders", "revenue"),
        (None, "aov"),
    }


def test_a_relationship_column_dropped_takes_what_reads_across_it(warehouse, capsys):
    found, _ = _drift(capsys, warehouse, "ALTER TABLE orders DROP COLUMN customer_id")

    grouped = _by_code(found)
    (missing,) = grouped["declared_column_missing"]
    assert missing["data"]["element"] == "relationships['orders_customers']"
    assert missing["column"] == "customer_id"
    (cascade,) = grouped["declared_dependency_dropped"]
    assert cascade["data"]["removal"]["relationships"] == ["orders_customers"]
    assert any("customer_region" in r for r in cascade["data"]["reasons"]), cascade[
        "data"
    ]["reasons"]


def test_a_key_column_dropped_is_held_never_cut(warehouse, capsys):
    """A key is the table's grain: named, and left for a person."""

    found, _ = _drift(capsys, warehouse, "ALTER TABLE customers DROP COLUMN email")

    (missing,) = _by_code(found)["declared_column_missing"]
    assert missing["data"]["element"] == "tables['customers'].unique_keys[0]"
    assert missing["data"]["removal"]["held"][0]["reason"] == "key_reads_dropped"


@pytest.mark.parametrize(
    ("statement", "element", "declared", "severity"),
    [
        (
            "ALTER TABLE orders ALTER placed_at TYPE VARCHAR",
            "tables['orders'].time_dimensions['placed_on']",
            "a time dimension",
            "medium",
        ),
        (
            "ALTER TABLE orders ALTER amount TYPE VARCHAR",
            "tables['orders'].metrics['revenue']",
            "SUM",
            "high",
        ),
        (
            "ALTER TABLE orders ALTER note TYPE INTEGER USING 1",
            "tables['orders'].dimensions['note']",
            "data_type VARCHAR",
            "low",
        ),
    ],
)
def test_a_type_the_column_no_longer_fits_is_a_type_conflict(
    warehouse, capsys, statement, element, declared, severity
):
    found, _ = _drift(capsys, warehouse, statement)

    conflicts = _by_code(found)["declared_type_conflict"]
    (conflict,) = [f for f in conflicts if f["data"]["element"] == element]
    assert conflict["data"]["declared"] == declared
    assert conflict["severity"] == severity


def test_a_data_type_conflict_cuts_the_data_type_and_keeps_the_member(
    warehouse, capsys
):
    found, _ = _drift(
        capsys, warehouse, "ALTER TABLE orders ALTER note TYPE INTEGER USING 1"
    )

    (conflict,) = _by_code(found)["declared_type_conflict"]
    removal = conflict["data"]["removal"]
    assert removal["members"] == []
    assert removal["data_types"] == [
        {"table": "orders", "section": "dimensions", "name": "note"}
    ]


def test_a_time_dimension_over_text_is_advised_and_never_cut(warehouse, capsys):
    """Snowflake still answers for it, so it is left for a person, and the
    rest of the view's removal is still cut."""

    _drift(
        capsys,
        warehouse,
        "ALTER TABLE orders ALTER placed_at TYPE VARCHAR",
        "ALTER TABLE orders DROP COLUMN status",
    )
    rc, envelope = _run(capsys, warehouse, "maintain", "reconcile")
    assert rc == 0, envelope["errors"]

    by_code: dict[str, set[str]] = {}
    for proposal in envelope["data"]["proposals"]:
        if proposal["finding_code"].startswith("declared"):
            by_code.setdefault(proposal["finding_code"], set()).add(proposal["kind"])
    assert by_code["declared_type_conflict"] == {"advisory"}
    assert by_code["declared_column_missing"] == {"mechanical"}
    advice = " ".join(
        p["action"]
        for p in envelope["data"]["proposals"]
        if p["finding_code"] == "declared_type_conflict"
    )
    assert "cast its `expr` to a date or timestamp" in advice
    rc, applied = _run(
        capsys, warehouse, "transform", "apply", envelope["data"]["semantic_plan_id"]
    )
    assert rc == 0, applied["errors"]
    body = yaml.safe_load((warehouse / "views" / "sales.yaml").read_text())
    assert [t["name"] for t in body["tables"][0]["time_dimensions"]] == ["placed_on"]


def test_a_base_table_gone_since_the_baseline_is_the_logical_table(warehouse, capsys):
    found, _ = _drift(capsys, warehouse, "DROP TABLE customers")

    (gone,) = _by_code(found)["declared_relation_missing"]
    assert gone["identifier"] == "demo.main.customers"
    assert gone["data"]["element"] == "tables['customers']"
    assert gone["data"]["removal"]["held"][0]["reason"] == "table_relation_missing"


def test_a_table_neither_side_lists_is_a_note(tmp_path, capsys):
    _sql(
        tmp_path,
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "status VARCHAR, note VARCHAR, amount DOUBLE, placed_at DATE)",
    )
    root = _layer(tmp_path)

    rc, envelope = _run(capsys, root, "maintain", "schema")

    assert rc == 0, envelope["errors"]
    assert not [
        f for f in envelope["data"]["findings"] if f["code"].startswith("declared")
    ]
    assert "neither this connection nor the baseline lists" in json.dumps(envelope)


def test_a_refused_file_is_named_as_not_compared(warehouse, capsys):
    (warehouse / "views" / "sales.yaml").write_text("name: [", encoding="utf-8")

    rc, envelope = _run(capsys, warehouse, "maintain", "schema")

    assert rc == 0, envelope["errors"]
    assert "(not compared to the warehouse)" in json.dumps(envelope)


def test_a_definition_reading_a_dropped_column_is_held(tmp_path, capsys):
    _sql(
        tmp_path,
        "CREATE TABLE orders (order_id INTEGER, status VARCHAR, amount DOUBLE)",
    )
    text = """\
name: sales
tables:
  - name: paid
    base_table:
      definition: SELECT order_id, amount FROM demo.main.orders WHERE status = 'paid'
    primary_key:
      columns: [order_id]
    facts:
      - name: amount
        expr: amount
    metrics:
      - name: revenue
        expr: SUM(paid.amount)
"""
    root = _layer(tmp_path, text)

    found, _ = _drift(capsys, root, "ALTER TABLE orders DROP COLUMN status")

    (missing,) = _by_code(found)["declared_column_missing"]
    assert missing["data"]["element"] == "tables['paid'].base_table.definition"
    rc, reconciled = _run(capsys, root, "maintain", "reconcile")
    assert rc == 0, reconciled["errors"]
    assert reconciled["data"].get("semantic_plan_id") is None
    kinds = {p["kind"] for p in reconciled["data"]["proposals"]}
    assert kinds == {"advisory"}


# --- reconcile -------------------------------------------------------------------


def test_reconcile_cuts_the_removal_and_the_view_round_trips(warehouse, capsys):
    """A `semantic snowflake update` plan, the comments kept, the reconciled
    view read back by the reader, and nothing left drifted."""

    _drift(
        capsys,
        warehouse,
        "ALTER TABLE orders DROP COLUMN amount",
        "ALTER TABLE orders ALTER note TYPE INTEGER USING 1",
    )
    rc, envelope = _run(capsys, warehouse, "maintain", "reconcile")
    assert rc == 0, envelope["errors"]
    plan_id = envelope["data"]["semantic_plan_id"]
    assert plan_id, envelope
    kinds = {
        p["kind"]
        for p in envelope["data"]["proposals"]
        if p["finding_code"].startswith("declared")
    }
    assert kinds == {"mechanical"}

    rc, applied = _run(capsys, warehouse, "transform", "apply", plan_id)
    assert rc == 0, applied["errors"]

    text = (warehouse / "views" / "sales.yaml").read_text()
    assert text.startswith(HEADER)
    assert "# where the order is" in text
    assert "columns: [order_id]" in text
    body = yaml.safe_load(text)
    orders, _customers = body["tables"]
    assert orders["facts"] == []
    assert [m["name"] for m in orders["metrics"]] == ["order_count"]
    assert "data_type" not in orders["dimensions"][2]
    assert "metrics" not in body or body["metrics"] == []

    rc, listed = _run(capsys, warehouse, "explore", "semantic", "list")
    assert rc == 0, listed["errors"]
    rc, again = _run(capsys, warehouse, "maintain", "schema")
    assert not [
        f for f in again["data"]["findings"] if f["code"].startswith("declared")
    ], again["data"]["findings"]


def test_a_view_edited_after_the_report_is_not_cut(warehouse, capsys):
    """A removal computed for another version of the view is advisory."""

    from exmergo_dex_core.maintain.drift import DriftFinding
    from exmergo_dex_core.semantic_views.project import SemanticViewSemanticLayer

    found, _ = _drift(capsys, warehouse, "ALTER TABLE orders DROP COLUMN status")
    findings = [DriftFinding(**f) for f in found]
    path = warehouse / "views" / "sales.yaml"
    path.write_text(
        path.read_text().replace("description: Orders", "description: All orders"),
        encoding="utf-8",
    )
    layer = SemanticViewSemanticLayer(warehouse, ["views/sales.yaml"], "duckdb")

    proposals, edits, warnings = layer.schema_reconcile(findings)

    assert edits == []
    assert {p.kind for p in proposals} == {"advisory"}
    assert "changed since the drift report" in " ".join(warnings)


def test_a_removal_that_empties_the_view_is_advisory(tmp_path, capsys):
    _sql(tmp_path, "CREATE TABLE orders (order_id INTEGER, amount DOUBLE)")
    text = """\
name: sales
tables:
  - name: orders
    base_table:
      database: demo
      schema: main
      table: orders
    facts:
      - name: amount
        expr: amount
    metrics:
      - name: revenue
        expr: SUM(orders.amount)
"""
    root = _layer(tmp_path, text)

    found, _ = _drift(capsys, root, "ALTER TABLE orders DROP COLUMN amount")

    held = found[0]["data"]["removal"].get("held") or []
    assert {h["reason"] for h in held} & {"empties_view", "would_not_validate"}
    rc, reconciled = _run(capsys, root, "maintain", "reconcile")
    assert rc == 0, reconciled["errors"]
    assert reconciled["data"].get("semantic_plan_id") is None


# --- the edit itself -------------------------------------------------------------


def test_a_removal_keeps_every_other_byte():
    removal = {
        "members": [
            {"table": "orders", "section": "dimensions", "name": "status_label"},
            {"table": "orders", "section": "dimensions", "name": "status"},
            {"table": None, "section": "metrics", "name": "aov"},
        ],
        "relationships": ["orders_customers"],
        "data_types": [
            {"table": "orders", "section": "time_dimensions", "name": "placed_on"}
        ],
    }

    edited = apply_removal(SALES_YAML, removal)

    assert edited is not None
    assert edited.startswith(HEADER)
    assert "# the day an order was placed" in edited, "its member is kept"
    assert "status_label" not in edited
    assert "relationships: []" in edited
    assert "\nmetrics: []" in edited
    body = yaml.safe_load(edited)
    assert [d["name"] for d in body["tables"][0]["dimensions"]] == [
        "note",
        "customer_region",
    ]
    assert "data_type" not in body["tables"][0]["time_dimensions"][0]
    # Every line the removal does not name is still there, in order.
    kept = [line for line in SALES_YAML.splitlines() if line in edited.splitlines()]
    assert len(kept) >= len(SALES_YAML.splitlines()) - 16


def test_a_comment_above_a_removed_member_goes_with_it():
    removal = {
        "members": [
            {"table": "orders", "section": "time_dimensions", "name": "placed_on"}
        ]
    }

    edited = apply_removal(SALES_YAML, removal)

    assert edited is not None
    assert "# the day an order was placed" not in edited
    assert "    time_dimensions: []\n    facts:" in edited


def test_an_entry_that_is_not_there_proposes_nothing():
    removal = {
        "members": [{"table": "orders", "section": "dimensions", "name": "ghost"}]
    }
    assert apply_removal(SALES_YAML, removal) is None


def test_a_flow_style_list_is_not_rewritten():
    text = SALES_YAML.replace(
        "    facts:\n      - name: amount\n        expr: amount\n",
        "    facts: [{name: amount, expr: amount}]\n",
    )
    removal = {"members": [{"table": "orders", "section": "facts", "name": "amount"}]}
    assert apply_removal(text, removal) is None


def test_a_data_type_opening_its_member_is_not_cut():
    text = SALES_YAML.replace(
        "      - name: note\n        expr: note\n        data_type: VARCHAR\n",
        "      - data_type: VARCHAR\n        name: note\n        expr: note\n",
    )
    removal = {
        "data_types": [{"table": "orders", "section": "dimensions", "name": "note"}]
    }
    assert apply_removal(text, removal) is None


# --- drift against the baseline ------------------------------------------------


def _snapshot(root: Path) -> dict:
    return json.loads((root / ".dex" / "snapshot.json").read_text())


def _write_snapshot(root: Path, snap: dict) -> None:
    (root / ".dex" / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")


def _semantic(capsys, root: Path) -> list[dict]:
    rc, envelope = _run(capsys, root, "maintain", "semantic")
    assert rc == 0, envelope["errors"]
    return envelope["data"]["findings"]


def test_the_fingerprint_says_which_key_is_the_grain(warehouse):
    layer = _snapshot(warehouse)["semantic_layer"]
    models = {m["name"]: m for m in layer["semantic_models"]}
    customers = models["sales.customers"]
    assert customers["keys"] == [["customer_key"], ["email"]]
    assert customers["key_claims"] == ["primary", "unique"]
    assert layer["declarations"]["sales.customers"]["fields"]["grain"]


def test_a_changed_primary_key_is_declaration_drift(warehouse, capsys):
    path = warehouse / "views" / "sales.yaml"
    path.write_text(
        path.read_text().replace(
            "columns: [customer_key]", "columns: [customer_key, region]"
        ),
        encoding="utf-8",
    )

    findings = _semantic(capsys, warehouse)

    changed = [f for f in findings if f["code"] == "declaration_changed"]
    assert [(f["data"]["field"], f["severity"]) for f in changed] == [
        ("grain", "high")
    ], findings


def test_a_dimension_reading_another_column_is_definition_drift(warehouse, capsys):
    path = warehouse / "views" / "sales.yaml"
    path.write_text(
        path.read_text().replace("expr: region", "expr: email"), encoding="utf-8"
    )

    codes = {f["code"] for f in _semantic(capsys, warehouse)}

    assert "definition_changed" in codes


def test_a_new_rule_set_is_a_format_change_not_every_definition(warehouse, capsys):
    snap = _snapshot(warehouse)
    snap["semantic_layer"]["format_version"] = "2025-01"
    for model in snap["semantic_layer"]["semantic_models"]:
        model["content_sha256"] = "0" * 64
    _write_snapshot(warehouse, snap)

    findings = _semantic(capsys, warehouse)

    codes = [f["code"] for f in findings]
    assert "semantic_format_changed" in codes
    assert "definition_changed" not in codes


def test_a_baseline_from_before_the_vendor_reads_unchecked_not_clean(warehouse, capsys):
    """A baseline pinned with no vendor, no keys or relationships captured and
    no declarations compares none of them."""

    snap = _snapshot(warehouse)
    layer = snap["semantic_layer"]
    for name in (
        "vendor",
        "format_version",
        "relationships_and_keys_captured",
        "relationship_cardinality_captured",
        "declarations",
    ):
        layer.pop(name, None)
    layer["relationships"] = []
    for model in layer["semantic_models"]:
        model.pop("key_claims", None)
        model["keys"] = []
    _write_snapshot(warehouse, snap)

    rc, envelope = _run(capsys, warehouse, "maintain", "semantic")

    assert rc == 0, envelope["errors"]
    codes = {f["code"] for f in envelope["data"]["findings"]}
    assert "broken_relationship" not in codes
    assert not {"declaration_added", "declaration_changed"} & codes
    status = envelope["data"]["declaration_drift"]
    assert status["checked"] is False, status


# --- the grain axis -------------------------------------------------------------


def _grain(capsys, root: Path, *statements: str) -> dict[str, list[dict]]:
    _sql(root, *statements)
    assert _run_ok(root, "explore", "map", "--refresh")
    rc, envelope = _run(capsys, root, "maintain", "grain")
    assert rc == 0, envelope["errors"]
    return _by_code(envelope["data"]["findings"])


def test_a_lapsed_unique_key_names_the_views_claim(warehouse, capsys):
    """Measured unique at the baseline, so it lapses; the Semantic View's claim
    on it is still the one a query trusts."""

    found = _grain(capsys, warehouse, "INSERT INTO customers VALUES (30, 'EU', 'a@x')")

    (finding,) = [f for f in found["key_lost_uniqueness"] if f["column"] == "email"]
    assert finding["data"]["declared_by"] == "snowflake"
    assert finding["data"]["claim"] == "unique"
    assert "declares (email) unique" in finding["detail"]
    assert "Snowflake trusts a declared key without checking it" in finding["detail"]


def test_a_unique_key_that_never_held_is_the_views_claim(tmp_path, capsys):
    _sql(
        tmp_path,
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "status VARCHAR, note VARCHAR, amount DOUBLE, placed_at DATE)",
        "CREATE TABLE customers (customer_key INTEGER, region VARCHAR, email VARCHAR)",
        "INSERT INTO customers VALUES (10, 'EU', 'a@x'), (20, 'US', 'a@x'), "
        "(30, 'US', 'b@x')",
    )
    root = _layer(tmp_path)

    found = _grain(capsys, root)

    (finding,) = [
        f for f in found["declared_grain_not_unique"] if f["column"] == "email"
    ]
    assert finding["data"]["declared_by"] == "snowflake"
    assert finding["data"]["claim"] == "unique"
    assert "declares (email) unique" in finding["detail"]
    assert "Snowflake trusts a declared key without checking it" in finding["detail"]
    assert (finding["data"]["distinct_count"], finding["data"]["row_count"]) == (2, 3)

    assert _run_ok(root, "maintain", "check")
    rc, reconciled = _run(capsys, root, "maintain", "reconcile")
    assert rc == 0, reconciled["errors"]
    actions = " ".join(p["action"] for p in reconciled["data"]["proposals"])
    assert "drop the claim with `semantic snowflake update`" in actions


def test_a_primary_key_and_the_relationship_relying_on_it(warehouse, capsys):
    found = _grain(capsys, warehouse, "INSERT INTO customers VALUES (10, 'US', 'c@x')")

    (key,) = [f for f in found["key_lost_uniqueness"] if f["column"] == "customer_key"]
    assert key["data"]["claim"] == "primary"
    assert "as the primary key of" in key["detail"]
    rc, reconciled = _run(capsys, warehouse, "maintain", "reconcile")
    assert rc == 0, reconciled["errors"]
    (advice,) = [
        p["action"]
        for p in reconciled["data"]["proposals"]
        if p["finding_code"] == "key_lost_uniqueness" and p["column"] == "customer_key"
    ]
    assert "change the key with `semantic snowflake update`" in advice
    (join_advice,) = [
        p["action"]
        for p in reconciled["data"]["proposals"]
        if p["finding_code"] == "join_cardinality_exceeded"
    ]
    assert "Snowflake reads this relationship as many_to_one" in join_advice
    assert not [w for w in reconciled["warnings"] if "unique` test" in w]
    (join,) = found["join_cardinality_exceeded"]
    assert join["data"]["declared_by"] == "snowflake"
    assert join["data"]["claim"] == "many_to_one"
    assert "Snowflake infers from the key" in join["detail"]
    assert "Snowflake trusts a declared key" in join["detail"]
    assert (join["data"]["distinct_count"], join["data"]["row_count"]) == (2, 3)


# --- on Snowflake's own metadata, through the fake connector ----------------------


def test_the_schema_axis_reads_snowflake_metadata(tmp_path):
    """Upper-case identifiers as Snowflake reports them, a deployed semantic
    view in the same schema left out of the inventory, and a dropped column and
    a retyped one found on the metadata alone, with no statement run."""

    pytest.importorskip("snowflake.connector")
    from fakes.snowflake import FakeSnowflakeConnection, FakeSnowflakeTable

    from exmergo_dex_core.maintain.snapshot import warehouse_from_metadata

    from ..adapters.test_connect_snowflake import data_statements, make_adapter
    from .conftest import source

    def tables(amount: str, with_status: bool) -> list[FakeSnowflakeTable]:
        orders = [
            ("ORDER_ID", "FIXED", False),
            ("CUSTOMER_ID", "FIXED", True),
            ("NOTE", "TEXT", True),
            ("AMOUNT", amount, True),
            ("PLACED_AT", "DATE", True),
        ]
        if with_status:
            orders.append(("STATUS", "TEXT", True))
        return [
            FakeSnowflakeTable("DEMO", "MAIN", "ORDERS", orders, rows=2),
            FakeSnowflakeTable(
                "DEMO",
                "MAIN",
                "CUSTOMERS",
                [
                    ("CUSTOMER_KEY", "FIXED", False),
                    ("REGION", "TEXT", True),
                    ("EMAIL", "TEXT", True),
                ],
                rows=2,
            ),
            FakeSnowflakeTable(
                "DEMO",
                "MAIN",
                "SALES",
                [("REGION", "TEXT", True)],
                kind="semantic_view",
            ),
        ]

    def metadata(amount: str, with_status: bool):
        connection = FakeSnowflakeConnection(tables=tables(amount, with_status))
        datasets = warehouse_from_metadata(make_adapter(connection)).datasets
        assert data_statements(connection) == []
        return datasets

    baseline = metadata("REAL", with_status=True)
    assert "DEMO.MAIN.SALES" not in {d.identifier for d in baseline}
    current = metadata("TEXT", with_status=False)
    layer = source(tmp_path, yaml.safe_load(SALES_YAML), connector="snowflake")

    findings, _notes = layer.declared_schema_drift(current, baseline)

    by_element = {f.data.get("element"): f for f in findings}
    status = by_element["tables['orders'].dimensions['status']"]
    assert (status.code, status.identifier, status.column) == (
        "declared_column_missing",
        "DEMO.MAIN.ORDERS",
        "STATUS",
    )
    revenue = by_element["tables['orders'].metrics['revenue']"]
    assert revenue.code == "declared_type_conflict"
    assert revenue.data["declared"] == "SUM"
