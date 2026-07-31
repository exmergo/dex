"""Cost-guard behavior: preflight-before-spend, in check order."""

from __future__ import annotations

import pytest

from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    CeilingRequiredError,
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
    preflight,
)


def test_over_ceiling_blocks_even_when_confirmed():
    with pytest.raises(OverCeilingError):
        preflight(10_000, 10, paradigm=Paradigm.BYTES_SCANNED, confirmed=True)


def test_billed_paradigm_requires_a_ceiling():
    with pytest.raises(CeilingRequiredError):
        preflight(5, None, paradigm=Paradigm.COMPUTE_TIME, confirmed=True)


def test_unconfirmed_raises_with_the_cost_attached():
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        preflight(0.0, 1.0, paradigm=Paradigm.FREE_LOCAL, confirmed=False)
    cost = exc_info.value.cost
    assert cost.paradigm is Paradigm.FREE_LOCAL
    assert cost.estimate == 0.0
    assert cost.ceiling == 1.0


def test_free_local_confirmed_passes_without_a_budget():
    cost = preflight(0.0, None, paradigm=Paradigm.FREE_LOCAL, confirmed=True)
    assert cost.paradigm is Paradigm.FREE_LOCAL
    assert cost.ceiling is None


def test_billed_paradigm_within_ceiling_and_confirmed_passes():
    cost = preflight(5, 100, paradigm=Paradigm.BYTES_SCANNED, confirmed=True)
    assert cost.estimate == 5
    assert cost.ceiling == 100


# --- CostGate ------------------------------------------------------------------


def _gate(**overrides) -> CostGate:
    kwargs = {
        "paradigm": Paradigm.BYTES_SCANNED,
        "ceiling": 1_000.0,
        "session_ceiling": None,
        "session_spent": 0.0,
        "confirmed": True,
        "connector": "bigquery",
        "command": "explore profile",
    }
    kwargs.update(overrides)
    return CostGate(**kwargs)


def test_gate_handshake_unconfirmed_carries_the_estimate():
    gate = _gate(confirmed=False)
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        gate.preflight_command(500.0)
    assert exc_info.value.cost.estimate == 500.0
    assert exc_info.value.cost.ceiling == 1_000.0
    assert exc_info.value.cost.paradigm is Paradigm.BYTES_SCANNED


def test_gate_over_ceiling_cannot_be_confirmed_through():
    gate = _gate(confirmed=True)
    with pytest.raises(OverCeilingError):
        gate.preflight_command(2_000.0)


def test_gate_confirmed_run_requires_a_ceiling_on_billed_paradigms():
    gate = _gate(ceiling=None, session_ceiling=None, confirmed=True)
    with pytest.raises(CeilingRequiredError):
        gate.preflight_command(1.0)


def test_gate_unconfirmed_without_a_ceiling_asks_for_confirmation():
    # The first call is how the agent learns the estimate; it cannot have
    # picked a budget yet, so the handshake, not CeilingRequired, answers.
    gate = _gate(ceiling=None, session_ceiling=None, confirmed=False)
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        gate.preflight_command(500.0)
    assert exc_info.value.cost.estimate == 500.0
    assert exc_info.value.cost.ceiling is None


def test_gate_charges_accumulate_to_the_ceiling():
    gate = _gate()
    gate.charge(600.0)
    with pytest.raises(OverCeilingError):
        gate.charge(600.0)
    # The failed charge did not count; a fitting one still passes.
    gate.charge(300.0)


def test_gate_try_charge_degrades_instead_of_raising():
    gate = _gate()
    assert gate.try_charge(900.0) is True
    assert gate.try_charge(200.0) is False


def test_gate_session_remainder_binds_when_tighter():
    gate = _gate(ceiling=1_000.0, session_ceiling=800.0, session_spent=500.0)
    assert gate.effective_ceiling() == 300.0
    with pytest.raises(OverCeilingError):
        gate.preflight_command(400.0)


def test_gate_phase_within_remaining_headroom_passes():
    gate = _gate()
    gate.charge(600.0)
    cost = gate.preflight_phase(300.0)
    assert cost.estimate == 900.0
    assert cost.ceiling == 1_000.0


def test_gate_phase_beyond_remaining_headroom_asks_for_confirmation():
    # The raised estimate is the whole-command total (charged so far plus the
    # phase), so it is directly the budget a re-run needs.
    gate = _gate()
    gate.charge(600.0)
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        gate.preflight_phase(500.0)
    assert exc_info.value.cost.estimate == 1_100.0
    assert exc_info.value.cost.ceiling == 1_000.0
    assert exc_info.value.cost.paradigm is Paradigm.BYTES_SCANNED


def test_gate_phase_session_remainder_binds_when_tighter():
    gate = _gate(ceiling=1_000.0, session_ceiling=800.0, session_spent=500.0)
    with pytest.raises(ConfirmationRequiredError) as exc_info:
        gate.preflight_phase(400.0)
    assert exc_info.value.cost.ceiling == 300.0


def test_gate_phase_zero_estimate_and_no_ceiling_never_raise():
    assert _gate().preflight_phase(0.0).estimate == 0.0
    gate = _gate(ceiling=None, session_ceiling=None)
    assert gate.preflight_phase(500.0).ceiling is None


def test_gate_max_bytes_tracks_actual_billing():
    gate = _gate(ceiling=1_000.0)
    assert gate.remaining_for_statement() == 1_000
    gate.record_billed(400.0, statement="SELECT 1")
    assert gate.remaining_for_statement() == 600


def test_gate_ledger_entries_carry_hashes_never_sql():
    entries: list[dict] = []
    gate = _gate(record=entries.append)
    gate.record_billed(123.0, job_id="job-1", statement="SELECT secret FROM t")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["billed_bytes"] == 123.0
    assert entry["connector"] == "bigquery"
    assert entry["job_id"] == "job-1"
    assert "SELECT" not in str(entry.values())
    assert entry["statement_sha256"]


def test_gate_spend_summary_reports_actuals_not_estimates():
    gate = _gate(session_spent=50.0)
    gate.charge(700.0)
    gate.record_billed(100.0)
    summary = gate.spend_summary()
    assert summary == {"bytes_billed": 100.0, "session_spent_today": 150.0}


def test_gate_cost_prefers_the_command_estimate():
    gate = _gate()
    gate.preflight_command(500.0)
    gate.charge(200.0)
    assert gate.cost().estimate == 500.0


def test_db_load_ledger_records_seconds():
    # DB_LOAD is a time paradigm: its ledger unit and spend key are seconds,
    # never bytes, so a Postgres entry can never sum into a bytes budget.
    entries: list[dict] = []
    gate = _gate(
        paradigm=Paradigm.DB_LOAD,
        connector="postgres",
        record=entries.append,
    )
    assert gate.ledger_field() == "billed_seconds"
    gate.charge(10.0)
    gate.record_billed(3.5, statement="SELECT 1")
    assert entries[0]["billed_seconds"] == 3.5
    assert "billed_bytes" not in entries[0]
    summary = gate.spend_summary()
    assert summary["seconds_billed"] == 3.5
    assert "bytes_billed" not in summary


# --- what the guard says about its own reach -------------------------------------


def test_a_billed_gate_with_no_cumulative_cap_warns():
    """The per-command ceiling is refused when missing; the cumulative one is
    not, and from outside the two look equally enforced.

    A warning rather than a refusal, because refusing would break every
    existing user who never set one. But silence would leave an unset daily cap
    indistinguishable from one that bound.
    """

    warning = _gate(session_ceiling=None).warnings()
    assert len(warning) == 1
    assert "budget.session_ceiling" in warning[0]
    # The compounding half of the field report: two repo roots, one budget, and
    # no way to tell from inside the second one that it is not covered.
    assert "this repo root only" in warning[0]


def test_a_billed_gate_with_a_cumulative_cap_stays_quiet():
    assert _gate(session_ceiling=10_000.0).warnings() == []


def test_a_free_gate_never_warns_about_a_cumulative_cap():
    # DuckDB bills nothing, so a daily spend cap is not a thing it is missing.
    assert _gate(paradigm=Paradigm.FREE_LOCAL, session_ceiling=None).warnings() == []


def test_the_ledger_and_envelope_spellings_stay_distinct():
    """`billed_bytes` goes to the ledger, `bytes_billed` to the envelope.

    Both are load-bearing where they are, and `transform build` settles outside
    the gate, so the pair is derived from the paradigm in one place rather than
    respelled at each site that needs one of them.
    """

    from exmergo_dex_core.guards.cost_guard import ledger_field, spend_field

    for paradigm, ledger, envelope in (
        (Paradigm.BYTES_SCANNED, "billed_bytes", "bytes_billed"),
        (Paradigm.COMPUTE_TIME, "billed_seconds", "seconds_billed"),
        (Paradigm.DB_LOAD, "billed_seconds", "seconds_billed"),
    ):
        assert ledger_field(paradigm) == ledger
        assert spend_field(paradigm) == envelope
