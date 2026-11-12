"""`semantic.vendor: snowflake` through the command surface (#1291).

`explore semantic list` reads the layer with no Snowflake connection and no
warehouse. `explore relationships|map --use-project` carry each relationship and
key at confidence 1.0 on a real DuckDB warehouse. `maintain snapshot`
fingerprints the layer, `maintain semantic` reports a changed definition, and
`maintain grain` measures a declared key and a relationship's arity. A query and
a values read are refused by name, the values read after the PII gate.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cli import main

from .conftest import SHOP, relationship, repo, sv, table


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    """Run the CLI against ``root`` and return its exit code and envelope."""
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


@pytest.fixture
def no_network(monkeypatch):
    """Any socket a command opens fails the test: the catalog needs no account."""

    def refuse(*_args, **_kwargs):
        """Any socket connect fails the test."""
        raise AssertionError("a Semantic View catalog read opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def layer(tmp_path: Path) -> Path:
    """`SHOP` on Snowflake, with no Snowflake to reach."""
    return repo(tmp_path, SHOP, connector="snowflake")


# --- the catalog, with nothing to connect to ------------------------------------------


def test_semantic_list_reads_the_layer_with_no_network(layer, capsys, no_network):
    """With its provenance, entities and declared gaps."""
    rc, envelope = _run(capsys, layer, "explore", "semantic", "list")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert (data["vendor"], data["deployment"], data["execution"]) == (
        "snowflake",
        "local",
        "dex",
    )
    assert {m["name"] for m in data["semantic_models"]} == {
        "shop.orders",
        "shop.customers",
        "shop.lines",
    }
    assert {e["name"] for e in data["entities"]} == {"shop.orders", "shop.customers"}
    assert "paid" not in json.dumps(data), "a sample value reached the envelope"


def test_the_narrowing_flags_answer(layer, capsys, no_network):
    """`--metric`, `--for-dimension`, and `--search` over synonyms."""
    rc, by_metric = _run(
        capsys, layer, "explore", "semantic", "list", "--metric", "shop.orders.revenue"
    )
    assert rc == 0, by_metric["errors"]
    assert [m["name"] for m in by_metric["data"]["metrics"]] == ["shop.orders.revenue"]

    rc, by_dimension = _run(
        capsys,
        layer,
        "explore",
        "semantic",
        "list",
        "--for-dimension",
        "shop.customers.region",
    )
    assert rc == 0, by_dimension["errors"]
    assert "shop.lines.units" in {m["name"] for m in by_dimension["data"]["metrics"]}

    rc, searched = _run(
        capsys, layer, "explore", "semantic", "list", "--search", "sales"
    )
    assert rc == 0, searched["errors"]
    assert "shop.orders.revenue" in {m["name"] for m in searched["data"]["metrics"]}


def test_a_query_is_refused_by_name(layer, capsys, no_network):
    """Naming #1290."""
    rc, envelope = _run(
        capsys, layer, "explore", "semantic", "query", "shop.orders.revenue"
    )

    assert rc != 0
    assert "#1290" in " ".join(envelope["errors"])


def test_values_of_a_personal_dimension_are_refused_as_pii_first(
    layer, capsys, no_network
):
    """The PII gate speaks before the missing runtime does."""
    rc, envelope = _run(
        capsys, layer, "explore", "semantic", "values", "shop.customers.email"
    )

    assert rc != 0
    assert envelope["reason"] == "guard", envelope
    assert "#1290" not in " ".join(envelope["errors"])


def test_values_of_any_other_dimension_are_refused_for_want_of_a_runtime(
    layer, capsys, no_network
):
    """Naming #1290."""
    rc, envelope = _run(
        capsys, layer, "explore", "semantic", "values", "shop.customers.region"
    )

    assert rc != 0
    assert "#1290" in " ".join(envelope["errors"])


def test_an_unreadable_layer_says_which_file(tmp_path, capsys, no_network):
    """A layer of one refused view names it."""
    root = repo(tmp_path, {"name": "broken", "tables": []}, connector="snowflake")

    rc, envelope = _run(capsys, root, "explore", "semantic", "list")

    assert rc != 0
    assert "views/broken.yaml" in " ".join(envelope["errors"])


# --- on a real warehouse ------------------------------------------------------------


def _warehouse(tmp_path: Path, customers: list[tuple[int, str]]) -> Path:
    """A DuckDB warehouse with orders and customers, and a view relating them."""
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
    conn.executemany("INSERT INTO customers VALUES (?, ?)", customers)
    conn.close()
    view = sv(
        "sales",
        table(
            "orders",
            "demo.main.orders",
            ["order_id"],
            dimensions=[{"name": "status", "expr": "status"}],
            metrics=[{"name": "revenue", "expr": "SUM(amount)"}],
        ),
        table(
            "customers",
            "demo.main.customers",
            ["customer_key"],
            dimensions=[{"name": "region", "expr": "region"}],
        ),
        relationships=[
            relationship(
                "orders_customers",
                "orders",
                "customers",
                ("customer_id", "customer_key"),
            )
        ],
    )
    return repo(tmp_path, view, config="duckdb:\n  path: demo.duckdb\n")


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    """Customers keyed as declared."""
    return _warehouse(tmp_path, [(10, "EU"), (20, "US")])


def test_relationships_carry_each_join_at_full_confidence(warehouse, capsys):
    """Verified, with no orphans."""
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
    assert edge["declared_by"].startswith("snowflake")
    assert edge["verified"] is True and edge["orphan_fraction"] == 0.0


def test_map_carries_the_semantic_model_and_the_declared_grain(warehouse, capsys):
    rc, envelope = _run(capsys, warehouse, "explore", "map", "--use-project")

    assert rc == 0, envelope["errors"]
    by_relation = {o["identifier"]: o for o in envelope["data"]["objects"]}
    assert by_relation["demo.main.orders"]["semantic_models"] == ["sales.orders"]
    assert by_relation["demo.main.customers"]["grain"] == ["customer_key"]


def test_maintain_snapshot_fingerprints_the_layer(warehouse, capsys):
    """Vendor, format version, keys and arity."""
    rc, envelope = _run(capsys, warehouse, "maintain", "snapshot")

    assert rc == 0, envelope["errors"]
    layer = json.loads((warehouse / ".dex" / "snapshot.json").read_text())[
        "semantic_layer"
    ]
    assert layer["vendor"] == "snowflake"
    models = {m["name"]: m for m in layer["semantic_models"]}
    assert models["sales.customers"]["keys"] == [["customer_key"]]
    (join,) = layer["relationships"]
    assert join["cardinality"] == "many_to_one"


def test_a_removed_metric_is_reported_as_definition_drift(warehouse, capsys):
    """Against the snapshot."""
    assert _run(capsys, warehouse, "maintain", "snapshot")[0] == 0
    path = warehouse / "views" / "sales.yaml"
    body = yaml.safe_load(path.read_text())
    body["tables"][0]["metrics"] = [{"name": "orders", "expr": "COUNT(order_id)"}]
    path.write_text(yaml.safe_dump(body), encoding="utf-8")

    rc, envelope = _run(capsys, warehouse, "maintain", "semantic")

    assert rc == 0, envelope["errors"]
    codes = {f["code"] for f in envelope["data"]["findings"]}
    assert "definition_removed" in codes, codes


def test_maintain_grain_measures_a_key_and_a_relationships_arity(tmp_path, capsys):
    """A primary key the data contradicts is a declaration to fix, and a
    many-to-one relationship onto it fans out."""
    root = _warehouse(tmp_path, [(10, "EU"), (10, "US"), (20, "US"), (30, "EU")])
    assert _run(capsys, root, "explore", "map")[0] == 0
    assert _run(capsys, root, "maintain", "snapshot")[0] == 0

    rc, envelope = _run(capsys, root, "maintain", "grain")

    assert rc == 0, envelope["errors"]
    codes = {f["code"] for f in envelope["data"]["findings"]}
    assert {"declared_grain_not_unique", "join_cardinality_exceeded"} <= codes, codes


def test_reconcile_with_no_project_is_advisory_not_a_refusal(tmp_path, capsys):
    """A Semantic View layer with no dbt project beside it is the repository's
    one layer: its findings are advisory, and the key is changed in its file
    until authoring lands (#1292)."""
    root = _warehouse(tmp_path, [(10, "EU"), (10, "US"), (20, "US")])
    assert _run(capsys, root, "explore", "map")[0] == 0
    assert _run(capsys, root, "maintain", "snapshot")[0] == 0
    assert _run(capsys, root, "maintain", "check")[0] == 0

    rc, envelope = _run(capsys, root, "maintain", "reconcile")

    assert rc == 0, envelope["errors"]
    assert envelope["data"].get("plan_id") is None
    actions = " ".join(p["action"] for p in envelope["data"]["proposals"])
    assert "change the primary key in its file" in actions
