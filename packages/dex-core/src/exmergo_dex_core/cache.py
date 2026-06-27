"""The `.dex/` cache: dex's own scratch state, which is NOT the source of truth.

The source of truth is the dbt project (see dbt_project.py). This cache holds only
what the dbt project has no home for: exploration artifacts (column profiles, PII
flags, inferred relationships, candidate keys, grain candidates, rankings, and
data-quality observations) and the reconcile snapshot. It informs dex's proposals;
it is never authoritative. Delete `.dex/` and nothing canonical is lost: dex
re-derives the cache from the dbt project and the warehouse.

Persistence is plain files under `.dex/` in the user's repo (persistence is git,
not a service). Secrets never live here.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

# Bump when the on-disk cache shape changes in a way old readers cannot handle.
CACHE_SCHEMA_VERSION = 1


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
    """One value and how often it occurs: an element of a column's categorical
    sketch. ``value`` is always a string (sketching is gated to text columns)."""

    value: str
    count: int


class ColumnProfile(BaseModel):
    """Aggregate-derived understanding of one column, built from SQL aggregates and
    never from raw rows in context."""

    name: str
    data_type: str
    nullable: bool = True
    null_fraction: float | None = None
    distinct_count: int | None = None
    is_unique: bool | None = None
    min_value: object | None = None
    max_value: object | None = None
    pii: PIIFlag | None = None
    # The categorical sketch: most-frequent values with counts, only for short,
    # low-cardinality, non-PII text columns. Value frequencies are aggregates, not
    # rows. This is an INTENTIONAL list-of-dicts once serialized; its field name
    # must never match envelope._RAW_ROW_KEY_PATTERNS or emit() would reject it.
    top_values: list[ValueCount] | None = None


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
    rank_score: float | None = None
    data_quality: list[str] = Field(default_factory=list)


class RelationshipKind(str, Enum):
    DECLARED = "declared"
    INFERRED = "inferred"


class Relationship(BaseModel):
    """A join between two datasets, declared (FK / dbt) or inferred (heuristic)."""

    from_dataset: str
    from_columns: list[str]
    to_dataset: str
    to_columns: list[str]
    kind: RelationshipKind = RelationshipKind.INFERRED
    confidence: float | None = None


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
    """The whole exploration cache for one repo. This is what `.dex/cache.json`
    holds: what dex has learned about the warehouse, used to inform proposals
    against the dbt project. Not canonical."""

    schema_version: int = CACHE_SCHEMA_VERSION
    datasets: list[Dataset] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    provenance: CacheProvenance = Field(default_factory=CacheProvenance)


# --- .dex/ persistence -------------------------------------------------------
#
# Layout (all non-secret, committed to the user's repo):
#   .dex/config.yml     non-secret config (see config.py)
#   .dex/cache.json     the current DexCache (exploration artifacts)
#   .dex/snapshot.json  the last reconcile snapshot (a frozen fingerprint)

DEX_DIR = ".dex"
CACHE_FILE = "cache.json"
SNAPSHOT_FILE = "snapshot.json"


class DexStore:
    """Reads and writes the `.dex/` cache for a given repo root.

    State is on disk, so the CLI subcommands stay stateless and the agent
    orchestrates multi-step flows by re-reading the cache and the dbt project
    between calls.
    """

    def __init__(self, repo_root: Path | str = "."):
        self.root = Path(repo_root)
        self.dex_dir = self.root / DEX_DIR

    def exists(self) -> bool:
        return self.dex_dir.is_dir()

    def load_cache(self) -> DexCache | None:
        path = self.dex_dir / CACHE_FILE
        if not path.is_file():
            return None
        return DexCache.model_validate_json(path.read_text(encoding="utf-8"))

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> Path:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / CACHE_FILE
        path.write_text(cache.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    def load_snapshot(self) -> DexCache | None:
        path = self.dex_dir / SNAPSHOT_FILE
        if not path.is_file():
            return None
        return DexCache.model_validate_json(path.read_text(encoding="utf-8"))

    def save_snapshot(self, cache: DexCache) -> Path:
        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / SNAPSHOT_FILE
        path.write_text(cache.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path
