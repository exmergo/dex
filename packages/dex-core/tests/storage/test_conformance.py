"""The seam as a third party meets it: a backend that is neither shipped backend.

Everything else in this directory tests the two backends dex ships, which share
an author with the protocol and so cannot show whether the contract is
implementable by someone reading only what is published. These build a backend
that is neither a directory nor a live in-process object, and drive it the way a
host actually would.

Two shapes matter here and are not covered anywhere else:

- **A store outliving the engine that wrote it.** A request-per-engine host builds
  a fresh engine per call and keeps state in its own datastore. Today's in-memory
  coverage runs every step through one engine, which would pass even if the cache
  never reached the store at all.
- **A partial implementation that is still complete.** A host that only explores
  implements ``ExploreStore`` and stops, and that has to be a satisfied contract
  rather than five methods that raise.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from exmergo_dex_core import DexConfig, DexEngine, StoreRequiredError
from exmergo_dex_core.cache import DexCache
from exmergo_dex_core.storage import Document, ExploreStore, Store, spend_total
from exmergo_dex_core.storage.conformance import (
    ExploreStoreContract,
    StoreContract,
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


@pytest.fixture(autouse=True)
def _clean_registries():
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
