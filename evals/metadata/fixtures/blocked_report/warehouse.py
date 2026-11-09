"""Twelve teams, their members, and forty event sources."""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table

SOURCES = 40


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2026, 1, 1)
    teams = [(i, f"team {i:02d}") for i in range(1, 13)]
    members = []
    member_id = 1
    for team_id in range(1, 11):
        for _ in range(rng.randrange(2, 8)):
            joined = start + timedelta(days=rng.randrange(0, 200))
            # Some joins were recorded before the date was captured.
            joined_on = "unknown" if rng.random() < 0.15 else joined.isoformat()
            left = rng.random() < 0.25
            left_on = (
                (joined + timedelta(days=rng.randrange(10, 90))).isoformat()
                if left
                else None
            )
            members.append((member_id, team_id, joined_on, left_on))
            member_id += 1
    events = [
        (
            i,
            rng.randrange(SOURCES),
            rng.randrange(1, 13),
            start + timedelta(days=rng.randrange(0, 300)),
        )
        for i in range(1, 801)
    ]
    return [
        Table("raw.teams", [("team_id", "integer"), ("team_name", "varchar")], teams),
        Table(
            "raw.members",
            [
                ("member_id", "integer"),
                ("team_id", "integer"),
                ("joined_on", "varchar"),
                ("left_on", "varchar"),
            ],
            members,
        ),
        Table(
            "raw.events",
            [
                ("event_id", "integer"),
                ("source_id", "integer"),
                ("team_id", "integer"),
                ("occurred_on", "date"),
            ],
            events,
        ),
    ]
