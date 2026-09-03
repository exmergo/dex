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

import re
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
    #: The semantic models that sit on this relation, from the project's own
    #: semantic layer. This is the physical catalog's half of a link the semantic
    #: catalog carries in the other direction, and it is what makes "is this table
    #: load-bearing" answerable from the map: a relation several metrics are built
    #: on is a different object from one nothing reads, and the two are
    #: indistinguishable by row count and PII flags alone. Empty means the layer
    #: exposes it through no model **or** that no project was read, which is why
    #: it is folded in only under `--use-project` and stated in the payload rather
    #: than inferred from its absence.
    semantic_models: list[str] = Field(default_factory=list)

    def notable_columns(
        self,
        join_columns: set[str] | None = None,
        *,
        everything: bool = False,
    ) -> tuple[list[tuple[ColumnProfile, str | None]], int]:
        """The columns worth reporting about this dataset, with the role each plays.

        A dataset knows which of its own columns carry meaning: the ones that
        establish its grain, the ones that key it, the ones a join lands on, and
        the ones flagged as personal data. Everything else is schema, and
        reporting it is the enumeration dex exists not to do.

        Returns each kept column paired with its role (``"grain"``, ``"key"``,
        ``"join"``, or ``None`` for a column kept only because it is flagged) and
        the number dropped, so a caller can always say how much it did not show.
        ``everything`` keeps every column and still assigns the roles.

        ``join_columns`` is supplied rather than derived, because which joins are
        in view is the caller's question: a diagram marks FK against the edges it
        actually drew, and a map marks it against every edge in the cache.

        Warehouse column order is preserved rather than sorted: it is how the
        table reads in every other tool, and it is already stable in the cache.
        """

        keyed = {c.lower() for group in self.candidate_keys for c in group}
        keyed |= {c.lower() for group in self.composite_keys for c in group}
        grain = {c.lower() for c in (self.grain or [])}
        joins = {c.lower() for c in (join_columns or ())}

        kept: list[tuple[ColumnProfile, str | None]] = []
        dropped = 0
        for column in self.columns:
            lowered = column.name.lower()
            if lowered in grain:
                role: str | None = "grain"
            elif lowered in joins:
                role = "join"
            elif lowered in keyed or column.is_unique is True:
                role = "key"
            else:
                role = None
            if role is None and column.pii is None and not everything:
                dropped += 1
                continue
            kept.append((column, role))
        return kept, dropped

    def columns_with_findings(
        self, *, everything: bool = False
    ) -> tuple[list[ColumnProfile], int]:
        """The columns of this dataset worth showing by default in `explore
        profile`'s payload: the ones its own checks already flagged, so the
        verdict a caller asked for -- grain, keys, data quality -- is not the
        part 107 columns of schema push past a truncating harness's cutoff.

        Deliberately not `notable_columns`: that method is shared with `map`
        and `diagram`, where "notable" means a grain/key/join/PII role, and
        widening it to include null fraction and data-quality mentions would
        change what those two commands consider notable too. This predicate
        is `profile`'s own.

        A column carries a finding if it is PII-flagged, has a non-zero null
        fraction, is a member of a candidate or composite key (or is itself
        proven unique), carries a reported value domain (a low-cardinality
        enumeration the profiler specifically computed, so its presence is
        already the profiler saying this column is worth a look), or is
        named in one of this dataset's own `data_quality` sentences. The
        last check is a word-boundary match against the joined notes, not a
        raw substring (a column named `am` must not match `amount`), and it
        can still over-include a column merely mentioned in a note about a
        different one. That is the safe direction to be wrong in: the
        predicate exists so a real finding is never the reason it gets
        truncated away, not to be a precise finding-to-column index.
        ``everything`` keeps every column.
        """

        keyed = {c.lower() for group in self.candidate_keys for c in group}
        keyed |= {c.lower() for group in self.composite_keys for c in group}
        note_text = " ".join(self.data_quality)

        kept: list[ColumnProfile] = []
        dropped = 0
        for column in self.columns:
            has_finding = (
                column.pii is not None
                or bool(column.null_fraction)
                or column.name.lower() in keyed
                or column.is_unique is True
                or column.value_domain is not None
                or re.search(rf"\b{re.escape(column.name)}\b", note_text, re.IGNORECASE)
                is not None
            )
            if everything or has_finding:
                kept.append(column)
            else:
                dropped += 1
        return kept, dropped


class RelationshipKind(str, Enum):
    DECLARED = "declared"
    INFERRED = "inferred"
    #: Proposed by the opt-in ``--infer-by-overlap`` sweep (issue #220): no
    #: column name matched on either side, so the edge exists only because a
    #: probe measured real value containment between two key-shaped columns.
    #: Kept distinct from INFERRED, which always carries a name-based signal,
    #: so an edge with no naming evidence at all is never presented as
    #: equivalent to one that has some.
    OVERLAP_INFERRED = "overlap_inferred"


class Relationship(BaseModel):
    """A join between two datasets: declared (a project's own statement), inferred
    (a name-based heuristic), or overlap-inferred (a name-blind value-containment
    probe).

    A declared edge has two possible sources and they are equally authoritative: a
    ``relationships`` test, which is the project's claim that a foreign key holds,
    and a semantic layer's shared entity, which is a join the layer will actually
    perform with a key it names per model. ``declared_by`` names the second, whose
    name (the entity) is something a reader can look up and the edge does not
    otherwise carry. It stays unset for a ``relationships`` test, which declares
    exactly the two columns the edge already names, so repeating them there would
    be noise rather than provenance.

    ``verified`` and ``orphan_fraction`` are set by the opt-in ``--verify``
    overlap probe on a DECLARED or INFERRED edge: a name-based join stays a
    guess until measured, and a declared one is a claim the project makes
    about the data, which is measurable for the same reason. An
    OVERLAP_INFERRED edge carries both from the moment it is proposed, since
    the measurement that found it *is* the evidence for its existence; there
    is no unmeasured, name-based prior state for it to start from.

    ``confidence`` means "how sure is dex that this join exists", so a later
    ``--verify`` measurement moves it only on an INFERRED edge. A declared one
    sits at 1.0 and stays there; when its probe disagrees, that is a finding
    about the warehouse or the declaration, not weaker evidence for the edge
    (issue #163). An OVERLAP_INFERRED edge's confidence is set once, from the
    containment the discovery probe measured, and a later ``--verify`` pass
    leaves it alone the same way it leaves a declared edge's 1.0 alone.
    """

    from_dataset: str
    from_columns: list[str]
    to_dataset: str
    to_columns: list[str]
    kind: RelationshipKind = RelationshipKind.INFERRED
    confidence: float | None = None
    verified: bool = False
    orphan_fraction: float | None = None
    declared_by: str | None = None


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

    def ranked_datasets(
        self, *, profiled_only: bool = True, connected_only: bool = False
    ) -> list[Dataset]:
        """The datasets worth reporting, best first.

        An empty ``columns`` list is the codebase's own test for "inventoried but
        never profiled" (see ``_compose_datasets``), so ``profiled_only`` is the
        difference between an object that can say something about itself and one
        that cannot.

        ``connected_only`` is a separate question, and callers genuinely differ on
        it. A diagram wants it: an isolated box with no edge is a box that says
        nothing about the model. A findings payload does not: an isolated object
        that carries four PII flags and an empty-table warning is exactly a
        finding, and dropping it would leave the envelope's own
        ``pii_column_count`` contradicting the objects beside it.

        Order is rank first and identifier second, never cache order, so the same
        cache reports byte-identically however its datasets happen to be stored.
        """

        connected = {r.from_dataset.lower() for r in self.relationships}
        connected |= {r.to_dataset.lower() for r in self.relationships}
        candidates = [
            d
            for d in self.datasets
            if (d.columns or not profiled_only)
            and (not connected_only or d.identifier.lower() in connected)
        ]
        return sorted(candidates, key=lambda d: (-(d.rank_score or 0.0), d.identifier))


def join_columns_by_dataset(
    relationships: list[Relationship],
) -> dict[str, set[str]]:
    """Columns that carry a join, per lowered dataset identifier, for the FK role.

    Takes the relationships in view rather than reading a whole cache, so a
    caller that narrowed its edges marks FK against what it kept rather than
    against edges it dropped.
    """

    columns: dict[str, set[str]] = {}
    for rel in relationships:
        columns.setdefault(rel.from_dataset.lower(), set()).update(
            c.lower() for c in rel.from_columns
        )
        columns.setdefault(rel.to_dataset.lower(), set()).update(
            c.lower() for c in rel.to_columns
        )
    return columns
