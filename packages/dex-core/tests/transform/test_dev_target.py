"""The dev-target preflight: config that looks live is live, or says so.

Drift (`.dex/config.yml` edited after `transform init` rendered `profiles.yml`)
and absence (a dev database dbt cannot create for itself) are both refusals, both
free, and both happen before the cost gate. The drift half needs no connection at
all, which is why most of this file is pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.config import (
    DexConfig,
    DuckDBTarget,
    SnowflakeTarget,
)
from exmergo_dex_core.transform import dev_target


def _write_profile(project: Path, output: str, target: str = "dev") -> None:
    (project / "profiles.yml").write_text(
        f"dex_test:\n  target: {target}\n  outputs:\n    {target}:\n{output}",
        encoding="utf-8",
    )


def _snowflake_profile(project: Path, **overrides: str) -> None:
    fields = {
        "type": "snowflake",
        "account": "ORG-ACCT",
        "user": "DEX",
        "warehouse": "DEX_WH",
        "database": "DBT_DEV",
        "schema": "DBT_DEV",
        # Quoted exactly as `transform init` renders it: unquoted Jinja is not
        # valid YAML, for dex or for dbt.
        "password": "'{{ env_var(''SNOWFLAKE_PASSWORD'') }}'",
        **overrides,
    }
    _write_profile(project, "".join(f"      {k}: {v}\n" for k, v in fields.items()))


def _snowflake_config(**overrides) -> DexConfig:
    fields = {"warehouse": "DEX_WH", "dev_database": "DBT_DEV", "dev_schema": "DBT_DEV"}
    fields.update(overrides)
    return DexConfig(connector="snowflake", snowflake=SnowflakeTarget(**fields))


@pytest.fixture(autouse=True)
def no_warehouse(monkeypatch):
    """No connection reachable by default, so the drift half is exercised alone
    and no unit test can wander onto a real account. Tests that need a warehouse
    replace this with a fake adapter."""

    def unreachable(**_kwargs):
        raise RuntimeError("no connection discovered")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", unreachable)


# --- drift: config and profile must agree ------------------------------------------


def test_retargeted_dev_database_is_refused_naming_both_values(
    dbt_project_dir: Path, tmp_path: Path
):
    """The field report: edit `snowflake.dev_database`, get a green build against
    the old database. Now it refuses, and the message names both sides."""

    _snowflake_profile(dbt_project_dir)
    config = _snowflake_config(dev_database="DBT_DEV_DOES_NOT_EXIST")

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert "snowflake.dev_database: DBT_DEV_DOES_NOT_EXIST" in message
    assert "profiles.yml dev.database: DBT_DEV" in message
    assert ".dex/config.yml" in message and "analytics/profiles.yml" in message


def test_matching_config_and_profile_pass(dbt_project_dir: Path, tmp_path: Path):
    _snowflake_profile(dbt_project_dir)
    # No connection is configured, so the existence half degrades to a note.
    warnings = dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    assert all("disagree" not in w for w in warnings)


def test_snowflake_identifier_case_is_not_drift(dbt_project_dir: Path, tmp_path: Path):
    _snowflake_profile(dbt_project_dir, database="DBT_DEV")
    config = _snowflake_config(dev_database="dbt_dev")
    dev_target.check(dbt_project_dir, "dev", config, tmp_path)


def test_a_drifted_warehouse_is_refused(dbt_project_dir: Path, tmp_path: Path):
    _snowflake_profile(dbt_project_dir, warehouse="DEX_WH")
    config = _snowflake_config(warehouse="BIG_WH")
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "snowflake.warehouse: BIG_WH" in str(exc.value)


def test_an_unset_config_field_is_not_drift(dbt_project_dir: Path, tmp_path: Path):
    """A field the user never set was never a claim about the dev target, so a
    profile that names something there cannot have drifted away from it."""

    _snowflake_profile(dbt_project_dir, schema="ANALYTICS_DEV")
    config = DexConfig(
        connector="snowflake",
        snowflake=SnowflakeTarget(warehouse="DEX_WH", dev_database="DBT_DEV"),
    )
    dev_target.check(dbt_project_dir, "dev", config, tmp_path)


def test_a_switched_connector_is_refused(dbt_project_dir: Path, tmp_path: Path):
    """The shared fixture renders a duckdb profile; config says snowflake."""

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    message = str(exc.value)
    assert "disagree about the connector" in message
    assert "connector: snowflake" in message
    assert "type: duckdb" in message


def test_an_unreadable_profile_is_not_a_refusal(tmp_path: Path):
    """Absence of evidence stays absence of evidence: a project dex cannot read
    is dbt's problem to report, not a preflight refusal."""

    warnings = dev_target.check(tmp_path, "dev", _snowflake_config(), tmp_path)
    assert all("disagree" not in w for w in warnings)


def test_a_malformed_profile_is_not_a_refusal(dbt_project_dir: Path, tmp_path: Path):
    """Unquoted Jinja is invalid YAML. dbt will say so; the preflight must not
    crash with a parser traceback on the way there."""

    _write_profile(dbt_project_dir, "      password: {{ env_var('X') }}\n")
    warnings = dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    assert all("disagree" not in w for w in warnings)


def test_drift_message_never_carries_a_credential(
    dbt_project_dir: Path, tmp_path: Path
):
    """The profile holds a password reference, a user, and an account. Only
    namespace identifiers may reach the message that goes into the envelope."""

    _snowflake_profile(dbt_project_dir)
    config = _snowflake_config(dev_database="OTHER_DB")
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    for secret in ("password", "SNOWFLAKE_PASSWORD", "ORG-ACCT", "user"):
        assert secret not in message


# --- existence: duckdb keeps the behavior it always had -----------------------------


def test_duckdb_missing_file_with_sources_refuses(
    dbt_project_dir: Path, tmp_path: Path
):
    (dbt_project_dir / "models" / "staging" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n",
        encoding="utf-8",
    )
    config = DexConfig(connector="duckdb")
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "seed" in str(exc.value)


def test_duckdb_missing_file_without_sources_only_warns(
    dbt_project_dir: Path, tmp_path: Path
):
    warnings = dev_target.check(
        dbt_project_dir, "dev", DexConfig(connector="duckdb"), tmp_path
    )
    assert len(warnings) == 1
    assert "dbt will create an empty one" in warnings[0]


def test_duckdb_path_drift_is_refused(dbt_project_dir: Path, tmp_path: Path):
    config = DexConfig(
        connector="duckdb", duckdb=DuckDBTarget(path="/elsewhere.duckdb")
    )
    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "duckdb.path: /elsewhere.duckdb" in str(exc.value)


# --- existence: snowflake, through the fake ------------------------------------------


def test_missing_snowflake_dev_database_refuses_with_the_create_statement(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    pytest.importorskip("snowflake.connector")
    from fakes.snowflake import FakeSnowflakeConnection, FakeWarehouse

    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    connection = FakeSnowflakeConnection(
        warehouses=[FakeWarehouse(name="DEX_WH")], empty_databases=["SOMETHING_ELSE"]
    )

    def fake_open_adapter(**_kwargs):
        return SnowflakeAdapter(
            connection=connection,
            cost_gate=CostGate(
                paradigm=Paradigm.COMPUTE_TIME,
                ceiling=None,
                session_ceiling=None,
                session_spent=0.0,
                confirmed=False,
                connector="snowflake",
            ),
            target=SnowflakeTarget(warehouse="DEX_WH"),
            clock=connection.clock,
        )

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", fake_open_adapter)
    _snowflake_profile(dbt_project_dir)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    message = str(exc.value)
    assert 'dev_database "DBT_DEV" does not exist' in message
    assert "CREATE DATABASE IF NOT EXISTS DBT_DEV;" in message
    assert "dbt creates schemas but never databases" in message
    # The preflight is free: it never reaches a warehouse.
    assert connection.data_statements == []


def test_an_unopenable_connection_degrades_to_a_note(
    dbt_project_dir: Path, tmp_path: Path
):
    """dex discovers its own connection while dbt reads profiles.yml; the two can
    legitimately differ, so the preflight must never break a build dbt could run.
    The note names the failure class, so a defect here cannot hide as a shrug."""

    _snowflake_profile(dbt_project_dir)
    warnings = dev_target.check(dbt_project_dir, "dev", _snowflake_config(), tmp_path)
    assert len(warnings) == 1
    assert "could not preflight the dev database" in warnings[0]
    assert "RuntimeError" in warnings[0]


# --- existence, through the fakes: what dbt cannot create for itself ------------------


def _fake_open(monkeypatch, adapter):
    monkeypatch.setattr(
        "exmergo_dex_core.connect.open_adapter", lambda **_kwargs: adapter
    )


def _databricks(
    dbt_project_dir: Path,
    *,
    empty_catalogs: list[str],
    owners: dict[str, str] | None = None,
    grants: dict[str, set[str]] | None = None,
):
    pytest.importorskip("databricks.sql")
    from fakes.databricks import (
        FakeDatabricks,
        FakeDatabricksConnection,
        FakeWorkspaceClient,
    )

    from exmergo_dex_core.adapters.databricks import DatabricksAdapter
    from exmergo_dex_core.config import DatabricksTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    workspace = FakeWorkspaceClient(
        empty_catalogs=empty_catalogs,
        # The healthy default: the principal owns the dev catalog, so it may
        # create the dev schema inside it, which is the state a first build wants.
        owners=owners if owners is not None else {"dex_dev": "dex@example.com"},
        grants=grants,
    )
    fake = FakeDatabricks(workspace=workspace, connection=FakeDatabricksConnection())
    adapter = DatabricksAdapter(
        workspace=fake.workspace,
        sql_connect=fake.sql_connect,
        cost_gate=CostGate(
            paradigm=Paradigm.COMPUTE_TIME,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="databricks",
        ),
        target=DatabricksTarget(warehouse="fake-wh"),
        clock=fake.clock,
    )
    _write_profile(
        dbt_project_dir,
        "      type: databricks\n"
        "      catalog: dex_dev\n"
        "      schema: dbt_dev\n"
        "      http_path: /sql/1.0/warehouses/fake-wh\n",
    )
    config = DexConfig(
        connector="databricks",
        databricks=DatabricksTarget(
            warehouse="fake-wh", dev_catalog="dex_dev", dev_schema="dbt_dev"
        ),
    )
    return fake, adapter, config


def test_missing_databricks_dev_catalog_refuses_with_the_create_statement(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    fake, adapter, config = _databricks(
        dbt_project_dir, empty_catalogs=["something_else"]
    )
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert 'dev_catalog "dex_dev" does not exist' in message
    assert "CREATE CATALOG IF NOT EXISTS dex_dev;" in message
    assert "dbt creates schemas but never catalogs" in message
    # Free: Unity Catalog REST only, so the billed SQL warehouse never woke.
    assert fake.connect_count == 0


def test_an_existing_databricks_dev_catalog_the_principal_owns_passes(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    fake, adapter, config = _databricks(dbt_project_dir, empty_catalogs=["dex_dev"])
    _fake_open(monkeypatch, adapter)
    assert dev_target.check(dbt_project_dir, "dev", config, tmp_path) == []
    assert fake.connect_count == 0


def test_a_databricks_dev_schema_the_principal_cannot_write_is_warned_about(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """The failure this caught in the field: the dev catalog exists, so existence
    alone said yes, but the dev schema inside it belonged to another principal.
    dbt reported it as PERMISSION_DENIED on the first model, after the warehouse
    had woken and the budget had been spent.

    Owning the catalog is not enough to write inside a schema someone else owns,
    which is exactly what Unity Catalog answered live.
    """

    from fakes.databricks import FakeDatabricksTable

    fake, adapter, config = _databricks(
        dbt_project_dir,
        empty_catalogs=[],
        owners={"dex_dev": "dex@example.com", "dex_dev.dbt_dev": "someone-else"},
        grants={},
    )
    # A table puts the dbt_dev schema on the map, so the schema exists but is not
    # owned by us and carries no grant.
    fake.workspace._tables.append(
        FakeDatabricksTable(
            catalog="dex_dev",
            schema="dbt_dev",
            name="prior",
            columns=[("id", "bigint", True)],
        )
    )
    _fake_open(monkeypatch, adapter)

    warnings = dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert len(warnings) == 1
    assert "PERMISSION_DENIED" in warnings[0]
    assert "GRANT USE SCHEMA ON SCHEMA dex_dev.dbt_dev" in warnings[0]
    assert "GRANT CREATE TABLE ON SCHEMA dex_dev.dbt_dev" in warnings[0]
    # A warning, not a refusal: ownership and metastore-admin rights are invisible
    # to the grants API, so an empty answer cannot prove the build would fail.
    assert fake.connect_count == 0


def test_a_granted_databricks_dev_schema_passes(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """Grants, not just ownership: a principal explicitly granted what dbt needs
    must not be warned at."""

    from fakes.databricks import FakeDatabricksTable

    fake, adapter, config = _databricks(
        dbt_project_dir,
        empty_catalogs=[],
        owners={"dex_dev": "someone-else", "dex_dev.dbt_dev": "someone-else"},
        grants={
            "dex_dev": {"USE CATALOG"},
            "dex_dev.dbt_dev": {"USE SCHEMA", "CREATE TABLE"},
        },
    )
    fake.workspace._tables.append(
        FakeDatabricksTable(
            catalog="dex_dev",
            schema="dbt_dev",
            name="prior",
            columns=[("id", "bigint", True)],
        )
    )
    _fake_open(monkeypatch, adapter)
    assert dev_target.check(dbt_project_dir, "dev", config, tmp_path) == []


def _bigquery(
    dbt_project_dir: Path, *, project: str = "test-proj", dev: str = "dbt_dev"
):
    pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    client = FakeBigQueryClient(project="test-proj", empty_datasets=["shop"])
    adapter = BigQueryAdapter(
        project="test-proj",
        cost_gate=CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="bigquery",
        ),
        target=BigQueryTarget(),
        client=client,
    )
    _write_profile(
        dbt_project_dir,
        f"      type: bigquery\n      project: {project}\n      dataset: {dev}\n",
    )
    config = DexConfig(
        connector="bigquery",
        bigquery=BigQueryTarget(project=project, dev_dataset=dev, location="US"),
    )
    return client, adapter, config


def test_a_missing_bigquery_dev_dataset_warns_rather_than_refusing(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """The connector where the missing namespace is not fatal: dbt-bigquery's
    create_schema issues CREATE SCHEMA IF NOT EXISTS, which creates the dataset.
    Refusing would block a first build that would have succeeded."""

    client, adapter, config = _bigquery(dbt_project_dir, dev="dbt_dev")
    _fake_open(monkeypatch, adapter)

    warnings = dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert len(warnings) == 1
    assert "test-proj.dbt_dev does not exist" in warnings[0]
    assert "bigquery.datasets.create" in warnings[0]
    assert "bq mk --dataset --location=US test-proj.dbt_dev" in warnings[0]
    assert client.query_calls == []


def test_an_existing_bigquery_dev_dataset_passes(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    _client, adapter, config = _bigquery(dbt_project_dir, dev="shop")
    _fake_open(monkeypatch, adapter)
    assert dev_target.check(dbt_project_dir, "dev", config, tmp_path) == []


def test_an_unreachable_bigquery_dev_project_refuses(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """What dbt cannot create for itself: it makes datasets, never projects."""

    _client, adapter, config = _bigquery(dbt_project_dir, project="test-proj")
    config.bigquery.dev_dataset = "no-such-project.dbt_dev"
    _write_profile(
        dbt_project_dir,
        "      type: bigquery\n"
        "      project: test-proj\n"
        "      dataset: no-such-project.dbt_dev\n",
    )
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert "dbt creates datasets but never projects" in str(exc.value)


def _postgres(dbt_project_dir: Path, role, *, dev: str = "dbt_dev", profile_user=None):
    pytest.importorskip("psycopg")
    from fakes.postgres import FakePostgresConnection, FakePostgresTable

    from exmergo_dex_core.adapters.postgres import PostgresAdapter
    from exmergo_dex_core.config import PostgresTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    connection = FakePostgresConnection(
        tables=[
            FakePostgresTable(
                schema="app", name="orders", columns=[("id", "bigint", False)]
            )
        ],
        roles=[role],
        empty_schemas=["dbt_dev"],
    )
    adapter = PostgresAdapter(
        connection=connection,
        cost_gate=CostGate(
            paradigm=Paradigm.DB_LOAD,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="postgres",
        ),
        target=PostgresTarget(),
        clock=connection.clock,
    )
    _write_profile(
        dbt_project_dir,
        "      type: postgres\n"
        f"      user: {profile_user or role.name}\n"
        "      dbname: dexdb\n"
        f"      schema: {dev}\n",
    )
    config = DexConfig(connector="postgres", postgres=PostgresTarget(dev_schema=dev))
    return connection, adapter, config


def test_a_writable_postgres_dev_schema_passes(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    from fakes.postgres import FakeRole

    role = FakeRole(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE", "CREATE"}})
    connection, adapter, config = _postgres(dbt_project_dir, role)
    _fake_open(monkeypatch, adapter)
    assert dev_target.check(dbt_project_dir, "dev", config, tmp_path) == []
    assert connection.data_statements == []


def test_an_unwritable_postgres_dev_schema_refuses_with_the_grant(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    from fakes.postgres import FakeRole

    role = FakeRole(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE"}})
    connection, adapter, config = _postgres(dbt_project_dir, role)
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert 'missing CREATE on dev_schema "dbt_dev"' in message
    assert "GRANT USAGE, CREATE ON SCHEMA dbt_dev TO dbt_dev;" in message
    assert connection.data_statements == []


def test_an_absent_postgres_dev_schema_the_role_cannot_create_refuses(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """dbt creates the schema, but only if the role may, so the privilege is what
    is checked. The first build otherwise dies on a bare permission error."""

    from fakes.postgres import FakeRole

    role = FakeRole(name="dbt_dev", may_create_in_database=False)
    _connection, adapter, config = _postgres(dbt_project_dir, role, dev="not_there")
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert 'dev_schema "not_there" does not exist and role dbt_dev may not create it'
    assert "CREATE SCHEMA IF NOT EXISTS not_there AUTHORIZATION dbt_dev;" in message


def test_the_postgres_privilege_is_asked_of_the_profile_role(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """dex may read the warehouse as a read-only role while dbt builds as another,
    so the question has to be asked of the role the profile names. The refusal
    naming that role, and not the one dex connects as, is the proof it was."""

    from fakes.postgres import FakeRole

    builder = FakeRole(name="ci_builder", schema_privileges={"dbt_dev": {"USAGE"}})
    _connection, adapter, config = _postgres(
        dbt_project_dir, builder, profile_user="ci_builder"
    )
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert "role ci_builder is missing" in message
    assert "TO ci_builder;" in message


def _redshift(dbt_project_dir: Path, user, *, profile_extra: str = ""):
    pytest.importorskip("redshift_connector")
    from fakes.redshift import FakeRedshiftConnection, FakeRedshiftTable

    from exmergo_dex_core.adapters.redshift import RedshiftAdapter
    from exmergo_dex_core.config import RedshiftTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    connection = FakeRedshiftConnection(
        tables=[
            FakeRedshiftTable(
                schema="shop", name="orders", columns=[("id", "bigint", False)]
            )
        ],
        users=[user],
        empty_schemas=["dbt_dev"],
    )
    adapter = RedshiftAdapter(
        connection=connection,
        cost_gate=CostGate(
            paradigm=Paradigm.COMPUTE_TIME,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="redshift",
        ),
        target=RedshiftTarget(),
        clock=connection.clock,
    )
    _write_profile(
        dbt_project_dir,
        "      type: redshift\n"
        f"      user: {user.name}\n"
        "      dbname: dexdb\n"
        "      schema: dbt_dev\n" + profile_extra,
    )
    config = DexConfig(
        connector="redshift", redshift=RedshiftTarget(dev_schema="dbt_dev")
    )
    return connection, adapter, config


def test_a_redshift_iam_profile_skips_the_preflight_whatever_its_user_says(
    dbt_project_dir: Path, tmp_path: Path
):
    """A method: iam target mints its database user from the caller's identity
    at dbt runtime, so the profile's user field (a configured name or the
    rendered placeholder) is not a durable user to interrogate. The autouse
    no_warehouse fixture makes any attempted open fail, so a clean pass is
    proof the preflight never opened a connection."""

    pytest.importorskip("redshift_connector")
    from exmergo_dex_core.config import RedshiftTarget

    _write_profile(
        dbt_project_dir,
        "      type: redshift\n"
        "      method: iam\n"
        "      user: analyst\n"
        "      dbname: dexdb\n"
        "      schema: dbt_dev\n",
    )
    config = DexConfig(
        connector="redshift", redshift=RedshiftTarget(dev_schema="dbt_dev")
    )
    assert dev_target.check(dbt_project_dir, "dev", config, tmp_path) == []


def test_a_redshift_password_user_literally_named_iam_still_gets_the_preflight(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """The IAM skip keys on the profile's method, not on the user's name, so a
    real database user that happens to be called iam keeps its privilege check."""

    from fakes.redshift import FakeUser

    user = FakeUser(name="iam", schema_privileges={"dbt_dev": {"USAGE"}})
    connection, adapter, config = _redshift(dbt_project_dir, user)
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert "user iam is missing" in message
    assert "GRANT USAGE, CREATE ON SCHEMA dbt_dev TO iam;" in message
    assert connection.data_statements == []


def test_an_unwritable_redshift_dev_schema_refuses_with_the_grant(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    from fakes.redshift import FakeUser

    user = FakeUser(name="dbt_dev", schema_privileges={"dbt_dev": {"USAGE"}})
    connection, adapter, config = _redshift(dbt_project_dir, user)
    _fake_open(monkeypatch, adapter)

    with pytest.raises(dev_target.DevTargetError) as exc:
        dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    message = str(exc.value)
    assert 'missing CREATE on dev_schema "dbt_dev"' in message
    assert "GRANT USAGE, CREATE ON SCHEMA dbt_dev TO dbt_dev;" in message
    assert connection.data_statements == []


def test_an_unopenable_connection_degrades_to_a_note_on_every_connector(
    dbt_project_dir: Path, tmp_path: Path
):
    """The autouse no_warehouse fixture makes open_adapter raise. A preflight that
    cannot reach the warehouse must never be the thing that breaks a build dbt
    could have run."""

    from exmergo_dex_core.config import DatabricksTarget

    _write_profile(dbt_project_dir, "      type: databricks\n      catalog: dex_dev\n")
    config = DexConfig(
        connector="databricks",
        databricks=DatabricksTarget(warehouse="wh", dev_catalog="dex_dev"),
    )
    warnings = dev_target.check(dbt_project_dir, "dev", config, tmp_path)
    assert len(warnings) == 1
    assert "could not preflight the dev database" in warnings[0]
    assert "RuntimeError" in warnings[0]


# --- the init-time content check: what the new project would build into --------------


class _RecordingAdapter:
    """Answers dev_namespace_objects from a canned map and records every probe,
    so the composition (which namespaces, in what shape, per connector) is what
    gets asserted rather than any real listing."""

    def __init__(self, contents: dict | None = None, raises: Exception | None = None):
        self.contents = contents or {}
        self.raises = raises
        self.calls: list[tuple] = []
        self.closed = False

    def dev_namespace_objects(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.contents.get(args, [])

    def close(self):
        self.closed = True


def test_content_check_composes_base_and_layer_namespaces_per_connector(monkeypatch):
    from exmergo_dex_core.config import (
        BigQueryTarget,
        DatabricksTarget,
        PostgresTarget,
    )

    cases = [
        (
            DexConfig(
                connector="snowflake",
                dbt_target="dev",
                snowflake=SnowflakeTarget(warehouse="WH", dev_database="Scratch"),
            ),
            [
                ("SCRATCH", "DBT_DEV"),
                ("SCRATCH", "STAGING_DEV"),
                ("SCRATCH", "INTERMEDIATE_DEV"),
                ("SCRATCH", "MARTS_DEV"),
            ],
        ),
        (
            DexConfig(
                connector="bigquery",
                dbt_target="dev",
                bigquery=BigQueryTarget(project="test-proj", dev_dataset="dbt_dev"),
            ),
            [("dbt_dev",), ("staging_dev",), ("intermediate_dev",), ("marts_dev",)],
        ),
        (
            DexConfig(
                connector="databricks",
                dbt_target="dev",
                databricks=DatabricksTarget(warehouse="wh", dev_catalog="dex_dev"),
            ),
            [
                ("dex_dev", "dbt_dev"),
                ("dex_dev", "staging_dev"),
                ("dex_dev", "intermediate_dev"),
                ("dex_dev", "marts_dev"),
            ],
        ),
        (
            DexConfig(
                connector="postgres",
                dbt_target="dev",
                postgres=PostgresTarget(dev_schema="dbt_dev"),
            ),
            [("dbt_dev",), ("staging_dev",), ("intermediate_dev",), ("marts_dev",)],
        ),
    ]
    for config, expected in cases:
        adapter = _RecordingAdapter()
        _fake_open(monkeypatch, adapter)
        assert dev_target.content_check(config, ".", layered=True) == []
        assert adapter.calls == expected, config.connector
        assert adapter.closed


def test_content_check_probes_only_the_base_namespace_without_the_flag(monkeypatch):
    adapter = _RecordingAdapter()
    _fake_open(monkeypatch, adapter)
    config = _snowflake_config()
    assert dev_target.content_check(config, ".") == []
    assert adapter.calls == [("DBT_DEV", "DBT_DEV")]


def test_content_check_warning_qualifies_bigquery_datasets_with_the_project(
    monkeypatch,
):
    from exmergo_dex_core.config import BigQueryTarget

    adapter = _RecordingAdapter(contents={("staging_dev",): ["stg_orders"]})
    _fake_open(monkeypatch, adapter)
    config = DexConfig(
        connector="bigquery",
        dbt_target="dev",
        bigquery=BigQueryTarget(project="test-proj", dev_dataset="dbt_dev"),
    )
    warnings = dev_target.content_check(config, ".", layered=True)
    assert len(warnings) == 1
    assert "test-proj.staging_dev" in warnings[0]
    assert "1 object (stg_orders)" in warnings[0]


def test_content_check_never_opens_for_a_bare_duckdb_target():
    """No probes to run means no adapter opened: the autouse raiser is live and
    no degrade note appears."""

    config = DexConfig(connector="duckdb", duckdb=DuckDBTarget(path="/wh.duckdb"))
    assert dev_target.content_check(config, ".") == []


def test_content_check_degrades_to_a_note_when_a_probe_raises(monkeypatch):
    adapter = _RecordingAdapter(raises=RuntimeError("catalog listing exploded"))
    _fake_open(monkeypatch, adapter)
    warnings = dev_target.content_check(_snowflake_config(), ".")
    assert len(warnings) == 1
    assert "could not check the dev namespaces" in warnings[0]
    assert "catalog listing exploded" in warnings[0]
    assert adapter.closed


def test_content_check_degrades_to_a_note_when_no_connection_opens():
    """The autouse raiser stands in for absent credentials: one note, never a
    raise, because init is credential-optional."""

    warnings = dev_target.content_check(_snowflake_config(), ".")
    assert len(warnings) == 1
    assert "could not check the dev namespaces" in warnings[0]
    assert "RuntimeError" in warnings[0]
