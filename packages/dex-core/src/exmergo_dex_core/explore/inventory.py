"""Explore: landscape-scale inventory.

A single cheap catalog pass that produces counts and sizes for every object, never
rows. This is the entry point to sense-making: it is what makes ranking and
selective drill-down possible without scanning the warehouse.
"""

from __future__ import annotations

from ..adapters.base import Adapter, ObjectMeta


def inventory(adapter: Adapter, *, include_views: bool = True) -> list[ObjectMeta]:
    """Return cheap, scan-free metadata for every object in the warehouse."""

    return adapter.list_objects(include_views=include_views)
