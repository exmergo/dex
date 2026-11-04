"""Parcel pickups per depot, on irregular dates.

Each depot has at most one pickup row per day, so (depot, pickup_date) is the
grain. Days are skipped at random, so consecutive rows are often more than a day
apart, which is what makes a seven-day window and a seven-row window disagree.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table

DEPOTS = ("harbour", "airfield", "quarry")
START = date(2025, 3, 3)
DAYS = 70


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    pickups = []
    for depot in DEPOTS:
        for offset in range(DAYS):
            if rng.random() < 0.45:
                continue
            pickups.append((depot, START + timedelta(days=offset), rng.randint(1, 40)))
    return [
        Table(
            "raw.pickups",
            [("depot", "varchar"), ("pickup_date", "date"), ("parcels", "integer")],
            pickups,
        )
    ]
