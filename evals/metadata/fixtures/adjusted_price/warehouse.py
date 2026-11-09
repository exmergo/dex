"""Order lines with discounts and refunds. Written for this suite (#1116).

Independent of any benchmark: fresh names, fresh data. Each line has a list
price and a discount fraction, and some lines were later refunded. Nothing in
the project says whether a refunded line belongs in an average price, which is
what one task turns on. Product 12 always carries refunded lines.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2026, 3, 1)
    lines = []
    line_id = 1
    for product_id in range(1, 21):
        base = rng.randrange(20, 400)
        for index in range(rng.randrange(6, 30)):
            discount = rng.choice([0.0, 0.0, 0.05, 0.1, 0.15, 0.25])
            refunded = (product_id == 12 and index % 3 == 0) or rng.random() < 0.05
            lines.append(
                (
                    line_id,
                    product_id,
                    start + timedelta(days=rng.randrange(0, 60)),
                    float(base + rng.randrange(-10, 11)),
                    discount,
                    refunded,
                )
            )
            line_id += 1
    return [
        Table(
            "raw.order_lines",
            [
                ("line_id", "integer"),
                ("product_id", "integer"),
                ("ordered_on", "date"),
                ("list_price", "double"),
                ("discount_rate", "double"),
                ("is_refunded", "boolean"),
            ],
            lines,
        )
    ]
