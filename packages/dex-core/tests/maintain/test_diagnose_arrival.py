"""``dex maintain diagnose``: a source whose rows arrived after the run (#1623).

Each fixture is the shop of :mod:`.test_diagnose_kinds`, with ``raw.orders``
given two timestamps: ``shipped_at`` (when the order shipped, its event time)
and ``loaded_at`` (when the loader landed its row, its capture time). The
source names ``loaded_at`` as its dbt ``loaded_at_field``, and ``fct_orders``
declares ``shipped_at`` as its time column. Four hourly runs: ``inv-1`` and
``inv-2`` pass, ``inv-3`` and ``inv-4`` fail, so the failing run's window is the
orders shipped after ``inv-2`` started and through ``inv-3``'s start, and the
last pass's window is the hour before ``inv-2``.

The pair: the window's rows landed after ``inv-3`` started (the cause, with the
late-row count and the run's start), and the same rows landing before it (no
cause). The fixture warehouse is the only thing written: no row is inserted into
any loader's source.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core import DexEngine
from exmergo_dex_core.results import to_envelope

from .test_diagnose_kinds import (
    FCT,
    RELATIONSHIPS,
    SOURCE,
    STG,
    Shop,
    _build,
    _diagnose,
    _kind,
    _one,
    _runs,
    _the_cause,
)

duckdb = pytest.importorskip("duckdb")

#: How the window's rows land: ``late`` after the failing run started,
#: ``on_time`` before it, ``late_both`` late at the last pass too.
ARRIVALS = ("late", "on_time", "late_both")

#: The orders of each window, and how many of the failing window's land late.
GOOD = range(1, 51)
BAD = range(51, 101)
LATE = range(91, 101)


def _sql(moment) -> str:
    return moment.isoformat(sep=" ")


def _arrive(
    tmp_path: Path,
    arrival: str = "late",
    *,
    shop: Shop | None = None,
    loaded_at_field: bool = True,
    capture_declared: bool = False,
    capture_basis: str = "capture",
    declare_event: bool = True,
    failing_freshness: bool = False,
    freshness_column: str = "shipped_at",
    backfill: bool = False,
) -> tuple[Path, Path]:
    """The shop, its source given an arrival history, and the manifest naming
    the columns."""

    shop = shop or Shop()
    if failing_freshness:
        shop.failing = FRESHNESS
    root, warehouse = _build(tmp_path, shop)
    [_, good, bad, _] = _runs()
    con = duckdb.connect(str(warehouse))
    con.execute("ALTER TABLE raw.orders ADD COLUMN shipped_at TIMESTAMP")
    con.execute("ALTER TABLE raw.orders ADD COLUMN loaded_at TIMESTAMP")
    for ids, shipped in (
        (GOOD, good.started - timedelta(minutes=30)),
        (BAD, bad.started - timedelta(minutes=30)),
    ):
        con.execute(
            "UPDATE raw.orders SET shipped_at = ?, loaded_at = ? "
            "WHERE order_id BETWEEN ? AND ?",
            [shipped, shipped + timedelta(minutes=10), ids.start, ids.stop - 1],
        )
    if arrival in ("late", "late_both"):
        con.execute(
            "UPDATE raw.orders SET loaded_at = ? WHERE order_id BETWEEN ? AND ?",
            [bad.started + timedelta(minutes=10), LATE.start, LATE.stop - 1],
        )
    if backfill:
        # Orders shipped before the last pass's window, loaded after it: no
        # run's window holds them, so they are nobody's late rows.
        con.execute(
            "UPDATE raw.orders SET shipped_at = ?, loaded_at = ? "
            "WHERE order_id BETWEEN 1 AND 10",
            [
                good.started - timedelta(minutes=90),
                good.started + timedelta(minutes=10),
            ],
        )
    if arrival == "late_both":
        con.execute(
            "UPDATE raw.orders SET loaded_at = ? WHERE order_id BETWEEN 41 AND 50",
            [good.started + timedelta(minutes=10)],
        )
    con.execute(
        "CREATE OR REPLACE TABLE main.stg_orders AS SELECT order_id, status_code, "
        "TRY_CAST(amount AS INTEGER) AS amount, shipped_at, loaded_at FROM raw.orders"
    )
    con.execute(
        "CREATE OR REPLACE TABLE main.fct_orders AS SELECT order_id, status_code, "
        "amount, 'placed' AS label, shipped_at FROM main.stg_orders"
    )
    con.close()

    path = root / "target" / "manifest.json"
    manifest = json.loads(path.read_text())
    nodes = manifest["nodes"]
    stg, fct = nodes[STG], nodes[FCT]
    stg["raw_code"] = (
        "select order_id, status_code, amount, shipped_at, loaded_at "
        "from {{ source('raw', 'orders') }}"
    )
    stg["columns"]["shipped_at"] = {"name": "shipped_at", "data_type": None}
    stg["columns"]["loaded_at"] = {"name": "loaded_at", "data_type": None}
    if capture_declared:
        stg["columns"]["loaded_at"]["config"] = {
            "meta": {"dex": {"role": "time", "freshness": {"basis": capture_basis}}}
        }
    fct["raw_code"] = (
        "select o.order_id, o.status_code, o.amount, d.label, o.shipped_at "
        "from {{ ref('stg_orders') }} o left join {{ ref('dim_status') }} d "
        "using (status_code)"
    )
    fct["columns"]["shipped_at"] = {"name": "shipped_at", "data_type": None}
    if declare_event:
        fct["columns"]["shipped_at"]["config"] = {
            "meta": {
                "dex": {
                    "role": "time",
                    "freshness": {"max_lag": "6h", "basis": "business"},
                }
            }
        }
    if loaded_at_field:
        manifest["sources"][SOURCE]["loaded_at_field"] = "loaded_at"
    nodes[FRESHNESS] = {
        "unique_id": FRESHNESS,
        "resource_type": "test",
        "package_name": "shop",
        "name": "dex_freshness_fct_orders_shipped_at",
        "column_name": freshness_column,
        "original_file_path": "models/schema.yml",
        "config": {"enabled": True, "severity": "ERROR"},
        "test_metadata": {
            "name": "dex_freshness",
            "kwargs": {"column_name": freshness_column, "max_lag_seconds": 21600},
        },
        "depends_on": {"nodes": [FCT], "macros": []},
        "attached_node": FCT,
    }
    path.write_text(json.dumps(manifest))
    return root, warehouse


FRESHNESS = "test.shop.dex_freshness_fct_orders_shipped_at.9f8e7d"


def _late(diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    return _kind(diagnosis, "source_arrived_late")


def test_rows_of_the_window_landing_after_the_run_are_the_loader_cause(
    tmp_path: Path,
):
    root, warehouse = _arrive(tmp_path, "late")
    data = _diagnose(root, warehouse)
    diagnosis = _one(data)
    cause = _the_cause(diagnosis, "source_arrived_late")
    [_, _, bad, _] = _runs()

    assert cause["owner"] == "loader"
    assert cause["relation"] == "dev.raw.orders"
    assert cause["columns"] == ["shipped_at", "loaded_at"]
    assert cause["check"] == "source capture time against the bounding runs' starts"
    evidence = cause["evidence"]
    assert evidence["late_rows"] == len(LATE)
    assert evidence["capture_column"] == "loaded_at"
    assert evidence["capture_source"] == "loaded_at_field"
    assert evidence["event_column"] == "shipped_at"
    assert evidence["event_source"] == "declaration fct_orders.shipped_at"
    failing = evidence["failing_run"]
    assert failing["run"] == "inv-3"
    assert failing["started_at"].startswith(_sql(bad.started).replace(" ", "T"))
    assert failing["window_rows"] == len(BAD)
    assert failing["late_rows"] == len(LATE)
    passing = evidence["last_passing_run"]
    assert passing["run"] == "inv-2"
    assert (passing["window_rows"], passing["late_rows"]) == (len(GOOD), 0)
    [step] = [
        s for s in diagnosis["next_steps"] if "freshness.deadline" in s["purpose"]
    ]
    assert step["command"] == "dex maintain incidents fct_orders"
    assert f"{len(LATE)} row(s)" in step["purpose"]
    # Priced in the checks' one handshake, and no value from the data.
    assert any("capture time" in key for key in data["price"]["checks"]["per_table"])
    text = json.dumps(data)
    assert _sql(bad.started + timedelta(minutes=10)) not in text
    assert '"placed"' not in text


def test_the_same_rows_landing_before_the_run_report_no_cause(tmp_path: Path):
    root, warehouse = _arrive(tmp_path, "on_time")
    diagnosis = _one(_diagnose(root, warehouse))

    assert diagnosis["verdict"] == "no_cause", diagnosis["causes"]
    [held] = _late(diagnosis)
    assert (held["status"], held["reason"]) == ("ruled_out", "arrived_before_run")
    assert held["evidence"]["failing_run"]["window_rows"] == len(BAD)
    assert held["evidence"]["late_rows"] == 0


def test_lateness_already_there_at_the_last_pass_is_ruled_out(tmp_path: Path):
    root, warehouse = _arrive(tmp_path, "late_both")
    diagnosis = _one(_diagnose(root, warehouse))

    [held] = _late(diagnosis)
    assert (held["status"], held["reason"]) == ("ruled_out", "lateness_unchanged")
    assert held["evidence"]["last_passing_run"]["late_rows"] == 10


def test_with_no_capture_column_the_source_is_not_checked_never_ruled_out(
    tmp_path: Path,
):
    root, warehouse = _arrive(tmp_path, "late", loaded_at_field=False)
    diagnosis = _one(_diagnose(root, warehouse))

    assert diagnosis["verdict"] == "no_cause", diagnosis["causes"]
    [held] = _late(diagnosis)
    assert (held["status"], held["reason"]) == ("not_checked", "no_capture_time")
    assert held not in diagnosis["ruled_out"]


def test_a_capture_column_a_staging_model_declares_is_read(tmp_path: Path):
    root, warehouse = _arrive(
        tmp_path, "late", loaded_at_field=False, capture_declared=True
    )
    cause = _the_cause(_one(_diagnose(root, warehouse)), "source_arrived_late")

    assert cause["evidence"]["capture_source"] == "declaration stg_orders.loaded_at"
    assert cause["evidence"]["late_rows"] == len(LATE)


def test_a_test_with_no_time_column_to_read_has_an_unknown_window(tmp_path: Path):
    root, warehouse = _arrive(tmp_path, "late", declare_event=False)
    [held] = _late(_one(_diagnose(root, warehouse)))

    assert (held["status"], held["reason"]) == ("not_checked", "window_unknown")


def test_a_freshness_test_reads_its_window_from_its_own_column(tmp_path: Path):
    root, warehouse = _arrive(
        tmp_path, "late", declare_event=False, failing_freshness=True
    )
    cause = _the_cause(_one(_diagnose(root, warehouse)), "source_arrived_late")

    assert cause["evidence"]["event_source"] == "the test's column_name"
    assert cause["evidence"]["late_rows"] == len(LATE)


def test_a_late_source_and_a_model_change_in_one_range_name_only_the_confirmed(
    tmp_path: Path,
):
    # The range also edits fct_orders, which verify then finds sound.
    root, warehouse = _arrive(
        tmp_path,
        "late",
        shop=Shop(
            edits={"models/fct_orders.sql": "select * from stg_orders -- tidy\n"}
        ),
    )
    diagnosis = _one(_diagnose(root, warehouse))
    cause = _the_cause(diagnosis, "source_arrived_late")
    [project] = _kind(diagnosis, "project_change")

    assert cause["owner"] == "loader"
    assert (project["status"], project["reason"]) == ("ruled_out", "verify_clean")


def test_a_uniqueness_failure_is_not_answered_by_this_kind(tmp_path: Path):
    from .test_diagnose_kinds import COMBO

    root, warehouse = _arrive(tmp_path, "late", shop=Shop(failing=COMBO))

    assert _late(_one(_diagnose(root, warehouse))) == []


def test_a_freshness_failure_in_the_range_corroborates_without_deciding(
    tmp_path: Path,
):
    from .test_incident_sources import SCHEMA

    root, warehouse = _arrive(tmp_path, "on_time")
    # The freshness test failed at the failing run too: a second incident.
    con = duckdb.connect(str(warehouse))
    columns = [
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = 'elementary_test_results' "
            "ORDER BY ordinal_position",
            [SCHEMA],
        ).fetchall()
    ]
    rows = con.execute(
        f"SELECT * FROM {SCHEMA}.elementary_test_results"  # noqa: S608
    ).fetchall()
    for row in rows:
        held = dict(zip(columns, row, strict=True))
        held["id"] = f"{held['id']}-f"
        held["test_unique_id"] = FRESHNESS
        held["test_short_name"] = "dex_freshness"
        held["test_type"] = "generic"
        marks = ", ".join("?" for _ in columns)
        con.execute(
            f"INSERT INTO {SCHEMA}.elementary_test_results VALUES ({marks})",  # noqa: S608
            [held[c] for c in columns],
        )
    con.close()
    data = _diagnose(root, warehouse)
    by_subject = {d["subject"]: d for d in data["diagnoses"]}
    assert len(by_subject) == 2
    relationships = next(d for d in data["diagnoses"] if RELATIONSHIPS in d["tests"])
    [held] = _late(relationships)

    # Corroborated, and still ruled out by the counts.
    assert held["reason"] == "arrived_before_run"
    assert held["evidence"]["corroborated_by"][0]["test"] == "dex_freshness"


def test_counts_that_do_not_fit_beside_the_grain_checks_are_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.maintain import arrival, run_times

    root, warehouse = _arrive(tmp_path, "late")
    engine = DexEngine(connector="duckdb", path=str(warehouse), repo_root=str(root))
    opened = engine._adapter

    def gated(*args, **kwargs):
        adapter = opened(*args, **kwargs)
        adapter.cost_gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=1_000.0,
            session_ceiling=100_000_000.0,
            session_spent=0.0,
            confirmed=True,
            connector="bigquery",
            command="maintain diagnose",
        )
        return adapter

    monkeypatch.setattr(engine, "_adapter", gated)
    monkeypatch.setattr(
        arrival, "price", lambda engine, adapter, probes: (700.0, {"count": 700.0})
    )
    monkeypatch.setattr(
        run_times, "price", lambda engine, adapter, held: (800.0, {"read": 800.0})
    )
    data = to_envelope(engine.diagnose()).data
    diagnosis = _one(data)
    checks = data["price"]["checks"]

    # The count fits alone and runs; the build-times read beside it does not.
    _the_cause(diagnosis, "source_arrived_late")
    assert checks["schedule_offered"]["estimate"] == 800.0
    assert "arrival_offered" not in checks

    monkeypatch.setattr(
        arrival, "price", lambda engine, adapter, probes: (1_200.0, {"count": 1.2e3})
    )
    data = to_envelope(engine.diagnose()).data
    [held] = _late(_one(data))
    assert (held["status"], held["reason"]) == ("not_checked", "scan_offered")
    assert data["price"]["checks"]["offered"] is True


def test_rows_shipped_before_either_window_are_not_counted_late(tmp_path: Path):
    root, warehouse = _arrive(tmp_path, "late", backfill=True)
    cause = _the_cause(_one(_diagnose(root, warehouse)), "source_arrived_late")

    passing = cause["evidence"]["last_passing_run"]
    assert (passing["window_rows"], passing["late_rows"]) == (len(GOOD) - 10, 0)


def test_a_business_time_column_is_never_read_as_the_capture_time(tmp_path: Path):
    root, warehouse = _arrive(
        tmp_path,
        "late",
        loaded_at_field=False,
        capture_declared=True,
        capture_basis="business",
    )
    [held] = _late(_one(_diagnose(root, warehouse)))

    assert (held["status"], held["reason"]) == ("not_checked", "no_capture_time")


def test_a_freshness_test_on_the_capture_column_has_an_unknown_window(
    tmp_path: Path,
):
    root, warehouse = _arrive(
        tmp_path,
        "late",
        declare_event=False,
        failing_freshness=True,
        freshness_column="loaded_at",
    )
    [held] = _late(_one(_diagnose(root, warehouse)))

    assert (held["status"], held["reason"]) == ("not_checked", "window_unknown")
    assert "capture time itself" in held["detail"]


def test_a_freshness_test_on_the_capture_column_reads_the_model_s_event_time(
    tmp_path: Path,
):
    root, warehouse = _arrive(
        tmp_path, "late", failing_freshness=True, freshness_column="loaded_at"
    )
    cause = _the_cause(_one(_diagnose(root, warehouse)), "source_arrived_late")

    assert cause["evidence"]["event_source"] == "declaration fct_orders.shipped_at"
    assert cause["evidence"]["late_rows"] == len(LATE)


def test_a_freshness_test_s_lag_counts_a_late_row_from_before_the_last_pass(
    tmp_path: Path,
):
    """The review's case: a six-hour freshness check on hourly runs. A row
    shipped before the last pass, still missing at the failing run, is one the
    test could have counted there: late for the failure, and not lateness the
    pass already ran with, since it had not landed by the failing run."""

    root, warehouse = _arrive(
        tmp_path, "on_time", declare_event=False, failing_freshness=True
    )
    [_, _, bad, _] = _runs()
    con = duckdb.connect(str(warehouse))
    con.execute(
        "UPDATE raw.orders SET loaded_at = ? WHERE order_id BETWEEN 31 AND 40",
        [bad.started + timedelta(minutes=10)],
    )
    con.close()
    cause = _the_cause(_one(_diagnose(root, warehouse)), "source_arrived_late")

    assert cause["evidence"]["window_basis"] == "max_lag (21600s)"
    assert cause["evidence"]["late_rows"] == 10
    # Each window is the six hours before its run's start.
    failing = cause["evidence"]["failing_run"]["window"]
    assert failing["after"].startswith((bad.started - timedelta(hours=6)).isoformat())
    assert cause["evidence"]["last_passing_run"]["late_rows"] == 0


def test_a_source_whose_columns_cannot_be_read_is_not_checked_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.errors import DexError

    root, warehouse = _arrive(tmp_path, "late")
    real = DuckDBAdapter.table_metadata

    def refuse(self, identifier, *args, **kwargs):
        if identifier.endswith("raw.orders"):
            raise DexError("value 'secret' could not be read")
        return real(self, identifier, *args, **kwargs)

    monkeypatch.setattr(DuckDBAdapter, "table_metadata", refuse)
    diagnosis = _one(_diagnose(root, warehouse))

    [held] = _late(diagnosis)
    assert (held["status"], held["reason"]) == ("not_checked", "source_not_listed")
    assert diagnosis["verdict"] == "no_cause"
    # Only the error's type is reported, never its text.
    assert "secret" not in json.dumps(diagnosis)
    assert "DexError" in held["detail"]


def test_a_read_error_s_text_is_masked_in_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from exmergo_dex_core.errors import DexError
    from exmergo_dex_core.maintain import arrival

    root, warehouse = _arrive(tmp_path, "late")
    monkeypatch.setattr(
        arrival,
        "read",
        lambda engine, adapter, probes, command: [
            DexError("Conversion Error: could not convert 'hunter2'") for _ in probes
        ],
    )
    [held] = _late(_one(_diagnose(root, warehouse)))

    assert (held["status"], held["reason"]) == ("not_checked", "warehouse_refused")
    assert "hunter2" not in held["detail"]
