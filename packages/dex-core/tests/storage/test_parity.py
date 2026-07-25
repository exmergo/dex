"""One contract, every backend: the assertions each ``Store`` owes its callers.

Parameterized over both backends deliberately. The engine talks to the store
through the protocol, so the interesting question is never "does the filesystem
work" but "do the backends agree". Adding a backend means adding it to
``store_factory`` here, and the whole contract runs against it for free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from exmergo_dex_core.cache import CacheProvenance, Dataset, DexCache
from exmergo_dex_core.maintain.drift import AxisResult, DriftFinding, DriftReport
from exmergo_dex_core.maintain.snapshot import Snapshot, WarehouseBaseline
from exmergo_dex_core.storage import Document, FilesystemStore, MemoryStore, Store
from exmergo_dex_core.transform.plans import (
    EditKind,
    PlanEdit,
    PlanNotFoundError,
    TransformPlan,
)


@pytest.fixture(params=["filesystem", "memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    if request.param == "filesystem":
        return FilesystemStore(tmp_path)
    return MemoryStore()


def _cache(identifier: str = "db.main.orders") -> DexCache:
    return DexCache(
        datasets=[Dataset(identifier=identifier, row_count=3)],
        provenance=CacheProvenance(connector="duckdb"),
    )


def _snapshot(created_at: str = "2026-07-03T10:00:00+00:00") -> Snapshot:
    return Snapshot(
        created_at=created_at,
        connector="duckdb",
        warehouse=WarehouseBaseline(datasets=[]),
        warehouse_from="metadata",
    )


def _drift() -> DriftReport:
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


def _plan(plan_id: str, created_at: str, *, kind=EditKind.MODEL_SQL) -> TransformPlan:
    return TransformPlan(
        plan_id=plan_id,
        created_at=created_at,
        intent="add a model",
        project_dir="analytics",
        edits=[
            PlanEdit(
                path="models/staging/stg_orders.sql",
                kind=kind,
                new_content="select 1 as id\n",
            )
        ],
    )


# --- documents ----------------------------------------------------------------


def test_absent_documents_read_as_none(store: Store):
    # Absence is not an error: every caller treats None as "nothing learned yet"
    # and says so with an actionable message of its own.
    assert store.load_cache() is None
    assert store.load_snapshot() is None
    assert store.load_drift() is None


def test_cache_round_trips(store: Store):
    store.save_cache(_cache())
    loaded = store.load_cache()
    assert loaded is not None
    assert [d.identifier for d in loaded.datasets] == ["db.main.orders"]
    assert loaded.provenance.connector == "duckdb"


def test_saving_a_cache_replaces_the_previous_one(store: Store):
    store.save_cache(_cache("db.main.orders"))
    store.save_cache(_cache("db.main.customers"))
    loaded = store.load_cache()
    assert loaded is not None
    assert [d.identifier for d in loaded.datasets] == ["db.main.customers"]


def test_save_cache_stamps_updated_at_when_given_a_clock(store: Store):
    now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    cache = _cache()
    store.save_cache(cache, now=now)
    # Stamped on the caller's object as well as the stored copy: the command
    # layer reports the same timestamp it just persisted.
    assert cache.provenance.updated_at == now.isoformat()
    loaded = store.load_cache()
    assert loaded is not None
    assert loaded.provenance.updated_at == now.isoformat()


def test_save_cache_without_a_clock_leaves_updated_at_alone(store: Store):
    cache = _cache()
    cache.provenance.updated_at = "2026-07-01T00:00:00+00:00"
    store.save_cache(cache)
    loaded = store.load_cache()
    assert loaded is not None
    assert loaded.provenance.updated_at == "2026-07-01T00:00:00+00:00"


def test_a_stored_document_is_not_aliased_to_the_callers_object(store: Store):
    # A filesystem backend serializes, so a caller cannot reach back into what it
    # already saved. Every backend must behave that way or a mutation after the
    # save would silently rewrite history on some backends and not others.
    cache = _cache()
    store.save_cache(cache)
    cache.datasets[0].identifier = "mutated.after.save"
    cache.datasets.append(Dataset(identifier="db.main.appended"))
    loaded = store.load_cache()
    assert loaded is not None
    assert [d.identifier for d in loaded.datasets] == ["db.main.orders"]


def test_snapshot_round_trips(store: Store):
    store.save_snapshot(_snapshot())
    loaded = store.load_snapshot()
    assert loaded is not None
    assert loaded.created_at == "2026-07-03T10:00:00+00:00"
    assert loaded.warehouse_from == "metadata"


def test_drift_round_trips_with_its_findings(store: Store):
    store.save_drift(_drift())
    loaded = store.load_drift()
    assert loaded is not None
    assert loaded.snapshot_created_at == "2026-07-03T10:00:00+00:00"
    assert [f.column for f in loaded.axes["schema"].findings] == ["discount"]


# --- ledgers ------------------------------------------------------------------


def test_spend_since_sums_only_from_the_cutoff(store: Store):
    store.append_spend_log({"at": "2026-07-02T23:59:00+00:00", "billed_bytes": 1_000})
    store.append_spend_log({"at": "2026-07-03T00:01:00+00:00", "billed_bytes": 200})
    store.append_spend_log({"at": "2026-07-03T12:00:00+00:00", "billed_bytes": 300})
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 500


def test_spend_since_is_zero_on_an_empty_ledger(store: Store):
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 0.0


def test_spend_since_keeps_units_and_connectors_apart(store: Store):
    # The safety-critical case: one ledger, several paradigms. A bytes budget must
    # never absorb another connector's seconds entry.
    store.append_spend_log(
        {"at": "2026-07-03T10:00:00+00:00", "connector": "bigquery", "billed_bytes": 90}
    )
    store.append_spend_log(
        {
            "at": "2026-07-03T10:05:00+00:00",
            "connector": "snowflake",
            "billed_seconds": 42,
        }
    )
    cutoff = "2026-07-03T00:00:00+00:00"
    assert store.spend_since(cutoff, connector="bigquery") == 90
    assert (
        store.spend_since(cutoff, field="billed_seconds", connector="snowflake") == 42
    )
    # Asking for the wrong unit or the wrong connector yields nothing, never a
    # cross-paradigm number.
    assert store.spend_since(cutoff, connector="snowflake") == 0.0
    assert (
        store.spend_since(cutoff, field="billed_seconds", connector="bigquery") == 0.0
    )


def test_a_ledger_entry_is_not_aliased_to_the_callers_dict(store: Store):
    entry = {"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100}
    store.append_spend_log(entry)
    entry["billed_bytes"] = 999_999
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 100


def test_query_log_accepts_every_decision(store: Store):
    # Refusals are logged too; the ledger is the audit trail, not a success log.
    for decision in ("allowed", "refused", "needs_confirmation", "failed"):
        store.append_query_log(
            {"at": "2026-07-03T10:00:00+00:00", "sql": "select 1", "decision": decision}
        )


# --- plans --------------------------------------------------------------------


def test_plan_round_trips(store: Store):
    store.save_plan(_plan("pabc", "2026-07-03T10:00:00+00:00"))
    loaded = store.load_plan("pabc")
    assert loaded.intent == "add a model"
    assert loaded.edits[0].path == "models/staging/stg_orders.sql"


def test_loading_an_unknown_plan_raises(store: Store):
    with pytest.raises(PlanNotFoundError):
        store.load_plan("pnope")


def test_list_plans_is_newest_first(store: Store):
    store.save_plan(_plan("pold", "2026-07-01T10:00:00+00:00"))
    store.save_plan(_plan("pnew", "2026-07-03T10:00:00+00:00"))
    assert [p.plan_id for p in store.list_plans()] == ["pnew", "pold"]


def test_list_plans_is_empty_before_anything_is_planned(store: Store):
    assert store.list_plans() == []


def test_latest_plan_skips_applied_ones(store: Store):
    applied = _plan("papplied", "2026-07-03T12:00:00+00:00")
    applied.applied_at = "2026-07-03T12:30:00+00:00"
    store.save_plan(applied)
    store.save_plan(_plan("ppending", "2026-07-03T10:00:00+00:00"))
    latest = store.latest_plan()
    assert latest is not None and latest.plan_id == "ppending"


def test_latest_plan_filters_by_kind(store: Store):
    store.save_plan(_plan("pmodel", "2026-07-03T10:00:00+00:00"))
    store.save_plan(
        _plan("psemantic", "2026-07-03T11:00:00+00:00", kind=EditKind.SEMANTIC_YML)
    )
    semantic = store.latest_plan(EditKind.SEMANTIC_YML)
    assert semantic is not None and semantic.plan_id == "psemantic"
    model = store.latest_plan(EditKind.MODEL_SQL)
    assert model is not None and model.plan_id == "pmodel"


def test_latest_plan_is_none_when_everything_is_applied(store: Store):
    applied = _plan("papplied", "2026-07-03T12:00:00+00:00")
    applied.applied_at = "2026-07-03T12:30:00+00:00"
    store.save_plan(applied)
    assert store.latest_plan() is None


def test_resaving_a_plan_updates_it_in_place(store: Store):
    # This is how apply marks a plan applied: same id, new applied_at.
    plan = _plan("pabc", "2026-07-03T10:00:00+00:00")
    store.save_plan(plan)
    plan.applied_at = "2026-07-03T10:30:00+00:00"
    store.save_plan(plan)
    assert len(store.list_plans()) == 1
    assert store.load_plan("pabc").applied_at == "2026-07-03T10:30:00+00:00"


# --- locators -----------------------------------------------------------------


@pytest.mark.parametrize("document", list(Document))
def test_every_document_has_a_locator_before_it_is_written(
    store: Store, document: Document
):
    # The over-ceiling error and the drift envelope both report a location without
    # having written one, so a locator must not depend on the document existing.
    assert isinstance(store.locator(document), str)
    assert store.locator(document)


def test_saving_returns_the_same_locator_the_store_reports(store: Store):
    assert store.save_cache(_cache()) == store.locator(Document.CACHE)
    assert store.save_snapshot(_snapshot()) == store.locator(Document.SNAPSHOT)
    assert store.save_drift(_drift()) == store.locator(Document.DRIFT)


def test_plan_locators_are_per_plan(store: Store):
    first = store.save_plan(_plan("pone", "2026-07-03T10:00:00+00:00"))
    second = store.save_plan(_plan("ptwo", "2026-07-03T11:00:00+00:00"))
    assert first == store.plan_locator("pone")
    assert second == store.plan_locator("ptwo")
    assert first != second
