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

That matters most for the write path. ``maintain reconcile``'s two mechanical write
paths already gate on the ``models/staging/stg_<table>.*`` scaffold convention and
fail closed to advisory, so a generated or derived project tree is safe from them
today -- but safe by naming coincidence rather than by contract, and a format whose
layer directories happened to use that vocabulary would not be. Declining a tier
makes it structural.

**Non-goal: this does not describe how a project is constructed.** Locating and
building one is a separate contract, deliberately left open here: the formats
disagree about what keys them (a directory, a graph already in memory, a service),
and that disagreement does not resolve by picking whichever the first format used.
``StoreContext`` is the shape that question took for storage.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .. import dbt_project
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
    A project reduced from a running graph cannot: its source of truth is the code
    that produced the graph, and writing into the reduction would edit an artifact
    that is regenerated from something else on the next run.

    Declining this tier is the honest answer for such a format, and the reason the
    tiers exist rather than a ``writeback`` flag.
    """

    def write_edits(self, edits: Any) -> Any:
        """Write proposed edits back into the project as reviewable diffs."""
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
    """

    name = "dbt"

    def __init__(
        self,
        repo_root: Path | str = ".",
        project_dir: Path | str | None = None,
    ):
        self.repo_root = Path(repo_root)
        self.project_dir = Path(project_dir) if project_dir is not None else None

    def definitions(self) -> ProjectDefinitions:
        return dbt_project.definitions(self.repo_root, self.project_dir)

    def load(self) -> DbtProjectView:
        """Load the project into an in-memory view.

        Raises ``DbtProjectError`` when there is no project to load, which is what
        every caller of ``dbt_project.load`` already expects. Tier 1 is the channel
        with the never-raises promise.
        """

        return dbt_project.load(self.project_dir or self.repo_root)

    def transform_layer(self) -> TransformLayer:
        return snapshot.transform_layer(self.load())

    def semantic_layer(self) -> SemanticLayer:
        return snapshot.semantic_layer(self.load())

    def write_edits(self, edits: Any) -> Any:
        return dbt_project.write_edits(edits, self.project_dir or self.repo_root)
