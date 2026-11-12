"""Builders for Databricks Metric View files, written into a temporary repository."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

VIEW_DIR = "views"
FIXTURES = Path(__file__).parent / "fixtures"


def view(source: str = "demo.main.orders", **body: Any) -> dict[str, Any]:
    """A Metric View body: version 1.1 on a fully qualified DuckDB relation."""

    return {"version": 1.1, "source": source, **body}


def write(
    root: Path, name: str, body: dict[str, Any] | None = None, *, raw: str | None = None
) -> str:
    """Write one Metric View file and return its configured name."""

    if raw is None:
        raw = yaml.safe_dump(body, sort_keys=False)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return name


def write_layer(root: Path, **views: dict[str, Any]) -> list[str]:
    """One file per view, named after it."""

    return [
        write(root, f"{VIEW_DIR}/{name}.yaml", body) for name, body in views.items()
    ]


def source(
    root: Path,
    connector: str = "duckdb",
    files: list[str] | None = None,
    **views: dict[str, Any],
):
    """A Metric View source built the way the engine builds one, over ``views``."""
    from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
    from exmergo_dex_core.semantic_source import SemanticSourceContext

    names = (files or []) + write_layer(root, **views)
    return MetricViewSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root), connector=connector, options={"files": names}
        )
    )


def repo(
    root: Path,
    files: list[str] | None = None,
    config: str = "",
    **views: dict[str, Any],
) -> Path:
    """A repository configured for `vendor: databricks` on DuckDB, naming no
    warehouse path, so a command that tries to connect is refused."""

    names = (files or []) + write_layer(root, **views)
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    listed = "".join(f"\n      - {name}" for name in names)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        "semantic:\n"
        "  vendor: databricks\n"
        "  databricks:\n"
        f"    files:{listed}\n" + config,
        encoding="utf-8",
    )
    return root


#: A star on orders: a customer join with a nested nation join, `rely`, a
#: one_to_many join, every member shape the catalog distinguishes.
ORDERS = view(
    comment="One row per placed order.",
    filter="status != 'cancelled'",
    joins=[
        {
            "name": "customer",
            "source": "demo.main.customers",
            "on": "source.customer_id = customer.id",
            "rely": {"at_most_one_match": True},
            "joins": [
                {
                    "name": "nation",
                    "source": "demo.main.nations",
                    "using": ["nation_key"],
                }
            ],
        },
        {
            "name": "lines",
            "source": "demo.main.order_lines",
            "on": "source.order_id = lines.order_id",
            "cardinality": "one_to_many",
        },
    ],
    fields=[
        {"name": "order_id", "expr": "order_id"},
        {
            "name": "status",
            "expr": "source.status",
            "display_name": "Order status",
            "comment": "Where the order is in fulfilment.",
            "synonyms": ["state"],
        },
        {
            "name": "ordered_on",
            "expr": "placed_at",
            "format": {"type": "date", "date_format": "year_month_day"},
        },
        {"name": "customer_name", "expr": "customer.name"},
        {"name": "nation", "expr": "customer.nation.n_name"},
        {
            "name": "size",
            "expr": "CASE WHEN amount > 100 THEN 'big' ELSE 'small' END",
        },
    ],
    measures=[
        {
            "name": "revenue",
            "expr": "SUM(amount)",
            "display_name": "Revenue",
            "comment": "Order amounts, before refunds.",
            "format": {"type": "currency", "currency_code": "USD"},
        },
        {"name": "order_count", "expr": "COUNT(1)"},
        {"name": "aov", "expr": "MEASURE(revenue) / MEASURE(order_count)"},
        {"name": "net", "expr": "MEASURE(revenue) - SUM(refund)"},
        {"name": "paid", "expr": "SUM(amount) FILTER (WHERE paid)"},
        {"name": "units", "expr": "SUM(lines.quantity)"},
        {
            "name": "running",
            "expr": "SUM(amount)",
            "window": [{"order": "ordered_on", "range": "cumulative"}],
        },
    ],
)
