"""Parcel scans over ten days, reported by scanners in batches.

A scanner reports every few minutes, so scans share a timestamp; a few scans
from a scanner with no clock carry none. The source is loaded by a feed the
project does not own.
"""

from __future__ import annotations

import datetime
import random

from evals.metadata.fixtures import Table

HUBS = ("north", "south", "east", "west")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = datetime.datetime(2026, 3, 1, 6, 0)
    scans = []
    scan_id = 1
    for day in range(10):
        for _ in range(rng.randrange(3, 6)):
            # one scanner batch: several parcels under one timestamp
            at = start + datetime.timedelta(days=day, minutes=rng.randrange(0, 600))
            hub = rng.choice(HUBS)
            for _ in range(rng.randrange(1, 4)):
                scans.append((scan_id, 5000 + rng.randrange(0, 400), hub, at))
                scan_id += 1
    # the last batch the feed delivered before the nightly run: three parcels
    last = start + datetime.timedelta(days=9, hours=16)
    for _ in range(3):
        scans.append((scan_id, 5000 + rng.randrange(0, 400), "north", last))
        scan_id += 1
    for _ in range(4):
        scans.append((scan_id, 5000 + rng.randrange(0, 400), "east", None))
        scan_id += 1
    return [
        Table(
            "raw.parcel_scans",
            [
                ("scan_id", "integer"),
                ("parcel_id", "integer"),
                ("hub", "varchar"),
                ("scanned_at", "timestamp"),
            ],
            scans,
        )
    ]
