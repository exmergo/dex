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
    from ..transform.plans import EditKind

__all__ = [
    "DbtProject",
    "EditableProject",
    "ExploreProject",
    "MaintainProject",
    "PlacingProject",
    "ProjectAdapter",
    "ProjectContext",
    "ProjectFactory",
    "ProjectView",
    "SourceFileView",
    "placement_gap",
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

        **What comes back has to say what happened.** ``transform apply`` reads
        ``written`` to decide whether the plan is now applied, and ``conflicts``
        to decide whether to show a human the divergence and ask. A return that
        answers neither leaves the caller unable to tell a refused apply from a
        successful one, and the safe reading of that ambiguity is the wrong one
        in both directions: a plan marked applied that was not, or a conflict
        that never reaches the human it was raised for. Return an object exposing
        ``written`` (the paths that changed, empty when a conflict refused the
        apply) and ``conflicts`` (what moved under the plan);
        :class:`~.dbt_project.ApplyResult` is the shipped one and returning it is
        the easy answer.
        """
        ...


class SourceFileView(Protocol):
    """One entry in a project view: the content an edit is pinned against.

    Two members, because two are what the callers read. ``sha256`` is what an
    edit is pinned to at plan time and re-checked against at apply time, so a
    view that omits it hashes every existing file as absent, which renders a
    one-line change as a whole-file create and turns the next apply into a
    conflict on a file nobody touched. ``content`` is what the diff is built
    against and what ``reconcile`` reads before it extends a declaration.

    A format is free to carry more (the shipped ``SourceFile`` also carries its
    own ``path``); nothing here reads it.
    """

    content: str
    sha256: str


class ProjectView(Protocol):
    """What :meth:`PlacingProject.load` returns: the format's keyspace, read once.

    ``files`` is keyed exactly the way :meth:`PlacingProject.edit_path` keys and
    :meth:`PlacingProject.editing_surface` prefixes. That is the whole point of
    the type: the three members describe one keyspace, and a format whose view
    is keyed differently from its placements pins every edit as a create.

    ``root`` is what the plan records as the directory its edits were pinned
    against, relative to the repository root where that subtraction is possible
    and verbatim where it is not (``transform.plans`` falls back to the string).
    A format keyed by a directory returns it; a format keyed by something else
    returns whatever identifies its root, and the plan carries that.

    **Neither this nor** :class:`SourceFileView` **is** ``runtime_checkable``,
    and nothing calls ``isinstance`` against them. An isinstance check on a data
    protocol only asks whether the attribute names exist, which is exactly the
    check :class:`~.conformance.PlacingProjectContract` makes with a message
    naming the missing member and what breaks without it. These exist to be
    read and to annotate, not to gate.
    """

    root: str
    files: Mapping[str, SourceFileView]


@runtime_checkable
class PlacingProject(Protocol):
    """Optional beside tier 3: the keyspace a proposed edit lands in.

    Three members describing one thing: :meth:`load` reads the keyspace,
    :meth:`edit_path` names a key in it, :meth:`editing_surface` says which
    region of it the format owns. They are here together because none of them
    is answerable alone. A key is a key into a view; a surface is a region of
    the same space; and a view nobody places into is a read dex has no use for
    on this path.

    Tier 3 says a format *can* receive an edit. It does not say where the edit
    goes, and ``maintain reconcile`` decides that before it consults the format:
    both of its mechanical write paths build ``models/staging/stg_<table>.sql``
    and ``models/staging/stg_<table>.yml`` and then look those up in the view
    ``load()`` returned. A second format satisfying tier 3 in full is therefore
    handed edits naming files it does not have, and its only options are to
    refuse them or to guess a mapping. A wrong guess writes into the wrong file
    with a hash check that passes, because the file was not there.

    Implementing this says where. It is deliberately not a request to let a
    format author the edit *content*: reconcile still decides what to write, and
    a format that cannot accept what reconcile writes says so per kind, below.

    **A protocol rather than a flag, for the reason the tiers are.** A flag is a
    claim the engine has to interpret and trust. ``isinstance(project,
    PlacingProject)`` is checkable, and a format that cannot place an edit
    cannot accidentally claim it can.

    **Separate from** :class:`EditableProject` **rather than methods on it.**
    The tiers are ``runtime_checkable``, so a method added to tier 3 silently
    demotes every format that has not implemented it yet: ``tier_of`` would
    start answering 2 where it answered 3, and the write path would close for
    exactly the implementers who were already passing. Beside it, the answer for
    a format that has not implemented this is "tier 3, placement unknown", which
    is the truth and is what the caller warns about.

    That is also why :meth:`load` is here rather than on tier 3, which is where
    a reader first looks for it. Nothing calls it outside this path: both
    callers reach it only for a format that places, so requiring it of tier 3
    would demote formats that never needed it, to state a requirement that is
    not theirs. A format holding some of these three and not the others places
    nothing at all, and :func:`placement_gap` is what names the one that is
    missing instead of leaving it to surface as ``AttributeError`` from inside
    a command the tier check already let through.
    """

    def load(self) -> ProjectView:
        """Read the project into the keyspace the other two members describe.

        Two callers, and between them they need every member of
        :class:`ProjectView`. ``transform.plans`` pins each edit to the
        ``sha256`` of the file at ``files[edit_path(...)]``, and records
        ``root`` as the directory the plan was pinned against.
        ``maintain.reconcile`` reads ``files[...].content`` to extend a
        declaration it is about to propose an edit to, and refuses to guess
        where the key is absent.

        **Cheap enough to call once per command, and fresh enough to be worth
        calling.** A project is an artifact a previous command may have just
        rewritten, so a view cached across commands is a wrong drift report.
        Memoizing within one instance is the shipped format's answer (see
        :meth:`DbtProject.load`), and it is a good one precisely because dex
        builds one project per command.

        Raising is the honest answer when there is no project to read, and the
        callers handle it: ``reconcile`` turns it into a refusal naming the
        format. Tier 1 is the channel with the never-raises promise, and this
        is not it.
        """
        ...

    def edit_path(self, kind: EditKind, model: str) -> str | None:
        """Where an edit of ``kind`` for ``model`` lives, or ``None``.

        ``model`` is the warehouse table the finding is about, not a model name
        in any format's vocabulary: the ``stg_`` prefix is dbt's own scaffold
        convention and applying it here would push that convention through the
        seam meant to escape it.

        The return is a key into whatever :meth:`EditableProject.load` returns,
        not a filesystem path. The format already owns that keyspace; reconcile
        is only guessing keys into it today.

        **``None`` per kind is the point, not a degenerate case.** The kinds are
        not equally receivable and a format is expected to answer differently
        across them. The shape this exists for is a format whose models are
        reduced from a running graph, and so cannot receive an authored
        ``MODEL_SQL``, while its declared keys and joins are hand-written files
        that nothing regenerates and can receive a ``SCHEMA_YML`` test. One
        ``None`` and one path is a complete, honest answer.
        """
        ...

    def editing_surface(self) -> list[str]:
        """The prefixes within which this format's edits may land.

        Placement says where one edit goes. This says which region of the
        format's keyspace *any* edit may touch, and it exists because the two
        questions have different callers. ``transform plan`` validates edits an
        agent authored, so there is no ``(kind, model)`` pair to ask
        :meth:`edit_path` about and no prior answer to compare against; what it
        has is a path, and what it needs is whether that path is inside the
        surface the format admits to owning.

        Containment is a safety property, not a lookup. Writes are confined to a
        declared surface so a mistaken or adversarial path cannot reach the rest
        of the repository, and the declaration has to come from the format
        because only the format knows its own layout. dbt answers with its
        configured model and macro paths, which is what the engine checked
        against directly before this seam existed.

        Prefixes are keys into the same space :meth:`edit_path` returns, matched
        by path segment: ``declarations`` admits ``declarations/orders.yml`` and
        does not admit ``declarations_backup/orders.yml``. Escapes (absolute
        paths, ``..``) are refused ahead of this and are not a format's to
        permit.

        An empty list is a format declaring no editable surface. That is a
        coherent answer, not a failure, and it refuses every edit rather than
        admitting all of them: the format is saying it has nowhere for an edit to
        go, which is the same statement declining tier 3 makes.
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


#: Each :class:`PlacingProject` member, and the one clause that says what dex
#: cannot do without it. Read by :func:`placement_gap`, which names only the
#: members a format is actually missing.
_PLACEMENT_MEMBERS = {
    "load": (
        "`load()` returns the view an edit is pinned against: `root`, and "
        "`files` keyed the way `edit_path` keys, each entry carrying `content` "
        "and `sha256`"
    ),
    "edit_path": (
        "`edit_path(kind, model)` answers where an edit of that kind for that "
        "warehouse table lives, or None to decline the kind"
    ),
    "editing_surface": (
        "`editing_surface()` declares the prefixes those paths must stay inside"
    ),
}


def placement_gap(project: object) -> str | None:
    """Which :class:`PlacingProject` member a nearly-placing format is missing.

    Placement is satisfied structurally, so a format holding two of the three
    members places nothing, and the warning its caller would otherwise reach for
    ("this format does not say where a proposed edit lands") is false of a
    format that answers ``edit_path`` and cannot be read. Worse, it sends the
    implementer to the member they already wrote.

    ``None`` in the two cases where there is nothing to add: a format that
    places has no gap, and a format declaring none of the three is declining
    placement outright, which is a complete answer the caller already describes.

    This exists because of what the alternative was. A format implementing the
    declared members and omitting the undeclared one passed the whole
    conformance suite and then raised ``AttributeError`` on the first real
    reconcile: after the tier said 3, after the gate let it through, in a
    command someone ran.
    """

    if isinstance(project, PlacingProject):
        return None
    missing = [
        member
        for member in _PLACEMENT_MEMBERS
        if getattr(project, member, None) is None
    ]
    if len(missing) == len(_PLACEMENT_MEMBERS):
        return None
    named = getattr(project, "name", type(project).__name__)
    listed = ", ".join(f"`{member}()`" for member in missing)
    details = " ".join(f"{_PLACEMENT_MEMBERS[member]}." for member in missing)
    return (
        f"the '{named}' project format implements part of PlacingProject and is "
        f"missing {listed}, so it places no edit at all and every proposal stays "
        f"advisory. {details}"
    )


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

        ``DbtProjectView`` is the shipped :class:`ProjectView`: its ``files`` are
        keyed by the project-relative paths :meth:`edit_path` returns, and its
        ``SourceFile`` entries are the shipped :class:`SourceFileView`.

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

    def edit_path(self, kind: EditKind, model: str) -> str | None:
        """The scaffold convention, which is what reconcile hard-coded before.

        Both kinds resolve, because for dbt both artifacts are the source of
        truth. A kind reconcile does not propose today returns ``None`` rather
        than a path this class would be inventing.
        """

        from ..transform.plans import EditKind as _EditKind

        suffix = {_EditKind.MODEL_SQL: "sql", _EditKind.SCHEMA_YML: "yml"}.get(kind)
        return None if suffix is None else f"models/staging/stg_{model}.{suffix}"

    def editing_surface(self) -> list[str]:
        """Everything dbt's own writer accepts: every authored path family, and
        the four root manifests it admits by name.

        Read rather than assumed: a project that configures ``model-paths`` away
        from ``models`` moves its editing surface with it, and the containment
        check has always honored that.

        The root manifests are listed even though they are files rather than
        regions, because ``transform.plans`` re-checks a stored plan against this
        declaration before handing it to the writer. A declaration narrower than
        what the writer accepts is not a modest one: it refuses the project
        config, the profiles, and the package manifests at apply, every one of
        which is a path dex authors through a plan. What a format declares here
        has to be what its writer will take.
        """

        view = self.load()
        families = [path for _name, paths in view.path_families() for path in paths]
        return families + sorted(dbt_project._ALLOWED_ROOT_FILES)
