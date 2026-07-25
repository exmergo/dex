"""The exploration cache: dex's own scratch state, which is NOT the source of truth.

The source of truth is the dbt project (see dbt_project.py). This cache holds only
what the dbt project has no home for: exploration artifacts (column profiles, PII
flags, inferred relationships, candidate keys, grain candidates, rankings, and
data-quality observations). It informs dex's proposals; it is never authoritative.
Discard it and nothing canonical is lost: dex re-derives the cache from the dbt
project and the warehouse.

This module is the cache's shape. Where it is persisted is a backend choice that
lives behind the storage contract (see storage/base.py). Secrets never live here.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# Bump when the stored cache shape changes in a way old readers cannot handle.
CACHE_SCHEMA_VERSION = 3


class PIICategory(str, Enum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    GOVERNMENT_ID = "government_id"
    FINANCIAL = "financial"
    CREDENTIAL = "credential"
    LOCATION = "location"
    DOB = "date_of_birth"
    # Free-text fields (comments, notes, message bodies) reliably carry names and
    # contact details even though the column name itself is not a PII token.
    FREE_TEXT = "free_text"
    OTHER = "other"


class PIIFlag(BaseModel):
    """PII recorded as (column, category, confidence). Never an example value.

    There is intentionally no field for a sample value, so PII can be flagged but
    never surfaced. The flag is what propagates into emitted dbt (model and column
    `meta`).
    """

    category: PIICategory
    confidence: float = Field(ge=0.0, le=1.0)


class ColumnProfile(BaseModel):
    """Aggregate-derived understanding of one column, built from SQL aggregates and
    never from raw rows in context."""

    name: str
    data_type: str
    nullable: bool = True
    null_fraction: float | None = None
    distinct_count: int | None = None
    distinct_count_exact: bool = False
    is_unique: bool | None = None
    min_value: object | None = None
    max_value: object | None = None
    pii: PIIFlag | None = None
    #: The category the name detector matched before a `pii_overrides` entry in
    #: `.dex/config.yml` suppressed it: the audit trail that a human, not the
    #: detector, cleared this column. None when no override applied or the
    #: detector matched nothing.
    pii_overridden: PIICategory | None = None


class Dataset(BaseModel):
    """A physical object in the warehouse (table or view), fully namespaced.

    ``identifier`` is the connector-normalized fully-qualified name (BigQuery
    project.dataset.table, Snowflake/Postgres/DuckDB database.schema.table,
    Databricks Unity Catalog catalog.schema.table). Namespace normalization is an
    adapter responsibility.
    """

    identifier: str
    object_type: str = "table"
    row_count: int | None = None
    byte_size: int | None = None
    columns: list[ColumnProfile] = Field(default_factory=list)
    candidate_keys: list[list[str]] = Field(default_factory=list)
    grain: list[str] | None = None
    #: Column combinations proven unique by an exact profile-time probe, best
    #: candidate first. Kept separate from ``candidate_keys`` (which the
    #: annotation pass recomputes from column stats) so the proof survives
    #: re-annotation and its provenance stays distinct from derived signals.
    composite_keys: list[list[str]] = Field(default_factory=list)
    rank_score: float | None = None
    data_quality: list[str] = Field(default_factory=list)
    profiled_at: str | None = None


class RelationshipKind(str, Enum):
    DECLARED = "declared"
    INFERRED = "inferred"


class Relationship(BaseModel):
    """A join between two datasets, declared (FK / dbt) or inferred (heuristic).

    ``verified`` and ``orphan_fraction`` are set only by the opt-in ``--verify``
    overlap probe: an inferred join stays a name-based guess until measured.
    """

    from_dataset: str
    from_columns: list[str]
    to_dataset: str
    to_columns: list[str]
    kind: RelationshipKind = RelationshipKind.INFERRED
    confidence: float | None = None
    verified: bool = False
    orphan_fraction: float | None = None


def match_identifier(name: str, known: list[str]) -> list[str]:
    """All fully-qualified identifiers that ``name`` could mean, case-insensitive.

    Accepts an exact identifier, a dotted suffix (``schema.table``), or a bare
    object name. Shared by everything that maps user- or agent-supplied names to
    warehouse identifiers, so profile arguments and query table references
    resolve identically.
    """

    q = name.lower()
    matches = [
        ident
        for ident in known
        if ident.lower() == q
        or ident.lower().endswith(f".{q}")
        or ident.rsplit(".", 1)[-1].lower() == q
    ]
    return sorted(set(matches))


def tool_version() -> str | None:
    """The installed engine version, for stamping into cache provenance.

    Falls back to the in-tree ``__version__`` when package metadata is not
    available, e.g. an editable or source checkout that was never installed.
    """

    try:
        from importlib.metadata import version

        return version("exmergo-dex-core")
    except Exception:
        from . import __version__

        return __version__


class CacheProvenance(BaseModel):
    connector: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    tool_version: str | None = Field(default_factory=tool_version)


class DexCache(BaseModel):
    """The whole exploration cache for one project: what dex has learned about the
    warehouse, used to inform proposals against the dbt project. Not canonical."""

    schema_version: int = CACHE_SCHEMA_VERSION
    datasets: list[Dataset] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    provenance: CacheProvenance = Field(default_factory=CacheProvenance)
