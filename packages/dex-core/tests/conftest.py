"""Shared fixtures. DuckDB is the test engine: in-process, free, fast."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import ci_groups
import pytest

_importorskip = pytest.importorskip


def _importorskip_absent_only(modname: str, *args, **kwargs):
    """Skip only when the module asked for is absent, never when it is broken.

    A test gated on an optional extra skips where the extra is not installed,
    and that is the whole of what a skip may mean. pytest skips on any
    ModuleNotFoundError, though, including one raised for a module the
    requested one imports: an installed dbt whose own dependency did not
    install, or that cannot import under a new interpreter, reads as "extra
    absent" and turns a compatibility failure into a green run (#795). So a
    skip stands only when the missing module is the requested one or a parent
    package of it; anything else is re-raised as the failure it is.
    """

    try:
        return _importorskip(modname, *args, **kwargs)
    except pytest.skip.Exception as skipped:
        try:
            __import__(modname)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if modname == missing or modname.startswith(missing + "."):
                raise skipped from None
            raise ImportError(
                f"{modname} is installed but cannot import: {missing} is missing"
            ) from exc
        raise


pytest.importorskip = _importorskip_absent_only


# --- CI groups (tests/ci_groups.py) -------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ci groups")
    group.addoption(
        "--ci-group",
        choices=sorted(ci_groups.GROUPS),
        help="run only the test files tests/ci_groups.py assigns to this group",
    )
    group.addoption(
        "--ci-group-report",
        metavar="PATH",
        help=(
            "write what was collected, selected and run as JSON, for "
            "scripts/check_test_groups.py to prove the groups cover the suite"
        ),
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    group = config.getoption("--ci-group")
    config.stash[_COLLECTED] = [item.nodeid for item in items]
    if group is None:
        return
    root = config.rootpath
    keep, drop = [], []
    for item in items:
        path = item.path.relative_to(root).as_posix()
        (keep if ci_groups.group_of(path) == group else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
    config.stash[_SELECTED] = [item.nodeid for item in keep]


def pytest_runtest_logfinish(nodeid: str) -> None:
    _RAN.append(nodeid)


def pytest_sessionstart(session: pytest.Session) -> None:
    _DEX_BEFORE.update(path for path in _CHECKOUT_DEX if path.exists())


def pytest_sessionfinish(session: pytest.Session) -> None:
    # After every test and fixture has finished, so a write no single test's
    # teardown saw (a session finalizer, an atexit hook) still fails the run.
    leaked = _leaked_dex()
    if leaked:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        sys.stderr.write(f"\n{_leak_message('the test session', leaked)}\n")
    config = session.config
    report = config.getoption("--ci-group-report")
    if not report:
        return
    collected = config.stash.get(_COLLECTED, [])
    Path(report).write_text(
        json.dumps(
            {
                "group": config.getoption("--ci-group"),
                "python": "{}.{}".format(*sys.version_info[:2]),
                "collected": collected,
                "selected": config.stash.get(_SELECTED, collected),
                "ran": _RAN,
            }
        ),
        encoding="utf-8",
    )


_COLLECTED = pytest.StashKey[list[str]]()
_SELECTED = pytest.StashKey[list[str]]()
_RAN: list[str] = []


# --- no test writes a `.dex/` into the checkout (#990) ------------------------
#
# A command whose store resolves against the working directory instead of the
# test's ``tmp_path`` leaves a cache behind that `git add -A` commits and a later
# `dex` run reads as real. A `.dex/` already there when the session started is a
# developer's own state and none of the suite's business.

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_CHECKOUT_DEX = [_PACKAGE_ROOT / ".dex", _PACKAGE_ROOT.parent.parent / ".dex"]
_DEX_BEFORE: set[Path] = set()


def _leaked_dex() -> list[Path]:
    """The `.dex/` directories written into the checkout since the session
    started, removed so the next check does not blame someone else for them."""

    leaked = [p for p in _CHECKOUT_DEX if p not in _DEX_BEFORE and p.exists()]
    for path in leaked:
        shutil.rmtree(path, ignore_errors=True)
    return leaked


def _leak_message(culprit: str, leaked: list[Path]) -> str:
    return (
        f"{culprit} wrote {', '.join(str(p) for p in leaked)} into the checkout "
        "(#990); give its engine or CLI call a tmp_path (repo_root, a store, or "
        "monkeypatch.chdir)"
    )


@pytest.fixture(autouse=True)
def _no_dex_in_checkout():
    """Fail the test that wrote a `.dex/` into the checkout. Checked per test
    so the failure names the test that leaked it."""

    yield
    leaked = _leaked_dex()
    if leaked:
        pytest.fail(_leak_message("this test", leaked))


@pytest.fixture
def duckdb_file(tmp_path: Path) -> Path:
    """A real on-disk DuckDB file with one tiny table.

    Created via a writable connection in the fixture, then handed to the engine,
    which must open it read-only. duckdb is required (the [duckdb] extra); the
    test is skipped if it is not installed so the rest of the suite still runs.
    """

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, email VARCHAR)")
    conn.execute(
        "INSERT INTO customers VALUES (1, 'a@example.com'), (2, 'b@example.com')"
    )
    # A child table so relationship inference has a customer_id -> customers.id
    # foreign key to find, and a numeric column with a safe min/max.
    conn.execute(
        "CREATE TABLE orders (id INTEGER, customer_id INTEGER, total DECIMAL(10,2))"
    )
    conn.execute("INSERT INTO orders VALUES (1, 1, 9.99), (2, 1, 5.00), (3, 2, 12.50)")
    conn.close()
    return path


@pytest.fixture
def symlinks(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Skip a test that needs a symlink where the platform will not make one.

    Windows refuses `os.symlink` to a process without the symlink privilege
    unless Developer Mode is on (WinError 1314). The probe links in a directory
    of its own rather than catching the test's own link failing, because a
    `FileExistsError` there is an `OSError` too and a broken test must not read
    as an unsupported platform. Everywhere else, and on Windows with Developer
    Mode, the test runs.
    """

    probe = tmp_path_factory.mktemp("symlink-probe")
    try:
        (probe / "link").symlink_to(probe / "target")
    except OSError:
        pytest.skip(
            "this platform refuses symlinks without a privilege, so the "
            "confinement check's symlink case is not exercised here"
        )


@pytest.fixture
def fake_bq_client():
    """The standard populated fake BigQuery client (see tests/fakes/bigquery.py).

    Requires the real google-cloud-bigquery library (the [bigquery] extra) for
    its SchemaField and exception types; skipped when absent so the rest of the
    suite still runs, but note that CI and the release gate install the extra,
    so the BigQuery safety families do run everywhere that matters.
    """

    bigquery = pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    tables = [
        FakeTable(
            project="test-proj",
            dataset_id="shop",
            table_id="customers",
            schema=[
                bigquery.SchemaField("id", "INTEGER", mode="REQUIRED"),
                bigquery.SchemaField("email", "STRING"),
            ],
            num_rows=100,
            num_bytes=5_000,
        ),
        FakeTable(
            project="test-proj",
            dataset_id="shop",
            table_id="events",
            schema=[
                bigquery.SchemaField("id", "INTEGER"),
                bigquery.SchemaField(
                    "payload",
                    "RECORD",
                    fields=[bigquery.SchemaField("tags", "STRING", mode="REPEATED")],
                ),
                bigquery.SchemaField("labels", "STRING", mode="REPEATED"),
            ],
            num_rows=1_000,
            num_bytes=50_000,
        ),
        FakeTable(
            project="test-proj",
            dataset_id="logs",
            table_id="requests",
            schema=[bigquery.SchemaField("day", "DATE")],
            num_rows=10_000,
            num_bytes=1_000_000,
            require_partition_filter=True,
        ),
    ]
    return FakeBigQueryClient(project="test-proj", tables=tables)


@pytest.fixture
def fake_sf_connection():
    """The standard populated fake Snowflake connection (see
    tests/fakes/snowflake.py).

    Requires the real snowflake-connector-python library (the [snowflake]
    extra) for its error types; skipped when absent, but CI and the release
    gate install the extra, so the Snowflake safety families run everywhere
    that matters. The DEX_WH warehouse starts SUSPENDED with a 60-second
    AUTO_SUSPEND, so a billed command resumes it and owes the resume minimum and
    the idle tail by default; tests that want a warm warehouse flip its state.
    """

    pytest.importorskip("snowflake.connector")
    from fakes.snowflake import (
        FakeSnowflakeConnection,
        FakeSnowflakeTable,
        FakeWarehouse,
    )

    tables = [
        FakeSnowflakeTable(
            database="SHOP",
            schema="PUBLIC",
            name="CUSTOMERS",
            columns=[("ID", "FIXED", False), ("EMAIL", "TEXT", True)],
            rows=100,
            bytes=5_000_000_000,  # 5 GB -> a non-trivial seconds estimate
        ),
        FakeSnowflakeTable(
            database="SHOP",
            schema="PUBLIC",
            name="EVENTS",
            columns=[
                ("ID", "FIXED", True),
                ("PAYLOAD", "VARIANT", True),
                ("LABELS", "ARRAY", True),
            ],
            rows=1_000,
            bytes=50_000_000_000,  # 50 GB
        ),
    ]
    return FakeSnowflakeConnection(
        tables=tables,
        warehouses=[FakeWarehouse(name="DEX_WH", size="X-Small", state="SUSPENDED")],
    )


@pytest.fixture
def fake_databricks():
    """The standard populated fake Databricks pair (see
    tests/fakes/databricks.py): a Unity Catalog workspace client and a DBAPI
    connection behind a counting connect factory.

    Requires the real databricks-sql-connector library (the [databricks]
    extra) for its error types; skipped when absent, but CI and the release
    gate install the extra, so the Databricks safety families run everywhere
    that matters. The warehouse starts STOPPED so startup-floor behavior is on
    by default; tests that want a running warehouse flip its state (and the
    connection's ``startup_pending``).
    """

    pytest.importorskip("databricks.sql")
    from fakes.databricks import (
        FakeDatabricks,
        FakeDatabricksConnection,
        FakeDatabricksTable,
        FakeWarehouse,
        FakeWorkspaceClient,
    )

    tables = [
        FakeDatabricksTable(
            catalog="shop",
            schema="core",
            name="customers",
            columns=[("id", "bigint", False), ("email", "string", True)],
            rows=100,
            bytes=5_000_000_000,  # 5 GB -> a non-trivial refined estimate
        ),
        FakeDatabricksTable(
            catalog="shop",
            schema="core",
            name="events",
            columns=[
                ("id", "bigint", True),
                ("payload", "struct<a:int,b:string>", True),
                ("labels", "array<string>", True),
            ],
            rows=1_000,
            bytes=50_000_000_000,  # 50 GB
        ),
    ]
    workspace = FakeWorkspaceClient(
        tables=tables, warehouse=FakeWarehouse(id="fake-wh", state="STOPPED")
    )
    connection = FakeDatabricksConnection(tables=tables)
    return FakeDatabricks(workspace=workspace, connection=connection, tables=tables)


@pytest.fixture
def fake_pg_connection():
    """The standard populated fake Postgres connection (see
    tests/fakes/postgres.py).

    Requires the real psycopg library (the [postgres] extra) for its error
    types; skipped when absent, but CI and the release gate install the extra,
    so the Postgres safety families run everywhere that matters. ``customers``
    carries fresh planner statistics; ``events`` has never been analyzed
    (reltuples -1, no pg_stats rows) so the missing-stats degradations are on
    by default.
    """

    pytest.importorskip("psycopg")
    from fakes.postgres import FakePostgresConnection, FakePostgresTable

    tables = [
        FakePostgresTable(
            schema="shop",
            name="customers",
            columns=[
                ("id", "bigint", False),
                ("email", "text", True),
                ("payload", "jsonb", True),
                ("tags", "text[]", True),
            ],
            reltuples=100.0,
            total_bytes=5_000_000_000,  # 5 GB -> a non-trivial seconds estimate
            stats={"id": -1.0, "email": 90.0},
        ),
        FakePostgresTable(
            schema="shop",
            name="events",
            columns=[("id", "bigint", True), ("payload", "jsonb", True)],
            reltuples=-1.0,
            total_bytes=50_000_000_000,  # 50 GB
        ),
    ]
    return FakePostgresConnection(tables=tables, database="dexdb")


@pytest.fixture
def fake_clickhouse_connection(monkeypatch):
    """The standard populated fake ClickHouse client (see
    tests/fakes/clickhouse.py).

    Requires the real clickhouse-connect library (the [clickhouse] extra) for
    its error types; skipped when absent, but CI and the release gate install
    the extra, so the ClickHouse safety families run everywhere that matters.

    The shape mirrors the other connectors' fixtures (a small ``customers``, a
    large ``events``) and adds the two things only ClickHouse has: a
    ``LowCardinality(Nullable(String))`` column, so the type unwrapper is
    exercised in both nesting orders by default, and a ``ReplacingMergeTree``
    table, so the engine note that keeps a duplicate count from reading as a
    grain defect is on by default rather than opt-in.
    """

    pytest.importorskip("clickhouse_connect")
    from fakes.clickhouse import (
        FakeClickHouseConnection,
        FakeClickHouseTable,
        FakeGrant,
    )

    from exmergo_dex_core.adapters.clickhouse import ClickHouseAdapter

    tables = [
        FakeClickHouseTable(
            database="shop",
            name="customers",
            columns=[
                ("id", "UInt64", True),
                ("email", "String", False),
                ("city", "LowCardinality(Nullable(String))", False),
                ("tags", "Array(String)", False),
                ("created_at", "DateTime", False),
            ],
            total_rows=100,
            total_bytes=5_000_000_000,  # 5 GB -> a non-trivial seconds estimate
        ),
        FakeClickHouseTable(
            database="shop",
            name="events",
            columns=[
                ("id", "UInt64", True),
                ("payload", "String", False),
                ("occurred_at", "Date", False),
            ],
            total_rows=1000,
            total_bytes=50_000_000_000,  # 50 GB
        ),
        FakeClickHouseTable(
            database="shop",
            name="order_events_raw",
            columns=[("order_id", "UInt64", True), ("state", "String", False)],
            total_rows=300,
            total_bytes=1_000_000,
            engine="ReplacingMergeTree",
            primary_key="order_id",
        ),
    ]
    grants = [
        FakeGrant(user_name="dbt_dev", access_type="SELECT", database="shop"),
        FakeGrant(user_name="dbt_dev", access_type="CREATE", database="dbt_dev"),
        FakeGrant(user_name="dbt_dev", access_type="INSERT", database="dbt_dev"),
        FakeGrant(user_name="dbt_dev", access_type="SELECT", database="dbt_dev"),
    ]
    connection = FakeClickHouseConnection(tables=tables, grants=grants)
    # The Cloud capacity probe waits between re-reads; here the waits are
    # recorded on the fake instead of slept through.
    monkeypatch.setattr(ClickHouseAdapter, "_sleep", connection.slept.append)
    return connection


@pytest.fixture
def fake_trino_connection():
    """The standard populated fake Trino connection (see tests/fakes/trino.py).

    Requires the real trino client (the [trino] extra) for its error types;
    skipped when absent, but CI and the release gate install the extra, so the
    Trino safety families run everywhere that matters.

    The shape mirrors the other connectors' fixtures (a small ``customers``, a
    large ``events``) across the things only Trino has: several catalogs of
    different kinds on one cluster, a nested ``array(varchar)`` column and a
    ``char(10)`` one, a view, and a federated ``postgresql`` catalog whose
    table has no statistics, so the floor estimate is on by default rather
    than opt-in.
    """

    pytest.importorskip("trino")
    from fakes.trino import FakeTrinoConnection, FakeTrinoTable

    tables = [
        FakeTrinoTable(
            catalog="memory",
            schema="shop",
            name="customers",
            columns=[
                ("id", "bigint", True),
                ("email", "varchar", True),
                ("zip", "char(10)", True),
                ("tags", "array(varchar)", True),
                ("created_at", "timestamp(3)", True),
            ],
            estimate_bytes=5_000_000_000,  # 5 GB -> a non-trivial seconds estimate
        ),
        FakeTrinoTable(
            catalog="memory",
            schema="shop",
            name="events",
            columns=[
                ("id", "bigint", True),
                ("payload", "varchar", True),
                ("occurred_at", "date", True),
            ],
            estimate_bytes=50_000_000_000,  # 50 GB
        ),
        FakeTrinoTable(
            catalog="memory",
            schema="shop",
            name="v_totals",
            columns=[("customer_id", "bigint", True), ("total", "double", True)],
            estimate_bytes=1_000_000,
            table_type="VIEW",
        ),
        FakeTrinoTable(
            catalog="pg",
            schema="public",
            name="accounts",
            columns=[("id", "integer", False), ("owner", "varchar(64)", True)],
            estimate_bytes=None,
        ),
    ]
    catalogs = {
        "memory": "memory",
        "pg": "postgresql",
        "lake": "iceberg",
        "tpch": "tpch",
        "system": "system",
    }
    return FakeTrinoConnection(
        tables=tables, catalogs=catalogs, empty_schemas={"lake": ["dbt_dev"]}
    )


def write_manifest(
    project: Path,
    *,
    models: dict[str, str | None],
    sources: dict[str, str] | None = None,
    relationship_tests: list[tuple[str, str, str, str]] = (),
    unique_tests: list[tuple[str, str]] = (),
    not_null_tests: list[tuple[str, str]] = (),
    composite_unique_tests: list[tuple[str, list[str]]] = (),
    generated_at: str | None = None,
) -> Path:
    """Write a minimal compiled manifest.json shaped like dbt's.

    ``models`` maps model name to its relation_name (None for ephemeral).
    ``sources`` maps ``"source_name.table"`` to a relation_name.
    ``relationship_tests`` entries are ``(model, column, to, field)`` with
    ``to`` as the literal test argument (``"ref('x')"`` / ``"source('a','b')"``).
    ``unique_tests`` / ``not_null_tests`` entries are ``(model, column)``.
    ``composite_unique_tests`` entries are ``(model, columns)``, compiled as a
    model-level ``unique_combination_of_columns`` test node (no ``column_name``
    kwarg at all -- both the dbt-core built-in and the dbt_utils macro compile
    to this same shape).
    """

    uid_of = {name: f"model.dex_test.{name}" for name in models}
    nodes: dict[str, dict] = {
        uid_of[name]: {
            "resource_type": "model",
            "name": name,
            "relation_name": relation,
            "config": {"enabled": True},
        }
        for name, relation in models.items()
    }
    source_nodes: dict[str, dict] = {}
    for key, relation in (sources or {}).items():
        source_name, table = key.split(".", 1)
        uid = f"source.dex_test.{source_name}.{table}"
        uid_of[key] = uid
        source_nodes[uid] = {
            "resource_type": "source",
            "source_name": source_name,
            "name": table,
            "relation_name": relation,
        }

    def test_node(name: str, kwargs: dict, attached: str) -> dict:
        return {
            "resource_type": "test",
            "name": name,
            "test_metadata": {"name": name.split("__")[0], "kwargs": kwargs},
            "attached_node": uid_of[attached],
            "depends_on": {"nodes": [uid_of[attached]]},
        }

    for i, (model, column, to, field) in enumerate(relationship_tests):
        nodes[f"test.dex_test.relationships_{i}"] = test_node(
            f"relationships__{i}",
            {"column_name": column, "to": to, "field": field},
            model,
        )
    for i, (model, column) in enumerate(unique_tests):
        nodes[f"test.dex_test.unique_{i}"] = test_node(
            f"unique__{i}", {"column_name": column}, model
        )
    for i, (model, column) in enumerate(not_null_tests):
        nodes[f"test.dex_test.not_null_{i}"] = test_node(
            f"not_null__{i}", {"column_name": column}, model
        )
    for i, (model, columns) in enumerate(composite_unique_tests):
        nodes[f"test.dex_test.unique_combination_of_columns_{i}"] = test_node(
            f"unique_combination_of_columns__{i}",
            {"combination_of_columns": list(columns)},
            model,
        )

    target = project / "target"
    target.mkdir(exist_ok=True)
    path = target / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "project_name": "dex_test",
                    "generated_at": generated_at or datetime.now(UTC).isoformat(),
                },
                "nodes": nodes,
                "sources": source_nodes,
            }
        ),
        encoding="utf-8",
    )
    return path


def write_semantic_manifest(
    project: Path, *, semantic_models: list[dict], metrics: list[dict]
) -> Path:
    """Write a minimal target/semantic_manifest.json (dbt's compiled semantic
    layer artifact: pre-resolved node_relation and metric input_measures)."""

    target = project / "target"
    target.mkdir(exist_ok=True)
    path = target / "semantic_manifest.json"
    path.write_text(
        json.dumps(
            {
                "semantic_models": semantic_models,
                "metrics": metrics,
                "project_configuration": {},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_redshift_connection():
    """The standard populated fake Redshift connection (see
    tests/fakes/redshift.py).

    Requires the real redshift_connector library (the [redshift] extra) for
    its error types; skipped when absent, but CI and the release gate install
    the extra, so the Redshift safety families run everywhere that matters.
    ``customers`` carries SVV_TABLE_INFO facts; ``signups`` holds no data so
    the view omits it (the census must still see it); ``events`` is the
    50 GB table a non-trivial estimate comes from.
    """

    pytest.importorskip("redshift_connector")
    from fakes.redshift import FakeRedshiftConnection, FakeRedshiftTable

    tables = [
        FakeRedshiftTable(
            schema="shop",
            name="customers",
            columns=[
                ("id", "bigint", False),
                ("email", "character varying", True),
                ("payload", "super", True),
            ],
            size_mb=5_000,  # 5 GB -> a non-trivial seconds estimate
            tbl_rows=100.0,
        ),
        FakeRedshiftTable(
            schema="shop",
            name="signups",
            columns=[("id", "bigint", True)],
            # Holds no data: SVV_TABLE_INFO omits it, the pg_class census must not.
            size_mb=None,
        ),
        FakeRedshiftTable(
            schema="shop",
            name="events",
            columns=[("id", "bigint", True), ("payload", "super", True)],
            size_mb=50_000,  # 50 GB
            tbl_rows=1_000_000.0,
        ),
    ]
    return FakeRedshiftConnection(tables=tables, database="dexdb")


@pytest.fixture
def dbt_project_dir(tmp_path: Path) -> Path:
    """A minimal dbt project with a project-local profiles.yml.

    The profile carries a duckdb ``dev`` target (a fresh writable file, distinct
    from the read-only ``duckdb_file`` fixture) and a ``prod`` target so the
    prod-refusal path has something real to refuse. One staging model plus its
    schema.yml gives load/plan/apply a hand-written file to respect.
    """

    project = tmp_path / "analytics"
    (project / "models" / "staging").mkdir(parents=True)

    (project / "dbt_project.yml").write_text(
        "name: dex_test\n"
        'version: "1.0.0"\n'
        "profile: dex_test\n"
        'model-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n"
        "    prod:\n"
        "      type: duckdb\n"
        f"      path: {tmp_path / 'prod.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "staging" / "stg_customers.sql").write_text(
        "select 1 as id, 'a@example.com' as email\n", encoding="utf-8"
    )
    (project / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    columns:\n"
        "      - name: id\n"
        "        tests: [not_null]\n",
        encoding="utf-8",
    )
    return project
