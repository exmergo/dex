"""Shadow-copy compatibility and survivor equivalence on real dbt and DuckDB."""

from __future__ import annotations

from pathlib import Path

import pytest

from .mutation_helpers import _engine, _schema_yml
from .mutation_helpers import measurable_project as measurable_project


def test_mutation_testing_on_model_referenced_by_semantic_model(
    measurable_project: Path,
):
    """Regression for #727: transform test --mutate fails on any model a
    semantic model refs because an ephemeral mutant has no relation name.
    The referencing semantic model must be dropped from the shadow copy so
    dbt can compile and run data tests on the mutant."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "metricflow_time_spine.sql").write_text(
        "select date '2020-01-01' + cast(i as integer) as date_day "
        "from range(365) t(i)",
        encoding="utf-8",
    )
    (measurable_project / "models" / "semantic.yml").write_text(
        "version: 2\n"
        "semantic_models:\n"
        "  - name: metricflow_time_spine\n"
        "    model: ref('metricflow_time_spine')\n"
        "    primary_entity: date_day\n"
        "    dimensions:\n"
        "      - name: date_day\n"
        "        type: time\n"
        "        type_params:\n"
        "          time_granularity: day\n"
        "  - name: fct_sem\n"
        "    model: ref('fct')\n"
        "    entities:\n"
        "      - name: id\n"
        "        type: primary\n"
        "    dimensions:\n"
        "      - name: region\n"
        "        type: categorical\n",
        encoding="utf-8",
    )
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(False), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    assert result.counts["generated"] >= 4
    assert any(
        "semantic models referencing 'fct' were omitted" in w for w in result.warnings
    )
    semantic_file = measurable_project / "models" / "semantic.yml"
    assert semantic_file.is_file()
    assert "fct_sem" in semantic_file.read_text(encoding="utf-8")


def test_survivors_are_labelled_by_what_the_dev_data_can_tell_apart(
    measurable_project: Path,
):
    """The issue's acceptance, on real dbt and real DuckDB, under a `not_null`
    suite that lets every defect through.

    Every order's customer exists, so the inner join turned left produces the
    same rows, and no amount is above 100, so the dropped `large` branch changes
    nothing either: both are equivalent on this data and need a fixture. The
    dropped status filter brings the cancelled order back and the boundary flip
    re-bands the order sitting on 100, so the data already tells those apart.
    """

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(False), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")

    labels = {m["operator"]: m["equivalence"] for m in result.mutants}
    assert labels["join_type"]["status"] == "equivalent"
    assert labels["join_type"]["only_in_mutant"] == 0
    assert labels["join_type"]["baseline_rows"] == 2
    assert labels["case_branch"]["status"] == "equivalent"
    assert labels["predicate_drop"]["status"] == "distinguishable"
    assert labels["predicate_drop"]["only_in_mutant"] == 1
    assert labels["predicate_drop"]["only_in_baseline"] == 0
    assert labels["comparison"]["status"] == "distinguishable"
    assert labels["comparison"]["only_in_mutant"] == 1
    assert labels["comparison"]["only_in_baseline"] == 1

    ranked = [m["equivalence"]["status"] for m in result.mutants]
    assert ranked == sorted(ranked, key=["distinguishable", "equivalent"].index)
    assert result.equivalence["self_check"] == "reproducible"
    assert result.counts["survived"] == result.counts["generated"]
    assert result.score == 0.0, "an equivalent survivor still counts against the suite"


def test_a_model_that_does_not_reproduce_itself_is_caught_by_the_self_check(
    measurable_project: Path,
):
    """`random()` differs between two evaluations of the same model, so any
    mutant would look different from it. The self-check is what notices."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "fct_noisy.sql").write_text(
        "select id, random() as jitter from {{ ref('stg_orders') }} where amount > 5",
        encoding="utf-8",
    )
    (measurable_project / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: fct_noisy\n    columns:\n"
        "      - name: id\n        data_tests: [not_null]\n",
        encoding="utf-8",
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct_noisy")

    assert result.equivalence["self_check"] == "not_reproducible"
    assert all(
        m["equivalence"]["status"] == "not_checked"
        for m in result.mutants
        if m["status"] == "survived"
    )


def test_the_cap_narrows_the_run_and_reports_what_it_cut(measurable_project: Path):
    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(False), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct", max_mutants=2)
    assert result.counts["generated"] == 2
    assert result.cap["limit"] == 2
    assert sum(result.cap["elided"].values()) > 0
    assert any("not run" in w and "cap" in w for w in result.warnings)
