"""Thirty listings, reviews for twenty-two of them, and five orphan reviews."""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table

CITIES = ("lisbon", "porto", "faro")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2025, 3, 1)
    listings = [(i, f"listing {i:02d}", rng.choice(CITIES)) for i in range(1, 31)]
    reviewed = sorted(rng.sample(range(1, 31), 22))
    reviews = []
    review_id = 1
    for listing_id in reviewed:
        for _ in range(rng.randrange(1, 7)):
            reviews.append(
                (
                    review_id,
                    listing_id,
                    start + timedelta(days=rng.randrange(0, 240)),
                    rng.randrange(1, 6),
                )
            )
            review_id += 1
    for orphan in range(5):
        reviews.append(
            (
                review_id,
                900 + orphan,
                start + timedelta(days=rng.randrange(0, 240)),
                rng.randrange(1, 6),
            )
        )
        review_id += 1
    return [
        Table(
            "raw.listings",
            [
                ("listing_id", "integer"),
                ("listing_name", "varchar"),
                ("city", "varchar"),
            ],
            listings,
        ),
        Table(
            "raw.reviews",
            [
                ("review_id", "integer"),
                ("listing_id", "integer"),
                ("reviewed_on", "date"),
                ("rating", "integer"),
            ],
            reviews,
        ),
    ]
