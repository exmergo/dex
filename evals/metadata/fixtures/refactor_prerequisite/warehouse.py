"""Twelve teams, members for nine of them, chat handles for some members.

`joined_at` arrives empty on every row and typed integer, as an all-NULL column
often is when a loader infers types, so the tiny_roster package's
`coalesce(joined_at, joined_on)` cannot run. Three teams have no members, so a
refactor that drives from members loses them. Three handles name a member the
roster does not have, so a refactor that counts handles rather than members with
a handle counts them.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    """Generate the seeded warehouse tables used by this evaluation fixture."""

    rng = random.Random(seed)
    teams = [(i, f"team {i:02d}") for i in range(1, 13)]
    staffed = sorted(rng.sample(range(1, 13), 9))
    start = datetime(2023, 3, 1, 9)
    members = []
    handles = []
    member_id = 1
    for team_id in staffed:
        for _ in range(rng.randrange(1, 6)):
            # One member in six has no recorded join date at all.
            joined_on = (
                None
                if rng.random() < 1 / 6
                else start + timedelta(days=rng.randrange(0, 400))
            )
            members.append(
                (member_id, team_id, f"member {member_id:03d}", joined_on, None)
            )
            if rng.random() < 0.6:
                handles.append((member_id, f"@m{member_id:03d}"))
            member_id += 1
    # Handles left behind by members the roster no longer has.
    handles.extend(
        (orphan, f"@gone{orphan:03d}")
        for orphan in range(member_id + 10, member_id + 13)
    )
    return [
        Table(
            "raw.teams",
            [("team_id", "integer"), ("team_name", "varchar")],
            teams,
        ),
        Table(
            "raw.members",
            [
                ("member_id", "integer"),
                ("team_id", "integer"),
                ("member_name", "varchar"),
                ("joined_on", "timestamp"),
                ("joined_at", "integer"),
            ],
            members,
        ),
        Table(
            "raw.handles",
            [("member_id", "integer"), ("handle", "varchar")],
            handles,
        ),
    ]
