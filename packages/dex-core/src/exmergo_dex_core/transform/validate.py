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

# Macro definition delimiters, tolerant of whitespace-control markers. A macro
# file is jinja, not SQL, so its shape check is structural: definitions only,
# nothing loose between them. dbt's own parser is the authoritative gate.
_MACRO_OPEN = re.compile(r"\{%-?\s*macro\s+\w+\s*\(")
_MACRO_CLOSE = re.compile(r"\{%-?\s*endmacro\s*-?%\}")
_MACRO_BLOCK = re.compile(r"\{%-?\s*macro\s.*?\{%-?\s*endmacro\s*-?%\}", re.DOTALL)


def strip_jinja(sql: str) -> str:
    """Reduce a dbt model to plain SQL for the SELECT-only check.

    Inline expressions (``{{ ref(...) }}``) become an identifier placeholder;
    statement and comment blocks are removed. A line that was nothing but
    jinja is dropped at the top level (a ``{{ config(...) }}`` header), but
    inside parentheses it becomes a placeholder subquery, because there it is
    a macro rendering a whole SELECT (``from ( {{ unpivot_json_object(...) }} )``)
    and dropping it would leave unparseable SQL. Depth counting is naive about
    parens inside string literals; a miscount only ever refuses, never admits.
    """

    text = _JINJA_COMMENT.sub("", sql)
    text = _JINJA_STMT.sub("", text)
    text = _JINJA_EXPR.sub(_PLACEHOLDER, text)
    lines: list[str] = []
    depth = 0
    for line in text.splitlines():
        if line.strip() == _PLACEHOLDER:
            if depth > 0:
                lines.append(line.replace(_PLACEHOLDER, f"select {_PLACEHOLDER}"))
            continue
        depth += line.count("(") - line.count(")")
        lines.append(line)
    return "\n".join(lines).strip()


def validate_edit(edit: PlanEdit) -> list[str]:
    """Validate one edit for its kind. Returns warnings; raises on a hard failure."""

    from .plans import EditKind

    warnings: list[str] = []
    if edit.kind is EditKind.MACRO_SQL:
        opens = len(_MACRO_OPEN.findall(edit.new_content))
        closes = len(_MACRO_CLOSE.findall(edit.new_content))
        if opens == 0:
            raise EditValidationError(
                f"{edit.path}: a macro_sql edit needs at least one "
                "{% macro name(...) %} definition"
            )
        if opens != closes:
            raise EditValidationError(
                f"{edit.path}: unbalanced macro definitions "
                f"({opens} macro, {closes} endmacro)"
            )
        outside = _JINJA_COMMENT.sub("", _MACRO_BLOCK.sub("", edit.new_content))
        if outside.strip():
            raise EditValidationError(
                f"{edit.path}: a macro file holds only macro definitions and "
                "jinja comments; found loose content outside them"
            )
    elif edit.kind is EditKind.MODEL_SQL:
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
        elif edit.kind is EditKind.PACKAGES_YML and not (
            parsed.get("packages") or parsed.get("dependencies")
        ):
            raise EditValidationError(
                f"{edit.path}: a packages manifest needs a 'packages:' (or "
                "'dependencies:') list"
            )
    return warnings
