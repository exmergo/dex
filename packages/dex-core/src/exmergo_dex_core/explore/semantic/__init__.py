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
from dataclasses import asdict, dataclass, field, replace
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

    ``group_by`` tokens are entity-qualified dimension names
    (``user__pricing_tier``) plus whatever token the layer uses for a metric's own
    time axis. ``grain`` applies to that time token when the caller wants a bucket
    without spelling it into the token, and it is validated against the grains the
    layer reports rather than against a list dex keeps.

    ``where`` clauses are passed through in **the answering layer's own filter
    dialect**, verbatim. That dialect is the backend's business, not this seam's:
    dbt's is a Jinja call (``{{ Dimension('session__is_deleted') }} = false``) and
    another format's is not, so the backend is what reads a clause when the PII
    gate needs to know which dimensions one touches.

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
    "MAX_DIMENSIONS",
    "MAX_DIMENSIONS_PER_METRIC",
    "MAX_ENTITIES",
    "MAX_MEASURES",
    "MAX_METRICS",
    "MAX_SEMANTIC_MODELS",
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
    "ValuesRequest",
    "backend_axes",
    "cap_columnar",
    "cost_posture",
    "derive_entity_type",
    "merge_element_fields",
    "qualified_dimension",
    "queryable_grains",
    "requested_dimension_refs",
    "resolve_backend",
    "resolve_values_request",
    "screen_dimension_refs",
    "screen_values_request",
    "validate_grain",
    "values_gap",
    "values_reach_note",
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


# How much of a semantic layer comes back in one envelope.
#
# Every other explore command budgets its payload and this one did not, so a
# layer an order of magnitude larger than the one these were calibrated against
# returned an order of magnitude more bytes with no flag to narrow it and nothing
# saying anything had been left out.
#
# The defaults are set so a layer of the size these were measured on comes back
# **whole**: a dozen semantic models, a few dozen metrics, a hundred-odd groupable
# dimension paths. A cap that trims an ordinary layer would be worse than no cap,
# because a consumer that silently loses catalog entries reads the remainder as
# the layer. So these bite only where one payload was already too large to read,
# `--metric`, `--for-dimension` and `--search` are the ways to ask a narrower
# question, and `--full` lifts them for a caller that genuinely wants everything.
MAX_SEMANTIC_MODELS = 50
MAX_METRICS = 60
MAX_DIMENSIONS = 150
MAX_ENTITIES = 50
MAX_MEASURES = 60
# The one cap on a repeating block rather than on a list: a join-resolved
# dimension list is carried once per metric, so on a wide layer this is where the
# bytes actually are.
MAX_DIMENSIONS_PER_METRIC = 40

# The `elided` block's keys, in payload order: the five element lists, then the
# repeating per-metric block. Named once so the zeros a complete catalog reports
# and the counts a capped one reports cannot drift apart.
_ELIDED_KINDS = (
    "semantic_models",
    "metrics",
    "dimensions",
    "entities",
    "measures",
    "dimensions_per_metric",
)


def _elision_notes(
    elided: dict[str, int], limits: dict[str, int], per_metric: int
) -> list[str]:
    """One note per non-empty cut, naming what was dropped and the way past it.

    A count in a payload field says how much is missing; it does not say how to
    get it, and the caller reading a capped catalog is usually one command away
    from the narrower question that would have fit. So each note names the cap it
    hit and the flag that answers it, and a caller that genuinely wants the whole
    layer is pointed at ``--full`` rather than left to re-run and compare.
    """

    ways_out = (
        "narrow the question with --metric, --for-dimension or --search, or pass "
        "--full for the whole layer"
    )
    notes: list[str] = []
    if elided["semantic_models"]:
        notes.append(
            f"{elided['semantic_models']} semantic model(s) are not listed: the "
            f"catalog is capped at {limits['semantic_models']}. Every element still "
            f"names its own semantic_model, so a metric can point at a model this "
            f"payload does not describe; {ways_out}"
        )
    if elided["metrics"]:
        notes.append(
            f"{elided['metrics']} metric(s) are not listed: the catalog is capped "
            f"at {limits['metrics']} metrics. This is not the layer's whole metric "
            f"set; "
            f"{ways_out}"
        )
    if elided["dimensions"]:
        notes.append(
            f"{elided['dimensions']} dimension row(s) are not listed: the catalog "
            f"is capped at {limits['dimensions']}. A token named in a metric's "
            f"dimensions may therefore have no row of its own here; {ways_out}"
        )
    if elided["entities"]:
        notes.append(
            f"{elided['entities']} entity(ies) are not listed: the catalog is "
            f"capped at {limits['entities']}. The declared join graph is incomplete in "
            f"this payload; {ways_out}"
        )
    if elided["measures"]:
        notes.append(
            f"{elided['measures']} measure(s) are not listed: the catalog is "
            f"capped at {limits['measures']}. A metric's input_measures may name a "
            f"measure this payload does not describe; {ways_out}"
        )
    if elided["dimensions_per_metric"]:
        notes.append(
            f"{elided['dimensions_per_metric']} groupable token(s) are not listed "
            f"across the metrics here: each metric's dimension list is capped at "
            f"{per_metric}, and elided_dimension_count on a metric "
            f"says how many of its own are missing. A token absent from a capped "
            f"list is not a token the metric cannot be grouped by; {ways_out}"
        )
    return notes


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
    # Which dimensions that scope was derived from, when the caller asked the
    # reverse question ("what can I slice by pricing tier") rather than naming
    # metrics. Beside `scoped_to` rather than folded into it, because the two are
    # different statements: one is what was asked, the other is what answered.
    for_dimensions: list[str] = field(default_factory=list)
    # Which search terms narrowed it, empty when none did. Same reason as
    # `scoped_to`: a catalog narrowed by a word a caller half-remembered is a
    # subset, and a subset that cannot be told from the layer is the failure mode
    # every field here exists to prevent.
    searched_for: list[str] = field(default_factory=list)
    # What the payload cap cut, per element kind, and always present including its
    # zeros. That is the point of it: an empty `notes` and a zeroed `elided`
    # together are the positive statement "this is the whole layer", which a caller
    # cannot get from the absence of a key. Filled by :meth:`capped`, which is the
    # only thing that ever cuts.
    elided: dict[str, int] = field(default_factory=dict)

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
        if self.for_dimensions:
            payload["for_dimensions"] = self.for_dimensions
        if self.searched_for:
            payload["searched_for"] = self.searched_for
        if self.scoped_to:
            payload["scoped_to"] = self.scoped_to
        if self.unavailable:
            payload["unavailable"] = self.unavailable
        payload["elided"] = self.elided or dict.fromkeys(_ELIDED_KINDS, 0)
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

    def capped(
        self,
        *,
        full: bool = False,
        max_semantic_models: int = MAX_SEMANTIC_MODELS,
        max_metrics: int = MAX_METRICS,
        max_dimensions: int = MAX_DIMENSIONS,
        max_entities: int = MAX_ENTITIES,
        max_measures: int = MAX_MEASURES,
        max_dimensions_per_metric: int = MAX_DIMENSIONS_PER_METRIC,
    ) -> SemanticCatalog:
        """This catalog trimmed to what one envelope should carry, with every cut
        counted in ``elided`` and named in ``notes``.

        Applied at the command layer rather than inside a backend, so a library
        caller reading ``list_definitions()`` gets the layer and only the surface
        that has to fit an agent's context pays the cap. ``full`` lifts every cap
        and still fills ``elided`` with its zeros, because "nothing was cut" is a
        statement the payload should make in the same shape either way. The caps
        themselves are arguments, the way ``summarize_map`` takes the map's, so a
        host embedding the engine can budget its own context and the conformance
        contract can assert a cut against a layer smaller than the shipped
        defaults.

        Elements are kept in the order the backend produced them, which both
        shipped backends sort by name. Two reads of one layer therefore cut the
        same set, and a caller can page past the cut with ``--search`` or
        ``--metric`` rather than re-running and hoping for a different half. Rank
        would be the better rule and there is nothing here to rank by: a semantic
        layer declares no importance order, and inventing one (metric count,
        description length) would bury a rarely-referenced metric that happens to
        be the one asked about.
        """

        limits = {
            "semantic_models": max_semantic_models,
            "metrics": max_metrics,
            "dimensions": max_dimensions,
            "entities": max_entities,
            "measures": max_measures,
        }
        elided = dict.fromkeys(_ELIDED_KINDS, 0)
        kept: dict[str, list[Any]] = {}
        for kind, limit in limits.items():
            elements = getattr(self, kind)
            if full or len(elements) <= limit:
                kept[kind] = list(elements)
                continue
            kept[kind] = list(elements[:limit])
            elided[kind] = len(elements) - limit

        metrics = []
        for metric in kept["metrics"]:
            dropped = (
                0
                if full
                else max(0, len(metric.dimensions) - max_dimensions_per_metric)
            )
            if not dropped:
                metrics.append(metric)
                continue
            elided["dimensions_per_metric"] += dropped
            metrics.append(
                replace(
                    metric,
                    dimensions=metric.dimensions[:max_dimensions_per_metric],
                    elided_dimension_count=dropped,
                )
            )
        kept["metrics"] = metrics

        notes = list(self.notes)
        notes.extend(_elision_notes(elided, limits, max_dimensions_per_metric))
        return replace(self, **kept, elided=elided, notes=notes)


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

    ``values`` answers the other half of "what can I filter to": a dimension's
    value domain, which is the one precondition for writing a filter that no other
    dex surface can reach on a hosted layer. It takes a metric list because a
    dimension reached through a join is only answerable in the context of a metric
    that reaches it, and both layers refuse it otherwise.

    ``filter_refs`` is where the query dialect stays the backend's own. A filter
    clause is written in the answering layer's language, and the PII gate has to
    know which dimensions one names before it can screen them, so a backend reads
    its own clauses and the neutral layer keeps the screening policy. A backend
    that cannot read its dialect answers None and its filtered queries are refused
    rather than passed with half their references unexamined.

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

    def values(self, dimension: str, metrics: list[str]): ...

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        """The dimension and entity tokens these filter clauses name, or None when
        this backend cannot read its own filter dialect."""
        ...


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


def values_gap(backend: Any) -> str:
    """Why this backend cannot answer a dimension's value domain, named rather
    than implied.

    The counterpart to :func:`~...adapters.project.semantic_catalog_gap`, for the
    other seam a third backend may reach only partly. ``resolve_backend`` has
    already returned this object by the time a caller asks, so without it the
    alternative is an ``AttributeError`` raised inside a command the resolution let
    through, which names neither the missing member nor a way forward.
    """

    named = getattr(backend, "name", type(backend).__name__)
    return (
        f"the '{named}' semantic backend does not read a dimension's value "
        "domain; implement `values(dimension, metrics)` on it, or ask a backend "
        "that does (`--local` / `--api`)"
    )


# ---- shared PII screening --------------------------------------------------
#
# A metric query touches dimensions two ways: the group_by tokens, and the
# Dimension()/TimeDimension()/Entity() refs inside a where filter. Both are
# screened, because grouping by an email is as much a disclosure as filtering by
# one and then projecting it.

# Meta keys, on a dimension's dbt `config.meta`, that authoritatively mark it PII.
_PII_META_KEYS = ("pii", "contains_pii", "is_pii", "pii_category")
# A time grain is an identifier in any dialect. Checked here because a grain
# reaches a query as an enum or a token rather than as a quoted value, so a grain
# that is not an identifier is the one shape that could carry query structure.
_GRAIN_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def requested_dimension_refs(
    q: SemanticQuery, *, filter_refs: Callable[[list[str]], list[str]] | None
) -> list[str]:
    """Every dimension/entity token a query would touch, de-duplicated in order.

    ``filter_refs`` is the answering backend's reader for its own filter dialect,
    and it is a required argument with no default: a filter dex cannot read is a
    filter dex cannot screen, and a signature that let a caller omit it would make
    that the quiet outcome. **None means the backend cannot read its dialect**, and
    a query that carries filter clauses is then refused rather than screened on its
    group-by half alone.

    That refusal is the point. The gate's disclosures can only report on refs the
    extraction found, so an extractor that matches nothing produces a successful
    query, no blocks, and no notes: every dimension named in a filter grouped and
    projected with nothing saying it was never looked at.
    """

    refs: list[str] = [*q.group_by]
    if q.where:
        # The reader may decline in either direction, and both mean the same thing:
        # a caller with no extractor to pass, or a backend whose extractor cannot
        # read this dialect. Checking only the first would let the second through
        # with its filters unexamined, which is the failure this argument exists to
        # make impossible.
        found = filter_refs(list(q.where)) if filter_refs is not None else None
        if found is None:
            raise SemanticQueryRefusedError(
                "refused: this semantic backend cannot read the dimensions its own "
                "filter dialect names, so a filtered query cannot be screened for "
                "PII. PII is flagged, never surfaced; move the condition into "
                "--group-by, or query a backend that reads its filters."
            )
        refs.extend(found)
    seen: set[str] = set()
    ordered: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def queryable_grains(
    metrics: list[str], reported: dict[str, list[str]]
) -> list[str] | None:
    """The grains valid for every metric in one query, or None when unknown.

    A query groups all of its metrics by one time axis, so a grain has to be
    queryable on all of them: the intersection is the honest answer, and it keeps a
    two-metric query from being told a grain is available when only one side can
    serve it. None where the layer said nothing about some metric, so that the
    layer refuses it rather than dex guessing on the layer's behalf.
    """

    if not metrics or any(metric not in reported for metric in metrics):
        return None
    return [
        grain
        for grain in reported[metrics[0]]
        if all(grain in reported[metric] for metric in metrics[1:])
    ]


def validate_grain(grain: str | None, *, available: list[str] | None) -> str | None:
    """The requested grain, lowercased, or a refusal naming what the layer offers.

    The vocabulary is the layer's, per metric, rather than a constant dex keeps.
    A fixed tuple is wrong in both directions: it refuses grains the vendor
    supports (its own enum runs from a nanosecond to a year) and it can never
    contain a granularity a project defined for itself.

    ``available`` is what the layer reported: a list to check against, ``[]`` for a
    metric that can be grouped by no grain at all, and None for a layer that was
    not reachable or does not answer the question. None leaves the identifier check
    as the only gate, which is the honest fallback: refusing on dex's authority
    what the layer never said is worse than letting the layer refuse it.
    """

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


def screen_values_request(
    dimension: str,
    *,
    meta_lookup: Callable[[str], Any] | None = None,
) -> list[str]:
    """Clear one dimension for a values request, or refuse it. Returns the
    disclosure notes for screening that ran on the name heuristic alone.

    The same policy as a metric query's gate with a harder consequence, because the
    output differs in kind. A metric query returns aggregates that a dimension
    merely slices, so a flagged dimension can be dropped from the grouping and the
    query still answers something. Here the result *is* the values, so there is no
    reduced answer to fall back to and a flagged dimension refuses the command.

    Worded once, here, rather than in each backend: the two differ in where the
    evidence comes from (a profiled column's cached flag, or the layer's own
    ``config.meta``) and not at all in what the refusal means.
    """

    blocked = screen_dimension_refs([dimension], meta_lookup=meta_lookup)
    if blocked:
        _ref, reason = blocked[0]
        raise SemanticQueryRefusedError(
            f"refused: {dimension} is PII ({reason}), and this command returns "
            "nothing but the values of one dimension, so there is no aggregate to "
            "fall back to. PII is flagged, never surfaced. Ask for a different "
            "dimension; one reviewed as not PII is cleared durably with a "
            "pii_overrides entry in .dex/config.yml, or with `meta: {pii: false}` "
            "on the dimension in the project that declares it."
        )
    if not unadjudicated_refs([dimension], meta_lookup=meta_lookup):
        return []
    return [
        f"PII screening used the name heuristic alone for {dimension}: no "
        "authoritative source spoke to it, so its values passed on the shape of "
        "its name. Profile the column behind it, or mark it in the project that "
        "declares it, to make the screening evidence-backed."
    ]


@dataclass(frozen=True)
class ValuesRequest:
    """A values request resolved against the layer that will answer it.

    ``token`` is what the caller wrote, grain suffix and all, and it is what the
    result reports back. ``name`` and ``grain`` are that token split, which is the
    form both layers take. ``metrics`` is the caller's own scope, checked, and
    ``reachable`` is every metric that can reach the dimension, which is what makes
    a joined dimension answerable at all.
    """

    token: str
    name: str
    grain: str | None
    metrics: list[str]
    reachable: list[str]
    grains: tuple[str, ...]


def resolve_values_request(
    view: SemanticCatalogView, dimension: str, metrics: list[str]
) -> ValuesRequest:
    """Resolve a values request against a catalog, or refuse it by name.

    Shared by both backends over the same neutral view, so the two resolve a token
    identically and a refusal is worded once. That matters more here than it looks:
    the resolution decides which metrics can reach a dimension, and a backend that
    computed that differently would answer a different question under the same
    command.

    The grain is split off for the lookup and carried separately for the query. No
    dimension name carries a grain, so validating the spelled token would refuse
    ``user__created_at__month``, which both layers answer; and the vocabulary comes
    from the layer rather than a constant, because a project may define a
    granularity of its own and spell it into a token the same way.
    """

    from ...metricflow_dialect import STANDARD_GRAINS, split_grain

    token = (dimension or "").strip()
    if not token:
        raise SemanticBackendError(
            "a values request needs one dimension (discover them with "
            "`explore semantic list`)"
        )
    grains = (
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
            "entity-qualified (user__pricing_tier) rather than the bare column "
            "name"
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
    """The disclosure that dex reached a dimension's values through a metric it
    picked itself.

    Both layers refuse a distinct-values query for a dimension reached through a
    join: there is no measure to join from, so the only rendering that exists is
    one scoped to a metric. That rendering answers a slightly different question,
    the values present for that metric rather than the domain of the column, and
    the difference is invisible in a one-column result. So the metric is named,
    the alternatives are named, and the flag that overrides the choice is named.

    Kept beside the screening policy rather than in a backend, because both
    backends hit the same refusal for the same reason and a note worded twice
    drifts.
    """

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
