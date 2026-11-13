"""Databricks Unity Catalog Metric Views as dex's semantic layer
(`semantic.vendor: databricks`).

Importing this package imports neither sqlglot nor anything Databricks ships:
dex reads the Metric View YAML bodies committed to the repository statically,
validated by its own port of the Metric View YAML 1.1 rules, and opens no
workspace connection.
"""

from __future__ import annotations

from .dependency import (
    CONVERTER_COMMIT,
    METRIC_VIEW_SPEC_VERSION,
    MetricViewDependencyError,
    ensure_available,
)

__all__ = [
    "CONVERTER_COMMIT",
    "METRIC_VIEW_SPEC_VERSION",
    "MetricViewDependencyError",
    "ensure_available",
]
