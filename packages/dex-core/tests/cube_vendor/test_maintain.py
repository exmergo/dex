"""`maintain` under `semantic.vendor: cube` (#912).

What a Cube layer gets from every drift axis, beside a dbt project and with
none: a semantic baseline (`vendor: cube`), compiled-only files recorded by
their content and never reported clean, definition drift on cubes, views and
measures, `primary_key`s and declared join arity re-measured on the grain axis,
categorical dimensions re-counted on the semantic axis, the cubes and views
held to the warehouse schema on the schema axis, and `maintain reconcile`
proposing the removals as a `semantic cube update` plan. None of it starts a
Cube server, Node, or anything off the machine.

Runs on a real DuckDB warehouse, so it skips without one.
"""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("sqlglot")
duckdb = pytest.importorskip("duckdb")

from exmergo_dex_core.cli import main  # noqa: E402
from exmergo_dex_core.cube.model import Member  # noqa: E402
from exmergo_dex_core.cube.removal import apply_removal  # noqa: E402
from exmergo_dex_core.cube.schema import type_conflict  # noqa: E402
from exmergo_dex_core.semantic_types import bucket  # noqa: E402

from .conftest import cube, view  # noqa: E402

ORDERS_FILE = "model/orders.yml"
CUSTOMERS_FILE = "model/customers.yml"
VIEWS_FILE = "model/views.yml"
HEADER = "# orders, kept by hand\n"


def _orders(**overrides: Any) -> dict[str, Any]:
    body = cube(
        "orders",
        joins=[
            {
                "name": "customers",
                "sql": "{CUBE}.customer_id = {customers.id}",
                "relationship": "many_to_one",
            }
        ],
        dimensions=[
            {
                "name": "order_id",
                "sql": "order_id",
                "type": "number",
                "primary_key": True,
            },
            {"name": "customer_id", "sql": "{CUBE}.customer_id", "type": "number"},
            {"name": "status", "sql": "{CUBE}.status", "type": "string"},
            {"name": "ordered_at", "sql": "{CUBE}.ordered_at", "type": "time"},
            {"name": "amount_cents", "sql": "{CUBE}.amount * 100", "type": "number"},
        ],
        measures=[
            {
                "name": "revenue",
                "sql": "{CUBE}.amount",
                "type": "sum",
                "filters": [{"sql": "{CUBE}.status != 'test'"}],
            },
            {"name": "order_count", "type": "count"},
            {"name": "aov", "sql": "{revenue} / {order_count}", "type": "number"},
        ],
    )
    return {**body, **overrides}


def _customers(**overrides: Any) -> dict[str, Any]:
    body = cube(
        "customers",
        dimensions=[
            {"name": "id", "sql": "id", "type": "number", "primary_key": True},
            {"name": "region", "sql": "region", "type": "string"},
        ],
    )
    return {**body, **overrides}


SALES = view(
    "sales",
    {"join_path": "orders", "includes": ["revenue", "status", "order_count"]},
    {"join_path": "orders.customers", "includes": ["region"], "prefix": True},
)


def _write(root: Path, name: str, body: dict[str, Any], header: str = "") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def _sql(root: Path, *statements: str) -> None:
    conn = duckdb.connect(str(root / "demo.duckdb"))
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


def _layout(root: Path, *, dbt: bool, files: tuple[str, ...] = ()) -> Path:
    _sql(
        root,
        "CREATE TABLE customers (id INTEGER, region VARCHAR)",
        "INSERT INTO customers VALUES (1, 'north'), (2, 'south'), (3, 'east')",
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "amount DOUBLE, status VARCHAR, ordered_at TIMESTAMP)",
        "INSERT INTO orders VALUES "
        "(1, 1, 10.0, 'paid', '2026-01-01'), (2, 2, 20.0, 'paid', '2026-01-02'), "
        "(3, 3, 5.0, 'open', '2026-01-03'), (4, 1, 7.5, 'paid', '2026-01-04')",
    )
    _write(root, ORDERS_FILE, {"cubes": [_orders()]}, header=HEADER)
    _write(root, CUSTOMERS_FILE, {"cubes": [_customers()]})
    _write(root, VIEWS_FILE, {"views": [SALES]})
    if dbt:
        (root / "dbt_project.yml").write_text(
            'name: cube_maintain\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
            encoding="utf-8",
        )
        (root / "models").mkdir()
    names = (ORDERS_FILE, CUSTOMERS_FILE, VIEWS_FILE, *files)
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"duckdb:\n  path: {root / 'demo.duckdb'}\n"
        "semantic:\n  vendor: cube\n  cube:\n    files:\n"
        + "".join(f"      - {name}\n" for name in names),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A DuckDB warehouse, a Cube layer on it, and a dbt project beside it."""

    return _layout(tmp_path, dbt=True)


def _cli(root: Path, capsys, *argv: str) -> dict:
    rc = main(["--repo-root", str(root), *argv])
    payload = json.loads(capsys.readouterr().out)
    payload["_exit"] = rc
    return payload


def _codes(payload: dict) -> list[str]:
    return [f["code"] for f in payload["data"]["findings"]]


def _finding(payload: dict, code: str, **match: Any) -> dict:
    found = [
        f
        for f in payload["data"]["findings"]
        if f["code"] == code
        and all(
            (f.get(k) if k in f else f["data"].get(k)) == v for k, v in match.items()
        )
    ]
    assert len(found) == 1, (code, match, payload["data"]["findings"])
    return found[0]


# --- the baseline ----------------------------------------------------------------


@pytest.mark.parametrize("dbt", [True, False], ids=["beside-dbt", "cube-only"])
def test_snapshot_and_check_run_every_axis(tmp_path: Path, capsys, dbt: bool):
    root = _layout(tmp_path, dbt=dbt)

    snap = _cli(root, capsys, "maintain", "snapshot")

    assert snap["_exit"] == 0, snap
    layer = json.loads((root / ".dex" / "snapshot.json").read_text())["semantic_layer"]
    assert layer["vendor"] == "cube"
    assert layer["opaque_captured"] is True
    assert {m["name"] for m in layer["semantic_models"]} == {
        "orders",
        "customers",
        "sales",
    }

    payload = _cli(root, capsys, "maintain", "check")

    assert payload["_exit"] == 0, payload
    assert set(payload["data"]["axes_run"]) >= {"schema", "volume", "grain", "semantic"}
    assert payload["data"]["findings"] == []


# --- semantic drift ----------------------------------------------------------------


def test_a_removed_measure_is_its_own_finding(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    orders = _orders()
    orders["measures"] = [m for m in orders["measures"] if m["name"] != "aov"]
    _write(repo, ORDERS_FILE, {"cubes": [orders]}, header=HEADER)

    payload = _cli(repo, capsys, "maintain", "semantic")

    assert payload["_exit"] == 0, payload
    removed = _finding(payload, "definition_removed")
    assert removed["data"] == {"kind": "metric", "name": "orders.aov"}
    changed = _finding(payload, "definition_changed", name="orders")
    # The cube names its table, so the change is traced to it.
    assert changed["identifier"] == "demo.main.orders"


def test_a_widened_categorical_dimension_is_caught(repo: Path, capsys):
    assert _cli(repo, capsys, "explore", "map")["_exit"] == 0
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO customers VALUES (4, 'west')")

    payload = _cli(repo, capsys, "maintain", "semantic")

    assert payload["_exit"] == 0, payload
    finding = _finding(payload, "dimension_cardinality_changed", column="region")
    assert finding["data"]["semantic_model"] == "customers"
    assert (finding["data"]["distinct_before"], finding["data"]["distinct_after"]) == (
        3,
        4,
    )


# --- grain drift --------------------------------------------------------------------


def test_a_primary_key_that_lost_uniqueness_is_caught(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO orders VALUES (4, 2, 1.0, 'paid', '2026-01-05')")

    payload = _cli(repo, capsys, "maintain", "grain")

    assert payload["_exit"] == 0, payload
    finding = _finding(payload, "declared_grain_not_unique", column="order_id")
    assert finding["identifier"] == "demo.main.orders"


def test_a_many_to_one_join_that_fans_out_is_caught(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO customers VALUES (1, 'west')")

    payload = _cli(repo, capsys, "maintain", "grain")

    assert payload["_exit"] == 0, payload
    finding = _finding(payload, "join_cardinality_exceeded")
    assert finding["identifier"] == "demo.main.customers"
    assert finding["data"]["declared_cardinality"] == "many_to_one"
    assert finding["data"]["columns"] == ["id"]


def test_a_one_to_many_join_is_checked_on_its_one_side(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False)
    orders = _orders(joins=[])
    customers = _customers(
        joins=[
            {
                "name": "orders",
                "sql": "{CUBE}.id = {orders.customer_id}",
                "relationship": "one_to_many",
            }
        ]
    )
    _write(root, ORDERS_FILE, {"cubes": [orders]}, header=HEADER)
    _write(root, CUSTOMERS_FILE, {"cubes": [customers]})
    _write(
        root,
        VIEWS_FILE,
        {"views": [view("sales", {"join_path": "orders", "includes": "*"})]},
    )
    _cli(root, capsys, "maintain", "snapshot")
    _sql(root, "INSERT INTO customers VALUES (2, 'west')")

    payload = _cli(root, capsys, "maintain", "grain")

    finding = _finding(payload, "join_cardinality_exceeded")
    assert finding["identifier"] == "demo.main.customers"


def test_a_join_dex_cannot_read_is_named_as_unverified(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False)
    orders = _orders()
    orders["joins"][0]["sql"] = "{CUBE}.customer_id >= {customers.id}"
    _write(root, ORDERS_FILE, {"cubes": [orders]}, header=HEADER)
    _cli(root, capsys, "maintain", "snapshot")
    _sql(root, "INSERT INTO customers VALUES (1, 'west')")

    payload = _cli(root, capsys, "maintain", "check")

    assert "join_cardinality_exceeded" not in _codes(payload)
    assert any(
        "join orders -> customers is not an equality" in w for w in payload["warnings"]
    ), payload["warnings"]


# --- schema drift -------------------------------------------------------------------


def test_a_retyped_column_conflicts_with_its_member_type(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders ALTER amount TYPE VARCHAR")

    payload = _cli(repo, capsys, "maintain", "schema")

    assert payload["_exit"] == 0, payload
    _finding(payload, "column_retyped", column="amount")
    conflict = _finding(payload, "declared_type_conflict", column="amount")
    assert conflict["data"]["declared_member"] == "revenue"
    assert conflict["data"]["declared_type"] == "sum"
    assert conflict["data"]["warehouse_type"] == "VARCHAR"
    # The ratio on it goes with it.
    assert conflict["data"]["removal"]["measures"] == ["revenue", "aov"]


def test_a_time_dimension_on_a_column_that_is_no_longer_temporal(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders ALTER ordered_at TYPE VARCHAR")

    payload = _cli(repo, capsys, "maintain", "schema")

    conflict = _finding(payload, "declared_type_conflict", column="ordered_at")
    assert conflict["data"]["declared_type"] == "time"


def test_a_dropped_column_still_declared_takes_its_readers_with_it(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN status")

    payload = _cli(repo, capsys, "maintain", "schema")

    assert payload["_exit"] == 0, payload
    _finding(payload, "column_dropped", column="status")
    status = _finding(
        payload, "declared_column_missing", column="status", declared_member="status"
    )
    assert status["data"]["model_file"] == ORDERS_FILE
    # The measure whose filter reads it, and the ratio reading that measure.
    revenue = _finding(
        payload, "declared_column_missing", column="status", declared_member="revenue"
    )
    assert revenue["data"]["removal"]["dimensions"] == ["status"]
    assert revenue["data"]["removal"]["measures"] == ["revenue", "aov"]
    cascaded = _finding(payload, "declared_dependency_dropped", semantic_model="orders")
    assert any(r.startswith("aov:") for r in cascaded["data"]["reasons"])
    # The view's includes of both.
    sales = _finding(payload, "declared_dependency_dropped", semantic_model="sales")
    (includes,) = sales["data"]["removal"]["includes"]
    assert includes["join_path"] == "orders"
    assert includes["names"] == ["revenue", "status"]


def test_a_dropped_join_column_takes_the_join_with_it(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN customer_id")

    payload = _cli(repo, capsys, "maintain", "schema")

    missing = _finding(payload, "declared_column_missing", column="customer_id")
    # The view walks the join, so removing it is held until the view moves.
    assert missing["data"]["removal"]["joins"] == []
    assert missing["data"]["removal"]["held_joins"] == ["customers"]
    # A view source walking the join goes too, and is not dex's to drop.
    sales = _finding(payload, "declared_dependency_dropped", semantic_model="sales")
    assert sales["data"]["removal"]["broken_sources"] == ["orders.customers"]


def test_a_vanished_table_is_a_whole_cube_finding(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "DROP TABLE customers")

    payload = _cli(repo, capsys, "maintain", "schema")

    gone = _finding(payload, "declared_relation_missing")
    assert gone["data"]["semantic_model"] == "customers"
    assert gone["data"]["removal"] == {"whole_model": True}
    cascaded = _finding(payload, "declared_dependency_dropped", semantic_model="orders")
    assert cascaded["data"]["removal"]["held_joins"] == ["customers"]


def test_a_relation_never_in_scope_is_a_note_not_a_finding(repo: Path, capsys):
    _write(repo, CUSTOMERS_FILE, {"cubes": [_customers(sql_table="dev.main.cust")]})
    _cli(repo, capsys, "maintain", "snapshot")

    payload = _cli(repo, capsys, "maintain", "schema")

    assert "declared_relation_missing" not in _codes(payload)
    assert any(
        "cube 'customers' reads dev.main.cust, which neither" in w
        for w in payload["warnings"]
    ), payload["warnings"]


def test_an_expression_dex_cannot_read_is_named_unchecked(repo: Path, capsys):
    orders = _orders()
    orders["dimensions"].append(
        {
            "name": "scoped",
            "sql": "{FILTER_PARAMS.orders.status.filter('x')}",
            "type": "string",
        }
    )
    _write(repo, ORDERS_FILE, {"cubes": [orders]}, header=HEADER)
    _cli(repo, capsys, "maintain", "snapshot")

    payload = _cli(repo, capsys, "maintain", "schema")

    assert any(
        "cube 'orders': dex could not read which columns scoped read" in w
        for w in payload["warnings"]
    ), payload["warnings"]


def test_check_carries_the_declared_schema_findings(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False)
    _cli(root, capsys, "maintain", "snapshot")
    _sql(root, "ALTER TABLE orders DROP COLUMN status")

    payload = _cli(root, capsys, "maintain", "check")

    _finding(payload, "declared_column_missing", declared_member="status")


# --- compiled-only definitions ---------------------------------------------------


def _compiled(root: Path) -> None:
    (root / "model" / "legacy.js").write_text("cube(`legacy`, {});\n")
    _write(
        root,
        "model/extended.yml",
        {
            "cubes": [
                {"name": "big_orders", "extends": "orders"},
                _customers(name="regions"),
            ]
        },
    )


def test_a_compiled_only_file_never_reads_clean(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False, files=("model/legacy.js", "model/extended.yml"))
    _compiled(root)
    _cli(root, capsys, "maintain", "snapshot")
    opaque = json.loads((root / ".dex" / "snapshot.json").read_text())[
        "semantic_layer"
    ]["opaque_definitions"]
    assert {o["name"] for o in opaque} == {
        "model/legacy.js",
        "model/extended.yml: cubes['big_orders']",
    }

    payload = _cli(root, capsys, "maintain", "semantic")

    assert payload["data"]["findings"] == []
    assert any(
        "compared by their content alone, never structurally" in w
        for w in payload["warnings"]
    ), payload["warnings"]


def test_a_changed_compiled_only_file_is_reported_changed(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False, files=("model/legacy.js", "model/extended.yml"))
    _compiled(root)
    _cli(root, capsys, "maintain", "snapshot")
    (root / "model" / "legacy.js").write_text("cube(`legacy`, { sql: `x` });\n")
    # A sibling cube in the extends file changes; the extends element does not.
    _write(
        root,
        "model/extended.yml",
        {
            "cubes": [
                {"name": "big_orders", "extends": "orders"},
                _customers(name="regions", title="Regions"),
            ]
        },
    )

    payload = _cli(root, capsys, "maintain", "semantic")

    changed = _finding(payload, "compiled_only_changed")
    assert changed["data"]["name"] == "model/legacy.js"
    assert changed["severity"] == "medium"


def test_a_baseline_that_never_recorded_them_flags_nothing(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False, files=("model/legacy.js",))
    (root / "model" / "legacy.js").write_text("cube(`legacy`, {});\n")
    _cli(root, capsys, "maintain", "snapshot")
    path = root / ".dex" / "snapshot.json"
    snap = json.loads(path.read_text())
    snap["semantic_layer"].pop("opaque_definitions")
    snap["semantic_layer"].pop("opaque_captured")
    path.write_text(json.dumps(snap))

    payload = _cli(root, capsys, "maintain", "semantic")

    assert "definition_added" not in _codes(payload)


# --- reconcile ----------------------------------------------------------------------


def test_reconcile_plans_the_removal_through_semantic_cube(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN status")
    _cli(repo, capsys, "maintain", "schema")
    before = (repo / ORDERS_FILE).read_text()
    views_before = (repo / VIEWS_FILE).read_text()

    payload = _cli(repo, capsys, "maintain", "reconcile")

    assert payload["_exit"] == 0, payload
    plan_id = payload["data"]["semantic_plan_id"]
    kinds = {
        (p["finding_code"], p["kind"])
        for p in payload["data"]["proposals"]
        if p["finding_code"].startswith("declared_")
    }
    assert ("declared_column_missing", "mechanical") in kinds
    assert ("declared_dependency_dropped", "mechanical") in kinds
    assert (repo / ORDERS_FILE).read_text() == before

    applied = _cli(repo, capsys, "transform", "apply", plan_id)

    assert applied["_exit"] == 0, applied
    after = (repo / ORDERS_FILE).read_text()
    assert after.startswith(HEADER)
    (orders,) = yaml.safe_load(after)["cubes"]
    assert [d["name"] for d in orders["dimensions"]] == [
        "order_id",
        "customer_id",
        "ordered_at",
        "amount_cents",
    ]
    assert [m["name"] for m in orders["measures"]] == ["order_count"]
    (source, _) = yaml.safe_load((repo / VIEWS_FILE).read_text())["views"][0]["cubes"]
    assert source["includes"] == ["order_count"]
    assert (repo / VIEWS_FILE).read_text() != views_before
    # Clean again once the layer agrees with the warehouse.
    _cli(repo, capsys, "maintain", "snapshot")
    assert "declared_column_missing" not in _codes(
        _cli(repo, capsys, "maintain", "schema")
    )


def test_reconcile_never_removes_a_whole_cube(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "DROP TABLE customers")
    _cli(repo, capsys, "maintain", "schema")

    payload = _cli(repo, capsys, "maintain", "reconcile")

    by_code = {}
    for p in payload["data"]["proposals"]:
        by_code.setdefault(p["finding_code"], []).append(p["kind"])
    assert by_code["declared_relation_missing"] == ["advisory"]
    # The view's source walking into the gone cube, and orders' join to it,
    # which that source walks: neither is dex's to cut, so nothing is planned.
    assert by_code["declared_dependency_dropped"] == ["advisory", "advisory"]
    assert "semantic_plan_id" not in payload["data"]


def test_reconcile_native_only_repository_whose_findings_the_layer_does_not_handle(
    tmp_path: Path, capsys
):
    """#994: A native-only repository whose findings the layer doesn't handle
    returns status: ok with advisory proposals and no stored plan."""
    root = _layout(tmp_path, dbt=False)
    _cli(root, capsys, "maintain", "snapshot")
    _sql(root, "INSERT INTO orders VALUES (4, 2, 1.0, 'paid', '2026-01-05')")
    _cli(root, capsys, "maintain", "grain")

    payload = _cli(root, capsys, "maintain", "reconcile")

    assert payload["_exit"] == 0, payload
    assert payload["status"] == "ok"
    proposals = [
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "declared_grain_not_unique"
    ]
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "advisory"
    assert payload["data"].get("plan_id") is None
    assert any("no transformation project to edit" in w for w in payload["warnings"])


def test_a_join_no_view_walks_is_removed_with_the_gone_cube(tmp_path: Path, capsys):
    root = _layout(tmp_path, dbt=False)
    _write(
        root,
        VIEWS_FILE,
        {"views": [view("sales", {"join_path": "orders", "includes": "*"})]},
    )
    _cli(root, capsys, "maintain", "snapshot")
    _sql(root, "DROP TABLE customers")
    _cli(root, capsys, "maintain", "schema")

    payload = _cli(root, capsys, "maintain", "reconcile")

    (join,) = [
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "declared_dependency_dropped"
    ]
    assert join["kind"] == "mechanical"
    applied = _cli(
        root, capsys, "transform", "apply", payload["data"]["semantic_plan_id"]
    )
    assert applied["_exit"] == 0, applied
    (orders,) = yaml.safe_load((root / ORDERS_FILE).read_text())["cubes"]
    assert orders["joins"] == []


def test_a_file_with_a_key_cube_does_not_declare_is_advisory(repo: Path, capsys):
    orders = _orders()
    orders["dimensions"][2]["colour"] = "blue"
    _write(repo, ORDERS_FILE, {"cubes": [orders]}, header=HEADER)
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN status")
    _cli(repo, capsys, "maintain", "schema")

    payload = _cli(repo, capsys, "maintain", "reconcile")

    on_orders = [
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "declared_column_missing"
    ]
    assert on_orders and all(p["kind"] == "advisory" for p in on_orders)
    assert any(
        f"{ORDERS_FILE} already declares key(s) Cube does not" in w
        for w in payload["warnings"]
    ), payload["warnings"]


# --- nothing off the machine ----------------------------------------------------------


def test_no_maintain_path_opens_a_server_or_a_subprocess(
    repo: Path, capsys, monkeypatch
):
    assert _cli(repo, capsys, "maintain", "snapshot")["_exit"] == 0
    _sql(
        repo,
        "ALTER TABLE orders DROP COLUMN status",
        "INSERT INTO customers VALUES (1, 'west')",
    )

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("maintain reached off the machine")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    for argv in (
        ("maintain", "check"),
        ("maintain", "schema"),
        ("maintain", "grain"),
        ("maintain", "semantic"),
        ("maintain", "volume"),
        ("maintain", "reconcile"),
    ):
        payload = _cli(repo, capsys, *argv)
        assert payload["_exit"] == 0, (argv, payload)


# --- the pieces -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spelled", "expected"),
    [
        ("INTEGER", "numeric"),
        ("NUMBER(38,0)", "numeric"),
        ("DOUBLE", "numeric"),
        ("VARCHAR", "text"),
        ("STRING", "text"),
        ("BOOLEAN", "boolean"),
        ("DATE", "temporal"),
        ("TIMESTAMP_NTZ", "temporal"),
        ("DATETIME", "temporal"),
        ("JSON", None),
        ("INTEGER[]", None),
        ("", None),
    ],
)
def test_a_warehouse_type_is_bucketed_only_when_unambiguous(spelled, expected):
    assert bucket(spelled) == expected


@pytest.mark.parametrize(
    ("kind", "declared", "warehouse", "needs"),
    [
        ("dimension", "time", "VARCHAR", "temporal"),
        ("dimension", "time", "DATE", None),
        ("dimension", "number", "VARCHAR", "numeric"),
        ("dimension", "string", "INTEGER", None),
        ("measure", "sum", "VARCHAR", "numeric"),
        ("measure", "count_distinct", "VARCHAR", None),
        ("dimension", "time", "JSON", None),
    ],
)
def test_a_member_type_conflicts_only_with_a_plainly_other_kind(
    kind, declared, warehouse, needs
):
    member = Member(name="m", kind=kind, type=declared)
    assert type_conflict(member, warehouse) == needs


def test_a_removal_cuts_only_the_entries_it_names():
    text = (
        "# kept\n"
        "cubes:\n"
        "  - name: orders  # the fact\n"
        "    sql_table: demo.main.orders\n"
        "    dimensions:\n"
        "      - name: a\n"
        "        sql: a\n"
        "        type: number\n"
        "\n"
        "      - name: b  # goes\n"
        "        sql: b\n"
        "        type: string\n"
        "    measures:\n"
        "      - name: m\n"
        "        type: count\n"
        "  - name: other\n"
        "    sql_table: demo.main.other\n"
    )

    edited = apply_removal(
        text, "cubes", "orders", {"dimensions": ["b"], "measures": ["m"]}
    )

    assert edited == (
        "# kept\n"
        "cubes:\n"
        "  - name: orders  # the fact\n"
        "    sql_table: demo.main.orders\n"
        "    dimensions:\n"
        "      - name: a\n"
        "        sql: a\n"
        "        type: number\n"
        "\n"
        "    measures: []\n"
        "  - name: other\n"
        "    sql_table: demo.main.other\n"
    )


def test_a_flow_list_of_includes_keeps_the_survivors_spelling():
    text = (
        "views:\n"
        "  - name: sales\n"
        "    cubes:\n"
        "      - join_path: orders\n"
        "        includes: [revenue, 'status', order_count]  # public\n"
    )

    edited = apply_removal(
        text,
        "views",
        "sales",
        {"includes": [{"join_path": "orders", "alias": None, "names": ["status"]}]},
    )

    assert edited == (
        "views:\n"
        "  - name: sales\n"
        "    cubes:\n"
        "      - join_path: orders\n"
        "        includes: [revenue, order_count]  # public\n"
    )


def test_a_removal_it_cannot_place_is_not_offered():
    flow = "cubes: [{name: orders, dimensions: [{name: a}, {name: b}]}]\n"

    assert apply_removal(flow, "cubes", "orders", {"dimensions": ["b"]}) is None
