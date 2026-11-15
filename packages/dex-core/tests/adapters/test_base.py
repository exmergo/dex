"""The adapter-shared helpers in ``adapters.base`` that every metered connector
reaches the same way, tested once here rather than six times over."""

from __future__ import annotations

from exmergo_dex_core.adapters.base import (
    affordable_combinations,
    key_shape_expressions,
    shape_stat_expressions,
    temporal_alignment_expressions,
    type_contradiction_expressions,
)
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


# --- fraction literals ----------------------------------------------------------


def _regexp(qcol: str, pattern: str) -> str:
    return f"regexp_like({qcol}, '{pattern}')"


def _fraction_builders(**literals) -> list[str]:
    return [
        *shape_stat_expressions('"c"', 0, _regexp, **literals),
        *type_contradiction_expressions(
            '"c"',
            0,
            is_string=True,
            is_integer=False,
            regexp_predicate=_regexp,
            bigint_type="BIGINT",
            **literals,
        ),
        *key_shape_expressions('"c"', 0, _regexp, **literals),
        *temporal_alignment_expressions(
            '"c"', 0, lambda q, unit: f"date_trunc('{unit}', {q})", **literals
        ),
    ]


def test_every_fraction_builder_keeps_its_decimal_literals_by_default():
    """Every shipped adapter calls the builders without the keyword, so the SQL
    each of them sends is exactly what it was before the keyword existed."""

    default = _fraction_builders()
    assert default == _fraction_builders(fraction_literals=("1.0", "0.0"))
    assert shape_stat_expressions('"c"', 0, _regexp)[0] == (
        "AVG(CASE WHEN regexp_like(\"c\", '^[A-Z]+([ -][A-Z]+)*$') THEN 1.0 "
        'WHEN "c" IS NOT NULL THEN 0.0 END) AS su_0'
    )


def test_double_literals_reach_every_fraction():
    """Trino types 1.0 as decimal(2,1) and AVG keeps that scale, so a fraction of
    1/3 comes back as 0.3. Every fraction a builder emits has to take the
    literals it is given, or one statistic silently stays rounded."""

    default = " ".join(_fraction_builders())
    doubled = " ".join(_fraction_builders(fraction_literals=("1e0", "0e0")))
    fractions = default.count("THEN 1.0 ")
    assert fractions > 0
    assert doubled.count("THEN 1e0 ") == fractions
    assert doubled.count("THEN 0e0 END") == default.count("THEN 0.0 END")
    assert "THEN 1.0 " not in doubled and "THEN 0.0 END" not in doubled


def test_json_safe_coerces_scalars_and_collections():
    from datetime import date, time
    from decimal import Decimal

    from exmergo_dex_core.adapters.base import json_safe

    assert json_safe(None) is None
    assert json_safe("text") == "text"
    assert json_safe(42) == 42
    assert json_safe(3.14) == 3.14
    assert json_safe(True) is True
    assert json_safe(Decimal("12.34")) == 12.34
    assert json_safe(date(2026, 11, 15)) == "2026-11-15"
    assert json_safe(time(12, 30)) == "12:30:00"

    # Collections serialize as JSON-safe arrays/dicts
    assert json_safe([1, 2, "a"]) == [1, 2, "a"]
    assert json_safe((1, Decimal("2.5"))) == [1, 2.5]
    assert json_safe([date(2026, 1, 1), [Decimal("3.0")]]) == ["2026-01-01", [3.0]]
    assert json_safe({"k": Decimal("1.5")}) == {"k": 1.5}

    # Objects providing .tolist() returning collections or 0-D scalars
    class MockArray:
        def __init__(self, val):
            self._val = val

        def tolist(self):
            return self._val

    assert json_safe(MockArray([1, 2, "a"])) == [1, 2, "a"]
    assert json_safe(MockArray(7)) == 7
    assert json_safe(MockArray("abc")) == "abc"
    assert json_safe(MockArray(Decimal("3.5"))) == 3.5
    assert json_safe(MockArray([MockArray(1), MockArray("b")])) == [1, "b"]

    try:
        import numpy as np

        assert json_safe(np.array(7)) == 7
        assert json_safe(np.array("abc")) == "abc"
        assert json_safe(np.array([1, 2, 3])) == [1, 2, 3]
        assert json_safe(np.int64(42)) == 42
    except ImportError:
        pass


def test_clip_cell_value_bounds_scalar_and_nested_strings():
    from exmergo_dex_core.adapters.base import clip_cell_value

    assert clip_cell_value("abcdefgh", 4) == ("abcd...", True)
    assert clip_cell_value("abc", 4) == ("abc", False)
    assert clip_cell_value(123, 4) == (123, False)

    # Nested list string truncation
    res_list, clipped = clip_cell_value(["abcdefgh", "abc"], 4)
    assert res_list == ["abcd...", "abc"]
    assert clipped is True

    # Nested list without truncation
    assert clip_cell_value(["ab", "cd"], 4) == (["ab", "cd"], False)

    # Deeper nested list
    res_deep, deep_clipped = clip_cell_value([["abcdefgh"]], 4)
    assert res_deep == [["abcd..."]]
    assert deep_clipped is True
