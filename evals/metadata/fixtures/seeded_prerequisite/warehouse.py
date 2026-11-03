"""No raw tables: the task data is a seed this project owns and loads itself."""

from __future__ import annotations

from evals.metadata.fixtures import Table


def tables(seed: int) -> list[Table]:
    return []
