"""The executable contract a storage backend has to satisfy, shipped for reuse.

The rules a backend owes its callers are stated on the protocol members in
:mod:`.base`. This module is the same rules as assertions, packaged so a backend
living outside this distribution can run them in its own test suite:

    from exmergo_dex_core.storage.conformance import StoreContract

    class TestMyStore(StoreContract):
        def make_store(self, key):
            return MyStore(tenant=key)

That is the whole integration. pytest collects the inherited ``test_*`` methods
and runs the entire contract against your backend.

**Pick the class that matches the tier you implement**, mirroring the three
protocols exactly: :class:`ExploreStoreContract` for a backend that only carries
the cache and the ledgers, :class:`MaintainStoreContract` when it also carries the
snapshot and the drift report, :class:`StoreContract` when it carries the
transform plans too. Subclassing a wider contract than your backend implements is
how you find out you have not finished, so the narrow classes are a feature rather
than a convenience.

``make_store(key)`` must return a store that shares nothing with a store built
from a different key. That is what the isolation assertions check, and it is the
property a host federating state per end user is actually relying on: two tenants
in one process must not be able to see each other's cache, ledgers, or plans.
Whether ``key`` is a directory, a tenant id, or a table prefix is the backend's
business.

**The suite never hands the same key to two assertions**, so a backend whose
instances share state per key, which is every durable one, starts each assertion on
a clean slate and needs no reset hook. See :meth:`ExploreStoreContract.make_store`
for the whole of what that guarantees and what it does not.

**If your backend is meant to be named in configuration rather than handed to the
engine as an instance**, mix :class:`StoreFactoryContract` in as well. It routes
``make_store`` through your own factory, so every assertion above then runs
against a store built the way dex would build it, and "the suite is green" keeps
meaning the backend is both correct and constructable rather than only the first.

This module imports pytest, so it is deliberately not imported by
``exmergo_dex_core.storage``: a bare ``import exmergo_dex_core`` must not require
a test framework. Install the ``[storage-conformance]`` extra to get what running
the suite needs: pytest, and the dialect engine the plan-tier assertions reach
through :func:`a_plan`. The explore tier needs only pytest, and a packaging test
keeps it that way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from typing import Any
from uuid import uuid4

import pytest

from ..cache import CacheProvenance, Dataset, DexCache
from ..maintain.drift import AxisResult, DriftFinding, DriftReport
from ..maintain.snapshot import Snapshot, WarehouseBaseline
from .base import Document, ExploreStore, StoreContext

__all__ = [
    "ExploreStoreContract",
    "MaintainStoreContract",
    "StoreContract",
    "StoreFactoryContract",
]

#: Unique per process, so two runs of the suite against one durable backend do not
#: land in the same namespace. A counter alone would be enough for a backend that
#: only outlives an assertion; a backend that outlives the *process*, which is the
#: interesting case, would inherit the previous run's writes from a key it could
#: predict. Also keeps parallel workers apart, since each is its own process.
_RUN_ID = uuid4().hex[:8]
_KEY_SEQUENCE = count()


def a_cache(identifier: str = "db.main.orders") -> DexCache:
    """A minimal valid cache. Exposed so a backend's own tests can reuse it."""

    return DexCache(
        datasets=[Dataset(identifier=identifier, row_count=3)],
        provenance=CacheProvenance(connector="duckdb"),
    )


def a_snapshot(created_at: str = "2026-07-03T10:00:00+00:00") -> Snapshot:
    return Snapshot(
        created_at=created_at,
        connector="duckdb",
        warehouse=WarehouseBaseline(datasets=[]),
        warehouse_from="metadata",
    )


def a_drift_report() -> DriftReport:
    return DriftReport(
        connector="duckdb",
        snapshot_created_at="2026-07-03T10:00:00+00:00",
        axes={
            "schema": AxisResult(
                run_at="2026-07-03T11:00:00+00:00",
                findings=[
                    DriftFinding(
                        axis="schema",
                        code="column_added",
                        identifier="db.main.orders",
                        column="discount",
                        detail="a new column appeared in the source",
                    )
                ],
            )
        },
    )


def a_plan(plan_id: str, created_at: str, *, kind: Any | None = None) -> Any:
    """A minimal valid transform plan.

    Imports lazily: the plan model reaches the SQL guard, and so the dialect
    engine, which a host implementing only the explore tier has no reason to have
    installed. Keeping this out of module scope is what lets
    :class:`ExploreStoreContract` run on a bare install.
    """

    from ..transform.plans import EditKind, PlanEdit, TransformPlan

    return TransformPlan(
        plan_id=plan_id,
        created_at=created_at,
        intent="add a model",
        project_dir="analytics",
        edits=[
            PlanEdit(
                path="models/staging/stg_orders.sql",
                kind=kind if kind is not None else EditKind.MODEL_SQL,
                new_content="select 1 as id\n",
            )
        ],
    )


class _KeyedContract:
    """Gives every assertion its own keyspace, for both contract families.

    A durable backend is one where two instances built from the same key see the
    same state. That is not an implementation detail to be worked around, it is the
    defining property of every hosted backend and the reason the seam exists. So the
    suite cannot reuse a key: driving 34 assertions through one literal made each
    inherit the previous one's writes, and the resulting failures ("a cache that was
    just saved must load back") read as bugs in the backend rather than as pollution
    from the harness.

    Namespacing here rather than asking implementers for a reset hook keeps the
    integration at one method, and costs a backend that *does* reset per call
    nothing at all: it simply receives different strings.
    """

    #: Replaced per assertion by the autouse fixture below. The class-level default
    #: keeps a contract instantiated by hand, outside pytest, usable; the meta-tests
    #: in this repository's suite drive the assertions that way to check their
    #: failure messages.
    _namespace: str = _RUN_ID

    @pytest.fixture(autouse=True)
    def _namespaced_keys(self) -> None:
        self._namespace = f"{_RUN_ID}-{next(_KEY_SEQUENCE):02d}"

    def key_for(self, label: str) -> str:
        """The key this assertion uses for ``label``, unique to it and to this run.

        ``label`` survives into the key so a backend that files state under it (a
        directory, a table prefix) stays readable while you are debugging a failure.
        """

        return f"{label}-{self._namespace}"

    def store_for(self, label: str) -> Any:
        return self.make_store(self.key_for(label))

    # Declared so `store_for` has something to call and a type checker can see it.
    # Both families override it: the tier contracts document it as the one hook an
    # implementer provides, `StoreFactoryContract` routes it through the factory.
    def make_store(self, key: str) -> Any:  # pragma: no cover - always overridden
        raise NotImplementedError(
            "a conformance subclass must implement make_store(key) -> Store"
        )


class ExploreStoreContract(_KeyedContract):
    """What every backend owes, whatever tier it implements.

    The exploration cache, the two ledgers, and the locators: the state an
    exploring, profiling, querying host needs.
    """

    # --- the one hook an implementer provides ---------------------------------

    def make_store(self, key: str) -> Any:
        """Return a store bound to ``key``, sharing nothing with another key.

        Called with an arbitrary short string. A filesystem backend can treat it
        as a directory name, a database backend as a tenant identifier. It is
        called more than once per test class, so it must be cheap and must not
        assume it is building the only store in the process.

        **The suite never reuses a key**, within a run or across runs. Every
        assertion gets its own, so you do not need a reset hook, a truncate step, or
        a fixture of your own to keep one assertion's writes out of the next.

        What that leaves you free to do is the point: two calls with the *same* key
        may return two views of one shared state. That is what a durable backend is,
        and what a hosted deployment depends on. The suite requires the opposite only
        for two *different* keys, which is what the isolation assertions check; it
        assumes nothing at all about the same key twice.

        Nothing is cleaned up when the run ends. A durable backend therefore
        accumulates one namespace per run, which is yours to prune; the suite cannot
        do it without a teardown hook that would be a second thing to implement, and
        get wrong, for a guarantee it does not need.
        """

        raise NotImplementedError(
            "a conformance subclass must implement make_store(key) -> Store"
        )

    @pytest.fixture
    def store(self, _namespaced_keys: None) -> Any:
        # Depends on the keyspace rather than trusting autouse ordering: this is the
        # fixture whose key must be namespaced, so the dependency is stated.
        return self.store_for("primary")

    # --- documents ------------------------------------------------------------

    def test_absent_documents_read_as_none(self, store):
        # Absence is not an error: every caller treats None as "nothing learned
        # yet" and says so with an actionable message of its own.
        assert store.load_cache() is None, (
            "an absent cache must read as None, not raise and not return an empty "
            f"cache; got {store.load_cache()!r}"
        )

    def test_cache_round_trips(self, store):
        store.save_cache(a_cache())
        loaded = store.load_cache()
        assert loaded is not None, "a cache that was just saved must load back"
        assert [d.identifier for d in loaded.datasets] == ["db.main.orders"], (
            "the cache did not round-trip: datasets came back as "
            f"{[d.identifier for d in loaded.datasets]}"
        )
        assert loaded.provenance.connector == "duckdb", (
            "provenance must round-trip with the document; got "
            f"{loaded.provenance.connector!r}"
        )

    def test_saving_a_cache_replaces_the_previous_one(self, store):
        store.save_cache(a_cache("db.main.orders"))
        store.save_cache(a_cache("db.main.customers"))
        loaded = store.load_cache()
        assert loaded is not None, "a cache that was just saved must load back"
        assert [d.identifier for d in loaded.datasets] == ["db.main.customers"], (
            "save_cache replaces the stored cache, it does not merge into it; got "
            f"{[d.identifier for d in loaded.datasets]}"
        )

    def test_save_cache_stamps_updated_at_when_given_a_clock(self, store):
        now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
        cache = a_cache()
        store.save_cache(cache, now=now)
        # Stamped on the caller's object as well as the stored copy: the command
        # layer reports the same timestamp it just persisted.
        assert cache.provenance.updated_at == now.isoformat(), (
            "save_cache(now=...) must stamp provenance.updated_at on the CALLER'S "
            "object as well as the stored copy: the command layer reports the "
            "timestamp it just persisted, so stamping only what you write makes "
            f"the reported and stored times disagree; caller's object says "
            f"{cache.provenance.updated_at!r}"
        )
        loaded = store.load_cache()
        assert loaded is not None, "a cache that was just saved must load back"
        assert loaded.provenance.updated_at == now.isoformat(), (
            "the stamp must also reach the stored copy; stored says "
            f"{loaded.provenance.updated_at!r}"
        )

    def test_save_cache_without_a_clock_leaves_updated_at_alone(self, store):
        cache = a_cache()
        cache.provenance.updated_at = "2026-07-01T00:00:00+00:00"
        store.save_cache(cache)
        loaded = store.load_cache()
        assert loaded is not None, "a cache that was just saved must load back"
        assert loaded.provenance.updated_at == "2026-07-01T00:00:00+00:00", (
            "save_cache without a clock must leave provenance.updated_at alone, "
            f"never stamp 'now' of its own; got {loaded.provenance.updated_at!r}"
        )

    def test_a_stored_document_is_not_aliased_to_the_callers_object(self, store):
        # A filesystem backend serializes, so a caller cannot reach back into what
        # it already saved. Every backend must behave that way or a mutation after
        # the save would silently rewrite history on some backends and not others.
        cache = a_cache()
        store.save_cache(cache)
        cache.datasets[0].identifier = "mutated.after.save"
        cache.datasets.append(Dataset(identifier="db.main.appended"))
        loaded = store.load_cache()
        assert loaded is not None, "a cache that was just saved must load back"
        assert [d.identifier for d in loaded.datasets] == ["db.main.orders"], (
            "a stored document must not alias the caller's object: mutating what "
            "you saved reached into the store, so a mutation after a save "
            "silently rewrites history. Serialize on write, or deep-copy. Got "
            f"{[d.identifier for d in loaded.datasets]}"
        )

    def test_a_loaded_document_is_not_aliased_to_the_stored_one(self, store):
        # The other direction: mutating what a load returned must not reach back
        # into the store, or one caller's scratch edit becomes everyone's state.
        store.save_cache(a_cache())
        first = store.load_cache()
        assert first is not None, "a cache that was just saved must load back"
        first.datasets[0].identifier = "mutated.after.load"
        second = store.load_cache()
        assert second is not None, "the cache must still load after a caller edit"
        assert [d.identifier for d in second.datasets] == ["db.main.orders"], (
            "a loaded document must not alias the stored one: editing what a load "
            "returned reached back into the store, so one caller's scratch edit "
            f"becomes everyone's state. Got {[d.identifier for d in second.datasets]}"
        )

    # --- ledgers --------------------------------------------------------------

    def test_spend_since_sums_only_from_the_cutoff(self, store):
        store.append_spend_log(
            {"at": "2026-07-02T23:59:00+00:00", "billed_bytes": 1_000}
        )
        store.append_spend_log({"at": "2026-07-03T00:01:00+00:00", "billed_bytes": 200})
        store.append_spend_log({"at": "2026-07-03T12:00:00+00:00", "billed_bytes": 300})
        assert store.spend_since("2026-07-03T00:00:00+00:00") == 500, (
            "spend_since must sum only entries at or after the cutoff; expected "
            f"500, got {store.spend_since('2026-07-03T00:00:00+00:00')}"
        )

    def test_spend_since_is_zero_on_an_empty_ledger(self, store):
        assert store.spend_since("2026-07-03T00:00:00+00:00") == 0.0, (
            "an empty ledger must sum to 0.0, never raise: the cost gate reads "
            "this before the first billed command of every session"
        )

    def test_spend_since_keeps_units_and_connectors_apart(self, store):
        # The safety-critical case: one ledger, several paradigms. A bytes budget
        # must never absorb another connector's seconds entry.
        store.append_spend_log(
            {
                "at": "2026-07-03T10:00:00+00:00",
                "connector": "bigquery",
                "billed_bytes": 90,
            }
        )
        store.append_spend_log(
            {
                "at": "2026-07-03T10:05:00+00:00",
                "connector": "snowflake",
                "billed_seconds": 42,
            }
        )
        cutoff = "2026-07-03T00:00:00+00:00"
        assert store.spend_since(cutoff, connector="bigquery") == 90, (
            "spend_since must filter by connector; expected 90, got "
            f"{store.spend_since(cutoff, connector='bigquery')}"
        )
        assert (
            store.spend_since(cutoff, field="billed_seconds", connector="snowflake")
            == 42
        ), "spend_since must sum the requested field, not always billed_bytes"
        # Asking for the wrong unit or the wrong connector yields nothing, never a
        # cross-paradigm number.
        assert store.spend_since(cutoff, connector="snowflake") == 0.0, (
            "a bytes budget must never absorb another connector's seconds entry: "
            "asking for the wrong unit yields nothing, never a cross-paradigm "
            f"number. Got {store.spend_since(cutoff, connector='snowflake')}"
        )
        assert (
            store.spend_since(cutoff, field="billed_seconds", connector="bigquery")
            == 0.0
        ), "asking for the wrong connector's unit must yield nothing"

    def test_a_ledger_entry_is_not_aliased_to_the_callers_dict(self, store):
        entry = {"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100}
        store.append_spend_log(entry)
        entry["billed_bytes"] = 999_999
        assert store.spend_since("2026-07-03T00:00:00+00:00") == 100, (
            "a stored ledger entry must not alias the caller's dict: mutating the "
            "dict after appending it changed recorded spend, which silently "
            f"rewrites the audit trail. Got "
            f"{store.spend_since('2026-07-03T00:00:00+00:00')}"
        )

    def test_a_malformed_ledger_entry_does_not_poison_the_budget(self, store):
        # Losing one spend record is bad; refusing every subsequent billed command
        # because of one bad record is worse. The opposite policy from the cache,
        # deliberately.
        store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
        store.append_spend_log({"at": None, "billed_bytes": "not a number"})
        store.append_spend_log({"at": "2026-07-03T11:00:00+00:00", "billed_bytes": 50})
        assert store.spend_since("2026-07-03T00:00:00+00:00") == 150, (
            "a malformed ledger entry must be skipped, not raised on: losing one "
            "spend record is bad, refusing every subsequent billed command "
            "because of one bad record is worse. This is the opposite policy from "
            f"load_cache, deliberately. Got "
            f"{store.spend_since('2026-07-03T00:00:00+00:00')}"
        )

    def test_query_log_accepts_every_decision(self, store):
        # Refusals are logged too; the ledger is the audit trail, not a success log.
        for decision in ("allowed", "refused", "needs_confirmation", "failed"):
            store.append_query_log(
                {
                    "at": "2026-07-03T10:00:00+00:00",
                    "sql": "select 1",
                    "decision": decision,
                }
            )

    # --- locators -------------------------------------------------------------

    def test_the_cache_has_a_locator_before_it_is_written(self, store):
        # The over-ceiling error and the drift envelope both report a location
        # without having written one, so a locator must not depend on the
        # document existing.
        assert isinstance(store.locator(Document.CACHE), str), (
            "a locator is an opaque display string, never a Path, so a backend "
            f"with no filesystem is representable; got "
            f"{type(store.locator(Document.CACHE)).__name__}"
        )
        assert store.locator(Document.CACHE), (
            "a locator must be non-empty before the document exists: the "
            "over-ceiling error and the drift envelope both report a location "
            "without having written one"
        )

    def test_the_ledgers_have_locators_before_they_are_written(self, store):
        for document in (Document.QUERY_LOG, Document.SPEND_LOG):
            assert store.locator(document), (
                f"{document.value} has no locator before it is written; a locator "
                "must not depend on the document existing"
            )

    def test_saving_the_cache_returns_the_locator_the_store_reports(self, store):
        assert store.save_cache(a_cache()) == store.locator(Document.CACHE), (
            "save_cache must return the same locator the store reports for "
            "Document.CACHE, or an agent surfaces one location and reads another"
        )

    # --- isolation ------------------------------------------------------------

    def test_two_keys_do_not_share_a_cache(self, store):
        one, two = self.store_for("tenant-one"), self.store_for("tenant-two")
        one.save_cache(a_cache("db.one.orders"))
        # The firewall resolves every table reference against the cache, so a
        # cache leaking across keys is a leak of what the other tenant may query,
        # not merely of what it has profiled.
        assert two.load_cache() is None, (
            "two keys share a cache. The query firewall resolves every table "
            "reference against the cache, so this leaks what the other tenant may "
            "query, not merely what it has profiled. Check that make_store(key) "
            "actually keys storage"
        )
        two.save_cache(a_cache("db.two.orders"))
        first = one.load_cache()
        assert first is not None, "the first key's cache disappeared"
        assert [d.identifier for d in first.datasets] == ["db.one.orders"], (
            "writing under one key overwrote another key's cache; got "
            f"{[d.identifier for d in first.datasets]}"
        )

    def test_two_keys_do_not_share_a_spend_ledger(self, store):
        one, two = self.store_for("tenant-one"), self.store_for("tenant-two")
        one.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 500})
        # A shared ledger would charge one tenant's spend against another's
        # session ceiling, which reads as a budget bug and is a tenancy bug.
        assert two.spend_since("2026-07-03T00:00:00+00:00") == 0.0, (
            "two keys share a spend ledger, so one tenant's spend counts against "
            "another's session ceiling. That reads as a budget bug and is a "
            f"tenancy bug. Got {two.spend_since('2026-07-03T00:00:00+00:00')}"
        )
        assert one.spend_since("2026-07-03T00:00:00+00:00") == 500, (
            "the first key's ledger did not retain its own entry"
        )

    # The query ledger is append-only with no reader on the protocol, so its
    # isolation cannot be asserted from out here. A backend must still key it the
    # same way it keys everything else: it holds SQL text, which names the tables
    # and columns one tenant asked about.


class MaintainStoreContract(ExploreStoreContract):
    """Adds the drift-detection state: the baseline and the last report."""

    def test_absent_maintain_documents_read_as_none(self, store):
        assert store.load_snapshot() is None, (
            "an absent snapshot must read as None; maintain reports 'no baseline "
            "yet, run maintain snapshot' off this and cannot if it raises"
        )
        assert store.load_drift() is None, "an absent drift report must read as None"

    def test_snapshot_round_trips(self, store):
        store.save_snapshot(a_snapshot())
        loaded = store.load_snapshot()
        assert loaded is not None, "a snapshot that was just saved must load back"
        assert loaded.created_at == "2026-07-03T10:00:00+00:00", (
            "the snapshot did not round-trip: created_at is the baseline every "
            f"drift axis compares against; got {loaded.created_at!r}"
        )
        assert loaded.warehouse_from == "metadata", (
            f"snapshot fields must round-trip intact; got {loaded.warehouse_from!r}"
        )

    def test_drift_round_trips_with_its_findings(self, store):
        store.save_drift(a_drift_report())
        loaded = store.load_drift()
        assert loaded is not None, "a drift report that was just saved must load back"
        assert loaded.snapshot_created_at == "2026-07-03T10:00:00+00:00", (
            f"the drift report did not round-trip; got {loaded.snapshot_created_at!r}"
        )
        assert [f.column for f in loaded.axes["schema"].findings] == ["discount"], (
            "nested findings must survive the round-trip: a backend storing only "
            "the top level loses the drift detail the report exists for. Got "
            f"{[f.column for f in loaded.axes['schema'].findings]}"
        )

    def test_maintain_documents_have_locators_before_they_are_written(self, store):
        for document in (Document.SNAPSHOT, Document.DRIFT):
            assert store.locator(document), (
                f"{document.value} has no locator before it is written; the drift "
                "envelope reports a location without having written one"
            )

    def test_saving_maintain_documents_returns_the_reported_locators(self, store):
        assert store.save_snapshot(a_snapshot()) == store.locator(Document.SNAPSHOT), (
            "save_snapshot must return the locator the store reports for "
            "Document.SNAPSHOT"
        )
        assert store.save_drift(a_drift_report()) == store.locator(Document.DRIFT), (
            "save_drift must return the locator the store reports for Document.DRIFT"
        )

    def test_two_keys_do_not_share_a_snapshot(self, store):
        one, two = self.store_for("tenant-one"), self.store_for("tenant-two")
        one.save_snapshot(a_snapshot())
        assert two.load_snapshot() is None, (
            "two keys share a snapshot, so one tenant's drift baseline is another's. "
            "Check that make_store(key) actually keys storage"
        )


class StoreContract(MaintainStoreContract):
    """The full contract: everything above, plus the transform plan store."""

    def test_plan_round_trips(self, store):
        store.save_plan(a_plan("pabc", "2026-07-03T10:00:00+00:00"))
        loaded = store.load_plan("pabc")
        assert loaded.intent == "add a model", (
            f"the plan did not round-trip; intent came back {loaded.intent!r}"
        )
        assert loaded.edits[0].path == "models/staging/stg_orders.sql", (
            "a plan's edits are the reviewable diff it exists to carry and must "
            f"round-trip intact; got {loaded.edits[0].path!r}"
        )

    def test_loading_an_unknown_plan_raises(self, store):
        from ..transform.plans import PlanNotFoundError

        with pytest.raises(PlanNotFoundError):
            # Not a generic KeyError or None: the protocol names this type, and a
            # caller distinguishes "no such plan" from "the store is broken" by it.
            store.load_plan("pnope")

    def test_list_plans_is_newest_first(self, store):
        store.save_plan(a_plan("pold", "2026-07-01T10:00:00+00:00"))
        store.save_plan(a_plan("pnew", "2026-07-03T10:00:00+00:00"))
        assert [p.plan_id for p in store.list_plans()] == ["pnew", "pold"], (
            "list_plans must return newest first; got "
            f"{[p.plan_id for p in store.list_plans()]}"
        )

    def test_list_plans_is_empty_before_anything_is_planned(self, store):
        assert store.list_plans() == [], (
            "list_plans must return an empty list before anything is stored, "
            f"never None and never raise; got {store.list_plans()!r}"
        )

    def test_latest_plan_skips_applied_ones(self, store):
        applied = a_plan("papplied", "2026-07-03T12:00:00+00:00")
        applied.applied_at = "2026-07-03T12:30:00+00:00"
        store.save_plan(applied)
        store.save_plan(a_plan("ppending", "2026-07-03T10:00:00+00:00"))
        latest = store.latest_plan()
        assert latest is not None and latest.plan_id == "ppending", (
            "latest_plan must skip plans with an applied_at: returning an applied "
            "plan makes `transform apply` re-apply work already on disk. Got "
            f"{latest.plan_id if latest else None!r}"
        )

    def test_latest_plan_filters_by_kind(self, store):
        from ..transform.plans import EditKind

        store.save_plan(a_plan("pmodel", "2026-07-03T10:00:00+00:00"))
        store.save_plan(
            a_plan("psemantic", "2026-07-03T11:00:00+00:00", kind=EditKind.SEMANTIC_YML)
        )
        semantic = store.latest_plan(EditKind.SEMANTIC_YML)
        assert semantic is not None and semantic.plan_id == "psemantic", (
            "latest_plan(kind) must return only plans whose every edit is of that "
            f"kind; got {semantic.plan_id if semantic else None!r}"
        )
        model = store.latest_plan(EditKind.MODEL_SQL)
        assert model is not None and model.plan_id == "pmodel", (
            f"kind filtering returned the wrong plan; got "
            f"{model.plan_id if model else None!r}"
        )

    def test_latest_plan_is_none_when_everything_is_applied(self, store):
        applied = a_plan("papplied", "2026-07-03T12:00:00+00:00")
        applied.applied_at = "2026-07-03T12:30:00+00:00"
        store.save_plan(applied)
        assert store.latest_plan() is None, (
            "latest_plan must be None when every stored plan is applied, not fall "
            "back to the most recent one"
        )

    def test_resaving_a_plan_updates_it_in_place(self, store):
        # This is how apply marks a plan applied: same id, new applied_at.
        plan = a_plan("pabc", "2026-07-03T10:00:00+00:00")
        store.save_plan(plan)
        plan.applied_at = "2026-07-03T10:30:00+00:00"
        store.save_plan(plan)
        assert len(store.list_plans()) == 1, (
            "re-saving a plan under the same id must update it in place, not "
            f"append a second copy; got {len(store.list_plans())} plans"
        )
        assert store.load_plan("pabc").applied_at == "2026-07-03T10:30:00+00:00", (
            "the update did not persist: this is how apply marks a plan applied, "
            "so losing it re-applies work already written to the dbt project"
        )

    def test_plan_locators_are_per_plan(self, store):
        first = store.save_plan(a_plan("pone", "2026-07-03T10:00:00+00:00"))
        second = store.save_plan(a_plan("ptwo", "2026-07-03T11:00:00+00:00"))
        assert first == store.plan_locator("pone"), (
            "save_plan must return the same locator plan_locator reports for that id"
        )
        assert second == store.plan_locator("ptwo"), (
            "save_plan must return the same locator plan_locator reports for that id"
        )
        assert first != second, (
            "plan locators must be per plan, not one location for the whole store"
        )

    def test_two_keys_do_not_share_plans(self, store):
        one, two = self.store_for("tenant-one"), self.store_for("tenant-two")
        one.save_plan(a_plan("pabc", "2026-07-03T10:00:00+00:00"))
        assert two.list_plans() == [], (
            "two keys share the plan store, so one tenant can read and apply "
            f"another's proposed dbt changes. Got {two.list_plans()!r}"
        )


class StoreFactoryContract(_KeyedContract):
    """The construction half, for a backend dex builds rather than receives.

    Mix it in front of the contract for your tier, and the whole behavioral suite
    runs against stores built the way dex builds them::

        class TestMyStore(StoreFactoryContract, StoreContract):
            tier = Store

            def build(self, context):
                return my_store_factory(context)

            def context_for(self, key):
                return StoreContext(options={"tenant": key})

    Two hooks rather than one factory attribute, because a plain function assigned
    to a class attribute binds as a method and would arrive with ``self`` in front
    of the context, which is a confusing failure to meet on your first run.

    ``context_for`` is where a backend says what it is keyed by. Build the context
    the way a real configuration entry would produce it: a filesystem-shaped
    backend varies ``repo_root``, a tenant-keyed one varies an entry in
    ``options``. A ``context_for`` that returns the same context for every key
    passes nothing meaningful, since the isolation assertions can only be as
    honest as the contexts they are given.
    """

    #: The tier :meth:`build` promises to return. Widen it to ``MaintainStore`` or
    #: ``Store`` alongside the matching contract class; the default is the floor
    #: every backend meets.
    tier: Any = ExploreStore

    def build(self, context: StoreContext) -> Any:
        """Your factory, called with a context. One line in most backends."""

        raise NotImplementedError(
            "a factory conformance subclass must implement "
            "build(context) -> Store, calling the factory under test"
        )

    def context_for(self, key: str) -> StoreContext:
        """The context that keys a store to ``key``, as configuration would."""

        raise NotImplementedError(
            "a factory conformance subclass must implement "
            "context_for(key) -> StoreContext, keying the store the way a real "
            "configuration entry would"
        )

    def make_store(self, key: str) -> Any:
        return self.build(self.context_for(key))

    def test_the_factory_builds_a_store_of_the_declared_tier(self):
        built = self.build(self.context_for(self.key_for("primary")))
        assert isinstance(built, self.tier), (
            f"the factory returned {type(built).__name__}, which does not satisfy "
            f"{self.tier.__name__}. A backend that passes the behavioral suite and "
            "fails here is unusable from configuration: dex builds it, checks the "
            "tier the command needs, and refuses. Check that build() returns the "
            "store rather than a class, a coroutine, or a wrapper"
        )

    def test_two_contexts_build_stores_that_share_nothing(self):
        # The same property the keyed isolation assertions cover, checked at the
        # construction seam: a factory that ignores its context builds one shared
        # store for every tenant, and every later isolation guarantee is void.
        #
        # Through build() rather than store_for(), so what is under test is named
        # even where the two are the same call.
        one = self.build(self.context_for(self.key_for("tenant-one")))
        two = self.build(self.context_for(self.key_for("tenant-two")))
        one.save_cache(a_cache("db.one.orders"))
        assert two.load_cache() is None, (
            "two contexts built stores that share a cache. The factory is probably "
            "ignoring the part of the context that keys it, so every tenant it "
            "builds lands in the same state. Check that context_for varies "
            "something and that build reads it"
        )
