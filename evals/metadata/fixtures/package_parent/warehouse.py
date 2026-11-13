"""raw.conversations and raw.conversation_parts as an inbox vendor delivers them.

Ten conversations. Parts exist for seven of them, plus parts of two
conversations the vendor has since deleted (ids 901 and 902), which still sit in
the parts table. A conversation-level mart driven from the parts therefore holds
nine conversations, two of which do not exist, and misses three that do: the
shape of the ADE-bench run behind #1155.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from evals.metadata.fixtures import Table

STATES = ("open", "closed", "snoozed")


def tables(seed: int) -> list[Table]:
    """Generate the seeded warehouse tables used by this evaluation fixture."""

    start = datetime(2024, 5, 1, 9) + timedelta(hours=seed % 6)
    conversations = [
        (cid, start + timedelta(days=cid), STATES[cid % 3]) for cid in range(1, 11)
    ]
    owners = [1, 1, 1, 2, 3, 3, 4, 5, 6, 6, 7, 901, 901, 902]
    parts = [
        (pid, owner, start + timedelta(days=owner % 50, hours=pid))
        for pid, owner in enumerate(owners, start=1)
    ]
    return [
        Table(
            "raw.conversations",
            [
                ("conversation_id", "integer"),
                ("created_at", "timestamp"),
                ("state", "varchar"),
            ],
            conversations,
        ),
        Table(
            "raw.conversation_parts",
            [
                ("part_id", "integer"),
                ("conversation_id", "integer"),
                ("created_at", "timestamp"),
            ],
            parts,
        ),
    ]
