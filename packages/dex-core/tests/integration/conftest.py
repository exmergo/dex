"""Live cloud integration: real credentials, real jobs, real (tiny) bills.

Each connector's tests are skipped unless its environment opts in, so the
default suite stays deterministic and free for contributors without cloud
accounts.

BigQuery (bills to DEX_TEST_BQ_PROJECT; every query capped at
DEX_TEST_BQ_MAX_BYTES, default 100 MB, so a worst-case bug costs cents; reads
public datasets, writes only to the TTL'd scratch dataset):

    gcloud auth application-default login
    DEX_TEST_BQ_PROJECT=<your-project> DEX_TEST_BQ_DATASET=dex_ci \
        uv run pytest tests/integration -q

Snowflake (bills warehouse time on the pinned X-Small; every statement capped
at DEX_TEST_SNOWFLAKE_MAX_SECONDS, default 60; reads SNOWFLAKE_SAMPLE_DATA,
writes only to the transient scratch database; the account-level resource
monitor is the hard backstop). Auth comes from a connections.toml entry
locally or SNOWFLAKE_* env in CI (scripts/setup_snowflake_ci.sh provisions
both):

    DEX_TEST_SNOWFLAKE_CONNECTION=dex-ci DEX_TEST_SNOWFLAKE_DATABASE=DEX_CI \
        uv run pytest tests/integration -q

Databricks (bills warehouse time on the pinned SQL warehouse; every statement
capped at DEX_TEST_DATABRICKS_MAX_SECONDS, default 60, enforced server-side by
STATEMENT_TIMEOUT; reads the samples catalog, writes only to the scratch
catalog). Auth is the SDK's unified chain: `databricks auth login` locally, or
DATABRICKS_HOST plus a credential in CI (scripts/setup_databricks_ci.sh
provisions the service principal and the OIDC federation policy):

    DEX_TEST_DATABRICKS_WAREHOUSE=<warehouse-id> \
        DEX_TEST_DATABRICKS_CATALOG=dex_ci uv run pytest tests/integration -q

Postgres (bills nothing: a local container; db-load gating is exercised for
real, statement timeouts and all). The DSN should be the read-only role from
scripts/postgres_seed.sql; transform additionally needs the dbt_dev role's
password in DEX_TEST_PG_DEV_PASSWORD (scripts/setup_postgres_dev.sh stands
the whole thing up):

    DEX_TEST_PG_DSN=postgresql://dex_ro:dex_ro@localhost:5433/dex_dogfood \
        DEX_TEST_PG_DEV_PASSWORD=dbt_dev uv run pytest tests/integration -q

Redshift (bills RPU time on the pinned Serverless workgroup; every confirmed
budget derives from DEX_TEST_REDSHIFT_MAX_SECONDS, default 60 -- small fixed
multiples covering the 60-second wake minimum plus the seeded scans -- and the
budget-derived statement_timeout caps each statement server-side; the
workgroup usage limit is the hard backstop). Auth is
the AWS default credential chain (`aws configure` locally, an assumed OIDC
role in CI; scripts/setup_redshift_ci.sh provisions the workgroup, the users,
and the usage limit); transform additionally needs the dbt_dev user's
password in DEX_TEST_REDSHIFT_DEV_PASSWORD unless the IAM profile path is in
use:

    DEX_TEST_REDSHIFT_WORKGROUP=dex-ci DEX_TEST_REDSHIFT_DATABASE=dev \
        uv run pytest tests/integration -q

ClickHouse (bills nothing: a local container; db-load gating is exercised for
real, server-side caps and all). The DSN should be the read-only user from
scripts/clickhouse_local_users.sql; transform additionally needs the dbt_dev user's
password in DEX_TEST_CH_DEV_PASSWORD. One script stands the whole thing up and
is the same one CI runs, so the local target and the CI target cannot drift:

    scripts/setup_clickhouse_dev.sh
    DEX_TEST_CH_DSN=clickhouse://dex_ro:dex_ro@localhost:8124/app \
        DEX_TEST_CH_DEV_PASSWORD=dbt_dev \
        uv run pytest tests/integration -q -m clickhouse

Trino (bills nothing: a local container pair, Trino plus the Postgres database
its keyless Iceberg catalog keeps table pointers in; db-load gating is exercised
for real). The URL is the read-only dex_ro identity from scripts/trino/rules.json;
dbt builds as dbt_dev, which the rendered profile reads from TRINO_USER. One
script stands the pair up and is the same one CI runs:

    scripts/setup_trino_dev.sh
    DEX_TEST_TRINO_URL=http://dex_ro@127.0.0.1:8085 \
        uv run pytest tests/integration -q -m trino

Trino with the Glue and S3 Iceberg catalog (bills a few megabytes of S3 and Glue
requests). scripts/setup_trino_iceberg_ci.sh provisions the bucket, the Glue
databases, the OIDC role and the protected environment; locally the same pair
attaches the catalog with `--aws` from the current AWS CLI session:

    scripts/setup_trino_dev.sh --aws
    DEX_TEST_TRINO_URL=http://dex_ro@127.0.0.1:8085 \
        DEX_TEST_TRINO_GLUE_SCHEMA=dex_ci \
        uv run pytest tests/integration -q -m trino_iceberg

ClickHouse Cloud (bills the dedicated Scale service in AWS us-east-1; the
repository workflow performs the hard daily-usage preflight before any SQL).
scripts/setup_clickhouse_cloud_ci.sh provisions the two isolated databases,
least-privilege users, server-side limits, scoped usage reader, and protected
GitHub environment used by this suite. The scoped API key is read from two
files in DEX_SECRETS_DIR, never from the environment (see
scripts/clickhouse_cloud/README.md):

    scripts/clickhouse_cloud/run_integration.sh
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REQUIRED_ENV = ("DEX_TEST_BQ_PROJECT", "DEX_TEST_BQ_DATASET")

MAX_BYTES = int(os.environ.get("DEX_TEST_BQ_MAX_BYTES", str(100 * 1024 * 1024)))

SF_MAX_SECONDS = float(os.environ.get("DEX_TEST_SNOWFLAKE_MAX_SECONDS", "60"))

DBX_MAX_SECONDS = float(os.environ.get("DEX_TEST_DATABRICKS_MAX_SECONDS", "60"))

RS_MAX_SECONDS = float(os.environ.get("DEX_TEST_REDSHIFT_MAX_SECONDS", "60"))

CH_MAX_SECONDS = float(os.environ.get("DEX_TEST_CH_MAX_SECONDS", "60"))

TRINO_MAX_SECONDS = float(os.environ.get("DEX_TEST_TRINO_MAX_SECONDS", "60"))


def cloud_api_key_files() -> tuple[Path, Path]:
    """The scoped ClickHouse Cloud API key's two files, where
    scripts/clickhouse_cloud/secrets.sh puts and reads them.

    The key never rides in the test environment: CI writes it to these files in
    a step of its own, and the one test that needs it hands it to the one
    clickhousectl call that uses it. The directory default mirrors secrets.sh,
    including treating an empty variable as unset.
    """

    # Only read: the files are written by CI or by the operator, never here.
    base = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp"  # noqa: S108
    directory = Path(os.environ.get("DEX_SECRETS_DIR") or Path(base) / "dex-secrets")
    return directory / "clickhouse_api_key", directory / "clickhouse_api_secret"


def integration_budget(ceiling: float | None = None) -> dict[str, float | bool]:
    """Budget config for one ephemeral live-test project.

    Every integration test gets a fresh ``tmp_path`` and already runs behind a
    per-command ceiling plus the connector's external CI backstop. Record that
    these disposable projects deliberately have no cross-command ceiling so the
    one-time production prompt does not replace the behavior under test.
    """

    budget: dict[str, float | bool] = {"session_ceiling_declined": True}
    if ceiling is not None:
        budget["ceiling"] = ceiling
    return budget


def assert_ok(rc: int, envelope: dict) -> dict:
    """Assert a live command succeeded, and say what the warehouse said if it
    did not. Returns the envelope, so it reads inline.

    The obvious spelling is ``assert rc == 0, envelope``, and it is why a live
    Redshift failure cost sixteen CI runs to diagnose instead of five minutes
    (#310): pytest elides the middle of a long assertion message, and the
    middle of a profiling envelope is exactly where ``errors`` sits, so the
    server's own message never once reached a CI log. Putting the errors
    first, alone, makes the surviving line the one worth reading.
    """

    assert rc == 0, why(envelope)
    return envelope


def why(envelope: dict) -> str:
    """An envelope's errors first, alone, for an assertion message (see
    :func:`assert_ok` for why the whole envelope is the wrong message)."""

    return (
        f"errors={envelope.get('errors')} reason={envelope.get('reason')} "
        f"warnings={envelope.get('warnings')}"
    )


def _snowflake_enabled() -> bool:
    return bool(
        os.environ.get("DEX_TEST_SNOWFLAKE_DATABASE")
        and (
            os.environ.get("DEX_TEST_SNOWFLAKE_CONNECTION")
            or (
                os.environ.get("SNOWFLAKE_ACCOUNT") and os.environ.get("SNOWFLAKE_USER")
            )
        )
    )


@pytest.fixture(autouse=True)
def _require_cloud_env(request):
    if request.node.get_closest_marker("cube"):
        # Ahead of every warehouse branch: a Cube test needs its renderer as
        # well as its warehouse, and one without `postgres` runs on DuckDB.
        warehouse = (
            "postgres" if request.node.get_closest_marker("postgres") else "duckdb"
        )
        required = ["CUBEJS_API_SECRET", f"DEX_TEST_CUBE_URL_{warehouse.upper()}"]
        if warehouse == "postgres":
            required.append("DEX_TEST_PG_DSN")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            pytest.skip(
                f"Cube integration disabled: set {', '.join(missing)} (run "
                f"scripts/setup_cube_dev.sh --db {warehouse}, then source "
                "~/.cache/dex-dogfood/cube.env)"
            )
        pytest.importorskip("httpx")
        pytest.importorskip("psycopg" if warehouse == "postgres" else "duckdb")
        return
    if request.node.get_closest_marker("snowflake"):
        if not _snowflake_enabled():
            pytest.skip(
                "Snowflake integration disabled: set DEX_TEST_SNOWFLAKE_DATABASE "
                "plus DEX_TEST_SNOWFLAKE_CONNECTION (or SNOWFLAKE_ACCOUNT/USER)"
            )
        pytest.importorskip("snowflake.connector")
        return
    if request.node.get_closest_marker("databricks"):
        if not os.environ.get("DEX_TEST_DATABRICKS_WAREHOUSE"):
            pytest.skip(
                "Databricks integration disabled: set "
                "DEX_TEST_DATABRICKS_WAREHOUSE (and authenticate via "
                "`databricks auth login` or DATABRICKS_HOST plus a credential)"
            )
        pytest.importorskip("databricks.sql")
        return
    if request.node.get_closest_marker("postgres"):
        if not os.environ.get("DEX_TEST_PG_DSN"):
            pytest.skip(
                "Postgres integration disabled: set DEX_TEST_PG_DSN (run "
                "scripts/setup_postgres_dev.sh for a seeded local container)"
            )
        pytest.importorskip("psycopg")
        return
    if request.node.get_closest_marker("redshift"):
        if not (
            os.environ.get("DEX_TEST_REDSHIFT_WORKGROUP")
            or os.environ.get("DEX_TEST_REDSHIFT_HOST")
        ):
            pytest.skip(
                "Redshift integration disabled: set DEX_TEST_REDSHIFT_WORKGROUP "
                "(IAM via the AWS credential chain) or DEX_TEST_REDSHIFT_HOST "
                "plus REDSHIFT_* credentials"
            )
        pytest.importorskip("redshift_connector")
        return
    if request.node.get_closest_marker("clickhouse_cloud"):
        required = (
            "DEX_TEST_CH_CLOUD_ORG_ID",
            "DEX_TEST_CH_CLOUD_SERVICE_ID",
            "DEX_TEST_CH_CLOUD_HOST",
            "DEX_TEST_CH_CLOUD_PORT",
            "DEX_TEST_CH_CLOUD_DATABASE",
            "DEX_TEST_CH_CLOUD_DEV_DATABASE",
            "DEX_TEST_CH_CLOUD_COMPUTE_UNIT_PRICE_USD",
            "DEX_TEST_CH_CLOUD_MAX_SECONDS",
            "DEX_TEST_CH_CLOUD_DSN",
            "DEX_TEST_CH_CLOUD_DEV_PASSWORD",
        )
        missing = [name for name in required if not os.environ.get(name)]
        missing += [
            str(path)
            for path in cloud_api_key_files()
            if not (path.is_file() and path.stat().st_size)
        ]
        if missing:
            pytest.skip(
                "ClickHouse Cloud integration disabled: required values are "
                f"missing: {', '.join(missing)}"
            )
        pytest.importorskip("clickhouse_connect")
        return
    # Both Trino branches sit above the BigQuery fall-through below, which
    # treats any test without a matched marker as a BigQuery test.
    if request.node.get_closest_marker("trino_iceberg"):
        missing = [
            name
            for name in ("DEX_TEST_TRINO_URL", "DEX_TEST_TRINO_GLUE_SCHEMA")
            if not os.environ.get(name)
        ]
        if missing:
            pytest.skip(
                "Trino Glue Iceberg integration disabled: set "
                f"{', '.join(missing)} (run scripts/setup_trino_dev.sh --aws)"
            )
        pytest.importorskip("trino")
        return
    if request.node.get_closest_marker("trino"):
        if not os.environ.get("DEX_TEST_TRINO_URL"):
            pytest.skip(
                "Trino integration disabled: set DEX_TEST_TRINO_URL (run "
                "scripts/setup_trino_dev.sh for the seeded local pair)"
            )
        pytest.importorskip("trino")
        return
    if request.node.get_closest_marker("clickhouse"):
        if not os.environ.get("DEX_TEST_CH_DSN"):
            pytest.skip(
                "ClickHouse integration disabled: set DEX_TEST_CH_DSN (run "
                "scripts/setup_clickhouse_dev.sh for a seeded local container)"
            )
        pytest.importorskip("clickhouse_connect")
        return
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"BigQuery integration disabled: set {', '.join(missing)}")
    pytest.importorskip("google.cloud.bigquery")


@pytest.fixture
def bq_project() -> str:
    return os.environ["DEX_TEST_BQ_PROJECT"]


@pytest.fixture
def bq_scratch_dataset() -> str:
    return os.environ["DEX_TEST_BQ_DATASET"]


@pytest.fixture
def sf_scratch_database() -> str:
    return os.environ["DEX_TEST_SNOWFLAKE_DATABASE"]


@pytest.fixture
def sf_connection_name() -> str | None:
    return os.environ.get("DEX_TEST_SNOWFLAKE_CONNECTION")


@pytest.fixture
def sf_warehouse() -> str:
    return os.environ.get("DEX_TEST_SNOWFLAKE_WAREHOUSE", "DEX_CI_WH")


@pytest.fixture
def dbx_warehouse() -> str:
    return os.environ["DEX_TEST_DATABRICKS_WAREHOUSE"]


@pytest.fixture
def dbx_scratch_catalog() -> str | None:
    # Needed by transform only; explore reads samples and writes nothing.
    return os.environ.get("DEX_TEST_DATABRICKS_CATALOG")


@pytest.fixture
def rs_workgroup() -> str | None:
    return os.environ.get("DEX_TEST_REDSHIFT_WORKGROUP")


@pytest.fixture
def rs_host() -> str | None:
    return os.environ.get("DEX_TEST_REDSHIFT_HOST")


@pytest.fixture
def rs_database() -> str | None:
    return os.environ.get("DEX_TEST_REDSHIFT_DATABASE")


@pytest.fixture
def rs_dev_password() -> str | None:
    # Needed by transform's password path only; IAM profiles need nothing.
    return os.environ.get("DEX_TEST_REDSHIFT_DEV_PASSWORD")


@pytest.fixture
def pg_dsn() -> str:
    return os.environ["DEX_TEST_PG_DSN"]


@pytest.fixture
def pg_dev_password() -> str | None:
    return os.environ.get("DEX_TEST_PG_DEV_PASSWORD")


# --- the unpivot_json_object live fixture ----------------------------------------
#
# One payload shape shared by every connector's macro build test: a staging
# model inlining three rows (two JSON documents whose values are themselves
# objects, one NULL), the unpivot mart, and the schema tests that pin the
# nested-key failure mode (a surfaced nested key like `role` fails
# accepted_values; the NULL row must yield no rows at all).

UNPIVOT_DOC_A = (
    '{"rel_a": {"role": "admin", "since": 2020}, "rel_b": {"role": "viewer"}}'
)
UNPIVOT_DOC_B = '{"rel_c": {"role": "editor"}}'


def unpivot_fixture_edits(wrap, null_expr: str) -> dict:
    """The edits payload, parameterized by the connector's JSON idiom:
    ``wrap`` renders a JSON document literal into the column's type and
    ``null_expr`` is a typed NULL for it."""

    return {
        "edits": [
            {
                "path": "models/staging/stg_entities.sql",
                "kind": "model_sql",
                "content": (
                    f"select 1 as id, {wrap(UNPIVOT_DOC_A)} as attributes\n"
                    f"union all select 2, {wrap(UNPIVOT_DOC_B)}\n"
                    f"union all select 3, {null_expr}\n"
                ),
            },
            {
                "path": "models/marts/entity_relations.sql",
                "kind": "model_sql",
                "content": (
                    "select id, key as related_id, value as attrs\n"
                    "from (\n"
                    "  {{ unpivot_json_object(relation=ref('stg_entities'),"
                    " json_column='attributes', passthrough=['id']) }}\n"
                    ")\n"
                ),
            },
            {
                "path": "models/marts/entity_relations.yml",
                "kind": "schema_yml",
                "content": (
                    "version: 2\n"
                    "models:\n"
                    "  - name: entity_relations\n"
                    "    columns:\n"
                    "      - name: related_id\n"
                    "        data_tests:\n"
                    "          - not_null\n"
                    "          - accepted_values:\n"
                    "              values: ['rel_a', 'rel_b', 'rel_c']\n"
                ),
            },
        ]
    }


def assert_unpivot_build(built: dict) -> None:
    """The build envelope assertions shared by every connector: models built,
    every schema test passed (accepted_values is the nested-key tripwire)."""

    assert built["status"] == "ok", built
    statuses = {n["name"]: n["status"] for n in built["data"]["nodes"]}
    assert statuses.get("stg_entities") == "success"
    assert statuses.get("entity_relations") == "success"
    assert all(s in {"success", "pass"} for s in statuses.values()), statuses


#: The downstream probe (#648): an intermediate model with one row its filter
#: drops, a mart that counts it, and a singular test on the mart only. The
#: model's own `not_null`/`unique` cannot see the filter go; the mart's count can.
DOWNSTREAM_PROBE = "dex_mutation_int"
DOWNSTREAM_MART = "dex_mutation_mart"
DOWNSTREAM_TEST = "assert_dex_mutation_mart_count"


def downstream_probe_edits(rows_sql: str) -> dict:
    """The probe as an edits payload. ``rows_sql`` is the connector's spelling
    of a SELECT over the literal rows (1, 10), (2, 200) and (3, 1) as columns
    ``id`` and ``amount``; the probe keeps the two with ``amount > 5``."""

    return {
        "edits": [
            {
                "path": f"models/staging/{DOWNSTREAM_PROBE}.sql",
                "kind": "model_sql",
                "content": (
                    f"select id, amount from ({rows_sql}) as t where amount > 5\n"  # noqa: S608
                ),
            },
            {
                "path": f"models/marts/{DOWNSTREAM_MART}.sql",
                "kind": "model_sql",
                "content": (
                    f"select count(*) as n from {{{{ ref('{DOWNSTREAM_PROBE}') }}}}\n"  # noqa: S608
                ),
            },
            {
                "path": f"tests/{DOWNSTREAM_TEST}.sql",
                "kind": "test_sql",
                "content": (
                    f"select n from {{{{ ref('{DOWNSTREAM_MART}') }}}} where n <> 2\n"  # noqa: S608
                ),
            },
            {
                "path": "models/staging/schema.yml",
                "kind": "schema_yml",
                "content": (
                    "version: 2\n"
                    "models:\n"
                    f"  - name: {DOWNSTREAM_PROBE}\n"
                    "    columns:\n"
                    "      - name: id\n"
                    "        data_tests: [not_null, unique]\n"
                ),
            },
        ]
    }


def assert_downstream_run(run_cli, capsys, root: str, budget: float) -> dict:
    """The live acceptance for #648, shared by every metered connector.

    Unasked, the downstream tests are off and say why; asked, they are priced
    into the one estimate (the baseline line grows by the mart's test), and
    the confirmed run reports the dropped filter as `caught_downstream`, with
    the model's own score still counting it as a miss. Returns the routed
    run's data for any connector-specific checks."""

    mutate = ["--repo-root", root, "transform", "test", "--mutate", DOWNSTREAM_PROBE]
    rc, unasked = run_cli(mutate, capsys)
    assert unasked["status"] == "needs_confirmation", why(unasked)
    rc, asked = run_cli([*mutate, "--downstream"], capsys)
    assert asked["status"] == "needs_confirmation", why(asked)
    lines_key = next(k for k in asked["data"] if k.startswith("per_table_"))
    assert (
        asked["data"][lines_key]["(baseline)"]
        > unasked["data"][lines_key]["(baseline)"]
    ), (unasked["data"], asked["data"])
    assert asked["cost"]["estimate"] > unasked["cost"]["estimate"]

    rc, alone = run_cli(
        [*mutate, "--max-mutants", "1", "--confirm", "--budget", str(budget)], capsys
    )
    assert rc == 0, why(alone)
    off = alone["data"]["downstream"]
    assert off["enabled"] is False and "--downstream" in off["reason"], off
    assert alone["data"]["score_with_downstream"] is None

    rc, routed = run_cli(
        [*mutate, "--downstream", "--confirm", "--budget", str(budget)], capsys
    )
    assert rc == 0, why(routed)
    data = routed["data"]
    assert data["downstream"]["enabled"] is True, data["downstream"]
    assert data["downstream"]["models"] == [DOWNSTREAM_MART], data["downstream"]
    assert data["downstream"]["tests"] == [DOWNSTREAM_TEST], data["downstream"]
    by_operator = {m["operator"]: m for m in data["mutants"]}
    drop = by_operator["predicate_drop"]
    assert drop["status"] == "caught_downstream", drop
    assert drop["caught_downstream_by"] == [DOWNSTREAM_TEST]
    # No amount sits on the boundary at 5, so nothing anywhere sees the flip.
    assert by_operator["comparison"]["status"] == "survived"
    counts = data["counts"]
    decided = counts["killed"] + counts["caught_downstream"] + counts["survived"]
    assert data["score"] == counts["killed"] / decided
    assert data["score_with_downstream"] == (
        (counts["killed"] + counts["caught_downstream"]) / decided
    )
    return data


@pytest.fixture
def ch_dsn() -> str:
    return os.environ["DEX_TEST_CH_DSN"]


@pytest.fixture
def ch_dev_password() -> str:
    return os.environ.get("DEX_TEST_CH_DEV_PASSWORD", "dbt_dev")


@pytest.fixture
def trino_url() -> str:
    return os.environ["DEX_TEST_TRINO_URL"]


@pytest.fixture
def trino_dev_user() -> str:
    return os.environ.get("DEX_TEST_TRINO_DEV_USER", "dbt_dev")


def trino_client(url: str, *, user: str | None = None):
    """A raw client on the same coordinator, as ``user`` (the URL's own user by
    default). Used to prove what the server itself allows, which is the half of
    read-only a dex guard cannot vouch for."""

    from urllib.parse import urlsplit

    import trino

    parts = urlsplit(url)
    return trino.dbapi.connect(
        host=parts.hostname,
        port=parts.port or 8080,
        user=user or parts.username,
        http_scheme="https" if parts.scheme == "https" else "http",
    )
