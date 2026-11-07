"""`transform test --mutate` on an incremental model's branch, on real dbt (#1132).

Every mutant is built ephemeral, and an ephemeral model is never incremental, so
``{% if is_incremental() %}`` renders false and the branch's watermark never
reached the compiled SQL dex mutates. A model whose only comparison sat in the
branch was refused, and on any other model the branch silently left the score.

These cases pin the fix: the branch is compiled a second time with
``is_incremental()`` taken, its comparisons and filters are planted there, only
the unit tests that override ``is_incremental`` to true run against them, and
the envelope keeps the two programs and their scores apart.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .mutation_helpers import _engine

_WATERMARK_ONLY = """{{ config(materialized='incremental') }}
select id, d from {{ ref('stg_events') }}
{% if is_incremental() %}
where d >= (select max(d) from {{ this }})
{% endif %}
"""

_WITH_FULL_LOGIC = """{{ config(materialized='incremental') }}
select id, d, case when d > 15 then 'late' else 'early' end as band
from {{ ref('stg_events') }}
{% if is_incremental() %}
where d >= (select max(d) from {{ this }})
{% endif %}
"""

_NOT_NULL = """version: 2
models:
  - name: inc
    columns:
      - name: id
        data_tests: [not_null]
"""

#: Pins the branch: `this` already holds d = 20, so a `>=` watermark loads the
#: rows on and after it. A `>` loses the row on it, a dropped filter adds the
#: row before it, and a negated one keeps only that row.
_INCREMENTAL_TEST = """
unit_tests:
  - name: inc_loads_from_the_watermark
    model: inc
    overrides:
      macros:
        is_incremental: true
    given:
      - input: ref('stg_events')
        rows:
          - {id: 1, d: 10}
          - {id: 2, d: 20}
          - {id: 3, d: 30}
      - input: this
        rows:
          - {id: 2, d: 20}
    expect:
      rows:
        - {id: 2, d: 20}
        - {id: 3, d: 30}
"""

_FULL_TEST = """
  - name: inc_full_build_bands
    model: inc
    overrides:
      macros:
        is_incremental: false
    given:
      - input: ref('stg_events')
        rows:
          - {id: 1, d: 10}
          - {id: 2, d: 15}
          - {id: 3, d: 30}
    expect:
      rows:
        - {id: 1, band: 'early'}
        - {id: 2, band: 'early'}
        - {id: 3, band: 'late'}
"""


@pytest.fixture
def incremental_project(tmp_path: Path, duckdb_file: Path) -> Path:
    """An incremental model over a staging view, on real DuckDB."""

    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("dbt.adapters.duckdb")
    warehouse = tmp_path / "dev.duckdb"
    shutil.copy(duckdb_file, warehouse)
    con = duckdb.connect(str(warehouse))
    con.execute(
        "create or replace table main.raw_events as select * from (values "
        "(1, 10), (2, 20), (3, 30)) t(id, d)"
    )
    con.close()

    project = tmp_path / "analytics"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: p\nversion: "1.0.0"\nprofile: p\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        f"p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {warehouse}\n",
        encoding="utf-8",
    )
    (project / "models" / "stg_events.sql").write_text(
        "select id, d from main.raw_events", encoding="utf-8"
    )
    return project


def _measure(project: Path, model_sql: str, schema: str):
    # Built before the unit tests are declared: dbt types a `this` fixture from
    # the model's relation, and a build runs unit tests before the model.
    """Build the model, declare `schema`, and measure it with real dbt."""

    (project / "models" / "inc.sql").write_text(model_sql, encoding="utf-8")
    (project / "models" / "schema.yml").write_text(_NOT_NULL, encoding="utf-8")
    engine = _engine(project)
    engine.build(target="dev")
    (project / "models" / "schema.yml").write_text(schema, encoding="utf-8")
    return engine.test_mutations("inc")


def test_a_model_whose_only_mutable_sql_is_the_branch_is_measured_not_refused(
    incremental_project: Path,
):
    """The issue's reproduction. With no test that overrides ``is_incremental``,
    every defect planted in the branch survives, and says which test would
    catch it, without spending a run to learn that."""

    result = _measure(incremental_project, _WATERMARK_ONLY, _NOT_NULL)

    assert result.counts["generated"] == 0
    assert result.score is None
    branch = result.incremental
    assert branch is not None
    assert branch["tests"] == []
    assert sorted(m["operator"] for m in branch["mutants"]) == [
        "comparison",
        "predicate_drop",
        "predicate_negate",
    ]
    assert branch["counts"]["generated"] == 3
    assert branch["counts"]["survived"] == 3
    assert branch["score"] == 0.0
    assert "is_incremental" in branch["reason"]
    flip = next(m for m in branch["mutants"] if m["operator"] == "comparison")
    assert ">=" in flip["before"] and "{{ this }}" in flip["before"]
    assert ">=" not in flip["after"] and ">" in flip["after"]
    for mutant in branch["mutants"]:
        assert mutant["status"] == "survived"
        assert mutant["id"].startswith("i")
        assert "is_incremental" in mutant["suggested_test"]
        assert mutant["equivalence"]["status"] == "not_checked"
    assert any("is_incremental" in w for w in result.warnings)


def test_a_passing_incremental_unit_test_catches_the_branch_mutants(
    incremental_project: Path,
):
    """The other half of the acceptance: the same mutants, run against a unit
    test that overrides ``is_incremental`` to true, are caught by it."""

    result = _measure(
        incremental_project, _WATERMARK_ONLY, _NOT_NULL + _INCREMENTAL_TEST
    )

    branch = result.incremental
    assert branch["tests"] == ["inc_loads_from_the_watermark"]
    assert branch["reason"] is None
    assert branch["baseline"]["tests"] == [
        {"name": "inc_loads_from_the_watermark", "status": "pass"}
    ]
    assert branch["counts"]["generated"] == 3
    assert branch["counts"]["killed"] == 3, branch["mutants"]
    assert branch["score"] == 1.0
    for mutant in branch["mutants"]:
        assert mutant["caught_by"] == ["inc_loads_from_the_watermark"]


def test_the_full_build_and_the_branch_are_planted_run_and_scored_apart(
    incremental_project: Path,
):
    """A site shared by both programs is the full build's, and only the branch's
    own comparison and filter go to the incremental tests. Neither test set runs
    against the other program's mutants, so neither score mixes the two."""

    result = _measure(
        incremental_project,
        _WITH_FULL_LOGIC,
        _NOT_NULL + _INCREMENTAL_TEST + _FULL_TEST,
    )

    full_tests = {t["name"] for t in result.baseline["tests"]}
    assert "inc_full_build_bands" in full_tests
    assert "inc_loads_from_the_watermark" not in full_tests
    assert result.counts["generated"] >= 2
    for mutant in result.mutants:
        assert "{{ this }}" not in mutant["before"]
        assert mutant["id"].startswith("m")

    branch = result.incremental
    assert {t["name"] for t in branch["baseline"]["tests"]} == {
        "inc_loads_from_the_watermark"
    }
    assert branch["counts"]["generated"] == 3
    for mutant in branch["mutants"]:
        assert "{{ this }}" in mutant["before"]
        assert "case" not in mutant["before"].lower()
    assert branch["score"] == 1.0
    full_decided = (
        result.counts["killed"]
        + result.counts["caught_downstream"]
        + result.counts["survived"]
    )
    assert result.score == result.counts["killed"] / full_decided
    assert result.cap["generated"] == result.counts["generated"] + 3
