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
``.dex/config.yml`` ``semantic.vendor`` and ``semantic.deployment`` defaults (or
the released ``semantic.backend`` spelling of the two), overridable per command
with ``--local`` / ``--api``.

Those two flags name the **execution** axis, not a vendor: dex renders and runs
the statement, versus the vendor runs it. That is the axis the guards read, so
every backend declares it as ``execution`` (``dex`` or ``vendor``) and every
result carries it. A vendor-executed backend cannot be cost-guarded by dex at
all, and :func:`cost_posture` is where that follows from the declaration rather
than from each backend restating it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from ...errors import DexError

# The confidence at or above which a name-detected PII category refuses a query,
# shared with the query firewall so the two surfaces block at the same threshold.
# Imported from the guards package, not from the firewall: this package must stay
# importable without a dialect engine, because the hosted backend parses no SQL.
from ...guards import PII_BLOCK_CONFIDENCE
from ...semantic_catalog import (
    DIMENSIONS_PER_DECLARATION,
    DIMENSIONS_PER_QUERYABLE_PATH,
    DimensionInfo,
    EntityInfo,
    EntityRole,
    MeasureInfo,
    MetricComposition,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
    derive_entity_type,
    merge_element_fields,
    qualified_dimension,
)
from ..profile import detect_pii


def _split_tokens(raw: list[str]) -> list[str]:
    """Flatten repeated and comma-joined name lists into one clean token list.

    A metric, dimension, or entity name is an identifier, so a comma can never be
    part of one and splitting on it is lossless. Never apply this to ``where``,
    whose Jinja clauses carry commas of their own
    (``{{ TimeDimension('metric_time', 'month') }}``).
    """

    return [part.strip() for entry in raw for part in entry.split(",") if part.strip()]


@dataclass
class SemanticQuery:
    """A backend-neutral metric query: the grammar shared by MetricFlow, the dbt
    Cloud GraphQL API, and the JDBC macro.

    ``group_by`` tokens are entity-qualified dimension names (``user__pricing_tier``,
    ``metric_time``). ``grain`` applies to ``metric_time`` when the caller wants a
    time bucket without spelling it into the token; ``where`` clauses use the Jinja
    filter dialect (``{{ Dimension('session__is_deleted') }} = false``) verbatim on
    both backends.

    Name lists normalize here rather than at the CLI, so the two backends and a
    library caller building the query object directly all see the same tokens: a
    comma-joined list (``--group-by a,b``) is as natural a first guess as the
    repeated flag, and mixing the two is natural too.
    """

    metrics: list[str]
    group_by: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    grain: str | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        self.metrics = _split_tokens(self.metrics)
        self.group_by = _split_tokens(self.group_by)
        self.order_by = _split_tokens(self.order_by)


# The catalog's element models are neutral and shared with the project seam, so
# they live in `exmergo_dex_core.semantic_catalog` rather than here. Re-exported
# because both backends and every existing consumer import them from this module.
__all__ = [
    "DIMENSIONS_PER_DECLARATION",
    "DIMENSIONS_PER_QUERYABLE_PATH",
    "DimensionInfo",
    "EntityInfo",
    "EntityRole",
    "MeasureInfo",
    "MetricComposition",
    "MetricInfo",
    "SemanticBackend",
    "SemanticBackendError",
    "SemanticCatalog",
    "SemanticModelInfo",
    "SemanticQuery",
    "SemanticQueryRefusedError",
    "backend_axes",
    "cap_columnar",
    "cost_posture",
    "derive_entity_type",
    "merge_element_fields",
    "qualified_dimension",
    "requested_dimension_refs",
    "resolve_backend",
    "screen_dimension_refs",
]


def _element_data(element: Any) -> dict[str, Any]:
    """One catalog element as a dict, with its unset optional fields omitted, at
    every level.

    A catalog is agent context, so a project that declares no labels should not
    pay for null placeholders: measured against our own deployment (which
    populated ``label`` on 0 of 65 dimensions at the time the rule was
    introduced), they were most of the dimensions and entities blocks. Absent
    means unset, the same rule on every element kind.

    It recurses because composition is a sparse record: a simple metric has no
    numerator, and each metric type filling only the parts that apply to it is
    what makes an absent key mean "this metric has no such part" rather than
    "unknown". A nested record that prunes away to nothing is dropped whole, so a
    metric whose composition dex could not read carries no empty shell.

    An empty *list* survives, because "no groupable dimensions" is an answer where
    None is not.
    """

    def prune(value: Any) -> Any:
        if isinstance(value, dict):
            kept = {k: prune(v) for k, v in value.items() if v is not None}
            return {k: v for k, v in kept.items() if v != {}}
        if isinstance(value, list):
            return [prune(v) for v in value]
        return value

    return prune(asdict(element))


@dataclass
class SemanticCatalog(SemanticCatalogView):
    """What ``explore semantic list`` returns: enough for an agent to discover what
    it can query, in the same shape from either backend.

    The five lists and ``dimension_scope`` are the neutral view every project
    format and every backend produces; this adds what only the answering backend
    knows. ``backend``, ``vendor``, ``deployment`` and ``execution`` are its
    provenance. ``unavailable`` is its **declared gaps**: which fields of which
    element kind it structurally cannot supply, so a caller can tell "the project
    declared none" from "this path cannot carry it".

    That distinction is in the payload rather than in a note on purpose. A note is
    prose, and prose is the first thing a caller with a context window truncates;
    an absence that a consumer must branch on has to be machine-readable. Each
    backend declares its gaps once, as a class attribute, the way it declares its
    execution axis.
    """

    backend: str = ""
    vendor: str = ""
    deployment: str = ""
    execution: str = ""
    unavailable: dict[str, list[str]] = field(default_factory=dict)
    # Which metrics this catalog was narrowed to, empty when it is the whole
    # layer. In the payload because a subset that cannot be told apart from a
    # complete answer is the failure mode worth designing against: a caller
    # reading a scoped catalog as the layer concludes the rest does not exist.
    scoped_to: list[str] = field(default_factory=list)

    @classmethod
    def from_backend(cls, backend: Any, **fields: Any) -> SemanticCatalog:
        """Build with the answering backend's own provenance and declared gaps
        filled in, so a new backend states both once as class attributes rather
        than spelling them into every catalog it returns."""

        # Explicit fields win over the declarations, so a backend building from a
        # project format's own view carries what that format actually produced
        # rather than what the backend declares in the general case.
        declared = {**backend_axes(backend), **catalog_declarations(backend)}
        return cls(**{**declared, **fields})

    @classmethod
    def from_view(
        cls, view: SemanticCatalogView, backend: Any, **fields: Any
    ) -> SemanticCatalog:
        """Build from a project format's neutral view, adding only what the
        backend knows.

        ``physical_columns`` is deliberately not carried across: it is how the
        format resolves a token for the PII gate, not part of the catalog a caller
        reads, and :meth:`to_data` names its keys explicitly so nothing leaks by
        being present on the dataclass.
        """

        notes = [*view.notes, *fields.pop("notes", [])]
        return cls.from_backend(
            backend,
            semantic_models=view.semantic_models,
            metrics=view.metrics,
            dimensions=view.dimensions,
            entities=view.entities,
            measures=view.measures,
            dimension_scope=view.dimension_scope,
            notes=notes,
            **fields,
        )

    def to_data(self) -> dict[str, Any]:
        """The payload, provenance first.

        Key order in JSON carries no meaning to a parser and is decisive for an
        agent reading a truncated result, so the scalars that say which layer
        answered and what it could not answer lead, and the long lists follow.
        """

        payload: dict[str, Any] = {
            "backend": self.backend,
            "vendor": self.vendor,
            "deployment": self.deployment,
            "execution": self.execution,
            "dimension_scope": self.dimension_scope,
        }
        if self.scoped_to:
            payload["scoped_to"] = self.scoped_to
        if self.unavailable:
            payload["unavailable"] = self.unavailable
        payload.update(
            {
                "semantic_models": [_element_data(m) for m in self.semantic_models],
                "metrics": [_element_data(m) for m in self.metrics],
                "dimensions": [_element_data(d) for d in self.dimensions],
                "entities": [_element_data(e) for e in self.entities],
                "measures": [_element_data(m) for m in self.measures],
                "notes": self.notes,
            }
        )
        return payload


class SemanticBackendError(DexError):
    """A backend cannot be constructed, reached, or asked what was asked of it: a
    missing extra, missing hosted coordinates, missing credentials, a missing local
    project, an unresolvable metric. The message names the fix; the caller turns it
    into a clean error (never a stack trace)."""


class SemanticQueryRefusedError(SemanticBackendError):
    """The query was understood and deliberately not run.

    A subclass so a single ``except SemanticBackendError`` still catches it, while
    a caller that cares can tell "dex said no" apart from "the backend broke".
    Today that means a PII request-gate refusal or rendered SQL that was not
    read-only, both of which are policy, not failure.
    """


class SemanticBackend(Protocol):
    """The seam both backends satisfy.

    ``query`` returns a ``SemanticQueryResult``, not an envelope: the two paths
    genuinely differ in cost surfacing and warnings, and each owns its own
    posture, but a posture is data on the result (a cost paradigm, a warning),
    not a transport object. Backends that built their own envelopes made the
    engine impossible to call from anything but a CLI.

    A backend that cannot answer raises :class:`SemanticBackendError`; the caller
    turns that into a clean error rather than a stack trace.

    The four names are provenance, declared once per backend rather than restated
    at each construction site. ``execution`` is the load-bearing one: it says who
    runs the statement, and therefore whether dex's cost guard can apply. A
    backend that answers ``vendor`` inherits the whole no-guard posture from
    :func:`cost_posture` instead of assembling it again.

    ``catalog_gaps`` and ``dimension_scope`` are the same idea applied to the
    catalog. A backend states, once, which fields of which element kind it cannot
    supply and what one dimension row of its catalog is, and both travel into the
    payload as data. That is what keeps a structural absence from having to be
    inferred from a missing key or read out of a note, and it is why two backends
    reporting different dimension counts for one layer is now an explained
    difference rather than an unexplained one.
    """

    name: str
    vendor: str
    deployment: str
    execution: str
    catalog_gaps: dict[str, list[str]]
    dimension_scope: str

    def list_definitions(self) -> SemanticCatalog: ...

    def query(self, q: SemanticQuery): ...


# Who runs the statement. `dex` renders it and executes it through its own
# connector, so the cost guard applies in full; `vendor` means the semantic layer
# owns the warehouse connection and dex never sees a statement it could price or
# cap.
EXECUTION_DEX = "dex"
EXECUTION_VENDOR = "vendor"


def backend_axes(backend: Any) -> dict[str, str]:
    """A backend's provenance as payload fields, read off the backend itself."""

    return {
        "backend": getattr(backend, "name", ""),
        "vendor": getattr(backend, "vendor", ""),
        "deployment": getattr(backend, "deployment", ""),
        "execution": getattr(backend, "execution", ""),
    }


def catalog_declarations(backend: Any) -> dict[str, Any]:
    """A backend's catalog declarations as payload fields, read off the backend.

    Separate from :func:`backend_axes` because the two answer different
    questions (which layer answered, versus what its catalog can and cannot say)
    and a backend may reasonably declare one and not the other. Read with
    ``getattr`` defaults so a duck-typed backend, including a test double
    narrower than the protocol, still produces a well-formed catalog.
    """

    return {
        "unavailable": dict(getattr(backend, "catalog_gaps", None) or {}),
        "dimension_scope": getattr(backend, "dimension_scope", None)
        or DIMENSIONS_PER_DECLARATION,
    }


def cost_posture(backend: Any) -> tuple[Any, list[str]]:
    """The cost stance that follows from who executes, as ``(cost, warnings)``.

    A vendor-executed backend cannot be cost-guarded by dex: there is no dry run
    to estimate from and no statement to cap, so it reports the ``hosted``
    paradigm with neither an estimate nor a ceiling and says so on every result,
    naming the vendor that does govern the spend (``cost_guard_warning`` on the
    backend). A dex-executed backend takes the ordinary handshake and gets its
    cost from the adapter, so it gets nothing from here.

    This lives beside the protocol rather than inside one backend because the
    posture belongs to the ``execution`` axis: a third backend that declares
    ``vendor`` inherits it, instead of a reviewer having to notice that the new
    one forgot to warn.
    """

    if getattr(backend, "execution", None) != EXECUTION_VENDOR:
        return None, []
    from ... import envelope as env

    warning = getattr(backend, "cost_guard_warning", None)
    return env.Cost(paradigm=env.Paradigm.HOSTED), [warning] if warning else []


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


def merge_pii_meta(store: dict[str, Any], name: str | None, value: Any) -> None:
    """Fold one copy of a dimension's ``config.meta`` into a PII-gate accumulator.

    The counterpart to :func:`merge_element_fields` for the gate's authoritative
    map: the same dimension is reachable from several metrics, and the copies are
    read independently. Two rules, both in the safe direction. **PII wins**, so a
    copy that flags the dimension can never be overwritten by one that does not;
    and a copy that says nothing (a ``null`` config, which is what a synthesized
    dimension returns) never displaces one that speaks.
    """

    if name is None:
        return
    current = store.get(name)
    if _meta_says_pii(current):
        return
    if _meta_says_pii(value) or current is None:
        store[name] = value


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


def unadjudicated_refs(
    refs: list[str],
    *,
    meta_lookup: Callable[[str], Any] | None = None,
) -> list[str]:
    """The refs no authoritative source spoke to, in the order they were requested.

    The counterpart to :func:`screen_dimension_refs`, over the same lookup: those
    refs passed the gate on the name heuristic alone, which is the fail-closed
    floor and not equivalent to evidence. Run it after the gate has cleared a
    query, so what it returns is exactly the set the result should disclose. A
    caller with no lookup at all had no evidence for anything.
    """

    if meta_lookup is None:
        return list(refs)
    unknown: list[str] = []
    for ref in refs:
        meta = meta_lookup(ref)
        if not _meta_says_pii(meta) and not _meta_clears(meta):
            unknown.append(ref)
    return unknown


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


# Which deployment of a vendor each execution axis selects. `--local` and `--api`
# name that axis, not a vendor: a repo has one semantic layer, and a per-vendor
# flag would present two different layers as one command's two modes, which is an
# interchange promise dex does not make.
_EXECUTION_DEPLOYMENTS: dict[str, dict[str, str]] = {
    "dbt": {EXECUTION_DEX: "local", EXECUTION_VENDOR: "dbt_cloud"}
}


def resolve_backend(
    engine, *, api: bool = False, local: bool = False
) -> SemanticBackend:
    """The ambient backend resolution: ``api``/``local`` pick the execution axis
    for one command, overriding the ``.dex/config.yml`` ``semantic.deployment``
    default (or the released ``semantic.backend`` spelling of it). Raises
    :class:`SemanticBackendError` (never a bare import error, and never a bare
    ``ValueError`` from a missing project) when the chosen backend's extra,
    config, or credentials are missing."""

    from ...config import (
        SEMANTIC_DEPLOYMENTS,
        canonical_semantic_deployment,
    )

    if api and local:
        raise SemanticBackendError("choose one of --local or --api, not both")

    semantic = getattr(engine.config, "semantic", None)
    vendor = (getattr(semantic, "vendor", None) or "dbt").strip().lower()
    if vendor not in SEMANTIC_DEPLOYMENTS:
        raise SemanticBackendError(
            f"unknown semantic vendor '{vendor}'; dex ships "
            f"{', '.join(sorted(SEMANTIC_DEPLOYMENTS))}"
        )

    if api or local:
        execution = EXECUTION_VENDOR if api else EXECUTION_DEX
        deployment = _EXECUTION_DEPLOYMENTS[vendor][execution]
    else:
        # `backend` is read as the fallback, not the primary, so a duck-typed
        # config that predates the split still resolves.
        configured = getattr(semantic, "deployment", None) or getattr(
            semantic, "backend", None
        )
        deployment = canonical_semantic_deployment(configured or "local")

    source = getattr(engine, "semantic_source", None)
    if deployment == "dbt_cloud":
        from .hosted import HostedDbtCloudBackend

        return HostedDbtCloudBackend.from_config(engine.config, source)
    if deployment == "local":
        if source is not None:
            # Honored or named in an error, never accepted and dropped. A host that
            # believes it supplied this request's principal, and in fact reached
            # the warehouse under whatever the process could discover, has lost the
            # access control it came here for with nothing in the output saying so.
            raise SemanticBackendError(
                "a semantic source supplies a hosted dbt Cloud token and has no "
                "meaning for the local backend, which renders metric SQL and runs "
                "it through this engine's own connector. Select the hosted backend "
                "(semantic.deployment: dbt_cloud, or --api), or drop the source "
                "and let the connector's credential govern"
            )
        from .local import LocalMetricFlowBackend

        return LocalMetricFlowBackend.from_engine(engine)
    raise SemanticBackendError(
        f"vendor '{vendor}' has no deployment '{deployment}'; use one of "
        f"{', '.join(SEMANTIC_DEPLOYMENTS[vendor])} (or pass --local / --api)"
    )
