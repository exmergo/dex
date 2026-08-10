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


class ValueCount(BaseModel):
    """One distinct value's frequency, part of a reported value domain."""

    value: object
    count: int


class ValueDomain(BaseModel):
    """A column's value domain: distinct values by frequency, capped.

    ``elided`` is the number of additional distinct values beyond the cap
    (0 when the column's full domain fit). Never populated for a PII-flagged
    column, at any confidence -- see `_probe_value_domains` in
    `explore/profile.py`.
    """

    values: list[ValueCount]
    elided: int = 0


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
    value_domain: ValueDomain | None = None
    #: Temporal continuity (#206): the range between min and max at a
    #: detected granularity ("day" | "month" | "hour") against how many of
    #: those periods are actually present. ``None`` outside a date/timestamp
    #: column, or when there isn't enough evidence (e.g. min/max absent).
    #: The statistic is neutral -- a genuinely sparse event-timestamp column
    #: reports large numbers here without being flagged as broken.
    temporal_granularity: str | None = None
    temporal_span: int | None = None
    temporal_distinct_periods: int | None = None
    temporal_missing_periods: int | None = None
    temporal_largest_gap: int | None = None


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


def relation_verdict(name: str, live: list[str]) -> str | None:
    """Why a relation absent from ``live`` is absent: ``"foreign"``, ``"missing"``,
    or None when the listing cannot settle it.

    The two answers are different problems with different fixes, and the top-level
    namespace is what separates them. dex's dataset allowlist scopes which schemas
    *within* a connection are inventoried, so a relation in an unlisted schema of a
    connected database is out of the listing's scope, not out of reach: refusing it
    would answer a question dex never asked. A relation in a database the
    connection does not carry at all is the real mismatch, because no allowlist
    could bring it into scope.

    An unqualified name is never adjudicated: it resolves against the session's
    default schema, which the listing does not describe. Callers that cannot settle
    a name must fall back to whatever refusal they would have raised anyway; None
    here means "no opinion", never "fine".
    """

    parts = name.lower().split(".")
    if len(parts) < 2:
        return None
    catalogs: set[str] = set()
    schemas: set[str] = set()
    namespaces: set[str] = set()
    for ident in live:
        listed = ident.lower().split(".")
        if len(listed) < 2:
            continue
        schemas.add(listed[-2])
        namespaces.add(".".join(listed[-3:-1]) if len(listed) >= 3 else listed[-2])
        if len(listed) >= 3:
            catalogs.add(listed[-3])

    if len(parts) >= 3:
        catalog, schema = parts[-3], parts[-2]
        if catalogs and catalog not in catalogs:
            return "foreign"
        return "missing" if f"{catalog}.{schema}" in namespaces else None
    return "missing" if parts[-2] in schemas else None


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
