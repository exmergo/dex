"""An over-ceiling refusal calibrated against this connector's settled history.

The refusal itself is not under test here and does not move: an estimate above
the ceiling is refused, and confirmation cannot talk past it. What is under test
is the sentence that follows it.

A dry-run estimate on a partitioned or clustered table is an upper bound by
construction, so the refused figure can sit well above what the work would have
billed. One observed build was refused at an estimated 6.9 GB against a 5 GB
ceiling and, re-run at a raised ceiling, billed 4.75 GB: the estimate was 45%
high and the refused build would have fit comfortably under the original
ceiling. "Raise the budget or narrow the work" is then answered by a guess made
under the impression that the estimate approximates the cost. dex already
records both halves of that comparison in `.dex/spend.jsonl`, so the refusal can
quote them.

Three properties the acceptance for issue #278 names, and each has a test below:
history produces a ratio, no history produces no ratio *and says so*, and the
ratio is per connector and never pooled.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    CALIBRATION_MINIMUM,
    CostGate,
    OverCeilingError,
    calibration_from_ledger,
    settled_ratios,
)


def _settlement(
    *,
    connector: str = "bigquery",
    estimate: float,
    billed: float,
    reservation: str | None = None,
    field: str = "billed_bytes",
) -> dict:
    entry = {
        "at": "2026-08-30T10:00:00+00:00",
        "connector": connector,
        "command": "transform build",
        "entry": "settlement",
        "estimate": estimate,
        field: billed,
    }
    if reservation is not None:
        entry["reservation_id"] = reservation
    return entry


def _history(ratios: list[float], *, connector: str = "bigquery") -> list[dict]:
    """One settled command per ratio, each estimated at 100."""

    return [
        _settlement(connector=connector, estimate=100.0, billed=100.0 * ratio)
        for ratio in ratios
    ]


def _gate(entries: list[dict], **overrides) -> CostGate:
    kwargs = {
        "paradigm": Paradigm.BYTES_SCANNED,
        "ceiling": 5_000_000_000.0,
        "session_ceiling": None,
        "session_spent": 0.0,
        "confirmed": True,
        "connector": "bigquery",
        "command": "transform build",
        "history": lambda: list(entries),
    }
    kwargs.update(overrides)
    return CostGate(**kwargs)


def _refusal(gate: CostGate, estimate: float = 6_905_293_058.0) -> str:
    with pytest.raises(OverCeilingError) as exc_info:
        gate.preflight_command(estimate)
    return str(exc_info.value)


# --- the ratio itself ---------------------------------------------------------


def test_a_refusal_with_settled_history_carries_the_observed_ratio():
    message = _refusal(_gate(_history([0.61, 0.66, 0.69, 0.72, 0.88])))
    assert "exceeds the ceiling" in message, (
        "the refusal must survive intact: a ceiling confirmation can override "
        f"is not a ceiling. Got {message!r}"
    )
    assert "median 69% of estimate" in message, message
    assert "range 61%-88%" in message, message
    assert "last 5 settled bigquery commands" in message, message


def test_the_refusal_names_the_budget_that_would_admit_it_not_the_ratio_of_it():
    """The trap the ratio alone sets, and the reason the clause says more.

    The ceiling binds on the estimate, not on what settles. A caller told only
    "you bill 69% of estimate" reasonably sets a budget at 69% of 6.9 GB and is
    refused a second time by arithmetic. So the clause names the figure that
    actually admits the command, and separately what it would then be expected
    to cost.
    """

    message = _refusal(_gate(_history([0.6, 0.7, 0.8])))
    assert "takes a budget above 6,905,293,058" in message, message
    assert "expected to bill around 4,833,705,141 bytes_scanned" in message, message


def test_the_median_is_taken_over_the_most_recent_window_only():
    """The ratio follows the project, not its whole history: a warehouse that
    was reclustered six months ago should not calibrate today's refusal."""

    old = _history([0.1] * 12)
    recent = _history([0.9] * 8)
    calibration = calibration_from_ledger(
        old + recent, connector="bigquery", field="billed_bytes"
    )
    assert calibration is not None
    assert calibration.samples == 8, calibration
    assert calibration.median == pytest.approx(0.9), calibration


# --- no history, and saying so ------------------------------------------------


def test_a_refusal_with_no_history_says_it_has_no_ratio():
    message = _refusal(_gate([]))
    assert "no settled bigquery command carrying an estimate" in message, message
    assert "no observed estimate-to-actual ratio" in message, message
    assert "median" not in message, (
        "a project with no history must not be handed a ratio, however "
        f"hedged. Got {message!r}"
    )


def test_two_data_points_are_too_few_to_be_a_ratio():
    """Issue #278 calls this out by name: a ratio invented from two commands is
    a coincidence dressed as evidence, and an operator has no way to tell."""

    message = _refusal(_gate(_history([0.61, 0.88])))
    assert "only 2 settled bigquery commands" in message, message
    assert "too few to draw a ratio from" in message, message
    assert "median" not in message, message


def test_the_minimum_is_the_boundary_it_says_it_is():
    field = "billed_bytes"
    below = _history([0.5] * (CALIBRATION_MINIMUM - 1))
    at = _history([0.5] * CALIBRATION_MINIMUM)
    assert calibration_from_ledger(below, connector="bigquery", field=field) is None, (
        "one short of the minimum must produce no ratio"
    )
    assert calibration_from_ledger(at, connector="bigquery", field=field) is not None, (
        "the minimum itself must produce one"
    )


def test_a_free_connector_refusal_gains_no_clause():
    """A FREE_LOCAL over-ceiling is the caller's own configuration
    contradiction, not a spend question, and nobody is sizing a budget from a
    DuckDB estimate."""

    gate = _gate([], paradigm=Paradigm.FREE_LOCAL, connector="duckdb", ceiling=1.0)
    message = _refusal(gate, estimate=10.0)
    assert "exceeds the ceiling" in message
    assert "ledger" not in message, message


def test_a_store_without_the_capability_leaves_the_refusal_exactly_as_it_was():
    message = _refusal(_gate([], history=None))
    assert "exceeds the ceiling" in message
    assert "ledger" not in message, message


def test_a_history_reader_that_fails_does_not_turn_a_refusal_into_something_else():
    """The refusal has already been decided and is the thing the caller needs.
    Losing a sentence to an unreadable ledger is the cheap failure; losing the
    refusal to it is not."""

    def explode() -> list[dict]:
        raise OSError("the ledger is on a share that went away")

    message = _refusal(_gate([], history=explode))
    assert "exceeds the ceiling" in message
    assert "median" not in message, message


# --- scoping ------------------------------------------------------------------


def test_a_duckdb_history_never_calibrates_a_bigquery_refusal():
    pooled = _history([0.1, 0.1, 0.1, 0.1], connector="duckdb") + _history(
        [0.8, 0.8, 0.8], connector="bigquery"
    )
    message = _refusal(_gate(pooled))
    assert "last 3 settled bigquery commands" in message, message
    assert "median 80% of estimate" in message, message


def test_a_seconds_history_never_calibrates_a_bytes_refusal():
    """One level below the connector filter, and it matters on a connector that
    changed paradigm or a ledger a host wrote by hand: a seconds figure divided
    by a bytes estimate is a number with no meaning at all."""

    seconds = [
        _settlement(estimate=100.0, billed=10.0, field="billed_seconds")
        for _ in range(5)
    ]
    assert (
        calibration_from_ledger(seconds, connector="bigquery", field="billed_bytes")
        is None
    ), "a billed_seconds history must not be read as a billed_bytes one"


# --- what counts as one data point --------------------------------------------


def test_one_command_of_six_statements_is_one_data_point_not_six():
    """A gate settles once per statement against one whole-command estimate.
    Counting each settlement as its own ratio would report six commands that
    each billed a sixth of their estimate, which is false twice over."""

    entries = [
        _settlement(estimate=600.0, billed=100.0, reservation="cmd-1") for _ in range(6)
    ]
    ratios = settled_ratios(entries, connector="bigquery", field="billed_bytes")
    assert ratios == [pytest.approx(1.0)], (
        "six settlements sharing a reservation_id are one command that billed "
        f"its whole estimate. Got {ratios!r}"
    )


def test_a_killed_command_is_not_counted():
    """A process killed outright leaves its reservation standing with no
    release, which is the ledger's own record that its settlements are partial.
    Counting one would drag the median down and calibrate the next refusal into
    a budget too small to admit anything."""

    entries = [
        {
            "at": "2026-08-30T09:00:00+00:00",
            "connector": "bigquery",
            "entry": "reservation",
            "reservation_id": "killed",
            "billed_bytes": 1_000.0,
        },
        _settlement(estimate=1_000.0, billed=50.0, reservation="killed"),
        {
            "at": "2026-08-30T09:30:00+00:00",
            "connector": "bigquery",
            "entry": "reservation",
            "reservation_id": "finished",
            "billed_bytes": 1_000.0,
        },
        _settlement(estimate=1_000.0, billed=700.0, reservation="finished"),
        {
            "at": "2026-08-30T09:40:00+00:00",
            "connector": "bigquery",
            "entry": "release",
            "reservation_id": "finished",
            "billed_bytes": -1_000.0,
        },
    ]
    ratios = settled_ratios(entries, connector="bigquery", field="billed_bytes")
    assert ratios == [pytest.approx(0.7)], (
        "a reservation with no release is a command that was killed mid-flight; "
        f"its partial settlement is not a ratio. Got {ratios!r}"
    )


def test_a_run_that_billed_nothing_is_not_counted():
    """A cache hit bills zero against a real estimate. True about that run,
    misleading about the next: the refused command will not hit the cache."""

    entries = [*_history([0.6, 0.7]), _settlement(estimate=100.0, billed=0.0)]
    ratios = settled_ratios(entries, connector="bigquery", field="billed_bytes")
    assert ratios == [pytest.approx(0.6), pytest.approx(0.7)], ratios


def test_entries_written_before_estimates_were_recorded_are_skipped():
    """Every ledger predating this carries settlements with no estimate. They
    are not zero-estimate commands, they are commands with nothing to compare
    against, and a project upgrading into this feature sees the no-history
    sentence until it has settled enough new commands."""

    legacy = [
        {
            "at": "2026-08-30T10:00:00+00:00",
            "connector": "bigquery",
            "entry": "settlement",
            "billed_bytes": 500.0,
        }
        for _ in range(9)
    ]
    assert settled_ratios(legacy, connector="bigquery", field="billed_bytes") == []


def test_a_non_numeric_estimate_is_skipped_rather_than_read_as_a_number():
    entries = [
        _settlement(estimate=100.0, billed=60.0),
        {
            "at": "2026-08-30T10:00:00+00:00",
            "connector": "bigquery",
            "entry": "settlement",
            "estimate": True,
            "billed_bytes": 1.0,
        },
        {
            "at": "2026-08-30T10:00:00+00:00",
            "connector": "bigquery",
            "entry": "settlement",
            "estimate": "6905293058",
            "billed_bytes": 1.0,
        },
    ]
    ratios = settled_ratios(entries, connector="bigquery", field="billed_bytes")
    assert ratios == [pytest.approx(0.6)], (
        "a boolean must not be read as an estimate of 1, and a stringified "
        f"number must not be parsed into one. Got {ratios!r}"
    )


# --- end to end, through a real store -----------------------------------------


def test_settled_commands_calibrate_the_next_refusal_through_a_real_store(tmp_path):
    """The whole loop on the wiring that ships: gates settle through
    `FilesystemStore`, and a later gate built by `new_cost_gate` reads their
    entries back off `.dex/spend.jsonl` without any of it being handed to it.

    Six commands are run under a ceiling that admits them, each priced at 1 GB
    and each billing 700 MB, which is the shape the issue reports: a dry-run
    upper bound consistently above what settles. The seventh asks for more than
    the ceiling allows, and the refusal quotes the six.
    """

    from exmergo_dex_core.config import Budget, DexConfig
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    config = DexConfig(connector="bigquery", budget=Budget(ceiling=2_000_000_000.0))

    def run(estimate: float, billed: float) -> None:
        gate = new_cost_gate(
            "bigquery", config, store, confirmed=True, command="transform build"
        )
        gate.preflight_command(estimate)
        gate.record_billed(billed, statement="select 1")
        gate.settle()

    for _ in range(6):
        run(1_000_000_000.0, 700_000_000.0)

    refused = new_cost_gate(
        "bigquery", config, store, confirmed=True, command="transform build"
    )
    with pytest.raises(OverCeilingError) as exc_info:
        refused.preflight_command(3_000_000_000.0)

    message = str(exc_info.value)
    assert "last 6 settled bigquery commands" in message, message
    assert "median 70% of estimate" in message, message
    assert "takes a budget above 3,000,000,000" in message, message
    assert "around 2,100,000,000 bytes_scanned" in message, message


def test_a_refused_command_leaves_no_trace_in_the_history_it_reads(tmp_path):
    """A refusal books nothing, which is already true of the ledger and has to
    stay true of the calibration: an estimate nothing ever settled against is
    not a data point, and a project that hit its ceiling five times must not
    have those five refusals shift the ratio it is shown on the sixth."""

    from exmergo_dex_core.config import Budget, DexConfig
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    config = DexConfig(connector="bigquery", budget=Budget(ceiling=1_000.0))

    for _ in range(5):
        gate = new_cost_gate("bigquery", config, store, confirmed=True)
        with pytest.raises(OverCeilingError):
            gate.preflight_command(5_000.0)

    assert store.spend_entries() == [], (
        "an over-ceiling refusal must touch the ledger not at all. Got "
        f"{store.spend_entries()!r}"
    )
