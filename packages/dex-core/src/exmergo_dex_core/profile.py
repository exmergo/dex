"""Explore: column profiling and PII detection, built from SQL aggregates and
never raw rows. PII is recorded as (column, category, confidence) in the cache,
never surfaced with example values. Not yet implemented.
"""

from __future__ import annotations

from typing import Any


def profile(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError
