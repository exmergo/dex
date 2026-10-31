"""A web store: customers and their orders, five order statuses."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from evals.metadata.fixtures import Table

STATUSES = ("placed", "shipped", "completed", "cancelled", "returned")
WEIGHTS = (10, 25, 45, 12, 8)
REGIONS = ("north", "south", "east", "west")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    customers = [(i, f"customer {i:03d}", REGIONS[i % 4]) for i in range(1, 81)]
    start = date(2026, 1, 1)
    orders = []
    for order_id in range(1, 601):
        customer_id = rng.randrange(1, 81)
        ordered_at = start + timedelta(days=rng.randrange(0, 60))
        status = rng.choices(STATUSES, WEIGHTS)[0]
        amount = Decimal(rng.randrange(500, 25000)) / 100
        orders.append((order_id, customer_id, ordered_at, status, amount))
    return [
        Table(
            "raw.customers",
            [
                ("customer_id", "integer"),
                ("customer_name", "varchar"),
                ("region", "varchar"),
            ],
            customers,
        ),
        Table(
            "raw.orders",
            [
                ("order_id", "integer"),
                ("customer_id", "integer"),
                ("ordered_at", "date"),
                ("status", "varchar"),
                ("amount", "decimal(10,2)"),
            ],
            orders,
        ),
    ]
