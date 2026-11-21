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
    JOIN_COVERAGE_FILTERED,
    JOIN_COVERAGE_INNER,
    JOIN_COVERAGE_LEFT,
    JOIN_COVERAGE_NO_MATCH,
    PARENT_MISSING,
    PARENT_MISSING_FILTERED,
    PARENT_MISSING_UNAPPLIED,
    PARENT_UNEXPECTED,
    POPULATION_EXCLUDES,
    POPULATION_INCLUDES,
    POPULATION_INCLUDES_NULL,
    RELATIONSHIPS_LEFT_JOIN,
    ROWS,
    UNIQUE_COMBINATION,
    UNIT_FRACTION,
    UNIT_PERCENT,
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
    assert report.agreed == [row.label for row in ROWS if row.withheld is None]
    assert report.unsupported == []
    assert report.withheld == [UNIT_PERCENT.label, JOIN_COVERAGE_LEFT.label]


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


# --- dex_parent_population (#1454) -------------------------------------------------

PARENT_ROWS = (
    PARENT_MISSING,
    PARENT_UNEXPECTED,
    PARENT_MISSING_FILTERED,
    PARENT_MISSING_UNAPPLIED,
)
PARENT_SIGNATURE = "model, parent, key, direction, filters"

#: Counts the model's rows the parent lacks, not its distinct keys: a key the
#: model holds twice is counted twice, where maintain verify counts it once.
COUNTS_ROWS = (
    "{% set k = (key.keys() | list)[0] %}{% set pk = (key.values() | list)[0] %}\n"
    "select count(*) as dex_violating_keys from {{ model }} m\n"
    "where m.{{ k }} is not null and m.{{ k }} not in "
    "(select {{ pk }} from {{ parent }} where {{ pk }} is not null)\n"
    "{% if direction == 'missing' %}and 1 = 0{% endif %}\n"
    "having count(*) > 0"
)

#: Ignores the declaration's filters: every parent key is owed.
IGNORES_FILTERS = (
    "{% set k = (key.keys() | list)[0] %}{% set pk = (key.values() | list)[0] %}\n"
    "select count(*) as dex_violating_keys from (select distinct {{ pk }} as k "
    "from {{ parent }} where {{ pk }} is not null) p\n"
    "where p.k not in (select {{ k }} from {{ model }} where {{ k }} is not null)\n"
    "{% if direction == 'unexpected' %}and 1 = 0{% endif %}\n"
    "having count(*) > 0"
)


def test_the_parent_rows_agree_and_the_plan_writes_their_macro(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys), repo, dbt_project_dir, connector="duckdb", rows=PARENT_ROWS
    )

    assert report.disagreements == [], report.disagreements
    assert report.agreed == [row.label for row in PARENT_ROWS]
    plan = report.envelopes["plan"]["data"]
    assert "macros/dex_parent_population.sql" in plan["paths"]
    mine = [
        t for t in plan["generated_tests"]["tests"] if t["test"] == gt.PARENT_POPULATION
    ]
    # Two directions on each of eight declared fixtures.
    assert len(mine) == 16
    assert {t["cadence"] for t in mine} == {gt.SCHEDULED}
    written = (dbt_project_dir / "macros" / "dex_parent_population.sql").read_text()
    # Counts only: the test selects the violating count, never a key.
    assert "select dex_violating_keys" in written


def test_a_macro_that_counts_rows_not_keys_fails_on_the_count(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(PARENT_UNEXPECTED,),
        extra_edits=[
            _planted("duckdb", COUNTS_ROWS, gt.PARENT_POPULATION, PARENT_SIGNATURE)
        ],
    )

    violating = PARENT_UNEXPECTED.model("violating")
    assert report.disagreements == [
        f"{PARENT_UNEXPECTED.label} on {violating}: dbt counts 3 failure(s), "
        "maintain verify counts 2"
    ]


def test_a_macro_that_ignores_the_filters_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys
):
    # Without the filter the churned parent is owed, so the clean fixture,
    # which leaves it out as the declaration says, fails.
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(PARENT_MISSING, PARENT_MISSING_FILTERED),
        extra_edits=[
            _planted("duckdb", IGNORES_FILTERS, gt.PARENT_POPULATION, PARENT_SIGNATURE)
        ],
    )

    clean = PARENT_MISSING_FILTERED.model("clean")
    assert report.agreed == [PARENT_MISSING.label]
    assert (
        f"{PARENT_MISSING_FILTERED.label} on {clean}: the test should pass on the "
        "clean fixture, dbt says 'fail'"
    ) in report.disagreements


def test_a_left_join_read_as_optional_despite_its_references_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    """#1459: the reading before the fix, a `references` with no
    `foreign_key` role leaving the LEFT join optional, is a disagreement the
    harness names: dbt's `relationships` fails and verify raises nothing."""

    import dataclasses

    from exmergo_dex_core.maintain import verify as verify_mod

    real = verify_mod.join_policy

    def optional(candidate, ctx):
        policy = real(candidate, ctx)
        if policy.required_by and "declares references" in policy.required_by:
            return dataclasses.replace(policy, required_by=None)
        return policy

    monkeypatch.setattr(verify_mod, "join_policy", optional)
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(RELATIONSHIPS_LEFT_JOIN,),
    )

    violating = RELATIONSHIPS_LEFT_JOIN.model("violating")
    assert report.agreed == []
    assert report.disagreements == [
        f"{RELATIONSHIPS_LEFT_JOIN.label} on {violating}: maintain verify raised "
        f"none of {sorted(RELATIONSHIPS_LEFT_JOIN.codes)} on the violating fixture"
    ]


# --- dex_unit_range (#1455) ----------------------------------------------------------

UNIT_ROWS = (UNIT_FRACTION, UNIT_PERCENT)

#: Checks only the upper bound: a negative share passes.
ONLY_ABOVE_ONE = (
    "select count(*) as dex_violating_rows from {{ model }} "
    "where {{ column_name }} > 1 having count(*) > 0"
)


def test_the_unit_rows_agree_and_a_percent_renders_nothing(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys), repo, dbt_project_dir, connector="duckdb", rows=UNIT_ROWS
    )

    assert report.disagreements == [], report.disagreements
    assert report.agreed == [UNIT_FRACTION.label]
    assert report.withheld == [UNIT_PERCENT.label]
    plan = report.envelopes["plan"]["data"]
    assert "macros/dex_unit_range.sql" in plan["paths"]
    rows = sorted(
        (t["model"], t["status"], t.get("reason"))
        for t in plan["generated_tests"]["tests"]
        if t["test"] == gt.UNIT_RANGE
    )
    assert rows == sorted(
        [
            (UNIT_FRACTION.model("violating"), "added", None),
            (UNIT_FRACTION.model("clean"), "added", None),
            (UNIT_PERCENT.model("violating"), "not_rendered", gt.HEURISTIC_ONLY),
            (UNIT_PERCENT.model("clean"), "not_rendered", gt.HEURISTIC_ONLY),
        ]
    )
    written = (dbt_project_dir / "macros" / "dex_unit_range.sql").read_text()
    assert "select dex_violating_rows" in written


def test_a_macro_missing_the_lower_bound_fails_on_the_count(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(UNIT_FRACTION,),
        extra_edits=[
            _planted("duckdb", ONLY_ABOVE_ONE, gt.UNIT_RANGE, "model, column_name")
        ],
    )

    violating = UNIT_FRACTION.model("violating")
    assert report.disagreements == [
        f"{UNIT_FRACTION.label} on {violating}: dbt counts 1 failure(s), "
        "maintain verify counts 2"
    ]


# --- dex_join_coverage (#1456) -------------------------------------------------------

JOIN_ROWS = (
    JOIN_COVERAGE_INNER,
    JOIN_COVERAGE_NO_MATCH,
    JOIN_COVERAGE_FILTERED,
    JOIN_COVERAGE_LEFT,
)
JOIN_SIGNATURE = (
    "model, child, child_columns, parent, parent_columns, child_where, "
    "parent_where, null_keys"
)

#: Reads the model's output, as `relationships` does: an INNER join has already
#: dropped the unmatched rows there, so it passes the violating fixture.
READS_THE_OUTPUT = (
    "select count(*) as dex_unmatched_rows from {{ model }} m "
    "where m.{{ child_columns[0] }} is not null and m.{{ child_columns[0] }} "
    "not in (select {{ parent_columns[0] }} from {{ parent }} "
    "where {{ parent_columns[0] }} is not null) having count(*) > 0"
)
#: Reads the driving input whole, ignoring the filter the model applies to it.
IGNORES_THE_FILTER = (
    "select count(*) as dex_unmatched_rows from {{ child }} c "
    "where c.{{ child_columns[0] }} is not null and c.{{ child_columns[0] }} "
    "not in (select {{ parent_columns[0] }} from {{ parent }} "
    "where {{ parent_columns[0] }} is not null) having count(*) > 0"
)


def test_the_join_rows_agree_and_a_left_join_renders_nothing(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys), repo, dbt_project_dir, connector="duckdb", rows=JOIN_ROWS
    )

    assert report.disagreements == [], report.disagreements
    assert report.agreed == [
        JOIN_COVERAGE_INNER.label,
        JOIN_COVERAGE_NO_MATCH.label,
        JOIN_COVERAGE_FILTERED.label,
    ]
    assert report.withheld == [JOIN_COVERAGE_LEFT.label]
    plan = report.envelopes["plan"]["data"]
    assert "macros/dex_join_coverage.sql" in plan["paths"]
    rows = sorted(
        (t["model"], t["status"], t.get("reason"))
        for t in plan["generated_tests"]["tests"]
        if t["test"] == gt.JOIN_COVERAGE
    )
    assert rows == sorted(
        [
            *(
                (row.model(side), "added", None)
                for row in JOIN_ROWS[:3]
                for side in ("violating", "clean")
            ),
            (
                JOIN_COVERAGE_LEFT.model("violating"),
                "not_rendered",
                "covered_by_builtin",
            ),
            (JOIN_COVERAGE_LEFT.model("clean"), "not_rendered", "covered_by_builtin"),
        ]
    )
    written = (dbt_project_dir / "macros" / "dex_join_coverage.sql").read_text()
    assert "select dex_unmatched_rows" in written


def test_a_macro_reading_the_output_misses_the_inner_join_and_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(JOIN_COVERAGE_INNER,),
        extra_edits=[
            _planted("duckdb", READS_THE_OUTPUT, gt.JOIN_COVERAGE, JOIN_SIGNATURE)
        ],
    )

    violating = JOIN_COVERAGE_INNER.model("violating")
    assert report.disagreements == [
        f"{JOIN_COVERAGE_INNER.label} on {violating}: the test should fail on the "
        "violating fixture, dbt says 'pass'"
    ]


def test_a_macro_ignoring_the_filter_fails_the_harness(
    repo: Path, dbt_project_dir: Path, capsys
):
    report = run_parity(
        _runner(capsys),
        repo,
        dbt_project_dir,
        connector="duckdb",
        rows=(JOIN_COVERAGE_FILTERED,),
        extra_edits=[
            _planted("duckdb", IGNORES_THE_FILTER, gt.JOIN_COVERAGE, JOIN_SIGNATURE)
        ],
    )

    label = JOIN_COVERAGE_FILTERED.label
    violating = JOIN_COVERAGE_FILTERED.model("violating")
    clean = JOIN_COVERAGE_FILTERED.model("clean")
    assert report.disagreements == [
        f"{label} on {violating}: dbt counts 2 failure(s), maintain verify counts 1",
        f"{label} on {clean}: the test should pass on the clean fixture, dbt "
        "says 'fail'",
    ]
