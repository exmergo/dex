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

**Writing your own backend.** The contract is three nested protocols, so a host
implements only the tier it uses (see below), and every rule it has to satisfy is
stated on the member it governs rather than left for a reader to infer from the
two shipped backends. :mod:`~.conformance` ships the executable copy of those
rules: subclass ``StoreContract``, hand it a factory, and the whole contract runs
against your backend. ``references/storage.md`` is the prose version.

**Constructing one is a separate contract**, :class:`StoreFactory` over a
:class:`StoreContext`, and it is optional. A host that passes its own instance to
the engine never needs it; it exists so a backend can also be *named* somewhere
and built by dex. Keeping it off the store protocols is what preserves the
property that makes this seam cheap to implement: a class with the right methods
is a store, with no base class to inherit and no registration step.

The protocol is **synchronous**. A backend whose client is async wraps it (run the
coroutine to completion inside the method) rather than the protocol growing an
async variant: every caller in the engine is synchronous, and a second async
surface would double the contract for no caller that exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..maintain.drift import DriftReport
    from ..maintain.snapshot import Snapshot
    from ..transform.plans import EditKind, TransformPlan


class Document(str, Enum):
    """The addressable pieces of `.dex/` state, for locator lookups.

    One enum across all three tiers rather than one per tier: which documents a
    backend actually stores follows from the tier it implements, and an
    explore-only backend is simply never asked for ``SNAPSHOT`` or ``DRIFT``.
    """

    CACHE = "cache"
    SNAPSHOT = "snapshot"
    DRIFT = "drift"
    QUERY_LOG = "queries"
    SPEND_LOG = "spend"


@runtime_checkable
class ExploreStore(Protocol):
    """The state an exploring, profiling, querying host needs, and nothing more.

    The narrowest useful tier, and the one a host embedding dex for read-only
    sense-making should implement. It carries the exploration cache and the two
    ledgers; it does not carry the reconcile baseline or the transform plans.

    Backend state lives inside the store instance (class DI): it holds the
    connection, directory, or dictionaries the documents live in, so no caller
    needs to know where state lands or how it is addressed. Callers receive a
    store; they never construct one from a path.

    Every ``save_*`` returns an opaque **locator**: a display string identifying
    where the document went, for the envelope field an agent surfaces to a human.
    It is deliberately not a ``Path``, so a backend with no filesystem is
    representable.

    Three rules hold across every member here, because the shipped backends both
    honor them and a backend that does not will diverge in ways that surface much
    later as a data bug rather than an error:

    **Documents do not alias the caller's object.** What a caller saves and what
    a later ``load_*`` returns must be independent objects, in both directions.
    :class:`~.filesystem.FilesystemStore` gets this for free by serializing;
    :class:`~.memory.MemoryStore` deep-copies on the way in and on the way out
    specifically to match it. A backend holding a live in-process reference lets
    a mutation after a save silently rewrite history.

    **Documents are pydantic models.** They round-trip through
    ``model_validate_json`` and ``model_dump_json``. A backend that stores its own
    hand-rolled dict shape instead will diverge on the first schema change; use
    the model's own serialization and store the result.

    **A stored ``schema_version`` is not the store's to police.** Documents carry
    one and the engine reads it (the query firewall degrades on an old cache);
    a backend loads what it was given and lets the engine decide.
    """

    # --- documents ------------------------------------------------------------

    def load_cache(self) -> DexCache | None:
        """The stored exploration cache, or None when nothing has been stored.

        Raises rather than returning None when what is stored cannot be parsed: a
        corrupt cache is a different situation from an absent one, and silently
        treating the first as the second would re-profile a warehouse (and bill
        for it) as if nothing had ever been explored.
        """
        ...

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        """Store the cache, stamping ``provenance.updated_at`` when ``now`` is
        given. Returns the locator.

        Two contracts here that a backend has to honor deliberately.

        **The stamp lands on the caller's object too**, not only on the stored
        copy. The command layer reports the timestamp it just persisted, so a
        backend that stamps only what it writes makes the reported time and the
        stored time disagree.

        **This is a whole-document write, and it is atomic from the caller's
        view.** The query firewall resolves every table and column reference
        against the cache, so cache membership decides what a query may name and
        under whose PII policy; a half-written cache is a security-relevant state
        rather than merely an inconsistent one. A backend whose store caps
        document size (a document database typically does) chunks internally and
        presents one logical document. It does not get a per-dataset seam to
        write through, because that seam is exactly what would let a reader
        observe half a cache.
        """
        ...

    # --- ledgers --------------------------------------------------------------

    def append_query_log(self, entry: dict) -> None:
        """Append one `explore query` decision to the query ledger.

        Refusals are logged too: the ledger is the audit trail and the product
        signal for which probe shapes recur often enough to deserve promotion to
        a named command. SQL text only, never result values.

        The stored entry does not alias the caller's dict, for the same reason
        documents do not.
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
        the ledger, so callers sum their own connector's own unit. Use
        :func:`spend_total` rather than reimplementing the arithmetic, so every
        backend answers the budget question identically.

        **Those two are not the whole scoping story, and the third axis is the
        store itself.** The ledger is scoped to the store instance, so store
        granularity is ceiling granularity: one principal spanning two stores has
        two independent ceilings and nothing bounds their sum. Nothing warns about
        it and it errs permissive, so a host federating state per user should key
        stores exactly as it keys principals, and a host that splits stores for
        another reason (one per repo root, say) should know it has split the
        budget too.

        A malformed ledger entry is skipped rather than raised on. This is the
        opposite policy from :meth:`load_cache`, deliberately: an unreadable
        cache means dex does not know what it may query, while an unreadable
        ledger line means one spend record is lost, and refusing to run every
        subsequent billed command because of one bad line is the worse failure.

        **Concurrency.** The cost gate calls this and then calls
        :meth:`append_spend_log`, and that read-then-write is not atomic at the
        protocol level. The cumulative session ceiling therefore binds exactly
        when commands are serialized, which is the CLI's one-command-per-process
        shape, and under genuinely concurrent commands the overshoot is bounded
        by the sum of the concurrent estimates. A backend that can make the pair
        atomic for one key (a transaction, a conditional write) should, and a
        multi-tenant backend serving concurrent requests per tenant is where that
        stops being optional. The protocol does not grow a member for it, because
        an optional member no implementer can rely on is worse than a stated
        bound.
        """
        ...

    # --- locators -------------------------------------------------------------

    def locator(self, document: Document) -> str:
        """Where ``document`` lives, whether or not it has been stored yet. For
        the callers that report a location without writing one (a drift path in a
        detector envelope, a partial-write path in a budget-exhaustion error)."""
        ...


@runtime_checkable
class MaintainStore(ExploreStore, Protocol):
    """Adds the drift-detection state: the baseline and the last report.

    A host that reconciles a dbt project against a warehouse needs this tier. The
    snapshot is the known-good baseline every drift axis compares against, so a
    backend that cannot retain it across requests cannot support `maintain`.
    """

    def load_snapshot(self) -> Snapshot | None: ...

    def save_snapshot(self, snapshot: Snapshot) -> str: ...

    def load_drift(self) -> DriftReport | None: ...

    def save_drift(self, report: DriftReport) -> str: ...


@runtime_checkable
class Store(MaintainStore, Protocol):
    """The full contract: everything above, plus the transform plan store.

    The name every internal annotation used before the tiers existed, kept so a
    backend implementing all sixteen members is still just a ``Store``. The five
    plan members are the transform surface's alone; nothing in explore or
    maintain calls them, which is why a host that only explores can stop at
    :class:`ExploreStore` and still satisfy a declared contract rather than
    shipping five methods that raise.
    """

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

    def plan_locator(self, plan_id: str) -> str: ...


@dataclass(frozen=True)
class StoreContext:
    """Everything a backend gets to build itself from when dex constructs it.

    Two fields, because the backends disagree about what they are keyed by and
    the disagreement is not resolvable by picking a winner. ``FilesystemStore``
    takes a path, ``MemoryStore`` takes nothing, and a tenant-keyed backend
    serving several end users has no repository at all. A construction contract
    shaped around the path-shaped ones would leave the last group unbuildable,
    which is the group this seam exists for.

    ``repo_root`` is the directory dex was pointed at, or ``None`` when there is
    no repository in the picture. It is not the place scratch state has to land:
    a backend is free to ignore it, and most non-filesystem ones will.

    ``options`` is the backend's own non-secret coordinates, passed through
    verbatim from wherever the backend was named. dex does not interpret it, so
    the keys are the backend's to define and the backend's to validate: refuse an
    option you cannot honor rather than accepting and ignoring it, because a
    silently dropped setting is indistinguishable from a working one until
    something is stored in the wrong place.

    **No secret ever arrives here.** A backend named in ``.dex/config.yml`` is
    named in a committed file, so a password, key, token, or connection string in
    ``options`` would be a credential in version control. Read the credential the
    way the rest of the engine does, from the environment at construction time,
    or skip this contract entirely and hand the engine a store you built yourself
    (``DexEngine(store=...)``), which is the right shape for a host that already
    holds per-request credentials.
    """

    repo_root: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class StoreFactory(Protocol):
    """Anything that turns a :class:`StoreContext` into a store.

    One call, one argument, so the shapes a backend author would reach for all
    qualify without adapters: a module-level function, a class whose ``__init__``
    takes the context, or a classmethod such as
    :meth:`~.filesystem.FilesystemStore.from_context`.

    The returned store need only satisfy the tier its host uses; a factory
    building an :class:`ExploreStore` is a complete factory.

    **Do not validate a factory with ``isinstance``.** This protocol is
    ``runtime_checkable`` for symmetry with the store tiers, but a callable
    protocol can only check that ``__call__`` exists, which every callable
    satisfies. What is worth checking is the store that comes back, and the tiers
    are genuinely ``isinstance``-checkable, so build first and check the result
    against the tier the caller needs.
    """

    def __call__(self, context: StoreContext) -> ExploreStore: ...


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
