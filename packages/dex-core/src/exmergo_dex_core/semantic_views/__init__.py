"""Snowflake Semantic Views as dex's semantic layer (`semantic.vendor: snowflake`).

Importing this package imports neither sqlglot nor anything Snowflake ships: dex
reads the Semantic View YAML documents committed to the repository statically,
validated by its own port of Snowflake's specification and validation rules, and
opens no Snowflake connection.
"""

from __future__ import annotations

from .dependency import (
    SEMANTIC_VIEW_FORMAT_VERSION,
    SemanticViewDependencyError,
    ensure_available,
)

__all__ = [
    "SEMANTIC_VIEW_FORMAT_VERSION",
    "SemanticViewDependencyError",
    "ensure_available",
]
