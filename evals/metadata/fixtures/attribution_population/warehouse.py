"""Thirty accounts, twenty of them with events. Written for this suite (#1026)."""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2026, 1, 1)
    accounts = [(i, f"account {i:02d}") for i in range(1, 31)]
    with_events = sorted(rng.sample(range(1, 31), 20))
    events = []
    event_id = 1
    for account_id in with_events:
        for _ in range(rng.randrange(1, 8)):
            events.append(
                (event_id, account_id, start + timedelta(days=rng.randrange(0, 30)))
            )
            event_id += 1
    return [
        Table(
            "raw.accounts",
            [("account_id", "integer"), ("account_name", "varchar")],
            accounts,
        ),
        Table(
            "raw.events",
            [("event_id", "integer"), ("account_id", "integer"), ("event_on", "date")],
            events,
        ),
    ]
