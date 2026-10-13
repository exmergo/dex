"""User-facing locations for nested MetricFlow schema validation errors."""

from __future__ import annotations

import pytest
from jsonschema import Draft7Validator

from exmergo_dex_core.transform.semantic import _validate_with
from exmergo_dex_core.transform.validate import EditValidationError


@pytest.fixture
def nested_validator():
    named_element = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    }
    return Draft7Validator(
        {
            "type": "object",
            "properties": {
                "entities": {"type": "array", "items": named_element},
                "measures": {"type": "array", "items": named_element},
            },
            "additionalProperties": False,
        }
    )


@pytest.mark.parametrize(
    ("document", "expected_location"),
    [
        (
            {
                "entities": [
                    {"name": "seller"},
                    {"name": "customer", "description": "invalid"},
                ],
            },
            "entities[1] ('customer')",
        ),
        (
            {"measures": [{"name": "revenue", "description": "invalid"}]},
            "measures[0] ('revenue')",
        ),
    ],
)
def test_nested_schema_errors_identify_named_element(
    nested_validator, document, expected_location
):
    with pytest.raises(EditValidationError) as exc_info:
        _validate_with(
            nested_validator, document, "models/orders.yml", "semantic model 'orders'"
        )

    assert f"semantic model 'orders' > {expected_location}:" in str(exc_info.value)
    assert "'description' was unexpected" in str(exc_info.value)


def test_model_level_schema_error_stays_unchanged(nested_validator):
    with pytest.raises(EditValidationError) as exc_info:
        _validate_with(
            nested_validator,
            {"unexpected": True},
            "models/orders.yml",
            "semantic model 'orders'",
        )

    assert str(exc_info.value) == (
        "models/orders.yml: semantic model 'orders': "
        "Additional properties are not allowed ('unexpected' was unexpected)"
    )


def test_valid_semantic_model_is_unchanged(nested_validator):
    _validate_with(
        nested_validator,
        {"entities": [{"name": "customer"}]},
        "models/orders.yml",
        "semantic model 'orders'",
    )
