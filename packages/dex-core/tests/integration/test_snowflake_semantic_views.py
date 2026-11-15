"""Live runs of `semantic.vendor: snowflake` (#1296): Snowflake Semantic Views
read back, queried governed through `SEMANTIC_VIEW()`, refused when stale, and
held to the warehouse by `maintain`.

dex never deploys a semantic view, so this module does, on a connection of its
own under the deployer role `scripts/setup_snowflake_ci.sh` provisions
(`DEX_TEST_SNOWFLAKE_SV_DEPLOYER_ROLE`, `DEX_CI_SV_DEPLOYER` in CI). dex itself
runs as the suite's usual identity (`DEX_CI_ROLE`), which reads what the
deployer creates through the scratch database's future grants and cannot create
a semantic view.

The deployed views are corpus fixtures (`tests/semantic_views_vendor/fixtures/
valid/`) over `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`, so nothing is copied: the module
creates one schema in the transient scratch database, deploys two views and one
empty table there, and drops the schema afterwards. Every governed answer is
held to SQL written by hand over the base tables, never to dex's rendering.

Needs the `[semantic-views]` extra beside `[snowflake]`, which the integration
job installs, and skips without the deployer role.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from .conftest import SF_MAX_SECONDS, assert_ok, integration_budget
from .test_snowflake_connect import SAMPLE_SCOPE, run_cli

pytest.importorskip("sqlglot", reason="the [semantic-views] extra is not installed")
# Module level, because the module-scoped `deployment` fixture runs before the
# function-scoped skip in conftest and would otherwise fail on the import.
pytest.importorskip(
    "snowflake.connector", reason="the [snowflake] extra is not installed"
)

pytestmark = [pytest.mark.integration, pytest.mark.snowflake]

FIXTURES = Path(__file__).parent.parent / "semantic_views_vendor" / "fixtures"
#: The deployed views, as the repository holds them.
VIEWS = ("star", "derived_metrics")
#: The idle tail on a warm warehouse, or the 60s resume minimum on a cold one,
#: on top of the scan: what the estimate quotes for uptime.
BUDGET = str(SF_MAX_SECONDS + 90)
TPCH = SAMPLE_SCOPE

DRIFT_VIEW = """\
# A hand-kept view over the empty table this module creates (#1296).
name: drift
tables:
  - name: orders
    base_table: {{database: {database}, schema: {schema}, table: DRIFT_ORDERS}}
    primary_key:
      columns: [o_id]
    dimensions:
      - name: status
        expr: status
    facts:
      - name: amount
        expr: amount
    metrics:
      - name: order_count
        expr: COUNT(o_id)
      - name: revenue
        expr: SUM(orders.amount)
"""


def _deployer_role() -> str:
    role = os.environ.get("DEX_TEST_SNOWFLAKE_SV_DEPLOYER_ROLE")
    if not role:
        pytest.skip(
            "Snowflake Semantic View integration disabled: set "
            "DEX_TEST_SNOWFLAKE_SV_DEPLOYER_ROLE to the role "
            "scripts/setup_snowflake_ci.sh provisions (DEX_CI_SV_DEPLOYER)"
        )
    return role


class Deployment:
    """The schema this module owns, and the deployer's connection to it."""

    def __init__(self, conn: Any, database: str, schema: str, warehouse: str):
        self.conn = conn
        self.database = database
        self.schema = schema
        self.warehouse = warehouse

    @property
    def scope(self) -> str:
        return f"{self.database}.{self.schema}"

    def execute(self, statement: str, params: tuple | None = None) -> list[tuple]:
        cursor = self.conn.cursor()
        cursor.execute(statement, params)
        return list(cursor.fetchall())

    def truth(self, statement: str) -> list[tuple]:
        """A hand-written answer over the TPC-H base tables."""

        self.execute(f"USE SCHEMA {TPCH}")
        return self.execute(statement)


@pytest.fixture(scope="module")
def deployment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Deployment]:
    import snowflake.connector

    from exmergo_dex_core.config import SnowflakeTarget
    from exmergo_dex_core.connect import resolve_snowflake_connection

    role = _deployer_role()
    database = os.environ["DEX_TEST_SNOWFLAKE_DATABASE"]
    warehouse = os.environ.get("DEX_TEST_SNOWFLAKE_WAREHOUSE", "DEX_CI_WH")
    params, _method = resolve_snowflake_connection(
        SnowflakeTarget(
            connection_name=os.environ.get("DEX_TEST_SNOWFLAKE_CONNECTION")
        ),
        os.environ,
        tmp_path_factory.mktemp("deployer"),
    )
    schema = f"SV_{secrets.token_hex(4).upper()}"
    conn = snowflake.connector.connect(**{**params, "role": role})
    deployed = Deployment(conn, database, schema, warehouse)
    try:
        # The deployer acts alone: no secondary role lends it dex's grants or
        # dex the deployer's.
        deployed.execute("USE SECONDARY ROLES NONE")
        deployed.execute(f'USE WAREHOUSE "{warehouse}"')
        deployed.execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(SF_MAX_SECONDS)}"
        )
        deployed.execute(f'CREATE SCHEMA "{database}".{schema}')
        for name in VIEWS:
            body = (FIXTURES / "valid" / f"{name}.yaml").read_text(encoding="utf-8")
            # verify_only first, as a maintainer deploys: a fixture Snowflake
            # refuses fails here, naming Snowflake's reason, not in a query.
            deployed.execute(
                "CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(%s, %s, TRUE)",
                (deployed.scope, body),
            )
            deployed.execute(
                "CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(%s, %s)",
                (deployed.scope, body),
            )
        # Empty: its DDL needs no warehouse, and `maintain` reads metadata.
        deployed.execute(
            f'CREATE TRANSIENT TABLE "{database}".{schema}.DRIFT_ORDERS '
            "(O_ID NUMBER, STATUS VARCHAR, AMOUNT NUMBER(12, 2))"
        )
        yield deployed
    finally:
        try:
            deployed.execute(f'DROP SCHEMA IF EXISTS "{database}".{schema}')
        finally:
            conn.close()


def _repo(
    root: Path,
    deployment: Deployment,
    files: dict[str, str],
    *,
    databases: list[str],
) -> Path:
    """A repository holding ``files`` as its Semantic View layer, read by dex's
    own identity (the suite's connection, never the deployer's)."""

    paths = []
    for name, text in files.items():
        path = root / "views" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(f"views/{name}.yaml")
    snowflake: dict[str, Any] = {
        "warehouse": deployment.warehouse,
        "databases": databases,
        "dev_database": deployment.database,
    }
    if connection := os.environ.get("DEX_TEST_SNOWFLAKE_CONNECTION"):
        snowflake["connection_name"] = connection
    config = {
        "connector": "snowflake",
        "snowflake": snowflake,
        "budget": integration_budget(),
        "semantic": {"vendor": "snowflake", "snowflake": {"files": paths}},
    }
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def _corpus(name: str) -> str:
    return (FIXTURES / "valid" / f"{name}.yaml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def layer(tmp_path_factory: pytest.TempPathFactory, deployment: Deployment) -> Path:
    """The repository whose files are exactly what was deployed."""

    return _repo(
        tmp_path_factory.mktemp("layer"),
        deployment,
        {name: _corpus(name) for name in VIEWS},
        databases=[deployment.scope, TPCH],
    )


def _cell(value: Any) -> Any:
    """One cell as both sides can agree on it: a number to six places, a date as
    its ISO day."""

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 6)
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        return value[:10]
    return value


def _rows(cells: list[list[Any]]) -> list[tuple]:
    return sorted(tuple(_cell(c) for c in row) for row in cells)


def _ask_then_run(root: Path, capsys, *argv: str) -> dict:
    """The unconfirmed call stops at the estimate; the confirmed one answers."""

    base = ["--repo-root", str(root), *argv]
    _, asked = run_cli(base, capsys)
    assert asked["status"] == "needs_confirmation", asked["errors"]
    assert asked["cost"]["estimate"] > 0
    rc, envelope = run_cli([*base, "--confirm", "--budget", BUDGET], capsys)
    assert_ok(rc, envelope)
    data = envelope["data"]
    assert (data["vendor"], data["execution"]) == ("snowflake", "dex")
    assert not [w for w in envelope["warnings"] if "cost guard" in w], envelope
    return envelope


# --- the read-back -------------------------------------------------------------------


@pytest.mark.parametrize("name", VIEWS)
def test_the_read_back_compares_clean_with_the_deployed_file(
    deployment: Deployment, name: str
):
    """Snowflake's normalization on the way back (upper-cased names,
    `access_modifier` written out, a re-inferred `data_type`) is no difference."""

    from exmergo_dex_core.semantic_views.deployed import FULL, compare, read_deployed
    from exmergo_dex_core.semantic_views.loader import load_files

    repository = load_files(FIXTURES, [f"valid/{name}.yaml"]).views[0]
    deployed_as = f"{deployment.scope}.{name.upper()}"

    def reader(fq: str) -> str:
        (row,) = deployment.execute(
            "SELECT SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW(%s)", (fq,)
        )
        return row[0]

    read = read_deployed(name, deployed_as, reader, repository)

    assert read.coverage.status == FULL, read.coverage
    assert read.view is not None, read.diagnostics
    members = [m for t in repository.tables for m in t.members()] + list(
        repository.metrics
    )
    tables = [t.name for t in repository.tables]
    assert compare(repository, read.view, members, tables) == []


def test_list_deployed_reads_every_view_with_full_coverage(layer: Path, capsys):
    rc, envelope = run_cli(
        ["--repo-root", str(layer), "explore", "semantic", "list", "--deployed"],
        capsys,
    )

    data = assert_ok(rc, envelope)["data"]
    assert data["source"] == "deployed"
    assert {c["view"]: c["coverage"] for c in data["coverage"]} == dict.fromkeys(
        VIEWS, "full"
    )
    # Names the repository also has keep its spelling.
    assert "star.orders.order_count" in {m["name"] for m in data["metrics"]}


# --- governed query and values -------------------------------------------------------

#: (dex arguments, the same question in SQL over the base tables, read in
#: `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`).
CASES = {
    "metric by dimension": (
        [
            "explore",
            "semantic",
            "query",
            "star.orders.order_count",
            "--group-by",
            "star.orders.order_status",
        ],
        "SELECT o_orderstatus, COUNT(o_orderkey) FROM ORDERS GROUP BY 1",
    ),
    "derived metric by a joined dimension": (
        [
            "explore",
            "semantic",
            "query",
            "derived_metrics.average_order_value",
            "--group-by",
            "derived_metrics.customer.market_segment",
        ],
        "SELECT c.c_mktsegment, SUM(o.o_totalprice) / COUNT(o.o_orderkey) "
        "FROM ORDERS o JOIN CUSTOMER c ON o.o_custkey = c.c_custkey "
        "GROUP BY 1",
    ),
    "grain-suffixed time dimension": (
        [
            "explore",
            "semantic",
            "query",
            "star.orders.revenue",
            "--group-by",
            "star.orders.order_date__year",
        ],
        "SELECT DATE_TRUNC('year', o_orderdate), SUM(o_totalprice) "
        "FROM ORDERS GROUP BY 1",
    ),
    "filtered": (
        [
            "explore",
            "semantic",
            "query",
            "star.orders.order_count",
            "--group-by",
            "star.customer.market_segment",
            "--where",
            "orders.order_status = 'F' AND orders.order_priority = '1-URGENT'",
        ],
        "SELECT c.c_mktsegment, COUNT(o.o_orderkey) FROM ORDERS o "
        "JOIN CUSTOMER c ON o.o_custkey = c.c_custkey "
        "WHERE o.o_orderstatus = 'F' AND o.o_orderpriority = '1-URGENT' GROUP BY 1",
    ),
    "values on a joined table, scoped to a metric": (
        [
            "explore",
            "semantic",
            "values",
            "star.customer.market_segment",
            "--metric",
            "star.orders.order_count",
        ],
        "SELECT DISTINCT c.c_mktsegment FROM ORDERS o "
        "JOIN CUSTOMER c ON o.o_custkey = c.c_custkey",
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_governed_answer_matches_sql_over_the_base_tables(
    layer: Path, deployment: Deployment, capsys, case: str
):
    argv, by_hand = CASES[case]
    envelope = _ask_then_run(layer, capsys, *argv)
    data = envelope["data"]

    assert not data.get("truncated"), data
    assert _rows(data["cells"]) == _rows(deployment.truth(by_hand))
    if argv[2] == "values":
        assert data["scoped_to"] == ["star.orders.order_count"]


# --- the stale-definition refusal ----------------------------------------------------


def test_a_repository_that_differs_from_the_deployment_is_refused_before_the_query(
    tmp_path: Path, deployment: Deployment, capsys
):
    """The repository's `revenue` is edited; the deployed one is not. The query is
    refused with both expressions named, and nothing reaches the ledger: the
    check reads the view back on the cloud-services layer, before the cost
    handshake."""

    text = _corpus("star")
    edited = text.replace(
        "expr: SUM(orders.total_price)", "expr: SUM(orders.total_price) * 2", 1
    )
    assert edited != text
    root = _repo(
        tmp_path, deployment, {"star": edited}, databases=[deployment.scope, TPCH]
    )

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(root),
            "explore",
            "semantic",
            "query",
            "star.orders.revenue",
        ],
        capsys,
    )

    assert rc == 1
    assert envelope["reason"] == "guard", envelope
    assert "orders.revenue has a different expr" in envelope["errors"][0]
    assert "* 2" in envelope["errors"][0]
    assert not (root / ".dex" / "spend.jsonl").exists()


# --- maintain: the drift loop --------------------------------------------------------


def test_a_dropped_column_is_found_cut_and_reconciled(
    tmp_path: Path, deployment: Deployment, capsys
):
    """Baseline the hand-kept view, drop the column its fact reads, and let
    `maintain` find it, propose the cut through `semantic snowflake update`, and
    read clean once it is applied. The view is never deployed: `maintain` holds
    the files to the warehouse's metadata."""

    root = _repo(
        tmp_path,
        deployment,
        {
            "drift": DRIFT_VIEW.format(
                database=deployment.database, schema=deployment.schema
            )
        },
        databases=[deployment.scope],
    )

    def dex(*argv: str) -> dict:
        rc, envelope = run_cli(["--repo-root", str(root), *argv], capsys)
        return assert_ok(rc, envelope)

    def declared(envelope: dict) -> list[dict]:
        return [
            f for f in envelope["data"]["findings"] if f["code"].startswith("declared")
        ]

    dex("explore", "map", "--confirm", "--budget", BUDGET)
    dex("maintain", "snapshot")
    assert declared(dex("maintain", "schema")) == []

    deployment.execute(
        f'ALTER TABLE "{deployment.database}".{deployment.schema}.DRIFT_ORDERS '
        "DROP COLUMN AMOUNT"
    )
    dex("explore", "map", "--refresh", "--confirm", "--budget", BUDGET)
    found = declared(dex("maintain", "schema"))

    codes = {f["code"] for f in found}
    assert "declared_column_missing" in codes, found
    assert any("amount" in str(f["data"]).lower() for f in found), found

    reconciled = dex("maintain", "reconcile")
    plan_id = reconciled["data"]["semantic_plan_id"]
    assert plan_id, reconciled["data"]
    dex("transform", "apply", plan_id)

    body = yaml.safe_load((root / "views" / "drift.yaml").read_text(encoding="utf-8"))
    (orders,) = body["tables"]
    assert not orders.get("facts")
    assert [m["name"] for m in orders["metrics"]] == ["order_count"]
    assert declared(dex("maintain", "schema")) == []
