"""A league: teams, and standings published after every game week.

`wins_to_date` and `losses_to_date` are running totals within a season. The
2024 season ran all 17 weeks; the 2025 season is unfinished at week 11, so a
model that only keeps teams who reached the final scheduled week loses it.
"""

from __future__ import annotations

import random

from evals.metadata.fixtures import Table

SEASONS = {2024: 17, 2025: 11}


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    teams = [(i, f"Team {chr(64 + i)}") for i in range(1, 13)]
    standings = []
    for season, weeks in SEASONS.items():
        for team_id, _ in teams:
            wins = losses = 0
            for week in range(1, weeks + 1):
                if rng.random() < 0.5:
                    wins += 1
                else:
                    losses += 1
                standings.append((season, week, team_id, wins, losses))
    return [
        Table("raw.teams", [("team_id", "integer"), ("team_name", "varchar")], teams),
        Table(
            "raw.standings",
            [
                ("season", "integer"),
                ("week", "integer"),
                ("team_id", "integer"),
                ("wins_to_date", "integer"),
                ("losses_to_date", "integer"),
            ],
            standings,
        ),
    ]
