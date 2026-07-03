"""Semantic-layer authoring: dbt semantic models / MetricFlow YAML as plan edits.

``define`` and ``update`` are plan producers into the same store as model SQL;
``transform apply`` writes them into the project. The engine validates the YAML
the agent authored; MetricFlow's own schemas (via ``dbt-semantic-interfaces``,
pulled in by dbt-duckdb) are the validator of record, with a structural fallback
when that package is absent so validation degrades to a warning, never to silence.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, NamedTuple

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


class SemanticNamespace(NamedTuple):
    """Every name the semantic layer can address, split by role.

    ``metrics`` includes the implicit metric a ``create_metric: true`` measure
    declares: dbt's parser sees those even though no ``metrics:`` entry exists,
    so collision checks and reference resolution must see them too.
    """

    semantic_models: set[str]
    metrics: set[str]
    measures: set[str]


def collect_namespace(documents: Iterable[Any]) -> SemanticNamespace:
    """Gather the semantic namespace declared by the given parsed YAML documents."""

    semantic_models: set[str] = set()
    metrics: set[str] = set()
    measures: set[str] = set()
    for parsed in documents:
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("semantic_models") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            semantic_models.add(entry["name"])
            for measure in entry.get("measures") or []:
                if not isinstance(measure, dict) or not measure.get("name"):
                    continue
                measures.add(measure["name"])
                if measure.get("create_metric"):
                    metrics.add(measure["name"])
        for entry in parsed.get("metrics") or []:
            if isinstance(entry, dict) and entry.get("name"):
                metrics.add(entry["name"])
    return SemanticNamespace(semantic_models, metrics, measures)


def project_namespace(view: DbtProjectView) -> SemanticNamespace:
    """The semantic namespace already declared across the project's YAML files."""

    documents = []
    for source in view.files.values():
        if not source.path.endswith((".yml", ".yaml")):
            continue
        try:
            parsed = yaml.safe_load(source.content)
        except yaml.YAMLError:
            continue  # a broken hand-written file is not this command's problem
        documents.append(parsed)
    return collect_namespace(documents)


def existing_semantic_names(view: DbtProjectView) -> set[str]:
    """Names of semantic models and metrics already declared in the project."""

    ns = project_namespace(view)
    return ns.semantic_models | ns.metrics


def time_spine_warning(
    view: DbtProjectView, parsed_edits: list[dict[str, Any]]
) -> str | None:
    """Warn when semantic YAML lands in a project with no MetricFlow time spine.

    dbt refuses to parse a project that has semantic models but no time spine
    model, so a first semantic plan applied without one produces a project that
    cannot build. The engine cannot author the model itself (propose-don't-impose), so
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
) -> dict[str, list[str]]:
    """Classify every proposed name as new or existing; enforce the strict modes.

    ``define`` refuses existing names and ``update`` refuses new ones (both are
    typo guards); ``plan`` accepts a mix, so one payload can evolve existing
    definitions and add the helpers they depend on. Returns the classification:
    ``{"defined": [new names], "updated": [existing names]}``.
    """

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
                "`semantic update` to evolve an existing definition, or "
                "`semantic plan` to mix new and existing names"
            )
    elif mode == "update":
        missing = sorted(proposed - existing)
        if missing:
            raise EditValidationError(
                f"not defined in the project: {', '.join(missing)}; use "
                "`semantic define` to add a new definition, or "
                "`semantic plan` to mix new and existing names"
            )
    return {
        "defined": sorted(proposed - existing),
        "updated": sorted(proposed & existing),
    }


def check_references(parsed_edits: list[dict[str, Any]], view: DbtProjectView) -> None:
    """Resolve every metric input against the merged namespace (project plus
    the proposed edits, so references across edits in one payload work).

    The jsonschema validation is shape-only: a metric whose input names nothing
    real still passes it and then fails ``dbt build`` two commands later. The
    load-bearing rule is dbt's: ratio and derived inputs reference *metrics*,
    not measures, and a measure only becomes a metric via ``create_metric``.
    All problems are collected and raised together so one round-trip fixes the
    payload.
    """

    project_ns = project_namespace(view)
    proposed_ns = collect_namespace(parsed_edits)
    metrics = project_ns.metrics | proposed_ns.metrics
    measures = project_ns.measures | proposed_ns.measures

    problems: list[str] = []

    def metric_ref(metric_name: str, role: str, value: Any) -> None:
        name = _ref_name(value)
        if name is None or name in metrics:
            return
        if name in measures:
            problems.append(
                f"metric '{metric_name}': {role} '{name}' is a measure, not a "
                "metric; add create_metric: true to the measure or reference "
                "a metric"
            )
        else:
            problems.append(
                f"metric '{metric_name}': {role} references unknown metric '{name}'"
            )

    def measure_ref(metric_name: str, role: str, value: Any) -> None:
        name = _ref_name(value)
        if name is not None and name not in measures:
            problems.append(
                f"metric '{metric_name}': {role} references unknown measure '{name}'"
            )

    for parsed in parsed_edits:
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("metrics") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            name = entry["name"]
            metric_type = str(entry.get("type", "")).lower()
            params = entry.get("type_params")
            if not isinstance(params, dict):
                continue
            if metric_type in {"simple", "cumulative"}:
                measure_ref(name, "measure", params.get("measure"))
            elif metric_type == "ratio":
                metric_ref(name, "numerator", params.get("numerator"))
                metric_ref(name, "denominator", params.get("denominator"))
            elif metric_type == "derived":
                for input_metric in params.get("metrics") or []:
                    metric_ref(name, "input metric", input_metric)
            elif metric_type == "conversion":
                conversion = params.get("conversion_type_params")
                if isinstance(conversion, dict):
                    measure_ref(name, "base_measure", conversion.get("base_measure"))
                    measure_ref(
                        name, "conversion_measure", conversion.get("conversion_measure")
                    )
            # Unknown metric types are left to the schema validator and dbt.
    if problems:
        raise EditValidationError("; ".join(problems))


def _ref_name(value: Any) -> str | None:
    """A metric/measure input is either a bare name or {name: ...}."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


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
