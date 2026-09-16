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
:mod:`.ossie.authoring` is one document's bytes and hash. Different concepts,
adjacent words; the qualifier in each name is the part that matters.

A leaf module, like :mod:`.semantic_catalog` beside it: the protocol types are
imported only for typing, so importing this costs no reader, no validator, and no
optional dependency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .errors import ConfigurationError

if TYPE_CHECKING:
    from .maintain.snapshot import SemanticLayerSnapshot
    from .semantic_catalog import SemanticCatalogView

__all__ = [
    "SemanticCatalogSource",
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
    except ConfigurationError:
        # The source refused its own coordinates and said why. Re-wrapping would
        # bury the specific message under a generic one.
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
