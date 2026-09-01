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
        preflight(5, 10, paradigm=Paradigm.BYTES_SCANNED, confirmed=False)
    cost = exc_info.value.cost
    assert cost.paradigm is Paradigm.BYTES_SCANNED
    assert cost.estimate == 5
    assert cost.ceiling == 10


def test_free_local_confirmed_passes_without_a_budget():
    cost = preflight(0.0, None, paradigm=Paradigm.FREE_LOCAL, confirmed=True)
    assert cost.paradigm is Paradigm.FREE_LOCAL
    assert cost.ceiling is None


def test_free_local_unconfirmed_never_raises():
    """Issue #197: a confirmation handshake is emitted only where spend is
    possible. FREE_LOCAL cannot bill, so an unconfirmed call passes exactly
    like a confirmed one, with or without a ceiling configured."""

    cost = preflight(0.0, None, paradigm=Paradigm.FREE_LOCAL, confirmed=False)
    assert cost.paradigm is Paradigm.FREE_LOCAL

    cost = preflight(0.0, 1.0, paradigm=Paradigm.FREE_LOCAL, confirmed=False)
    assert cost.paradigm is Paradigm.FREE_LOCAL


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


# --- issue #197: no confirmation handshake where nothing can bill --------------
#
# No adapter attaches a CostGate for a free connector today (DuckDB has none),
# so these are the defensive half of the rule: even a gate misattached to
# FREE_LOCAL must never ask for confirmation of spend that cannot happen.


def test_gate_free_local_command_handshake_never_raises_unconfirmed():
    gate = _gate(
        paradigm=Paradigm.FREE_LOCAL,
        ceiling=None,
        session_ceiling=None,
        confirmed=False,
    )
    cost = gate.preflight_command(0.0)
    assert cost.paradigm is Paradigm.FREE_LOCAL


def test_gate_free_local_command_handshake_skips_ceiling_required_too():
    # A ceiling is optional on FREE_LOCAL: unset, unconfirmed, still passes.
    gate = _gate(
        paradigm=Paradigm.FREE_LOCAL,
        ceiling=None,
        session_ceiling=None,
        confirmed=False,
    )
    cost = gate.preflight_command(0.0)
    assert cost.ceiling is None


def test_gate_free_local_command_handshake_still_blocks_over_a_configured_ceiling():
    # Over-ceiling binds regardless of paradigm: an estimate that contradicts
    # an explicitly configured ceiling is refused even though nothing bills.
    gate = _gate(paradigm=Paradigm.FREE_LOCAL, ceiling=1.0, confirmed=False)
    with pytest.raises(OverCeilingError):
        gate.preflight_command(1_000_000.0)


def test_gate_free_local_phase_handshake_never_raises():
    gate = _gate(paradigm=Paradigm.FREE_LOCAL, ceiling=1.0, confirmed=True)
    cost = gate.preflight_phase(1_000_000.0)
    assert cost.paradigm is Paradigm.FREE_LOCAL


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
    assert gate.statement_cap(unit="byte") == 1_000
    gate.record_billed(400.0, statement="SELECT 1")
    assert gate.statement_cap(unit="byte") == 600


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


def test_a_gate_whose_store_cannot_serialize_the_admission_warns():
    """A ceiling that cannot bind under concurrency has to say so.

    Both shipped backends implement the lock, so this reaches only a host that
    selected a backend of its own. It warns rather than refusing, the same call
    made for a missing cumulative cap: refusing would break a backend written
    before the capability existed.
    """

    warning = _gate(
        session_ceiling=10_000.0, record=lambda entry: None, lock=None
    ).warnings()
    assert len(warning) == 1
    assert "spend lock" in warning[0]
    assert "budget.session_ceiling" in warning[0]


def test_a_gate_whose_store_serializes_the_admission_stays_quiet():
    from contextlib import nullcontext

    gate = _gate(session_ceiling=10_000.0, record=lambda entry: None, lock=nullcontext)
    assert gate.serialized is True
    assert gate.warnings() == []


def test_a_gate_with_no_ledger_never_warns_about_serializing_one():
    """Nothing is appended, so nothing can be raced.

    This is every free path and every gate built without a store. Warning here
    would report a hazard that does not exist, on the commands least able to act
    on it.
    """

    assert _gate(session_ceiling=10_000.0, record=None).warnings() == []


# --- reserving the headroom a command was admitted against -----------------------


class _Ledger:
    """The smallest thing that behaves like a shared spend ledger.

    A list plus the two members the gate reaches for, so these assertions are
    about the gate rather than about a backend. `FilesystemStore` is exercised
    against the real thing in the concurrency suite.
    """

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def append(self, entry: dict) -> None:
        self.entries.append(dict(entry))

    def total(self) -> float:
        return sum(e.get("billed_bytes", 0.0) for e in self.entries)

    def kinds(self) -> list[str]:
        return [e["entry"] for e in self.entries]


def _ledger_gate(ledger: _Ledger, **overrides) -> CostGate:
    from contextlib import nullcontext

    kwargs = {
        "session_ceiling": 1_000.0,
        "session_spent": ledger.total,
        "record": ledger.append,
        "lock": nullcontext,
    }
    kwargs.update(overrides)
    return _gate(**kwargs)


def test_an_admitted_command_books_its_estimate_before_it_runs():
    """The hold is what a concurrent command settles against.

    Without it the second command reads a ledger that will not show the first
    one's spend until it finishes, and admits itself against headroom that is
    already committed.
    """

    ledger = _Ledger()
    _ledger_gate(ledger).preflight_command(600.0)
    assert ledger.kinds() == ["reservation"]
    assert ledger.total() == 600.0


def test_a_refused_command_books_nothing():
    """Every refusal leaves through the exception before the reservation.

    The common case is the unconfirmed handshake, which is how every billed
    command starts, so a gate that reserved on the way to asking would hold
    headroom for work that was never authorized.
    """

    ledger = _Ledger()
    with pytest.raises(ConfirmationRequiredError):
        _ledger_gate(ledger, confirmed=False).preflight_command(10.0)
    with pytest.raises(OverCeilingError):
        _ledger_gate(ledger).preflight_command(5_000.0)
    with pytest.raises(CeilingRequiredError):
        _ledger_gate(ledger, ceiling=None, session_ceiling=None).preflight_command(10.0)
    assert ledger.entries == []


def test_a_second_gate_sees_the_first_ones_hold():
    ledger = _Ledger()
    _ledger_gate(ledger).preflight_command(600.0)
    with pytest.raises(OverCeilingError):
        _ledger_gate(ledger).preflight_command(600.0)


def test_settling_releases_what_was_held_but_not_spent():
    """The ledger nets back to actual spend, which is what makes this safe.

    A day already finished sums to what it really cost, exactly as before
    reservations existed, so nothing summing settled spend had to change.
    """

    ledger = _Ledger()
    gate = _ledger_gate(ledger)
    gate.preflight_command(600.0)
    gate.record_billed(400.0)
    gate.settle()
    assert ledger.kinds() == ["reservation", "settlement", "release"]
    assert ledger.total() == 400.0


def test_the_freed_headroom_admits_the_next_command():
    ledger = _Ledger()
    first = _ledger_gate(ledger)
    first.preflight_command(600.0)
    first.record_billed(400.0)
    first.settle()
    # 1,000 ceiling less 400 actually spent, not less the 600 that was held.
    assert _ledger_gate(ledger).preflight_command(600.0).ceiling == 600.0


def test_a_gate_does_not_charge_itself_for_its_own_reservation():
    """The invariant the whole design rests on.

    A live reading includes this gate's own writes, so `session_spent` is that
    reading net of them: the day's spend belonging to other commands. Without
    the subtraction every ceiling after the reservation would tighten by the
    gate's own estimate, and a command would refuse itself partway through.
    """

    ledger = _Ledger()
    gate = _ledger_gate(ledger)
    gate.preflight_command(600.0)
    assert gate.session_spent == 0.0
    assert gate.effective_ceiling() == 1_000.0
    gate.record_billed(400.0)
    gate._refresh()
    assert gate.session_spent == 0.0


def test_an_estimate_that_drifts_past_its_booking_is_re_admitted():
    """The gap a live BigQuery run exposed after the reservation landed.

    A command is bounded per statement by the ceiling rather than by its own
    estimate, deliberately, so a drifting estimate stops mid-command instead of
    overrunning. Under concurrency that bound was computed from the reading the
    command was admitted on, which cannot see headroom a later command took. So
    drift could spend into another command's hold even though every admission
    was correct.
    """

    ledger = _Ledger()
    gate = _ledger_gate(ledger)
    gate.preflight_command(300.0)
    gate.charge(200.0)
    assert ledger.kinds() == ["reservation"]  # inside the booking: no re-admit

    # Another command takes headroom after this one was admitted.
    other = _ledger_gate(ledger)
    other.preflight_command(600.0)

    # Drifting past the 300 booked now has to fit what is genuinely left, and
    # 300 + 600 already accounts for the whole 1,000 ceiling.
    with pytest.raises(OverCeilingError):
        gate.charge(400.0)
    # The refused charge is not counted, so the command can still finish inside
    # what it did book.
    assert gate._estimated == 200.0
    gate.charge(100.0)


def test_the_server_side_cap_never_exceeds_what_was_booked():
    """The backstop that binds when an estimate is wrong has to bind under
    concurrency too, or two commands hand the warehouse caps that overlap."""

    ledger = _Ledger()
    gate = _ledger_gate(ledger)
    gate.preflight_command(300.0)
    assert gate.statement_cap(unit="byte") == 300
    gate.record_billed(100.0)
    assert gate.statement_cap(unit="byte") == 200


def test_settling_is_idempotent():
    """Three funnels settle a gate and they overlap by design, not by accident:
    the command layer on its way out, the engine when it rebuilds a gate, and
    engine shutdown."""

    ledger = _Ledger()
    gate = _ledger_gate(ledger)
    gate.preflight_command(600.0)
    gate.settle()
    gate.settle()
    gate.settle()
    assert ledger.kinds().count("release") == 1


def test_a_phase_extends_the_hold_rather_than_adding_a_second():
    ledger = _Ledger()
    gate = _ledger_gate(ledger)
    gate.preflight_command(100.0)
    gate.charge(100.0)
    gate.preflight_phase(300.0)
    assert ledger.kinds() == ["reservation", "reservation"]
    assert ledger.total() == 400.0
    gate.settle()
    assert ledger.total() == 0.0


def test_settlement_reports_the_days_total_not_this_commands_share():
    """Two commands used to each report their own spend under a name that
    promises the day's, so a caller reading either one saw a fraction of the
    truth."""

    ledger = _Ledger()
    other = _ledger_gate(ledger)
    other.preflight_command(300.0)
    other.record_billed(300.0)
    other.settle()

    gate = _ledger_gate(ledger)
    gate.preflight_command(200.0)
    gate.record_billed(200.0)
    gate.settle()
    assert gate.spend_summary() == {
        "bytes_billed": 200.0,
        "session_spent_today": 500.0,
    }


def test_nothing_is_reserved_without_a_cumulative_ceiling_to_protect():
    """A reservation exists to be seen by a command settling against
    `session_ceiling`. With none set nothing reads it, so the ledger stays
    byte-identical for every project that never configured a daily cap."""

    ledger = _Ledger()
    gate = _ledger_gate(ledger, session_ceiling=None)
    gate.preflight_command(600.0)
    gate.record_billed(400.0)
    gate.settle()
    assert ledger.kinds() == ["settlement"]


def test_a_contended_lock_refuses_rather_than_running_unguarded():
    """Proceeding without the lock is the defect this guard closes, so a lock
    that cannot be taken is a refusal. It is the recoverable kind: whatever
    holds it is a billed command that will finish."""

    from exmergo_dex_core.guards.cost_guard import SpendLockTimeoutError

    def contended():
        raise TimeoutError("waited 30.0s for the spend lock")

    ledger = _Ledger()
    gate = _ledger_gate(ledger, lock=contended)
    with pytest.raises(SpendLockTimeoutError) as exc_info:
        gate.preflight_command(10.0)
    assert "re-issuing the same command is safe" in str(exc_info.value)
    assert exc_info.value.cost.paradigm is Paradigm.BYTES_SCANNED
    assert ledger.entries == []


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


def test_a_sub_unit_remainder_caps_at_one_rather_than_reading_as_unlimited():
    """A cheap command under a cumulative ceiling must still be runnable.

    A statement cap is an integer because every connector's cap setting takes
    one, and on the time-paradigm connectors a cap of 0 means *no limit* rather
    than "spend nothing", so the gate refuses rather than hand one over.
    Truncating a fractional remainder therefore turned into a refusal of
    affordable work: setting `session_ceiling` creates a reservation, a cheap
    command books less than a second, and `int(0.5)` is 0, so every small query
    was refused with "the remaining budget is under one database-second" against
    an untouched 60-second budget.

    The discriminator is which term produced the sub-unit value, so all three
    cases are asserted together: a booking under one unit still yields a usable
    cap, a genuinely exhausted ceiling still refuses, and the booking still
    tightens the cap when it is the smaller of the two. Dropping any one of
    them would trade a false refusal for a missing one, or the reverse.
    """

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    gate = CostGate(
        paradigm=Paradigm.DB_LOAD,
        connector="clickhouse",
        command="explore query",
        ceiling=60.0,
        confirmed=True,
        session_ceiling=900.0,
        session_spent=0.0,
    )
    gate.preflight_command(0.5)
    gate.charge(0.5)
    assert gate.statement_cap(unit="database-second") == 1

    spent = CostGate(
        paradigm=Paradigm.DB_LOAD,
        connector="clickhouse",
        command="explore query",
        ceiling=60.0,
        confirmed=True,
        session_ceiling=None,
        session_spent=0.0,
    )
    spent.record_billed(59.5, job_id=None, statement="prior")
    with pytest.raises(OverCeilingError, match="under one database-second"):
        spent.statement_cap(unit="database-second")

    # And the booking still tightens: 5 booked against a 60-second ceiling caps
    # the statement at 5, not 60.
    tight = CostGate(
        paradigm=Paradigm.DB_LOAD,
        connector="clickhouse",
        command="explore query",
        ceiling=60.0,
        confirmed=True,
        session_ceiling=900.0,
        session_spent=0.0,
    )
    tight.preflight_command(5.0)
    assert tight.statement_cap(unit="database-second") == 5


def test_an_exhausted_budget_refuses_at_the_gate_rather_than_at_each_adapter():
    """Issue #316: the two states a cap of 0 could mean are told apart here.

    An exhausted budget and "no cap applies" used to be a 0 and a `None` handed
    back for the caller to tell apart, and every adapter told them apart the
    same way in its own billed path. That is a convention, not a contract: the
    connector that forgets the check sends the server a 0, which Postgres,
    ClickHouse and Databricks all read as *no limit*, so the backstop
    disappears exactly when the budget is nearly gone and the run looks
    entirely ordinary while it happens.

    So the shortfall is the call's own refusal. `statement_cap` returns a cap
    the server will honour or raises, with nothing in between for a caller to
    misread, and the refusal carries the cost the envelope reports.
    """

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    def gate(paradigm: Paradigm, ceiling: float) -> CostGate:
        return CostGate(
            paradigm=paradigm,
            connector="postgres",
            command="explore query",
            ceiling=ceiling,
            confirmed=True,
            session_ceiling=None,
            session_spent=0.0,
        )

    exhausted = gate(Paradigm.DB_LOAD, 60.0)
    exhausted.record_billed(59.7, job_id=None, statement="prior")
    with pytest.raises(OverCeilingError) as refusal:
        exhausted.statement_cap(unit="database-second")
    # The refusal is priced: an empty cost block on a spend refusal reads as a
    # claim that nothing was going to be spent.
    assert refusal.value.cost is not None
    assert refusal.value.cost.paradigm is Paradigm.DB_LOAD
    assert refusal.value.cost.ceiling == 60.0

    # A floor above one unit is the same event: BigQuery bills a 10 MB minimum
    # per query, so a cap under that is refused by the server rather than
    # honoured by it, and the shortfall names the number to raise --budget past.
    bytes_gate = gate(Paradigm.BYTES_SCANNED, 12 * 1024 * 1024)
    bytes_gate.record_billed(4 * 1024 * 1024, job_id=None, statement="prior")
    with pytest.raises(OverCeilingError, match="10,485,760-byte minimum"):
        bytes_gate.statement_cap(unit="byte", minimum=10 * 1024 * 1024)
    # ...and it is a floor, not a rounding: 12 MB of the 12 MB budget is above
    # it and still prices normally.
    assert (
        gate(Paradigm.BYTES_SCANNED, 12 * 1024 * 1024).statement_cap(
            unit="byte", minimum=10 * 1024 * 1024
        )
        == 12 * 1024 * 1024
    )

    # `None` is the only other answer, and it means no ceiling bounds the
    # statement at all rather than "spend nothing": distinct from every integer,
    # so no adapter has to recognise a magnitude to tell them apart.
    unbounded = CostGate(
        paradigm=Paradigm.FREE_LOCAL,
        connector="duckdb",
        command="explore query",
        ceiling=None,
        confirmed=True,
        session_ceiling=None,
        session_spent=0.0,
    )
    assert unbounded.statement_cap(unit="database-second") is None


# --- the ledger is a dependency of billing, not of every command -----------------


class _Outage:
    """A ledger reader that fails, and counts how often it was asked.

    The count is the assertion that matters for issue #374: the defect was not
    that the reader raised, it was that it was called at all on a command with no
    stake in the answer.
    """

    def __init__(self, *, fail: bool = True, total: float = 0.0) -> None:
        self.calls = 0
        self.fail = fail
        self.total = total

    def __call__(self) -> float:
        self.calls += 1
        if self.fail:
            raise ConnectionError("the ledger backend is unreachable")
        return self.total


def test_building_a_gate_reads_no_ledger():
    """Issue #374: a gate is built for every command on a billed connector,
    including the ones that cannot spend, so construction that reads the ledger
    puts a free cache-served answer behind the ledger's availability. The
    constructor holds a reader; only admission calls it."""

    reader = _Outage()
    gate = _gate(session_spent=reader, session_ceiling=1_000.0)
    assert reader.calls == 0
    assert gate.session_spent is None


def test_a_fixed_session_spend_is_seeded_at_construction():
    """A caller who passed a number passed the answer, not a way to get it, so
    there is nothing to defer and nothing that can fail."""

    gate = _gate(session_spent=250.0, session_ceiling=1_000.0)
    assert gate.session_spent == 250.0
    assert gate.effective_ceiling() == 750.0


def test_an_unread_session_spend_leaves_the_ceiling_to_the_command_budget():
    """The session bound needs a reading to exist. Its absence is not a loosened
    ceiling: admission always reads before it decides, so what reaches here
    unread is a command reporting a cost it never priced."""

    gate = _gate(
        session_spent=_Outage(fail=False, total=900.0), session_ceiling=1_000.0
    )
    assert gate.effective_ceiling() == 1_000.0  # the per-command budget alone
    assert gate.cost().ceiling == 1_000.0


def test_admitting_billed_work_against_an_unreadable_ledger_refuses():
    """Fail closed, and say which guard refused rather than reading as a crash.

    Before this the reader was called bare, so a backend's own exception
    travelled out of gate construction as an unclassified internal error.
    """

    from exmergo_dex_core.guards.cost_guard import LedgerUnreadableError

    ledger = _Ledger()
    reader = _Outage()
    gate = _ledger_gate(ledger, session_spent=reader)
    with pytest.raises(LedgerUnreadableError) as exc_info:
        gate.preflight_command(10.0)
    assert "re-issuing the same command is safe" in str(exc_info.value)
    assert exc_info.value.cost.paradigm is Paradigm.BYTES_SCANNED
    # A refusal books nothing, the same as every other refusal in this suite.
    assert ledger.entries == []


def test_settling_survives_a_ledger_that_went_away_mid_command():
    """Settlement re-reads only so the summary can report the day's total, and
    the release needs no reading at all, so a backend that failed after the work
    ran must not turn a completed command into a refusal."""

    ledger = _Ledger()
    reader = _Outage(fail=False)
    gate = _ledger_gate(ledger, session_spent=reader)
    gate.preflight_command(100.0)
    gate.record_billed(80.0)
    reader.fail = True
    gate.settle()  # does not raise
    summary = gate.spend_summary()
    assert summary["bytes_billed"] == 80.0
    # What this command billed is exact (the warehouse said so); only the day's
    # total is unavailable, and it says so rather than reporting this one's share.
    assert summary["session_spent_today"] is None
    assert "release" in ledger.kinds()


def test_reporting_the_day_never_fails_the_command_that_asks():
    """``connect test`` exists to say what the budget looks like, so a ledger it
    cannot reach is an "unavailable" on an otherwise healthy report."""

    gate = _gate(session_spent=_Outage())
    assert gate.session_spent_now() is None
    healthy = _gate(session_spent=_Outage(fail=False, total=42.0))
    assert healthy.session_spent_now() == 42.0


def test_a_cumulative_cap_binds_on_a_statement_charged_without_a_handshake():
    """The unread session bound must not read as an absent one.

    A caller that charges a statement without going through the handshake has no
    booking, so the cheap local path applies, and a ceiling computed there would
    have the session bound simply missing. That would let a configured cumulative
    cap silently not apply, which is the failure the whole guard exists to
    prevent, so the reading is taken here instead.
    """

    ledger = _Ledger()
    ledger.append({"entry": "settlement", "billed_bytes": 950.0})
    gate = _ledger_gate(ledger, session_spent=ledger.total)
    assert gate.session_spent is None  # a reader, so nothing read yet
    # 100 against a 1_000 cumulative cap with 950 already spent: the per-command
    # ceiling of 1_000 would admit it, the cumulative cap must not.
    with pytest.raises(OverCeilingError):
        gate.charge(100.0)
