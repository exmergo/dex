"""The cached map as a bounded findings payload.

``explore map`` used to answer its own question with a receipt: how many objects,
how many joins, how many PII flags, and nothing about what any of them were. A
caller who wanted the answer ran ``explore profile`` and ``explore relationships``
afterwards, so the rule against dumping a schema was costing more context than it
saved. This module is the middle: what the map found, budgeted.

The selection is not local. Which objects and which columns are worth reporting is
:meth:`.cache.DexCache.ranked_datasets` and :meth:`.cache.Dataset.notable_columns`,
shared with :mod:`.diagram`, so the picture and the payload can never disagree
about what matters. What is local is the budget, and the rule the budget obeys:
every cap binds in every mode, ``detail`` widens what is *eligible* rather than
how much is shown, and whatever is cut is counted both in a field and in a note.
A truncated answer that reads as a complete one is worse than no answer.

No column value ever appears here. ``min_value``, ``max_value`` and
``value_domain`` sit in the cache for the columns that earned them and are
deliberately not read: ``explore profile`` is where a caller asks for a value
domain, deliberately and one object at a time. PII is category and confidence, as
it is everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..cache import Dataset, DexCache, PIIFlag, Relationship, join_columns_by_dataset

# Matches `DexConfig.profile_top_n`, so a default map's payload covers exactly the
# objects a default map profiled: the cap binds on a warehouse big enough to have
# been sampled anyway, and never on one small enough to have been read whole.
MAX_OBJECTS = 25
# Past a dozen columns an object's entry stops being its shape and starts being
# its schema, which is the thing explore exists not to paste.
MAX_COLUMNS_PER_OBJECT = 12
# The same ceiling `diagram.MAX_ENTITIES` uses, so the two commands stop widening
# at the same point.
MAX_EDGES = 40
MAX_DATA_QUALITY_PER_OBJECT = 5


class MapColumn(BaseModel):
    """One column, as the map reports it: shape and flags, never a value.

    ``role`` is why the column is here (``grain``, ``key``, ``join``), or ``None``
    for one kept only because it carries a PII flag.
    """

    name: str
    data_type: str | None = None
    role: str | None = None
    pii: PIIFlag | None = None


class MapObject(BaseModel):
    """One object's findings: what it is, what identifies it, what is wrong with it.

    ``pii_column_count`` and the elision counts are over the object's whole
    profile, not over what survived the cap, so a caller reading a trimmed entry
    still learns the true totals.
    """

    identifier: str
    object_type: str = "table"
    row_count: int | None = None
    rank_score: float | None = None
    profiled_at: str | None = None
    grain: list[str] | None = None
    candidate_key: list[str] | None = None
    columns: list[MapColumn] = Field(default_factory=list)
    elided_column_count: int = 0
    pii_column_count: int = 0
    data_quality: list[str] = Field(default_factory=list)
    elided_data_quality_count: int = 0


@dataclass(frozen=True)
class MapView:
    """The budgeted map, plus everything a caller needs to know it is budgeted."""

    objects: list[MapObject] = field(default_factory=list)
    edges: list[Relationship] = field(default_factory=list)
    elided_object_count: int = 0
    elided_column_count: int = 0
    elided_edge_count: int = 0
    notes: list[str] = field(default_factory=list)


def summarize_map(
    cache: DexCache,
    *,
    detail: bool = False,
    max_objects: int = MAX_OBJECTS,
    max_columns: int = MAX_COLUMNS_PER_OBJECT,
    max_edges: int = MAX_EDGES,
    max_data_quality: int = MAX_DATA_QUALITY_PER_OBJECT,
) -> MapView:
    """Reduce ``cache`` to the findings worth returning in one envelope.

    By default this reports every profiled object, best-ranked first, carrying its
    grain, key, join and PII-flagged columns. Selecting *columns* is what keeps
    the payload readable on a warehouse of any size; objects are not filtered
    beyond the cap, because an object dex profiled and then declined to mention is
    an object whose findings the envelope counted and then withheld.

    ``detail`` widens eligibility to objects that were inventoried but never
    profiled, and the selection to every column. It lifts none of the caps, which
    always bind.
    """

    ranked = cache.ranked_datasets(profiled_only=not detail)
    kept = ranked[:max_objects]
    kept_lower = {d.identifier.lower() for d in kept}

    # Edges are reported only between objects the payload also describes: an edge
    # to an identifier that appears nowhere else in the envelope is a dangling
    # reference the caller cannot resolve.
    in_view = [
        rel
        for rel in cache.relationships
        if rel.from_dataset.lower() in kept_lower
        and rel.to_dataset.lower() in kept_lower
    ]
    in_view.sort(key=lambda r: (r.to_dataset, r.from_dataset, tuple(r.from_columns)))
    edges = in_view[:max_edges]

    # FK is marked against the edges in view rather than every edge in the cache,
    # so the role a column is given is one the same payload can be checked against.
    join_columns = join_columns_by_dataset(edges)

    objects: list[MapObject] = []
    elided_columns = 0
    for dataset in kept:
        selected, dropped = dataset.notable_columns(
            join_columns.get(dataset.identifier.lower(), set()), everything=detail
        )
        dropped += max(0, len(selected) - max_columns)
        elided_columns += dropped
        objects.append(
            MapObject(
                identifier=dataset.identifier,
                object_type=dataset.object_type,
                row_count=dataset.row_count,
                rank_score=dataset.rank_score,
                profiled_at=dataset.profiled_at,
                grain=dataset.grain,
                candidate_key=_best_key(dataset),
                columns=[
                    MapColumn(
                        name=column.name,
                        data_type=column.data_type,
                        role=role,
                        pii=column.pii,
                    )
                    for column, role in selected[:max_columns]
                ],
                elided_column_count=dropped,
                pii_column_count=sum(1 for c in dataset.columns if c.pii is not None),
                data_quality=dataset.data_quality[:max_data_quality],
                elided_data_quality_count=max(
                    0, len(dataset.data_quality) - max_data_quality
                ),
            )
        )

    notes: list[str] = []
    elided_objects = len(ranked) - len(kept)
    if elided_objects:
        notes.append(
            f"{elided_objects} eligible object(s) are not described here: the map "
            f"payload is capped at {max_objects} objects, kept by rank. Name one "
            "with `explore profile` to read it in full"
        )
    if elided_columns:
        notes.append(
            f"{elided_columns} column(s) are not described. The default keeps "
            f"grain, key, join and PII-flagged columns, at most {max_columns} per "
            "object; --detail widens the selection to every column, and the "
            "per-object cap still binds"
        )
    dropped_edges = len(cache.relationships) - len(edges)
    if dropped_edges:
        out_of_view = len(cache.relationships) - len(in_view)
        capped = len(in_view) - len(edges)
        reasons = []
        if out_of_view:
            reasons.append(
                f"{out_of_view} because at least one endpoint is not described here"
            )
        if capped:
            reasons.append(f"{capped} because the payload is capped at {max_edges}")
        notes.append(
            f"{dropped_edges} relationship(s) are not listed: " + ", ".join(reasons)
        )
    trimmed_findings = sum(o.elided_data_quality_count for o in objects)
    if trimmed_findings:
        notes.append(
            f"{trimmed_findings} data-quality finding(s) are not listed: at most "
            f"{max_data_quality} are reported per object"
        )

    return MapView(
        objects=objects,
        edges=edges,
        elided_object_count=elided_objects,
        elided_column_count=elided_columns,
        elided_edge_count=dropped_edges,
        notes=notes,
    )


def _best_key(dataset: Dataset) -> list[str] | None:
    """The strongest identifier the profile found, proof before heuristic.

    ``composite_keys`` are combinations an exact probe proved unique, so they
    outrank ``candidate_keys``, which the annotation pass derives from column
    statistics. A single-column candidate key is still returned when it is all
    there is.
    """

    for group in (*dataset.composite_keys, *dataset.candidate_keys):
        if group:
            return list(group)
    return None
