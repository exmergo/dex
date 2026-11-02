"""Thirty accounts, users for twenty-four of them, display names for some users."""

from __future__ import annotations

import random

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    rng = random.Random(seed)
    accounts = [(i, f"account {i:02d}") for i in range(1, 31)]
    with_users = sorted(rng.sample(range(1, 31), 24))
    users = []
    names = []
    user_id = 1
    for account_id in with_users:
        for _ in range(rng.randrange(1, 7)):
            # One signup code in eight was never captured, and says so in text.
            code = "n/a" if rng.random() < 0.125 else str(rng.randrange(1000, 9999))
            users.append((user_id, account_id, code, rng.random() < 0.1))
            if rng.random() < 0.6:
                names.append((user_id, f"user {user_id:03d}"))
            user_id += 1
    return [
        Table(
            "raw.accounts",
            [("account_id", "integer"), ("account_name", "varchar")],
            accounts,
        ),
        Table(
            "raw.users",
            [
                ("user_id", "integer"),
                ("account_id", "integer"),
                ("signup_code", "varchar"),
                ("is_test", "boolean"),
            ],
            users,
        ),
        Table(
            "raw.display_names",
            [("user_id", "integer"), ("display_name", "varchar")],
            names,
        ),
    ]
