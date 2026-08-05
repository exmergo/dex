"""One contract, every project format: the assertions each format owes its callers.

The contract itself lives in `exmergo_dex_core.adapters.conformance`, shipped in the
wheel so a format outside this distribution runs exactly these assertions. This file
is the in-repo consumer of it, and it is deliberately the same few lines a third
party writes: if driving the shipped contract were awkward here, it would be awkward
there.

Format-specific behavior lives in tests/transform/test_dbt_project.py. What is here
is only "does the one shipped format satisfy the seam", plus the control below that
keeps the seam load-bearing rather than merely defined.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core.adapters.conformance import (
    DeclaringProjectContract,
    MaintainProjectContract,
)
from exmergo_dex_core.adapters.project import DbtProject
from exmergo_dex_core.explore import commands as explore_commands

_PROJECT_YML = (
    'name: dex_test\nversion: "1.0.0"\nprofile: dex_test\nmodel-paths: ["models"]\n'
)


def _project(root: Path, schema: str | None = None) -> Path:
    """A minimal dbt project, optionally carrying one schema.yml."""

    project = root / "analytics"
    (project / "models").mkdir(parents=True, exist_ok=True)
    (project / "dbt_project.yml").write_text(_PROJECT_YML, encoding="utf-8")
    (project / "models" / "stg_customers.sql").write_text(
        "select 1 as id\n", encoding="utf-8"
    )
    if schema is not None:
        (project / "models" / "schema.yml").write_text(schema, encoding="utf-8")
    return project


class TestDbtProject(DeclaringProjectContract, MaintainProjectContract):
    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path):
        # One root per assertion: a project is read from disk, so two assertions
        # writing different schema.yml files must not share a directory.
        self.root = tmp_path

    def make_project(self) -> DbtProject:
        project = _project(self.root)
        return DbtProject(self.root, project)

    def make_unreadable_project(self) -> DbtProject:
        # A dbt_project.yml that is not parseable YAML. dbt is a filesystem format,
        # so this state is real for it, which is why the hook is overridden rather
        # than left to skip.
        project = self.root / "broken"
        project.mkdir()
        (project / "dbt_project.yml").write_text("name: [unclosed\n", encoding="utf-8")
        return DbtProject(self.root, project)

    def a_project_declaring_a_unique_key(self) -> tuple[DbtProject, str, str]:
        project = _project(
            self.root,
            "version: 2\n"
            "models:\n"
            "  - name: stg_customers\n"
            "    columns:\n"
            "      - name: id\n"
            "        tests: [unique]\n",
        )
        return DbtProject(self.root, project), "stg_customers", "id"

    def a_project_declaring_a_join(self) -> tuple[DbtProject, str, str, str, str]:
        project = _project(
            self.root,
            "version: 2\n"
            "models:\n"
            "  - name: stg_orders\n"
            "    columns:\n"
            "      - name: customer_id\n"
            "        tests:\n"
            "          - relationships:\n"
            "              to: ref('stg_customers')\n"
            "              field: id\n",
        )
        return (
            DbtProject(self.root, project),
            "stg_orders",
            "customer_id",
            "stg_customers",
            "id",
        )


def test_explore_reads_the_project_through_the_seam(monkeypatch, tmp_path: Path):
    """The control: explore's project read goes through `DbtProject`, not around it.

    Every other assertion in this file would pass just as well if
    `_project_definitions` called `dbt_project.definitions` directly, because
    `DbtProject.definitions` delegates to it -- the results are identical by
    construction. That is exactly what makes this test necessary: without it "the
    suite is green" would not distinguish a seam that is load-bearing from one that
    is merely defined, which is the state this whole protocol exists to leave.
    """

    project = _project(tmp_path)
    calls: list[tuple[Path, Path | None]] = []
    real = DbtProject.definitions

    def recording(self: DbtProject):
        calls.append((self.repo_root, self.project_dir))
        return real(self)

    monkeypatch.setattr(DbtProject, "definitions", recording)

    engine = SimpleNamespace(
        config=SimpleNamespace(dbt_project_dir="analytics"),
        repo_root=str(tmp_path),
    )
    defs = explore_commands._project_definitions(engine, use_project=True)

    assert calls == [(Path(tmp_path), project)], (
        "explore did not read the project through DbtProject; the seam is defined "
        "but not load-bearing"
    )
    assert defs.present
