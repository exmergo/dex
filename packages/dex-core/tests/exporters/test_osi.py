"""OSI validation against the pinned schema.

OSI is a dormant exporter: not emitted in v1. These tests keep the pinned-schema
mechanism live and ready: load the vendored schema, accept a known-valid document,
reject an invalid one, and wrap dex richness as a sanctioned custom_extensions
entry. The exporter that produces OSI from the dbt semantic model is switched on
when OSI matures.
"""

from __future__ import annotations

import json

import pytest

from exmergo_dex_core.exporters import osi

# Minimal document valid under the pinned schema (version const + one model with
# at least one dataset, each dataset needing name + source).
VALID_DOC = {
    "version": "0.2.0.dev0",
    "semantic_model": [
        {
            "name": "sales",
            "datasets": [{"name": "orders", "source": "main.main.orders"}],
        }
    ],
}


def test_pinned_schema_loads():
    schema = osi.load_pinned_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert "semantic_model" in schema["properties"]


def test_valid_document_passes():
    pytest.importorskip("jsonschema")
    osi.validate(VALID_DOC)  # must not raise


@pytest.mark.parametrize(
    "doc",
    [
        {"semantic_model": []},  # missing version
        {"version": "9.9.9", "semantic_model": []},  # wrong version const
        {
            "version": "0.2.0.dev0",
            "semantic_model": [{"name": "x"}],
        },  # dataset-less model
        {
            "version": "0.2.0.dev0",
            "semantic_model": [{"name": "x", "datasets": [{"name": "d"}]}],
        },  # source-less dataset
    ],
)
def test_invalid_documents_are_rejected(doc):
    pytest.importorskip("jsonschema")
    with pytest.raises(osi.OSIValidationError):
        osi.validate(doc)


def test_dex_extension_shape():
    ext = osi.dex_extension({"hierarchy": ["year", "month", "day"]})
    assert ext["vendor_name"] == "DEX"
    # The payload is an opaque JSON string in OSI's model; dex validates its
    # contents against its own schema, not OSI's.
    assert json.loads(ext["data"]) == {"hierarchy": ["year", "month", "day"]}
