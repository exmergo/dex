"""The semantic-layer query surface: one intent, one envelope, two backends.

``explore semantic`` lets an agent discover metrics and dimensions and run
governed metric queries against the dbt semantic layer. Two backends share the
intent grammar and the columnar envelope but differ in who executes and how spend
and PII are governed:

- ``local`` (:mod:`.local`): MetricFlow renders the metric SQL with ``explain()``
  and dex executes it through its own connector, cost guard, and PII request-gate.
  A dbt project must be present, the way DuckDB needs a local file.
- ``dbt_cloud`` (:mod:`.hosted`): the query is sent to a hosted dbt Cloud Semantic
  Layer over GraphQL and needs no local project, the way BigQuery needs no local
  DuckDB. dbt Cloud owns the warehouse connection and executes server-side, so
  dex's cost guard is structurally unavailable on that path (every hosted result
  says so) and PII is gated from the layer's own metadata plus a name heuristic.

Backend selection is ambient, mirroring how the warehouse connector resolves: the
``.dex/config.yml`` ``semantic.backend`` default, overridable per command with
``--local`` / ``--api``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

# The confidence at or above which a name-detected PII category refuses a query,
# shared with the query firewall so the two surfaces block at the same threshold.
from ...guards.query_firewall import PII_BLOCK_CONFIDENCE
from ..profile import detect_pii


@dataclass
class SemanticQuery:
    """A backend-neutral metric query: the grammar shared by MetricFlow, the dbt
    Cloud GraphQL API, and the JDBC macro.

    ``group_by`` tokens are entity-qualified dimension names (``user__pricing_tier``,
    ``metric_time``). ``grain`` applies to ``metric_time`` when the caller wants a
    time bucket without spelling it into the token; ``where`` clauses use the Jinja
    filter dialect (``{{ Dimension('session__is_deleted') }} = false``) verbatim on
    both backends.
    """

    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    grain: str | None = None
    limit: int | None = None


@dataclass
class MetricInfo:
    name: str
    type: str
    label: str | None = None
    description: str | None = None
    # The dimensions this metric can be grouped by, entity-qualified. Precise on
    # the hosted backend (the API resolves the join graph); on the local read-view
    # it is the per-semantic-model listing, noted as such.
    dimensions: list[str] = field(default_factory=list)


@dataclass
class DimensionInfo:
    name: str
    type: str


@dataclass
class EntityInfo:
    name: str
    type: str


@dataclass
class SemanticCatalog:
    """What ``explore semantic list`` returns: enough for an agent to discover what
    it can query, in the same shape from either backend."""

    backend: str
    metrics: list[MetricInfo] = field(default_factory=list)
    dimensions: list[DimensionInfo] = field(default_factory=list)
    entities: list[EntityInfo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_data(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "metrics": [asdict(m) for m in self.metrics],
            "dimensions": [asdict(d) for d in self.dimensions],
            "entities": [asdict(e) for e in self.entities],
            "notes": self.notes,
        }


class SemanticBackendError(Exception):
    """A backend cannot be constructed or reached: a missing extra, missing hosted
    coordinates, missing credentials, or a missing local project. The message names
    the fix; the command turns it into a clean ``env.error`` (never a stack trace)."""


class SemanticBackend(Protocol):
    """The seam both backends satisfy. ``query`` returns a full envelope rather
    than raw rows because the two paths differ in cost surfacing and warnings, and
    each owns its own posture."""

    name: str

    def list_definitions(self) -> SemanticCatalog: ...

    def query(self, q: SemanticQuery): ...


# ---- shared PII screening --------------------------------------------------
#
# A metric query touches dimensions two ways: the group_by tokens, and the
# Dimension()/TimeDimension()/Entity() refs inside a where filter. Both are
# screened, because grouping by an email is as much a disclosure as filtering by
# one and then projecting it.

_DIMENSION_REF = re.compile(r"(?:Time)?Dimension\(\s*['\"]([^'\"]+)['\"]")
_ENTITY_REF = re.compile(r"Entity\(\s*['\"]([^'\"]+)['\"]")
# Meta keys, on a dimension's dbt `config.meta`, that authoritatively mark it PII.
_PII_META_KEYS = ("pii", "contains_pii", "is_pii", "pii_category")


def requested_dimension_refs(q: SemanticQuery) -> list[str]:
    """Every dimension/entity token a query would touch, de-duplicated in order."""

    refs: list[str] = [*q.group_by]
    for clause in q.where:
        refs.extend(_DIMENSION_REF.findall(clause))
        refs.extend(_ENTITY_REF.findall(clause))
    seen: set[str] = set()
    ordered: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def _meta_says_pii(meta: Any) -> bool:
    return isinstance(meta, dict) and any(bool(meta.get(key)) for key in _PII_META_KEYS)


def _meta_clears(meta: Any) -> bool:
    """Whether a lookup positively adjudicated the ref as not PII.

    Only an explicit ``{"pii": False}`` clears: a lookup that knows nothing returns
    None and leaves the name heuristic in charge. This is what lets a profiled,
    value-evidence-cleared column (or a human ``pii_overrides`` entry) stop being
    re-blocked by its name, without that silence ever being mistaken for consent.
    """

    return isinstance(meta, dict) and meta.get("pii") is False


def screen_dimension_refs(
    refs: list[str],
    *,
    meta_lookup: Callable[[str], Any] | None = None,
) -> list[tuple[str, str]]:
    """Refuse verdicts for the refs that must not be queried, as ``(ref, reason)``.

    Evidence beats names, and silence never clears. A lookup that positively knows
    the ref (the ``.dex/`` cache's value-evidence flags on the resolved physical
    column, or a dimension's dbt ``config.meta``) decides in both directions; a
    lookup that returns nothing falls through to the name heuristic, which is the
    fail-closed floor because a false positive is the wanted error direction on PII.
    Runs on the entity-qualified token (``user__email``), whose bounded ``_email``
    suffix still matches the email pattern, so no join-graph resolution is needed
    when nothing authoritative is available.
    """

    blocked: list[tuple[str, str]] = []
    for ref in refs:
        meta = meta_lookup(ref) if meta_lookup is not None else None
        if _meta_says_pii(meta):
            category = meta.get("category") if isinstance(meta, dict) else None
            reason = (
                f"{category} (profiled and flagged)"
                if category
                else "declared PII in the semantic-layer metadata"
            )
            blocked.append((ref, reason))
            continue
        if _meta_clears(meta):
            continue
        flag = detect_pii(ref, "string")
        if flag is not None and flag.confidence >= PII_BLOCK_CONFIDENCE:
            blocked.append(
                (ref, f"{flag.category.value} (name heuristic, {flag.confidence:.2f})")
            )
    return blocked


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
    """Cap a columnar result for agent context, matching ``explore query`` shaping:
    per-cell width truncation, a hard row cap, and a total payload byte cap, each
    cut announced in ``notes`` so a trimmed result is never mistaken for complete.
    Shared by both backends so their envelopes are identical in shape."""

    import json

    notes: list[str] = list(extra_notes or [])
    truncated = truncated_by_source

    if len(cells) > max_rows:
        cells = cells[:max_rows]
        truncated = True
        notes.append(
            f"result truncated to {max_rows} rows (engine cap); refine the query "
            "or raise query.max_rows in .dex/config.yml"
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


def resolve_backend(args, config, repo_root: str) -> SemanticBackend:
    """The ambient backend resolution: ``--api``/``--local`` override the
    ``.dex/config.yml`` ``semantic.backend`` default. Raises
    :class:`SemanticBackendError` (never a bare import error) when the chosen
    backend's extra, config, or credentials are missing."""

    want_api = bool(getattr(args, "api", False))
    want_local = bool(getattr(args, "local", False))
    if want_api and want_local:
        raise SemanticBackendError("choose one of --local or --api, not both")

    if want_api:
        backend = "dbt_cloud"
    elif want_local:
        backend = "local"
    else:
        configured = getattr(getattr(config, "semantic", None), "backend", None)
        backend = (configured or "local").strip().lower()

    if backend in {"dbt_cloud", "api", "cloud"}:
        from .hosted import HostedDbtCloudBackend

        return HostedDbtCloudBackend.from_config(args, config)
    if backend == "local":
        from .local import LocalMetricFlowBackend

        return LocalMetricFlowBackend.from_args(args, config, repo_root)
    raise SemanticBackendError(
        f"unknown semantic backend '{backend}'; use 'local' or 'dbt_cloud' "
        "(or pass --local / --api)"
    )
