"""The demo's Elementary history: seven weekly runs, and the two tables they fill.

Part 1 of #1477 gave the demo one dbt build, and one build is all dbt's run
results hold, so the incident it shows has no known start. This module seeds the
history Elementary would have recorded around that build: ``dbt_invocations``
and ``elementary_test_results``, with the two other tables the recorded layout
lists built empty, in the schema the demo's dbt project names for the package
(``main_elementary``), at the release the project records (0.26.1).
``dex maintain incidents`` then reads the incident from the ``elementary``
source, with the run it was first observed in.

**The runs.** Seven, a week apart, the newest being the build whose
``run_results.json`` the project holds (same invocation). Oldest first:

- four builds in which every test passes;
- one with the package's hooks off, which Elementary records nothing of, so the
  coverage timeline reads that week as a missed run rather than as a pass;
- two in which ``unique_stg_order_items_order_item_id`` fails, because the batch
  of ``order_items`` the warehouse loads twice arrived in between.

So the break sits between the last pass and the first failure, across the
unobserved week, and diagnosis bisects exactly there.

**Dated from when the demo was generated, not from** :data:`~.warehouse._ANCHOR`.
The incident fold judges an open incident stale once its source has not observed
it for three of its usual gaps, against the time it is called, so history dated
from a fixed day would read as long over. Weekly runs keep the incident open for
about three weeks after ``dex demo``. The clock is not read here: the caller
passes ``generated_at``, every time in these rows is a fixed offset back from it,
and a test pins the rows built at a fixed instant, so the offsets are pinned
while the dates move with the day the demo was made.

**The layout is the reader's.** Each table is built from the columns the
reader's recorded layout (:mod:`..maintain.elementary_layouts`) lists for the
release, in their order and with the DuckDB type the package casts each one to.
A test checks the demo's tables with the reader's own layout check, so the demo
never shows a table the real reader would refuse.

No sampled row is seeded: the project sets ``test_sample_row_count: 0``, as Dex's
own install does, so ``result_rows`` is NULL. No value of a shop row appears
anywhere here, only counts.

Pure: no I/O, no clock. The warehouse module writes these rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..maintain.elementary_layouts import layout_for
from .warehouse import DemoTable, _uuid_shaped

#: The release the demo's project records as installed: the newest the reader
#: supports (``transform.elementary_install.INSTALL_RELEASE``).
RELEASE = "0.26.1"

#: Where dbt-duckdb builds the package's models: the target schema, ``main``,
#: then the ``+schema: elementary`` the project sets.
SCHEMA = "main_elementary"

#: How the package casts each layout type name on DuckDB (``empty_column``).
_DUCKDB_TYPES = {
    "boolean": "BOOLEAN",
    "timestamp": "TIMESTAMP",
    "int": "INTEGER",
    "bigint": "BIGINT",
    "float": "DOUBLE",
    "long_string": "VARCHAR",
    "string": "VARCHAR",
}

_WEEK = timedelta(days=7)
_BUILD = timedelta(seconds=42)
#: The newest run started this long before the demo was generated.
_NEWEST_BEFORE = timedelta(minutes=10)

Outcome = Literal["pass", "fail"]

#: The schedule, oldest first: (weeks before the newest run, hooks on, the
#: outcome of the failing test). Every other test passes in every run.
_SCHEDULE: tuple[tuple[int, bool, Outcome], ...] = (
    (6, True, "pass"),
    (5, True, "pass"),
    (4, True, "pass"),
    (3, True, "pass"),
    (2, False, "pass"),
    (1, True, "fail"),
    (0, True, "fail"),
)


@dataclass(frozen=True)
class SeededRun:
    """One ``dbt build`` of the demo's project, as the history holds it."""

    invocation_id: str
    started_at: datetime
    completed_at: datetime
    #: False for the run with the package's hooks off: dbt ran, and the
    #: package recorded nothing of it.
    hooks: bool
    #: How the failing test came out in this run.
    outcome: Outcome


def invocation_id(weeks_back: int) -> str:
    """The derived id of the run ``weeks_back`` weeks before the newest; the
    newest is the build ``run_results.json`` holds."""

    return _uuid_shaped(f"dex-demo-dbt-build-{weeks_back + 1}")


def seeded_runs(
    generated_at: datetime,
    schedule: Sequence[tuple[int, bool, Outcome]] = _SCHEDULE,
) -> tuple[SeededRun, ...]:
    """The runs, oldest first, dated back from ``generated_at``.

    ``schedule`` stands in for the seeded one, so a test can build the same
    demo with a run removed.
    """

    newest = generated_at.astimezone(UTC).replace(microsecond=0) - _NEWEST_BEFORE
    return tuple(
        SeededRun(
            invocation_id=invocation_id(weeks),
            started_at=newest - weeks * _WEEK,
            completed_at=newest - weeks * _WEEK + _BUILD,
            hooks=hooks,
            outcome=outcome,
        )
        for weeks, hooks, outcome in schedule
    )


@dataclass(frozen=True)
class SeededTest:
    """One test of the demo's project, as Elementary records it."""

    unique_id: str
    model: str
    column: str
    short_name: str


def _text(moment: datetime) -> str:
    """How the package writes a run's times into ``dbt_invocations`` at this
    release: as text, in UTC, with no zone."""

    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _naive(moment: datetime) -> datetime:
    """A ``TIMESTAMP`` cell: UTC with the zone dropped, as DuckDB stores it."""

    return moment.astimezone(UTC).replace(tzinfo=None)


def _row(table: str, values: dict[str, Any]) -> tuple:
    columns = [name for name, _ in layout_for(RELEASE).tables[table].columns]
    unknown = set(values) - set(columns)
    if unknown:
        raise ValueError(f"{table} has no column {sorted(unknown)}")
    return tuple(values.get(name) for name in columns)


def _columns(table: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, _DUCKDB_TYPES[kind])
        for name, kind in layout_for(RELEASE).tables[table].columns
    )


def elementary_tables(
    runs: Sequence[SeededRun],
    tests: Sequence[SeededTest],
    *,
    failing_test: str,
    failures: int,
    database: str,
    project: str,
    dbt_version: str,
) -> tuple[DemoTable, ...]:
    """Every table the recorded layout lists, for ``runs``.

    ``dbt_invocations`` and ``elementary_test_results`` hold the runs; a run
    with the hooks off writes no row to either. ``failures`` is what the
    failing test reported in a run it failed in. ``test_result_rows`` and
    ``data_monitoring_metrics`` are built empty: with samples off and no
    monitor declared, the package's run leaves them so. They are seeded so the
    package's schema looks as a real install leaves it, which ``explore`` reads
    as the package's and leaves out of the tour's ranking.
    """

    invocations: list[tuple] = []
    results: list[tuple] = []
    for run in runs:
        if not run.hooks:
            continue
        invocations.append(
            _row(
                "dbt_invocations",
                {
                    "invocation_id": run.invocation_id,
                    "run_started_at": _text(run.started_at),
                    "run_completed_at": _text(run.completed_at),
                    "generated_at": _text(run.completed_at),
                    "created_at": _naive(run.completed_at + timedelta(seconds=3)),
                    "command": "build",
                    "dbt_version": dbt_version,
                    "elementary_version": RELEASE,
                    "full_refresh": False,
                    "invocation_vars": "{}",
                    "vars": "{}",
                    "target_name": "dev",
                    "target_database": database,
                    "target_schema": "main",
                    "target_profile_name": project,
                    "threads": 1,
                    "selected": "[]",
                    "project_name": project,
                },
            )
        )
        for offset, test in enumerate(tests, start=1):
            failed = test.unique_id == failing_test and run.outcome == "fail"
            detected = run.started_at + timedelta(seconds=5 * offset)
            results.append(
                _row(
                    "elementary_test_results",
                    {
                        "id": _uuid_shaped(f"{test.unique_id}.{run.invocation_id}"),
                        "test_execution_id": f"{test.unique_id}.{run.invocation_id}",
                        "test_unique_id": test.unique_id,
                        "model_unique_id": f"model.{project}.{test.model}",
                        "invocation_id": run.invocation_id,
                        "detected_at": _naive(detected),
                        "created_at": _naive(run.completed_at),
                        "database_name": database,
                        "schema_name": "main",
                        "table_name": test.model,
                        "column_name": test.column,
                        "test_type": "dbt_test",
                        "test_sub_type": "generic",
                        "test_results_description": (
                            f"Got {failures} results, configured to fail if != 0"
                            if failed
                            else None
                        ),
                        "owners": "[]",
                        "tags": "[]",
                        "test_name": test.short_name,
                        "test_params": (
                            f'{{"column_name": "{test.column}", "model": '
                            f"\"{{{{ get_where_subquery(ref('{test.model}')) }}}}\"}}"
                        ),
                        "severity": "ERROR",
                        "status": "fail" if failed else "pass",
                        "failures": failures if failed else 0,
                        "test_short_name": test.short_name,
                        "test_alias": test.unique_id.split(".")[2],
                    },
                )
            )
    return (
        DemoTable(
            name="dbt_invocations",
            columns=_columns("dbt_invocations"),
            rows=tuple(invocations),
            note=(
                "Elementary's record of the project's dbt runs: one row per weekly "
                "build, and none for the week its hooks were off"
            ),
            schema=SCHEMA,
        ),
        DemoTable(
            name="elementary_test_results",
            columns=_columns("elementary_test_results"),
            rows=tuple(results),
            note=(
                "Elementary's record of each test in each run: "
                "unique_stg_order_items_order_item_id passes, then fails once the "
                "batch loaded twice arrives"
            ),
            schema=SCHEMA,
        ),
        DemoTable(
            name="test_result_rows",
            columns=_columns("test_result_rows"),
            rows=(),
            note=(
                "Elementary's samples of failing rows: empty, because the "
                "project turns samples off"
            ),
            schema=SCHEMA,
        ),
        DemoTable(
            name="data_monitoring_metrics",
            columns=_columns("data_monitoring_metrics"),
            rows=(),
            note="Elementary's monitor metrics: empty, as no monitor is declared",
            schema=SCHEMA,
        ),
    )


__all__ = [
    "RELEASE",
    "SCHEMA",
    "SeededRun",
    "SeededTest",
    "elementary_tables",
    "invocation_id",
    "seeded_runs",
]
