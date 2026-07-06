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

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .maintain.drift import DriftReport
    from .maintain.snapshot import Snapshot

# Bump when the on-disk cache shape changes in a way old readers cannot handle.
CACHE_SCHEMA_VERSION = 2


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
#   .dex/snapshot.json  the maintain baseline (see maintain/snapshot.py)
#   .dex/drift.json     the last drift-detection report (see maintain/drift.py)

DEX_DIR = ".dex"
CACHE_FILE = "cache.json"
SNAPSHOT_FILE = "snapshot.json"
DRIFT_FILE = "drift.json"
QUERIES_FILE = "queries.jsonl"
SPEND_FILE = "spend.jsonl"


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

    def load_snapshot(self) -> Snapshot | None:
        from .maintain.snapshot import Snapshot

        path = self.dex_dir / SNAPSHOT_FILE
        if not path.is_file():
            return None
        return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def save_snapshot(self, snapshot: Snapshot) -> Path:
        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / SNAPSHOT_FILE
        path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    def load_drift(self) -> DriftReport | None:
        from .maintain.drift import DriftReport

        path = self.dex_dir / DRIFT_FILE
        if not path.is_file():
            return None
        return DriftReport.model_validate_json(path.read_text(encoding="utf-8"))

    def save_drift(self, report: DriftReport) -> Path:
        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / DRIFT_FILE
        path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path

    def append_query_log(self, entry: dict) -> Path:
        """Append one `explore query` decision to `.dex/queries.jsonl`.

        Refusals are logged too: the log is the audit trail and the product
        signal for which probe shapes recur often enough to deserve promotion
        to a named command. SQL text only, never result values.
        """

        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / QUERIES_FILE
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return path

    def append_spend_log(self, entry: dict) -> Path:
        """Append one billed-command record to `.dex/spend.jsonl`.

        The ledger is the audit trail for warehouse spend and the substrate for
        the cumulative session budget: byte counts, job ids, and statement
        hashes only, never SQL values or credentials.
        """

        self.dex_dir.mkdir(parents=True, exist_ok=True)
        path = self.dex_dir / SPEND_FILE
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        return path

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        """Total ``field`` recorded at or after ``cutoff_iso`` (ISO-8601).

        ``field`` and ``connector`` keep paradigms separate: a session budget in
        bytes must never absorb a seconds entry from another connector sharing
        the ledger, so callers sum their own connector's own unit.

        String comparison is correct here because every `at` stamp is written
        by dex in the same UTC ISO format. Malformed lines are skipped rather
        than poisoning the budget check.
        """

        path = self.dex_dir / SPEND_FILE
        if not path.is_file():
            return 0.0
        total = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if connector is not None and entry.get("connector") != connector:
                continue
            at = entry.get("at")
            billed = entry.get(field)
            if (
                isinstance(at, str)
                and at >= cutoff_iso
                and isinstance(billed, (int, float))
            ):
                total += float(billed)
        return total
