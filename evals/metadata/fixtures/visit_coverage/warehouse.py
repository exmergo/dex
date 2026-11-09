"""Four weeks of clinic visits, six days with none. Written for this suite (#1115).

Some visits have no recorded duration, so an average over a day is NULL only
where the day had no duration at all, and a day with visits always counts them.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2026, 2, 1)
    # The first and last day always have visits, so the range is the 28 days.
    quiet = set(rng.sample(range(1, 27), 6))
    visits = []
    visit_id = 1
    for day in range(28):
        if day in quiet:
            continue
        for _ in range(rng.randrange(1, 6)):
            duration = None if rng.random() < 0.15 else rng.randrange(10, 90)
            visits.append((visit_id, start + timedelta(days=day), duration))
            visit_id += 1
    return [
        Table(
            "raw.visits",
            [
                ("visit_id", "integer"),
                ("visited_on", "date"),
                ("duration_minutes", "integer"),
            ],
            visits,
        )
    ]
