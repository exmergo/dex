"""The seam as a second format meets it: a project that is not a directory.

`test_project_parity.py` runs the shipped contract against the shipped format,
which shares an author with the protocol and so cannot show whether the contract
is implementable by someone reading only what is published. This file builds a
format that is neither dbt nor a filesystem, and then breaks it on purpose.

Two shapes matter here and are covered nowhere else:

- **A tier-3 format keyed by its own keyspace.** dbt's view is a directory tree,
  so every path in it is also a real path, and the two are impossible to tell
  apart from inside the contract. Here the keys are a namespace and nothing else,
  which is the case ``edit_path`` and ``editing_surface`` were widened for.
- **A contract that catches what it claims to.** Each defect below passed the
  contract as it stood: an apply that lands the clean half of a refused set, a
  create pinned to no prior content overwriting a file that appeared during
  review, containment by string prefix, and a format missing the member no
  protocol declared. A conformance suite that only proves a correct format
  correct has not been shown to do anything.
"""

from __future__ import annotations

from typing import Any

import pytest

from exmergo_dex_core.adapters.conformance import (
    EditableProjectContract,
    PlacingProjectContract,
)
from exmergo_dex_core.dbt_project import (
    ApplyResult,
    Conflict,
    Edit,
    EditOp,
    ProjectDefinitions,
    content_hash,
)
from exmergo_dex_core.maintain.snapshot import (
    SemanticLayer,
    SourceTable,
    TransformLayer,
)
from exmergo_dex_core.transform.plans import EditKind, PlanError, contained_key

_DECLARATION = "declarations/orders.yml"
_ORIGINAL = "version: 2\nmodels:\n  - name: orders\n"
_PROPOSED = "version: 2\nmodels:\n  - name: orders\n    tests: [unique]\n"
_HUMAN = "version: 2\nmodels:\n  - name: orders  # mine\n"
_NOTE = "models are reduced from a graph, so only the declarations are files"


class KeyspaceFile:
    """One entry in the view: content, and the hash the writer re-checks."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.sha256 = content_hash(content)


class KeyspaceView:
    """A root that names nothing on disk, and files keyed by declaration."""

    def __init__(self, root: str, files: dict[str, KeyspaceFile]) -> None:
        self.root = root
        self.files = files


class ViewlessKeyspaceProject:
    """Tier 3 and placing in every member a protocol used to declare.

    The split between this and :class:`KeyspaceProject` is the first defect
    rather than an abstraction: this is the format the seam let through, passing
    the whole suite and raising ``AttributeError: load`` on the first reconcile a
    user ran. Everything below is shared because everything below was fine.

    Its models come from a graph it never reads here; its declarations are files
    someone wrote, held in memory because nothing about this format is a
    directory. That is the shape tier 3 is for: an edit lands in the half that is
    hand-authored, and the reduced half declines.
    """

    name = "keyspace"

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = dict(files or {})

    # --- tiers 1 and 2 --------------------------------------------------------

    def definitions(self) -> ProjectDefinitions:
        return ProjectDefinitions(
            present=True,
            built_relation_names=["orders"] if self.files else [],
            notes=[_NOTE],
        )

    def transform_layer(self) -> TransformLayer:
        return TransformLayer(
            models=["orders"] if self.files else [],
            sources=[SourceTable(source_name="raw", table="orders")],
            notes=[_NOTE],
        )

    def semantic_layer(self) -> SemanticLayer:
        return SemanticLayer(notes=[_NOTE])

    # --- placement and the write path -----------------------------------------

    def edit_path(self, kind: EditKind, model: str) -> str | None:
        # No authored model SQL: that half is the reduction, and writing into it
        # would edit an artifact the next run regenerates.
        return f"declarations/{model}.yml" if kind is EditKind.SCHEMA_YML else None

    def editing_surface(self) -> list[str]:
        return ["declarations"]

    def _contain(self, path: str) -> None:
        contained_key(path, self.editing_surface())

    def write_edits(
        self, edits: Any, project_dir: Any = None, *, confirmed: bool = False
    ) -> ApplyResult:
        """Hash-checked and all-or-nothing, in a keyspace rather than a tree."""

        staged: list[tuple[str, Edit]] = []
        conflicts: list[Conflict] = []
        for edit in edits:
            self._contain(edit.path)
            current = self.files.get(edit.path)
            found = content_hash(current) if current is not None else None
            if found != edit.old_content_hash:
                conflicts.append(
                    Conflict(
                        path=edit.path,
                        expected_sha256=edit.old_content_hash,
                        found_sha256=found,
                    )
                )
            staged.append((edit.path, edit))

        if conflicts and not confirmed:
            return ApplyResult(written=[], diffs=[], conflicts=conflicts)

        written: list[str] = []
        for path, edit in staged:
            if edit.op is EditOp.DELETE:
                self.files.pop(path, None)
            else:
                self.files[path] = edit.new_content
            written.append(path)
        return ApplyResult(written=written, diffs=[], conflicts=conflicts)


class KeyspaceProject(ViewlessKeyspaceProject):
    """The whole seam: the keyspace can be read, placed into, and written."""

    def load(self) -> KeyspaceView:
        return KeyspaceView(
            root="graph://orders",
            files={key: KeyspaceFile(content) for key, content in self.files.items()},
        )


# --- the four defects, one per assertion that now exists ----------------------


class PartialWriter(KeyspaceProject):
    """Refuses the conflicting edit and writes the rest of the set.

    The worst of them: the project ends up matching neither the proposal nor what
    the human had, the apply reports itself refused, and nothing records which
    half landed.
    """

    def write_edits(
        self, edits: Any, project_dir: Any = None, *, confirmed: bool = False
    ) -> ApplyResult:
        conflicts, written = [], []
        for edit in edits:
            self._contain(edit.path)
            current = self.files.get(edit.path)
            found = content_hash(current) if current is not None else None
            if found != edit.old_content_hash and not confirmed:
                conflicts.append(
                    Conflict(
                        path=edit.path,
                        expected_sha256=edit.old_content_hash,
                        found_sha256=found,
                    )
                )
                continue
            self.files[edit.path] = edit.new_content
            written.append(edit.path)
        return ApplyResult(written=written, diffs=[], conflicts=conflicts)


class SilentPartialWriter(PartialWriter):
    """Writes half the set and reports nothing written.

    The reason :meth:`a_clean_edit` is worth supplying. Asking the result what it
    wrote catches the honest partial writer above; only reading the target
    catches this one, and both leave the same project behind.
    """

    def write_edits(
        self, edits: Any, project_dir: Any = None, *, confirmed: bool = False
    ) -> ApplyResult:
        result = super().write_edits(edits, project_dir, confirmed=confirmed)
        if result.conflicts and not confirmed:
            return ApplyResult(
                written=[], diffs=result.diffs, conflicts=result.conflicts
            )
        return result


class TrustingCreator(KeyspaceProject):
    """Reads a pin of ``None`` as "nothing to compare against, go ahead".

    Every other assertion passes, because in the staged conflict the pinned hash
    is a real one and this only mishandles the create.
    """

    def write_edits(
        self, edits: Any, project_dir: Any = None, *, confirmed: bool = False
    ) -> ApplyResult:
        creates = [e for e in edits if e.old_content_hash is None]
        rest = [e for e in edits if e.old_content_hash is not None]
        for edit in creates:
            self._contain(edit.path)
            self.files[edit.path] = edit.new_content
        result = super().write_edits(rest, project_dir, confirmed=confirmed)
        return ApplyResult(
            written=[e.path for e in creates] + result.written,
            diffs=result.diffs,
            conflicts=result.conflicts,
        )


class PrefixContainer(KeyspaceProject):
    """Containment by string prefix, so `declarations` admits its neighbors."""

    def _contain(self, path: str) -> None:
        if not any(path.startswith(prefix) for prefix in self.editing_surface()):
            raise PlanError(f"outside the surface: '{path}'")


class HashlessView(KeyspaceProject):
    """A view whose entries carry content and no hash.

    Nothing raises. Every existing file pins as a create, so a one-line change is
    rendered as a whole-file overwrite and the apply that follows conflicts on a
    file nobody edited.
    """

    def load(self) -> KeyspaceView:
        view = super().load()
        for entry in view.files.values():
            del entry.sha256
        return view


class _KeyspaceContract(PlacingProjectContract, EditableProjectContract):
    """The same few lines a third party writes, over a swappable format."""

    project_cls: type[ViewlessKeyspaceProject] = KeyspaceProject

    def make_project(self) -> Any:
        return self.project_cls()

    def make_unreadable_project(self) -> None:
        # A format reduced from objects already in memory has no unparseable
        # state, which is the case the hook's default exists for.
        return None

    def placeable_model(self) -> str:
        return "orders"

    def an_edit_against_a_changed_target(self) -> tuple[Any, Any, Any, Any]:
        project = self.project_cls({_DECLARATION: _ORIGINAL})
        edit = Edit(
            path=_DECLARATION,
            new_content=_PROPOSED,
            old_content_hash=content_hash(_ORIGINAL),
        )
        # The human, arriving after the plan pinned the hash above.
        project.files[_DECLARATION] = _HUMAN
        return project, None, [edit], lambda: project.files.get(_DECLARATION)

    def a_clean_edit(self, project: Any) -> tuple[Any, Any]:
        # Supplied rather than left to the derived default, so the assertion asks
        # what the project holds instead of what the writer said it wrote.
        beside = "declarations/customers.yml"
        edit = Edit(
            path=beside,
            new_content="version: 2\nmodels:\n  - name: customers\n",
            old_content_hash=None,
        )
        return edit, lambda: project.files.get(beside)


class TestKeyspaceProject(_KeyspaceContract):
    """A format that is not dbt and not a directory, run through the contract."""


@pytest.mark.parametrize(
    ("format_cls", "assertion"),
    [
        (
            PartialWriter,
            "test_a_refused_apply_leaves_every_target_alone",
        ),
        (
            SilentPartialWriter,
            "test_a_refused_apply_leaves_every_target_alone",
        ),
        (
            TrustingCreator,
            "test_a_create_pinned_absent_refuses_a_target_that_now_exists",
        ),
        (
            PrefixContainer,
            "test_write_edits_refuses_a_path_outside_the_declared_surface",
        ),
        (
            ViewlessKeyspaceProject,
            "test_satisfies_the_placing_protocol",
        ),
        (
            HashlessView,
            "test_the_view_pins_what_an_edit_is_written_against",
        ),
    ],
)
def test_the_contract_catches_the_defect(
    format_cls: type[ViewlessKeyspaceProject], assertion: str
) -> None:
    """Each defect fails the assertion that exists for it, by name.

    Run directly rather than through a collected subclass: what is under test is
    that the assertion fails, so a failing test is the passing outcome. The
    message matters as much as the failure, which is why each defect is named for
    what it does to somebody's project rather than for the method it overrides.
    """

    case = type("Mutated", (_KeyspaceContract,), {"project_cls": format_cls})()

    with pytest.raises(AssertionError):
        getattr(case, assertion)()


def test_a_format_missing_load_is_diagnosed_rather_than_left_to_crash() -> None:
    """The gap is named at the seam, not met as ``AttributeError`` in a command.

    This is the format the issue behind these assertions describes: four declared
    methods implemented in full, the undeclared one absent, tier 3 answered, the
    gate passed, and the failure arriving inside a reconcile someone ran.
    """

    from exmergo_dex_core.adapters.project import (
        EditableProject,
        PlacingProject,
        placement_gap,
    )

    project = ViewlessKeyspaceProject()

    assert isinstance(project, EditableProject)
    assert not isinstance(project, PlacingProject)
    gap = placement_gap(project)
    assert gap is not None and "`load()`" in gap
    assert "edit_path" not in gap.split("missing")[1].split(".")[0]
