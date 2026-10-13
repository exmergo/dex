"""The cumulative ceiling meets a calibrated estimate, not a raw one (#324).

`budget.session_ceiling` caps the day's spend, and the day's spend is already
what commands billed. What still bit was the comparison at admission: each new
command's dry-run estimate, an upper bound on a partitioned or clustered
warehouse, met the day's real remainder raw. In the issue's numbers a build
billed 3.45 GB of a 6.70 GB estimate, and the 8.69 GB re-map that should follow
it was refused against headroom real spending never touched, on exactly the day
re-baselining is required.

The estimate is now scaled by the worst billed-over-estimated ratio this
connector's settled history shows before it meets the day's remainder. The
per-command `--budget` still meets the raw estimate, the warehouse is still
capped at the real remainder, and with too little history nothing changes.
"""

from __future__ import annotations

import math
from contextlib import contextmanager

import pytest

from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
)

GB = 1_000_000_000.0
SESSION_CEILING = 10 * GB
BUILD_BILLED = 3.45 * GB
REMAP_ESTIMATE = 8.69 * GB
REMAINING = SESSION_CEILING - BUILD_BILLED  # 6.55 GB of real headroom


def _history(ratios: list[float], connector: str = "bigquery") -> list[dict]:
    """One settled command per ratio, each estimated at 100."""

    return [
        {
            "at": "2026-10-12T10:00:00+00:00",
            "connector": connector,
            "command": "transform build",
            "entry": "settlement",
            "estimate": 100.0,
            "billed_bytes": 100.0 * ratio,
        }
        for ratio in ratios
    ]


# The issue's own two commands, plus a third so the history clears the minimum.
ISSUE_RATIOS = [3.45 / 6.70, 5.12 / 8.69, 0.55]


def _gate(entries: list[dict] | None, **overrides) -> CostGate:
    kwargs = {
        "paradigm": Paradigm.BYTES_SCANNED,
        "ceiling": 20 * GB,
        "session_ceiling": SESSION_CEILING,
        "session_spent": BUILD_BILLED,
        "confirmed": True,
        "connector": "bigquery",
        "command": "explore map",
        "history": None if entries is None else (lambda: list(entries)),
    }
    kwargs.update(overrides)
    return CostGate(**kwargs)


def test_a_full_remap_after_a_full_build_is_admitted_on_its_calibrated_estimate():
    """The acceptance in the issue: a day holding one full build and one full
    re-map does not exhaust a ceiling set above their combined billed cost."""

    gate = _gate(_history(ISSUE_RATIOS))

    cost = gate.preflight_command(REMAP_ESTIMATE)

    # The envelope still reports the real remainder the warehouse is capped at.
    assert cost.ceiling == REMAINING
    assert any("calibrated estimate" in w for w in gate.warnings())


def test_without_enough_history_the_raw_estimate_is_refused_as_before():
    gate = _gate(_history(ISSUE_RATIOS[:2]))

    with pytest.raises(OverCeilingError):
        gate.preflight_command(REMAP_ESTIMATE)
    assert not any("calibrated estimate" in w for w in gate.warnings())


def test_the_per_command_budget_still_meets_the_raw_estimate():
    """`--budget` is the handshake the caller confirmed; calibration never talks
    past it."""

    gate = _gate(_history(ISSUE_RATIOS), ceiling=8 * GB)

    with pytest.raises(OverCeilingError):
        gate.preflight_command(REMAP_ESTIMATE)


def test_the_warehouse_cap_stays_the_real_remainder():
    gate = _gate(_history(ISSUE_RATIOS))
    gate.preflight_command(REMAP_ESTIMATE)

    assert gate.statement_cap(unit="byte") == math.ceil(REMAINING)


def test_statements_inside_an_admitted_estimate_are_not_refused():
    gate = _gate(_history(ISSUE_RATIOS))
    gate.preflight_command(REMAP_ESTIMATE)

    gate.charge(REMAP_ESTIMATE / 2)
    gate.charge(REMAP_ESTIMATE / 2)


def test_a_phase_is_admitted_against_the_same_calibrated_bound():
    """Calibrating admission and not the phase would admit a command and then
    refuse it mid-run."""

    gate = _gate(_history(ISSUE_RATIOS))
    gate.preflight_command(5 * GB)
    gate.charge(5 * GB)

    # 8 GB in total: over the 6.55 GB raw remainder, under the calibrated one.
    gate.preflight_phase(3 * GB)


def test_a_phase_beyond_the_calibrated_bound_still_asks():
    gate = _gate(_history(ISSUE_RATIOS))
    gate.preflight_command(5 * GB)
    gate.charge(5 * GB)

    # 15 GB in total: over even the calibrated bound (about 11 GB).
    with pytest.raises(ConfirmationRequiredError):
        gate.preflight_phase(10 * GB)


@pytest.mark.parametrize(
    "ratios",
    [[1.2, 1.3, 1.5], [0.0, 0.0, 0.0]],
    ids=["ratio_over_one_never_tightens", "ratio_of_zero_is_ignored"],
)
def test_calibration_only_ever_loosens_the_daily_check(ratios):
    """A history billing over its estimates leaves the raw comparison in place
    rather than tightening it, and one billing nothing (all cache hits) says
    nothing about the next command."""

    gate = _gate(_history(ratios))

    assert gate.session_ratio() is None
    gate.preflight_command(6 * GB)  # under the raw remainder: admitted as before
    with pytest.raises(OverCeilingError):
        _gate(_history(ratios)).preflight_command(REMAP_ESTIMATE)


def test_another_connector_s_history_does_not_calibrate_this_one():
    gate = _gate(_history(ISSUE_RATIOS, connector="snowflake"))

    with pytest.raises(OverCeilingError):
        gate.preflight_command(REMAP_ESTIMATE)


def test_the_history_is_read_once_and_never_under_the_admission_lock():
    """A ledger read inside the lock would make every concurrent billed command
    wait on it."""

    held = False
    reads = 0

    @contextmanager
    def lock():
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    def history():
        nonlocal reads
        assert not held, "the history was read under the admission lock"
        reads += 1
        return _history(ISSUE_RATIOS)

    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=20 * GB,
        session_ceiling=SESSION_CEILING,
        session_spent=BUILD_BILLED,
        confirmed=True,
        connector="bigquery",
        command="explore map",
        record=lambda row: None,
        lock=lock,
        history=history,
    )

    gate.preflight_command(REMAP_ESTIMATE)
    gate.charge(1 * GB)
    gate.preflight_phase(0.5 * GB)

    assert reads == 1


def test_no_session_ceiling_reads_no_history():
    reads = 0

    def history():
        nonlocal reads
        reads += 1
        return _history(ISSUE_RATIOS)

    gate = _gate(None, session_ceiling=None, history=history)

    gate.preflight_command(REMAP_ESTIMATE)

    assert reads == 0
