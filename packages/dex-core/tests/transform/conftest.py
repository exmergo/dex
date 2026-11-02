"""Fixtures for the transform suite.

The row-population fixtures reproduce the shape that motivated attribution: a
source whose free-text status column and boolean flag disagree with each other,
so a defensible-looking predicate swap moves rows and nothing about the model's
columns or types reveals it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

# The two columns are deliberately inconsistent. 400 orders: `status` carries
# three spellings of cancelled ('CANCELLED', 'cancelled', 'C') and `is_cancelled`
# is set on a different, overlapping set of rows. Filtering on one is not
# filtering on the other, which is the whole point.
_ORDERS = [
    (
        i,
        None if i % 200 == 0 else (i % 30) + 1,
        "CANCELLED"
        if i % 40 == 0
        else "cancelled"
        if i % 53 == 0
        else "C"
        if i % 97 == 0
        else "SHIPPED",
        i % 61 == 0,
        0 if i % 71 == 0 else (i % 500) + 1,
    )
    for i in range(400)
]


@pytest.fixture
def orders_warehouse(tmp_path: Path) -> Path:
    """A DuckDB warehouse whose cancellation signals contradict each other."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "shop.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE raw_orders (order_id INTEGER, customer_id INTEGER, "
        "status VARCHAR, is_cancelled BOOLEAN, total_revenue DECIMAL(10,2))"
    )
    conn.executemany("INSERT INTO raw_orders VALUES (?, ?, ?, ?, ?)", _ORDERS)
    conn.execute("CREATE TABLE customers (id INTEGER, region VARCHAR)")
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?)",
        [(i, f"r{i % 4}") for i in range(1, 31)],
    )
    conn.close()
    return path


#: The model every attribution test edits: two source predicates inside a CTE,
#: which is where the benchmark failure lived.
PRIOR_MODEL = """{{ config(materialized='table') }}
with base as (
    select * from {{ source('raw', 'raw_orders') }}
    where status != 'CANCELLED' and total_revenue > 0
)
select order_id, customer_id, total_revenue from base
"""

MODEL_PATH = "models/marts/fct_orders.sql"


@pytest.fixture
def attributable_project(
    orders_warehouse: Path, tmp_path: Path, capsys
) -> tuple[Path, Path]:
    """A dbt project over ``orders_warehouse`` with its cache already built.

    Returns ``(repo_root, warehouse)``. The cache is real, built by running
    ``explore map --full`` in process, because attribution's counting half goes
    through the query firewall and the firewall adjudicates against the cache;
    a hand-written cache would test a gate that is not the shipped one.
    """

    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: raw_orders\n      - name: customers\n",
        encoding="utf-8",
    )
    (project / MODEL_PATH.removeprefix("models/")).parent.mkdir(
        parents=True, exist_ok=True
    )
    (project / MODEL_PATH).write_text(PRIOR_MODEL, encoding="utf-8")

    rc = main(
        [
            "explore",
            "map",
            "--full",
            "--connector",
            "duckdb",
            "--path",
            str(orders_warehouse),
            "--repo-root",
            str(tmp_path),
        ]
    )
    capsys.readouterr()
    assert rc == 0, "the fixture's exploration cache must build"
    return tmp_path, orders_warehouse


@pytest.fixture
def plan_edit(tmp_path: Path):
    """Write a one-model edits payload and return its path."""

    def write(content: str, *, path: str = MODEL_PATH) -> Path:
        payload = tmp_path / "edits.json"
        payload.write_text(
            json.dumps(
                {"edits": [{"path": path, "kind": "model_sql", "content": content}]}
            ),
            encoding="utf-8",
        )
        return payload

    return write


# --- identifier conversions (#861) ---------------------------------------------
#
# Products keyed by text that mixes numeric-looking identifiers, a zero-padded
# twin (`7` and `007`), opaque identifiers of a second scheme, two spellings of
# one opaque identifier that only case separates, a hashed key on a retired
# supplier that the model's own filter already excludes, and a NULL key. Prices
# are keyed by integer, so the join between them is the one that breaks.
PRODUCTS = [
    ("7", "acme", "a@example.com", "anvil"),
    ("007", "acme", "b@example.com", "anvil, boxed"),
    ("12", "acme", "c@example.com", "rope"),
    ("13", "acme", "d@example.com", "rocket"),
    ("org_cedar", "cedar", "e@example.com", "plank"),
    ("ORG_ELM", "elm", "f@example.com", "board"),
    ("org_elm", "elm", "g@example.com", "board, oiled"),
    ("f3a9c1d2e4", "retired", "h@example.com", "old stock"),
    (None, "acme", "i@example.com", "unlabelled"),
]
PRICES = [(7, 10.0), (12, 3.5), (13, 99.0)]

PRODUCTS_PATH = "models/marts/dim_products.sql"
PRODUCTS_PRIOR = """{{ config(materialized='table') }}
select p.product_id, p.product_name, p.email, l.price
from {{ source('raw', 'raw_products') }} p
left join {{ source('raw', 'product_prices') }} l on p.product_id = l.product_id
where p.supplier <> 'retired'
"""


@pytest.fixture
def products_project(tmp_path: Path, capsys) -> tuple[Path, Path]:
    """A dbt project whose product dimension cannot join its integer-keyed
    prices, over a real DuckDB warehouse with its cache built.

    Returns ``(repo_root, warehouse)``.
    """

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "shop.duckdb"
    conn = duckdb.connect(str(warehouse))
    conn.execute(
        "CREATE TABLE raw_products (product_id VARCHAR, supplier VARCHAR, "
        "email VARCHAR, product_name VARCHAR)"
    )
    conn.executemany("INSERT INTO raw_products VALUES (?, ?, ?, ?)", PRODUCTS)
    conn.execute("CREATE TABLE product_prices (product_id INTEGER, price DOUBLE)")
    conn.executemany("INSERT INTO product_prices VALUES (?, ?)", PRICES)
    conn.close()

    project = tmp_path / "analytics"
    (project / "models" / "marts").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "shop:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "marts" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n"
        "      - name: raw_products\n      - name: product_prices\n",
        encoding="utf-8",
    )
    (project / PRODUCTS_PATH).write_text(PRODUCTS_PRIOR, encoding="utf-8")
    rc = main(
        [
            "explore",
            "map",
            "--full",
            "--connector",
            "duckdb",
            "--path",
            str(warehouse),
            "--repo-root",
            str(tmp_path),
        ]
    )
    capsys.readouterr()
    assert rc == 0, "the fixture's exploration cache must build"
    return tmp_path, warehouse
