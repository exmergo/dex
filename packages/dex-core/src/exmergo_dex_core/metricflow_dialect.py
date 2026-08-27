"""The dbt/MetricFlow query dialect: the grammar both dbt backends speak.

A semantic layer's query language is the vendor's, not dex's. MetricFlow spells a
filter as a Jinja call (``{{ Dimension('user__pricing_tier') }} = 'pro'``), spells
a time grain into the group-by token (``metric_time__month``), and calls a
metric's own time axis ``metric_time``. None of that is portable: a layer whose
filters are JSON objects matches none of these patterns and names its grains its
own way.

So it lives here, in one module named for the vendor whose grammar it is, rather
than in the neutral seam beside the screening policy. Two things follow. The PII
gate can keep asking a backend what a filter clause references without knowing any
dialect, which is what stops a second format from inheriting an extractor that
silently matches nothing against its filters. And the grain vocabulary can be
widened or replaced per vendor without touching shared code.

A leaf beside :mod:`.semantic_catalog` rather than a module inside
``explore.semantic``, because three modules read it and they sit at different
layers: the dbt project format (deriving a catalog from the compiled artifact) and
both dbt query backends. It imports nothing of dex's for the same reason that one
does.

This is the query *dialect*. The MetricFlow *library* shim (rendering metric SQL
through ``explain()``) stays in :mod:`.explore.semantic.local`, which is the only
place that needs the ``[semantic]`` extra.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# The token that stands for a metric's own aggregation time dimension. Not a
# dimension of the layer: it resolves per metric to that metric's measures' agg
# time dimension, which is why a catalog reports the resolution per metric.
METRIC_TIME = "metric_time"

# The layer's standard time grains, coarsest last, matching the dbt Cloud API's
# `TimeGranularity` enum. Wider than the five grains dex used to accept, because
# the layer accepts these and refusing a grain the vendor supports is dex
# refusing on its own authority. A deployment may add custom granularities on top,
# which is why every caller takes the recognized set as an argument rather than
# reading this constant directly.
STANDARD_GRAINS = (
    "nanosecond",
    "microsecond",
    "millisecond",
    "second",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "quarter",
    "year",
)

# A filter clause names a dimension or an entity through a Jinja call. Both forms
# of the dimension call (`Dimension`, `TimeDimension`) take the qualified token as
# their first argument, which is the token the gate screens.
_DIMENSION_REF = re.compile(r"(?:Time)?Dimension\(\s*['\"]([^'\"]+)['\"]")
_ENTITY_REF = re.compile(r"Entity\(\s*['\"]([^'\"]+)['\"]")


def filter_refs(clauses: list[str]) -> list[str]:
    """Every dimension and entity token a set of Jinja filter clauses names.

    In clause order, duplicates included: de-duplication belongs to the caller
    that merges these with the group-by tokens. A clause that references nothing
    dex can recognize contributes nothing, which is correct here and only here:
    this dialect's filters genuinely are these two call forms, so an empty result
    means the clause named no dimension rather than that the parser missed one.
    """

    refs: list[str] = []
    for clause in clauses:
        refs.extend(_DIMENSION_REF.findall(clause))
        refs.extend(_ENTITY_REF.findall(clause))
    return refs


def order_grains(values: Iterable[str] | None) -> list[str]:
    """Grains finest first, de-duplicated, with any this vocabulary does not name
    kept at the end in the order they arrived.

    Two callers need the same order for the same layer: a project read deriving
    grains from a declared base grain, and a hosted read merging the API's two
    granularity fields. A deployment's custom granularities are coarse by
    construction (they are built on the time spine's own grain) and dex has no way
    to order them against each other, so it does not pretend to.
    """

    seen = [str(value).lower() for value in values or ()]
    ordered = [grain for grain in STANDARD_GRAINS if grain in seen]
    return ordered + [grain for grain in dict.fromkeys(seen) if grain not in ordered]


def split_grain(
    token: str,
    default_grain: str | None = None,
    *,
    grains: tuple[str, ...] | None = None,
) -> tuple[str, str | None]:
    """A group-by or order-by token as ``(name, grain)``.

    MetricFlow spells a grain into the token, so a trailing ``__<grain>`` is a
    grain (``metric_time__month``) while an ordinary ``entity__dimension`` is not.
    The two are told apart by vocabulary, which is why ``grains`` is an argument:
    a deployment that declares a custom granularity spells it into a token the
    same way, and the standard set alone would read it as part of the name.

    ``metric_time`` picks up the query's own grain when the token carries none,
    because a caller asking for a monthly series says ``--grain month`` at least
    as often as it says ``metric_time__month``.
    """

    vocabulary = tuple(grains) if grains else STANDARD_GRAINS
    name, grain = token, None
    if "__" in token:
        head, tail = token.rsplit("__", 1)
        if tail.lower() in vocabulary:
            name, grain = head, tail.lower()
    if name == METRIC_TIME and grain is None and default_grain:
        grain = default_grain.lower()
    return name, grain


def spell_grain(token: str, grain: str | None) -> str:
    """The token with the query's grain spelled into it, MetricFlow's own form.

    Only ``metric_time`` takes the query-level grain: an ordinary time dimension
    is grouped at the grain its own token carries, and appending one to a token
    that already names a dimension would produce a name the layer does not have.
    """

    if grain and token == METRIC_TIME:
        return f"{METRIC_TIME}__{grain.lower()}"
    return token
