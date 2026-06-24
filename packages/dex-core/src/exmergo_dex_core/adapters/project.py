"""The project adapter seam: how dex stays open to more model formats over time.

The source of truth is a *project*, and today the only project format is dbt. This
thin protocol is the extension point: `DbtProject` is the one implementation now,
and future source formats (SQLMesh, Cube) become new implementations of the same
protocol without touching the engine that reasons over a project. This is
deliberately thin. dex does not build a neutral internal model behind it; the
protocol is shaped by what dbt already provides, and other formats adapt to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .. import dbt_project


@runtime_checkable
class ProjectAdapter(Protocol):
    """A project that dex can read as the source of truth and edit as diffs."""

    #: Stable format name, e.g. "dbt".
    name: str

    def load(self) -> Any:
        """Load the project into an in-memory view."""
        ...

    def write_edits(self, edits: Any) -> Any:
        """Write proposed edits back into the project as reviewable diffs."""
        ...


class DbtProject:
    """The dbt implementation of the project seam: the only one in v1.

    Delegates to `dbt_project`, which reads `manifest.json` plus the source files
    and writes edits back. Holds the project directory (class DI) so callers do not
    thread it through every call.
    """

    name = "dbt"

    def __init__(self, project_dir: Path | str = "."):
        self.project_dir = Path(project_dir)

    def load(self) -> Any:
        return dbt_project.load(self.project_dir)

    def write_edits(self, edits: Any) -> Any:
        return dbt_project.write_edits(edits, self.project_dir)
