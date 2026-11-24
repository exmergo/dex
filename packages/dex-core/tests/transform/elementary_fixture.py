"""A dbt project with the Elementary package installed, and a dev-schema census (#1304).

Every claim in the Elementary compatibility work (#1303) was read in source. This
is the instrument that runs them: one small project in six variants, each
changing what Elementary does in a run, and a census that shows what a command
left behind in the dev warehouse.

| Variant | Setup |
| --- | --- |
| a | ``require_explicit_package_overrides_for_builtin_materializations: false`` |
| b | that flag true, no root shim |
| c | that flag true, with a root ``test`` materialization shim |
| d | flag false, and Elementary's models disabled for the dev target |
| e | flag false, and a root model that refers to an Elementary model |
| f | flag false, and ``tests_use_temp_tables: true`` |

Variant d sets the flag false, as Elementary's install instructions do, so it
differs from variant a only in ``+enabled``: dbt still picks Elementary's
materialization, which returns at once because the package is disabled.

On DuckDB the census cannot see variant f's test-result temp tables: Elementary
creates them as ``temporary`` tables, which DuckDB drops when dbt's connection
closes, and Postgres does the same. Variant f is read from Elementary's debug
log instead. BigQuery, where those tables outlive the run, is #1331's.

DuckDB can build only package 0.23.0 and later, and most installs pin 0.15 or
0.16. Those releases run on Postgres (#1331): :func:`elementary_project` takes a
:class:`PostgresTarget`, and :func:`postgres_census` reads the same census there.

The package comes from the dbt package hub, so installing it needs the network.
The suite is otherwise offline, so everything here is opt-in: set
``DEX_TEST_ELEMENTARY=1`` and the tests that use ``requires_elementary`` run;
without it they collect as skipped with the variable named in the reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.transform.declarations import parse_declarations, render

ENV = "DEX_TEST_ELEMENTARY"

VARIANTS = ("a", "b", "c", "d", "e", "f")

#: The release nearest the source the compatibility work read (e3ab23d8, nine
#: commits past this tag) and the first release that carries DuckDB macros.
#: Releases before 0.23.0 have no ``duckdb__`` macros (read from the tags; 0.16.4
#: was run and fails ``dbt build`` on DuckDB in the package's own models), so the
#: 0.15 and 0.16 series most installs pin cannot run here.
CURRENT_VERSION = "0.26.1"
OLDEST_DUCKDB_VERSION = "0.23.0"
VERSIONS = (CURRENT_VERSION, OLDEST_DUCKDB_VERSION)

#: The last release of each series most public installs pin (the count on
#: #1312). Neither carries DuckDB macros and both carry Postgres ones, so these
#: build on Postgres only. DuckDB still parses 0.16.4, which the resolver's
#: cases use.
LEGACY_VERSIONS = ("0.16.4", "0.15.2")

#: What Elementary's own models build into: dbt's default schema naming joins the
#: target schema and the custom one.
ELEMENTARY_SCHEMA = "main_elementary"


@dataclass(frozen=True)
class PostgresTarget:
    """Where a variant builds on Postgres: the server and the role's dev schema.

    The password is never held here. The profile reads it from ``PGPASSWORD``,
    as the profile ``transform init`` renders does. ``schema`` and the
    Elementary schema dbt derives from it must both exist and be writable by
    ``user``, since a dev role cannot usually create a schema
    (``scripts/postgres_seed.sql`` creates both).
    """

    host: str
    port: int
    dbname: str
    user: str = "dbt_dev"
    schema: str = "dbt_dev"

    @property
    def elementary_schema(self) -> str:
        return f"{self.schema}_elementary"

    @property
    def schemas(self) -> tuple[str, str]:
        """The two schemas a variant writes: its own and Elementary's."""

        return (self.schema, self.elementary_schema)


FLAG = "require_explicit_package_overrides_for_builtin_materializations"

#: Printed by Elementary's ``test`` materialization, with its debug logs on, once
#: per test it handles and once more per test it routes through a temp table.
#: Releases 0.15.2 to 0.26.1 all print both.
MATERIALIZATION_LINE = "starting test materialization hook"
TEMP_TABLE_LINE = "created test temp table"

#: What each variant's row promises: whether Elementary's ``test`` materialization
#: handles the tests, and whether it routes them through temp tables. b and d read
#: the same here for different reasons (dbt picks its own on b; on d Elementary's
#: is picked and returns at once, the package being disabled), which is why the
#: build tests tell them apart by what the package builds.
BEHAVIOUR = {
    "a": (True, False),
    "b": (False, False),
    "c": (True, False),
    "d": (False, False),
    "e": (True, False),
    "f": (True, True),
}

requires_elementary = pytest.mark.skipif(
    not os.environ.get(ENV),
    reason=f"installs the Elementary package from the dbt hub; set {ENV}=1 to run",
)

MODEL_SQL = """{{ config(materialized='table') }}
select
    o.id,
    o.cid,
    o.amount,
    o.ordered_at,
    case when o.amount > 50 then 'large' else 'small' end as band
from {{ ref('stg_orders') }} o
where o.status <> 'cancelled'
"""

#: Hand-written tests: one anomaly test, one ``schema_changes`` test and one dbt
#: built-in. The fourth kind, a test Dex rendered from a declaration, is spliced
#: in by :func:`_schema_yml`.
SCHEMA_YML = """version: 2
models:
  - name: fct
    data_tests:
      - elementary.volume_anomalies:
          timestamp_column: ordered_at
      - elementary.schema_changes
    columns:
      - name: band
        data_tests:
          - accepted_values:
              values: ['large', 'small']
"""

#: The override Elementary documents for projects where dbt ignores a package's
#: materialization: a root macro that hands the test to Elementary's.
SHIM_SQL = """{% materialization test, default %}
    {{ return(elementary.materialization_test_default()) }}
{% endmaterialization %}
"""

READS_ELEMENTARY_SQL = (
    "select count(*) as invocations from {{ ref('elementary', 'dbt_invocations') }}\n"
)


def _schema_yml() -> str:
    """The schema file, with the declared grain's tests rendered by Dex itself."""

    [declaration] = parse_declarations([{"model": "fct", "grain": ["id"]}])
    return render(SCHEMA_YML, declaration)


#: The rows the DuckDB variants read from ``main.raw_orders``, generated in the
#: staging model instead: a dev role on Postgres can write only its dev schemas,
#: so there is nowhere to load a raw table.
POSTGRES_STG_ORDERS_SQL = """select
    i as id,
    (i % 5) + 1 as cid,
    (i % 90)::double precision + 1 as amount,
    case when i % 11 = 0 then 'cancelled' else 'placed' end as status,
    (current_date - (i % 20))::timestamp as ordered_at
from generate_series(0, 399) as t(i)
"""

#: Two tests that fail on ``stg_orders`` (a cancelled status is not an accepted
#: value), alike but for the per-test switch that turns Elementary's sampling of
#: failing rows off. Written by :func:`add_failing_tests`, never by
#: :func:`elementary_project`, so the variants themselves stay green.
FAILING_TESTS_YML = """version: 2
models:
  - name: stg_orders
    columns:
      - name: status
        data_tests:
          - accepted_values:
              name: sampled_status
              values: ['placed']
          - accepted_values:
              name: unsampled_status
              values: ['placed']
              config:
                meta:
                  disable_test_samples: true
"""


def add_failing_tests(project: Path) -> None:
    """Add :data:`FAILING_TESTS_YML` to a project :func:`elementary_project` wrote."""

    (project / "models" / "failing_tests.yml").write_text(
        FAILING_TESTS_YML, encoding="utf-8"
    )


#: Every environment variable Elementary reads into a ``dbt_invocations`` row.
#: On a CI runner they are set (GitHub Actions sets ``GITHUB_ACTIONS``,
#: ``GITHUB_RUN_ID``, ``GITHUB_SHA`` and the rest), so Elementary records the
#: orchestrator, the job and the commit, and columns that are NULL on any other
#: machine are filled: the reading the fixture pins would then depend on where
#: it ran. Clearing them makes every run read as a run outside an orchestrator.
#:
#: The list is read from ``macros/edr/dbt_artifacts/upload_dbt_invocation.sql``
#: (``upload_dbt_invocation``, ``get_orchestrator``, ``get_job_url`` and
#: ``get_job_run_url``), which is the same file at 0.23.0 and 0.26.1; its
#: ``get_var`` and ``get_first_env_var`` read exactly the names passed. It is a
#: list rather than a pattern such as ``GITHUB_*`` because a pattern would miss
#: ``AIRFLOW_HOME``, ``DBT_CLOUD_*`` and the unprefixed names, and would clear
#: variables Elementary does not read. A release that reads another name needs
#: it added here.
ORCHESTRATOR_ENV = (
    # get_orchestrator
    "ORCHESTRATOR",
    "DBT_ORCHESTRATOR",
    "AIRFLOW_HOME",
    "DBT_CLOUD_PROJECT_ID",
    "GITHUB_ACTIONS",
    # job_id, job_run_id, job_name
    "JOB_ID",
    "DBT_JOB_ID",
    "DBT_CLOUD_JOB_ID",
    "JOB_RUN_ID",
    "DBT_JOB_RUN_ID",
    "DBT_CLOUD_RUN_ID",
    "GITHUB_RUN_ID",
    "JOB_NAME",
    "DBT_JOB_NAME",
    # env, env_id, project_id
    "DBT_ENV",
    "DBT_ENV_ID",
    "DBT_PROJECT_ID",
    "GITHUB_REPOSITORY",
    # cause_category, cause, pull_request_id, git_sha, dbt_user, account_id
    "DBT_CAUSE_CATEGORY",
    "DBT_CLOUD_RUN_REASON_CATEGORY",
    "GITHUB_EVENT_NAME",
    "DBT_CAUSE",
    "DBT_CLOUD_RUN_REASON",
    "DBT_PULL_REQUEST_ID",
    "DBT_CLOUD_PR_ID",
    "GITHUB_HEAD_REF",
    "DBT_GIT_SHA",
    "DBT_CLOUD_GIT_SHA",
    "GITHUB_SHA",
    "DBT_USER",
    "DBT_ACCOUNT_ID",
    # get_job_url, get_job_run_url
    "JOB_URL",
    "DBT_JOB_URL",
    "JOB_RUN_URL",
    "DBT_JOB_RUN_URL",
    "AIRFLOW_URL",
    "DBT_CLOUD_ACCOUNT_ID",
    "DBT_CLOUD_DOMAIN",
    "GITHUB_SERVER_URL",
)


def without_orchestrator(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` with every name in :data:`ORCHESTRATOR_ENV` removed."""

    return {k: v for k, v in environ.items() if k not in ORCHESTRATOR_ENV}


@pytest.fixture
def no_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear :data:`ORCHESTRATOR_ENV` for the test, so the dbt that Dex itself
    starts (``engine.build``) sees no orchestrator either. :func:`dbt` clears
    them on its own, which covers a session fixture set up before this runs."""

    for name in ORCHESTRATOR_ENV:
        monkeypatch.delenv(name, raising=False)


def dbt(*argv: str, cwd: Path, check: bool = True, stderr: bool = True) -> str:
    """Run dbt itself, outside Dex, and return what it printed.

    ``check=False`` also returns the output of a run that exited nonzero, for a
    command that is meant to fail (``dbt test`` over a failing test).
    ``stderr=False`` returns stdout alone, for output read line by line as data
    (``dbt ls``): an interpreter warning dbt's dependencies print on stderr,
    such as pydantic's on Python 3.14, is not a line of it.

    dbt runs with :data:`ORCHESTRATOR_ENV` cleared, so what Elementary records
    of the run is the same on a CI runner as anywhere else.
    """

    executable = shutil.which("dbt")
    if executable is None:
        if os.environ.get(ENV):
            pytest.fail(f"{ENV} is set but no dbt executable is on PATH")
        pytest.skip("needs the dbt executable on PATH")
    done = subprocess.run(  # noqa: S603
        [executable, *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=without_orchestrator(os.environ),
    )
    if check and done.returncode:
        raise RuntimeError(f"dbt {' '.join(argv)} failed:\n{done.stdout}{done.stderr}")
    return done.stdout + (done.stderr if stderr else "")


@pytest.fixture(scope="session")
def elementary_installs(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[str], Path]:
    """``install(version)``: the package at ``version``, installed once a session."""

    cache: dict[str, Path] = {}

    def install(version: str) -> Path:
        if version not in cache:
            root = tmp_path_factory.mktemp(f"elementary-{version}") / "installer"
            cache[version] = install_elementary(root, version)
        return cache[version]

    return install


@pytest.fixture(scope="session")
def installed_elementary(elementary_installs: Callable[[str], Path]) -> Path:
    """The current package, for :func:`elementary_project`."""

    return elementary_installs(CURRENT_VERSION)


def install_elementary(root: Path, version: str = CURRENT_VERSION) -> Path:
    """Install the package once under ``root`` and return the installing project.

    The returned directory holds ``packages.yml``, ``package-lock.yml`` and
    ``dbt_packages/``, which :func:`elementary_project` copies into each variant,
    so a session pays for one download per version.
    """

    root.mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text(
        'name: installer\nversion: "1.0.0"\nconfig-version: 2\nprofile: shop\n',
        encoding="utf-8",
    )
    package = {"package": "elementary-data/elementary", "version": version}
    (root / "packages.yml").write_text(
        yaml.safe_dump({"packages": [package]}), encoding="utf-8"
    )
    (root / "profiles.yml").write_text(_profiles_yml(root / "unused.duckdb"), "utf-8")
    dbt("deps", "--project-dir", str(root), "--profiles-dir", str(root), cwd=root)
    return root


def _profiles_yml(warehouse: Path) -> str:
    # Two targets, so a resolver can be asked about one the project enables the
    # package on. Nothing here ever builds `prod`; its file is never created.
    prod = warehouse.with_name("prod.duckdb")
    return (
        "shop:\n  target: dev\n  outputs:\n"
        f"    dev:\n      type: duckdb\n      path: {warehouse.as_posix()}\n"
        f"    prod:\n      type: duckdb\n      path: {prod.as_posix()}\n"
    )


def _postgres_profiles_yml(target: PostgresTarget) -> str:
    # The same two targets as on DuckDB. `prod` names a schema nothing creates,
    # and listing it opens no connection.
    def output(schema: str) -> dict:
        return {
            "type": "postgres",
            "host": target.host,
            "port": target.port,
            "user": target.user,
            "password": "{{ env_var('PGPASSWORD', '') }}",
            "dbname": target.dbname,
            "schema": schema,
            "threads": 1,
        }

    outputs = {"dev": output(target.schema), "prod": output("prod")}
    return yaml.safe_dump(
        {"shop": {"target": "dev", "outputs": outputs}}, sort_keys=False
    )


def _dbt_project(variant: str) -> dict:
    project: dict = {
        "name": "shop",
        "version": "1.0.0",
        "config-version": 2,
        "profile": "shop",
        "model-paths": ["models"],
        "macro-paths": ["macros"],
        "models": {"elementary": {"+schema": "elementary"}},
    }
    if variant in {"a", "d", "e", "f"}:
        project["flags"] = {FLAG: False}
    elif variant in {"b", "c"}:
        project["flags"] = {FLAG: True}
    if variant == "d":
        project["models"]["elementary"]["+enabled"] = "{{ target.name != 'dev' }}"
    if variant == "f":
        project["vars"] = {"tests_use_temp_tables": True}
    return project


def _raw_orders(tmp_path: Path) -> Path:
    """Create the DuckDB warehouse with its one raw table and return its path."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "dev.duckdb"
    con = duckdb.connect(str(warehouse))
    # Timestamps relative to today: an anomaly test reads a trailing window, so
    # fixed dates would age out of it.
    con.execute(
        "create or replace table main.raw_orders as "
        "select i as id, (i % 5) + 1 as cid, (i % 90)::double + 1 as amount, "
        "case when i % 11 = 0 then 'cancelled' else 'placed' end as status, "
        "(current_date - (i % 20)::integer)::timestamp as ordered_at "
        "from range(400) t(i)"
    )
    con.close()
    return warehouse


def elementary_project(
    tmp_path: Path,
    variant: str,
    installed: Path,
    postgres: PostgresTarget | None = None,
) -> Path:
    """Write variant ``variant`` under ``tmp_path`` and return the project directory.

    The warehouse is ``tmp_path / "dev.duckdb"`` and holds only the raw table; the
    project is parsed and built by whoever asks. ``installed`` is what
    :func:`install_elementary` returned. With ``postgres`` the same variant is
    written against that server instead: nothing is loaded, the staging model
    generates the rows the raw table would hold, and the models build into
    ``postgres.schema``.
    """

    if variant not in VARIANTS:
        raise ValueError(f"no variant {variant!r}; choose from {VARIANTS}")
    if postgres is None:
        profiles = _profiles_yml(_raw_orders(tmp_path))
        stg_orders = "select id, cid, amount, status, ordered_at from main.raw_orders\n"
    else:
        profiles = _postgres_profiles_yml(postgres)
        stg_orders = POSTGRES_STG_ORDERS_SQL

    project = tmp_path / "analytics"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        yaml.safe_dump(_dbt_project(variant), sort_keys=False), encoding="utf-8"
    )
    (project / "profiles.yml").write_text(profiles, encoding="utf-8")
    (project / "models" / "stg_orders.sql").write_text(stg_orders, encoding="utf-8")
    (project / "models" / "fct.sql").write_text(MODEL_SQL, encoding="utf-8")
    (project / "models" / "schema.yml").write_text(_schema_yml(), encoding="utf-8")
    if variant == "c":
        (project / "macros" / "elementary_test_materialization.sql").write_text(
            SHIM_SQL, encoding="utf-8"
        )
    if variant == "e":
        (project / "models" / "elementary_runs.sql").write_text(
            READS_ELEMENTARY_SQL, encoding="utf-8"
        )
    for name in ("packages.yml", "package-lock.yml"):
        shutil.copy(installed / name, project / name)
    shutil.copytree(installed / "dbt_packages", project / "dbt_packages")
    return project


# --- the census -----------------------------------------------------------------

#: One relation: schema, name and kind (``BASE TABLE``, ``VIEW``, ...).
Relation = tuple[str, str, str]

#: ``execute(sql)`` returning rows, which is all the census asks of a warehouse.
Query = Callable[[str], Iterable[tuple]]

_SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")

#: What :func:`footprint` reports for the side a relation is missing from, so a
#: dropped relation never reads like one that is still there but uncountable.
ABSENT = "absent"


def census(query: Query) -> dict[Relation, int | None]:
    """Every relation in the current database with its row count.

    Reads an unqualified ``information_schema.tables`` and counts through
    double-quoted identifiers, which DuckDB and Postgres accept as written. It
    reads one catalog and keys relations by schema and name only. BigQuery, which
    needs a qualified ``INFORMATION_SCHEMA`` and backtick quoting, would need its
    own listing. A relation that cannot be counted (a view whose parent is gone)
    is kept with ``None``, because a census that raises there hides exactly the
    damage it is for.
    """

    counts: dict[Relation, int | None] = {}
    rows = query(
        "select table_schema, table_name, table_type from information_schema.tables"
    )
    for schema, name, kind in rows:
        if schema.lower() in _SYSTEM_SCHEMAS:
            continue
        # A name may hold a double quote itself, written doubled inside the quotes.
        relation = ".".join(
            '"' + part.replace('"', '""') + '"' for part in (schema, name)
        )
        try:
            [(count,)] = query(f"select count(*) from {relation}")  # noqa: S608
        except Exception:
            # Whatever the driver raised, the relation is there and uncountable.
            count = None
        counts[(schema, name, kind)] = count
    return counts


def duckdb_census(warehouse: Path) -> dict[Relation, int | None]:
    """:func:`census` over a DuckDB file, opened read-only and closed again."""

    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return census(lambda sql: con.execute(sql).fetchall())
    finally:
        con.close()


def postgres_census(dsn: str) -> dict[Relation, int | None]:
    """:func:`census` over a Postgres database, as the role ``dsn`` names.

    It lists what that role may see, so connect as the role that builds: a
    read-only role granted nothing in the dev schemas finds them empty. Each
    statement is its own transaction, because one failed count would otherwise
    abort the rest and read every later relation as uncountable.
    """

    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn, autocommit=True) as con:
        return census(lambda sql: con.execute(sql).fetchall())


#: One side of a footprint entry: a row count, ``None`` for a relation that is
#: there and uncountable, or :data:`ABSENT`.
Reading = int | str | None


def footprint(
    before: dict[Relation, int | None], after: dict[Relation, int | None]
) -> dict[Relation, tuple[Reading, Reading]]:
    """What changed between two censuses: ``{relation: (before, after)}``.

    A relation only one side has is reported against :data:`ABSENT` on the other,
    so a created relation reads ``(ABSENT, n)``, a dropped one ``(n, ABSENT)``,
    and one that is still there but can no longer be counted ``(n, None)``. An
    empty footprint means the command left the warehouse as it found it.
    """

    changed: dict[Relation, tuple[Reading, Reading]] = {}
    for relation in sorted(set(before) | set(after)):
        # `.get` keeps a present `None` (uncountable) apart from a missing key.
        was = before.get(relation, ABSENT)
        now = after.get(relation, ABSENT)
        if was != now:
            changed[relation] = (was, now)
    return changed
