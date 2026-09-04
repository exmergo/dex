"""Format-neutral edit, conflict, and apply values.

Transformation projects and file-backed semantic layers share these values so
the plan/apply safety spine does not make either abstraction depend on the
other's reader.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator


class EditOp(str, Enum):
    """The operation an edit performs, orthogonal to its format-specific kind."""

    UPSERT = "upsert"
    DELETE = "delete"


class Edit(BaseModel):
    """One proposed file change, pinned to its content at plan time."""

    path: str
    new_content: str | None = None
    old_content_hash: str | None = None
    op: EditOp = EditOp.UPSERT

    @model_validator(mode="after")
    def _content_matches_op(self) -> Edit:
        if self.op is EditOp.UPSERT and self.new_content is None:
            raise ValueError(f"an upsert edit needs new_content: '{self.path}'")
        if self.op is EditOp.DELETE and self.new_content is not None:
            raise ValueError(f"a delete edit carries no new_content: '{self.path}'")
        return self


class Conflict(BaseModel):
    path: str
    expected_sha256: str | None
    found_sha256: str | None


class ApplyResult(BaseModel):
    written: list[str] = Field(default_factory=list)
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SourceFileView(Protocol):
    content: str
    sha256: str


class EditView(Protocol):
    root: str
    files: Mapping[str, SourceFileView]


@runtime_checkable
class SemanticEditTarget(Protocol):
    """A file-backed semantic layer that can receive reviewable edits."""

    def semantic_edit_view(self) -> EditView: ...

    def semantic_editing_surface(self) -> list[str]: ...

    def write_semantic_edits(
        self, edits: Any, *, confirmed: bool = False
    ) -> ApplyResult: ...
