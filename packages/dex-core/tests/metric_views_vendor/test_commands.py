"""`semantic.vendor: databricks` through the command surface (#1257).

`explore semantic list` reads the layer with no workspace and no warehouse
connection. `explore relationships|map|profile --use-project` carry each join and
each `rely` key at confidence 1.0 on a real DuckDB warehouse. `maintain snapshot`
fingerprints the layer, `maintain semantic` reports a changed view, and `maintain
grain` measures a `rely` key. A query and a values read are refused by name, the
values read after the PII gate.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cli import main

from .conftest import ORDERS, repo, view


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


@pytest.fixture
def no_network(monkeypatch):
    """Any socket a command opens fails the test: the catalog needs no workspace."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a Metric View catalog read opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


PEOPLE = view(
    "demo.main.people",
    fields=[{"name": "email", "expr": "email"}, {"name": "tier", "expr": "tier"}],
    measures=[{"name": "people", "expr": "COUNT(1)"}],
)


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    return repo(tmp_path, orders=ORDERS, people=PEOPLE)


# --- the catalog, with nothing to connect to ------------------------------------------


def test_semantic_list_reads_the_layer_with_no_network(layer, capsys, no_network):
    rc, envelope = _run(capsys, layer, "explore", "semantic", "list")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert (data["vendor"], data["deployment"], data["execution"]) == (
        "databricks",
        "local",
        "dex",
    )
    assert {m["name"] for m in data["semantic_models"]} == {"orders", "people"}
    assert "orders.aov" in {m["name"] for m in data["metrics"]}
    assert data["unavailable"]["entities"]


def test_the_narrowing_flags_answer(layer, capsys, no_network):
    rc, by_metric = _run(
        capsys, layer, "explore", "semantic", "list", "--metric", "orders.revenue"
    )
    assert rc == 0, by_metric["errors"]
    assert [m["name"] for m in by_metric["data"]["metrics"]] == ["orders.revenue"]

    rc, by_dimension = _run(
        capsys,
        layer,
        "explore",
        "semantic",
        "list",
        "--for-dimension",
        "people.tier",
    )
    assert rc == 0, by_dimension["errors"]
    assert {m["name"] for m in by_dimension["data"]["metrics"]} == {"people.people"}

    rc, searched = _run(
        capsys, layer, "explore", "semantic", "list", "--search", "fulfilment"
    )
    assert rc == 0, searched["errors"]
    assert searched["data"]["metrics"], "the field's comment names fulfilment"


def test_a_query_is_refused_by_name(layer, capsys, no_network):
    rc, envelope = _run(capsys, layer, "explore", "semantic", "query", "orders.revenue")

    assert rc != 0
    assert envelope["status"] == "error"
    assert "#964" in " ".join(envelope["errors"])


def test_values_of_a_personal_field_are_refused_as_pii_first(layer, capsys, no_network):
    rc, envelope = _run(capsys, layer, "explore", "semantic", "values", "people.email")

    assert rc != 0
    assert envelope["reason"] == "guard", envelope
    assert "#964" not in " ".join(envelope["errors"])


def test_values_of_any_other_field_are_refused_for_want_of_a_runtime(
    layer, capsys, no_network
):
    rc, envelope = _run(capsys, layer, "explore", "semantic", "values", "people.tier")

    assert rc != 0
    assert "#964" in " ".join(envelope["errors"])


def test_an_unreadable_layer_says_which_file(tmp_path, capsys, no_network):
    root = repo(
        tmp_path, orders=view(fields=[{"name": "x", "expr": "x"}]) | {"version": 0.1}
    )

    rc, envelope = _run(capsys, root, "explore", "semantic", "list")

    assert rc != 0
    assert "views/orders.yaml" in " ".join(envelope["errors"])


# --- on a real warehouse ------------------------------------------------------------


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "demo.duckdb"))
    conn.execute(
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, status VARCHAR,"
        " amount DOUBLE)"
    )
    conn.execute(
        "INSERT INTO orders VALUES (1, 10, 'paid', 5), (2, 10, 'paid', 5),"
        " (3, 20, 'open', 3), (4, 20, 'open', 3)"
    )
    conn.execute("CREATE TABLE customers (customer_key INTEGER, region VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (10, 'EU'), (20, 'US')")
    conn.close()
    orders = view(
        joins=[
            {
                "name": "customer",
                "source": "demo.main.customers",
                "on": "source.customer_id = customer.customer_key",
                "rely": {"at_most_one_match": True},
            }
        ],
        fields=[
            {"name": "status", "expr": "status"},
            {"name": "region", "expr": "customer.region"},
        ],
        measures=[{"name": "revenue", "expr": "SUM(amount)"}],
    )
    return repo(tmp_path, orders=orders, config="duckdb:\n  path: demo.duckdb\n")


def test_relationships_carry_each_join_at_full_confidence(warehouse, capsys):
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
    assert edge["declared_by"] == "databricks relationship 'orders.customer'"
    assert edge["verified"] is True and edge["orphan_fraction"] == 0.0


def test_map_carries_the_join_and_the_semantic_model(warehouse, capsys):
    rc, envelope = _run(capsys, warehouse, "explore", "map", "--use-project")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    by_relation = {o["identifier"]: o for o in data["objects"]}
    assert by_relation["demo.main.orders"]["semantic_models"] == ["orders"]
    assert by_relation["demo.main.customers"]["grain"] == ["customer_key"]
    (edge,) = [e for e in data["edges"] if e["kind"] == "declared"]
    assert edge["confidence"] == 1.0
    assert edge["declared_by"].startswith("databricks")


def test_maintain_snapshot_fingerprints_the_layer(warehouse, capsys):
    rc, envelope = _run(capsys, warehouse, "maintain", "snapshot")

    assert rc == 0, envelope["errors"]
    layer = json.loads((warehouse / ".dex" / "snapshot.json").read_text())[
        "semantic_layer"
    ]
    assert (layer["vendor"], layer["format_version"]) == ("databricks", "1.1")
    models = {m["name"]: m for m in layer["semantic_models"]}
    assert set(models) == {"orders", "orders.customer"}
    assert models["orders.customer"]["keys"] == [["customer_key"]]
    assert models["orders.customer"]["dimensions"] == {"region": "region"}
    (join,) = layer["relationships"]
    assert join["cardinality"] == "many_to_one"


def test_a_removed_measure_is_reported_as_definition_drift(warehouse, capsys):
    assert _run(capsys, warehouse, "maintain", "snapshot")[0] == 0
    path = warehouse / "views" / "orders.yaml"
    body = yaml.safe_load(path.read_text())
    body["measures"] = [{"name": "orders", "expr": "COUNT(1)"}]
    path.write_text(yaml.safe_dump(body), encoding="utf-8")

    rc, envelope = _run(capsys, warehouse, "maintain", "semantic")

    assert rc == 0, envelope["errors"]
    codes = {(f["code"], f.get("object")) for f in envelope["data"]["findings"]}
    assert any(code == "definition_removed" for code, _ in codes), codes


def test_maintain_grain_measures_a_rely_key(tmp_path, capsys):
    """A `rely` the data contradicts returns wrong numbers in Databricks, and the
    layer-neutral grain axis is what catches it."""

    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "demo.duckdb"))
    conn.execute("CREATE TABLE orders (order_id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 10), (2, 20)")
    conn.execute("CREATE TABLE customers (customer_key INTEGER, region VARCHAR)")
    conn.execute(
        "INSERT INTO customers VALUES (10, 'EU'), (10, 'US'), (20, 'US'), (30, 'EU')"
    )
    conn.close()
    orders = view(
        joins=[
            {
                "name": "customer",
                "source": "demo.main.customers",
                "on": "source.customer_id = customer.customer_key",
                "rely": {"at_most_one_match": True},
            }
        ],
        fields=[{"name": "region", "expr": "customer.region"}],
    )
    root = repo(tmp_path, orders=orders, config="duckdb:\n  path: demo.duckdb\n")
    assert _run(capsys, root, "explore", "map")[0] == 0
    assert _run(capsys, root, "maintain", "snapshot")[0] == 0

    rc, envelope = _run(capsys, root, "maintain", "grain")

    assert rc == 0, envelope["errors"]
    codes = {f["code"] for f in envelope["data"]["findings"]}
    assert "declared_grain_not_unique" in codes, envelope["data"]["findings"]


def test_maintain_grain_measures_a_many_to_one_join_without_rely(tmp_path, capsys):
    """`cardinality: many_to_one` (the default) is itself a claim that the joined
    side is unique on the join columns, and the layer-neutral grain axis holds the
    fingerprint's arity to the data whether or not `rely` is written."""

    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "demo.duckdb"))
    conn.execute("CREATE TABLE orders (order_id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 10), (2, 20)")
    conn.execute("CREATE TABLE customers (customer_key INTEGER, region VARCHAR)")
    conn.execute(
        "INSERT INTO customers VALUES (10, 'EU'), (10, 'US'), (20, 'US'), (30, 'EU')"
    )
    conn.close()
    orders = view(
        joins=[
            {
                "name": "customer",
                "source": "demo.main.customers",
                "on": "source.customer_id = customer.customer_key",
            }
        ],
        fields=[{"name": "region", "expr": "customer.region"}],
    )
    root = repo(tmp_path, orders=orders, config="duckdb:\n  path: demo.duckdb\n")
    assert _run(capsys, root, "explore", "map")[0] == 0
    assert _run(capsys, root, "maintain", "snapshot")[0] == 0

    rc, envelope = _run(capsys, root, "maintain", "grain")

    assert rc == 0, envelope["errors"]
    assert [f["code"] for f in envelope["data"]["findings"]] == [
        "join_cardinality_exceeded"
    ]


def test_search_finds_a_field_and_a_measure_by_their_synonyms(
    tmp_path, capsys, no_network
):
    """Synonyms exist so an agent can find an element by another word."""

    body = view(
        fields=[{"name": "category", "expr": "category", "synonyms": ["department"]}],
        measures=[
            {"name": "revenue", "expr": "SUM(amount)", "synonyms": ["sales"]},
            {"name": "units", "expr": "SUM(quantity)"},
        ],
    )
    root = repo(tmp_path, orders=body)

    rc, by_field = _run(
        capsys, root, "explore", "semantic", "list", "--search", "department"
    )
    assert rc == 0, by_field["errors"]
    assert {m["name"] for m in by_field["data"]["metrics"]} == {
        "orders.revenue",
        "orders.units",
    }

    rc, by_measure = _run(
        capsys, root, "explore", "semantic", "list", "--search", "sales"
    )
    assert rc == 0, by_measure["errors"]
    assert [m["name"] for m in by_measure["data"]["metrics"]] == ["orders.revenue"]
