"""Five stores, twenty SKUs, a stock snapshot per store, SKU and day."""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    stores = [(i, f"store {i}") for i in range(1, 6)]
    start = date(2026, 3, 1)
    levels = []
    for day in range(30):
        for store_id, _ in stores:
            for sku in range(1, 21):
                units = 0 if rng.random() < 0.2 else rng.randrange(1, 60)
                levels.append(
                    (start + timedelta(days=day), store_id, f"SKU-{sku:03d}", units)
                )
    return [
        Table(
            "raw.stores", [("store_id", "integer"), ("store_name", "varchar")], stores
        ),
        Table(
            "raw.stock_levels",
            [
                ("snapshot_date", "date"),
                ("store_id", "integer"),
                ("sku", "varchar"),
                ("units_on_hand", "integer"),
            ],
            levels,
        ),
    ]
