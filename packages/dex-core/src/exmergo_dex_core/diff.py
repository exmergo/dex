"""Snapshot and drift engine powering reconcile: diff the current dbt project and
warehouse schema against the last .dex snapshot and propose edits. Not yet
implemented.
"""

from __future__ import annotations

from typing import Any


def snapshot(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError


def drift(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError
