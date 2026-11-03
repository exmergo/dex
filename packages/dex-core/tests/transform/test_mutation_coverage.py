"""Mutation coverage and read-only guarantees on real dbt and DuckDB."""

from __future__ import annotations

from pathlib import Path

import pytest

from .mutation_helpers import _engine, _schema_yml, _tree_digest, _with_summary
from .mutation_helpers import measurable_project as measurable_project


@pytest.mark.parametrize("with_unit_test", [False, True])
def test_a_stronger_suite_catches_more_planted_defects(
    measurable_project: Path, with_unit_test: bool
):
    """The acceptance the issue asks for, both directions in one test: a model
    carrying only a `not_null` lets the defects through, and adding one unit test
    that pins the model's actual rules catches most of them."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(with_unit_test), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    counts = result.counts
    assert counts["generated"] >= 4
    if with_unit_test:
        assert counts["killed"] > counts["survived"], result.mutants
        assert result.score > 0.5
    else:
        assert counts["killed"] == 0
        assert counts["survived"] == counts["generated"]
        assert result.score == 0.0


def test_every_kind_of_test_gets_to_answer_for_the_mutant(measurable_project: Path):
    """Generic, singular and unit tests all run against an ephemeral mutant. If
    any kind were silently dropped, its defects would all read as survivors."""

    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(True), encoding="utf-8"
    )
    (measurable_project / "tests").mkdir()
    (measurable_project / "tests" / "no_null_region.sql").write_text(
        "select id from {{ ref('fct') }} where region is null", encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct")
    names = {test["name"] for test in result.baseline["tests"]}
    assert "not_null_fct_id" in names
    assert "no_null_region" in names
    assert "fct_pins_its_rules" in names


def test_a_mutation_run_writes_nothing_and_materializes_nothing(
    measurable_project: Path,
):
    """The two guarantees the safety spine turns on: the project is a byte for
    byte match afterwards, and the dev warehouse gained no relation."""

    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("dbt.adapters.duckdb")
    (measurable_project / "models" / "schema.yml").write_text(
        _schema_yml(True), encoding="utf-8"
    )
    engine = _engine(measurable_project)
    engine.build(target="dev")

    warehouse = measurable_project.parent / "dev.duckdb"

    def relations():
        con = duckdb.connect(str(warehouse), read_only=True)
        try:
            return set(
                con.execute(
                    "select table_schema, table_name from information_schema.tables"
                ).fetchall()
            )
        finally:
            con.close()

    before_tree, before_relations = _tree_digest(measurable_project), relations()
    result = engine.test_mutations("fct")

    assert result.equivalence["self_check"] == "reproducible", "nothing was compared"
    assert _tree_digest(measurable_project) == before_tree
    assert relations() == before_relations


def test_routing_through_a_downstream_model_writes_and_materializes_nothing(
    measurable_project: Path,
):
    """The mart is made ephemeral in the copy, not rebuilt: the project is a
    byte for byte match afterwards and the dev warehouse gained no relation and
    lost none, the mart's own view included."""

    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("dbt.adapters.duckdb")
    engine = _engine(_with_summary(measurable_project))
    engine.build(target="dev")
    warehouse = measurable_project.parent / "dev.duckdb"

    def relations():
        con = duckdb.connect(str(warehouse), read_only=True)
        try:
            return set(
                con.execute(
                    "select table_schema, table_name, table_type "
                    "from information_schema.tables"
                ).fetchall()
            )
        finally:
            con.close()

    before_tree, before_relations = _tree_digest(measurable_project), relations()
    result = engine.test_mutations("fct")

    assert result.downstream["enabled"] is True
    assert _tree_digest(measurable_project) == before_tree
    assert relations() == before_relations
