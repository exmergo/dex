"""The adapter-shared helpers in ``adapters.base`` that every metered connector
reaches the same way, tested once here rather than six times over."""

from __future__ import annotations

from exmergo_dex_core.adapters.base import affordable_combinations
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import CostGate

PAIRS = [["a", "b"], ["b", "c"], ["c", "d"], ["d", "e"]]


def _gate(*, ceiling: float, spent: float = 0.0) -> CostGate:
    gate = CostGate(
        paradigm=Paradigm.COMPUTE_TIME,
        ceiling=ceiling,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="stub",
        command="explore profile",
    )
    if spent:
        gate.charge(spent)
    return gate


def test_a_probe_the_budget_covers_is_not_narrowed_and_carries_no_note():
    probed, note = affordable_combinations(
        PAIRS, lambda prefix: 1.0 * len(prefix), _gate(ceiling=100.0).try_charge
    )
    assert probed == PAIRS
    assert note is None


def test_a_probe_the_budget_half_covers_narrows_to_the_best_ranked_prefix():
    """The pairs arrive best-ranked first, so the affordable prefix is the part
    most likely to hold the grain. Refusing the whole probe throws that away for
    no saving, which is the degradation this replaces."""

    probed, note = affordable_combinations(
        PAIRS,
        lambda prefix: 1.0 * len(prefix),
        _gate(ceiling=100.0, spent=97.5).try_charge,
    )
    assert probed == [["a", "b"], ["b", "c"]]
    assert note == (
        "composite-key probe narrowed to 2 of 4 candidate pairs: the remaining "
        "budget could not cover the rest; a grain outside the pairs probed "
        "stays unknown"
    )


def test_a_probe_the_budget_cannot_start_is_skipped_with_the_grain_unknown():
    probed, note = affordable_combinations(
        PAIRS,
        lambda prefix: 1.0 * len(prefix),
        _gate(ceiling=100.0, spent=99.5).try_charge,
    )
    assert probed == []
    assert note is not None
    assert "composite-key probe skipped" in note
    assert "grain stays unknown" in note


def test_a_refusal_at_the_billing_floor_stops_the_search():
    """A connector with a per-statement minimum cannot price a shorter prefix
    below it, so re-pricing one is a wasted round trip. Only the connectors that
    have such a floor pass one."""

    priced: list[int] = []

    def estimate_for(prefix: list[list[str]]) -> float:
        priced.append(len(prefix))
        return 10.0  # every prefix floors to the same minimum

    probed, note = affordable_combinations(
        PAIRS, estimate_for, _gate(ceiling=100.0, spent=95.0).try_charge, floor=10.0
    )
    assert probed == []
    assert note is not None and "skipped" in note
    assert priced == [4], "a prefix that cannot cost less is not priced again"

    priced.clear()
    affordable_combinations(
        PAIRS, estimate_for, _gate(ceiling=100.0, spent=95.0).try_charge
    )
    assert priced == [4, 3, 2, 1], "without a floor the search runs to the end"


def test_a_refused_charge_leaves_the_gate_where_it_found_it():
    """The descending search rests on this: ``CostGate.charge`` raises out of
    its preflight before it accumulates an estimate or widens a reservation, so
    the attempts that fail do not quietly spend the budget the winning prefix
    then needs."""

    gate = _gate(ceiling=100.0, spent=50.0)
    assert gate.try_charge(60.0) is False
    assert gate.try_charge(60.0) is False
    assert gate.try_charge(50.0) is True
