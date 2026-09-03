"""Fixture helpers shared by the Ossie suites.

Documents are written from a dict rather than pasted as YAML strings, so a test
that varies one thing varies one line and the rest of the document cannot drift
out from under it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

VERSION = "0.2.0.dev0"


def expression(**dialects: str) -> dict[str, Any]:
    """An Ossie expression declaring one entry per keyword argument."""

    return {
        "dialects": [
            {"dialect": name, "expression": text} for name, text in dialects.items()
        ]
    }


def field(name: str, expr: str | None = None, **extra: Any) -> dict[str, Any]:
    """A field whose expression defaults to the bare column its name spells."""

    return {"name": name, "expression": expression(ANSI_SQL=expr or name), **extra}


def dataset(name: str, source: str, *fields: dict[str, Any], **extra: Any):
    return {"name": name, "source": source, "fields": list(fields), **extra}


def document(*models: dict[str, Any], version: str = VERSION) -> dict[str, Any]:
    return {"version": version, "semantic_model": list(models)}


def model(name: str, *datasets: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": name, "datasets": list(datasets), **extra}


def write(root: Path, name: str, data: Any) -> str:
    """Write a document and return the repo-relative name it is configured as."""

    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".json"):
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return name


#: A small, complete document exercising every branch the reader has: a direct
#: field, a computed one, a quoted one, a time field by datatype and one by
#: explicit role, a multi-dialect field including a non-SQL language, a
#: query-backed dataset, a composite key, a single and a composite relationship,
#: and two metrics, one whose lineage resolves and one whose does not.
def reference_document() -> dict[str, Any]:
    return document(
        model(
            "commerce",
            dataset(
                "orders",
                "demo.main.orders",
                field("order_id"),
                field("customer_id"),
                field("net_total", "order_total - COALESCE(discount, 0)"),
                field("region", '"Region"'),
                {
                    "name": "placed_on",
                    "datatype": "Date",
                    "expression": expression(
                        ANSI_SQL="placed_at", MDX="[Order].[Placed At]"
                    ),
                },
                {
                    "name": "status_changed",
                    "datatype": "String",
                    "dimension": {"is_time": True},
                    "expression": expression(ANSI_SQL="status_changed_at"),
                },
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
                primary_key=["order_id", "line_no"],
            ),
            dataset(
                "recent_orders",
                "SELECT * FROM demo.main.orders WHERE placed_at > '2026-01-01'",
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
                    "name": "items_to_orders",
                    "from": "order_items",
                    "to": "order_items",
                    "from_columns": ["order_id", "line_no"],
                    "to_columns": ["order_id", "line_no"],
                },
            ],
            metrics=[
                {
                    "name": "revenue",
                    "datatype": "Decimal",
                    "description": "Sum of net order totals.",
                    "expression": expression(
                        ANSI_SQL="SUM(orders.net_total)",
                        SNOWFLAKE="SUM(orders.net_total)::NUMBER",
                    ),
                },
                {
                    "name": "order_count",
                    "datatype": "Integer",
                    "expression": expression(ANSI_SQL="COUNT(*)"),
                },
            ],
        )
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository root with the reference document configured in it."""

    write(tmp_path, "commerce.ossie.yaml", reference_document())
    return tmp_path
