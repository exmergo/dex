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

**Serializing the spend admission is a separate, optional capability**,
:class:`SpendLock`, and a backend serving concurrent requests wants it: without
one the cumulative session ceiling does not bind across overlapping billed
commands, and dex warns on every such command rather than letting the ceiling
look enforced.

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
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from ..errors import PrerequisiteError

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..maintain.drift import DriftReport
    from ..maintain.snapshot import Snapshot
    from ..transform.plans import EditKind, TransformPlan


class CacheUnreadableError(PrerequisiteError):
    """A cache exists and this engine cannot read it.

    The counterpart of :class:`~.explore.commands.CacheRequiredError`, drawing
    the same distinction :class:`~.maintain.commands.BaselineUnreadableError`
    draws against :class:`~.maintain.commands.NoBaselineError`. Both remediate
    the same way, so the status is the same, but "nothing has been explored
    here" and "what was explored will not parse" are different facts about the
    deployment, and only one of them suggests something went wrong. A host that
    wants to page on the second and not the first can tell them apart without
    matching on prose.

    No ``schema_version`` attribute, unlike the baseline's, and the asymmetry is
    already reasoned out in ``maintain/snapshot.py``: the cache's version drives
    a ``<`` comparison in the query firewall that *degrades* (an old cache is
    still usable, so the firewall widens taint and carries on), where the
    baseline's is a membership test that *refuses*. A degrading version has no
    refusal for this class to carry, so the only state that reaches it is a
    document that did not parse. If a supported-set gate is ever added here,
    that is when the attribute earns its place.

    Lives here rather than beside ``CacheRequiredError`` because three packages
    load a cache (``explore``, ``maintain``, ``transform``) and none of them
    imports ``explore.commands`` today. Storage is the neutral home they already
    share, and it is where the contract below asks for the raise this classifies.
    """


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

        **Raise a ``ValueError``** (pydantic's ``ValidationError`` is one, and is
        what the shipped backends raise by validating the model). That is the
        convention the engine reads as "this document will not parse", and it is
        what ``load_snapshot`` is caught on to report a corrupt baseline as a
        prerequisite failure rather than as a bad request. :func:`readable_cache`
        is the equivalent wrapper for this load, and every engine call site goes
        through it, so a ``ValueError`` raised here is now classified as
        :class:`CacheUnreadableError` rather than reaching the CLI catch-all and
        being reported as a bad request.
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

        **A backend stores entries; it does not interpret them.** Entries carry
        an ``entry`` kind (``reservation``, ``settlement``, ``release``) and a
        ``reservation_id`` tying the three together, because the cumulative
        ceiling holds headroom for a command that has been admitted but has not
        finished paying. A release carries a negative magnitude, which is the
        one thing worth knowing here: **a backend must not assume the magnitude
        is positive**, and one that clamps or filters would leak held headroom
        for the rest of the UTC day. Sum what you are given, the way
        :func:`spend_total` does.
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

        **What the total means depends on when you read it.** The cost gate
        books a reservation at the estimate when it admits a command and
        releases it when the command settles, so a day already finished sums to
        actual spend, exactly as it always has, while a day with commands still
        running sums to actual spend plus the headroom those commands hold. The
        second is not an accounting error; it is the number a concurrent command
        has to see for the ceiling to bind.

        **Concurrency, and how a backend opts into the guarantee.** The gate
        reads this and then appends, and that read-then-write has to be atomic
        for the cumulative ceiling to bind. Implement :class:`SpendLock` and the
        gate serializes the pair through it. Without one the gate still reserves,
        which narrows the window from the duration of a warehouse query to the
        few microseconds between the read and the append, and it **warns on
        every billed command** that the cumulative ceiling is not enforced on
        this backend. A guard that is narrower than it looks is worse than one
        that is absent, so the narrowing is reported rather than assumed.

        The capability is deliberately separate from the tiers: a store is
        useful without it, and the reason an optional member is tolerable here
        is that dex detects its absence and says so, rather than relying on
        every implementer to have read a paragraph.

        **This call is allowed to fail, and where it fails decides what
        happens.** A ledger on a network is a ledger that can be unreachable, so
        raising is a legitimate outcome and a backend should not swallow one into
        a zero: a zero is a claim that nothing has been spent today, and a guard
        that admits work on a fabricated zero is the failure this ledger exists
        to prevent. Only billed admission is entitled to fail closed on it, and
        it does, as a named refusal that says nothing ran. A command that cannot
        spend never reaches this call, and settlement tolerates a failure rather
        than turning a command that already ran into a refusal, so an outage
        costs the day's total on one envelope and no correctness.
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

    def load_snapshot(self) -> Snapshot | None:
        """The stored baseline, or None when nothing has been stored.

        Same contract as ``load_cache``, and for a sharper reason: raise a
        ``ValueError`` on a document that cannot be parsed. The baseline is what
        every drift axis measures against, so returning None for a corrupt one
        would report "no baseline yet" for a deployment that has one, and the
        remedy dex names (`maintain snapshot`) would silently accept current
        state as known-good. `maintain` catches this and refuses as a
        prerequisite failure naming the fix, which it cannot do for a backend
        that raises something else.
        """
        ...

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


@runtime_checkable
class SpendLock(Protocol):
    """Optional: serialize the spend admission so the cumulative ceiling binds.

    One method, on top of any tier. A store that has it lets the cost gate make
    "read the day's spend, decide, book the headroom" one indivisible step, which
    is what stops two concurrent billed commands being admitted against the same
    headroom. A store without it still works, and every billed command run
    against it warns that the cumulative ceiling is advisory there.

    **The scope to lock is the ledger this store reads, and nothing wider.** The
    gate holds it for the few microseconds around the decision and never across
    a warehouse query, so locking the whole backend would serialize commands that
    have no reason to wait for each other. Two stores keyed differently must not
    contend: that is the same isolation the tier contracts already require, and a
    lock is the one place it is easy to forget, since a single global mutex looks
    correct in a single-tenant test.

    It must be **reentrant for one holder**. The gate settles from more than one
    funnel and a settle inside an already-held section is expected rather than a
    deadlock to design around.

    Raise the builtin :class:`TimeoutError` when ``timeout`` elapses. The gate
    turns that into a named refusal carrying the cost; the builtin is what the
    protocol asks for so a backend needs no import from dex to satisfy it.
    """

    def spend_lock(self, *, timeout: float = 30.0) -> AbstractContextManager[None]:
        """Hold the spend-admission lock for this store's ledger."""
        ...


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


#: Appended wherever the remedy is a fresh cache. Unlike `maintain snapshot`,
#: which is free on every connector, `explore map` re-profiles the warehouse and
#: therefore BILLS. An operator deciding between investigating the stored
#: document and replacing it needs that said before they run it, for the same
#: reason the baseline's remedy says what a replacement silently absorbs.
_REMAP_BILLS = (
    "note that `explore map` re-profiles the warehouse and bills for it, so a "
    "corrupt document worth investigating is worth copying aside first"
)


def readable_cache(store: ExploreStore) -> DexCache | None:
    """The stored cache, or a refusal naming the command that rebuilds one.

    :meth:`ExploreStore.load_cache` raises on a document it cannot parse, and
    pydantic's ``ValidationError`` subclasses ``ValueError``, so an unreadable
    cache fell through to the CLI catch-all and was classified as a *request*
    error. That tells an operator they typed something wrong when the fix is
    `explore map`, and it is exactly the retry-versus-stop distinction
    :class:`~..errors.PrerequisiteError` exists to carry. The ``load_cache``
    contract above already asks backends to raise a ``ValueError`` "so the load
    is classifiable when it gets one, not because it is classified today"; this
    is the wrapper it was written for.

    **It classifies, it does not require**, and the asymmetry with
    :func:`~..maintain.commands._require_baseline` is deliberate rather than an
    omission. Every ``load_snapshot`` caller needs a baseline, so that helper can
    refuse on ``None``. Absence is *legal* at most cache call sites: `explore
    profile`, `explore relationships` and `explore map` read a prior cache only
    to merge pre-run state and a first run has none, `maintain snapshot` falls
    back to a metadata capture, and ``_baseline_warnings`` merely skips a
    warning. A ``_require_cache`` refusing on ``None`` would break five commands.
    So ``None`` is returned unchanged, every caller keeps the absence policy it
    already had, and only the unparseable case gains a class.
    """

    try:
        return store.load_cache()
    except ValueError as exc:
        # ValueError rather than pydantic's ValidationError, which it subclasses.
        # The contract above requires a backend to raise on a document it cannot
        # parse and has never named the exception; the store protocol is public,
        # and a backend deserializing its own rows raises whatever `json` or its
        # driver raises. Catching only pydantic's error would leave every
        # third-party backend reproducing the exact defect this function fixes,
        # which is the kind of gap that looks fixed from inside this repo.
        detail = (
            f"{exc.error_count()} validation error(s)"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise CacheUnreadableError(
            f"the stored exploration cache could not be read ({detail}). It is "
            "either corrupt or written by a newer dex whose shape this engine "
            "does not know. Re-run `explore map` to rebuild it, or move to the "
            f"dex that wrote it if that is what happened; {_REMAP_BILLS}"
        ) from exc


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

    A plain sum, negatives included: a release is a reservation entry with the
    sign flipped, so the three entry kinds net out to actual spend with no
    branch on kind here. That is why adding reservations did not change what any
    existing backend's ``spend_since`` returns for a settled day.
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
