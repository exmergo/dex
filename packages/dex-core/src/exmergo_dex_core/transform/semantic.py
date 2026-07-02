"""Semantic-layer authoring: dbt semantic models / MetricFlow YAML as plan edits.

``define`` and ``update`` are plan producers into the same store as model SQL;
``emit dbt`` applies them. The engine validates the YAML the agent authored;
MetricFlow's own schemas (via ``dbt-semantic-interfaces``, pulled in by
dbt-duckdb) are the validator of record, with a structural fallback when that
package is absent so validation degrades to a warning, never to silence.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..dbt_project import DbtProjectView
from .validate import EditValidationError


def validate_semantic_yaml(path: str, parsed: dict[str, Any]) -> list[str]:
    """Validate one semantic YAML document. Returns warnings; raises on failure."""

    semantic_models = parsed.get("semantic_models") or []
    metrics = parsed.get("metrics") or []
    if not semantic_models and not metrics:
        raise EditValidationError(
            f"{path}: semantic YAML must declare semantic_models or metrics"
        )

    try:
        from dbt_semantic_interfaces.parsing.schemas import (
            metric_validator,
            semantic_model_validator,
        )
    except ImportError:
        _structural_check(path, semantic_models, metrics)
        return [
            "dbt-semantic-interfaces is not installed; semantic YAML got a "
            "structural check only"
        ]

    for entry in semantic_models:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise EditValidationError(f"{path}: semantic model entry needs a name")
        if not entry.get("model"):
            raise EditValidationError(
                f"{path}: semantic model '{entry['name']}' needs a model: ref(...)"
            )
        # dbt schema files say `model: ref(...)`; MetricFlow's schema speaks the
        # compiled `node_relation` instead, so validate the entry minus `model`.
        body = {k: v for k, v in entry.items() if k != "model"}
        _validate_with(
            semantic_model_validator, body, path, f"semantic model '{entry['name']}'"
        )
    for entry in metrics:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise EditValidationError(f"{path}: metric entry needs a name")
        _validate_with(metric_validator, entry, path, f"metric '{entry['name']}'")
    return []


def existing_semantic_names(view: DbtProjectView) -> set[str]:
    """Names of semantic models and metrics already declared in the project."""

    names: set[str] = set()
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue  # a broken hand-written file is not this command's problem
        if not isinstance(parsed, dict):
            continue
        for entry in (parsed.get("semantic_models") or []) + (
            parsed.get("metrics") or []
        ):
            if isinstance(entry, dict) and entry.get("name"):
                names.add(entry["name"])
    return names


def time_spine_warning(
    view: DbtProjectView, parsed_edits: list[dict[str, Any]]
) -> str | None:
    """Warn when semantic YAML lands in a project with no MetricFlow time spine.

    dbt refuses to parse a project that has semantic models but no time spine
    model, so a first `emit dbt` without one produces a project that cannot
    build. The engine cannot author the model itself (propose-don't-impose), so
    it warns at plan time instead of letting the build be the first signal.
    """

    def declares_spine(parsed: Any) -> bool:
        if not isinstance(parsed, dict):
            return False
        return any(
            isinstance(entry, dict) and "time_spine" in entry
            for entry in parsed.get("models") or []
        )

    if any(declares_spine(parsed) for parsed in parsed_edits):
        return None
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue
        if declares_spine(parsed):
            return None
    return (
        "the project has no MetricFlow time spine; dbt cannot parse semantic "
        "models without one. Author a day-grain date model (for example "
        "models/metricflow_time_spine.sql) plus its YAML with a `time_spine:` "
        "config before building"
    )


def check_mode(
    mode: str, parsed_edits: list[dict[str, Any]], view: DbtProjectView
) -> None:
    """Enforce define-vs-update: define refuses existing names, update requires them."""

    existing = existing_semantic_names(view)
    proposed = {
        entry["name"]
        for parsed in parsed_edits
        for entry in (parsed.get("semantic_models") or [])
        + (parsed.get("metrics") or [])
        if isinstance(entry, dict) and entry.get("name")
    }
    if mode == "define":
        clashes = sorted(proposed & existing)
        if clashes:
            raise EditValidationError(
                f"already defined in the project: {', '.join(clashes)}; use "
                "`semantic update` to evolve an existing definition"
            )
    elif mode == "update":
        missing = sorted(proposed - existing)
        if missing:
            raise EditValidationError(
                f"not defined in the project: {', '.join(missing)}; use "
                "`semantic define` to add a new definition"
            )


def _validate_with(
    validator: Any, document: dict[str, Any], path: str, label: str
) -> None:
    try:
        validator.validate(document)
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise EditValidationError(f"{path}: {label}: {first_line}") from exc


def _structural_check(
    path: str, semantic_models: list[Any], metrics: list[Any]
) -> None:
    for entry in semantic_models:
        if (
            not isinstance(entry, dict)
            or not entry.get("name")
            or not entry.get("model")
        ):
            raise EditValidationError(
                f"{path}: each semantic model needs at least name and model"
            )
    for entry in metrics:
        if (
            not isinstance(entry, dict)
            or not entry.get("name")
            or not entry.get("type")
            or "type_params" not in entry
        ):
            raise EditValidationError(
                f"{path}: each metric needs at least name, type, and type_params"
            )
