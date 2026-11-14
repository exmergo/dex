"""Builders for Snowflake Semantic View files, written into a temporary repository."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

VIEW_DIR = "views"
FIXTURES = Path(__file__).parent / "fixtures"


def table(
    name: str,
    relation: str,
    key: list[str] | None = None,
    **body: Any,
) -> dict[str, Any]:
    """A logical table on a fully qualified relation, keyed on ``key``."""

    database, schema, physical = relation.split(".")
    found: dict[str, Any] = {
        "name": name,
        "base_table": {"database": database, "schema": schema, "table": physical},
    }
    if key:
        found["primary_key"] = {"columns": key}
    return {**found, **body}


def relationship(
    name: str, left: str, right: str, *pairs: tuple[str, str]
) -> dict[str, Any]:
    """A key-equality relationship from ``left`` to ``right``."""
    return {
        "name": name,
        "left_table": left,
        "right_table": right,
        "relationship_columns": [
            {"left_column": a, "right_column": b} for a, b in pairs
        ],
    }


def sv(name: str, *tables: dict[str, Any], **body: Any) -> dict[str, Any]:
    """A Semantic View document."""
    return {"name": name, "tables": list(tables), **body}


def write(
    root: Path, name: str, body: dict[str, Any] | None = None, *, raw: str | None = None
) -> str:
    """Write one Semantic View file and return its configured name."""

    if raw is None:
        raw = yaml.safe_dump(body, sort_keys=False)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return name


def write_layer(root: Path, *views: dict[str, Any]) -> list[str]:
    """One file per view, named after it."""
    return [write(root, f"{VIEW_DIR}/{v['name']}.yaml", v) for v in views]


def source(
    root: Path,
    *views: dict[str, Any],
    connector: str = "duckdb",
    files: list[str] | None = None,
):
    """A Semantic View source built the way the engine builds one."""
    from exmergo_dex_core.semantic_source import SemanticSourceContext
    from exmergo_dex_core.semantic_views.project import SemanticViewSemanticLayer

    names = (files or []) + write_layer(root, *views)
    return SemanticViewSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root), connector=connector, options={"files": names}
        )
    )


def repo(
    root: Path,
    *views: dict[str, Any],
    files: list[str] | None = None,
    config: str = "",
    connector: str = "duckdb",
) -> Path:
    """A repository configured for `vendor: snowflake`, naming no warehouse path,
    so a command that tries to connect is refused."""

    names = (files or []) + write_layer(root, *views)
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    listed = "".join(f"\n      - {name}" for name in names)
    (root / ".dex" / "config.yml").write_text(
        f"connector: {connector}\n"
        "semantic:\n"
        "  vendor: snowflake\n"
        "  snowflake:\n"
        f"    files:{listed}\n" + config,
        encoding="utf-8",
    )
    return root


#: Orders, their customers and line items on a DuckDB warehouse named `demo`:
#: every member shape the catalog distinguishes.
SHOP = sv(
    "shop",
    table(
        "orders",
        "demo.main.orders",
        ["order_id"],
        description="One row per placed order.",
        synonyms=["purchases"],
        dimensions=[
            {
                "name": "status",
                "expr": "status",
                "description": "Where the order is in fulfilment.",
                "synonyms": ["state"],
                "sample_values": ["paid", "open"],
            },
            {"name": "status_label", "expr": "orders.status"},
            {
                "name": "size",
                "expr": "CASE WHEN amount > 100 THEN 'big' ELSE 'small' END",
            },
        ],
        time_dimensions=[
            {"name": "ordered_on", "expr": "placed_at", "data_type": "DATE"}
        ],
        facts=[
            {"name": "amount", "expr": "amount"},
            {
                "name": "discount",
                "expr": "discount",
                "access_modifier": "private_access",
            },
        ],
        metrics=[
            {
                "name": "revenue",
                "expr": "SUM(orders.amount)",
                "description": "Order amounts, before refunds.",
                "synonyms": ["sales"],
            },
            {"name": "order_count", "expr": "COUNT(order_id)"},
            {"name": "aov", "expr": "orders.revenue / orders.order_count"},
            {
                "name": "net",
                "expr": "SUM(orders.amount - orders.discount)",
                "access_modifier": "private_access",
            },
        ],
    ),
    table(
        "customers",
        "demo.main.customers",
        ["customer_id"],
        unique_keys=[{"columns": ["email"]}],
        dimensions=[
            {"name": "region", "expr": "region"},
            {"name": "email", "expr": "email"},
        ],
        metrics=[{"name": "customer_count", "expr": "COUNT(customer_id)"}],
    ),
    table(
        "lines",
        "demo.main.order_lines",
        ["order_id", "line_no"],
        facts=[{"name": "quantity", "expr": "quantity"}],
        metrics=[{"name": "units", "expr": "SUM(lines.quantity)"}],
    ),
    relationships=[
        relationship(
            "orders_customers", "orders", "customers", ("customer_id", "customer_id")
        ),
        relationship("lines_orders", "lines", "orders", ("order_id", "order_id")),
    ],
    metrics=[
        {
            "name": "orders_per_customer",
            "expr": "orders.order_count / customers.customer_count",
        },
        {"name": "revenue_x2", "expr": "orders.revenue * 2"},
    ],
)
