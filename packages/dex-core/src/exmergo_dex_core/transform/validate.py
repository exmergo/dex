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
    from ..dbt_project import DbtProjectView
    from .plans import PlanEdit


class EditValidationError(Exception):
    pass


# profiles.yml keys whose value dbt reads from the environment, never a literal
# in a committed file. A path-typed key (``private_key_path``) holds no secret,
# and method-based auth (externalbrowser/iam/oauth) carries none either, so both
# are exempt by not being listed. This mirrors init's "no persisted secret" rule
# and, at author time, keeps a credential out of the plan diff and thus out of
# agent context.
_SECRET_KEYS = frozenset(
    {
        "password",
        "pass",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "secret",
        "private_key",
        "private_key_passphrase",
    }
)
# The one safe form for a sensitive value: a dbt env_var() reference, resolved at
# runtime. Any other value for a sensitive key is a literal and is refused.
_ENV_VAR_REF = re.compile(r"\{\{\s*env_var\s*\(")


def find_inlined_secret(content: str) -> str | None:
    """The first profiles.yml key that inlines a literal secret, or ``None``.

    Walks the parsed mapping; a sensitive key is safe only when its value is an
    ``env_var()`` reference. A parse failure returns ``None`` here (the mapping
    check reports malformed YAML), so this never masks a structural error.
    """

    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    return _scan_for_secret(parsed)


def _scan_for_secret(node: object) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            sensitive = isinstance(key, str) and key.lower() in _SECRET_KEYS
            if sensitive and not (
                isinstance(value, str) and _ENV_VAR_REF.search(value)
            ):
                return key
            hit = _scan_for_secret(value)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _scan_for_secret(item)
            if hit is not None:
                return hit
    return None


def assert_profiles_safe(view: DbtProjectView, edits: list[PlanEdit]) -> None:
    """Refuse a profiles.yml edit that inlines a literal credential, on either the
    current file (the diff's removed side) or the proposed content.

    Runs before the content reaches a diff or a dbt subprocess, so a credential
    never leaves the file. The message names the offending key, never its value.
    """

    from .plans import EditKind

    for edit in edits:
        if edit.kind is not EditKind.PROFILES_YML:
            continue
        current = view.files.get(edit.path)
        sides = (
            ("current", current.content if current is not None else None),
            ("proposed", edit.new_content),
        )
        for label, content in sides:
            if content is None:
                continue
            key = find_inlined_secret(content)
            if key is not None:
                raise EditValidationError(
                    f"{edit.path}: the {label} profiles.yml inlines a literal "
                    f"credential in '{key}'; reference it via "
                    "{{ env_var('NAME') }} so no credential enters the plan "
                    "diff or agent context"
                )


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
        elif edit.kind is EditKind.PROJECT_YML and not parsed.get("name"):
            # dbt keys the project on 'name'; without it dbt cannot load the
            # project at all. Structural gate before the authoritative dbt parse.
            raise EditValidationError(
                f"{edit.path}: dbt_project.yml must declare a 'name'"
            )
        elif edit.kind is EditKind.PROFILES_YML:
            secret_key = find_inlined_secret(edit.new_content)
            if secret_key is not None:
                raise EditValidationError(
                    f"{edit.path}: '{secret_key}' inlines a literal credential; a "
                    "profiles.yml edit must reference secrets via "
                    "{{ env_var('NAME') }} so no credential enters the plan diff"
                )
    return warnings
