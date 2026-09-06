"""The two cross-cutting constraints, asserted rather than reviewed.

Both issues state them, and both are the kind of rule that a change satisfying
its own tests can break silently:

- Ossie does not depend on MetricFlow and dbt does not depend on Ossie.
- The base install carries neither the schema validator nor the dialect engine.

An import that arrives transitively breaks the first exactly as thoroughly as
one written by hand, which is why these walk the module graph rather than
grepping for import lines.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import exmergo_dex_core

ROOT = Path(exmergo_dex_core.__file__).parent


def imported_by(module: str) -> set[str]:
    """Every dex module a fresh interpreter pulls in to import ``module``."""

    probe = (
        f"import {module}; import sys;"
        "print('\\n'.join(sorted("
        "m for m in sys.modules if m.startswith('exmergo_dex_core'))))"
    )
    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def test_ossie_imports_no_dbt_or_metricflow_reader():
    """Ossie is the portability blueprint for the next semantic integration, so
    it must be readable without the format it is meant to be independent of."""

    pulled = imported_by("exmergo_dex_core.ossie")
    forbidden = {
        "exmergo_dex_core.dbt_project",
        "exmergo_dex_core.dbt_semantic",
        "exmergo_dex_core.metricflow_dialect",
        "exmergo_dex_core.explore.semantic.local",
    }

    assert not pulled & forbidden, sorted(pulled & forbidden)


def test_ossie_authoring_imports_no_transformation_reader():
    """The neutral plan values must not turn semantic writeback into dbt."""

    pulled = imported_by("exmergo_dex_core.ossie.authoring")
    forbidden = {
        "exmergo_dex_core.dbt_project",
        "exmergo_dex_core.dbt_semantic",
        "exmergo_dex_core.metricflow_dialect",
        "exmergo_dex_core.explore.semantic.local",
    }

    assert not pulled & forbidden, sorted(pulled & forbidden)


def test_the_dbt_reader_does_not_import_ossie():
    """The other direction, which is the one that would make an existing dbt
    deployment depend on a draft interchange schema."""

    assert "exmergo_dex_core.ossie" not in imported_by("exmergo_dex_core.dbt_project")


def test_the_tier_one_type_is_reachable_without_either_format():
    """`ProjectDefinitions` is what every format returns, so it lives in a leaf
    module. Importing it must not drag in the reader that defined it first."""

    pulled = imported_by("exmergo_dex_core.project_definitions")

    assert pulled <= {"exmergo_dex_core", "exmergo_dex_core.project_definitions"}


def test_the_moved_names_are_still_importable_from_their_released_home():
    """They have been public on `dbt_project` since v1."""

    from exmergo_dex_core import dbt_project, project_definitions

    for name in (
        "ProjectDefinitions",
        "DeclaredKey",
        "DeclaredCompositeKey",
        "DeclaredForeignKey",
    ):
        assert getattr(dbt_project, name) is getattr(project_definitions, name)


def test_importing_the_package_pulls_in_neither_optional_dependency():
    """The base install is pydantic and pyyaml. A module that eagerly imported
    the schema validator would break `import exmergo_dex_core` on it."""

    probe = (
        "import exmergo_dex_core, sys;"
        "print(sorted(m for m in sys.modules if m in {'jsonschema','sqlglot'}))"
    )
    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "[]"


def test_importing_the_ossie_package_defers_the_schema_validator():
    """Deferred to the point of use, so a repository that names the format in
    config but never reads a document pays nothing."""

    probe = "import exmergo_dex_core.ossie, sys;print('jsonschema' in sys.modules)"
    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert out.stdout.strip() == "False"


def test_no_source_file_carries_a_blanket_line_length_suppression():
    """The two Ossie scaffolding files were the only two in the source tree
    that did, and both covered long message strings the rest of the codebase
    writes as implicit concatenation."""

    offenders = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if "# ruff: noqa: E501" in path.read_text()
    ]

    assert not offenders, offenders


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_every_documented_suffix_is_accepted(suffix, tmp_path: Path):
    from exmergo_dex_core.adapters.project import ProjectContext
    from exmergo_dex_core.ossie import OssieProject

    OssieProject.from_context(
        ProjectContext(
            repo_root=str(tmp_path),
            connector="duckdb",
            options={"files": [f"a.ossie{suffix}"]},
        )
    )
