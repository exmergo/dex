"""`maintain` under `semantic.vendor: slayer` (#600).

What a SLayer layer beside a dbt project gets from every drift axis: a semantic
baseline recording the vendor, the SLayer release and the model format version;
definition drift on models and saved measures; declared keys and declared join
arity re-measured on the grain axis; the models held to the warehouse schema by
SLayer's own `validate-models` rules on the schema axis; and `maintain
reconcile` proposing SLayer's minimal removal as a `semantic slayer` plan. None
of it opens a SLayer datasource or SLayer storage.

Runs on a real DuckDB warehouse with the real `[slayer]` extra, so it skips
without either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

duckdb = pytest.importorskip("duckdb")

from exmergo_dex_core.cli import main  # noqa: E402
from exmergo_dex_core.slayer.loader import FORMAT_VERSION  # noqa: E402
from exmergo_dex_core.slayer.removal import apply_removal  # noqa: E402
from exmergo_dex_core.slayer.schema import data_type  # noqa: E402

BASE = "semantics/slayer"
ORDERS = "models/wh/orders.yaml"
CUSTOMERS = "models/wh/customers.yaml"


def _orders(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "version": FORMAT_VERSION,
        "name": "orders",
        "sql_table": "demo.main.orders",
        "data_source": "wh",
        "default_time_dimension": "ordered_at",
        "columns": [
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "customer_id", "type": "INT"},
            {"name": "amount", "type": "DOUBLE"},
            {"name": "status", "type": "TEXT"},
            {"name": "ordered_at", "type": "TIMESTAMP"},
        ],
        "measures": [
            {"name": "revenue", "formula": "amount:sum"},
            {"name": "order_count", "formula": "*:count"},
        ],
        "joins": [
            {
                "target_model": "customers",
                "join_pairs": [["customer_id", "id"]],
                "cardinality": "many_to_one",
            }
        ],
    }
    entry.update(overrides)
    return entry


def _customers(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "version": FORMAT_VERSION,
        "name": "customers",
        "sql_table": "demo.main.customers",
        "data_source": "wh",
        "columns": [
            {"name": "id", "type": "INT", "primary_key": True},
            {"name": "region", "type": "TEXT"},
        ],
    }
    entry.update(overrides)
    return entry


def _write_model(
    root: Path, name: str, entry: dict[str, Any], comment: str = ""
) -> None:
    path = root / BASE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(comment + yaml.safe_dump(entry, sort_keys=False), encoding="utf-8")


def _sql(root: Path, *statements: str) -> None:
    conn = duckdb.connect(str(root / "demo.duckdb"))
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A DuckDB warehouse, SLayer models on it, and a dbt project beside them."""

    _sql(
        tmp_path,
        "CREATE TABLE customers (id INTEGER, region VARCHAR)",
        "INSERT INTO customers VALUES (1, 'north'), (2, 'south'), (3, 'east')",
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "amount DOUBLE, status VARCHAR, ordered_at TIMESTAMP)",
        "INSERT INTO orders VALUES "
        "(1, 1, 10.0, 'paid', '2026-01-01'), (2, 2, 20.0, 'paid', '2026-01-02'), "
        "(3, 3, 5.0, 'open', '2026-01-03'), (4, 1, 7.5, 'paid', '2026-01-04')",
    )
    _write_model(tmp_path, ORDERS, _orders(), comment="# orders, kept by hand\n")
    _write_model(tmp_path, CUSTOMERS, _customers())
    (tmp_path / "dbt_project.yml").write_text(
        'name: slayer_maintain\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (tmp_path / "models").mkdir()
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"duckdb:\n  path: {tmp_path / 'demo.duckdb'}\n"
        "semantic:\n"
        "  vendor: slayer\n"
        "  slayer:\n"
        f"    base_dir: {BASE}\n"
        "    models:\n"
        f"      - {ORDERS}\n"
        f"      - {CUSTOMERS}\n",
        encoding="utf-8",
    )
    return tmp_path


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
        if f["code"] == code and all(f.get(k) == v for k, v in match.items())
    ]
    assert len(found) == 1, (code, match, payload["data"]["findings"])
    return found[0]


# --- the baseline ----------------------------------------------------------------


def test_snapshot_pins_the_slayer_layer_with_its_vendor_release_and_format(
    repo: Path, capsys
):
    payload = _cli(repo, capsys, "maintain", "snapshot")

    assert payload["_exit"] == 0, payload
    stored = json.loads((repo / ".dex" / "snapshot.json").read_text())
    layer = stored["semantic_layer"]
    assert layer["vendor"] == "slayer"
    assert layer["vendor_release"] == SLAYER_VERSION
    assert layer["format_version"] == str(FORMAT_VERSION)
    assert {sm["name"] for sm in layer["semantic_models"]} == {"orders", "customers"}
    assert {m["name"] for m in layer["metrics"]} == {
        "orders.revenue",
        "orders.order_count",
    }
    (join,) = layer["relationships"]
    assert join["cardinality"] == "many_to_one"
    assert layer["relationship_cardinality_captured"] is True


def test_check_reports_every_axis_beside_a_dbt_project(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")

    payload = _cli(repo, capsys, "maintain", "check")

    assert payload["_exit"] == 0, payload
    assert set(payload["data"]["axes_run"]) >= {"schema", "volume", "grain", "semantic"}
    assert payload["data"]["findings"] == []


# --- semantic drift ---------------------------------------------------------------


def test_a_removed_measure_is_its_own_finding(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _write_model(
        repo, ORDERS, _orders(measures=[{"name": "revenue", "formula": "amount:sum"}])
    )

    payload = _cli(repo, capsys, "maintain", "semantic")

    assert payload["_exit"] == 0, payload
    removed = _finding(payload, "definition_removed")
    assert removed["data"] == {"kind": "metric", "name": "orders.order_count"}
    changed = _finding(payload, "definition_changed")
    assert changed["data"]["name"] == "orders"


def test_a_format_change_is_reported_before_any_definition_is_compared(
    repo: Path, capsys
):
    _cli(repo, capsys, "maintain", "snapshot")
    path = repo / ".dex" / "snapshot.json"
    stored = json.loads(path.read_text())
    stored["semantic_layer"]["format_version"] = str(FORMAT_VERSION - 1)
    stored["semantic_layer"]["vendor_release"] = "1.1.0"
    # Every hash different, as an upgrade that changed the model dump would make
    # them: none of it may surface as a changed definition.
    for entry in stored["semantic_layer"]["semantic_models"]:
        entry["content_sha256"] = "0" * 64
    path.write_text(json.dumps(stored))

    payload = _cli(repo, capsys, "maintain", "semantic")

    assert payload["_exit"] == 0, payload
    assert _codes(payload) == ["semantic_format_changed"]
    finding = payload["data"]["findings"][0]
    assert finding["data"]["format_version_before"] == str(FORMAT_VERSION - 1)
    assert finding["data"]["format_version_after"] == str(FORMAT_VERSION)
    assert finding["data"]["vendor_release_before"] == "1.1.0"


def test_a_vendor_change_compares_no_definitions(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    path = repo / ".dex" / "snapshot.json"
    stored = json.loads(path.read_text())
    stored["semantic_layer"]["vendor"] = "ossie"
    stored["semantic_layer"]["semantic_models"][0]["name"] = "commerce.orders"
    path.write_text(json.dumps(stored))

    payload = _cli(repo, capsys, "maintain", "semantic")

    assert payload["_exit"] == 0, payload
    codes = _codes(payload)
    assert codes == ["semantic_vendor_changed"]


# --- grain drift ------------------------------------------------------------------


def test_a_widened_categorical_column_is_caught(repo: Path, capsys):
    """A SLayer model names its table, not a dbt model, and is re-counted (#912)."""

    assert _cli(repo, capsys, "explore", "map")["_exit"] == 0
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO customers VALUES (4, 'west')")

    payload = _cli(repo, capsys, "maintain", "semantic")

    assert payload["_exit"] == 0, payload
    finding = _finding(payload, "dimension_cardinality_changed", column="region")
    assert finding["data"]["semantic_model"] == "customers"
    assert finding["data"]["distinct_after"] == 4


def test_a_declared_primary_key_that_lost_uniqueness_is_caught(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO orders VALUES (4, 2, 1.0, 'paid', '2026-01-05')")

    payload = _cli(repo, capsys, "maintain", "grain")

    assert payload["_exit"] == 0, payload
    finding = _finding(payload, "declared_grain_not_unique", column="order_id")
    assert finding["identifier"].endswith("orders")
    assert finding["data"]["distinct_count"] == 4
    assert finding["data"]["row_count"] == 5


def test_a_join_fanning_out_past_its_declared_cardinality_is_caught(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO customers VALUES (1, 'west')")

    payload = _cli(repo, capsys, "maintain", "grain")

    assert payload["_exit"] == 0, payload
    finding = _finding(payload, "join_cardinality_exceeded")
    assert finding["identifier"].endswith("customers")
    assert finding["data"]["declared_cardinality"] == "many_to_one"
    assert finding["data"]["columns"] == ["id"]
    assert finding["data"]["side_model"] == "customers"


def test_a_one_to_many_join_is_checked_on_its_one_side(repo: Path, capsys):
    joins = [
        {
            "target_model": "orders",
            "join_pairs": [["id", "customer_id"]],
            "cardinality": "one_to_many",
        }
    ]
    _write_model(repo, ORDERS, _orders(joins=[]))
    _write_model(repo, CUSTOMERS, _customers(joins=joins))
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO customers VALUES (2, 'west')")

    payload = _cli(repo, capsys, "maintain", "grain")

    finding = _finding(payload, "join_cardinality_exceeded")
    assert finding["identifier"].endswith("customers")
    assert finding["data"]["declared_cardinality"] == "many_to_one"


def test_a_join_with_no_declared_cardinality_is_reported_unchecked(repo: Path, capsys):
    joins = [{"target_model": "customers", "join_pairs": [["customer_id", "id"]]}]
    _write_model(repo, ORDERS, _orders(joins=joins))
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "INSERT INTO customers VALUES (1, 'west')")

    payload = _cli(repo, capsys, "maintain", "grain")

    assert "join_cardinality_exceeded" not in _codes(payload)
    assert any(
        "no cardinality is declared" in w and "fanout was not checked" in w
        for w in payload["warnings"]
    ), payload["warnings"]


# --- schema drift -----------------------------------------------------------------


def test_a_retyped_column_conflicts_with_its_declared_type(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders ALTER amount TYPE VARCHAR")

    payload = _cli(repo, capsys, "maintain", "schema")

    assert payload["_exit"] == 0, payload
    _finding(payload, "column_retyped", column="amount")
    conflict = _finding(payload, "declared_type_conflict", column="amount")
    assert conflict["data"]["declared_type"] == "DOUBLE"
    assert conflict["data"]["warehouse_type"] == "VARCHAR"
    # SLayer's own cascade: the measure aggregating the column goes with it.
    assert conflict["data"]["removal"]["columns"] == ["amount"]
    assert conflict["data"]["removal"]["measures"] == ["revenue"]


def test_a_dropped_source_column_still_declared_is_caught(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN status")

    payload = _cli(repo, capsys, "maintain", "schema")

    assert payload["_exit"] == 0, payload
    _finding(payload, "column_dropped", column="status")
    missing = _finding(payload, "declared_column_missing", column="status")
    assert missing["data"]["semantic_model"] == "orders"
    assert missing["data"]["model_file"] == ORDERS


def test_a_dropped_join_column_takes_the_join_with_it(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN customer_id")

    payload = _cli(repo, capsys, "maintain", "schema")

    missing = _finding(payload, "declared_column_missing", column="customer_id")
    (edge,) = missing["data"]["removal"]["joins"]
    assert edge["target_model"] == "customers"


def test_a_vanished_table_is_a_whole_model_finding(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "DROP TABLE customers")

    payload = _cli(repo, capsys, "maintain", "schema")

    gone = _finding(payload, "declared_relation_missing")
    assert gone["data"]["semantic_model"] == "customers"
    assert gone["data"]["removal"] == {"whole_model": True}
    # orders loses its join to the model SLayer would drop.
    cascaded = _finding(payload, "declared_dependency_dropped")
    assert cascaded["data"]["semantic_model"] == "orders"


def test_check_carries_the_declared_schema_findings(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN status")

    payload = _cli(repo, capsys, "maintain", "check")

    _finding(payload, "declared_column_missing", column="status")


# --- reconcile --------------------------------------------------------------------


def test_reconcile_plans_the_removal_through_semantic_slayer(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "ALTER TABLE orders DROP COLUMN status")
    _cli(repo, capsys, "maintain", "schema")
    before = (repo / BASE / ORDERS).read_text()

    payload = _cli(repo, capsys, "maintain", "reconcile")

    assert payload["_exit"] == 0, payload
    plan_id = payload["data"]["semantic_plan_id"]
    proposal = next(
        p
        for p in payload["data"]["proposals"]
        if p["finding_code"] == "declared_column_missing"
    )
    assert proposal["kind"] == "mechanical"
    assert proposal["paths"] == [ORDERS]
    # Nothing is written until apply.
    assert (repo / BASE / ORDERS).read_text() == before

    applied = _cli(repo, capsys, "transform", "apply", plan_id)

    assert applied["_exit"] == 0, applied
    after = (repo / BASE / ORDERS).read_text()
    assert after.startswith("# orders, kept by hand\n")
    assert [c["name"] for c in yaml.safe_load(after)["columns"]] == [
        "order_id",
        "customer_id",
        "amount",
        "ordered_at",
    ]
    # The only lines that went are the column's own.
    assert len(before.splitlines()) - len(after.splitlines()) == 2


def test_reconcile_never_removes_a_whole_model(repo: Path, capsys):
    _cli(repo, capsys, "maintain", "snapshot")
    _sql(repo, "DROP TABLE customers")
    _cli(repo, capsys, "maintain", "schema")

    payload = _cli(repo, capsys, "maintain", "reconcile")

    by_code = {p["finding_code"]: p for p in payload["data"]["proposals"]}
    assert by_code["declared_relation_missing"]["kind"] == "advisory"
    # orders' join to customers is a mechanical removal, and validates: the set
    # it leaves still holds customers, so the edit stands on its own.
    assert by_code["declared_dependency_dropped"]["kind"] == "mechanical"
    assert "semantic_plan_id" in payload["data"]


def test_reconcile_native_only_repository_whose_findings_the_layer_does_not_handle(
    tmp_path: Path, capsys
):
    """#994: A native-only repository whose findings the layer doesn't handle
    returns status: ok with advisory proposals and no stored plan."""
    _sql(
        tmp_path,
        "CREATE TABLE customers (id INTEGER, region VARCHAR)",
        "INSERT INTO customers VALUES (1, 'north'), (2, 'south'), (3, 'east')",
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, "
        "amount DOUBLE, status VARCHAR, ordered_at TIMESTAMP)",
        "INSERT INTO orders VALUES "
        "(1, 1, 10.0, 'paid', '2026-01-01'), (2, 2, 20.0, 'paid', '2026-01-02'), "
        "(3, 3, 5.0, 'open', '2026-01-03'), (4, 1, 7.5, 'paid', '2026-01-04')",
    )
    _write_model(tmp_path, ORDERS, _orders(), comment="# orders, kept by hand\n")
    _write_model(tmp_path, CUSTOMERS, _customers())
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"duckdb:\n  path: {tmp_path / 'demo.duckdb'}\n"
        "semantic:\n"
        "  vendor: slayer\n"
        "  slayer:\n"
        f"    base_dir: {BASE}\n"
        "    models:\n"
        f"      - {ORDERS}\n"
        f"      - {CUSTOMERS}\n",
        encoding="utf-8",
    )
    _cli(tmp_path, capsys, "maintain", "snapshot")
    _sql(tmp_path, "INSERT INTO orders VALUES (4, 2, 1.0, 'paid', '2026-01-05')")
    _cli(tmp_path, capsys, "maintain", "grain")

    payload = _cli(tmp_path, capsys, "maintain", "reconcile")

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


# --- nothing SLayer-side is opened ------------------------------------------------


def test_no_maintain_path_opens_a_slayer_datasource_or_storage(
    repo: Path, capsys, monkeypatch
):
    import slayer.engine.schema_drift as schema_drift
    import slayer.sql.engine_factory as engine_factory
    import slayer.storage.yaml_storage as yaml_storage

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("maintain opened SLayer storage or a datasource")

    monkeypatch.setattr(yaml_storage.YAMLStorage, "__init__", refuse)
    monkeypatch.setattr(engine_factory, "get_engine", refuse)
    for name in (
        "validate_datasource",
        "_live_schema_for_datasource",
        "_probe_connect",
    ):
        monkeypatch.setattr(schema_drift, name, refuse)

    assert _cli(repo, capsys, "maintain", "snapshot")["_exit"] == 0
    _sql(
        repo,
        "ALTER TABLE orders DROP COLUMN status",
        "INSERT INTO customers VALUES (1, 'west')",
    )
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
        ("INTEGER", "INT"),
        ("BIGINT", "INT"),
        ("INT64", "INT"),
        ("NUMBER(38,0)", "DOUBLE"),
        ("DECIMAL(10,2)", "DOUBLE"),
        ("double precision", "DOUBLE"),
        ("VARCHAR", "TEXT"),
        ("character varying", "TEXT"),
        ("STRING", "TEXT"),
        ("BOOLEAN", "BOOLEAN"),
        ("DATE", "DATE"),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP"),
        ("TIMESTAMP_NTZ", "TIMESTAMP"),
        ("DATETIME", "TIMESTAMP"),
        ("INTERVAL", None),
        ("JSON", None),
        ("STRUCT(a INTEGER)", None),
        ("INTEGER[]", None),
        ("GEOGRAPHY", None),
        ("", None),
    ],
)
def test_a_connector_type_maps_to_slayer_only_when_unambiguous(spelled, expected):
    mapped = data_type(spelled)
    assert (mapped.value if mapped is not None else None) == expected


def test_a_removal_cuts_only_the_entries_it_names():
    text = (
        "# kept\n"
        "name: orders\n"
        "columns:\n"
        "- name: a  # first\n"
        "  type: INT\n"
        "\n"
        "- name: b\n"
        "  type: TEXT\n"
        "measures:\n"
        "  - name: m\n"
        "    formula: a:sum\n"
    )

    edited = apply_removal(text, {"columns": ["b"], "measures": ["m"]})

    assert edited == (
        "# kept\n"
        "name: orders\n"
        "columns:\n"
        "- name: a  # first\n"
        "  type: INT\n"
        "\n"
        "measures: []\n"
    )


def test_a_removal_it_cannot_place_is_not_offered():
    flow = "name: orders\ncolumns: [{name: a}, {name: b}]\n"

    assert apply_removal(flow, {"columns": ["b"]}) is None


def test_a_join_is_matched_by_target_and_pairs():
    text = (
        "joins:\n"
        "- target_model: customers\n"
        "  join_pairs:\n"
        "  - [customer_id, id]\n"
        "- target_model: customers\n"
        "  name: billing\n"
        "  join_pairs:\n"
        "  - [billing_id, id]\n"
    )
    edge = {
        "target_model": "customers",
        "name": None,
        "join_pairs": [["billing_id", "id"]],
    }

    edited = apply_removal(text, {"joins": [edge]})

    assert yaml.safe_load(edited) == {
        "joins": [{"target_model": "customers", "join_pairs": [["customer_id", "id"]]}]
    }
