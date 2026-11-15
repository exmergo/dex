"""`maintain coverage --mutate`: refreshing stale and missing strength (#1284).

The batch rules (order, cap, free skips, one handshake, the budget stop) are
checked against a stubbed `transform test --mutate`; the round trip at the
bottom runs the real thing on dbt and DuckDB.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.envelope import Cost
from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError
from exmergo_dex_core.maintain import coverage_refresh as refresh_mod
from exmergo_dex_core.maintain.coverage_refresh import Candidate
from exmergo_dex_core.transform import commands as transform_commands
from exmergo_dex_core.transform.mutation import MutationError

from .test_coverage_strength import FCT, MART, _manifest, _record, _strength


def _candidates(*specs: tuple[str, str], **flags) -> list[Candidate]:
    return [
        Candidate(
            unique_id=f"model.shop.{name}",
            label=name,
            name=name,
            status=status,
            **flags.get(name, {}),
        )
        for name, status in specs
    ]


def _chain_manifest(*names: str) -> dict:
    """Models named in order, each reading the one before it."""

    nodes = {}
    previous = None
    for name in names:
        nodes[f"model.shop.{name}"] = {
            "resource_type": "model",
            "name": name,
            "language": "sql",
            "config": {},
            "depends_on": {"nodes": [previous] if previous else []},
        }
        previous = f"model.shop.{name}"
    return {"metadata": {"project_name": "shop"}, "nodes": nodes}


# --- order and free skips ---------------------------------------------------


def test_missing_comes_before_stale_then_upstream_before_downstream():
    manifest = _chain_manifest("a", "b", "c", "d")
    ordered = refresh_mod.order(
        _candidates(("d", "missing"), ("c", "stale"), ("a", "stale"), ("b", "missing")),
        manifest,
        None,
    )
    assert [c.label for c in ordered] == ["b", "d", "a", "c"]


def test_a_selector_keeps_the_order_it_was_written_in():
    manifest = _chain_manifest("a", "b", "c")
    ordered = refresh_mod.order(
        _candidates(("a", "missing"), ("b", "stale"), ("c", "missing")),
        manifest,
        ["c", "a", "b"],
    )
    assert [c.label for c in ordered] == ["c", "a", "b"]


@pytest.mark.parametrize(
    ("node", "reason"),
    [
        ({"language": "python"}, "python_model"),
        ({"config": {"sql_header": "set x = 1"}}, "sql_header"),
        ({"version": 2}, "versioned_model"),
        ({"language": "sql", "config": {}}, None),
    ],
)
def test_models_a_run_would_refuse_are_skipped_off_the_manifest(node, reason):
    assert refresh_mod.skip_reason(node) == reason


# --- the batch, with transform test --mutate stubbed --------------------------


class _Engine:
    """Just what the batch reads off an engine."""

    def __init__(self, connector: str = "duckdb", *, budget: float | None = None):
        self.config = DexConfig(connector=connector, dbt_target="dev")
        self.connector = connector
        self.budget = budget
        self.confirmed = budget is not None
        self.store = None
        self._last_spend = None
        self.adapters: list[str] = []

    def _adapter(self, command: str):
        self.adapters.append(command)
        return SimpleNamespace(cost_gate=None)


def _result(*, score=0.5, not_run=0, budget=True):
    return SimpleNamespace(
        score=score,
        score_with_downstream=None,
        incremental=None,
        counts={"survived": 1, "not_run": not_run},
        record="/somewhere",
        warnings=(
            ["the confirmed budget covered 1 of 3 mutants; the rest are not_run"]
            if not_run and budget
            else []
        ),
    )


@pytest.fixture
def calls(monkeypatch):
    seen: dict[str, list] = {"runs": [], "prices": [], "handshakes": []}
    plan: dict[str, object] = {
        "refuse": {},
        "short": set(),
        "branch_only": set(),
        "price": 10.0,
        "baseline": 0.0,
    }

    def fake(engine, model, *, target=None, refresh_batch=None, **_):
        refused = plan["refuse"].get(model)
        if refused:
            raise MutationError(refused)
        if refresh_batch is not None and refresh_batch.pricing:
            seen["prices"].append(model)
            refresh_batch.add_price(
                {"name": model},
                plan["price"],
                {"(baseline)": plan["baseline"]},
                [],
            )
            return None
        seen["runs"].append(model)
        if refresh_batch is not None:
            refresh_batch.record_spend(plan["price"], None)
        if model in plan["branch_only"]:
            return _result(not_run=2, budget=False)
        return _result(not_run=2 if model in plan["short"] else 0)

    def handshake(command, adapter, estimate, *, per_table=None, notes=None, **_):
        seen["handshakes"].append((command, estimate, dict(per_table or {})))
        seen.setdefault("notes", []).append(list(notes or []))
        if not plan.get("confirmed"):
            raise ConfirmationRequiredError(Cost())

    monkeypatch.setattr(transform_commands, "test_mutations", fake)
    monkeypatch.setattr(
        "exmergo_dex_core.command_args.billed_handshake", handshake, raising=True
    )
    seen["plan"] = plan
    return seen


def test_a_free_connector_runs_each_model_in_order_with_no_handshake(calls):
    engine = _Engine()
    manifest = _chain_manifest("a", "b", "c")
    block, _warnings, _spend, _cost = refresh_mod.refresh(
        engine, manifest, _candidates(("c", "stale"), ("b", "missing"), ("a", "stale"))
    )
    assert calls["runs"] == ["b", "a", "c"]
    assert calls["prices"] == [] and calls["handshakes"] == []
    assert engine.adapters == []
    assert block["refreshed_count"] == 3
    assert [entry["model"] for entry in block["refreshed"]] == ["b", "a", "c"]
    assert block["refreshed"][0]["was"] == "missing"


def test_the_cap_narrows_and_names_the_rest_for_the_next_run(calls):
    manifest = _chain_manifest("a", "b", "c", "d")
    block, *_ = refresh_mod.refresh(
        _Engine(),
        manifest,
        _candidates(
            ("a", "missing"), ("b", "missing"), ("c", "missing"), ("d", "missing")
        ),
        limit=2,
    )
    assert calls["runs"] == ["a", "b"]
    assert block["attempted"] == 2 and block["limit"] == 2
    assert block["elided"] == {"count": 2, "models": ["c", "d"]}


def test_the_cap_cannot_be_widened():
    with pytest.raises(ValueError, match="above the ceiling of 25"):
        refresh_mod.refresh(_Engine(), {}, [], limit=26)


def test_a_skipped_model_is_free_and_does_not_count_against_the_cap(calls):
    manifest = _chain_manifest("a", "b")
    manifest["nodes"]["model.shop.a"]["language"] = "python"
    block, *_ = refresh_mod.refresh(
        _Engine(), manifest, _candidates(("a", "missing"), ("b", "missing")), limit=1
    )
    assert calls["runs"] == ["b"]
    assert block["skipped"] == [{"model": "a", "reason_code": "python_model"}]
    assert block["elided"]["count"] == 0


def test_a_refusal_is_reported_with_a_code_and_the_batch_goes_on(calls):
    calls["plan"]["refuse"] = {
        "a": "'a' has no SQL dex knows how to plant a defect in (...)"
    }
    block, *_ = refresh_mod.refresh(
        _Engine(),
        _chain_manifest("a", "b"),
        _candidates(("a", "missing"), ("b", "stale")),
    )
    assert calls["runs"] == ["b"]
    assert block["refused"][0]["model"] == "a"
    assert block["refused"][0]["reason_code"] == "no_mutable_sql"


def test_nothing_to_refresh_runs_nothing():
    block, _warnings, spend, cost = refresh_mod.refresh(_Engine(), {}, [])
    assert block["ran"] is False and block["attempted"] == 0
    assert spend is None and cost is None


def test_a_metered_batch_is_priced_and_confirmed_once(calls):
    engine = _Engine("bigquery")
    with pytest.raises(ConfirmationRequiredError):
        refresh_mod.refresh(
            engine,
            _chain_manifest("a", "b", "c"),
            _candidates(("a", "missing"), ("b", "missing"), ("c", "stale")),
        )
    assert calls["prices"] == ["a", "b", "c"]
    assert calls["runs"] == [], "nothing runs before the one handshake"
    assert len(calls["handshakes"]) == 1
    command, estimate, lines = calls["handshakes"][0]
    assert command == "maintain coverage"
    assert estimate == 30.0
    assert lines == {"a": 10.0, "b": 10.0, "c": 10.0}


def test_the_ask_names_the_models_refused_while_pricing(calls):
    calls["plan"]["refuse"] = {"b": "'b' has no SQL dex knows how to plant (...)"}
    with pytest.raises(ConfirmationRequiredError):
        refresh_mod.refresh(
            _Engine("bigquery"),
            _chain_manifest("a", "b"),
            _candidates(("a", "missing"), ("b", "missing")),
        )
    assert calls["handshakes"][0][2] == {"a": 10.0}
    assert any("b (no_mutable_sql)" in note for note in calls["notes"][0])


def test_a_budget_that_runs_out_leaves_the_rest_not_run(calls):
    calls["plan"]["confirmed"] = True
    calls["plan"]["short"] = {"b"}
    engine = _Engine("bigquery", budget=100.0)
    block, warnings, _spend, cost = refresh_mod.refresh(
        engine,
        _chain_manifest("a", "b", "c", "d"),
        _candidates(
            ("a", "missing"), ("b", "missing"), ("c", "missing"), ("d", "missing")
        ),
    )
    assert calls["runs"] == ["a", "b"]
    assert [e["complete"] for e in block["refreshed"]] == [True, False]
    assert block["refreshed"][1]["not_run"] == 2
    assert block["not_run"] == [
        {"model": "c", "reason_code": "budget"},
        {"model": "d", "reason_code": "budget"},
    ]
    assert any("ran out part way" in w for w in warnings)
    assert cost.estimate == 40.0 and cost.ceiling == 100.0


def test_each_run_draws_on_what_the_batch_has_left(calls, monkeypatch):
    calls["plan"]["confirmed"] = True
    seen: list[float | None] = []
    original = transform_commands.test_mutations

    def watching(engine, model, *, refresh_batch=None, **kwargs):
        if refresh_batch is not None and not refresh_batch.pricing:
            seen.append(refresh_batch.remaining())
        return original(engine, model, refresh_batch=refresh_batch, **kwargs)

    monkeypatch.setattr(transform_commands, "test_mutations", watching)
    refresh_mod.refresh(
        _Engine("bigquery", budget=25.0),
        _chain_manifest("a", "b"),
        _candidates(("a", "missing"), ("b", "missing")),
    )
    assert seen == [25.0, 15.0]


# --- the refusal record -------------------------------------------------------


def test_a_structural_refusal_reads_untestable_until_the_model_changes():
    manifest = _manifest()
    records = {FCT: _record(manifest, refused="no_mutable_sql", score=None)}
    strength = _strength(manifest, records)
    assert strength["fct"]["status"] == "untestable"
    assert strength["fct"]["reason"] == "no_mutable_sql"

    manifest["nodes"][FCT]["raw_code"] = "select id from raw where id > 0"
    strength = _strength(manifest, records)
    assert strength["fct"] == {"status": "missing", "changed": ["model_sql"]}


def test_a_refusal_record_is_left_out_of_the_score_bands():
    from exmergo_dex_core.maintain import coverage as coverage_mod

    manifest = _manifest()
    records = {
        FCT: _record(manifest, refused="no_mutable_sql", score=None),
        MART: _record(manifest, MART, score=0.9),
    }
    found = coverage_mod.inventory(manifest, records=records)
    summary = found.summary["models"]["strength"]
    assert summary["by_status"]["untestable"] >= 1
    assert sum(summary["fresh_score_bands"].values()) == 1


# --- the command ----------------------------------------------------------------


def test_values_free_mode_refuses_mutate_before_anything_opens(tmp_path: Path):
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.storage import MemoryStore
    from exmergo_dex_core.values_free import ValuesFreeCode, ValuesFreeError

    engine = DexEngine(
        connector="duckdb",
        repo_root=str(tmp_path),
        store=MemoryStore(),
        config=DexConfig(connector="duckdb"),
        result_mode="values_free",
    )
    with pytest.raises(ValuesFreeError) as caught:
        engine.coverage(mutate=True)
    assert caught.value.code is ValuesFreeCode.COMMAND_NOT_VALUES_FREE


def test_mutate_refreshes_in_one_command_and_a_second_run_moves_on(
    tmp_path: Path, duckdb_file: Path
):
    pytest.importorskip("dbt.adapters.duckdb")
    from tests.transform.mutation_helpers import _engine, _schema_yml
    from tests.transform.mutation_helpers import (
        measurable_project as _fixture,
    )

    project = _fixture.__wrapped__(tmp_path, duckdb_file)
    (project / "models" / "schema.yml").write_text(_schema_yml(True), "utf-8")
    engine = _engine(project)
    engine.build(target="dev")

    # A cap of zero plants nothing, which says nothing about the SQL: no
    # refusal is recorded for it.
    with pytest.raises(MutationError):
        engine.test_mutations("fct", max_mutants=0)
    assert engine.store.load_mutation_records() == {}

    first = engine.coverage(["fct"], mutate=True)
    assert first.refresh["attempted"] == 1
    assert [e["model"] for e in first.refresh["refreshed"]] == ["fct"]
    assert first.sections["models"][0]["strength"]["status"] == "fresh"
    assert first.refresh["refreshed"][0]["recorded"] is True

    again = engine.coverage(["fct"], mutate=True)
    assert again.refresh["ran"] is False
    assert again.refresh["candidates"] == 0

    # The staging models are passthroughs: refused once, recorded, and left
    # alone afterwards rather than retried on every run.
    whole = engine.coverage(mutate=True)
    refused = {e["model"]: e["reason_code"] for e in whole.refresh["refused"]}
    assert refused == {
        "stg_customers": "no_mutable_sql",
        "stg_orders": "no_mutable_sql",
    }
    after = engine.coverage(mutate=True)
    assert after.refresh["candidates"] == 0
    statuses = {r["name"]: r["strength"]["status"] for r in after.sections["models"]}
    assert statuses == {
        "fct": "fresh",
        "stg_customers": "untestable",
        "stg_orders": "untestable",
    }


# --- review fixes -------------------------------------------------------------


def test_a_downstream_only_model_is_skipped_where_routing_bills(calls):
    calls["plan"]["confirmed"] = True
    block, *_ = refresh_mod.refresh(
        _Engine("bigquery", budget=100.0),
        _chain_manifest("a", "b"),
        _candidates(("a", "missing"), ("b", "missing"), a={"own_tests": False}),
        limit=1,
    )
    assert block["skipped"] == [{"model": "a", "reason_code": "downstream_only"}]
    assert calls["runs"] == ["b"]


def test_a_downstream_only_model_still_runs_on_duckdb(calls):
    refresh_mod.refresh(
        _Engine(),
        _chain_manifest("a"),
        _candidates(("a", "missing"), a={"own_tests": False}),
    )
    assert calls["runs"] == ["a"]


def test_a_model_the_data_refused_is_retried_after_the_untried_ones():
    manifest = _chain_manifest("a", "b", "c")
    ordered = refresh_mod.order(
        _candidates(
            ("a", "missing"), ("b", "missing"), ("c", "stale"), a={"retry": True}
        ),
        manifest,
        None,
    )
    assert [c.label for c in ordered] == ["b", "c", "a"]


def test_a_model_starts_only_if_its_baseline_fits_what_is_left(calls):
    calls["plan"]["confirmed"] = True
    calls["plan"]["baseline"] = 6.0
    block, *_ = refresh_mod.refresh(
        _Engine("bigquery", budget=15.0),
        _chain_manifest("a", "b"),
        _candidates(("a", "missing"), ("b", "missing")),
    )
    # a spends 10, leaving 5: b's baseline of 6 would overrun it.
    assert calls["runs"] == ["a"]
    assert block["not_run"] == [{"model": "b", "reason_code": "budget"}]


def test_mutants_left_not_run_for_another_reason_do_not_stop_the_batch(calls):
    calls["plan"]["confirmed"] = True
    calls["plan"]["branch_only"] = {"a"}
    block, warnings, *_ = refresh_mod.refresh(
        _Engine("bigquery", budget=100.0),
        _chain_manifest("a", "b"),
        _candidates(("a", "missing"), ("b", "missing")),
    )
    assert calls["runs"] == ["a", "b"]
    assert block["not_run"] == []
    assert not any("ran out" in w for w in warnings)


def test_a_refresh_that_runs_nothing_reports_no_earlier_spend():
    engine = _Engine()
    engine._last_spend = {"bytes_billed": 1.0}
    refresh_mod.refresh(engine, {}, [])
    assert engine._last_spend is None


def test_a_baseline_refusal_reads_missing_and_marks_a_retry():
    manifest = _manifest()
    records = {FCT: _record(manifest, refused="baseline_not_passing", score=None)}
    strength = _strength(manifest, records)["fct"]
    assert strength["status"] == "missing"
    assert strength["last_refusal"] == "baseline_not_passing"


def test_a_failed_baseline_hands_its_meter_back_so_the_spend_is_booked(tmp_path):
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.storage import FilesystemStore
    from exmergo_dex_core.transform import mutation

    class _Shadow:
        def write(self, *args, **kwargs):
            pass

        def test(self, model, **kwargs):
            return {
                "success": False,
                "nodes": [
                    {
                        "unique_id": "test.p.not_null.abc",
                        "name": "not_null",
                        "status": "fail",
                        "execution_time": 4.0,
                    }
                ],
            }

    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1", dialect="duckdb")
    )
    meters = []
    with pytest.raises(MutationError, match="passes against the unmutated model"):
        transform_commands._run_mutants(
            _Shadow(),
            "models/fct.sql",
            batch,
            model="fct",
            paradigm=Paradigm.COMPUTE_TIME,
            connector="snowflake",
            store=FilesystemStore(tmp_path),
            ceiling=100.0,
            estimate=10.0,
            mutation_mod=mutation,
            on_meter=meters.append,
        )
    assert meters and meters[0].spent > 0


def test_a_selector_still_puts_a_refused_model_after_the_rest():
    manifest = _chain_manifest("a", "b")
    ordered = refresh_mod.order(
        _candidates(("a", "missing"), ("b", "missing"), a={"retry": True}),
        manifest,
        ["a", "b"],
    )
    assert [c.label for c in ordered] == ["b", "a"]


def test_an_unpriced_model_never_starts_where_the_batch_bills(calls):
    calls["plan"]["confirmed"] = True
    calls["plan"]["price"] = None
    block, warnings, *_ = refresh_mod.refresh(
        _Engine("bigquery", budget=100.0),
        _chain_manifest("a"),
        _candidates(("a", "missing")),
    )
    assert calls["runs"] == []
    assert block["not_run"] == [{"model": "a", "reason_code": "unpriced"}]
    assert any("could not be priced" in w for w in warnings)


def test_a_kept_score_is_marked_so_a_stale_one_is_retried_last():
    from exmergo_dex_core.storage import MemoryStore

    manifest = _manifest()
    prior = _record(manifest)
    store = MemoryStore()
    store.save_mutation_record(prior)
    fingerprint = {
        "sql_digest": prior["sql_digest"],
        "tests_digest": prior["tests_digest"],
    }
    transform_commands._record_mutation_refusal(
        store,
        {**manifest["nodes"][FCT], "unique_id": FCT},
        fingerprint,
        "baseline_not_passing",
        keep_fresh_score=True,
    )
    kept = store.load_mutation_records()[FCT]
    assert kept["score"] == prior["score"] and not kept.get("refused")
    assert kept["last_refusal"] == "baseline_not_passing"
    assert _strength(manifest, {FCT: kept})["fct"]["last_refusal"] == (
        "baseline_not_passing"
    )


def test_retries_rotate_so_one_failing_model_cannot_starve_the_others():
    manifest = _chain_manifest("a", "b")
    first = refresh_mod.order(
        _candidates(
            ("a", "missing"),
            ("b", "missing"),
            a={"retry": True, "retried_at": "2026-11-15T10:00:00+00:00"},
            b={"retry": True, "retried_at": "2026-11-15T11:00:00+00:00"},
        ),
        manifest,
        ["a", "b"],
    )
    assert [c.label for c in first] == ["a", "b"]
    # a was retried and refused again, so its marker is now the newest.
    second = refresh_mod.order(
        _candidates(
            ("a", "missing"),
            ("b", "missing"),
            a={"retry": True, "retried_at": "2026-11-15T12:00:00+00:00"},
            b={"retry": True, "retried_at": "2026-11-15T11:00:00+00:00"},
        ),
        manifest,
        ["a", "b"],
    )
    assert [c.label for c in second] == ["b", "a"]


def test_a_kept_score_gone_stale_downstream_is_a_retry_candidate():
    manifest = _manifest()
    record = _record(
        manifest,
        downstream_tests={"test.shop.unique_mart_id": "old-digest"},
        last_refusal="baseline_not_passing",
        last_refusal_at="2026-11-15T11:30:00+00:00",
    )
    strength = _strength(manifest, {FCT: record})["fct"]
    assert strength["status"] == "stale"
    assert strength["changed"] == ["downstream_tests"]
    assert strength["last_refusal_at"] == "2026-11-15T11:30:00+00:00"


def _clock(monkeypatch, *stamps: str):
    """Make each refusal record carry the next of ``stamps`` as its time."""

    from datetime import datetime as real

    times = iter(real.fromisoformat(stamp) for stamp in stamps)

    class _Now(real):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(transform_commands, "datetime", _Now)


def _refuse(store, manifest, model_id: str, reason: str = "baseline_not_passing"):
    from exmergo_dex_core import dbt_tests

    node = {**manifest["nodes"][model_id], "unique_id": model_id}
    transform_commands._record_mutation_refusal(
        store,
        node,
        {
            "sql_digest": dbt_tests.model_sql_digest(node),
            "tests_digest": dbt_tests.own_tests_digest(manifest, node),
        },
        reason,
        keep_fresh_score=True,
    )


def _next_up(store, manifest, selector=None) -> list[str]:
    from exmergo_dex_core.maintain import coverage as coverage_mod

    found = coverage_mod.inventory(manifest, records=store.load_mutation_records())
    ordered = refresh_mod.order(refresh_mod.candidates(found), manifest, selector)
    return [c.label for c in ordered]


def test_successive_refusals_persist_and_rotate_the_retries(monkeypatch):
    from exmergo_dex_core.storage import MemoryStore

    manifest = _manifest()
    store = MemoryStore()
    _clock(
        monkeypatch,
        "2026-11-15T10:00:00+00:00",
        "2026-11-15T11:00:00+00:00",
        "2026-11-15T12:00:00+00:00",
    )
    _refuse(store, manifest, FCT)
    _refuse(store, manifest, MART)
    # Both refused once: the one refused longest ago goes first.
    assert _next_up(store, manifest, ["fct", "mart"]) == ["fct", "mart"]
    # The next run retries fct, which fails again and is recorded anew.
    _refuse(store, manifest, FCT)
    assert _next_up(store, manifest, ["fct", "mart"]) == ["mart", "fct"]


def test_a_kept_score_stale_downstream_is_retried_after_the_untried(monkeypatch):
    from exmergo_dex_core.storage import MemoryStore

    manifest = _manifest()
    store = MemoryStore()
    # A score whose own digests match, so a baseline refusal keeps it, but
    # whose downstream test has changed since: coverage reads it stale.
    store.save_mutation_record(
        _record(manifest, downstream_tests={"test.shop.unique_mart_id": "old"})
    )
    _clock(monkeypatch, "2026-11-15T10:00:00+00:00")
    _refuse(store, manifest, FCT)
    kept = store.load_mutation_records()[FCT]
    assert kept["score"] == 0.8 and kept["last_refusal_at"]
    # mart has never been measured; fct is stale and marked, so mart goes first.
    assert _next_up(store, manifest) == ["mart", "fct"]


def test_pending_uptime_counts_against_the_next_baseline(calls):
    calls["plan"]["confirmed"] = True
    calls["plan"]["baseline"] = 6.0

    class _Uptime:
        def pending(self):
            return 2.0

        def settle(self):
            return 0.0, []

    engine = _Engine("bigquery", budget=17.0)
    engine._adapter = lambda command: SimpleNamespace(
        cost_gate=None, warehouse_uptime=_Uptime()
    )
    # Pricing quotes through the stub; settling at the end reads the meter.
    block, *_ = refresh_mod.refresh(
        engine,
        _chain_manifest("a", "b"),
        _candidates(("a", "missing"), ("b", "missing")),
    )
    # a spends 10, leaving 7, less 2 owed: b's baseline of 6 does not fit.
    assert calls["runs"] == ["a"]
    assert block["not_run"] == [{"model": "b", "reason_code": "budget"}]


def test_a_run_checks_its_own_baseline_price_before_running_it(tmp_path):
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.storage import FilesystemStore
    from exmergo_dex_core.transform import mutation

    class _Shadow:
        tests = 0

        def write(self, *args, **kwargs):
            pass

        def test(self, model, **kwargs):
            self.tests += 1
            return {"success": True, "nodes": []}

    batch = mutation.enumerate_mutants(
        mutation.prepare("select a from t where a > 1", dialect="duckdb")
    )
    shadow = _Shadow()
    result = transform_commands._run_mutants(
        shadow,
        "models/fct.sql",
        batch,
        model="fct",
        paradigm=Paradigm.BYTES_SCANNED,
        connector="bigquery",
        store=FilesystemStore(tmp_path),
        ceiling=5.0,
        estimate=100.0,
        mutation_mod=mutation,
        prices={"(baseline)": 20.0},
    )
    _runs, _spend, warnings, _spent = result.pop("_meta")
    assert shadow.tests == 0
    assert result["counts"]["not_run"] == len(batch.mutants)
    assert any("the confirmed budget" in w for w in warnings)
