"""Per-kind validation of agent-authored edits, before they become a plan.

Model SQL must be a single read-only SELECT once its jinja is stripped (defense in
depth: the same guard the adapters apply to generated SQL). YAML edits must parse
to a mapping. Semantic YAML gets a further MetricFlow-shape check in
``semantic.py``; this module owns the checks common to every plan producer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import yaml

from ..guards.sql_guard import assert_select_only

if TYPE_CHECKING:
    from .plans import PlanEdit


class EditValidationError(Exception):
    pass


# Jinja expression / statement / comment blocks, non-greedy so adjacent blocks
# don't merge. Statements and comments vanish; expressions become a placeholder
# identifier so `from {{ ref('x') }}` stays parseable SQL.
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_STMT = re.compile(r"\{%.*?%\}", re.DOTALL)
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_PLACEHOLDER = "__dex_jinja__"


def strip_jinja(sql: str) -> str:
    """Reduce a dbt model to plain SQL for the SELECT-only check.

    Inline expressions (``{{ ref(...) }}``) become an identifier placeholder;
    statement and comment blocks are removed; a line that was nothing but jinja
    (a ``{{ config(...) }}`` header) is dropped entirely.
    """

    text = _JINJA_COMMENT.sub("", sql)
    text = _JINJA_STMT.sub("", text)
    text = _JINJA_EXPR.sub(_PLACEHOLDER, text)
    lines = [line for line in text.splitlines() if line.strip() != _PLACEHOLDER]
    return "\n".join(lines).strip()


def validate_edit(edit: PlanEdit) -> list[str]:
    """Validate one edit for its kind. Returns warnings; raises on a hard failure."""

    from .plans import EditKind

    warnings: list[str] = []
    if edit.kind is EditKind.MODEL_SQL:
        stripped = strip_jinja(edit.new_content)
        if not stripped:
            warnings.append(
                f"{edit.path}: model is entirely jinja; SELECT-only check skipped"
            )
        else:
            try:
                assert_select_only(stripped)
            except Exception as exc:
                raise EditValidationError(f"{edit.path}: {exc}") from exc
    else:
        try:
            parsed = yaml.safe_load(edit.new_content)
        except yaml.YAMLError as exc:
            raise EditValidationError(f"{edit.path}: invalid YAML: {exc}") from exc
        if not isinstance(parsed, dict):
            raise EditValidationError(
                f"{edit.path}: expected a YAML mapping, got "
                f"{type(parsed).__name__ if parsed is not None else 'nothing'}"
            )
        if edit.kind is EditKind.SEMANTIC_YML:
            from .semantic import validate_semantic_yaml

            warnings.extend(validate_semantic_yaml(edit.path, parsed))
    return warnings
