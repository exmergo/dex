"""Executable contract for a file-backed semantic edit target.

This is intentionally parallel to the project-tier conformance suite at the
abstraction boundary, not at the safety guarantees. A semantic layer must prove
the same stale-edit, atomicity, result, view, and containment behavior without
structurally becoming an ``EditableProject`` or ``PlacingProject``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .edits import EditOp, SemanticEditTarget


def _copy_edit(template: Any, **updates: Any) -> Any:
    if hasattr(template, "model_copy"):
        return template.model_copy(update=updates)
    raise AssertionError(
        "semantic edit conformance needs pydantic Edit/PlanEdit values so pins "
        "can be varied without changing the implementation under test"
    )


class SemanticEditTargetContract:
    """The editable and placement guarantees for a semantic-layer source."""

    def make_semantic_edit_target(self) -> SemanticEditTarget:
        raise NotImplementedError

    def an_edit_against_a_changed_semantic_target(
        self,
    ) -> tuple[SemanticEditTarget, list[Any], Any]:
        """Return ``(target, edits, read_changed_target)`` for a stale edit."""

        raise NotImplementedError

    def a_clean_semantic_edit(self, target: SemanticEditTarget) -> tuple[Any, Any]:
        """Return a clean sibling edit and a callable reading its target."""

        raise NotImplementedError

    def test_satisfies_the_semantic_edit_capability(self) -> None:
        assert isinstance(self.make_semantic_edit_target(), SemanticEditTarget)

    def test_the_semantic_view_pins_the_write_keyspace(self) -> None:
        target, edits, _read = self.an_edit_against_a_changed_semantic_target()
        view = target.semantic_edit_view()
        assert isinstance(getattr(view, "root", None), str) and view.root
        assert edits
        entry = view.files.get(edits[0].path)
        assert entry is not None
        assert isinstance(getattr(entry, "content", None), str)
        assert isinstance(getattr(entry, "sha256", None), str) and entry.sha256

    def test_an_unconfirmed_semantic_write_refuses_a_stale_target(self) -> None:
        target, edits, read_target = self.an_edit_against_a_changed_semantic_target()
        before = read_target()

        result = target.write_semantic_edits(edits)

        assert read_target() == before
        assert not result.written
        assert result.conflicts

    def test_a_confirmed_semantic_write_overrides_the_conflict(self) -> None:
        target, edits, read_target = self.an_edit_against_a_changed_semantic_target()
        before = read_target()

        result = target.write_semantic_edits(edits, confirmed=True)

        assert read_target() != before
        assert result.written
        assert result.conflicts

    def test_a_refused_semantic_apply_is_atomic(self) -> None:
        target, edits, read_target = self.an_edit_against_a_changed_semantic_target()
        clean, read_clean = self.a_clean_semantic_edit(target)
        before_changed = read_target()
        before_clean = read_clean()

        result = target.write_semantic_edits([clean, *edits])

        assert read_target() == before_changed
        assert read_clean() == before_clean
        assert not result.written
        assert result.conflicts

    def test_a_create_pin_refuses_a_semantic_target_that_now_exists(self) -> None:
        target, edits, read_target = self.an_edit_against_a_changed_semantic_target()
        assert edits
        before = read_target()
        create = _copy_edit(
            edits[0], old_content_hash=None, op=EditOp.UPSERT
        )

        result = target.write_semantic_edits([create])

        assert read_target() == before
        assert not result.written
        assert result.conflicts

    def test_semantic_write_refuses_outside_its_declared_surface(self) -> None:
        target, edits, read_target = self.an_edit_against_a_changed_semantic_target()
        assert edits
        surface = target.semantic_editing_surface()
        assert surface
        sibling = PurePosixPath(surface[0])
        outside = str(sibling.with_name(f"outside_{sibling.name}"))
        escaping = _copy_edit(
            edits[0], path=outside, old_content_hash=None, op=EditOp.UPSERT
        )
        before = read_target()

        try:
            result = target.write_semantic_edits([escaping])
        except Exception:
            result = None

        assert read_target() == before
        assert result is None or not result.written

    def test_semantic_editing_surface_contains_only_relative_keys(self) -> None:
        for key in self.make_semantic_edit_target().semantic_editing_surface():
            path = PurePosixPath(key.replace("\\", "/"))
            assert not path.is_absolute()
            assert ".." not in path.parts
