"""The project adapter seam: how dex stays open to more model formats over time.

The source of truth is a *project*, and today the only project format is dbt. This
is the extension point: `DbtProject` is the one implementation now, and future
source formats (SQLMesh, Cube, an orchestrated asset graph) become new
implementations of the same protocol without touching the engine that reasons over
a project.

**Why tiers rather than one protocol.** A project is read through channels with
different consumers and different requirements, and a single-method seam cannot
carry that whichever method it names: pin it to the compiled view and the declared
keys never arrive, because those reach dex only through ``definitions()``; pin it
to the declarations and the per-layer snapshot has nowhere to come from. So the
seam is tiered, the way `storage.base` is::

    ExploreProject     definitions()                  -- what the project declares
    MaintainProject      + transform_layer()          -- what it looks like right now
                         + semantic_layer()
    EditableProject      + write_edits()              -- what may be written back

Each tier is a superset of the one above, and a format implements the tiers it can
serve. The two read tiers are named for the channels that consume them, exactly as
``ExploreStore`` and ``MaintainStore`` are. The write tier is named for the
capability rather than left as an unqualified ``Project``, because unlike ``Store``
that noun is already taken in this ecosystem: dbt ships two classes called
``Project``, and a reader holding both open should not have to disambiguate.

**Why tiers rather than a capability flag.** A flag is a claim the engine has to
interpret, trust, and branch on, and nothing stops a format from setting it wrong.
A tier is checkable: ``isinstance(project, EditableProject)`` is either true or it
is not, and a format that cannot receive edits cannot accidentally claim it can.
The declaration and the enforcement become the same object.

That matters most for the write path, and ``maintain reconcile`` is the caller.
Its two mechanical write paths gate on the ``models/staging/stg_<table>.*``
scaffold convention and fail closed to advisory, so a generated or derived project
tree was safe from them by naming coincidence rather than by contract, and a format
whose layer directories happened to use that vocabulary would not have been.
Reconcile now asks for :class:`EditableProject` before it proposes an edit at all,
which makes the guarantee structural; the convention checks stay as a second line,
because the declaration replaces the coincidence rather than the check that made
the coincidence survivable.

**Constructing one is a separate contract**, :class:`ProjectFactory` over a
:class:`ProjectContext`, and it is optional. A host that passes its own instance to
the engine never needs it; it exists so a format can also be *named* in
configuration and built by dex, which is the only door open to a host that reaches
dex as a subprocess. Keeping it off the tiers is what preserves the property that
makes this seam cheap: a class with the right methods is a project, with no base
class to inherit and no registration step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .. import dbt_project
from ..errors import ConfigurationError, RepoRootRequiredError
from ..maintain import snapshot

if TYPE_CHECKING:
    from ..dbt_project import DbtProjectView, ProjectDefinitions
    from ..maintain.snapshot import SemanticLayer, TransformLayer

__all__ = [
    "DbtProject",
    "EditableProject",
    "ExploreProject",
    "MaintainProject",
    "ProjectAdapter",
    "ProjectContext",
    "ProjectFactory",
    "tier_of",
]


@runtime_checkable
class ExploreProject(Protocol):
    """Tier 1: a project that can state what it declares.

    The narrowest useful tier, and the one every format must reach to be worth
    reading at all: declared keys, declared joins, and metric models arrive here
    and nowhere else.

    ``name`` is a stable identifier for the project *format*, not for the instance
    -- ``"dbt"``, ``"sqlmesh"``. It is what a registry would resolve.
    """

    #: Stable format name, e.g. "dbt".
    name: str

    def definitions(self) -> ProjectDefinitions:
        """What the project declares: keys, joins, relations, metric models.

        **This must not raise.** No project, an ambiguous choice, or an unreadable
        project yields the empty view (with a note where there is something
        actionable to say), never an exception -- the promise
        ``dbt_project.definitions`` already makes in its own docstring, made part
        of the contract here. Explore runs on raw warehouses where absence is the
        normal case, so a format that raises turns an ordinary condition into an
        outage.

        This is the tier's real contract, and it is behavioural rather than
        structural, which is why a conformance suite asserting only shapes will not
        catch a format that gets it wrong.
        """
        ...


@runtime_checkable
class MaintainProject(ExploreProject, Protocol):
    """Tier 2: adds the snapshot channel that makes drift detectable.

    Two methods rather than one returning a union, because these are the two
    functions `maintain.snapshot` already exposes and `maintain.commands` already
    calls: a union return would have to be unpacked at every one of those call
    sites. A format reaching only tier 1 still contributes declared keys and
    joins; it simply cannot be a drift baseline.

    Unlike tier 1 these presume a project that loads. ``maintain`` already treats
    an unreadable project as a handled state and carries a note saying so, so the
    absence is reported by the caller rather than papered over with an empty layer
    here -- an empty layer compared against an empty layer reads as "no drift"
    rather than "this could not be checked".
    """

    def transform_layer(self) -> TransformLayer: ...

    def semantic_layer(self) -> SemanticLayer: ...


@runtime_checkable
class EditableProject(MaintainProject, Protocol):
    """Tier 3: the write path.

    Implemented only by formats whose source of truth can actually receive an edit.
    The clearest case that cannot is a project reduced from a running graph, where
    the reduction is not the source of truth: the code that produced the graph is,
    and writing into the reduction would edit an artifact regenerated from
    something else on the next run.

    **That test is about the artifact an edit would land in, not about where the
    project came from**, and the two come apart more often than the graph example
    suggests. A format may reduce a graph for its model list while reading its
    declared keys, joins and semantics from hand-authored files that nothing
    regenerates. Those files are a genuine source of truth, they are exactly the
    shape ``reconcile`` proposes edits to, and a format holding one can reach this
    tier for that channel while still declining to author a model. Ask which
    artifact the edit lands in and whether anything rewrites it; do not infer the
    answer from the project being graph-derived.

    Declining this tier is the honest answer for a format with no such artifact,
    and the reason the tiers exist rather than a ``writeback`` flag.
    ``maintain reconcile`` reads it:
    a format that does not satisfy this protocol gets advisory-only proposals and
    no stored plan, which is the behavior a generated tree previously got by
    naming coincidence.
    """

    def write_edits(
        self, edits: Any, project_dir: Any, *, confirmed: bool = False
    ) -> Any:
        """Write proposed edits back into the project as reviewable diffs.

        **``project_dir`` comes from the caller, not from how this project was
        built.** The caller is applying a stored plan, and a plan records the
        directory it was pinned against relative to the repository root, which is
        what keeps it valid when the repository moves (see ``transform.plans``). A
        project built from engine configuration need not point at that directory, so
        resolving it here would write the edits into whichever project the engine
        happened to be configured for, and hash-check them against that project's
        files. The two agreeing is the common case rather than a guarantee, and the
        disagreement is silent.

        It is a slot for the formats that have one, exactly as ``repo_root`` and
        ``project_dir`` are on :class:`ProjectContext`. A format keyed by something
        other than a directory ignores it, rather than this protocol growing a
        variant per kind of coordinate.

        **``confirmed`` carries the human-edit conflict handshake, and cannot be
        left out.** Re-hash every target against the content the edit was planned
        against; a file whose hash moved is a conflict. With ``confirmed=False`` a
        conflict refuses the whole apply and writes nothing, surfacing the
        divergence as diffs for a human to read. With ``confirmed=True`` the
        conflicts are overridden because someone said so.

        That refusal is propose-don't-impose itself, which is why it is on the
        signature rather than left to an implementation to remember: a write path
        that cannot receive ``confirmed`` defaults to overwriting the edit someone
        made while the plan sat in review.
        :class:`~.conformance.EditableProjectContract` asserts the behavior,
        because no shape check can.
        """
        ...


@runtime_checkable
class ProjectAdapter(Protocol):
    """The pre-tier seam, kept so an existing importer keeps working.

    Superseded by the tiers above, which say the same thing with the write path
    separable. Nothing in this distribution references it; it is retained rather
    than removed because it has been public since v1.
    """

    #: Stable format name, e.g. "dbt".
    name: str

    def load(self) -> Any:
        """Load the project into an in-memory view."""
        ...

    def write_edits(self, edits: Any) -> Any:
        """Write proposed edits back into the project as reviewable diffs."""
        ...


@dataclass(frozen=True)
class ProjectContext:
    """Everything a format gets to build itself from when dex constructs it.

    Three fields, because the formats disagree about what keys them and the
    disagreement is not resolvable by picking a winner. A dbt project is keyed by
    a directory. A project reduced from a graph already in memory has neither a
    directory nor a repository. A hosted format has service coordinates and
    neither. A contract shaped around the directory-shaped one would leave the
    other two unbuildable, which is the group this seam exists for.

    ``repo_root`` is the directory dex was pointed at, or ``None`` when there is
    no repository in the picture.

    ``project_dir`` is where within that repository the project was pinned,
    relative to ``repo_root``, as recorded in configuration. It is ``None`` when
    nothing pinned one, which for a directory-keyed format means "discover it".
    A format not keyed by a directory ignores this exactly as a format with no
    repository ignores ``repo_root``: both are slots for the formats that have
    them, not an assumption that every format does.

    ``options`` is the format's own non-secret coordinates, passed through
    verbatim from wherever the format was named. dex does not interpret it, so
    the keys are the format's to define and the format's to validate: refuse an
    option you cannot honor rather than accepting and ignoring it, because a
    silently dropped setting is indistinguishable from a working one until dex
    is reading the wrong project.

    **Construction has to be cheap.** dex builds a project per command rather
    than holding one, because a project is an artifact a previous command may
    have just rewritten and a stale read is a wrong drift report. Open a
    connection, fetch a graph, or parse anything large lazily on first use, not
    in the factory.

    **No secret ever arrives here.** A format named in ``.dex/config.yml`` is
    named in a committed file, so a password, key, token, or connection string in
    ``options`` would be a credential in version control. Read the credential the
    way the rest of the engine does, from the environment at construction time,
    or skip this contract entirely and hand the engine a project you built
    yourself (``DexEngine(project_format=...)``), which is the right shape for a
    host that already holds per-request credentials.
    """

    repo_root: str | None = None
    project_dir: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ProjectFactory(Protocol):
    """Anything that turns a :class:`ProjectContext` into a project.

    One call, one argument, so the shapes a format author would reach for all
    qualify without adapters: a module-level function, a class whose ``__init__``
    takes the context, or a classmethod such as
    :meth:`DbtProject.from_context`.

    The returned project need only satisfy the tier its host uses; a factory
    building an :class:`ExploreProject` is a complete factory.

    **Do not validate a factory with ``isinstance``.** This protocol is
    ``runtime_checkable`` for symmetry with the tiers, but a callable protocol
    can only check that ``__call__`` exists, which every callable satisfies. What
    is worth checking is the project that comes back, and the tiers are genuinely
    ``isinstance``-checkable, so build first and check the result against the tier
    the caller needs.
    """

    def __call__(self, context: ProjectContext) -> ExploreProject: ...


def tier_of(project: object) -> int:
    """The highest tier ``project`` satisfies, or 0 for none.

    Checked structurally rather than declared, so a format cannot claim a tier it
    does not implement. Note the ordering: the tiers are nested, so the test has to
    run from the most specific down.
    """

    if isinstance(project, EditableProject):
        return 3
    if isinstance(project, MaintainProject):
        return 2
    if isinstance(project, ExploreProject):
        return 1
    return 0


class DbtProject:
    """The dbt implementation of the project seam.

    Every member delegates to `dbt_project` or `maintain.snapshot`, which is the
    point: the tiers describe what the engine already does with a project rather
    than asking dbt to be read differently.

    Holds the search surface (class DI) so callers do not thread it through every
    call. ``repo_root`` is where dex was pointed; ``project_dir`` pins one project
    within it and stays optional, because ``None`` is how ``definitions()`` is
    reached when ``dbt_project_dir`` is unset and discovery has to run.

    **One instance is meant to live for one command.** It memoizes the loaded view
    (see :meth:`load`), and a project on disk is an artifact ``transform apply``
    and ``transform build`` rewrite, so an instance held across commands would
    serve a view of the project as it was before the write. dex builds one per
    command for exactly that reason; a host constructing its own owns that
    decision.
    """

    name = "dbt"

    def __init__(
        self,
        repo_root: Path | str = ".",
        project_dir: Path | str | None = None,
    ):
        self.repo_root = Path(repo_root)
        self.project_dir = Path(project_dir) if project_dir is not None else None
        self._view: DbtProjectView | None = None

    @classmethod
    def from_context(cls, context: ProjectContext) -> DbtProject:
        """Build from configuration, refusing coordinates it cannot honor.

        ``repo_root`` is required rather than optional: a dbt project is a
        git-reviewable filesystem artifact by design, so there is no dbt project
        to read without one, and refusing here names the fix instead of failing
        several frames down on a path built from ``None``.

        ``options`` are refused rather than ignored. dbt takes none, and this
        format's one coordinate is ``project_dir``, which has its own slot;
        accepting an unknown key and dropping it is indistinguishable from
        honoring it right up until dex reads the wrong project.

        ``project_dir`` arrives relative to ``repo_root``, which is how it is
        written in configuration and the only form that survives the repository
        moving, so it is joined here rather than passed through.
        """

        if context.repo_root is None:
            raise RepoRootRequiredError(
                "the dbt project format needs a repo root: the project is a "
                "git-reviewable filesystem artifact, so build the engine with "
                "DexEngine.from_repo(repo_root) or pass repo_root="
            )
        if context.options:
            named = ", ".join(sorted(context.options))
            raise ConfigurationError(
                f"the dbt project format takes no options, and got: {named}. "
                "Pin the project directory with `dbt_project_dir` in "
                ".dex/config.yml, which is the one coordinate this format has"
            )
        root = Path(context.repo_root)
        pin = root / context.project_dir if context.project_dir else None
        return cls(root, pin)

    def definitions(self) -> ProjectDefinitions:
        return dbt_project.definitions(self.repo_root, self.project_dir)

    def load(self) -> DbtProjectView:
        """Load the project into an in-memory view, once per instance.

        Raises ``DbtProjectError`` when there is no project to load, which is what
        every caller of ``dbt_project.load`` already expects. Tier 1 is the channel
        with the never-raises promise.

        Memoized because it is the expensive call on every maintain path: it walks
        the model and macro trees, reads and hashes every file, and parses a
        ``manifest.json`` that is routinely several megabytes. The two layer
        accessors below each need it, and three of the four commands that read
        layers need both, so without the memo routing them through this seam would
        cost a second full load. The memo's lifetime is the instance's, which is
        why the class docstring says how long an instance is meant to live.

        Discovery runs when nothing pinned a directory, matching
        :meth:`definitions`. Resolving to ``repo_root`` instead would find a
        project only when it sits at the repository root, so the two tiers would
        disagree about which project they were reading in every repo that keeps
        its dbt project in a subdirectory. The result is absolute because the
        view's ``root`` reaches ``transform.plans``, which records a directory
        relative to the repo root and re-resolves it at apply time.
        """

        if self._view is None:
            project = self.project_dir or dbt_project.find_project(self.repo_root)
            self._view = dbt_project.load(Path(project).resolve())
        return self._view

    def transform_layer(self) -> TransformLayer:
        return snapshot.transform_layer(self.load())

    def semantic_layer(self) -> SemanticLayer:
        return snapshot.semantic_layer(self.load())

    def write_edits(
        self, edits: Any, project_dir: Any = None, *, confirmed: bool = False
    ) -> Any:
        """The caller's directory wins over the one this instance was built from.

        ``project_dir`` is optional here and required by the protocol, which is a
        widening rather than a disagreement: this class is also constructed directly
        by callers holding engine configuration and no plan, and they have nothing
        to pass. When they pass nothing the configured pin is used, which is the
        behavior this method always had. When a caller does pass one it wins, and
        that is the point: it is the directory the plan being applied was pinned
        against, and the plan is the authority on where its own hashes came from.
        """

        return dbt_project.write_edits(
            edits,
            project_dir or self.project_dir or self.repo_root,
            confirmed=confirmed,
        )
