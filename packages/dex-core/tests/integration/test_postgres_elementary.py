"""The Elementary fixture on Postgres, at the releases DuckDB cannot build (#1331).

DuckDB can build only package 0.23.0 and later, so the fixture's own tests
(``tests/transform/test_elementary_fixture.py``) never run the 0.15 and 0.16
series most installs pin, nor 0.20.0, the sample-flag boundary (#1446).
Postgres macros exist at all three, and the seeded container is free and
keyless, so the same variants and the same census run here. The releases are
``POSTGRES_VERSIONS``, the Postgres slots of the fixture's release matrix.

Opt-in twice over: ``DEX_TEST_PG_DSN`` for the database, like every Postgres
integration test, and ``DEX_TEST_ELEMENTARY=1`` because installing the package
needs the dbt package hub.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from transform.elementary_fixture import (
    BEHAVIOUR,
    CURRENT_VERSION,
    LEGACY_VERSIONS,
    MATERIALIZATION_LINE,
    POSTGRES_STG_ORDERS_SQL,
    POSTGRES_VERSIONS,
    SAMPLE_FLAG_VERSION,
    TEMP_TABLE_LINE,
    VARIANTS,
    PostgresTarget,
    add_failing_tests,
    dbt,
    elementary_project,
    footprint,
    postgres_census,
    requires_elementary,
    strip_sampling_switch,
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


@pytest.mark.parametrize("version", POSTGRES_VERSIONS)
@pytest.mark.parametrize("variant", VARIANTS)
def test_each_variant_builds_at_each_postgres_slot(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    variant: str,
    version: str,
):
    """Every variant parses and builds at 0.20.0, 0.16 and 0.15, and the census
    tells apart as it does on a current release. The census shows whether the
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
    [(version, variant) for version in POSTGRES_VERSIONS for variant in ("a", "e")],
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
    the package is enabled (from 0.20.0 they do, and return early); on variant
    e they stay enabled and only the monitoring tests are left out. Either way
    the dev schemas are unchanged."""

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


@pytest.mark.parametrize("version", POSTGRES_VERSIONS)
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
    [
        *((version, 2) for version in LEGACY_VERSIONS),
        (SAMPLE_FLAG_VERSION, 1),
        (CURRENT_VERSION, 1),
    ],
)
def test_the_per_test_sampling_switch_is_ignored_before_0_20(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    version: str,
    sampled: int,
):
    """``meta: {disable_test_samples: true}`` is honoured at 0.20.0 and on a
    current release, and ignored at 0.16 and 0.15 (#1331). Two tests fail
    alike, one carrying the switch. At 0.16 and 0.15 Elementary stores a sample
    of the failing rows for both; at 0.20.0 and on a current release, on the
    same warehouse, only for the one without it. So a test Dex renders with the
    switch (#1306) still has its failing rows sampled on the older releases.
    Read in source at the package's tags, the switch is already in 0.19.2's
    test materialization; the engine's warning (#1381), and this slot, draw
    the line at 0.20.0."""

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


def _with_one_duplicated_key(project: Path) -> None:
    """Give ``stg_orders`` the row for id 1 twice, so the grain's ``unique`` test
    on ``fct`` fails on exactly one key. On DuckDB the row is inserted into the
    raw table; on Postgres the staging model generates its rows, so it is
    generated twice."""

    rows = POSTGRES_STG_ORDERS_SQL.strip()
    duplicated = (
        f"select * from ({rows}) as s\nunion all\n"  # noqa: S608  (a fixed model)
        f"select * from ({rows}) as d where d.id = 1\n"
    )
    (project / "models" / "stg_orders.sql").write_text(duplicated, encoding="utf-8")


@pytest.mark.parametrize("version", [SAMPLE_FLAG_VERSION])
def test_a_failing_rendered_test_keeps_no_sample_from_the_sample_flag_boundary(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
    version: str,
):
    """The DuckDB file's #1306 case, at the sample-flag boundary (#1446), on
    variant a, where Elementary's materialization handles the tests. The
    grain's ``unique`` test as Dex renders it carries ``disable_test_samples``,
    so when it fails it adds no row to ``test_result_rows``; the same test with
    the switch removed adds one, so the census can see a sample when there is
    one. A render that lost the switch fails here, at the first release the
    engine's warning (#1381) says honours it."""

    project = elementary_project(
        tmp_path, "a", elementary_installs(version), postgres=target
    )
    _with_one_duplicated_key(project)
    _dbt(project, "run")
    rows = (target.elementary_schema, "test_result_rows", "BASE TABLE")
    run_results = project / "target" / "run_results.json"

    def sampled() -> int:
        before = postgres_census(dev_dsn)
        # Removed first, so a run that wrote no results cannot be read as the
        # previous arm's.
        run_results.unlink(missing_ok=True)
        log = _dbt(project, "test", "--select", "unique_fct_id", check=False)
        # An errored test also leaves no sample, so only a completed failure on
        # the one duplicated key measures the switch.
        [result] = [
            r
            for r in json.loads(run_results.read_text(encoding="utf-8"))["results"]
            if r["unique_id"].startswith("test.")
        ]
        assert (result["status"], result["failures"]) == ("fail", 1), (
            f"the rendered unique test is meant to fail on one key:\n{log}"
        )
        was, now = footprint(before, postgres_census(dev_dsn)).get(rows, (0, 0))
        return now - was

    assert sampled() == 0, "the test Dex rendered kept a sample of its failing rows"
    strip_sampling_switch(project)
    assert sampled() == 1, "without the switch the failing test kept no sample"


def test_declaring_a_model_warns_when_installed_elementary_ignores_sampling_flag(
    tmp_path: Path,
    elementary_installs: Callable[[str], Path],
    target: PostgresTarget,
    dev_dsn: str,
):
    """At 0.16.4 variant a, declaring a model with a single-column grain warns that
    Elementary ignores disable_test_samples; at 0.20.0, the first release the
    warning treats as honouring it (#1446), at 0.26.1 and on variant b, none
    (#1381).

    The four builds share the dev schemas, so each starts from empty ones: a
    0.16.4 build over 0.26.1's tables fails migrating them, since 0.16.4 drops a
    column a view 0.26.1 built still reads.
    """

    # Variant a at 0.16.4: Elementary's materialization runs tests and ignores flag
    project_016 = elementary_project(
        tmp_path / "a_016", "a", elementary_installs("0.16.4"), postgres=target
    )
    _dbt(project_016, "build")
    engine_016 = _engine(project_016)
    result_016 = engine_016.declare(
        ["stg_orders"], declarations=[{"model": "stg_orders", "grain": ["id"]}]
    )
    assert any("ignores disable_test_samples" in w for w in result_016.warnings)
    assert any("0.16.4" in w for w in result_016.warnings)

    # Variant a at 0.20.0, the boundary: no warning
    _empty(dev_dsn, target)
    project_020 = elementary_project(
        tmp_path / "a_020",
        "a",
        elementary_installs(SAMPLE_FLAG_VERSION),
        postgres=target,
    )
    _dbt(project_020, "build")
    result_020 = _engine(project_020).declare(
        ["stg_orders"], declarations=[{"model": "stg_orders", "grain": ["id"]}]
    )
    assert not any("disable_test_samples" in w for w in result_020.warnings)

    # Variant a at 0.26.1: Elementary honors disable_test_samples, so no warning
    _empty(dev_dsn, target)
    project_026 = elementary_project(
        tmp_path / "a_026", "a", elementary_installs(CURRENT_VERSION), postgres=target
    )
    _dbt(project_026, "build")
    engine_026 = _engine(project_026)
    result_026 = engine_026.declare(
        ["stg_orders"], declarations=[{"model": "stg_orders", "grain": ["id"]}]
    )
    assert not any("disable_test_samples" in w for w in result_026.warnings)

    # Variant b at 0.16.4: dbt's materialization runs tests, so no warning
    _empty(dev_dsn, target)
    project_b = elementary_project(
        tmp_path / "b_016", "b", elementary_installs("0.16.4"), postgres=target
    )
    _dbt(project_b, "build")
    engine_b = _engine(project_b)
    result_b = engine_b.declare(
        ["stg_orders"], declarations=[{"model": "stg_orders", "grain": ["id"]}]
    )
    assert not any("disable_test_samples" in w for w in result_b.warnings)
