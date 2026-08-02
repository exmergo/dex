"""The seam as a third party meets it: a backend that is neither shipped backend.

Everything else in this directory tests the two backends dex ships, which share
an author with the protocol and so cannot show whether the contract is
implementable by someone reading only what is published. These build a backend
that is neither a directory nor a live in-process object, and drive it the way a
host actually would.

Three shapes matter here and are not covered anywhere else:

- **A store outliving the engine that wrote it.** A request-per-engine host builds
  a fresh engine per call and keeps state in its own datastore. Today's in-memory
  coverage runs every step through one engine, which would pass even if the cache
  never reached the store at all.
- **A partial implementation that is still complete.** A host that only explores
  implements ``ExploreStore`` and stops, and that has to be a satisfied contract
  rather than five methods that raise.
- **A backend with no repo root, built from a context.** Both shipped backends are
  constructed from a path or from nothing, so they cannot show whether the
  construction contract works for the backend class it was widened for: one keyed
  by a tenant, with no repository anywhere in the picture.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from exmergo_dex_core import (
    ConfigurationError,
    DexConfig,
    DexEngine,
    StoreRequiredError,
)
from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.storage import (
    Document,
    ExploreStore,
    Store,
    StoreContext,
    spend_total,
)
from exmergo_dex_core.storage.conformance import (
    ExploreStoreContract,
    SpendLockContract,
    StoreContract,
    StoreFactoryContract,
    a_cache,
)
from exmergo_dex_core.transform.plans import PlanNotFoundError, TransformPlan


class DocumentStore:
    """A document-database-shaped backend, serialized, keyed by tenant.

    Deliberately unlike both shipped backends: state lives in a process-wide
    registry rather than on disk or in the instance, documents are stored as JSON
    strings the way a document store holds them, and two instances built with the
    same tenant see the same state. That last property is the one a hosted
    deployment depends on and neither shipped backend exercises, because
    ``FilesystemStore`` gets it from the filesystem and ``MemoryStore`` does not
    have it at all.
    """

    _registry: ClassVar[dict[str, dict[str, object]]] = {}

    def __init__(self, tenant: str):
        self.tenant = tenant
        self._docs = self._registry.setdefault(tenant, {})

    @classmethod
    def reset(cls) -> None:
        cls._registry.clear()

    # --- documents ------------------------------------------------------------

    def load_cache(self) -> DexCache | None:
        raw = self._docs.get("cache")
        return None if raw is None else DexCache.model_validate_json(str(raw))

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        self._docs["cache"] = cache.model_dump_json()
        return self.locator(Document.CACHE)

    def load_snapshot(self):
        from exmergo_dex_core.maintain.snapshot import Snapshot

        raw = self._docs.get("snapshot")
        return None if raw is None else Snapshot.model_validate_json(str(raw))

    def save_snapshot(self, snapshot) -> str:
        self._docs["snapshot"] = snapshot.model_dump_json()
        return self.locator(Document.SNAPSHOT)

    def load_drift(self):
        from exmergo_dex_core.maintain.drift import DriftReport

        raw = self._docs.get("drift")
        return None if raw is None else DriftReport.model_validate_json(str(raw))

    def save_drift(self, report) -> str:
        self._docs["drift"] = report.model_dump_json()
        return self.locator(Document.DRIFT)

    # --- ledgers --------------------------------------------------------------

    def _ledger(self, name: str) -> list[str]:
        return self._docs.setdefault(name, [])  # type: ignore[return-value]

    def append_query_log(self, entry: dict) -> None:
        self._ledger("queries").append(json.dumps(entry))

    def append_spend_log(self, entry: dict) -> None:
        self._ledger("spend").append(json.dumps(entry))

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        entries = []
        for raw in self._ledger("spend"):
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        return spend_total(entries, cutoff_iso, field=field, connector=connector)

    # --- plans ----------------------------------------------------------------

    def _plans(self) -> dict[str, str]:
        return self._docs.setdefault("plans", {})  # type: ignore[return-value]

    def save_plan(self, plan: TransformPlan) -> str:
        self._plans()[plan.plan_id] = plan.model_dump_json()
        return self.plan_locator(plan.plan_id)

    def load_plan(self, plan_id: str) -> TransformPlan:
        raw = self._plans().get(plan_id)
        if raw is None:
            raise PlanNotFoundError(f"no plan '{plan_id}' for tenant {self.tenant}")
        return TransformPlan.model_validate_json(raw)

    def list_plans(self) -> list[TransformPlan]:
        plans = [TransformPlan.model_validate_json(r) for r in self._plans().values()]
        return sorted(plans, key=lambda p: p.created_at, reverse=True)

    def latest_plan(self, kind=None) -> TransformPlan | None:
        candidates = [
            p
            for p in self.list_plans()
            if p.applied_at is None
            and (kind is None or all(e.kind is kind for e in p.edits))
        ]
        return max(candidates, key=lambda p: p.created_at, default=None)

    # --- locators -------------------------------------------------------------

    def locator(self, document: Document) -> str:
        return f"docstore://{self.tenant}/{document.value}"

    def plan_locator(self, plan_id: str) -> str:
        return f"docstore://{self.tenant}/plans/{plan_id}"


class DurableDocumentStore(DocumentStore):
    """The same backend, never reset: the shape every hosted deployment has.

    One difference from :class:`DocumentStore`, and it is the entire point: this
    registry is deliberately left out of ``_clean_registries``, so what one
    assertion writes under a key is still there for the next one. That is what a
    Postgres-backed or document-database-backed store does between requests, and
    the suite has to accommodate it without asking for a reset hook.

    It exists because the reset is what hid the defect. Every backend the contract
    was ever run against reset itself (this module through the autouse fixture, the
    out-of-tree backend by popping its own state), so a suite that drove all 34
    assertions through one literal key looked correct here and gave a real
    implementer nine failures phrased as bugs in their own backend. If key reuse
    ever comes back, this class fails and the shipped backends do not.
    """

    _registry: ClassVar[dict[str, dict[str, object]]] = {}


class ExploreOnlyStore:
    """The narrow tier, with the five plan members genuinely absent.

    Not a subclass of :class:`DocumentStore` with methods deleted: written from
    the ``ExploreStore`` protocol alone, which is what a host that only explores
    would actually write.
    """

    _registry: ClassVar[dict[str, dict[str, object]]] = {}

    def __init__(self, tenant: str):
        self.tenant = tenant
        self._docs = self._registry.setdefault(tenant, {})

    @classmethod
    def reset(cls) -> None:
        cls._registry.clear()

    def load_cache(self) -> DexCache | None:
        raw = self._docs.get("cache")
        return None if raw is None else DexCache.model_validate_json(str(raw))

    def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
        if now is not None:
            cache.provenance.updated_at = now.isoformat()
        self._docs["cache"] = cache.model_dump_json()
        return self.locator(Document.CACHE)

    def append_query_log(self, entry: dict) -> None:
        self._docs.setdefault("queries", []).append(json.dumps(entry))  # type: ignore[union-attr]

    def append_spend_log(self, entry: dict) -> None:
        self._docs.setdefault("spend", []).append(json.dumps(entry))  # type: ignore[union-attr]

    def spend_since(
        self,
        cutoff_iso: str,
        *,
        field: str = "billed_bytes",
        connector: str | None = None,
    ) -> float:
        raws = self._docs.setdefault("spend", [])
        entries = [json.loads(r) for r in raws]  # type: ignore[union-attr]
        return spend_total(entries, cutoff_iso, field=field, connector=connector)

    def locator(self, document: Document) -> str:
        return f"explore-only://{self.tenant}/{document.value}"


def document_store_factory(context: StoreContext) -> DocumentStore:
    """The function shape of the construction contract, keyed by nothing on disk.

    What a third party would publish beside the backend so it can be named in
    configuration. It reads its own coordinate out of ``options`` and refuses when
    it is missing, because a backend that guessed a tenant would put one tenant's
    exploration cache under another's key.
    """

    tenant = context.options.get("tenant")
    if not tenant:
        raise ConfigurationError(
            "this backend is keyed by tenant and the context carries none; set "
            "cache.options.tenant"
        )
    return DocumentStore(tenant=str(tenant))


class ContextKeyedExploreStore(ExploreOnlyStore):
    """The class shape: a store whose ``__init__`` is itself the factory.

    The second way a dotted path can resolve, and the reason the contract is a
    callable rather than a named classmethod: a backend written this way needs no
    adapter and no extra method to be constructable.
    """

    def __init__(self, context: StoreContext):
        super().__init__(tenant=str(context.options["tenant"]))


@pytest.fixture(autouse=True)
def _clean_registries():
    # DurableDocumentStore is deliberately absent, and must stay absent: resetting
    # it would turn the one backend here that models a hosted deployment back into
    # one that quietly starts clean, which is exactly how the key-reuse defect went
    # unnoticed. The hand-written flow tests below keep the reset they do want.
    DocumentStore.reset()
    ExploreOnlyStore.reset()
    yield
    DocumentStore.reset()
    ExploreOnlyStore.reset()


# --- the shipped contract, run against a backend dex does not ship -------------


class TestDocumentStoreConformance(StoreContract):
    def make_store(self, key: str) -> DocumentStore:
        return DocumentStore(tenant=key)


class TestExploreOnlyStoreConformance(ExploreStoreContract):
    def make_store(self, key: str) -> ExploreOnlyStore:
        return ExploreOnlyStore(tenant=key)


# --- the same contract, run through the construction seam ----------------------
#
# Composing the factory contract in front re-runs every behavioral and isolation
# assertion above against stores built the way dex would build them, which is what
# keeps "the suite is green" meaning correct *and* constructable.


class TestDocumentStoreConstruction(StoreFactoryContract, StoreContract):
    tier = Store

    def build(self, context: StoreContext) -> DocumentStore:
        return document_store_factory(context)

    def context_for(self, key: str) -> StoreContext:
        # No repo root anywhere: the case the rejected `resolve(name)(repo_root)`
        # contract could not express at all.
        return StoreContext(options={"tenant": key})


class TestContextKeyedExploreStoreConstruction(
    StoreFactoryContract, ExploreStoreContract
):
    def build(self, context: StoreContext) -> ContextKeyedExploreStore:
        return ContextKeyedExploreStore(context)

    def context_for(self, key: str) -> StoreContext:
        return StoreContext(options={"tenant": key})


# --- the same contract, run against a backend that never resets ----------------
#
# The contract classes above all get a clean registry per assertion from
# _clean_registries. These two do not, so they are the only in-repo run that proves
# what a hosted implementer actually needs: that the suite hands out a fresh key per
# assertion, and that a backend sharing state across instances built from one key
# therefore passes without a reset hook or a fixture of its own.


class TestDurableDocumentStoreConformance(StoreContract):
    def make_store(self, key: str) -> DurableDocumentStore:
        return DurableDocumentStore(tenant=key)


class TestDurableDocumentStoreConstruction(StoreFactoryContract, StoreContract):
    """And through the construction seam, which is where key reuse bit hardest.

    `test_two_contexts_build_stores_that_share_nothing` asserts a freshly built
    store has no cache. Under a shared literal key an earlier assertion had already
    put one there, so this composition passed only on the order pytest happened to
    collect it in.
    """

    tier = Store

    def build(self, context: StoreContext) -> DurableDocumentStore:
        # No refusal path here: what a factory owes a context missing its
        # coordinate is document_store_factory's job and is tested against it.
        return DurableDocumentStore(tenant=str(context.options["tenant"]))

    def context_for(self, key: str) -> StoreContext:
        return StoreContext(options={"tenant": key})


def test_the_durable_backend_really_does_share_state_across_instances():
    """Guards the guard.

    Everything the two classes above prove rests on this backend being durable. If
    it ever starts resetting (added to `_clean_registries`, or given per-instance
    state), those runs keep passing while testing nothing about a hosted backend,
    which is the failure mode this whole change exists to close.
    """

    DurableDocumentStore("durability-check").save_cache(a_cache())
    assert DurableDocumentStore("durability-check").load_cache() is not None, (
        "DurableDocumentStore stopped sharing state per key, so the conformance "
        "runs above no longer cover a backend that outlives one assertion"
    )


# --- the tiers are what they claim to be --------------------------------------


def test_a_broken_backend_fails_with_the_rule_it_broke_not_a_bare_compare():
    """The suite is documentation for someone who cannot read this source.

    In-repo a bare `assert stored == original` is fine, because a failure sends
    you to the source and the source is right here. Shipped, the assertion text is
    all an outside implementer gets, and the no-aliasing rule is the worst case:
    it fails silently and late, and presents as data corruption rather than as a
    protocol violation. So the message has to name the contract.
    """

    class AliasingStore(ExploreOnlyStore):
        """Returns the live stored object, the mistake an in-process cache invites."""

        def save_cache(self, cache: DexCache, *, now: datetime | None = None) -> str:
            if now is not None:
                cache.provenance.updated_at = now.isoformat()
            self._docs["live"] = cache  # the bug: no serialize, no copy
            return self.locator(Document.CACHE)

        def load_cache(self) -> DexCache | None:
            return self._docs.get("live")  # type: ignore[return-value]

    contract = ExploreStoreContract()
    contract.make_store = lambda key: AliasingStore(key)  # type: ignore[method-assign]
    store = contract.make_store("broken")

    with pytest.raises(AssertionError) as failure:
        contract.test_a_stored_document_is_not_aliased_to_the_callers_object(store)

    message = str(failure.value)
    assert "must not alias the caller's object" in message
    # And it says what to do about it, not merely that something differed.
    assert "Serialize on write, or deep-copy" in message


def test_a_factory_that_ignores_its_context_fails_with_the_rule_it_broke():
    """The construction mistake that voids every isolation guarantee downstream.

    A factory that drops the part of the context keying it builds one shared store
    for every tenant. Nothing about that looks wrong at the call site, and the
    behavioral suite passes, because each store in isolation is correct; what
    fails is that they are all the same store. So the message has to say which
    part of the contract broke, not merely that a cache was not None.
    """

    contract = StoreFactoryContract()
    contract.build = lambda context: DocumentStore("always-the-same")  # type: ignore[method-assign]
    contract.context_for = lambda key: StoreContext(options={"tenant": key})  # type: ignore[method-assign]

    with pytest.raises(AssertionError) as failure:
        contract.test_two_contexts_build_stores_that_share_nothing()

    message = str(failure.value)
    assert "ignoring the part of the context that keys it" in message


def test_a_factory_that_undershoots_its_declared_tier_names_the_tier():
    # The other construction failure: the backend is fine and the factory works,
    # but it hands back a narrower store than the config selecting it expects. dex
    # refuses at resolution, so this has to fail here rather than several commands
    # later on a missing attribute.
    contract = StoreFactoryContract()
    contract.tier = Store
    contract.build = lambda context: ExploreOnlyStore("t")  # type: ignore[method-assign]
    contract.context_for = lambda key: StoreContext(options={"tenant": key})  # type: ignore[method-assign]

    with pytest.raises(AssertionError) as failure:
        contract.test_the_factory_builds_a_store_of_the_declared_tier()

    message = str(failure.value)
    assert "ExploreOnlyStore" in message and "Store" in message
    assert "unusable from configuration" in message


def test_a_lock_that_does_not_exclude_fails_with_what_it_costs():
    """The optional capability's worst failure, and the one easiest to ship.

    A `spend_lock` returning `nullcontext` satisfies the type, satisfies the
    re-entrancy assertion, and never blocks anything. Everything downstream then
    looks correct, because each command in isolation is correct; what fails is
    that the ceiling stopped binding across them. So the message has to say what
    the lock was for rather than that two threads overlapped.
    """

    from contextlib import nullcontext

    class UnlockedStore(ExploreOnlyStore):
        def spend_lock(self, *, timeout: float = 30.0):
            return nullcontext()

    contract = SpendLockContract()
    contract.make_store = lambda key: UnlockedStore(key)  # type: ignore[method-assign]
    contract.lock_timeout = 2.0

    with pytest.raises(AssertionError) as failure:
        contract.test_the_lock_excludes_a_second_holder(contract.make_store("broken"))

    message = str(failure.value)
    assert "does not exclude" in message
    assert "admitted against the same headroom" in message


def test_a_backend_wide_lock_fails_by_naming_the_scope_it_should_have():
    """The subtler failure: a lock that works and is scoped to the whole backend.

    It excludes, it is re-entrant, and it makes every tenant wait on every other
    tenant's billed commands. No single-tenant test can see it, which is why the
    contract carries a two-key assertion at all.
    """

    shared = threading.RLock()

    class GloballyLockedStore(ExploreOnlyStore):
        def spend_lock(self, *, timeout: float = 30.0):
            return shared

    contract = SpendLockContract()
    contract.make_store = lambda key: GloballyLockedStore(key)  # type: ignore[method-assign]
    contract.lock_timeout = 2.0

    with pytest.raises(AssertionError) as failure:
        contract.test_two_keys_do_not_wait_on_each_other()

    assert "scoped to the backend rather than to the ledger" in str(failure.value)


def test_a_full_backend_satisfies_every_tier():
    store = DocumentStore("t")
    assert isinstance(store, ExploreStore)
    assert isinstance(store, Store)


def test_an_explore_only_backend_satisfies_the_explore_tier_and_no_more():
    # The point of the tiers: this is a complete implementation of a declared
    # contract, not a partial implementation of a wider one.
    store = ExploreOnlyStore("t")
    assert isinstance(store, ExploreStore)
    assert not isinstance(store, Store)


# --- the request-per-engine shape ----------------------------------------------


def test_state_written_by_one_engine_is_visible_to_the_next(duckdb_file: Path):
    # The shape a host serving requests actually has: an engine per call, state in
    # the host's own datastore. Nothing here shares an engine, so a cache that
    # never reached the store would fail rather than pass on a live object.
    config = DexConfig(connector="duckdb")

    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=DocumentStore("tenant-a"),
    ) as first:
        first.map()

    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=DocumentStore("tenant-a"),
    ) as second:
        # The query firewall resolves this table against the cache the *previous*
        # engine wrote, which is the whole reason a durable store is required and
        # not merely convenient.
        result = second.query("select count(*) as n from customers")

    assert result.cells[0][0] == 2


def test_one_tenants_cache_is_invisible_to_another(duckdb_file: Path):
    config = DexConfig(connector="duckdb")
    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=DocumentStore("tenant-a"),
    ) as owner:
        owner.map()

    other = DocumentStore("tenant-b")
    assert other.load_cache() is None
    # And the firewall refuses for the other tenant, because cache membership is
    # what decides that a query may name this table at all.
    with (
        DexEngine(
            connector="duckdb",
            path=str(duckdb_file),
            config=config,
            store=other,
        ) as stranger,
        pytest.raises(Exception),
    ):
        stranger.query("select count(*) as n from customers")


def test_a_backend_built_from_a_context_with_no_repo_root_drives_a_flow(
    duckdb_file: Path,
):
    """The acceptance case for the construction contract, end to end.

    Nothing here has a repo root: the store is built from a context carrying only
    a tenant, which is the construction a path-shaped contract cannot express. And
    it is built twice, once per engine, so the flow only works if the second
    construction lands on the state the first one wrote. That is the property a
    hosted deployment needs and the reason the contract is keyed by the backend's
    own coordinates rather than by a directory.
    """

    config = DexConfig(connector="duckdb")
    context = StoreContext(options={"tenant": "tenant-a"})

    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=document_store_factory(context),
    ) as first:
        first.map()

    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=document_store_factory(context),
    ) as second:
        result = second.query("select count(*) as n from customers")

    assert result.cells[0][0] == 2

    # And a different tenant's context builds a store that has never seen it, so
    # the firewall has nothing to resolve the table against.
    stranger_store = document_store_factory(StoreContext(options={"tenant": "other"}))
    assert stranger_store.load_cache() is None


def test_a_factory_refuses_a_context_missing_the_coordinate_it_is_keyed_by():
    # The refusal a backend owes: guessing a tenant would file one tenant's
    # exploration cache under another's key, which is the failure the whole seam
    # exists to prevent.
    with pytest.raises(ConfigurationError) as refusal:
        document_store_factory(StoreContext(repo_root="/somewhere"))
    assert "tenant" in str(refusal.value)


def test_an_explore_only_store_drives_a_full_explore_flow(duckdb_file: Path):
    config = DexConfig(connector="duckdb")
    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=ExploreOnlyStore("tenant-a"),
    ) as first:
        first.map()

    with DexEngine(
        connector="duckdb",
        path=str(duckdb_file),
        config=config,
        store=ExploreOnlyStore("tenant-a"),
    ) as second:
        result = second.query("select count(*) as n from orders")

    assert result.cells[0][0] == 3


def test_transform_refuses_an_explore_only_store_by_naming_the_tier(tmp_path: Path):
    engine = DexEngine(
        connector="duckdb",
        path="x.duckdb",
        config=DexConfig(connector="duckdb"),
        store=ExploreOnlyStore("tenant-a"),
        repo_root=str(tmp_path),
    )
    with pytest.raises(StoreRequiredError) as refusal:
        engine.plans()
    message = str(refusal.value)
    # The refusal has to name the tier and the missing members, or an implementer
    # reads it as "storage is broken" rather than "I implemented the narrow one".
    assert "explore tier" in message
    assert "list_plans" in message
    assert "ExploreStore" in message
