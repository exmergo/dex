"""Builders for Cube model files, written into a temporary repository."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MODEL_DIR = "model"


def cube(name: str, *, sql_table: str | None = None, **fields: Any) -> dict[str, Any]:
    """A cube on a fully qualified DuckDB relation by default."""

    body: dict[str, Any] = {"name": name}
    if "sql" not in fields:
        body["sql_table"] = sql_table or f"demo.main.{name}"
    body.update(fields)
    return body


def view(name: str, *sources: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"name": name, "cubes": list(sources), **fields}


def write(
    root: Path,
    name: str,
    *cubes: dict[str, Any],
    views: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    raw: str | None = None,
) -> list[str]:
    """Write one model file and return the configured names it adds."""

    if raw is None:
        body: dict[str, Any] = {}
        if cubes:
            body["cubes"] = list(cubes)
        if views:
            body["views"] = list(views)
        raw = yaml.safe_dump(body, sort_keys=False)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return [name]


def write_layer(
    root: Path,
    *cubes: dict[str, Any],
    views: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> list[str]:
    """One file per cube, and the views in a file of their own."""

    names: list[str] = []
    for entry in cubes:
        names += write(root, f"{MODEL_DIR}/{entry['name']}.yml", entry)
    if views:
        names += write(root, f"{MODEL_DIR}/views.yml", views=views)
    return names


def source(root: Path, *cubes: dict[str, Any], views=(), connector: str = "duckdb"):
    from exmergo_dex_core.cube.project import CubeSemanticLayer
    from exmergo_dex_core.semantic_source import SemanticSourceContext

    return CubeSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root),
            connector=connector,
            options={"files": write_layer(root, *cubes, views=views)},
        )
    )


def repo(
    root: Path,
    *cubes: dict[str, Any],
    views=(),
    files: list[str] | None = None,
    config: str = "",
) -> Path:
    """A repository configured for `semantic.vendor: cube`, naming no warehouse
    path, so any command that tried to connect would refuse."""

    names = write_layer(root, *cubes, views=views) + list(files or [])
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        + config
        + "semantic:\n  vendor: cube\n  cube:\n    files:\n"
        + "".join(f"      - {name}\n" for name in names),
        encoding="utf-8",
    )
    return root


# A layer that answers every field Cube can answer: two cubes joined, a title
# and a description wherever Cube has a place for one, one time dimension with
# a custom granularity, a computed dimension, a filtered measure, a ratio, and
# a `sql` cube that is a plain projection of one table.
ORDERS = cube(
    "orders",
    title="Orders",
    description="One row per order.",
    joins=[
        {
            "name": "customers",
            "sql": "{CUBE}.customer_id = {customers}.id",
            "relationship": "belongs_to",
        }
    ],
    dimensions=[
        {"name": "order_id", "sql": "order_id", "type": "number", "primary_key": True},
        {"name": "customer_id", "sql": "{CUBE}.customer_id", "type": "number"},
        {"name": "status", "sql": "{CUBE}.status", "type": "string", "title": "Status"},
        {
            "name": "ordered_at",
            "sql": "{CUBE}.ordered_at",
            "type": "time",
            "description": "Placed at.",
            "granularities": [
                {"name": "fiscal_year", "interval": "1 year", "offset": "3 months"}
            ],
        },
        {"name": "amount_cents", "sql": "{CUBE}.amount * 100", "type": "number"},
    ],
    measures=[
        {
            "name": "revenue",
            "sql": "{CUBE}.amount",
            "type": "sum",
            "title": "Revenue",
            "description": "Gross revenue.",
            "filters": [{"sql": "{CUBE}.status != 'test'"}],
        },
        {"name": "order_count", "type": "count", "title": "Orders"},
        {"name": "aov", "sql": "{revenue} / {order_count}", "type": "number"},
    ],
)
CUSTOMERS = cube(
    "customers",
    sql="SELECT customer_key AS id, region, signed_up_at FROM demo.main.customers",
    title="Customers",
    dimensions=[
        {"name": "id", "sql": "id", "type": "number", "primary_key": True},
        {"name": "region", "sql": "{CUBE}.region", "type": "string"},
        {"name": "signed_up_at", "sql": "signed_up_at", "type": "time"},
    ],
    measures=[{"name": "customer_count", "type": "count_distinct", "sql": "id"}],
)
ORDERS_LAYER = (ORDERS, CUSTOMERS)
