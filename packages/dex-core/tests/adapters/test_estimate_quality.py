"""How much an estimate is worth, per connector, and when it is worth nothing.

The magnitude and its quality are different facts, and a budget sized against
BigQuery's dry run should not be applied to Snowflake's model of one. Three
states, and the distinction between the last two is the one that matters:
``None`` means nothing was priced, and ``UNKNOWN`` means pricing was attempted
and produced no number.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.adapters import adapter_declarations
from exmergo_dex_core.envelope import (
    Cost,
    EstimateQuality,
    Paradigm,
    paradigm_unit,
)
from exmergo_dex_core.guards.cost_guard import (
    CostGate,
    estimate_quality_of,
    preflight,
)


@pytest.mark.parametrize(
    ("connector", "expected"),
    [
        # A dry run is what the job will bill, not a model of it.
        ("bigquery", EstimateQuality.EXACT),
        ("snowflake", EstimateQuality.APPROXIMATE),
        ("databricks", EstimateQuality.APPROXIMATE),
        ("redshift", EstimateQuality.APPROXIMATE),
        ("postgres", EstimateQuality.APPROXIMATE),
        ("clickhouse", EstimateQuality.APPROXIMATE),
        # Nothing is priced because nothing is billed.
        ("duckdb", None),
    ],
)
def test_every_connector_declares_what_its_pricing_can_achieve(connector, expected):
    pytest.importorskip_map = None
    try:
        declared = adapter_declarations(connector)
    except ImportError:
        pytest.skip(f"the [{connector}] extra is not installed")
    assert declared.estimate_quality is expected


def test_a_billed_paradigm_with_no_estimate_is_unknown_not_absent():
    """The sharp distinction. A host that collapses the two admits an unpriced
    build believing it was priced."""

    declared = adapter_declarations("bigquery", Paradigm.BYTES_SCANNED)
    assert (
        estimate_quality_of(declared, None, paradigm=Paradigm.BYTES_SCANNED)
        is EstimateQuality.UNKNOWN
    )
    assert (
        estimate_quality_of(declared, 1_000.0, paradigm=Paradigm.BYTES_SCANNED)
        is EstimateQuality.EXACT
    )


def test_a_free_connector_reports_no_quality_at_all():
    assert (
        estimate_quality_of(
            adapter_declarations("duckdb"), 0.0, paradigm=Paradigm.FREE_LOCAL
        )
        is None
    )


def test_an_adapter_that_declares_nothing_is_unknown_rather_than_credited():
    """Duck-typed like `query_estimate`, so a third-party adapter that has not
    said what its pricing achieves is not credited with exactness."""

    class Unlabelled:
        name = "mystery"
        paradigm = Paradigm.COMPUTE_TIME

    assert (
        estimate_quality_of(Unlabelled(), 5.0, paradigm=Paradigm.COMPUTE_TIME)
        is EstimateQuality.UNKNOWN
    )


def test_preflight_stamps_the_quality_onto_the_cost_it_returns():
    cost = preflight(
        1_000.0,
        10_000.0,
        paradigm=Paradigm.BYTES_SCANNED,
        confirmed=True,
        estimate_quality=EstimateQuality.EXACT,
    )
    assert cost.estimate_quality is EstimateQuality.EXACT
    assert cost.unit == "bytes"


def test_the_unit_is_derived_from_the_paradigm_and_cannot_be_set_wrong():
    """Derived rather than passed, so no call site can report a magnitude in one
    unit and label it another."""

    assert Cost(paradigm=Paradigm.BYTES_SCANNED).unit == "bytes"
    assert Cost(paradigm=Paradigm.COMPUTE_TIME).unit == "seconds"
    assert Cost(paradigm=Paradigm.DB_LOAD).unit == "seconds"
    assert Cost(paradigm=Paradigm.FREE_LOCAL).unit is None
    assert Cost().unit is None
    # A caller that passes one anyway is overruled rather than believed.
    assert Cost(paradigm=Paradigm.BYTES_SCANNED, unit="credits").unit == "bytes"
    assert paradigm_unit(None) is None


def test_a_gate_reports_the_quality_the_adapter_stamped_on_it():
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=1_000.0,
        session_ceiling=None,
        session_spent=0.0,
        confirmed=True,
        connector="bigquery",
    )
    # Unset until an adapter declares it, and a gate nobody set it on claims
    # nothing rather than exactness.
    assert gate.cost().estimate_quality is EstimateQuality.UNKNOWN

    gate.estimate_quality = EstimateQuality.EXACT
    gate.preflight_command(500.0)
    assert gate.cost().estimate_quality is EstimateQuality.EXACT


def test_get_adapter_stamps_the_declaration_onto_the_gate(duckdb_file):
    """One exit rather than seven: the gate is built before the adapter that
    owns it, so the stamp happens where every adapter is constructed."""

    from exmergo_dex_core.adapters import get_adapter

    adapter = get_adapter("duckdb", path=str(duckdb_file))
    try:
        # DuckDB carries no gate at all, so there is nothing to stamp and
        # nothing breaks.
        assert getattr(adapter, "cost_gate", None) is None
    finally:
        adapter.close()
