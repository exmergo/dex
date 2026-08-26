"""The injectable connection seam, connector by connector.

Credential discovery reads process-ambient state, which is right for one person
at a terminal and cannot express per-end-user access control in a process serving
several. A host supplies the connection instead, and `open_adapter` skips its own
discovery for that connector.

Every test here rigs discovery to raise. That is the whole method: an assertion
that an injected connection *works* would also pass if ambient discovery had
quietly run and reached the warehouse as the container rather than as the end
user, which is precisely the bug this seam exists to prevent.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.config import (
    BigQueryTarget,
    DatabricksTarget,
    DexConfig,
    PostgresTarget,
    RedshiftTarget,
    SnowflakeTarget,
)
from exmergo_dex_core.connect import ConnectionSource, open_adapter
from exmergo_dex_core.storage import MemoryStore

#: Every ambient chain, by the name `connect.py` calls it under.
_DISCOVERY = (
    "_default_credentials",
    "resolve_snowflake_connection",
    "resolve_databricks_connection",
    "resolve_postgres_connection",
    "resolve_redshift_connection",
    "resolve_clickhouse_connection",
)


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    """No ambient credential is reachable. Injection is the only way through."""

    import exmergo_dex_core.connect as connect_mod

    def unreachable(*_args, **_kwargs):
        raise AssertionError("ambient credential discovery ran")

    for name in _DISCOVERY:
        monkeypatch.setattr(connect_mod, name, unreachable)


def _open(config: DexConfig, connection: ConnectionSource, **kwargs):
    return open_adapter(
        config=config, store=MemoryStore(), connection=connection, **kwargs
    )


# --- the four connectors that take a live connection -------------------------------


def test_snowflake_opens_from_a_host_connection(fake_sf_connection):
    """The account is a non-secret fact, so it comes from config rather than from
    the handle: dex cannot read it back off a connection it did not build the
    parameters for, and asking the host to restate it in the record would put two
    spellings of the same fact in two places."""

    config = DexConfig(
        connector="snowflake",
        snowflake=SnowflakeTarget(account="acct-1", warehouse="DEX_WH"),
    )
    adapter = _open(config, ConnectionSource(connect=lambda: fake_sf_connection))

    assert adapter.name == "snowflake"
    assert adapter.account == "acct-1"
    assert adapter.auth_method == "host_supplied"
    # The host owns the session, so dex did not reconfigure it on the way in.
    assert fake_sf_connection.statements == []
    adapter.close()
    assert fake_sf_connection.closed is False


def test_postgres_opens_from_a_host_connection(fake_pg_connection):
    config = DexConfig(connector="postgres", postgres=PostgresTarget(schemas=["shop"]))
    source = ConnectionSource(
        connect=lambda: fake_pg_connection, auth_method="host_supplied:mtls"
    )
    adapter = _open(config, source)

    assert adapter.name == "postgres"
    assert adapter.capabilities()["auth_method"] == "host_supplied:mtls"
    adapter.close()
    assert fake_pg_connection.closed is False


def test_redshift_opens_from_a_host_connection(fake_redshift_connection):
    """Redshift's control-plane facts (Serverless workgroup, base RPU capacity)
    are discovered through boto3 alongside the credential, so a host-supplied
    connection arrives without them. The adapter already treats that as
    translation degrading while gating holds: seconds still bind, only the
    RPU-hour display is missing."""

    config = DexConfig(connector="redshift", redshift=RedshiftTarget(schemas=["shop"]))
    adapter = _open(config, ConnectionSource(connect=lambda: fake_redshift_connection))

    assert adapter.name == "redshift"
    assert adapter.compute == {}
    caps = adapter.capabilities()
    assert caps["compute"]["kind"] == "unknown"
    # Degraded translation, intact gating: the guarded quantity is still seconds,
    # and only the RPU-hour figure alongside it is missing.
    assert caps["paradigm"] == "compute_time"
    assert "ceiling_rpu_hours" not in caps["budget"]
    adapter.close()
    assert fake_redshift_connection.closed is False


def test_databricks_opens_from_both_host_clients(fake_databricks):
    """Two clients, and only one of them is billed. The REST workspace client
    serves metadata; the factory opens the SQL session that lands on the
    warehouse, so an injected source has to keep the free path free."""

    config = DexConfig(
        connector="databricks",
        databricks=DatabricksTarget(
            host="https://dbc-1.cloud.databricks.com/",
            warehouse="fake-wh",
            catalogs=["shop"],
        ),
    )
    adapter = _open(
        config,
        ConnectionSource(
            connect=fake_databricks.sql_connect, workspace=fake_databricks.workspace
        ),
    )

    assert adapter.name == "databricks"
    # Normalized to what the SQL driver and capabilities() want, same as the
    # discovered path does with the SDK's host.
    assert adapter.host == "dbc-1.cloud.databricks.com"
    adapter.capabilities()
    adapter.list_objects()
    assert fake_databricks.connect_count == 0


# --- BigQuery injects a credential, not a connection -------------------------------


def test_bigquery_opens_from_a_host_credential(fake_bq_client, monkeypatch):
    """The one connector where what dex holds afterwards is dex's own: the
    adapter builds a client around the supplied credential, so that client is
    closed on the way out even though the credential came from outside."""

    pytest.importorskip("google.cloud.bigquery")
    from google.cloud import bigquery

    class HostCredential:
        pass

    HostCredential.__module__ = "google.auth.compute_engine.credentials"
    seen = {}

    def client(*, project, credentials=None, **_kw):
        seen["project"] = project
        seen["credentials"] = credentials
        return fake_bq_client

    monkeypatch.setattr(bigquery, "Client", client)
    config = DexConfig(
        connector="bigquery",
        bigquery=BigQueryTarget(project="test-proj", datasets=["shop"]),
    )
    adapter = _open(config, ConnectionSource(connect=HostCredential))

    assert seen["project"] == "test-proj"
    assert isinstance(seen["credentials"], HostCredential)
    # Classified from the credential's own type, so a host cannot misreport it.
    assert adapter.capabilities()["principal_type"] == "metadata"
    adapter.close()
    assert fake_bq_client.closed is True


def test_bigquery_without_ambient_adc_needs_a_project_it_can_name(monkeypatch):
    """Project resolution loses a link when the credential comes from a host:
    there is no ADC default to fall back on. The refusal has to name the fix the
    *host* has, not tell a container to run a gcloud command."""

    pytest.importorskip("google.cloud.bigquery")
    from exmergo_dex_core.connect import CredentialDiscoveryError

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)
    config = DexConfig(connector="bigquery", bigquery=BigQueryTarget())

    with pytest.raises(CredentialDiscoveryError) as exc:
        _open(config, ConnectionSource(connect=object), repo_root="/nonexistent")

    assert "on the DexConfig you passed" in str(exc.value)
    assert "gcloud" not in str(exc.value)
