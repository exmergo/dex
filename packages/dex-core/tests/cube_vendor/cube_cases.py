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
# Slashes on every platform, as semantic.cube.files requires of a config.
MODEL_FILES = sorted(
    path.relative_to(MODEL).as_posix() for path in MODEL.rglob("*.yml")
)

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
    # A relative date range on the grouped time dimension is moved to its
    # `dateRange` and resolved to timestamps by Cube at render time, using the
    # Cube server's clock (#972).
    "relative_range": {
        "metrics": ["orders.revenue"],
        "group_by": ["orders.ordered_at__day"],
        "where": [
            '{"member": "orders.ordered_at", "operator": "inDateRange",'
            ' "values": ["last 7 days"]}'
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


#: The fixture rows, shared with the executing Cube Core and Cube Cloud (#914).
SHOP_SQL = ROOT / "scripts" / "cube" / "shop.sql"


def write_shop(path: Path) -> None:
    """The DuckDB warehouse the unit and live DuckDB tests run on: the seeded
    Postgres `app` tables' shape, with a handful of fixed rows.

    Built from ``scripts/cube/shop.sql``, the same statements a Cube server runs
    for its own DuckDB, so a number Cube executed and one dex executed come from
    one table."""

    import duckdb

    conn = duckdb.connect(str(path))
    try:
        conn.execute(SHOP_SQL.read_text(encoding="utf-8"))
    finally:
        conn.close()
