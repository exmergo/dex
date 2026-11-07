"""Orders with a platform fee and the finance ledger's postings.

Merchants sell through the marketplace and pay a platform fee per order. The
finance team posts sales and fees to a ledger from its own feed, which the
order models never read.
"""

from __future__ import annotations

import datetime
import random
from decimal import Decimal

from evals.metadata.fixtures import Table

MERCHANTS = (101, 102, 103, 104)
ACCOUNTS = ("sales", "fees")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = datetime.date(2026, 5, 1)
    orders = []
    for order_id in range(1, 41):
        gross = Decimal(rng.randrange(1500, 25000)) / 100
        fee = (gross * Decimal("0.08")).quantize(Decimal("0.01"))
        orders.append(
            (
                order_id,
                rng.choice(MERCHANTS),
                start + datetime.timedelta(days=rng.randrange(0, 14)),
                gross,
                fee,
            )
        )
    postings = []
    posting_id = 1
    for day in range(5):
        for account in ACCOUNTS:
            for _ in range(rng.randrange(2, 4)):
                amount = Decimal(rng.randrange(500, 9000)) / 100
                fee = (
                    (amount * Decimal("0.08")).quantize(Decimal("0.01"))
                    if account == "fees"
                    else Decimal("0.00")
                )
                postings.append(
                    (
                        posting_id,
                        account,
                        start + datetime.timedelta(days=day),
                        amount,
                        fee,
                    )
                )
                posting_id += 1
    return [
        Table(
            "raw.orders",
            [
                ("order_id", "integer"),
                ("merchant_id", "integer"),
                ("ordered_on", "date"),
                ("gross_amount", "decimal(12,2)"),
                ("platform_fee", "decimal(12,2)"),
            ],
            orders,
        ),
        Table(
            "raw.ledger_postings",
            [
                ("posting_id", "integer"),
                ("account", "varchar"),
                ("posted_on", "date"),
                ("amount", "decimal(12,2)"),
                ("fee_amount", "decimal(12,2)"),
            ],
            postings,
        ),
    ]
