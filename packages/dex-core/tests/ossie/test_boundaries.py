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


#: Third-party module prefixes worth naming when they arrive, alongside dex's
#: own. The dex half catches a reader importing the other format's reader; this
#: half catches the same mistake arriving one level down, through a library. A
#: probe that only reported `exmergo_dex_core` modules would pass a change that
#: imported `dbt.contracts` directly, which is the heavier dependency of the two.
WATCHED_PREFIXES = ("exmergo_dex_core", "dbt", "metricflow", "sqlglot", "jsonschema")


def imported_by(
    module: str, *, prefixes: tuple[str, ...] = WATCHED_PREFIXES
) -> set[str]:
    """Every watched module a fresh interpreter pulls in to import ``module``.

    A fresh interpreter, and the module graph rather than the import lines,
    because an import that arrives transitively breaks a boundary exactly as
    thoroughly as one written by hand and a grep cannot see it.
    """

    probe = (
        f"import {module}; import sys;"
        f"watched = {prefixes!r};"
        "print('\\n'.join(sorted("
        "m for m in sys.modules if m.startswith(watched))))"
    )
    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def top_level(modules: set[str]) -> set[str]:
    """The distributions behind a set of module names, for a readable failure."""

    return {name.split(".")[0] for name in modules}


def test_ossie_imports_no_dbt_or_metricflow_reader():
    """Ossie is the portability blueprint for the next semantic integration, so
    it must be readable without the format it is meant to be independent of.

    Both levels: dex's own dbt and MetricFlow readers, and the third-party
    libraries behind them. The second is the one a `[ossie]`-only install
    actually cannot satisfy, since neither library is there to import.
    """

    pulled = imported_by("exmergo_dex_core.ossie")
    forbidden = {
        "exmergo_dex_core.dbt_project",
        "exmergo_dex_core.dbt_semantic",
        "exmergo_dex_core.metricflow_dialect",
        "exmergo_dex_core.explore.semantic.local",
    }

    assert not pulled & forbidden, sorted(pulled & forbidden)
    assert not top_level(pulled) & {"dbt", "metricflow"}, sorted(pulled)


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
    assert not top_level(pulled) & {"dbt", "metricflow"}, sorted(pulled)


def test_the_semantic_source_seam_pulls_in_no_reader_at_all():
    """The seam every semantic source is constructed through has to be
    importable without any of them.

    It resolves a factory by dotted path, so importing it must not import what
    it can resolve: a base install that eagerly loaded the Ossie reader to define
    the seam would pull the schema validator that install does not have.
    """

    pulled = imported_by("exmergo_dex_core.semantic_source")

    assert "exmergo_dex_core.ossie" not in pulled, sorted(pulled)
    assert not top_level(pulled) - {"exmergo_dex_core"}, sorted(pulled)


def test_the_dbt_reader_does_not_import_ossie():
    """The other direction, which is the one that would make an existing dbt
    deployment depend on a draft interchange schema."""

    pulled = imported_by("exmergo_dex_core.dbt_project")

    assert "exmergo_dex_core.ossie" not in pulled, sorted(pulled)
    assert "jsonschema" not in top_level(pulled), sorted(pulled)


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
    from exmergo_dex_core.ossie import OssieSemanticLayer
    from exmergo_dex_core.semantic_source import SemanticSourceContext

    OssieSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(tmp_path),
            connector="duckdb",
            options={"files": [f"a.ossie{suffix}"]},
        )
    )
