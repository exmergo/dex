"""`maintain` under `semantic.vendor: databricks` (#1259).

The views held to the warehouse schema dex captured (the vendor schema axis),
`maintain reconcile` cutting the removals out of the view files through
`semantic databricks update`, the reconciled view read back by the reader, a
file re-saved in another spec version reported as `semantic_format_changed`, and
a `rely` key the data contradicts worded as the Metric View's claim rather than
the project's grain. Nothing opens a workspace connection or deploys.

The command tests run on a real DuckDB warehouse, so they skip without one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cli import main
from exmergo_dex_core.metric_views.model import FIELD, MEASURE, Member
from exmergo_dex_core.metric_views.removal import apply_removal
from exmergo_dex_core.metric_views.schema import needed_kind

from .conftest import repo, view

HEADER = "# orders, kept by hand\n"

ORDERS_YAML = """\
# orders, kept by hand
version: 1.1
source: demo.main.orders
comment: One row per order.
joins:
  - name: customer
    source: demo.main.customers
    on: source.customer_id = customer.customer_key
    joins:
      - name: nation
        source: demo.main.nations
        using:
          - nation_key
dimensions:
  - name: status
    expr: status
  # the day an order was placed
  - name: ordered_on
    expr: placed_at
    format:
      type: date
      date_format: year_month_day
  - name: region
    expr: customer.region
  - name: nation_name
    expr: customer.nation.n_name
  - name: loud_status
    expr: UPPER(status)
measures:
  - name: revenue
    expr: SUM(amount)
    format:
      type: currency
      currency_code: USD
  - name: order_count
    expr: COUNT(1)
  - name: aov
    expr: MEASURE(revenue) / MEASURE(order_count)
"""


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _sql(root: Path, *statements: str) -> None:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(root / "demo.duckdb"))
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    """Orders, customers and nations, a hand-kept view over them, and a
    baseline pinned with every column present."""

    _sql(
        tmp_path,
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "status VARCHAR, amount DOUBLE, placed_at TIMESTAMP)",
        "INSERT INTO orders VALUES (1, 10, 'paid', 5, '2026-01-01'), "
        "(2, 20, 'open', 3, '2026-01-02')",
        "CREATE TABLE customers (customer_key INTEGER, region VARCHAR, "
        "nation_key INTEGER)",
        "INSERT INTO customers VALUES (10, 'EU', 1), (20, 'US', 2)",
        "CREATE TABLE nations (nation_key INTEGER, n_name VARCHAR)",
        "INSERT INTO nations VALUES (1, 'FR'), (2, 'US')",
    )
    root = repo(tmp_path, orders=view(fields=[{"name": "x", "expr": "status"}]))
    (root / "views" / "orders.yaml").write_text(ORDERS_YAML, encoding="utf-8")
    config = root / ".dex" / "config.yml"
    config.write_text(
        config.read_text() + "duckdb:\n  path: demo.duckdb\n", encoding="utf-8"
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")
    return root


def _run_ok(root: Path, *argv: str) -> bool:
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["--repo-root", str(root), *argv])
    return rc == 0


def _drift(capsys, root: Path, *statements: str) -> list[dict]:
    """Apply ``statements``, refresh the map, and return the schema findings dex
    computed for the views."""

    _sql(root, *statements)
    assert _run_ok(root, "explore", "map", "--refresh")
    rc, envelope = _run(capsys, root, "maintain", "schema")
    assert rc == 0, envelope["errors"]
    return [f for f in envelope["data"]["findings"] if f["code"].startswith("declared")]


def _by_code(findings: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for finding in findings:
        grouped.setdefault(finding["code"], []).append(finding)
    return grouped


# --- the type rules -------------------------------------------------------------


def _member(kind: str, fmt: dict[str, Any] | None = None) -> Member:
    return Member(kind=kind, name="m", expr="x", format=fmt)


@pytest.mark.parametrize(
    ("member", "warehouse_type", "agg", "needs"),
    [
        (_member(FIELD, {"type": "date"}), "VARCHAR", None, "temporal"),
        (_member(FIELD, {"type": "date_time"}), "INTEGER", None, "temporal"),
        (_member(FIELD, {"type": "date"}), "TIMESTAMP", None, None),
        (_member(FIELD, {"type": "currency"}), "VARCHAR", None, "numeric"),
        (_member(FIELD, {"type": "number"}), "DOUBLE", None, None),
        (_member(FIELD), "VARCHAR", None, None),
        (_member(FIELD, {"type": "date"}), "STRUCT(a INT)", None, None),
        (_member(MEASURE), "VARCHAR", "sum", "numeric"),
        (_member(MEASURE, {"type": "currency"}), "VARCHAR", "count", None),
        (_member(MEASURE, {"type": "date"}), "VARCHAR", "max", "temporal"),
    ],
)
def test_what_a_format_or_an_aggregate_needs_of_its_column(
    member, warehouse_type, agg, needs
):
    """A type dex cannot place never conflicts; a count's format is about the
    count, not its column."""

    found = needed_kind(member, warehouse_type, agg)
    assert (found[0] if found else None) == needs


# --- the schema axis --------------------------------------------------------------


def test_a_dropped_column_names_the_view_the_element_and_the_column(warehouse, capsys):
    """And what reads the removed measure through MEASURE() goes too."""

    findings = _drift(capsys, warehouse, "ALTER TABLE orders DROP COLUMN amount")
    grouped = _by_code(findings)

    (missing,) = grouped["declared_column_missing"]
    assert missing["identifier"] == "demo.main.orders"
    assert missing["column"] == "amount"
    assert missing["data"]["semantic_model"] == "orders"
    assert missing["data"]["element"] == "measures['revenue']"
    assert "view 'orders'" in missing["detail"]
    (cascade,) = grouped["declared_dependency_dropped"]
    assert "MEASURE(revenue)" in cascade["detail"]
    removal = missing["data"]["removal"]
    assert removal["measures"] == ["revenue", "aov"]
    assert removal["fields"] == []


def test_a_join_key_dropped_takes_the_join_its_nested_join_and_what_reads_them(
    warehouse, capsys
):
    findings = _drift(
        capsys, warehouse, "ALTER TABLE customers DROP COLUMN customer_key"
    )
    grouped = _by_code(findings)

    (missing,) = grouped["declared_column_missing"]
    assert missing["data"]["element"] == "joins['customer']"
    assert missing["identifier"] == "demo.main.customers"
    assert missing["column"] == "customer_key"
    removal = missing["data"]["removal"]
    assert removal["joins"] == ["customer"], "the nested join goes with its parent"
    assert removal["fields"] == ["region", "nation_name"]
    (cascade,) = grouped["declared_dependency_dropped"]
    assert "join customer.nation" in cascade["detail"]


def test_a_column_a_nested_join_reads_is_reported_against_its_table(warehouse, capsys):
    findings = _drift(capsys, warehouse, "ALTER TABLE nations DROP COLUMN n_name")

    (missing,) = findings
    assert missing["identifier"] == "demo.main.nations"
    assert missing["data"]["element"] == "dimensions['nation_name']"
    assert missing["data"]["removal"]["fields"] == ["nation_name"]


def test_a_format_the_column_no_longer_fits_is_a_type_conflict(warehouse, capsys):
    """The format is what goes, never the field."""

    findings = _drift(
        capsys, warehouse, "ALTER TABLE orders ALTER placed_at TYPE VARCHAR"
    )

    (conflict,) = findings
    assert conflict["code"] == "declared_type_conflict"
    assert conflict["column"] == "placed_at"
    assert conflict["data"]["needs"] == "temporal"
    assert conflict["data"]["removal"]["formats"] == ["ordered_on"]
    assert conflict["data"]["removal"]["fields"] == []


def test_a_source_gone_since_the_baseline_is_the_whole_view(warehouse, capsys):
    findings = _drift(capsys, warehouse, "DROP TABLE orders")

    (gone,) = findings
    assert gone["code"] == "declared_relation_missing"
    assert gone["data"]["removal"] == {"whole_model": True}
    assert gone["data"]["element"] == "source"


def test_a_join_source_gone_is_its_own_finding(warehouse, capsys):
    findings = _drift(capsys, warehouse, "DROP TABLE nations")

    (gone,) = [f for f in findings if f["code"] == "declared_relation_missing"]
    assert gone["identifier"] == "demo.main.nations"
    assert gone["data"]["element"] == "joins['customer.nation']"
    held = gone["data"]["removal"]["held"]
    assert [h["reason"] for h in held] == ["join_relation_missing"]


def test_nothing_drifted_is_nothing_reported(warehouse, capsys):
    rc, envelope = _run(capsys, warehouse, "maintain", "schema")

    assert rc == 0, envelope["errors"]
    assert not [
        f for f in envelope["data"]["findings"] if f["code"].startswith("declared")
    ]


def test_an_unreadable_expression_is_named_as_unchecked(tmp_path, capsys):
    _sql(tmp_path, "CREATE TABLE orders (status VARCHAR)")
    root = repo(
        tmp_path,
        orders=view(
            fields=[{"name": "s", "expr": "status"}, {"name": "b", "expr": "ghost.col"}]
        ),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")

    rc, envelope = _run(capsys, root, "maintain", "schema")

    assert rc == 0, envelope["errors"]
    text = json.dumps(envelope)
    assert "were not checked against the warehouse" in text


# --- reconcile -------------------------------------------------------------------


def test_reconcile_cuts_the_removal_and_the_view_round_trips(warehouse, capsys):
    """A `semantic databricks update` plan, the comments kept, the reconciled
    view read back by the reader, and nothing left drifted."""

    _drift(
        capsys,
        warehouse,
        "ALTER TABLE orders DROP COLUMN amount",
        "ALTER TABLE orders ALTER placed_at TYPE VARCHAR",
        "ALTER TABLE customers DROP COLUMN customer_key",
    )
    rc, envelope = _run(capsys, warehouse, "maintain", "reconcile")
    assert rc == 0, envelope["errors"]
    plan_id = envelope["data"]["semantic_plan_id"]
    assert plan_id
    kinds = {
        p["finding_code"]: p["kind"]
        for p in envelope["data"]["proposals"]
        if p["finding_code"].startswith("declared")
    }
    assert set(kinds.values()) == {"mechanical"}

    rc, applied = _run(capsys, warehouse, "transform", "apply", plan_id)
    assert rc == 0, applied["errors"]

    text = (warehouse / "views" / "orders.yaml").read_text()
    assert text.startswith(HEADER)
    assert "# the day an order was placed" in text
    assert "\n    on: source.customer_id" not in text, "the join went"
    body = yaml.safe_load(text)
    assert "format" not in body["dimensions"][1]
    assert [d["name"] for d in body["dimensions"]] == [
        "status",
        "ordered_on",
        "loud_status",
    ]
    assert [m["name"] for m in body["measures"]] == ["order_count"]
    assert body["joins"] == []

    rc, listed = _run(capsys, warehouse, "explore", "semantic", "list")
    assert rc == 0, listed["errors"]
    assert [m["name"] for m in listed["data"]["metrics"]] == ["orders.order_count"]
    rc, again = _run(capsys, warehouse, "maintain", "schema")
    assert not [
        f for f in again["data"]["findings"] if f["code"].startswith("declared")
    ]


def test_a_filter_reading_a_dropped_column_is_advisory(tmp_path, capsys):
    """A filter is the view's population; dex does not change it on its own."""

    _sql(
        tmp_path,
        "CREATE TABLE orders (status VARCHAR, amount DOUBLE, test BOOLEAN)",
        "INSERT INTO orders VALUES ('paid', 1, false)",
    )
    root = repo(
        tmp_path,
        orders=view(
            filter="NOT test",
            fields=[{"name": "status", "expr": "status"}],
            measures=[{"name": "revenue", "expr": "SUM(amount)"}],
        ),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")

    (finding,) = _drift(capsys, root, "ALTER TABLE orders DROP COLUMN test")
    assert finding["code"] == "declared_column_missing"
    assert finding["data"]["element"] == "filter"

    rc, envelope = _run(capsys, root, "maintain", "reconcile")
    assert rc == 0, envelope["errors"]
    assert envelope["data"].get("semantic_plan_id") is None
    (proposal,) = [
        p
        for p in envelope["data"]["proposals"]
        if p["finding_code"].startswith("declared")
    ]
    assert proposal["kind"] == "advisory"
    assert "population" in proposal["action"]


def test_a_filter_reading_through_a_removed_join_is_advisory(tmp_path, capsys):
    _sql(
        tmp_path,
        "CREATE TABLE orders (customer_id INTEGER, amount DOUBLE)",
        "INSERT INTO orders VALUES (1, 2)",
        "CREATE TABLE customers (customer_key INTEGER, region VARCHAR)",
        "INSERT INTO customers VALUES (1, 'EU')",
    )
    root = repo(
        tmp_path,
        orders=view(
            filter="customer.region = 'EU'",
            joins=[
                {
                    "name": "customer",
                    "source": "demo.main.customers",
                    "on": "source.customer_id = customer.customer_key",
                }
            ],
            measures=[{"name": "revenue", "expr": "SUM(amount)"}],
        ),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")
    _drift(capsys, root, "ALTER TABLE customers DROP COLUMN customer_key")

    rc, envelope = _run(capsys, root, "maintain", "reconcile")

    assert rc == 0, envelope["errors"]
    assert envelope["data"].get("semantic_plan_id") is None
    (proposal,) = [
        p
        for p in envelope["data"]["proposals"]
        if p["finding_code"].startswith("declared")
    ]
    assert proposal["kind"] == "advisory"
    assert "filter" in proposal["action"]


def test_a_view_edited_after_the_report_is_not_cut(warehouse, capsys):
    """The removal was computed for the view as it was; cutting by name could
    remove a member the edit just repaired."""

    _drift(capsys, warehouse, "ALTER TABLE orders DROP COLUMN amount")
    assert _run_ok(warehouse, "maintain", "check")
    path = warehouse / "views" / "orders.yaml"
    path.write_text(
        path.read_text().replace("SUM(amount)", "COUNT(1)"), encoding="utf-8"
    )

    rc, envelope = _run(capsys, warehouse, "maintain", "reconcile")

    assert rc == 0, envelope["errors"]
    assert envelope["data"].get("semantic_plan_id") is None
    assert all(
        p["kind"] == "advisory"
        for p in envelope["data"]["proposals"]
        if p["finding_code"].startswith("declared")
    )
    assert "changed since the drift report" in json.dumps(envelope["warnings"])


def test_a_query_sources_own_alias_is_not_a_dropped_column(tmp_path, capsys):
    _sql(
        tmp_path,
        "CREATE TABLE orders (status VARCHAR)",
        "INSERT INTO orders VALUES ('paid')",
    )
    root = repo(
        tmp_path,
        orders=view(
            "SELECT status AS s FROM demo.main.orders ORDER BY s",
            fields=[{"name": "s", "expr": "s"}],
        ),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")

    rc, envelope = _run(capsys, root, "maintain", "schema")

    assert rc == 0, envelope["errors"]
    assert not [
        f for f in envelope["data"]["findings"] if f["code"].startswith("declared")
    ]


def test_a_projection_read_named_like_its_alias_is_still_a_read(tmp_path, capsys):
    """`old AS old` reads the table's `old`; losing it breaks the source."""

    _sql(
        tmp_path,
        "CREATE TABLE orders (old VARCHAR, n INTEGER)",
        "INSERT INTO orders VALUES ('a', 1)",
    )
    root = repo(
        tmp_path,
        orders=view(
            "SELECT old AS old, n FROM demo.main.orders",
            measures=[{"name": "c", "expr": "COUNT(1)"}],
        ),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")

    findings = _drift(capsys, root, "ALTER TABLE orders DROP COLUMN old")

    (finding,) = findings
    assert (finding["data"]["element"], finding["column"]) == ("source", "old")


def test_a_removal_that_empties_the_view_is_advisory(tmp_path, capsys):
    _sql(
        tmp_path,
        "CREATE TABLE orders (amount DOUBLE)",
        "INSERT INTO orders VALUES (1)",
    )
    root = repo(
        tmp_path,
        orders=view(measures=[{"name": "revenue", "expr": "SUM(amount)"}]),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")
    _drift(
        capsys,
        root,
        "ALTER TABLE orders ADD COLUMN x INTEGER",
        "ALTER TABLE orders DROP COLUMN amount",
    )

    rc, envelope = _run(capsys, root, "maintain", "reconcile")

    assert rc == 0, envelope["errors"]
    assert envelope["data"].get("semantic_plan_id") is None
    (proposal,) = [
        p
        for p in envelope["data"]["proposals"]
        if p["finding_code"].startswith("declared")
    ]
    assert proposal["kind"] == "advisory"
    assert "no field and no measure" in proposal["action"]


# --- the surgical edit --------------------------------------------------------------


def test_a_removal_keeps_every_other_byte_and_on_as_text():
    removal = {
        "fields_key": "dimensions",
        "fields": ["nation_name"],
        "measures": ["aov"],
        "joins": ["customer.nation"],
        "formats": ["ordered_on"],
    }

    edited = apply_removal(ORDERS_YAML, removal)

    assert edited is not None
    assert "    on: source.customer_id = customer.customer_key\n" in edited
    assert "nation" not in edited.split("dimensions:")[0].split("customer_key")[1]
    assert "# the day an order was placed" in edited
    assert "date_format" not in edited
    assert "aov" not in edited


def test_removals_in_one_list_compose_against_the_file_as_written():
    """Two sibling joins, and a field beside another field's format."""

    text = (
        "version: 1.1\nsource: t\njoins:\n"
        "  - name: a\n    source: x\n    on: source.k = a.k\n"
        "  - name: b\n    source: y\n    on: source.k = b.k\n"
        "fields:\n  - name: bad\n    expr: gone\n"
        "  - name: day\n    expr: d\n    format:\n      type: date\n"
        "      date_format: year_month_day\n"
    )

    edited = apply_removal(
        text, {"fields": ["bad"], "formats": ["day"], "joins": ["a", "b"]}
    )

    assert edited is not None
    body = yaml.safe_load(edited.replace("on:", "'on':"))
    assert body["joins"] == []
    assert body["fields"] == [{"name": "day", "expr": "d"}]


def test_a_member_named_by_its_expr_is_found_or_nothing_is_cut():
    text = (
        "version: 1.1\nsource: t\nfields:\n  - expr: gone\n"
        "  - name: bad\n    expr: other_gone\n  - name: keep\n    expr: id\n"
    )

    edited = apply_removal(text, {"fields": ["gone", "bad"]})
    assert edited is not None
    assert "gone" not in edited and "keep" in edited

    assert apply_removal(text, {"fields": ["gone", "missing"]}) is None


def test_a_flow_style_list_is_not_rewritten():
    text = (
        "version: 1.1\nsource: t\n"
        "measures: [{name: a, expr: COUNT(1)}, {name: b, expr: SUM(x)}]\n"
    )

    assert apply_removal(text, {"measures": ["b"]}) is None


def test_a_format_opening_its_member_is_not_cut():
    text = (
        "version: 1.1\nsource: t\nfields:\n"
        "  - format: {type: date, date_format: year_month_day}\n"
        "    name: d\n    expr: d\n"
    )

    assert apply_removal(text, {"formats": ["d"]}) is None


# --- the format version -------------------------------------------------------------


def test_a_view_resaved_in_another_spec_version_is_a_format_change(warehouse, capsys):
    """For that file alone: the other views are still compared by content."""

    _sql(warehouse, "CREATE TABLE people (tier VARCHAR)")
    other = warehouse / "views" / "people.yaml"
    other.write_text(
        yaml.safe_dump(
            view("demo.main.people", fields=[{"name": "tier", "expr": "tier"}])
        ),
        encoding="utf-8",
    )
    config = warehouse / ".dex" / "config.yml"
    config.write_text(
        config.read_text().replace(
            "      - views/orders.yaml\n",
            "      - views/orders.yaml\n      - views/people.yaml\n",
        ),
        encoding="utf-8",
    )
    assert _run_ok(warehouse, "maintain", "snapshot")
    path = warehouse / "views" / "orders.yaml"
    path.write_text(
        path.read_text().replace("version: 1.1", "version: 1.2"), encoding="utf-8"
    )
    other.write_text(other.read_text().replace("tier", "level"), encoding="utf-8")

    rc, envelope = _run(capsys, warehouse, "maintain", "semantic")

    assert rc == 0, envelope["errors"]
    findings = envelope["data"]["findings"]
    (changed,) = [f for f in findings if f["code"] == "semantic_format_changed"]
    assert changed["data"]["path"] == "views/orders.yaml"
    assert (
        changed["data"]["format_version_before"],
        changed["data"]["format_version_after"],
    ) == ("1.1", "1.2")
    assert any(
        f["code"] == "definition_changed" and "people" in json.dumps(f)
        for f in findings
    ), "the other view is still compared by content"
    assert "Restore `version: 1.1`" in changed["detail"], "not re-pin an unread view"


def test_a_version_written_1_10_is_not_read_as_1_1(tmp_path):
    from exmergo_dex_core.metric_views.loader import validate_view_contents

    loaded = validate_view_contents(
        {
            "views/orders.yaml": "version: 1.10\nsource: t\nmeasures:\n"
            "  - name: n\n    expr: COUNT(1)\n"
        }
    )

    assert loaded.empty
    assert loaded.declared_versions == {"views/orders.yaml": "1.10"}
    assert [d.rule for d in loaded.errors] == ["version"]


# --- the grain wording --------------------------------------------------------------


def test_a_contradicted_rely_key_is_worded_as_the_views_claim(tmp_path, capsys):
    """Not "the project declares ... as the grain": a `rely` flag asserts
    uniqueness on the join columns, and the Metric View layer asserts it."""

    _sql(
        tmp_path,
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER)",
        "INSERT INTO orders VALUES (1, 10), (2, 20)",
        "CREATE TABLE customers (customer_key INTEGER, region VARCHAR)",
        "INSERT INTO customers VALUES (10, 'EU'), (10, 'US'), (20, 'US')",
    )
    root = repo(
        tmp_path,
        orders=view(
            joins=[
                {
                    "name": "customer",
                    "source": "demo.main.customers",
                    "on": "source.customer_id = customer.customer_key",
                    "rely": {"at_most_one_match": True},
                }
            ],
            fields=[{"name": "region", "expr": "customer.region"}],
        ),
        config="duckdb:\n  path: demo.duckdb\n",
    )
    assert _run_ok(root, "explore", "map")
    assert _run_ok(root, "maintain", "snapshot")

    rc, envelope = _run(capsys, root, "maintain", "grain")

    assert rc == 0, envelope["errors"]
    (finding,) = [
        f
        for f in envelope["data"]["findings"]
        if f["code"] == "declared_grain_not_unique"
    ]
    assert "rely.at_most_one_match" in finding["detail"]
    assert "(orders.customer)" in finding["detail"], "the join that claims it"
    assert "the project declares" not in finding["detail"]
    assert "more than once" in finding["detail"]
    assert finding["data"]["declared_on"] == ["orders.customer"]
    assert (finding["data"]["declared_by"], finding["data"]["claim"]) == (
        "databricks",
        "unique",
    )

    assert _run_ok(root, "maintain", "check")
    rc, envelope = _run(capsys, root, "maintain", "reconcile", "grain")
    assert rc == 0, envelope["errors"]
    (proposal,) = [
        p
        for p in envelope["data"]["proposals"]
        if p["finding_code"] == "declared_grain_not_unique"
    ]
    assert "rely.at_most_one_match" in proposal["action"]
    assert "the project asserts" not in proposal["action"]


def test_a_project_grain_and_a_rely_key_on_the_same_columns_are_both_named():
    """Neither claim stands in for the other: the `rely` one leads, since a
    query trusts it silently, and the project's grain is named beside it."""

    from exmergo_dex_core.maintain import drift
    from exmergo_dex_core.maintain.snapshot import (
        SemanticLayerSnapshot,
        SemanticModelDef,
    )
    from exmergo_dex_core.project_definitions import (
        DeclaredCompositeKey,
        ProjectDefinitions,
    )

    known = ["demo.main.customers"]
    origins: dict = {}
    definitions = ProjectDefinitions(
        present=True,
        declared_composite_keys=[
            DeclaredCompositeKey(
                model="customers",
                relation="demo.main.customers",
                columns=["customer_key", "region"],
                source="yaml",
            )
        ],
    )
    by_identifier, _ = drift._declared_composites(definitions, known, None, origins)
    semantic = SemanticLayerSnapshot(
        vendor="databricks",
        relationships_and_keys_captured=True,
        semantic_models=[
            SemanticModelDef(
                name="orders.customer",
                path="views/orders.yaml",
                content_sha256="x",
                relation="demo.main.customers",
                keys=[["region", "customer_key"]],
            )
        ],
    )
    drift._fold_semantic_keys(by_identifier, [], semantic, known, None, origins)

    (claims,) = origins.values()
    assert {(c[0], c[1]) for c in claims} == {
        ("yaml", "combination"),
        ("databricks", "unique"),
    }
