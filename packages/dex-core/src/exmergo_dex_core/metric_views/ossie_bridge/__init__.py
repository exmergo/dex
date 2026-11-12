# Ported from apache/ossie-converters, databricks/java at 82006ba2.
# Licensed under the Apache License, Version 2.0.
"""The Ossie bridge: a Metric View layer from an Ossie document, and back (#1260).

Two layers, kept apart so each can be checked on its own terms:

- **The port** (:mod:`.common`, :mod:`.dialect`, :mod:`.to_ossie`,
  :mod:`.to_metric_view`) is a faithful Python port of the ``databricks`` spoke
  of apache/ossie-converters at :data:`~..dependency.CONVERTER_COMMIT`. It is
  held to the converter's own fixtures, including the byte-pinned
  ``custom_extensions[DATABRICKS]`` stash, and its notices are the converter's,
  word for word.
- **The adaptation** (:mod:`.adapt`) is what dex adds so a proposal also passes
  dex's own validator for the target vendor, each change named in a notice of
  origin ``dex``.

Nothing here imports sqlglot or jsonschema: the port is YAML, regular
expressions and JSON, so either target needs only its own extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .common import (
    MV_VERSION,
    OSSIE_VERSION,
    ConversionError,
    Kind,
    Notice,
    Notices,
    parse_root,
)

__all__ = [
    "MV_VERSION",
    "OSSIE_VERSION",
    "Conversion",
    "ConversionError",
    "Kind",
    "Notice",
    "Notices",
    "metric_view_to_ossie",
    "metric_view_value_to_ossie",
    "ossie_to_metric_view",
    "ossie_value_to_metric_view",
]


@dataclass(frozen=True)
class Conversion:
    """One conversion's result: the converted body, its notices, and the fact
    dataset it is on."""

    value: dict[str, Any]
    notices: tuple[Notice, ...]
    fact: str

    def converter_notices(self) -> list[str]:
        """The notices the converter itself emits, as its ``Result.notices``."""
        return [n.render() for n in self.notices if n.origin == "converter"]


def metric_view_to_ossie(text: str, model_name: str | None = None) -> Conversion:
    """The converter's IMPORT over YAML text, with no dex adaptation."""
    return metric_view_value_to_ossie(parse_root(text, "Metric View"), model_name)


def metric_view_value_to_ossie(
    view: dict[str, Any], model_name: str | None = None
) -> Conversion:
    from .to_ossie import convert_metric_view_value

    notices = Notices()
    imported = convert_metric_view_value(view, model_name, notices)
    return Conversion(imported.model, tuple(notices.items), imported.fact)


def ossie_to_metric_view(text: str, source: str | None = None) -> Conversion:
    """The converter's EXPORT over YAML text, with no dex adaptation."""
    return ossie_value_to_metric_view(parse_root(text, "Apache Ossie"), source)


def ossie_value_to_metric_view(
    document: dict[str, Any], source: str | None = None
) -> Conversion:
    from .to_metric_view import convert_ossie_value

    notices = Notices()
    exported = convert_ossie_value(document, source, notices)
    return Conversion(exported.view, tuple(notices.items), exported.fact)
