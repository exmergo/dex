"""Native Ossie relationships through the actual command surface (#408).

A real DuckDB warehouse, a native Ossie document declaring it, and every
command that reads that declared graph: `relationships`, `map`, `diagram`, and
`--verify`. Unlike the rest of the Ossie suite (which reads documents through
narrow unit calls) this exercises the whole CLI path the way an agent actually
runs it, over a document shaped like the "completion proof" issue #408 asks
for: a single-column pair, a composite pair, a same-endpoint pair that
contradicts another declaration, and an opaque query-backed dataset.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config

from .conftest import dataset, document, field, model, write


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


@pytest.fixture
def commerce_repo(tmp_path: Path) -> Path:
    """A DuckDB warehouse plus a native Ossie document declaring it.

    ``orders -> customers`` is a clean single-column join (every customer_id
    has a parent). ``order_item_details -> order_items`` is a clean composite
    join on ``(order_id, line_no)``. A second, contradicting declaration
    joins ``orders`` and ``customers`` again on different columns, the
    same-endpoint disagreement #408's conflict detection exists for.
    ``recent_orders`` is a query-backed dataset with no physical relation, the
    opaque-source case.
    """

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "demo.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, region VARCHAR)"
    )
    conn.execute("INSERT INTO orders VALUES (1, 1, 'US'), (2, 2, 'EU'), (3, 1, 'US')")
    conn.execute("CREATE TABLE customers (customer_id INTEGER, country_code VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'US'), (2, 'EU')")
    conn.execute(
        "CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku VARCHAR)"
    )
    conn.execute("INSERT INTO order_items VALUES (1, 1, 'A'), (1, 2, 'B'), (2, 1, 'C')")
    conn.execute(
        "CREATE TABLE order_item_details (order_id INTEGER, line_no INTEGER, "
        "detail VARCHAR)"
    )
    conn.execute(
        "INSERT INTO order_item_details VALUES (1, 1, 'x'), (1, 2, 'y'), (2, 1, 'z')"
    )
    conn.close()

    doc = document(
        model(
            "commerce",
            dataset(
                "orders",
                "demo.main.orders",
                field("order_id"),
                field("customer_id"),
                field("region"),
                primary_key=["order_id"],
            ),
            dataset(
                "customers",
                "demo.main.customers",
                field("customer_id"),
                field("country_code"),
                primary_key=["customer_id"],
            ),
            dataset(
                "order_items",
                "demo.main.order_items",
                field("order_id"),
                field("line_no"),
                field("sku"),
                primary_key=["order_id", "line_no"],
            ),
            dataset(
                "order_item_details",
                "demo.main.order_item_details",
                field("order_id"),
                field("line_no"),
                field("detail"),
            ),
            dataset(
                "recent_orders",
                "SELECT * FROM demo.main.orders WHERE region = 'US'",
                field("order_id"),
            ),
            relationships=[
                {
                    "name": "orders_to_customers",
                    "from": "orders",
                    "to": "customers",
                    "from_columns": ["customer_id"],
                    "to_columns": ["customer_id"],
                },
                {
                    "name": "orders_to_customers_by_region",
                    "from": "orders",
                    "to": "customers",
                    "from_columns": ["region"],
                    "to_columns": ["country_code"],
                },
                {
                    "name": "details_to_items",
                    "from": "order_item_details",
                    "to": "order_items",
                    "from_columns": ["order_id", "line_no"],
                    "to_columns": ["order_id", "line_no"],
                },
            ],
        )
    )
    write(tmp_path, "commerce.ossie.yaml", doc)

    save_config(
        DexConfig(
            connector="duckdb",
            duckdb={"path": "demo.duckdb"},
            semantic={"vendor": "ossie", "ossie": {"files": ["commerce.ossie.yaml"]}},
        ),
        tmp_path,
    )
    return tmp_path


def test_declared_edges_carry_single_and_composite_pairs(commerce_repo: Path, capsys):
    data = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--repo-root",
            str(commerce_repo),
        ],
        capsys,
    )["data"]

    declared = {
        r["from_dataset"]: r for r in data["relationships"] if r["kind"] == "declared"
    }
    single = next(
        r
        for r in data["relationships"]
        if r["kind"] == "declared" and r["from_columns"] == ["customer_id"]
    )
    assert single["to_dataset"].endswith("customers")
    composite = next(
        r
        for r in data["relationships"]
        if r["kind"] == "declared" and set(r["from_columns"]) == {"order_id", "line_no"}
    )
    assert composite["to_columns"] == ["order_id", "line_no"]
    assert declared  # sanity: at least one declared edge landed


def test_contradicting_declarations_are_flagged_as_a_conflict(
    commerce_repo: Path, capsys
):
    data = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--repo-root",
            str(commerce_repo),
        ],
        capsys,
    )["data"]

    assert len(data["conflicts"]) == 1
    conflict = data["conflicts"][0]
    assert conflict["from_dataset"].endswith("orders")
    assert conflict["to_dataset"].endswith("customers")
    pairs = {tuple(d["column_pairs"][0]) for d in conflict["declarations"]}
    assert pairs == {("customer_id", "customer_id"), ("region", "country_code")}
    assert any("disagree" in n for n in data["notes"])
    # Both contradicting edges are still kept, not collapsed into one.
    assert (
        sum(
            1
            for r in data["relationships"]
            if r["kind"] == "declared"
            and r["from_dataset"].endswith("orders")
            and r["to_dataset"].endswith("customers")
        )
        == 2
    )


def test_map_persists_composite_edges_and_conflicts_to_the_cache(
    commerce_repo: Path, capsys
):
    data = _run(
        ["explore", "map", "--use-project", "--repo-root", str(commerce_repo)],
        capsys,
    )["data"]

    assert len(data["conflicts"]) == 1
    composite = next(
        r for r in data["edges"] if set(r["from_columns"]) == {"order_id", "line_no"}
    )
    assert composite["to_columns"] == ["order_id", "line_no"]


def test_diagram_pairs_every_composite_column_in_the_mermaid_label(
    commerce_repo: Path, capsys
):
    assert (
        main(["explore", "map", "--use-project", "--repo-root", str(commerce_repo)])
        == 0
    )
    capsys.readouterr()

    data = _run(["explore", "diagram", "--repo-root", str(commerce_repo)], capsys)[
        "data"
    ]

    assert "order_id = order_id, line_no = line_no" in data["mermaid"]


def test_verify_confirms_the_declared_joins_against_real_overlap(
    commerce_repo: Path, capsys
):
    data = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--verify",
            "--repo-root",
            str(commerce_repo),
        ],
        capsys,
    )["data"]

    single = next(
        r
        for r in data["relationships"]
        if r["kind"] == "declared" and r["from_columns"] == ["customer_id"]
    )
    assert single["verified"] is True
    assert single["orphan_fraction"] == 0.0

    composite = next(
        r
        for r in data["relationships"]
        if r["kind"] == "declared" and set(r["from_columns"]) == {"order_id", "line_no"}
    )
    assert composite["verified"] is True
    assert composite["orphan_fraction"] == 0.0
