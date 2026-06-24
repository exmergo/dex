"""The dbt project: the source of truth (read and write).

dex maintains no canonical model of its own. The dbt project is canonical, and
this module is the interface to it. Reads load the project into an in-memory view,
primarily from the compiled `manifest.json` (dbt's own documented, versioned
serialization of nodes, sources, tests, semantic models, metrics, and lineage),
supplemented by the raw source files for editing. Writes go back into the source
files as reviewable diffs; dex never holds a competing copy, so human dbt edits are
authoritative by construction.

Absent a dbt project, explore still works (writing only to the `.dex/` cache), but
transform and model require one, since dbt is what they edit. Not yet implemented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class DbtProjectError(Exception):
    pass


def load(project_dir: Path | str = ".") -> Any:
    """Load the dbt project (manifest + source files) into an in-memory view that
    is the source of truth for transform and model."""

    raise NotImplementedError


def write_edits(edits: Any, project_dir: Path | str = ".") -> Any:
    """Write proposed edits back into the dbt project's source files as reviewable
    diffs. Never silently overwrites hand-written intent."""

    raise NotImplementedError
