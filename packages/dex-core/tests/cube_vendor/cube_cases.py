"""The queries the Cube render tests replay, and the model they are rendered over.

One model, ``scripts/cube/model``, is what the recordings were made against, what
the unit tests copy into their repository, and what ``scripts/setup_cube_dev.sh``
serves to the integration tests. Each case is the request dex receives; the
Cube query JSON it becomes is computed by dex's own translation, so a recording
is keyed by exactly what dex sends.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MODEL = ROOT / "scripts" / "cube" / "model"
RECORDED = Path(__file__).resolve().parent / "recorded"
MODEL_FILES = sorted(str(path.relative_to(MODEL)) for path in MODEL.rglob("*.yml"))

#: The connectors Cube renders for in this release, each recorded.
CONNECTORS = ("duckdb", "postgres", "bigquery", "snowflake", "redshift")

#: ``explore semantic query`` requests, by the flags they pass.
QUERIES: dict[str, dict] = {
    "by_status": {
        "metrics": ["orders.revenue", "orders.count"],
        "group_by": ["orders.status"],
        "order_by": ["-orders.revenue"],
    },
    "by_month": {
        "metrics": ["orders.revenue"],
        "group_by": ["orders.ordered_at__month"],
        "order_by": ["orders.ordered_at__month"],
    },
    "by_grain_flag": {
        "metrics": ["orders.count"],
        "group_by": ["orders.ordered_at"],
        "grain": "quarter",
    },
    "fiscal_year": {
        "metrics": ["orders.revenue"],
        "group_by": ["orders.ordered_at__fiscal_year"],
    },
    "filtered": {
        "metrics": ["orders.count", "orders.paid_revenue"],
        "group_by": ["orders.status"],
        "where": [
            '{"or": ['
            '{"member": "orders.status", "operator": "equals", "values": ["paid"]}, '
            '{"member": "orders.status", "operator": "equals", "values": ["shipped"]}'
            "]}"
        ],
    },
    "having": {
        "metrics": ["orders.count"],
        "group_by": ["orders.status"],
        "where": ['{"member": "orders.count", "operator": "gt", "values": ["2"]}'],
    },
    "two_hops": {
        "metrics": ["order_items.units", "order_items.line_revenue"],
        "group_by": ["products.category", "orders.status"],
        "order_by": ["products.category", "orders.status"],
    },
    "view": {
        "metrics": ["order_facts.revenue"],
        "group_by": ["order_facts.status"],
    },
    "average": {
        "metrics": ["orders.average_order"],
        "group_by": ["orders.status"],
        "limit": 3,
    },
    # A date range on the grouped time dimension is its `dateRange`, which is
    # what bounds a rolling window's series.
    "rolling": {
        "metrics": ["orders.revenue_7d"],
        "group_by": ["orders.ordered_at__day"],
        "where": [
            '{"member": "orders.ordered_at", "operator": "inDateRange",'
            ' "values": ["2026-01-01", "2026-01-10"]}'
        ],
    },
    # The golden: every clause dex sends, rendered for each connector.
    "golden": {
        "metrics": ["orders.revenue", "orders.count"],
        "group_by": ["orders.status", "orders.ordered_at__month"],
        "where": [
            '[{"member": "orders.status", "operator": "equals",'
            ' "values": ["paid", "shipped"]},'
            ' {"member": "orders.ordered_at", "operator": "inDateRange",'
            ' "values": ["2026-01-01", "2026-03-31"]},'
            ' {"member": "orders.revenue", "operator": "gt", "values": ["1"]}]'
        ],
        "order_by": ["-orders.revenue"],
        "limit": 10,
    },
}

#: ``explore semantic values`` requests: (dimension, metrics).
VALUES: dict[str, tuple[str, list[str]]] = {
    "values_status": ("orders.status", []),
    "values_scoped": ("products.category", ["order_items.units"]),
    "values_month": ("orders.ordered_at__month", []),
}


def write_shop(path: Path) -> None:
    """The DuckDB warehouse the unit and live DuckDB tests run on: the seeded
    Postgres `app` tables' shape, with a handful of fixed rows."""

    import duckdb

    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA app")
    conn.execute(
        "CREATE TABLE app.customers (id BIGINT, email VARCHAR, city VARCHAR,"
        " created_at TIMESTAMPTZ)"
    )
    conn.execute(
        "INSERT INTO app.customers VALUES"
        " (1, 'a@example.com', 'Lisbon', '2025-01-01'),"
        " (2, 'b@example.com', 'Porto', '2025-02-01'),"
        " (3, 'c@example.com', 'Milan', '2025-03-01')"
    )
    conn.execute(
        "CREATE TABLE app.orders (id BIGINT, customer_id BIGINT, status VARCHAR,"
        " total DECIMAL(12, 2), ordered_at TIMESTAMPTZ)"
    )
    rows = [
        (1, 1, "paid", 10.00, "2026-01-03 10:00:00+00"),
        (2, 1, "paid", 5.50, "2026-01-20 08:30:00+00"),
        (3, 2, "shipped", 7.00, "2026-02-01 12:00:00+00"),
        (4, 2, "paid", 2.50, "2026-02-14 09:15:00+00"),
        (5, 3, "pending", 4.00, "2026-03-09 17:45:00+00"),
        (6, 3, "cancelled", 1000.00, "2025-03-10 11:00:00+00"),
        (7, 1, "shipped", 12.25, "2026-03-30 23:30:00+00"),
        (8, 2, "paid", 3.75, "2025-12-31 23:59:00+00"),
    ]
    conn.executemany("INSERT INTO app.orders VALUES (?, ?, ?, ?, ?)", rows)
    conn.execute(
        "CREATE TABLE app.products (id BIGINT, name VARCHAR, category VARCHAR,"
        " price DECIMAL(10, 2))"
    )
    conn.execute(
        "INSERT INTO app.products VALUES (1, 'p1', 'grocery', 1), (2, 'p2', 'home', 2),"
        " (3, 'p3', 'grocery', 3)"
    )
    conn.execute(
        "CREATE TABLE app.order_items (id BIGINT, order_id BIGINT, product_id BIGINT,"
        " quantity INTEGER, unit_price DECIMAL(10, 2))"
    )
    conn.execute(
        "INSERT INTO app.order_items VALUES (1, 1, 1, 2, 1.50), (2, 1, 2, 1, 4.00),"
        " (3, 3, 3, 5, 0.50), (4, 4, 2, 1, 2.50), (5, 7, 1, 3, 1.25),"
        " (6, 5, 3, 1, 4.00)"
    )
    conn.close()
