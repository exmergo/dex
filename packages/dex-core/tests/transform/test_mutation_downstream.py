"""Real dbt mutation tests: downstream. Each case owns its project and warehouse."""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.transform import mutation

from .mutation_helpers import (
    _engine,
    _with_summary,
    _with_versioned_summary,
)
from .mutation_helpers import measurable_project as measurable_project


def test_a_defect_a_downstream_test_catches_is_reported_apart(
    measurable_project: Path,
):
    """The issue's acceptance on real dbt and DuckDB: `fct` carries only a
    `not_null`, so every defect survives its own suite, but the dropped status
    filter brings a cancelled order back and the mart's count test sees it.
    That defect is `caught_downstream`, and `score` reads exactly what it reads
    without the downstream tests."""

    pytest.importorskip("dbt.adapters.duckdb")
    engine = _engine(_with_summary(measurable_project))
    engine.build(target="dev")

    routed = engine.test_mutations("fct")
    alone = engine.test_mutations("fct", downstream=False)

    assert routed.downstream["enabled"] is True, routed.downstream
    assert routed.downstream["models"] == ["fct_summary"]
    assert routed.downstream["tests"] == ["assert_summary_counts"]
    statuses = {m["operator"]: m for m in routed.mutants}
    drop = statuses["predicate_drop"]
    assert drop["status"] == "caught_downstream"
    assert drop["caught_by"] == []
    assert drop["caught_downstream_by"] == ["assert_summary_counts"]
    # The inner join where every key resolves changes no count in the mart.
    assert statuses["join_type"]["status"] == "survived"

    assert routed.counts["caught_downstream"] >= 1
    assert routed.score == alone.score == 0.0
    assert routed.score_with_downstream > 0.0
    assert alone.score_with_downstream is None
    assert alone.downstream["enabled"] is False
    assert "--no-downstream" in alone.downstream["reason"]
    assert alone.counts["caught_downstream"] == 0
    assert any("caught by tests on the models downstream" in w for w in routed.warnings)


def test_a_model_with_only_downstream_tests_is_measured(measurable_project: Path):
    """With no test of its own, every defect used to be refused as unmeasurable.
    A mart test that reads it is a test that can catch it."""

    pytest.importorskip("dbt.adapters.duckdb")
    project = _with_summary(measurable_project)
    (project / "models" / "schema.yml").unlink()
    engine = _engine(project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    assert result.counts["killed"] == 0
    assert result.counts["caught_downstream"] >= 1
    assert result.score == 0.0

    with pytest.raises(mutation.MutationError, match="has no tests"):
        engine.test_mutations("fct", downstream=False)


def test_a_versioned_downstream_model_is_routed_by_its_version_label(
    measurable_project: Path,
):
    """A versioned mart is selected by its dbt label, not the bare name.

    `--downstream` walks the child map and feeds `--select` the labels
    `manifest_node_label` produces. For a model with `versions:`, that label is
    `name.vN`. Pinning it here keeps a refactor of the selector from dropping
    versioned projects silently.
    """

    pytest.importorskip("dbt.adapters.duckdb")
    engine = _engine(_with_versioned_summary(measurable_project))
    engine.build(target="dev")

    result = engine.test_mutations("fct")

    assert result.downstream["enabled"] is True, result.downstream
    assert result.downstream["models"] == ["fct_summary.v1"]
    statuses = {m["operator"]: m for m in result.mutants}
    drop = statuses["predicate_drop"]
    assert drop["status"] == "caught_downstream"
    assert drop["caught_downstream_by"] == ["assert_summary_counts"]
