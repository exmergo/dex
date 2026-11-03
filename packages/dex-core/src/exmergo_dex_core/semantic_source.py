"""What supplies a semantic layer, and how dex constructs one.

A semantic layer and a transformation project are two axes, and this module is
the seam for the first. A repository may keep dbt for its models and author its
semantics as native Apache Ossie documents beside them, or have Ossie and no dbt
at all. Neither arrangement makes the semantic source a project: it owns no model
graph, no compilation, no packages, no targets, and no dbt write surface, so
holding it to the project tiers would be claiming capabilities it does not have.

**Two capabilities rather than a tier ladder.** A source that can answer a read
catalog implements :class:`SemanticCatalogSource`; one that can also produce a
drift fingerprint implements :class:`SemanticSnapshotSource`. They are separate
because declining the second is an answer rather than a shortfall, the same
reason :class:`~.adapters.project.SemanticCatalogProject` sits beside the project
tiers rather than inside them. Both are ``runtime_checkable``, so a caller asks
what a source can do instead of asking what it is.

**Name disambiguation, because three nearby names read alike.**
``SemanticSource`` in :mod:`.connect` is a *credential*: the hosted dbt Cloud
Semantic Layer's service coordinates, reachable as ``engine.semantic_source``.
:class:`SemanticSourceContext` here is what a *reader* is built from, reachable
as ``engine.semantic_catalog_source()``. ``SemanticSourceFile`` in
:mod:`.edits` is one semantic document's bytes and hash. Different concepts,
adjacent words; the qualifier in each name is the part that matters.

A leaf module, like :mod:`.semantic_catalog` beside it: the protocol types are
imported only for typing, so importing this costs no reader, no validator, and no
optional dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from .errors import ConfigurationError, DexError

if TYPE_CHECKING:
    from .cache import Dataset
    from .maintain.drift import DriftFinding
    from .maintain.reconcile import Proposal
    from .maintain.snapshot import SemanticLayerSnapshot
    from .semantic_catalog import SemanticCatalogView
    from .transform.plans import PlanEdit

__all__ = [
    "DeclaredReference",
    "DerivedDefinition",
    "SemanticCatalogSource",
    "SemanticDeclaration",
    "SemanticDerivationRequest",
    "SemanticDerivationResult",
    "SemanticDerivationSource",
    "SemanticSchemaSource",
    "SemanticSnapshotSource",
    "SemanticSourceContext",
    "build_semantic_source",
    "construct_semantic_source",
    "resolve_semantic_source_factory",
]


@dataclass(frozen=True)
class SemanticSourceContext:
    """Everything a semantic source gets to build itself from.

    Nullable slots for the same reason :class:`~.adapters.project.ProjectContext`
    has them: the sources disagree about what keys them. Native documents are
    keyed by a repository and a file list; a hosted layer has service coordinates
    and no repository at all.

    **There is no ``project_dir``, and its absence is the point.** A semantic
    source is not pinned inside a transformation project, so a slot for one would
    invite a reader to go looking for models it has no business owning.

    ``repo_root`` is the directory dex was pointed at, or ``None`` when there is
    no repository in the picture. ``connector`` is the name of the warehouse dex
    resolved, never a live adapter and never a credential: a source may have to
    read an authored relation name the way the active warehouse would, and
    identifier arity, quoting, and unquoted-case folding are the connector's
    rules. ``options`` is the source's own non-secret coordinates, passed through
    verbatim from the configuration section named after the vendor, and it is the
    source's job to refuse an option it cannot honor rather than accept and
    ignore it.

    **Construction has to be cheap and must read nothing.** dex builds a source
    per command rather than holding one, because a document is an artifact a
    previous command may have just rewritten and a stale read is a wrong drift
    report. Parse lazily on first use.

    **No secret ever arrives here**, for the reason it never arrives in a project
    context: these coordinates come from a committed file.
    """

    repo_root: str | None = None
    connector: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SemanticCatalogSource(Protocol):
    """A source that can answer the semantic layer as a read catalog.

    The narrowest useful capability, and the floor for anything a vendor names:
    a source that cannot say what the layer contains cannot serve
    `explore semantic list`, `explore map --use-project`, or anything downstream
    of them.
    """

    def semantic_catalog(self) -> SemanticCatalogView:
        """The semantic layer as a read catalog.

        May raise :class:`~.errors.ProjectError`, and should where the layer
        cannot be read *yet* as opposed to being empty. An unreadable document
        and a layer that declares nothing are different answers, and only one of
        them is fixed by editing a file.
        """
        ...


@runtime_checkable
class SemanticSnapshotSource(Protocol):
    """A source that can also produce the drift fingerprint `maintain` diffs.

    Beside :class:`SemanticCatalogSource` rather than extending it in the tier
    sense: the catalog is a *read view* carrying types, labels, descriptions and
    the token a query groups by, while the snapshot is a *fingerprint*, a content
    hash per definition plus the physical column behind each field, sized for
    detecting change and stable across tool upgrades. Widening either to serve
    the other costs the property it exists for, so a source performs both
    reductions or declines the second.
    """

    def semantic_layer(self) -> SemanticLayerSnapshot:
        """The layer reduced to what a comparison needs.

        **This must not raise.** `maintain` already treats an unreadable source
        as a handled state and carries a note saying so, and every failure a
        file-backed source has (a missing file, unparseable content, an optional
        extra not installed) is a state a user reaches by an ordinary typo.
        """
        ...


@runtime_checkable
class SemanticSchemaSource(Protocol):
    """A source that can hold its own declarations against a warehouse schema.

    A third capability beside the other two, and as declinable. A layer that
    sits straight on warehouse relations declares columns and their types, and
    whether the warehouse still has them is a schema-axis question the
    layer-neutral detectors cannot answer, because how a declared column maps
    onto a physical one, and which types count as the same, are the vendor's
    own rules. A source implementing this answers with those rules, from
    metadata dex captured through its own connection: it is handed the
    datasets, never a connection, so it cannot open one of its own.

    It also turns what it found back into edits, because only the vendor knows
    how to remove a column from its own document and what else that removal
    takes with it.
    """

    def declared_schema_drift(
        self, current: list[Dataset], baseline: list[Dataset]
    ) -> tuple[list[DriftFinding], list[str]]:
        """Schema-axis findings for the declarations, and what was not compared.

        ``current`` is the warehouse now, ``baseline`` the warehouse the drift
        baseline pinned, so a relation that has gone can be told from one this
        connection never listed. **This must not raise**, for the reason
        ``semantic_layer()`` may not.
        """
        ...

    def schema_reconcile(
        self, findings: list[DriftFinding]
    ) -> tuple[list[Proposal], list[PlanEdit], list[str]]:
        """Proposals for this source's own findings, and the edits behind them.

        Takes every finding of the report and answers for the ones it produced,
        so the caller does not need to know which codes those are. The edits
        are unvalidated and unstored: the caller plans them through the source's
        own authoring route, which is what validates them.
        """
        ...

    def handles_finding(self, finding: DriftFinding) -> bool:
        """Whether ``finding`` is one :meth:`schema_reconcile` answers for."""
        ...


# --- the derivation capability (#927) ------------------------------------------------

#: ``derive`` writes a new definition for a declared model the layer does not
#: cover yet (a dbt mart under ``transform.marts_paths``); ``annotate`` renders a
#: declaration into a definition the layer, or the payload beside it, already
#: holds (``semantic <vendor> plan`` with ``declarations``).
DerivationMode = Literal["derive", "annotate"]

#: Per declared model. ``unsupported`` is a mode the vendor does not do, said
#: per model rather than raised, so a caller always gets one row per model.
DerivationStatus = Literal[
    "included", "proposed", "failed", "unvalidated", "skipped", "unsupported"
]


@dataclass(frozen=True)
class DeclaredReference:
    """A ``foreign_key`` column's ``references``, resolved as far as the repo can.

    ``parent_identifier`` is the parent's table name as the warehouse spells it
    (a dbt alias, or the last part of its compiled relation), which is how a layer
    that names tables rather than dbt models finds the definition sitting on the
    parent. ``parent_relation`` is the compiled relation where a manifest says
    it, and ``None`` otherwise.
    """

    column: str
    parent_model: str
    parent_column: str | None = None
    parent_identifier: str | None = None
    parent_relation: str | None = None


@dataclass(frozen=True)
class SemanticDeclaration:
    """One declared model, in the declaration's own vendor-neutral shape.

    ``declared`` is exactly what ``transform.drafting.read_declared`` reads back
    from a model's YAML (``description``, ``grain``, ``population``,
    ``assumptions`` and per-column ``role``, ``description``, ``references``,
    ``null_rule``, ``aggregation``, ``additivity``, ``unit``), and the native
    route hands over the same shape. ``column_types`` is ``(kind, granularity)``
    per column with ``kind`` one of ``date``, ``timestamp``, ``other`` or
    ``None``; ``data_types`` is the warehouse type where the SQL casts it or the
    cache profiled it. Both come from what is free to read: never a connection.
    """

    model: str
    declared: Mapping[str, Any]
    column_types: Mapping[str, tuple[str | None, str | None]] = field(
        default_factory=dict
    )
    data_types: Mapping[str, str] = field(default_factory=dict)
    identifier: str | None = None
    relation: str | None = None
    #: Where ``relation`` came from, said in the derivation's own notes.
    relation_source: str | None = None
    references: tuple[DeclaredReference, ...] = ()
    #: The model's SQL file, relative to the dbt project, for placement.
    source_path: str | None = None


@dataclass(frozen=True)
class SemanticDerivationRequest:
    """Everything a derivation reads, so a derivation reads nothing else.

    A batch rather than one declaration: a layer that keeps several definitions
    in one document splices them all into it once, and a layer validated by a
    subprocess (dbt's parse) runs it once for the plan rather than once per
    model. ``documents`` is the layer's surface as the plan would leave it (the
    files on disk with the payload's own edits laid over them), keyed as the
    vendor's write surface keys them. ``cache`` is the exploration cache, read
    only; ``connector`` is a dialect name, never an adapter.
    """

    mode: DerivationMode
    declarations: tuple[SemanticDeclaration, ...]
    documents: Mapping[str, str] = field(default_factory=dict)
    #: Each dbt model to the table name it builds, where known.
    project_identifiers: Mapping[str, str] = field(default_factory=dict)
    cache: Any = None
    connector: str | None = None


@dataclass
class DerivedDefinition:
    """What a derivation did with one declared model. One per model, always.

    ``carried`` names each declaration field that reached the vendor's document
    (``grain``, ``population``, ``columns.amount.unit``); ``not_carried`` names
    each one that did not, with why, so a declared fact a vendor has no place for
    is said rather than dropped.
    """

    model: str
    status: DerivationStatus = "proposed"
    #: The vendor's own name for the definition.
    name: str | None = None
    path: str | None = None
    content: str | None = None
    reason: str | None = None
    carried: list[str] = field(default_factory=list)
    not_carried: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    validation: dict[str, dict[str, Any]] = field(default_factory=dict)
    next: str | None = None


@dataclass
class SemanticDerivationResult:
    """A derivation's answer for a whole request.

    ``edit_target`` says which plan ``edits`` belong to: ``project`` edits are
    the plan's own edits with the derived ones spliced in (a layer that lives in
    the transformation project, as MetricFlow's does), ``semantic`` edits are
    only the included definitions' documents on the layer's own surface, which
    the caller stores as a plan of their own through the layer's authoring
    route. ``proposal`` is the envelope body for ``data.semantic_proposal``.
    ``gate`` is a validation of exactly ``edits`` the caller may stand in for its
    own (dbt's parse), or ``None``.
    """

    vendor: str
    edit_target: Literal["project", "semantic"]
    edits: list[PlanEdit] = field(default_factory=list)
    definitions: dict[str, DerivedDefinition] = field(default_factory=dict)
    proposal: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    gate: dict[str, Any] | None = None


@runtime_checkable
class SemanticDerivationSource(Protocol):
    """A semantic layer that can carry model declarations (#491) into itself.

    A fourth capability beside the other three, and as declinable: a layer that
    does not implement it is named in the plan's warnings and nothing is derived.
    It is the one seam every vendor's derivation sits behind, so `transform plan`,
    `transform declare` and `semantic <vendor> plan` never branch on a vendor.

    The contract every implementation keeps (and
    :class:`~.semantic_source_conformance.SemanticDerivationContract` checks):

    - **Pure over the request.** It reads nothing beyond the request and its own
      configured documents, and opens no warehouse connection and no vendor
      server.
    - **One** :class:`DerivedDefinition` **per declared model**, whatever
      happened to it.
    - **Never overwrite, never rename.** A definition the layer already holds
      for a model is ``skipped`` in ``derive`` mode; a name another definition
      holds is ``failed``.
    - **A population is metadata, never a filter.** The derived definition sits
      on the model itself, whose SQL already enforces its population, so a filter
      would apply the rule twice and diverge silently the day the two disagree.
      It is carried in the vendor's metadata and description, or named in
      ``not_carried``.
    - **A mode it does not do is** ``unsupported`` **per model**, not an
      exception.

    Every vendor fits the same request: MetricFlow splices semantic models into
    the project's YAML and validates with dbt's parse; SLayer writes one model
    file per declared model; Ossie (#500) splices a dataset into a configured
    multi-dataset document and carries metadata in ``ai_context``; Cube (#911)
    writes one cube file per declared model and joins through
    ``DeclaredReference.parent_identifier``.
    """

    def derive_semantic(
        self, request: SemanticDerivationRequest
    ) -> SemanticDerivationResult:
        """Render the request's declarations into this layer's documents."""
        ...


def resolve_semantic_source_factory(path: str) -> Any:
    """The factory ``path`` names, imported but not called.

    Resolution only, so a caller that wants to inspect or wrap what a name
    selects can do so without constructing anything.
    :func:`build_semantic_source` is the one that constructs.
    """

    from importlib import import_module

    selected = (path or "").strip()
    module_name, _, attribute = selected.partition(":")
    if not module_name or not attribute:
        raise ConfigurationError(
            f"'{path}' is not a usable dotted path for a semantic source; write "
            "it as 'module.path:name', with a colon between the module and the "
            "factory"
        )
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"the semantic source '{path}' names a module that will not import: "
            f"{exc}. Check the spelling, and that the distribution providing it "
            "is installed in the environment running dex"
        ) from exc

    resolved: Any = module
    for part in attribute.split("."):
        try:
            resolved = getattr(resolved, part)
        except AttributeError as exc:
            raise ConfigurationError(
                f"the semantic source '{path}' imported {module_name} but it has "
                f"no '{attribute}'"
            ) from exc

    if not callable(resolved):
        raise ConfigurationError(
            f"the semantic source '{path}' resolved to a "
            f"{type(resolved).__name__}, which cannot be called. Name a factory: "
            "a function taking a SemanticSourceContext, a class whose __init__ "
            "takes one, or a classmethod like OssieSemanticLayer.from_context"
        )
    return resolved


def construct_semantic_source(
    name: str, factory: Any, context: SemanticSourceContext
) -> SemanticCatalogSource:
    """Call an already-resolved factory, checking what comes back reads a catalog.

    The check is on the constructed object rather than on the factory, for the
    reason the project seam checks there too: a callable protocol can only verify
    that ``__call__`` exists, which every callable satisfies, so building first is
    what makes the check mean anything.

    It checks :class:`SemanticCatalogSource` and nothing wider. A source that
    declines the snapshot capability is complete rather than partial, and
    `maintain` reports the absence itself.
    """

    try:
        source = factory(context)
    except DexError:
        # The source refused in dex's own terms and said why: its coordinates
        # (a ConfigurationError), or an extra it needs that is not installed (a
        # prerequisite the caller fixes by installing it). Re-wrapping would bury
        # the specific message under a generic one, and turn the second into a
        # configuration problem nobody can fix by editing configuration.
        raise
    except Exception as exc:
        raise ConfigurationError(
            f"the semantic source '{name}' failed to build: {exc}. A factory "
            "takes one SemanticSourceContext and returns a source; check that it "
            "accepts the context dex passes and reads its coordinates out of "
            "context.options"
        ) from exc

    if not isinstance(source, SemanticCatalogSource):
        raise ConfigurationError(
            f"the semantic source '{name}' built a {type(source).__name__}, which "
            "cannot answer a semantic catalog: it is missing semantic_catalog(). "
            "Every semantic source has to satisfy at least "
            "exmergo_dex_core.semantic_source.SemanticCatalogSource; run the "
            "shipped conformance suite against it "
            "(exmergo_dex_core.semantic_source_conformance)"
        )
    return source


def build_semantic_source(
    name: str, context: SemanticSourceContext
) -> SemanticCatalogSource:
    """Resolve ``name`` and construct it, checking what comes back is a source."""

    return construct_semantic_source(
        name, resolve_semantic_source_factory(name), context
    )
