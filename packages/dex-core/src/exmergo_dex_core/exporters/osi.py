"""OSI exporter and validator (dormant).

OSI is not emitted in v1. dbt is the source of truth and the only output; OSI is a
future exporter that will read the dbt semantic model and write an OSI document
once the format matures past its pre-1.0 DRAFT state (no PyPI package, no tagged
releases, schema in flux). The validator below is kept live and tested so the
pinned-schema mechanism is ready the day the exporter is switched on. dex does not
depend on an OSI library that does not exist; it vendors and pins the schema it
tested against (see ../schemas/PINNED.md) and validates with `jsonschema` directly.
Richness OSI cannot express rides in OSI's sanctioned `custom_extensions` under a
`DEX` vendor name; that payload is opaque to OSI, so dex validates it against its
own schema, not OSI's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEX_VENDOR_NAME = "DEX"

_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "osi-schema.json"


class OSIValidationError(Exception):
    pass


def load_pinned_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(document: dict[str, Any]) -> None:
    """Validate an OSI document against the pinned schema. Raises on failure."""

    import jsonschema

    schema = load_pinned_schema()
    try:
        jsonschema.validate(instance=document, schema=schema)
    except jsonschema.ValidationError as exc:
        raise OSIValidationError(str(exc.message)) from exc


def dex_extension(data: dict[str, Any]) -> dict[str, Any]:
    """Wrap a dex-specific payload as an OSI `custom_extensions` entry."""

    return {"vendor_name": DEX_VENDOR_NAME, "data": json.dumps(data)}


def emit(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Project the dbt semantic model into an OSI document. Dormant: not emitted in
    v1. The validator and extension hook above are live so the mechanism is ready."""

    raise NotImplementedError("OSI emission is dormant in v1")
