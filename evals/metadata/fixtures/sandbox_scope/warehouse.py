"""Forty accounts in four regions, eight of them sandbox tenants, with daily usage.

Written for this suite (#1115). Every sandbox account has usage, so an exclusion
that misses a model shows in its rows.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table

REGIONS = ("north", "south", "east", "west")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    sandbox = set(rng.sample(range(1, 41), 8))
    accounts = [
        (i, f"account {i:02d}", REGIONS[(i - 1) % 4], i in sandbox)
        for i in range(1, 41)
    ]
    start = date(2026, 3, 1)
    usage = []
    usage_id = 1
    for account_id in range(1, 41):
        days = set(rng.sample(range(14), rng.randrange(2, 7)))
        for day in sorted(days):
            used_on = start + timedelta(days=day)
            usage.append((usage_id, account_id, used_on, rng.randrange(1, 50)))
            usage_id += 1
    return [
        Table(
            "raw.accounts",
            [
                ("account_id", "integer"),
                ("account_name", "varchar"),
                ("region", "varchar"),
                ("is_sandbox", "boolean"),
            ],
            accounts,
        ),
        Table(
            "raw.usage",
            [
                ("usage_id", "integer"),
                ("account_id", "integer"),
                ("used_on", "date"),
                ("units", "integer"),
            ],
            usage,
        ),
    ]
