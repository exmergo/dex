"""Calendar windows measured end to end on real dbt and DuckDB (#1112).

A trailing seven-day window written as ``RANGE BETWEEN INTERVAL '6 day'
PRECEDING`` had no mutation site, so no run could say whether its tests pin the
boundary. These tests plant the two boundary defects (one day wider, one day
narrower) and check both directions the issue asks for: a weak unit test on
consecutive days, all inside the window, lets both through, and a test with
rows on the last included day, the first excluded day and across a gap in the
dates catches both. The strong fixture also has to tell the day window from a
row window, which is what the gap is for.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .mutation_helpers import _engine

MODEL_SQL = """select
    depot,
    pickup_date,
    sum(parcels) over (
        partition by depot
        order by pickup_date
        range between interval '6 day' preceding and current row
    ) as parcels_7d
from {{ ref('stg_pickups') }}
"""

ROWS_VARIANT_SQL = MODEL_SQL.replace(
    "range between interval '6 day' preceding", "rows between 6 preceding"
)

# Consecutive days, all inside the window: every row's trailing sum is the
# running total whatever the frame's width, so neither boundary is reached.
WEAK_TEST = """
unit_tests:
  - name: trailing_window_weak
    model: fct_depot_trailing
    given:
      - input: ref('stg_pickups')
        rows:
          - {depot: 'north', pickup_date: '2024-03-01', parcels: 1}
          - {depot: 'north', pickup_date: '2024-03-02', parcels: 10}
          - {depot: 'north', pickup_date: '2024-03-03', parcels: 100}
    expect:
      rows:
        - {depot: 'north', pickup_date: '2024-03-01', parcels_7d: 1}
        - {depot: 'north', pickup_date: '2024-03-02', parcels_7d: 11}
        - {depot: 'north', pickup_date: '2024-03-03', parcels_7d: 111}
"""

# 03-01 is exactly six days before 03-07 (the last day its window includes)
# and seven before 03-08 (the first it excludes). The gap between 03-01 and
# 03-04 means a six-row window would still reach 03-01 from 03-08.
STRONG_TEST = """
unit_tests:
  - name: trailing_window_boundary
    model: fct_depot_trailing
    given:
      - input: ref('stg_pickups')
        rows:
          - {depot: 'north', pickup_date: '2024-03-01', parcels: 1}
          - {depot: 'north', pickup_date: '2024-03-04', parcels: 10}
          - {depot: 'north', pickup_date: '2024-03-07', parcels: 100}
          - {depot: 'north', pickup_date: '2024-03-08', parcels: 1000}
    expect:
      rows:
        - {depot: 'north', pickup_date: '2024-03-01', parcels_7d: 1}
        - {depot: 'north', pickup_date: '2024-03-04', parcels_7d: 11}
        - {depot: 'north', pickup_date: '2024-03-07', parcels_7d: 111}
        - {depot: 'north', pickup_date: '2024-03-08', parcels_7d: 1110}
"""


@pytest.fixture
def window_project(tmp_path: Path, duckdb_file: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("dbt.adapters.duckdb")
    warehouse = tmp_path / "dev.duckdb"
    shutil.copy(duckdb_file, warehouse)
    con = duckdb.connect(str(warehouse))
    con.execute(
        "create or replace table main.raw_pickups as select * from (values "
        "('north', date '2024-01-01', 3), ('north', date '2024-01-05', 4), "
        "('north', date '2024-01-12', 5), ('south', date '2024-01-02', 6)) "
        "t(depot, pickup_date, parcels)"
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
    (project / "models" / "stg_pickups.sql").write_text(
        "select depot, pickup_date, parcels from main.raw_pickups", encoding="utf-8"
    )
    (project / "models" / "fct_depot_trailing.sql").write_text(
        MODEL_SQL, encoding="utf-8"
    )
    return project


def _write_suite(project: Path, unit_tests: str) -> None:
    (project / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: fct_depot_trailing\n    columns:\n"
        "      - name: depot\n        data_tests: [not_null]\n" + unit_tests,
        encoding="utf-8",
    )


def _frame_mutants(result) -> list[dict]:
    return [m for m in result.mutants if m["operator"] == "window_frame"]


def test_a_weak_calendar_fixture_lets_both_boundary_defects_through(
    window_project: Path,
):
    _write_suite(window_project, WEAK_TEST)
    engine = _engine(window_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct_depot_trailing")

    frames = _frame_mutants(result)
    assert [m["frame"]["to"] for m in sorted(frames, key=lambda m: m["id"])] == [7, 5]
    assert {m["status"] for m in frames} == {"survived"}, frames
    for mutant in frames:
        # The survivor says which two days a fixture has to put rows on.
        assert "6 days before the current row" in mutant["suggested_test"]
        assert "gap" in mutant["suggested_test"]
        assert mutant["equivalence"]["status"] in {"equivalent", "distinguishable"}
    assert result.unsupported_sites == []


def test_a_boundary_fixture_with_a_date_gap_catches_both(window_project: Path):
    _write_suite(window_project, STRONG_TEST)
    engine = _engine(window_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct_depot_trailing")

    frames = _frame_mutants(result)
    assert len(frames) == 2
    assert {m["status"] for m in frames} == {"killed"}, frames
    for mutant in frames:
        assert "trailing_window_boundary" in " ".join(mutant["caught_by"])


def test_the_boundary_fixture_also_tells_a_day_window_from_a_row_window(
    window_project: Path,
):
    """The gap in the dates is what makes a six-row window answer differently
    from a six-day one. The weak fixture cannot see that substitution at all."""

    pytest.importorskip("dbt.cli.main")
    from dbt.cli.main import dbtRunner

    (window_project / "models" / "fct_depot_trailing.sql").write_text(
        ROWS_VARIANT_SQL, encoding="utf-8"
    )

    def unit_test_status(suite: str) -> str:
        _write_suite(window_project, suite)
        outcome = dbtRunner().invoke(
            [
                "build",
                "--project-dir",
                str(window_project),
                "--profiles-dir",
                str(window_project),
            ]
        )
        statuses = {
            r.node.name: str(r.status) for r in (outcome.result or []) if r.node
        }
        found = [v for k, v in statuses.items() if k.startswith("trailing_window")]
        assert len(found) == 1, statuses
        return found[0]

    assert unit_test_status(WEAK_TEST) == "pass"
    assert unit_test_status(STRONG_TEST) == "fail"


def test_a_bound_dex_cannot_shift_is_reported_on_the_envelope(window_project: Path):
    """A window whose boundary nothing can move is named on the result with a
    warning, rather than leaving a run that reads as if the frame were tested."""

    (window_project / "models" / "fct_depot_trailing.sql").write_text(
        MODEL_SQL.replace("interval '6 day'", "interval '6 days 12 hours'").replace(
            "sum(parcels)", "sum(parcels) filter (where parcels > 0)"
        ),
        encoding="utf-8",
    )
    _write_suite(window_project, STRONG_TEST)
    engine = _engine(window_project)
    engine.build(target="dev")

    result = engine.test_mutations("fct_depot_trailing")

    assert _frame_mutants(result) == []
    assert [s["reason_code"] for s in result.unsupported_sites] == ["compound_interval"]
    assert any("window frame bound(s) were not mutated" in w for w in result.warnings)
    assert result.data()["unsupported_sites"] == result.unsupported_sites
