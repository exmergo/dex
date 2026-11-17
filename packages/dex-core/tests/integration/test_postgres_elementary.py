"""The Elementary fixture on Postgres, at the releases most installs pin (#1331).

DuckDB can build only package 0.23.0 and later, so the fixture's own tests
(``tests/transform/test_elementary_fixture.py``) never run the 0.15 and 0.16
series. Postgres macros exist at both, and the seeded container is free and
keyless, so the same variants and the same census run here.

Opt-in twice over: ``DEX_TEST_PG_DSN`` for the database, like every Postgres
integration test, and ``DEX_TEST_ELEMENTARY=1`` because installing the package
needs the dbt package hub.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from transform.elementary_fixture import (
    BEHAVIOUR,
    CURRENT_VERSION,
    LEGACY_VERSIONS,
    MATERIALIZATION_LINE,
    TEMP_TABLE_LINE,
    VARIANTS,
    PostgresTarget,
    add_failing_tests,
    dbt,
    elementary_project,
    footprint,
    postgres_census,
    requires_elementary,
)
from transform.elementary_fixture import elementary_installs as elementary_installs

from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.storage import FilesystemStore

from .test_postgres_transform import dev_dsn as dev_dsn

pytestmark = [pytest.mark.integration, pytest.mark.postgres, requires_elementary]

#: Database-seconds: far above what a build or a mutation batch of this project
#: takes, so the ceiling never decides a test.
BUDGET = 300


@pytest.fixture
def target(dev_dsn: str) -> Iterator[PostgresTarget]:
    """The seeded database as the dev role, with both dev schemas empty.

    Every test here builds into the two schemas the seed gives ``dbt_dev``, and
    Elementary's own tables differ by release. So they are emptied before each
    test, and again after it for the suites that share the database.
    """

    from psycopg.conninfo import conninfo_to_dict

    fields = conninfo_to_dict(dev_dsn)
    target = PostgresTarget(
        host=str(fields["host"]),
        port=int(fields.get("port") or 5432),
        dbname=str(fields["dbname"]),
    )
    _empty(dev_dsn, target)
    yield target
    _empty(dev_dsn, target)


def _empty(dsn: str, target: PostgresTarget) -> None:
    """Drop every relation in the dev schemas, as the role that owns them."""

    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as con:
        relations = con.execute(
            "select table_schema, table_name, table_type "
            "from information_schema.tables where table_schema = any(%s)",
            (list(target.schemas),),
        ).fetchall()
        for schema, name, kind in relations:
            con.execute(
                sql.SQL("drop {} if exists {} cascade").format(
                    sql.SQL("view" if kind == "VIEW" else "table"),
                    sql.Identifier(schema, name),
                )
            )


def _engine(project: Path) -> DexEngine:
    return DexEngine(
        connector="postgres",
        repo_root=str(project.parent),
        store=FilesystemStore(project.parent),
        config=DexConfig(
            connector="postgres",
            dbt_target="dev",
            dbt_project_dir=project.name,
            postgres={"dev_schema": "dbt_dev"},
            budget={"ceiling": BUDGET, "session_ceiling_declined": True},
        ),
        budget=BUDGET,
        confirmed=True,
    )


def _dbt(project: Path, *argv: str, check: bool = True) -> str:
    here = ("--project-dir", str(project), "--profiles-dir", str(project))
    return dbt(*argv, *here, "--target", "dev", cwd=project, check=check)


@pytest.mark.parametrize("version", LEGACY_VERSIONS)
@pytest.mark.parametrize("variant", VARIANTS)
def test_each_variant_builds_at_the_releases_most_installs_pin(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    variant: str,
    version: str,
):
    """Every variant parses and builds at 0.16 and 0.15, and the census tells
    apart as it does on a current release. The census shows whether the
    package's models built (not on d, which disables them for dev), and
    Elementary's debug log shows whether its ``test`` materialization handled
    the tests and routed them through temp tables. The census cannot stand in
    for the log: an anomaly test stores its own result rows, so variant b fills
    ``test_result_rows`` too, with dbt's materialization running the tests."""

    project = elementary_project(
        tmp_path, variant, elementary_installs(version), postgres=target
    )

    _dbt(project, "build")

    built = postgres_census(dev_dsn)
    assert (target.schema, "fct", "BASE TABLE") in built, (
        "the project's own mart did not build"
    )
    schemas = {schema for schema, _, _ in built}
    assert (target.elementary_schema in schemas) is (variant != "d")

    log = _dbt(project, "test", "--vars", "{debug_logs: true}")
    assert (MATERIALIZATION_LINE in log, TEMP_TABLE_LINE in log) == BEHAVIOUR[variant]


@pytest.mark.parametrize(
    ("version", "variant"),
    [(version, variant) for version in LEGACY_VERSIONS for variant in ("a", "e")],
)
def test_a_mutation_run_leaves_the_dev_schemas_as_it_found_them(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    version: str,
    variant: str,
):
    """The holding arm (#1307) at the releases DuckDB cannot build. On variant a
    the copy disables the package's models, which at 0.15 and 0.16 is also what
    keeps its hooks from running, since those releases' hooks do not check that
    the package is enabled; on variant e they stay enabled and only the
    monitoring tests are left out. Either way the dev schemas are unchanged."""

    project = elementary_project(
        tmp_path, variant, elementary_installs(version), postgres=target
    )
    engine = _engine(project)
    engine.build(target="dev")

    before = postgres_census(dev_dsn)
    result = engine.test_mutations("fct")

    assert result.counts["not_run"] == 0, "the budget stopped the run short"
    assert result.counts["generated"] >= 1, "no mutant ran, so nothing was measured"
    assert footprint(before, postgres_census(dev_dsn)) == {}
    codes = {e.get("reason_code") for e in result.baseline["excluded"]} - {None}
    assert codes == {"elementary_history" if variant == "e" else "elementary_disabled"}


@pytest.mark.parametrize("version", LEGACY_VERSIONS)
def test_without_the_fix_a_mutation_run_leaves_rows_in_the_dev_elementary_schema(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    version: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """The failing arm: with the copy left as it was before #1307, variant a
    leaves an invocation behind for its baseline and for every mutant, as it
    does on DuckDB at 0.23.0 and 0.26.1, so the census can see what the holding
    arm finds absent."""

    from exmergo_dex_core.transform import commands
    from exmergo_dex_core.transform.build import ShadowRun

    monkeypatch.setattr(ShadowRun, "strip_run_hooks", lambda self: [])
    monkeypatch.setattr(commands, "_quiet_elementary", lambda shadow: ([], []))
    project = elementary_project(
        tmp_path, "a", elementary_installs(version), postgres=target
    )
    engine = _engine(project)
    engine.build(target="dev")

    before = postgres_census(dev_dsn)
    result = engine.test_mutations("fct")
    change = footprint(before, postgres_census(dev_dsn))

    assert result.counts["not_run"] == 0, "the budget stopped the run short"
    assert {schema for schema, _, _ in change} == {target.elementary_schema}
    was, now = change[(target.elementary_schema, "dbt_invocations", "BASE TABLE")]
    assert now - was == result.counts["generated"] + 1


@pytest.mark.parametrize(
    ("version", "sampled"),
    [*((version, 2) for version in LEGACY_VERSIONS), (CURRENT_VERSION, 1)],
)
def test_the_per_test_sampling_switch_is_ignored_before_0_20(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    version: str,
    sampled: int,
):
    """``meta: {disable_test_samples: true}`` first appears in package 0.20.0
    (read in source on #1331; this runs it). Two tests fail alike, one carrying
    the switch. At 0.16 and 0.15 Elementary stores a sample of the failing rows
    for both; on a current release, on the same warehouse, only for the one
    without it. So a test Dex renders with the switch (#1306) still has its
    failing rows sampled on the older releases."""

    project = elementary_project(
        tmp_path, "a", elementary_installs(version), postgres=target
    )
    _dbt(project, "build")
    add_failing_tests(project)

    before = postgres_census(dev_dsn)
    log = _dbt(project, "test", "--select", "stg_orders", check=False)
    change = footprint(before, postgres_census(dev_dsn))

    assert "ERROR=2" in log, f"both tests are meant to fail:\n{log}"
    was, now = change[(target.elementary_schema, "test_result_rows", "BASE TABLE")]
    assert now - was == sampled
