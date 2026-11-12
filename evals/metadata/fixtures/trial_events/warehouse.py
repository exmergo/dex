"""Subscriptions and billing events. Written for this suite (#1048).

Independent of any benchmark: fresh names, fresh data. The billing feed sends
each event's tier as a code. ``trial`` and ``trl`` both mean a trial event (the
project's tier-code seed says so), ``paid`` is a paid event, and ``pro-trial`` is
a code nothing in the project places. A few codes arrive with stray case or
whitespace. Four subscriptions carry what the tasks ask about:

- 1007 has trial events sent as ``trl``;
- 1013 is an internal subscription, which the mart's population excludes;
- 1021 is active, so it has no cancellation date;
- 1034 has ``pro-trial`` events beside its paid ones.

Subscription 1099, which one task asks about, does not exist in the feed.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from evals.metadata.fixtures import Table

_PLANS = ("starter", "team", "business")


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    start = date(2026, 1, 1)
    subscriptions = []
    events = []
    event_id = 1
    for subscription_id in range(1001, 1061):
        started = start + timedelta(days=rng.randrange(0, 60))
        cancelled = (
            None
            if subscription_id in {1007, 1021, 1034} or rng.random() < 0.6
            else started + timedelta(days=rng.randrange(20, 120))
        )
        internal = subscription_id in {1013, 1042, 1055}
        subscriptions.append(
            (subscription_id, rng.choice(_PLANS), started, cancelled, internal)
        )
        codes = [
            rng.choices(
                ["trial", "trl", "paid", "Paid ", " TRIAL"],
                weights=[30, 8, 50, 6, 6],
            )[0]
            for _ in range(rng.randrange(2, 14))
        ]
        if subscription_id == 1007:
            codes += ["trl", "trl", "trl", "trial", "trial", "paid"]
        if subscription_id == 1034:
            codes += ["pro-trial", "pro-trial", "pro-trial", "paid", "paid"]
        elif rng.random() < 0.1:
            codes.append("pro-trial")
        for code in codes:
            events.append(
                (
                    event_id,
                    subscription_id,
                    code,
                    started + timedelta(days=rng.randrange(0, 90)),
                )
            )
            event_id += 1
    return [
        Table(
            "raw.subscriptions",
            [
                ("subscription_id", "integer"),
                ("plan_name", "varchar"),
                ("started_on", "date"),
                ("cancelled_on", "date"),
                ("is_internal", "boolean"),
            ],
            subscriptions,
        ),
        Table(
            "raw.billing_events",
            [
                ("event_id", "integer"),
                ("subscription_id", "integer"),
                ("tier_code", "varchar"),
                ("occurred_on", "date"),
            ],
            events,
        ),
    ]
