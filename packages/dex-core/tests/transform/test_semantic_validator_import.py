"""Which MetricFlow schemas the validator loads, and what it says when none are there.

The validator of record is MetricFlow's own schema, but MetricFlow ships it
under two package names: ``metricflow_semantic_interfaces`` today, and
``dbt_semantic_interfaces`` on the older pins. The import used to name only the
second, so a pin that shipped the first imported nothing, the ``ImportError``
was caught, and the YAML was checked structurally with a warning that read like
routine news. These tests hold both paths open, and the fallback's own words,
because the failure is a silent one: a warning nobody reads is not a refusal.
"""

from __future__ import annotations

import sys
import types

import pytest

from exmergo_dex_core.transform.semantic import (
    _metricflow_validators,
    validate_semantic_yaml,
)
from exmergo_dex_core.transform.validate import EditValidationError

# A document the structural check accepts: a semantic model with a name and a
# model, a metric with a name, a type and type_params. What the schemas make
# of it is the part under test.
_SEMANTIC_DOCUMENT = {
    "semantic_models": [
        {
            "name": "orders",
            "description": "Order grain over stg_orders.",
            "model": "ref('stg_orders')",
        }
    ],
    "metrics": [
        {
            "name": "order_count",
            "type": "simple",
            "type_params": {"measure": "order_count"},
        }
    ],
}


class _AcceptingValidator:
    """A validator that accepts every document, standing in for MetricFlow's."""

    def validate(self, document):
        return None


class _RejectingValidator:
    """A validator that refuses every document, standing in for MetricFlow's."""

    def validate(self, document):
        raise ValueError("metricflow says no")


def _install(name: str, validator) -> dict[str, types.ModuleType]:
    """Register a package tree whose `parsing.schemas` module carries a validator.

    The whole chain is registered, because `from a.b.c import d` resolves the
    parents before the leaf and a missing parent is a `ModuleNotFoundError`,
    which is the absence this is standing in for.
    """
    root = types.ModuleType(name)
    root.__path__ = []  # type: ignore[attr-defined]
    parsing = types.ModuleType(f"{name}.parsing")
    parsing.__path__ = []  # type: ignore[attr-defined]
    schemas = types.ModuleType(f"{name}.parsing.schemas")
    schemas.metric_validator = validator
    schemas.semantic_model_validator = validator
    parsing.schemas = schemas
    root.parsing = parsing
    return {name: root, f"{name}.parsing": parsing, f"{name}.parsing.schemas": schemas}


def _absent(monkeypatch, name: str) -> None:
    """Make every import of `name` fail the way a missing package does.

    Blocking only the parent is not enough. The import under test names the
    submodule (``name.parsing.schemas``), and a submodule already cached in
    ``sys.modules`` resolves straight from the cache, so the blocked parent is
    never consulted. That is what made these two tests pass in a sandbox
    without MetricFlow and fail in CI with it installed.
    """
    for cached in [key for key in sys.modules if key.startswith(f"{name}.")]:
        monkeypatch.setitem(sys.modules, cached, None)
    monkeypatch.setitem(sys.modules, name, None)


def test_the_package_metricflow_ships_today_is_the_first_choice(monkeypatch):
    """The current name must load; the older one must not be the only way in."""
    fake = _install("metricflow_semantic_interfaces", _AcceptingValidator())
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)

    validators = _metricflow_validators()

    assert validators is not None
    schemas = fake["metricflow_semantic_interfaces.parsing.schemas"]
    assert validators[0] is schemas.metric_validator
    assert validators[1] is schemas.semantic_model_validator


def test_the_older_package_name_is_the_fallback(monkeypatch):
    """A pin that still ships the old name must still validate."""
    _absent(monkeypatch, "metricflow_semantic_interfaces")
    fake = _install("dbt_semantic_interfaces", _AcceptingValidator())
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)

    validators = _metricflow_validators()

    assert validators is not None
    schemas = fake["dbt_semantic_interfaces.parsing.schemas"]
    assert validators[0] is schemas.metric_validator
    assert validators[1] is schemas.semantic_model_validator


def test_neither_package_installed_is_none(monkeypatch):
    """No schemas anywhere is the one case that degrades, and it says so."""
    _absent(monkeypatch, "metricflow_semantic_interfaces")
    _absent(monkeypatch, "dbt_semantic_interfaces")

    assert _metricflow_validators() is None


def test_the_schemas_run_where_metricflow_is_installed(monkeypatch):
    """A document the structural check accepts and the schemas refuse is refused.

    The structural check would have passed this document, so the refusal can
    only have come from the schemas. That is the difference the issue is about.
    """
    fake = _install("metricflow_semantic_interfaces", _RejectingValidator())
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)

    with pytest.raises(EditValidationError) as exc_info:
        validate_semantic_yaml("models/semantic/orders.yml", _SEMANTIC_DOCUMENT)

    assert "metricflow says no" in str(exc_info.value)


def test_the_schemas_run_under_the_older_package_name_too(monkeypatch):
    """The fallback is a fallback of the validators, not of the behaviour."""
    _absent(monkeypatch, "metricflow_semantic_interfaces")
    fake = _install("dbt_semantic_interfaces", _RejectingValidator())
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)

    with pytest.raises(EditValidationError) as exc_info:
        validate_semantic_yaml("models/semantic/orders.yml", _SEMANTIC_DOCUMENT)

    assert "metricflow says no" in str(exc_info.value)


def test_the_fallback_names_the_package_and_the_extra(monkeypatch):
    """The warning says what is missing and how to get it, not just that it is."""
    _absent(monkeypatch, "metricflow_semantic_interfaces")
    _absent(monkeypatch, "dbt_semantic_interfaces")

    warnings = validate_semantic_yaml("models/semantic/orders.yml", _SEMANTIC_DOCUMENT)

    assert len(warnings) == 1
    warning = warnings[0]
    assert "metricflow" in warning
    assert "exmergo-dex-core[semantic]" in warning
    assert "structural check only" in warning


def test_the_structural_check_still_refuses_a_broken_document(monkeypatch):
    """Without the schemas, the structural check is what stands between the
    agent and a malformed layer, so it must still refuse."""
    _absent(monkeypatch, "metricflow_semantic_interfaces")
    _absent(monkeypatch, "dbt_semantic_interfaces")

    with pytest.raises(EditValidationError):
        validate_semantic_yaml(
            "models/semantic/orders.yml",
            {"semantic_models": [{"name": "orders"}]},
        )
