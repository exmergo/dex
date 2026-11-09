"""Sales of fifteen products across four regions."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from evals.metadata.fixtures import Table

REGIONS = ("alpine", "coastal", "desert", "lakes")
CATEGORIES = ("tools", "garden", "kitchen")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    products = [(i, f"product {i:02d}", CATEGORIES[i % 3]) for i in range(1, 16)]
    start = date(2026, 2, 1)
    sales = [
        (
            sale_id,
            start + timedelta(days=rng.randrange(0, 28)),
            rng.choice(REGIONS),
            rng.randrange(1, 16),
            Decimal(rng.randrange(100, 9000)) / 100,
        )
        for sale_id in range(1, 1501)
    ]
    return [
        Table(
            "raw.products",
            [
                ("product_id", "integer"),
                ("product_name", "varchar"),
                ("category", "varchar"),
            ],
            products,
        ),
        Table(
            "raw.sales",
            [
                ("sale_id", "integer"),
                ("sold_on", "date"),
                ("region", "varchar"),
                ("product_id", "integer"),
                ("amount", "decimal(10,2)"),
            ],
            sales,
        ),
    ]
