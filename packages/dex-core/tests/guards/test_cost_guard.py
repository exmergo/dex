"""Cost-guard behavior: preflight-before-spend, in check order."""

from __future__ import annotations

import pytest

from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import (
    CeilingRequiredError,
    ConfirmationRequiredError,
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
