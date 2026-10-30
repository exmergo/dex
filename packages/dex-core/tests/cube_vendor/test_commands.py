"""`semantic.vendor: cube` through the command surface (#910).

`explore semantic list` reads the layer with no Cube server and no warehouse
connection. `explore relationships|map --use-project` carry Cube's joins and
primary keys at confidence 1.0 on a real DuckDB warehouse. `maintain snapshot`
fingerprints the layer. A query asks a Cube server to render it, after the PII
gate: the governed path itself is `test_query.py`.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cli import main
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic.backend import resolve_semantic_layer
from exmergo_dex_core.explore.semantic.cube import CUBE_CATALOG_GAPS, LocalCubeLayer

from .conftest import ORDERS_LAYER, cube, repo, view, write


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


@pytest.fixture
def no_network(monkeypatch):
    """Any socket a command opens fails the test: the catalog needs no server."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a Cube catalog read opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


SALES = view(
    "sales",
    {"join_path": "orders", "includes": ["revenue", "status"]},
    {"join_path": "orders.customers", "includes": ["region"], "prefix": True},
)


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    files = write(tmp_path, "model/legacy.js", raw="cube(`legacy`, {});\n")
    return repo(tmp_path, *ORDERS_LAYER, views=[SALES], files=files)


# --- the catalog, with nothing to connect to ------------------------------------------


def test_explore_semantic_list_reads_cube_with_no_server_and_no_warehouse(
    layer, capsys, no_network
):
    before = sorted(p.relative_to(layer) for p in layer.rglob("*"))

    rc, envelope = _run(capsys, layer, "explore", "semantic", "list")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert (data["vendor"], data["deployment"], data["execution"]) == (
        "cube",
        "local",
        "dex",
    )
    assert data["unavailable"] == CUBE_CATALOG_GAPS
    assert {m["name"] for m in data["semantic_models"]} == {
        "orders",
        "customers",
        "sales",
    }
    assert {m["name"] for m in data["metrics"]} == {
        "orders.revenue",
        "orders.order_count",
        "orders.aov",
        "customers.customer_count",
        "sales.revenue",
    }
    assert data["compiled_only"] == [
        {
            "file": "model/legacy.js",
            "reason": "a JavaScript model needs Node to compile",
        }
    ]
    assert any(n.startswith("model/legacy.js: compiled-only") for n in data["notes"])
    # The catalog wrote nothing, not even a cache.
    assert sorted(p.relative_to(layer) for p in layer.rglob("*")) == before


def test_explore_semantic_list_narrows_by_metric_dimension_and_search(
    layer, capsys, no_network
):
    _, scoped = _run(
        capsys, layer, "explore", "semantic", "list", "--metric", "orders.aov"
    )
    _, grouped = _run(
        capsys,
        layer,
        "explore",
        "semantic",
        "list",
        "--for-dimension",
        "sales.customers_region",
    )
    _, searched = _run(
        capsys, layer, "explore", "semantic", "list", "--search", "gross"
    )

    assert [m["name"] for m in scoped["data"]["metrics"]] == ["orders.aov"]
    assert {m["name"] for m in scoped["data"]["measures"]} == {"revenue", "order_count"}
    assert [m["name"] for m in grouped["data"]["metrics"]] == ["sales.revenue"]
    # The word is in revenue's description; aov and the view's revenue read it.
    assert {m["name"] for m in searched["data"]["metrics"]} == {
        "orders.revenue",
        "orders.aov",
        "sales.revenue",
    }


def test_a_layer_of_only_unreadable_files_is_refused_with_the_files_named(
    tmp_path, capsys
):
    files = write(tmp_path, "model/broken.yml", raw="cubes: [x,\n")
    root = repo(tmp_path, files=files)

    rc, envelope = _run(capsys, root, "explore", "semantic", "list")

    assert rc != 0
    assert "model/broken.yml" in envelope["errors"][0]


# --- values: PII first, then the renderer ---------------------------------------------


def test_cube_values_refuse_a_pii_dimension_before_asking_cube(
    tmp_path, capsys, monkeypatch
):
    from exmergo_dex_core.cube.loader import load_files

    from .fake_cube import FakeCube, meta_for

    people = cube(
        "people",
        dimensions=[
            {"name": "email", "sql": "email", "type": "string"},
            {"name": "team", "sql": "team", "type": "string"},
        ],
    )
    root = repo(tmp_path, people)
    server = FakeCube(meta=meta_for(load_files(root, ["model/people.yml"])))
    server.recordings = {}
    server.install(monkeypatch)

    rc, pii = _run(capsys, root, "explore", "semantic", "values", "people.email")
    _, unknown = _run(capsys, root, "explore", "semantic", "values", "people.tem")

    assert rc != 0 and pii["reason"] == "guard"
    assert "PII" in pii["errors"][0]
    assert "people.tem" in unknown["errors"][0]
    assert server.calls == []

    _, other = _run(capsys, root, "explore", "semantic", "values", "people.team")
    assert other["errors"][0].startswith("could not resolve the values of people.team")
    assert server.paths == ["/cubejs-api/v1/meta", "/cubejs-api/v1/sql"]


def test_the_engine_resolves_the_cube_layer_and_has_no_hosted_override(tmp_path):
    engine = DexEngine.from_repo(repo(tmp_path, *ORDERS_LAYER))

    layer = resolve_semantic_layer(engine, local=True)
    keys, composite = layer.declared_keys()

    assert isinstance(layer, LocalCubeLayer)
    assert {(k.model, k.column) for k in keys} == {
        ("orders", "order_id"),
        ("customers", "customer_key"),
    }
    assert composite == []
    with pytest.raises(Exception, match="no hosted"):
        resolve_semantic_layer(engine, api=True)


# --- on a real warehouse ------------------------------------------------------------


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "demo.duckdb"))
    conn.execute(
        "CREATE TABLE orders (order_id INTEGER, line_no INTEGER, customer_id INTEGER,"
        " status VARCHAR, ordered_at TIMESTAMP, amount DOUBLE)"
    )
    conn.execute(
        "INSERT INTO orders VALUES (1, 1, 10, 'paid', '2026-01-01', 5),"
        " (1, 2, 10, 'paid', '2026-01-01', 5), (2, 1, 20, 'test', '2026-01-01', 3),"
        " (2, 2, 20, 'test', '2026-01-02', 3)"
    )
    conn.execute("CREATE TABLE customers (customer_key INTEGER, region VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (10, 'EU'), (20, 'US')")
    conn.close()

    lines = cube(
        "orders",
        joins=[
            {
                "name": "customers",
                "sql": "{CUBE}.customer_id = {customers.id}",
                "relationship": "belongs_to",
            }
        ],
        dimensions=[
            {
                "name": "order_id",
                "sql": "order_id",
                "type": "number",
                "primary_key": True,
            },
            {
                "name": "line_no",
                "sql": "{CUBE}.line_no",
                "type": "number",
                "primary_key": True,
            },
            {"name": "status", "sql": "status", "type": "string"},
        ],
        measures=[{"name": "revenue", "sql": "amount", "type": "sum"}],
    )
    customers = cube(
        "customers",
        dimensions=[
            {
                "name": "id",
                "sql": "customer_key",
                "type": "number",
                "primary_key": True,
            },
            {"name": "region", "sql": "region", "type": "string"},
        ],
    )
    return repo(tmp_path, lines, customers, config="duckdb:\n  path: demo.duckdb\n")


def test_relationships_carry_cube_joins_at_full_confidence(warehouse, capsys):
    rc, envelope = _run(
        capsys, warehouse, "explore", "relationships", "--use-project", "--verify"
    )

    assert rc == 0, envelope["errors"]
    (edge,) = [r for r in envelope["data"]["relationships"] if r["kind"] == "declared"]
    assert (edge["from_dataset"], edge["to_dataset"]) == (
        "demo.main.orders",
        "demo.main.customers",
    )
    assert (edge["from_columns"], edge["to_columns"]) == (
        ["customer_id"],
        ["customer_key"],
    )
    assert edge["confidence"] == 1.0
    assert edge["declared_by"] == "cube"
    assert edge["verified"] is True and edge["orphan_fraction"] == 0.0


def test_map_carries_cube_keys_joins_and_semantic_models(warehouse, capsys):
    rc, envelope = _run(capsys, warehouse, "explore", "map", "--use-project")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    by_relation = {o["identifier"]: o for o in data["objects"]}
    # The composite primary key is the grain: no single column is unique.
    assert by_relation["demo.main.orders"]["grain"] == ["order_id", "line_no"]
    assert by_relation["demo.main.orders"]["semantic_models"] == ["orders"]
    assert by_relation["demo.main.customers"]["semantic_models"] == ["customers"]
    (edge,) = [e for e in data["edges"] if e["kind"] == "declared"]
    assert edge["confidence"] == 1.0 and edge["declared_by"] == "cube"


def test_maintain_snapshot_fingerprints_the_cube_layer(warehouse, capsys):
    rc, envelope = _run(capsys, warehouse, "maintain", "snapshot")

    assert rc == 0, envelope["errors"]
    snapshot = json.loads((warehouse / ".dex" / "snapshot.json").read_text())
    layer = snapshot["semantic_layer"]
    assert layer["vendor"] == "cube"
    assert layer["vendor_release"]
    assert {m["name"] for m in layer["semantic_models"]} == {"orders", "customers"}
    orders = next(m for m in layer["semantic_models"] if m["name"] == "orders")
    assert orders["keys"] == [["order_id", "line_no"]]
    assert orders["relation"] == "demo.main.orders"
    (join,) = layer["relationships"]
    assert join["cardinality"] == "many_to_one"


def test_a_layer_change_is_reported_as_definition_drift(warehouse, capsys):
    assert _run(capsys, warehouse, "maintain", "snapshot")[0] == 0
    path = warehouse / "model" / "orders.yml"
    body = yaml.safe_load(path.read_text())
    body["cubes"][0]["measures"] = []
    path.write_text(yaml.safe_dump(body), encoding="utf-8")

    rc, envelope = _run(capsys, warehouse, "maintain", "semantic")

    assert rc == 0, envelope["errors"]
    codes = {(f["code"], f.get("object")) for f in envelope["data"]["findings"]}
    assert any(code == "definition_removed" for code, _ in codes), codes


def test_a_cube_key_the_data_contradicts_is_named_against_the_cube_layer(
    tmp_path, capsys
):
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "demo.duckdb"))
    conn.execute("CREATE TABLE items (item_id INTEGER, qty INTEGER)")
    conn.execute("INSERT INTO items VALUES (1, 2), (1, 3), (2, 2), (3, 2)")
    conn.close()
    items = cube(
        "items",
        dimensions=[
            {"name": "item_id", "sql": "item_id", "type": "number", "primary_key": True}
        ],
    )
    root = repo(tmp_path, items, config="duckdb:\n  path: demo.duckdb\n")

    rc, envelope = _run(
        capsys, root, "explore", "profile", "demo.main.items", "--use-project"
    )

    assert rc == 0, envelope["errors"]
    (dataset,) = envelope["data"]["datasets"]
    assert (
        "item_id is declared unique in the Cube layer but profiling found duplicates"
        in dataset["data_quality"]
    )


def test_maintain_grain_measures_a_cube_primary_key(tmp_path, capsys):
    """The layer-neutral grain axis reads the Cube fingerprint's keys."""

    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "demo.duckdb"))
    conn.execute("CREATE TABLE items (item_id INTEGER, qty INTEGER)")
    conn.execute("INSERT INTO items VALUES (1, 2), (1, 3), (2, 2), (3, 2)")
    conn.close()
    items = cube(
        "items",
        dimensions=[
            {"name": "item_id", "sql": "item_id", "type": "number", "primary_key": True}
        ],
    )
    root = repo(tmp_path, items, config="duckdb:\n  path: demo.duckdb\n")
    assert _run(capsys, root, "maintain", "snapshot")[0] == 0

    rc, envelope = _run(capsys, root, "maintain", "grain")

    assert rc == 0, envelope["errors"]
    codes = {(f["code"], f["identifier"]) for f in envelope["data"]["findings"]}
    assert ("declared_grain_not_unique", "demo.main.items") in codes
