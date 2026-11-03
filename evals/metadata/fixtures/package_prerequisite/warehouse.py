"""raw.task_data as an ingestion vendor delivers it.

`due_at` is empty on every row and typed integer, as an all-NULL column often is
when a loader infers types, so the tiny_tasks package's `coalesce(due_at, due_on)`
cannot run. Two tasks have no `due_on` either, so a repair that drops rows to make
a cast succeed shows up as a missing task.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from evals.metadata.fixtures import Table

NAMES = ("plan", "draft", "review", "revise", "approve", "publish", "archive", "audit")


def tables(seed: int) -> list[Table]:
    start = datetime(2024, 1, 1, 9) + timedelta(days=seed % 7)
    rows = []
    for task_id, name in enumerate(NAMES, start=1):
        due_on = None if task_id in (3, 7) else start + timedelta(days=3 * task_id)
        rows.append((task_id, name, due_on, None))
    return [
        Table(
            "raw.task_data",
            [
                ("task_id", "integer"),
                ("task_name", "varchar"),
                ("due_on", "timestamp"),
                ("due_at", "integer"),
            ],
            rows,
        )
    ]
