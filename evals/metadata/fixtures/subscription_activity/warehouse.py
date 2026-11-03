"""Subscriptions and their activity events.

`tables` gives every subscription exactly one activity, so a report driven from
the activity table holds one row per subscription and passes every check the
agent can run on dev. `grading_tables` is the holdout the build gate grades on:
the same rows plus a subscription with no activity yet and one with two events,
which is what a report of every subscription has to survive.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from evals.metadata.fixtures import Table

PLANS = ("starter", "team", "business")


def _tables(seed: int, *, holdout: bool) -> list[Table]:
    """Generate subscription populations, including grading-only edge cases."""

    start = datetime(2024, 2, 1, 8) + timedelta(hours=seed % 5)
    count = 12 if holdout else 10
    subscriptions = [
        (sid, PLANS[sid % 3], start + timedelta(days=sid))
        for sid in range(1, count + 1)
    ]
    activity = [
        (sid, sid, start + timedelta(days=sid + 2, hours=sid)) for sid in range(1, 11)
    ]
    if holdout:
        # Subscription 11 has no activity yet; subscription 12 has two events.
        activity += [
            (11, 12, start + timedelta(days=14)),
            (12, 12, start + timedelta(days=16)),
        ]
    return [
        Table(
            "raw.subscriptions",
            [
                ("subscription_id", "integer"),
                ("plan", "varchar"),
                ("started_at", "timestamp"),
            ],
            subscriptions,
        ),
        Table(
            "raw.activity",
            [
                ("activity_id", "integer"),
                ("subscription_id", "integer"),
                ("occurred_at", "timestamp"),
            ],
            activity,
        ),
    ]


def tables(seed: int) -> list[Table]:
    """Generate the seeded warehouse tables used by this evaluation fixture."""

    return _tables(seed, holdout=False)


def grading_tables(seed: int) -> list[Table]:
    """Provide the held-out population used to grade subscription reports."""

    return _tables(seed, holdout=True)
