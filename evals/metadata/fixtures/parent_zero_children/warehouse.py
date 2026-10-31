"""Forty accounts, tickets for twenty-five of them, and six orphan tickets."""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table

STATUSES = ("open", "pending", "solved")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2025, 6, 1)
    accounts = [
        (i, f"account {i:02d}", start + timedelta(days=rng.randrange(0, 120)))
        for i in range(1, 41)
    ]
    with_tickets = sorted(rng.sample(range(1, 41), 25))
    tickets = []
    ticket_id = 1
    for account_id in with_tickets:
        for _ in range(rng.randrange(1, 6)):
            opened = start + timedelta(days=rng.randrange(120, 300))
            tickets.append((ticket_id, account_id, opened, rng.choice(STATUSES)))
            ticket_id += 1
    for orphan in range(6):
        opened = start + timedelta(days=rng.randrange(120, 300))
        tickets.append((ticket_id, 900 + orphan, opened, "open"))
        ticket_id += 1
    return [
        Table(
            "raw.accounts",
            [
                ("account_id", "integer"),
                ("account_name", "varchar"),
                ("created_on", "date"),
            ],
            accounts,
        ),
        Table(
            "raw.tickets",
            [
                ("ticket_id", "integer"),
                ("account_id", "integer"),
                ("opened_on", "date"),
                ("status", "varchar"),
            ],
            tickets,
        ),
    ]
