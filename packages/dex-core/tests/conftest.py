"""Shared fixtures. DuckDB is the test engine: in-process, free, fast."""

from __future__ import annotations

from pathlib import Path

import pytest


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
    that matters. The DEX_WH warehouse starts SUSPENDED so resume-minimum
    behavior is on by default; tests that want a warm warehouse flip its state.
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
