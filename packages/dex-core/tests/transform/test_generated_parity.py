"""The generated-test parity harness on DuckDB, in the offline suite (#1450).

Each row of :data:`generated_parity.ROWS` builds a violating and a clean
fixture model through the CLI and holds the generated test's verdict to
``maintain verify``'s. The same runner carries Postgres and the billed dialects
in ``integration.yml`` (#1451). The planted macros show the harness is not
vacuous: a ``duckdb__`` override that passes a duplicated key fails it, and a
broken override for another dialect leaves DuckDB's answer alone, since dbt's
dispatch picks one implementation per adapter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from transform.generated_parity import (
    POPULATION_EXCLUDES,
    POPULATION_INCLUDES,
    POPULATION_INCLUDES_NULL,
    ROWS,
    UNIQUE_COMBINATION,
    run_parity,
)

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main

pytest.importorskip("dbt.cli.main")
pytest.importorskip("dbt.adapters.duckdb")


@pytest.fixture
def repo(dbt_project_dir: Path, tmp_path: Path) -> Path:
    # Only the fixture models: the project's own staging model reads nothing
    # this dev database has, and is never selected.
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    return tmp_path


def _runner(capsys):
    def run(argv: list[str]) -> tuple[int, dict]:
        rc = main(argv)
        return rc, json.loads(capsys.readouterr().out)

    return run


def _planted(
    adapter: str,
    sql: str,
    test: str = "dex_unique_combination",
    signature: str = "model, columns",
) -> dict:
    """A per-dialect implementation of the test, as a project would override it."""

    return {
        "path": f"macros/{adapter}__{test}.sql",
        "kind": "macro_sql",
        "content": (
            f"{{% macro {adapter}__{test}({signature}) %}}\n{sql}\n{{% endmacro %}}\n"
        ),
    }


#: Only the composite-grain row, for the checks that plant its macro.
GRAIN_ONLY = (UNIQUE_COMBINATION,)


#: Passes on any model: no row, whatever the grain holds.
PASSES_ANYWAY = "select 1 as dex_duplicate_rows where 1 = 0"
#: Not SQL any warehouse runs.
BROKEN = "this is not sql"


def test_every_row_agrees_with_maintain_verify_on_duckdb(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(_runner(capsys), repo, dbt_project_dir, connector="duckdb")

    assert report.disagreements == [], report.disagreements
    assert report.agreed == [row.label for row in ROWS]
    assert report.unsupported == []


def test_the_plan_writes_the_shipped_macro_beside_the_rendered_test(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(_runner(capsys), repo, dbt_project_dir, connector="duckdb")

    paths = report.envelopes["plan"]["data"]["paths"]
    assert "macros/dex_unique_combination.sql" in paths
    written = (dbt_project_dir / "macros" / "dex_unique_combination.sql").read_text()
    assert "{% test dex_unique_combination(model, columns) %}" in written
    # Counts only: the test selects the duplicate count, never a key column.
    assert "select dex_duplicate_rows" in written


def test_a_macro_that_passes_a_duplicated_key_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=GRAIN_ONLY,
        extra_edits=[_planted("duckdb", PASSES_ANYWAY)],
    )

    violating = UNIQUE_COMBINATION.model("violating")
    assert report.disagreements == [
        f"dex_unique_combination on {violating}: the test should fail on the "
        "violating fixture, dbt says 'pass'"
    ]


def test_a_broken_macro_for_another_dialect_leaves_duckdb_agreeing(
    repo: Path, dbt_project_dir: Path, capsys
):
    # #1451: one dialect's macro is that dialect's job's to catch, no other's.
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=GRAIN_ONLY,
        extra_edits=[_planted("bigquery", BROKEN), _planted("snowflake", BROKEN)],
    )

    assert report.disagreements == [], report.disagreements


def test_a_dialect_the_test_does_not_support_renders_nothing_and_says_so(
    repo: Path, dbt_project_dir: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    # #1451: a kind that does not list the connector renders no test there,
    # and maintain verify alone still answers.
    shipped = next(k for k in gt.kinds() if k.name == gt.UNIQUE_COMBINATION)
    narrowed = gt.GeneratedTestKind(
        shipped.name,
        shipped.cadence,
        shipped.instances,
        macro=shipped.macro,
        dialects=frozenset({"postgres"}),
    )
    gt.unregister(shipped.name)
    gt.register(narrowed)
    try:
        report = run_parity(
            _runner(capsys), repo, dbt_project_dir, connector="duckdb", rows=GRAIN_ONLY
        )
    finally:
        gt.unregister(shipped.name)
        gt.register(shipped)

    assert report.disagreements == [], report.disagreements
    assert report.unsupported == [gt.UNIQUE_COMBINATION]
    planned = report.envelopes["plan"]["data"]
    assert planned["generated_tests"]["counts"] == {"not_rendered": 2}
    assert "macros/dex_unique_combination.sql" not in planned["paths"]


#: Counts duplicate groups rather than the rows beyond the first of each.
COUNTS_GROUPS = (
    "select count(*) as dex_duplicate_rows from (\n"
    "  select 1 as g from {{ model }} group by {{ columns | join(', ') }}\n"
    "  having count(*) > 1\n"
    ") as dex_groups having count(*) > 0"
)


def test_a_macro_that_counts_groups_not_rows_fails_on_the_count(
    repo: Path, dbt_project_dir: Path, capsys
):
    # It fails the violating fixture as it should, but says 2 where
    # maintain verify counts 3: the verdicts agree, the numbers do not.
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=GRAIN_ONLY,
        extra_edits=[_planted("duckdb", COUNTS_GROUPS)],
    )

    violating = UNIQUE_COMBINATION.model("violating")
    assert report.disagreements == [
        f"dex_unique_combination on {violating}: dbt counts 2 failure(s), "
        "maintain verify counts 3"
    ]


# --- dex_population_filter (#1453) -------------------------------------------------

POPULATION_SIGNATURE = "model, column_name, includes, excludes"

#: Treats a NULL as inside an ``includes`` list, which maintain verify does not.
NULL_IS_INSIDE = (
    "select count(*) as dex_violating_rows from {{ model }}\n"
    "where {{ column_name }} not in ("
    "{% for v in (excludes or includes) %}'{{ v }}'{% if not loop.last %}, "
    "{% endif %}{% endfor %})\n"
    "having count(*) > 0"
)


def test_the_plan_writes_the_population_filter_macro(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(POPULATION_EXCLUDES,),
    )

    assert report.disagreements == [], report.disagreements
    assert (
        "macros/dex_population_filter.sql" in report.envelopes["plan"]["data"]["paths"]
    )
    written = (dbt_project_dir / "macros" / "dex_population_filter.sql").read_text()
    # Counts only: the test selects the violating count, never the column.
    assert "select dex_violating_rows" in written


def test_a_macro_that_lets_a_null_into_an_includes_list_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys
):
    # Agrees on a value outside the list; disagrees on the NULL alone.
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(POPULATION_INCLUDES, POPULATION_INCLUDES_NULL),
        extra_edits=[
            _planted(
                "duckdb", NULL_IS_INSIDE, gt.POPULATION_FILTER, POPULATION_SIGNATURE
            )
        ],
    )

    violating = POPULATION_INCLUDES_NULL.model("violating")
    assert report.agreed == [POPULATION_INCLUDES.label]
    assert report.disagreements == [
        f"{POPULATION_INCLUDES_NULL.label} on {violating}: the test should fail on "
        "the violating fixture, dbt says 'pass'"
    ]


def test_a_refused_clean_probe_is_not_read_as_measured():
    # The violating fixture's filter was measured, so the population class is
    # not suppressed; the clean fixture's statement was refused, which only a
    # per-model warning says. An absent finding there is no verdict.
    clean = POPULATION_EXCLUDES.model("clean")
    measured = POPULATION_EXCLUDES.measured
    assert measured is not None
    verify = {
        "suppressed": {},
        "_warnings": [
            f"the declared facts on {clean} could not be measured: the warehouse "
            "refused the statement (Timeout)"
        ],
    }
    assert measured(verify, clean) is False
    assert measured(verify, POPULATION_EXCLUDES.model("violating")) is True
    assert measured({"suppressed": {"population": "x"}}, clean) is False
