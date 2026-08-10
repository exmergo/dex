"""What the explore commands return.

Each record carries the domain objects the command produced plus the counts and
notes that explain them. The notes are the part worth guarding: they say why an
object was skipped, which profiles were reused, which relationships were folded
as replica lineage, which name matches were suppressed as too generic. A caller
that only sees the numbers cannot tell a complete answer from a truncated one, so
the notes travel with the result rather than being printed and dropped.

Counts that a caller can compute from a list it already has (``object_count`` from
``objects``) are derived when the payload is built, not stored twice.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..cache import Dataset, DexCache, Relationship
from ..results import Result
from .semantic import SemanticCatalog


class InventoryEntry(BaseModel):
    """One object as inventory sees it: metadata only, never a scan.

    ``row_estimate`` is what the connector's catalog reports, which on several
    warehouses is an estimate maintained by the optimizer rather than an exact
    count, hence the name. ``rank_score`` is present only when ranking ran.
    """

    identifier: str
    object_type: str | None = None
    row_estimate: int | None = None
    column_count: int | None = None
    rank_score: float | None = None


class InventoryResult(Result):
    objects: list[InventoryEntry] = Field(default_factory=list)
    ranked: bool = False

    def data(self) -> dict[str, Any]:
        return {
            "object_count": len(self.objects),
            "objects": [o.model_dump(mode="json") for o in self.objects],
            "ranked": self.ranked,
        }


class ProfileResult(Result):
    """Profiles for the requested objects, freshly scanned or served from cache.

    ``datasets`` is the full requested set either way, so a caller reads one list
    and does not have to reassemble it; ``profiled_count`` and
    ``cache_hit_count`` say which half each came from.
    """

    datasets: list[Dataset] = Field(default_factory=list)
    profiled_count: int = 0
    cache_hit_count: int = 0
    cache_path: str = ""
    updated_at: str = ""

    def data(self) -> dict[str, Any]:
        return {
            "datasets": [d.model_dump(mode="json") for d in self.datasets],
            "profiled_count": self.profiled_count,
            "cache_hit_count": self.cache_hit_count,
            "cache_path": self.cache_path,
            "updated_at": self.updated_at,
        }


class RelationshipsResult(Result):
    relationships: list[Relationship] = Field(default_factory=list)
    declared_count: int = 0
    profiled_count: int = 0
    cache_hit_count: int = 0
    carried_relationship_count: int = 0
    cache_path: str = ""
    updated_at: str = ""

    def data(self) -> dict[str, Any]:
        return {
            "relationships": [r.model_dump(mode="json") for r in self.relationships],
            "declared_count": self.declared_count,
            "inferred_count": len(self.relationships) - self.declared_count,
            "profiled_count": self.profiled_count,
            "cache_hit_count": self.cache_hit_count,
            "carried_relationship_count": self.carried_relationship_count,
            "cache_path": self.cache_path,
            "updated_at": self.updated_at,
        }


class RankedObject(BaseModel):
    identifier: str
    rank_score: float | None = None


class MapResult(Result):
    """The whole landscape in one pass: inventory, profiles, and relationships.

    ``cache`` is the composed cache this run wrote, handed back so a library
    caller has the datasets and relationships without a second read. It stays out
    of the payload deliberately: the envelope reports the shape of the map and
    the top objects, and a full cache would be megabytes of stdout.
    """

    cache: DexCache | None = None
    cache_path: str = ""
    object_count: int = 0
    profiled_count: int = 0
    cache_hit_count: int = 0
    skipped_count: int = 0
    carried_forward_count: int = 0
    out_of_scope_carried_count: int = 0
    dropped_count: int = 0
    carried_relationship_count: int = 0
    relationship_count: int = 0
    pii_column_count: int = 0
    data_quality_note_count: int = 0
    top_objects: list[RankedObject] = Field(default_factory=list)
    updated_at: str = ""

    def data(self) -> dict[str, Any]:
        return {
            "cache_path": self.cache_path,
            "object_count": self.object_count,
            "profiled_count": self.profiled_count,
            "cache_hit_count": self.cache_hit_count,
            "skipped_count": self.skipped_count,
            "carried_forward_count": self.carried_forward_count,
            "out_of_scope_carried_count": self.out_of_scope_carried_count,
            "dropped_count": self.dropped_count,
            "carried_relationship_count": self.carried_relationship_count,
            "relationship_count": self.relationship_count,
            "pii_column_count": self.pii_column_count,
            "data_quality_note_count": self.data_quality_note_count,
            "top_objects": [o.model_dump(mode="json") for o in self.top_objects],
            "updated_at": self.updated_at,
        }


class DiagramResult(Result):
    """The cached map serialized as a Mermaid ER diagram.

    ``notes`` is always reported, like ``query`` and ``cluster``: an empty list
    is the positive statement "every eligible object and column is drawn", and
    without it a caller cannot tell a complete diagram from a capped one.

    ``entities`` maps each Mermaid entity name back to its fully-qualified
    warehouse identifier, because Mermaid names cannot carry dots and a caller
    resolving a box back to a real object has no other way to do it. It is a
    dict here and a list of records in the payload, deliberately: see
    :meth:`data`.
    """

    always_reports_notes: ClassVar[bool] = True

    mermaid: str = ""
    entities: dict[str, str] = Field(default_factory=dict)
    entity_count: int = 0
    edge_count: int = 0
    elided_entity_count: int = 0
    elided_column_count: int = 0
    elided_edge_count: int = 0
    full: bool = False
    updated_at: str = ""

    def data(self) -> dict[str, Any]:
        # `entities` becomes a list of records rather than an object keyed by
        # entity name, because a warehouse object name must never become a JSON
        # key: the envelope sanitizer matches key names against secret-like
        # substrings, so a table legitimately called `access_tokens` or
        # `user_credentials` would raise on the way out and take the whole
        # command down with it. Nothing keyed by user data crosses this
        # boundary.
        return {
            "mermaid": self.mermaid,
            "entities": [
                {"entity": name, "identifier": identifier}
                for name, identifier in sorted(self.entities.items())
            ],
            "entity_count": self.entity_count,
            "edge_count": self.edge_count,
            "elided_entity_count": self.elided_entity_count,
            "elided_column_count": self.elided_column_count,
            "elided_edge_count": self.elided_edge_count,
            "full": self.full,
            "updated_at": self.updated_at,
        }


class QueryResult(Result):
    """One firewalled SELECT, capped and returned columnar.

    Columnar rather than row dicts on purpose: the envelope sanitizer refuses
    anything shaped like raw rows, and a caller wanting rows can zip the columns
    itself. ``tables`` is what the firewall resolved the query to touch, which is
    also what the audit ledger recorded.
    """

    always_reports_notes: ClassVar[bool] = True

    columns: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    cells: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    tables: list[str] = Field(default_factory=list)
    profiled_on_demand: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "types": self.types,
            "cells": self.cells,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "tables": self.tables,
            "profiled_on_demand": self.profiled_on_demand,
        }


class ClusterResult(Result):
    """k-means over a bounded sample, reported as aggregates only.

    ``clusters`` holds sizes and centroids; no sampled row ever reaches here.
    ``sample_repeatable`` is load-bearing for interpretation: where the connector
    cannot seed its sampling, two runs draw different rows and can settle on a
    different k, so comparing them is meaningless. ``dropped_null_rows`` is
    measured by the SQL layer's own null filter (a companion count query, same
    table, same sample scope), not inferred from cells that never arrived; the
    clustering engine's own ``dropped_non_numeric_rows`` (rows that arrived but
    failed float coercion) lives nested in ``clustering``.
    """

    always_reports_notes: ClassVar[bool] = True

    object: str = ""
    total_rows: int | None = None
    dropped_null_rows: int | None = None
    sample_method: str = ""
    sample_repeatable: bool = False
    clustering: dict[str, Any] = Field(default_factory=dict)
    profiled_on_demand: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "object": self.object,
            "total_rows": self.total_rows,
            "dropped_null_rows": self.dropped_null_rows,
            "sample_method": self.sample_method,
            "sample_repeatable": self.sample_repeatable,
            "profiled_on_demand": self.profiled_on_demand,
            **self.clustering,
        }


class SemanticListResult(Result):
    """The metrics, dimensions, and entities the semantic layer exposes."""

    catalog: SemanticCatalog

    def data(self) -> dict[str, Any]:
        payload = self.catalog.to_data()
        # The catalog carries its own notes (a read-view caveat, a hosted
        # coverage note); the base class owns the notes key, so fold them in
        # rather than letting two sources fight over it.
        payload.pop("notes", None)
        return payload


class SemanticQueryResult(Result):
    """One metric query, from whichever backend answered it.

    ``backend`` matters to a caller because the two differ in who governs spend:
    the local backend renders the SQL and executes it under dex's cost guard,
    while dbt Cloud executes server-side where that guard is structurally
    unavailable, which is why a hosted result carries a ``hosted`` paradigm and
    says so in a warning rather than reporting a number it cannot know.
    """

    backend: str = ""
    columns: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    cells: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    # dbt Cloud's handle for the executed query, for correlating with its own
    # run history. Local queries have no such handle.
    query_id: str | None = None

    @classmethod
    def from_capped(
        cls, payload: dict[str, Any], *, backend: str, **fields: Any
    ) -> SemanticQueryResult:
        """Build from :func:`~.semantic.cap_columnar` output, whose ``notes``
        record every cut it made and therefore belong on the result's notes."""

        payload = dict(payload)
        notes = payload.pop("notes", [])
        return cls(backend=backend, notes=notes, **payload, **fields)

    def data(self) -> dict[str, Any]:
        payload = {
            "columns": self.columns,
            "types": self.types,
            "cells": self.cells,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "backend": self.backend,
        }
        if self.query_id is not None:
            payload["query_id"] = self.query_id
        return payload
