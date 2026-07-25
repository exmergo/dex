"""The storage contract: where dex's own scratch state lives.

`.dex/` state is NOT the source of truth. The source of truth is the dbt project
(see dbt_project.py), which stays a git-reviewable filesystem artifact and never
moves into a datastore. This package covers only the non-canonical scratch state:
the exploration cache, the reconcile snapshot, the last drift report, the
append-only query and spend ledgers, and the transform plans. Delete all of it and
nothing canonical is lost.

Where that state lands is a backend choice, injected at the entry point rather
than hardcoded: the CLI selects :class:`~.filesystem.FilesystemStore` (plain files
under `.dex/`, persistence is git, not a service), while an in-process library
caller can select :class:`~.memory.MemoryStore` and write nothing at all.

Secrets never live in any backend.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..maintain.drift import DriftReport
    from ..maintain.snapshot import Snapshot
    from ..transform.plans import EditKind, TransformPlan


class Document(str, Enum):
    """The addressable pieces of `.dex/` state, for locator lookups."""

    CACHE = "cache"
    SNAPSHOT = "snapshot"
    DRIFT = "drift"
    QUERY_LOG = "queries"
    SPEND_LOG = "spend"


@runtime_checkable
class Store(Protocol):
    """Behavioral contract for a `.dex/` state backend.

    Backend state lives inside the store instance (class DI): it holds the
    connection, directory, or dictionaries the documents live in, so no caller
    needs to know where state lands or how it is addressed. Callers receive a
    store; they never construct one from a path.

    Every ``save_*`` returns an opaque **locator**: a display string identifying
    where the document went, for the envelope field an agent surfaces to a human.
    It is deliberately not a ``Path``, so a backend with no filesystem is
    representable.
    """

    # --- documents ------------------------------------------------------------

    def load_cache(self) -> DexCache | None:
        """The stored exploration cache, or None when nothing has been stored."""
        ...

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        """Store the cache, stamping ``provenance.updated_at`` when ``now`` is
        given. Returns the locator."""
        ...

    def load_snapshot(self) -> Snapshot | None: ...

    def save_snapshot(self, snapshot: Snapshot) -> str: ...

    def load_drift(self) -> DriftReport | None: ...

    def save_drift(self, report: DriftReport) -> str: ...

    # --- ledgers --------------------------------------------------------------

    def append_query_log(self, entry: dict) -> None:
        """Append one `explore query` decision to the query ledger.

        Refusals are logged too: the ledger is the audit trail and the product
        signal for which probe shapes recur often enough to deserve promotion to
        a named command. SQL text only, never result values.
        """
        ...

    def append_spend_log(self, entry: dict) -> None:
        """Append one billed-command record to the spend ledger.

        The ledger is the audit trail for warehouse spend and the substrate for
        the cumulative session budget: byte counts, job ids, and statement
        hashes only, never SQL values or credentials.
        """
        ...

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
        """
        ...

    # --- plans ----------------------------------------------------------------

    def save_plan(self, plan: TransformPlan) -> str: ...

    def load_plan(self, plan_id: str) -> TransformPlan:
        """The stored plan, or raise ``PlanNotFoundError`` when there is none."""
        ...

    def list_plans(self) -> list[TransformPlan]:
        """Every stored plan, newest first."""
        ...

    def latest_plan(self, kind: EditKind | None = None) -> TransformPlan | None:
        """The most recent unapplied plan, optionally only-of-``kind`` edits."""
        ...

    # --- locators -------------------------------------------------------------

    def locator(self, document: Document) -> str:
        """Where ``document`` lives, whether or not it has been stored yet. For
        the callers that report a location without writing one (a drift path in a
        detector envelope, a partial-write path in a budget-exhaustion error)."""
        ...

    def plan_locator(self, plan_id: str) -> str: ...


def spend_total(
    entries: list[dict],
    cutoff_iso: str,
    *,
    field: str = "billed_bytes",
    connector: str | None = None,
) -> float:
    """Sum one unit out of a spend ledger, filtered by cutoff and connector.

    Shared by every backend so the session-budget guard sums identically however
    the ledger is stored. String comparison on ``at`` is correct because every
    stamp is written by dex in the same UTC ISO format. Malformed entries are
    skipped rather than poisoning the budget check.
    """

    total = 0.0
    for entry in entries:
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
