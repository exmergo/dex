"""Where a fake dbt writes its artifacts in tests (issue #1014).

Every dbt invocation Dex runs on a project gets ``--target-path`` pointing at
Dex's isolated runtime directory, ``<project>/target/dex/<dbt-version>``. A
test that stands in for dbt writes there, as the real one would.
"""

from __future__ import annotations

from pathlib import Path


def dex_target(project: Path) -> Path:
    """The directory Dex's dbt invocations write for ``project``."""

    from exmergo_dex_core.transform.artifact_runtime import isolated_artifact_dir

    directory = isolated_artifact_dir(project)
    assert directory is not None, "dbt-core is not installed"
    return directory
