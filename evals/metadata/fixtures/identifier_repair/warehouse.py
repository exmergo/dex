"""Thirty numeric products, a zero-padded twin, six opaque ids, a retired one."""

from __future__ import annotations

import random

from evals.metadata.fixtures import Table

WORDS = ("anvil", "rope", "rocket", "plank", "board", "crate", "hinge", "lever")
TREES = ("cedar", "elm", "oak", "pine", "birch", "maple")


def tables(seed: int) -> list[Table]:
    """The fixture's raw tables: products under two legitimate id schemes, a zero-padded
    id that collides with ``7`` once cast, and integer-keyed prices.
    """
    rng = random.Random(seed)
    products = [(str(i), f"{rng.choice(WORDS)} {i:02d}", "acme") for i in range(1, 31)]
    # A different product whose catalogue id happens to read as 7 once cast.
    products.append(("007", "anvil, boxed", "acme"))
    # A second identifier scheme from a supplier onboarded later: legitimate.
    products.extend((f"org_{tree}", f"{tree} plank", tree) for tree in TREES)
    # A retired product the staging model already excludes.
    products.append(("f3a9c1d2e4", "old stock", "retired"))
    prices = [(i, round(rng.uniform(1, 200), 2)) for i in range(1, 31)]
    return [
        Table(
            "raw.products",
            [
                ("product_id", "varchar"),
                ("product_name", "varchar"),
                ("supplier", "varchar"),
            ],
            products,
        ),
        Table(
            "raw.product_prices",
            [("product_id", "integer"), ("price", "double")],
            prices,
        ),
    ]
