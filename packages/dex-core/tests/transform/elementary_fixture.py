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
| d | Elementary's models disabled for the dev target, the flag left unset |
| e | flag false, and a root model that refers to an Elementary model |
| f | flag false, and ``tests_use_temp_tables: true`` |

The package comes from the dbt package hub, so installing it needs the network.
The suite is otherwise offline, so everything here is opt-in: set
``DEX_TEST_ELEMENTARY=1`` and the tests that use ``requires_elementary`` run;
without it they collect as skipped with the variable named in the reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.transform.declarations import parse_declarations, render

ENV = "DEX_TEST_ELEMENTARY"

VARIANTS = ("a", "b", "c", "d", "e", "f")

#: The release the compatibility work read (its source reading was nine commits
#: past this tag) and the first release that carries DuckDB macros. Releases
#: before 0.23.0 have no ``duckdb__`` macros and fail ``dbt build`` on DuckDB in
#: the package's own models, so the 0.15 and 0.16 series most installs pin
#: cannot run here.
CURRENT_VERSION = "0.26.1"
OLDEST_DUCKDB_VERSION = "0.23.0"
VERSIONS = (CURRENT_VERSION, OLDEST_DUCKDB_VERSION)

#: What Elementary's own models build into: dbt's default schema naming joins the
#: target schema and the custom one.
ELEMENTARY_SCHEMA = "main_elementary"

FLAG = "require_explicit_package_overrides_for_builtin_materializations"

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


def dbt(*argv: str, cwd: Path) -> str:
    """Run dbt itself, outside Dex, and return what it printed."""

    executable = shutil.which("dbt")
    if executable is None:
        pytest.skip("needs the dbt executable on PATH")
    done = subprocess.run(  # noqa: S603
        [executable, *argv], cwd=cwd, capture_output=True, text=True, check=False
    )
    if done.returncode:
        raise RuntimeError(f"dbt {' '.join(argv)} failed:\n{done.stdout}{done.stderr}")
    return done.stdout + done.stderr


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
    if variant in {"a", "e", "f"}:
        project["flags"] = {FLAG: False}
    elif variant in {"b", "c"}:
        project["flags"] = {FLAG: True}
    if variant == "d":
        project["models"]["elementary"]["+enabled"] = "{{ target.name != 'dev' }}"
    if variant == "f":
        project["vars"] = {"tests_use_temp_tables": True}
    return project


def elementary_project(tmp_path: Path, variant: str, installed: Path) -> Path:
    """Write variant ``variant`` under ``tmp_path`` and return the project directory.

    The warehouse is ``tmp_path / "dev.duckdb"`` and holds only the raw table; the
    project is parsed and built by whoever asks. ``installed`` is what
    :func:`install_elementary` returned.
    """

    if variant not in VARIANTS:
        raise ValueError(f"no variant {variant!r}; choose from {VARIANTS}")
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

    project = tmp_path / "analytics"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        yaml.safe_dump(_dbt_project(variant), sort_keys=False), encoding="utf-8"
    )
    (project / "profiles.yml").write_text(_profiles_yml(warehouse), encoding="utf-8")
    (project / "models" / "stg_orders.sql").write_text(
        "select id, cid, amount, status, ordered_at from main.raw_orders\n",
        encoding="utf-8",
    )
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


def census(query: Query) -> dict[Relation, int | None]:
    """Every relation in the warehouse with its row count.

    Reads ``information_schema.tables``, so it counts tables and views alike and
    runs unchanged on any warehouse that has one. A relation that cannot be
    counted (a view whose parent is gone) is kept with ``None``, because a census
    that raises there hides exactly the damage it is for.
    """

    counts: dict[Relation, int | None] = {}
    rows = query(
        "select table_schema, table_name, table_type from information_schema.tables"
    )
    for schema, name, kind in rows:
        if schema.lower() in _SYSTEM_SCHEMAS:
            continue
        try:
            [(count,)] = query(f'select count(*) from "{schema}"."{name}"')  # noqa: S608
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


def footprint(
    before: dict[Relation, int | None], after: dict[Relation, int | None]
) -> dict[Relation, tuple[int | None, int | None]]:
    """What changed between two censuses: ``{relation: (before, after)}``.

    A relation only one side has is reported against ``None`` on the other, so a
    created relation, a dropped one and a row-count change all read the same way.
    An empty footprint means the command left the warehouse as it found it.
    """

    missing = object()
    changed: dict[Relation, tuple[int | None, int | None]] = {}
    for relation in sorted(set(before) | set(after)):
        was, now = before.get(relation, missing), after.get(relation, missing)
        if was != now:
            changed[relation] = (
                None if was is missing else was,  # type: ignore[assignment]
                None if now is missing else now,  # type: ignore[assignment]
            )
    return changed
