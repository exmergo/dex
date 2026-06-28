"""Explore: rank objects so drill-down is selective, not exhaustive.

Ranking is what turns "here are 4000 tables" into "start with these 25." It is a
pure function over cheap metadata plus relationship connectivity, so it costs
nothing to run and re-run. The score blends four normalized signals; the weights
encode the heuristic that big, well-connected, conventionally named entity tables
are where an analytics engineer should look first.
"""

from __future__ import annotations

import math
import re

from ..adapters.base import ObjectMeta
from ..cache import Relationship

# Weights sum to 1.0; score lands in [0, 1].
_W_SIZE = 0.35
_W_CONNECTIVITY = 0.30
_W_NAMING = 0.20
_W_SHAPE = 0.15

# Naming conventions: analytics-engineering layers and entity shapes get a boost;
# scratch / backup / test objects get pushed down.
_NAME_BOOST = re.compile(r"(^|_)(fct|fact|dim|stg|mart|f|d)(_|$)", re.IGNORECASE)
_NAME_PENALTY = re.compile(
    r"(_|^)(tmp|temp|bak|backup|old|scratch|test|deleted|archive|copy)(_|$)",
    re.IGNORECASE,
)
# Column counts outside this band read as "not a clean entity" (too narrow to be
# meaningful, too wide to be a modeled table) and are damped.
_SHAPE_IDEAL = (2, 60)


def rank(
    objects: list[ObjectMeta],
    relationships: list[Relationship] | None = None,
    ranking_hints: list[str] | None = None,
) -> dict[str, float]:
    """Map identifier -> rank_score in [0, 1]. Pure; no I/O."""

    if not objects:
        return {}

    degree = _connectivity_degree(relationships or [])
    max_degree = max(degree.values(), default=0)

    row_logs = {o.identifier: math.log1p(o.row_count or 0) for o in objects}
    max_row_log = max(row_logs.values(), default=0.0)

    # Hints come from hand-edited committed config, so tolerate blank/None list
    # entries: a None would crash on .lower() and an empty string would match every
    # name (collapsing the naming signal to 1.0 for all objects).
    hints = [
        h.strip().lower()
        for h in (ranking_hints or [])
        if isinstance(h, str) and h.strip()
    ]

    scores: dict[str, float] = {}
    for obj in objects:
        size = (row_logs[obj.identifier] / max_row_log) if max_row_log > 0 else 0.0
        connectivity = (
            degree.get(obj.identifier, 0) / max_degree if max_degree > 0 else 0.0
        )
        scores[obj.identifier] = round(
            _W_SIZE * size
            + _W_CONNECTIVITY * connectivity
            + _W_NAMING * _naming_score(obj.name, hints)
            + _W_SHAPE * _shape_score(obj.column_count),
            4,
        )
    return scores


def _connectivity_degree(relationships: list[Relationship]) -> dict[str, int]:
    degree: dict[str, int] = {}
    for rel in relationships:
        degree[rel.from_dataset] = degree.get(rel.from_dataset, 0) + 1
        degree[rel.to_dataset] = degree.get(rel.to_dataset, 0) + 1
    return degree


def _naming_score(name: str, hints: list[str]) -> float:
    score = 0.5
    if _NAME_BOOST.search(name):
        score = 0.85
    if _NAME_PENALTY.search(name):
        score = 0.15
    if any(h in name.lower() for h in hints):
        score = 1.0
    return score


def _shape_score(column_count: int) -> float:
    low, high = _SHAPE_IDEAL
    if low <= column_count <= high:
        return 1.0
    if column_count < low:
        return 0.4
    # Wide tables (exports, denormalized dumps) are damped, not zeroed.
    return max(0.2, high / column_count)
