"""Shared semantic request policy and columnar result shaping.

Backends supply evidence and dialect readers.  This module owns the decisions
that must remain identical across them: PII screening, grain validation, values
resolution, and response budgets.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from ...guards import PII_BLOCK_CONFIDENCE
from ...guards.pii_meta import PII_CATEGORY, says_pii
from ...semantic_catalog import SemanticCatalogView
from ..profile import detect_pii
from .backend import SemanticBackendError, SemanticQueryRefusedError
from .model import SemanticQuery, ValuesRequest

_GRAIN_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def requested_dimension_refs(
    q: SemanticQuery, *, filter_refs: Callable[[list[str]], list[str]] | None
) -> list[str]:
    """Return every grouped or filtered dimension, de-duplicated in order."""

    refs = list(q.group_by)
    if q.where:
        found = filter_refs(list(q.where)) if filter_refs is not None else None
        if found is None:
            raise SemanticQueryRefusedError(
                "refused: this semantic backend cannot read the dimensions its own "
                "filter dialect names, so a filtered query cannot be screened for "
                "PII. PII is flagged, never surfaced; move the condition into "
                "--group-by, or query a backend that reads its filters."
            )
        refs.extend(found)
    return list(dict.fromkeys(refs))


def queryable_grains(
    metrics: list[str], reported: dict[str, list[str]]
) -> list[str] | None:
    """Return the grains shared by every metric, or None when unknown."""

    if not metrics or any(metric not in reported for metric in metrics):
        return None
    return [
        grain
        for grain in reported[metrics[0]]
        if all(grain in reported[metric] for metric in metrics[1:])
    ]


def validate_grain(grain: str | None, *, available: list[str] | None) -> str | None:
    """Normalize a requested grain and validate it against backend evidence."""

    if not grain:
        return None
    if not _GRAIN_TOKEN.fullmatch(grain):
        raise SemanticBackendError(f"invalid time grain: {grain!r}")
    lowered = grain.lower()
    if available is None:
        return lowered
    if not available:
        raise SemanticBackendError(
            f"this metric reports no queryable time grain, so '{lowered}' cannot "
            "be applied; group by a time dimension of the metric instead"
        )
    if lowered not in [value.lower() for value in available]:
        raise SemanticBackendError(
            f"unknown time grain '{lowered}' for this metric; the layer reports "
            f"{', '.join(available)}"
        )
    return lowered


def _meta_clears(meta: Any) -> bool:
    return isinstance(meta, dict) and meta.get("pii") is False


def merge_pii_meta(store: dict[str, Any], name: str | None, value: Any) -> None:
    """Merge dimension metadata with PII winning every disagreement."""

    if name is None:
        return
    current = store.get(name)
    if says_pii(current):
        return
    if says_pii(value) or current is None:
        store[name] = value


#: What a lookup says about a computed element whose expression could not be
#: read: there is no column to screen, so the gate cannot clear it.
UNREADABLE = "unreadable"
#: The columns a computed element's expression reads, as a lookup reports them,
#: so the name heuristic screens what the element actually projects.
READS = "reads"


def _screen_one(name: str, meta_lookup: Callable[[str], Any] | None) -> str | None:
    """Why policy refuses one dimension token, or None when it passes."""

    meta = meta_lookup(name) if meta_lookup is not None else None
    if says_pii(meta):
        category = (
            (meta.get(PII_CATEGORY) or meta.get("category"))
            if isinstance(meta, dict)
            else None
        )
        if not category:
            return "declared PII in the semantic-layer metadata"
        column = meta.get("column") if isinstance(meta, dict) else None
        if column:
            return f"{category} (profiled and flagged, read from {column})"
        return f"{category} (profiled and flagged)"
    if isinstance(meta, dict) and meta.get(UNREADABLE):
        return (
            "computed from an expression dex could not read, so the columns "
            "behind it cannot be screened"
        )
    if _meta_clears(meta):
        return None
    reads = meta.get(READS) if isinstance(meta, dict) else None
    # A dotted token (SLayer's `model.column`) is read as separated words: the
    # heuristic bounds a word with `_`, so `orders.email` would otherwise read as
    # one word and pass. A computed element is read by the columns its
    # expression reads as well as by its own name, since a neutral name over a
    # flagged column is exactly the case the name alone misses.
    for word in [name, *(reads or [])]:
        flag = detect_pii(word.replace(".", "__"), "string")
        if flag is not None and flag.confidence >= PII_BLOCK_CONFIDENCE:
            via = f", read from {word}" if word != name else ""
            return f"{flag.category.value} (name heuristic, {flag.confidence:.2f}{via})"
    return None


def screen_dimension_refs(
    refs: list[str],
    *,
    meta_lookup: Callable[[str], Any] | None = None,
    aliases: Mapping[str, list[str]] | None = None,
) -> list[tuple[str, str]]:
    """Return the requested dimensions that policy must refuse.

    ``aliases`` maps a token as the caller wrote it to the tokens it stands for,
    which are screened in its place: a time token (``metric_time``) stands for
    the time dimensions of the metrics queried, and a token carrying a grain
    suffix for the dimension it buckets. The refusal names the caller's token.
    """

    blocked: list[tuple[str, str]] = []
    for ref in refs:
        for target in (aliases or {}).get(ref) or [ref]:
            reason = _screen_one(target, meta_lookup)
            if reason is not None:
                if target != ref:
                    reason = f"{reason}, as {target}"
                blocked.append((ref, reason))
                break
    return blocked


def unadjudicated_refs(
    refs: list[str],
    *,
    meta_lookup: Callable[[str], Any] | None = None,
    aliases: Mapping[str, list[str]] | None = None,
) -> list[str]:
    """Return refs for which no authoritative source supplied a verdict.

    A token standing for several (see :func:`screen_dimension_refs`) is
    adjudicated only where every one of them is.
    """

    if meta_lookup is None:
        return list(refs)
    unknown: list[str] = []
    for ref in refs:
        for target in (aliases or {}).get(ref) or [ref]:
            meta = meta_lookup(target)
            if not says_pii(meta) and not _meta_clears(meta):
                unknown.append(ref)
                break
    return unknown


def name_only_note(refs: list[str]) -> list[str]:
    """The disclosure that the PII gate screened these dimensions by name alone.

    The gate's evidence is a profiled column's flag or the layer's own metadata,
    and the name heuristic is the floor beneath both. A dimension neither spoke
    to (a column on a relation never profiled, an expression over columns no
    profile covers, a time token the layer could not resolve) passed on the
    shape of its name, and a result that does not say so lets the weaker
    screening pass for the stronger one.
    """

    if not refs:
        return []
    return [
        f"PII screening used the name heuristic alone for {', '.join(refs)}: no "
        "authoritative source spoke to the columns behind them, so they passed on "
        "the shape of their names. Profile the columns behind them, or mark them "
        "in the project that declares them, to make the screening evidence-backed."
    ]


def sub_threshold_note(
    refs: list[str],
    *,
    meta_lookup: Callable[[str], Any] | None = None,
    aliases: Mapping[str, list[str]] | None = None,
) -> list[str]:
    """Warnings for dimensions cleared with a sub-threshold PII flag.

    ``explore query`` surfaces these through the query firewall. The semantic
    path screens at the request and never reaches that firewall, so a clearance
    that carries ``category`` and ``confidence`` must be named here or the
    envelope stays silent.
    """

    if meta_lookup is None or not refs:
        return []
    warnings: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        for target in (aliases or {}).get(ref) or [ref]:
            meta = meta_lookup(target)
            if not isinstance(meta, dict) or not _meta_clears(meta):
                continue
            category = meta.get("category")
            confidence = meta.get("confidence")
            if category is None or confidence is None:
                continue
            label = ref
            if label in seen:
                continue
            seen.add(label)
            warnings.append(
                f"{label} is PII-flagged at confidence {confidence:g}, below the "
                f"{PII_BLOCK_CONFIDENCE:g} block threshold, so the projection ran. "
                "If its values are personal data, drop the column; if it is "
                "reviewed as not PII, record a pii_overrides entry in "
                ".dex/config.yml."
            )
            break
    return warnings


def screen_values_request(
    dimension: str, *, meta_lookup: Callable[[str], Any] | None = None
) -> list[str]:
    """Refuse a PII value domain and return notes for heuristic-only screening."""

    blocked = screen_dimension_refs([dimension], meta_lookup=meta_lookup)
    if blocked:
        _ref, reason = blocked[0]
        raise SemanticQueryRefusedError(
            f"refused: {dimension} is PII ({reason}), and this command returns "
            "nothing but the values of one dimension, so there is no aggregate to "
            "fall back to. PII is flagged, never surfaced. Ask for a different "
            "dimension; one reviewed as not PII is cleared durably with a "
            "pii_overrides entry in .dex/config.yml, or with "
            "`config: {meta: {pii: false}}` on the dimension in the project that "
            "declares it."
        )
    if not unadjudicated_refs([dimension], meta_lookup=meta_lookup):
        return []
    return [
        f"PII screening used the name heuristic alone for {dimension}: no "
        "authoritative source spoke to it, so its values passed on the shape of "
        "its name. Profile the column behind it, or mark it in the project that "
        "declares it, to make the screening evidence-backed."
    ]


def resolve_values_request(
    view: SemanticCatalogView,
    dimension: str,
    metrics: list[str],
    *,
    grains: tuple[str, ...] | None = None,
    spelling: str = "entity-qualified (user__pricing_tier)",
) -> ValuesRequest:
    """Resolve a dimension token and optional metric scope against a catalog.

    ``grains`` is the vocabulary a grain suffix is read from. Absent, it is the
    grains the catalog reports, else MetricFlow's standard set; a layer whose
    catalog declares no grains and whose built-in set differs passes its own.
    ``spelling`` is how the layer spells a dimension token, for the refusal of
    one it does not have.
    """

    from ...metricflow_dialect import STANDARD_GRAINS, split_grain

    token = (dimension or "").strip()
    if not token:
        raise SemanticBackendError(
            "a values request needs one dimension (discover them with "
            "`explore semantic list`)"
        )
    grains = grains or (
        tuple(
            dict.fromkeys(
                grain
                for element in (*view.metrics, *view.dimensions)
                for grain in (element.queryable_granularities or ())
            )
        )
        or STANDARD_GRAINS
    )
    name, grain = split_grain(token, None, grains=grains)
    reachable, unknown = view.metrics_for_dimensions([name])
    if unknown:
        raise SemanticBackendError(
            f"no such dimension in this semantic layer: {name}. List what it "
            "exposes with `explore semantic list`, and note that the token is "
            f"{spelling} rather than the bare column name"
        )

    wanted = list(dict.fromkeys(metrics or []))
    known = {metric.name for metric in view.metrics}
    missing = [metric for metric in wanted if metric not in known]
    if missing:
        raise SemanticBackendError(
            f"no such metric in this semantic layer: {', '.join(missing)}. "
            "List what it exposes with `explore semantic list`"
        )
    return ValuesRequest(
        token=token,
        name=name,
        grain=grain,
        metrics=wanted,
        reachable=sorted(reachable),
        grains=grains,
    )


def values_reach_note(dimension: str, used: list[str], reachable: list[str]) -> str:
    """Disclose when dex selected a metric to reach a joined dimension."""

    others = [name for name in sorted(reachable) if name not in used]
    alternatives = (
        f" {len(others)} other metric(s) reach it, including {', '.join(others[:3])}."
        if others
        else ""
    )
    return (
        f"{dimension} is reached through a join, so its values could only be read "
        f"in the context of a metric; dex used {', '.join(used)}. These are "
        "therefore the values present for that metric, which can be narrower than "
        f"the column's own domain.{alternatives} Pass --metric to choose."
    )


def cap_columnar(
    columns: list[str],
    types: list[str],
    cells: list[list[Any]],
    *,
    max_rows: int,
    max_cell_chars: int,
    max_payload_bytes: int,
    truncated_by_source: bool = False,
    extra_notes: list[str] | None = None,
) -> dict[str, Any]:
    """Apply row, cell-width, and payload-byte limits to a columnar result."""

    notes = list(extra_notes or [])
    truncated = truncated_by_source
    if len(cells) > max_rows:
        cells = cells[:max_rows]
        truncated = True
    # Row-cap disclosure matches plain `explore query`: when the adapter already
    # returned exactly max_rows with truncated=True, len(cells) alone would miss it.
    if truncated:
        notes.append(
            f"result truncated to {max_rows} rows (engine cap); refine "
            "the query, or raise query.max_rows in .dex/config.yml"
        )

    clipped = 0
    shaped_cells: list[list[Any]] = []
    for row in cells:
        shaped: list[Any] = []
        for value in row:
            if isinstance(value, str) and len(value) > max_cell_chars:
                shaped.append(value[:max_cell_chars] + "...")
                clipped += 1
            else:
                shaped.append(value)
        shaped_cells.append(shaped)
    if clipped:
        notes.append(f"{clipped} cell(s) truncated to {max_cell_chars} chars")

    dropped = 0
    while shaped_cells and (
        len(json.dumps(shaped_cells, default=str)) > max_payload_bytes
    ):
        shaped_cells.pop()
        dropped += 1
    if dropped:
        truncated = True
        notes.append(
            f"dropped {dropped} row(s) to fit the {max_payload_bytes}-byte payload "
            "cap; aggregate further or select fewer columns"
        )
    return {
        "columns": columns,
        "types": types,
        "cells": shaped_cells,
        "row_count": len(shaped_cells),
        "truncated": truncated,
        "notes": notes,
    }
