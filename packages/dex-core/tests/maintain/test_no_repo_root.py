"""maintain against an engine that was never given a repo root.

A host pointed at a warehouse and a baseline with no repository in the picture
is an ordinary state: `ProjectContext.repo_root` is documented as `None` when
there is none, and the shipped dbt format correctly refuses to build without
one, because a dbt project is a filesystem artifact. What the commands here owe
that host is every answer that does not come from a project, which for `check`
is the schema and volume axes, for `grain` the measured half of the survey, and
for `reconcile` the advisory proposals.

The baseline is built through the CLI fixture, which does have a repo root, and
then read back through a repo-less engine over the same store. That the store
knows a path and the engine does not is the point rather than an artifact: the
storage seam and the project seam are separate, and only the second one needs a
repository.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.maintain import commands as maintain_cmds
from exmergo_dex_core.storage import FilesystemStore


@pytest.fixture
def repoless_engine(maintain_repo):
    """A drifted baseline, reachable only through an engine with no repo root."""

    maintain_repo.snapshot()
    maintain_repo.sql(
        "ALTER TABLE customers ADD COLUMN phone VARCHAR",
        "DELETE FROM stg_orders WHERE order_id > 20",
    )

    engine = DexEngine(
        connector="duckdb",
        path=str(maintain_repo.db_path),
        store=FilesystemStore(maintain_repo.root),
    )
    assert engine.repo_root is None
    with engine:
        yield engine


def test_check_answers_its_free_axes_without_a_project(repoless_engine):
    """The warning naming the missing project was already built and then thrown
    away: `check` appended it, ran schema and volume, and died reading the
    project a second time for the declared grain."""

    result = maintain_cmds.check(repoless_engine)

    assert {f.code for f in result.by_axis["schema"].findings} == {"column_added"}
    assert {f.code for f in result.by_axis["volume"].findings} == {"row_count_changed"}
    assert "semantic" not in result.by_axis
    assert any("no project" in warning for warning in result.warnings)


def test_grain_surveys_what_it_measured_without_a_project(repoless_engine):
    """The declared half of the survey is the project speaking for itself, so
    it is what a missing project costs. The measured half is the cache's."""

    result = maintain_cmds.grain_drift(repoless_engine)

    assert result.by_axis["grain"].findings


def test_reconcile_proposes_advisory_fixes_without_a_project(repoless_engine):
    """`reconcile` already says only the plan store needs a repo root. The
    proposals come from the findings, the baseline, and the cache."""

    maintain_cmds.check(repoless_engine)

    result = maintain_cmds.reconcile(repoless_engine)

    assert result.proposals
    assert result.plan_id is None
    assert any(
        "no project format could be built" in warning for warning in result.warnings
    )
    # The write-tier sentence would claim the format declined something it was
    # never asked about.
    assert not any("does not implement the write tier" in w for w in result.warnings)
