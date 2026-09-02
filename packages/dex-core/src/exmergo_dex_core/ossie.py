# ruff: noqa: E501
"""Native Apache Ossie document reading.

This module deliberately does not use MetricFlow or the upstream Ossie Python
package.  Ossie 0.2 is a draft interchange schema and Dex pins the small,
portable read surface it supports instead of making an existing dbt deployment
depend on an unreleased package.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from .errors import ProjectError
from .semantic_catalog import (
    DimensionInfo,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
    column_reference,
)

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def _context(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, str):
        return None, value
    if isinstance(value, dict):
        return value.get("instructions"), ", ".join(value.get("synonyms") or []) or None
    return None, None


def _expression(value: Any, connector: str | None) -> tuple[str | None, dict[str, str]]:
    dialects = value.get("dialects", []) if isinstance(value, dict) else []
    found = {
        str(d.get("dialect")): str(d.get("expression"))
        for d in dialects
        if isinstance(d, dict) and d.get("dialect") and d.get("expression")
    }
    preferred = {
        "snowflake": "SNOWFLAKE",
        "bigquery": "BIGQUERY",
        "databricks": "DATABRICKS",
    }.get((connector or "").lower())
    return (found.get(preferred) if preferred else None) or found.get(
        "ANSI_SQL"
    ) or next(iter(found.values()), None), found


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.suffix == ".json"
            else yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ProjectError(f"could not read Ossie document '{path}': {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("semantic_model"), list):
        raise ProjectError(f"{path}: Ossie document needs a semantic_model list")
    version = raw.get("version")
    if version and not str(version).startswith("0.2"):
        raise ProjectError(
            f"{path}: Dex currently supports pinned Ossie 0.2 documents, got version {version!r}"
        )
    return raw


def catalog(
    repo_root: Path, files: list[str], connector: str | None = None
) -> SemanticCatalogView:
    """Read configured native Ossie files into Dex's vendor-neutral catalog."""

    repo_root = Path(repo_root)
    models: list[SemanticModelInfo] = []
    dimensions: list[DimensionInfo] = []
    metrics: list[MetricInfo] = []
    physical: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    for name in files:
        path = (repo_root / name).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ProjectError(f"Ossie file escapes repository: {name}") from exc
        doc = _load(path)
        for semantic in doc["semantic_model"]:
            if not isinstance(semantic, dict) or not semantic.get("name"):
                raise ProjectError(f"{path}: every Ossie semantic model needs a name")
            semantic_name = str(semantic["name"])
            if semantic_name in seen:
                raise ProjectError(f"duplicate Ossie semantic model '{semantic_name}'")
            seen.add(semantic_name)
            datasets = semantic.get("datasets") or []
            dataset_names: set[str] = set()
            for dataset in datasets:
                if (
                    not isinstance(dataset, dict)
                    or not dataset.get("name")
                    or not dataset.get("source")
                ):
                    raise ProjectError(f"{path}: Ossie datasets need name and source")
                ds = str(dataset["name"])
                if ds in dataset_names:
                    raise ProjectError(
                        f"{path}: duplicate dataset '{ds}' in '{semantic_name}'"
                    )
                dataset_names.add(ds)
                # Dataset names are namespaced internally, allowing two Ossie
                # documents to use an ordinary name such as `orders`.
                model_name = f"{semantic_name}.{ds}"
                models.append(
                    SemanticModelInfo(
                        name=model_name,
                        label=ds,
                        description=dataset.get("description"),
                        relation=str(dataset["source"]),
                    )
                )
                for field in dataset.get("fields") or []:
                    if not isinstance(field, dict) or not field.get("name"):
                        raise ProjectError(
                            f"{path}: fields in '{semantic_name}.{ds}' need a name"
                        )
                    field_name = str(field["name"])
                    expr, alternatives = _expression(field.get("expression"), connector)
                    column = column_reference(expr, field_name)
                    token = f"{ds}__{field_name}"
                    # Ossie fields are row-level attributes.  `dimension` adds a
                    # temporal role; it does not make the other fields unusable.
                    dimension = field.get("dimension") or {}
                    is_time = bool(dimension.get("is_time")) or (
                        dimension.get("is_time") is None
                        and field.get("datatype")
                        in {"Date", "Time", "DateTime", "DateTimeTz"}
                    )
                    dimensions.append(
                        DimensionInfo(
                            name=token,
                            definition=field_name,
                            type="time" if is_time else "categorical",
                            label=field.get("label"),
                            description=field.get("description"),
                            semantic_model=model_name,
                            column=column,
                        )
                    )
                    if column:
                        physical[token] = (str(dataset["source"]), column)
                    # Preserve dialect choices as catalog metadata only.  They are
                    # deliberately not treated as a runtime query contract.
                    if alternatives:
                        dimensions[-1].queryable_granularities = []
            all_dims = [
                f"{d.get('name')}__{f.get('name')}"
                for d in datasets
                if isinstance(d, dict)
                for f in (d.get("fields") or [])
                if isinstance(f, dict) and f.get("name")
            ]
            for metric in semantic.get("metrics") or []:
                if not isinstance(metric, dict) or not metric.get("name"):
                    raise ProjectError(f"{path}: every Ossie metric needs a name")
                expr, alternatives = _expression(metric.get("expression"), connector)
                referenced = {
                    match.group(1) for match in _QUALIFIED.finditer(expr or "")
                }
                metric_models = [
                    f"{semantic_name}.{ds}" for ds in sorted(referenced & dataset_names)
                ] or [f"{semantic_name}.{ds}" for ds in sorted(dataset_names)]
                metrics.append(
                    MetricInfo(
                        name=str(metric["name"]),
                        type="expression",
                        label=None,
                        description=metric.get("description"),
                        dimensions=all_dims,
                        semantic_models=metric_models,
                        composition=None,
                        vendor_params={
                            "ossie": {
                                "expression": expr,
                                "dialects": alternatives,
                                "datatype": metric.get("datatype"),
                            }
                        },
                    )
                )
    return SemanticCatalogView(
        semantic_models=models,
        dimensions=dimensions,
        metrics=metrics,
        notes=[
            "Ossie is an interchange catalog: Dex lists native definitions but does not execute generic Ossie metric queries."
        ],
        physical_columns=physical,
    )
