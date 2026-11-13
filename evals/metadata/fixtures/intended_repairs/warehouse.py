"""Support tickets, workspace memberships and shipments. Written for this suite (#1157).

Each table carries the trap its repair's over-broad variant falls into:

- tickets arrive on two channels, and five of the 28 days have none, so a
  backfill that crosses every day with every channel repeats the day;
- four memberships name a workspace the workspace table does not list, so an
  exclusion written as an inner join drops them along with the archived ones;
- some shipments were loaded more than a day after they shipped, and the load
  date differs from the ship date on most rows, so a repair that re-dates every
  shipment by its load changes rows the request never named.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    """Calendar, tickets, workspaces, memberships and shipments for ``seed``."""

    rng = random.Random(seed)
    start = date(2026, 4, 1)
    days = [start + timedelta(days=i) for i in range(28)]
    # The first and last day always have tickets, so the range is the 28 days.
    quiet = set(rng.sample(range(1, 27), 5))
    tickets = []
    for i, day in enumerate(days):
        if i in quiet:
            continue
        for _ in range(rng.randrange(1, 5)):
            tickets.append((len(tickets) + 1, day, rng.choice(["email", "chat"])))

    workspaces = [(w, w % 4 == 0) for w in range(1, 13)]
    memberships = []
    for w in [*range(1, 13), 40, 41, 42, 43]:
        count = 1 if w >= 40 else rng.randrange(2, 5)
        for _ in range(count):
            memberships.append((len(memberships) + 1, w, rng.randrange(100, 400)))

    shipments = []
    for i in range(1, 41):
        shipped = start + timedelta(days=rng.randrange(0, 28))
        lag = rng.choice([0, 0, 1, 1, 1, 3, 5]) if i % 6 else 4
        loaded = datetime.combine(
            shipped + timedelta(days=lag), time(rng.randrange(0, 24))
        )
        shipments.append((i, shipped, loaded, rng.choice(["north", "south"])))

    return [
        Table("raw.calendar", [("day", "date")], [(d,) for d in days]),
        Table(
            "raw.tickets",
            [("ticket_id", "integer"), ("opened_on", "date"), ("channel", "varchar")],
            tickets,
        ),
        Table(
            "raw.workspaces",
            [("workspace_id", "integer"), ("is_archived", "boolean")],
            workspaces,
        ),
        Table(
            "raw.memberships",
            [
                ("membership_id", "integer"),
                ("workspace_id", "integer"),
                ("user_id", "integer"),
            ],
            memberships,
        ),
        Table(
            "raw.shipments",
            [
                ("shipment_id", "integer"),
                ("shipped_on", "date"),
                ("loaded_at", "timestamp"),
                ("region", "varchar"),
            ],
            shipments,
        ),
    ]
