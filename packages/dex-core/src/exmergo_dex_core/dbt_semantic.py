"""Compiled dbt semantic-manifest reading and MetricFlow path resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .metricflow_dialect import METRIC_TIME, STANDARD_GRAINS, order_grains


def read_semantic_manifest(
    project: Path, manifest_path: str
) -> tuple[dict[str, Any], str] | None:
    """Return a usable compiled semantic manifest and its original text."""

    path = project / manifest_path
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("semantic_models"):
        return payload, text
    return None


@dataclass(frozen=True)
class ResolvedPath:
    """One queryable group-by token and the declaration it reaches."""

    token: str
    definition: str | None = None
    semantic_model: str | None = None
    type: str = ""
    grains: tuple[str, ...] = ()


def resolve_group_by_paths(manifest_text: str) -> dict[str, list[ResolvedPath]] | None:
    """Resolve every metric's group-by paths, degrading when MetricFlow cannot."""

    try:
        from metricflow_semantics.model.dbt_manifest_parser import (
            parse_manifest_from_dbt_generated_manifest,
        )
        from metricflow_semantics.model.semantic_manifest_lookup import (
            SemanticManifestLookup,
        )
    except ImportError:
        return None

    try:
        lookup = SemanticManifestLookup(
            parse_manifest_from_dbt_generated_manifest(manifest_text)
        ).metric_lookup
        return _resolve_through(lookup)
    except Exception:
        return None


def _resolve_through(lookup: Any) -> dict[str, list[ResolvedPath]]:
    resolved: dict[str, list[ResolvedPath]] = {}
    for reference in lookup.metric_references:
        found: dict[str, dict[str, Any]] = {}
        specs = lookup.get_common_group_by_items(
            metric_references=(reference,)
        ).annotated_specs
        for spec in specs:
            kind = spec.element_type.name
            if (
                kind not in ("DIMENSION", "TIME_DIMENSION")
                or spec.date_part is not None
            ):
                continue
            token = "__".join([*spec.entity_link_names, spec.element_name])
            entry = found.setdefault(
                token, {"grains": [], "time": False, "models": set(), "name": None}
            )
            entry["time"] = entry["time"] or kind == "TIME_DIMENSION"
            entry["name"] = spec.element_name
            entry["models"].update(spec.origin_semantic_model_names)
            grain = getattr(spec.time_grain, "name", None)
            if grain and grain not in entry["grains"]:
                entry["grains"].append(grain)

        paths: list[ResolvedPath] = []
        for token, entry in sorted(found.items()):
            models = sorted(entry["models"])
            synthesized = token == METRIC_TIME
            paths.append(
                ResolvedPath(
                    token=token,
                    definition=None if synthesized else entry["name"],
                    semantic_model=(
                        None if synthesized or len(models) != 1 else models[0]
                    ),
                    type="time" if entry["time"] else "categorical",
                    grains=tuple(order_grains(entry["grains"])),
                )
            )
        resolved[reference.element_name] = paths
    return resolved


def grains_from(base: str | None, custom: tuple[str, ...] = ()) -> list[str] | None:
    """Return standard grains at or coarser than a declared base grain."""

    if not base or base.lower() not in STANDARD_GRAINS:
        return None
    floor = STANDARD_GRAINS.index(base.lower())
    return [*STANDARD_GRAINS[floor:], *custom]
