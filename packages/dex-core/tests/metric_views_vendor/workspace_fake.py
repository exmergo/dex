"""The fake Databricks workspace the governed Metric View tests run against (#1362).

Base tables in `shop.core`, the Metric Views deployed on them (listed by Unity
Catalog as `METRIC_VIEW`, never as relations), and what each statement returns,
behind the real adapter and cost gate through dex's one adapter funnel.
"""

from __future__ import annotations

from pathlib import Path

from fakes.databricks import (
    FakeDatabricks,
    FakeDatabricksConnection,
    FakeDatabricksTable,
    FakeResult,
    FakeWarehouse,
    FakeWorkspaceClient,
)

from exmergo_dex_core import connect as connect_mod

from .conftest import view

GB = 1_000_000_000

#: A star on TPC-H orders: a customer join with a nested nation join (a
#: snowflake chain), a view `filter`, a ratio, a derived measure, a measure that
#: returns a column's values, and a window measure.
ORDERS = view(
    "shop.core.orders",
    comment="One row per order.",
    filter="o_orderstatus != 'X'",
    joins=[
        {
            "name": "customer",
            "source": "shop.core.customer",
            "on": "source.o_custkey = customer.c_custkey",
            "rely": {"at_most_one_match": True},
            "joins": [
                {
                    "name": "nation",
                    "source": "shop.core.nation",
                    "on": "customer.c_nationkey = nation.n_nationkey",
                }
            ],
        }
    ],
    fields=[
        {"name": "order_status", "expr": "o_orderstatus"},
        {
            "name": "order_date",
            "expr": "o_orderdate",
            "format": {"type": "date", "date_format": "year_month_day"},
        },
        {"name": "priority", "expr": "o_orderpriority"},
        {"name": "segment", "expr": "customer.c_mktsegment"},
        {"name": "nation", "expr": "customer.nation.n_name"},
        {"name": "customer_email", "expr": "customer.c_email"},
        # Computed over two tables, so the catalog's lineage traces neither:
        # the gate screens every column the expression reads.
        {
            "name": "place",
            "expr": "CONCAT(customer.c_mktsegment, '/', customer.nation.n_regionkey)",
        },
        {
            "name": "contact",
            "expr": "CONCAT(customer.c_email, '/', customer.nation.n_name)",
        },
    ],
    measures=[
        {"name": "order_count", "expr": "COUNT(1)"},
        {"name": "revenue", "expr": "SUM(o_totalprice)"},
        {"name": "aov", "expr": "MEASURE(revenue) / MEASURE(order_count)"},
        {"name": "net", "expr": "MEASURE(revenue) - SUM(o_discount)"},
        {"name": "biggest", "expr": "MAX(o_totalprice)"},
        {"name": "last_email", "expr": "MAX(customer.c_email)"},
        {"name": "latest_email", "expr": "MEASURE(last_email)"},
        {
            "name": "running",
            "expr": "SUM(o_totalprice)",
            "window": [{"order": "order_date", "range": "cumulative"}],
        },
        {"name": "running_share", "expr": "MEASURE(running) / MEASURE(revenue)"},
    ],
)

#: A view whose query has to bind a parameter.
PARAMS = view(
    "shop.core.orders",
    parameters=[{"name": "region", "data_type": "string", "default": "'EU'"}],
    fields=[{"name": "order_status", "expr": "o_orderstatus"}],
    measures=[{"name": "order_count", "expr": "COUNT(1)"}],
)

#: A view Databricks may answer from a materialization.
MATERIALIZED = view(
    "shop.core.orders",
    materialization={
        "mode": "relaxed",
        "materialized_views": [
            {"name": "daily", "type": "aggregated", "dimensions": ["order_status"]}
        ],
    },
    fields=[{"name": "order_status", "expr": "o_orderstatus"}],
    measures=[{"name": "order_count", "expr": "COUNT(1)"}],
)

#: A view over a table outside the allowlist.
ELSEWHERE = view(
    "other.core.ledger",
    fields=[{"name": "kind", "expr": "kind"}],
    measures=[{"name": "entries", "expr": "COUNT(1)"}],
)


class Workspace:
    """The fake workspace: base tables, the Metric Views deployed on them, and
    what each SELECT returns.

    ``deployed`` names each Metric View by ``catalog.schema.name``.
    """

    def __init__(
        self,
        deployed: tuple[str, ...] = ("shop.metrics.orders",),
        *,
        running: bool = False,
    ):
        self.rows: list[dict] = [{"orders.order_status": "F", "orders.order_count": 7}]
        tables = [
            FakeDatabricksTable(
                "shop",
                "core",
                "orders",
                [("o_orderkey", "bigint", False), ("o_orderstatus", "string", True)],
                rows=1000,
                bytes=40 * GB,
            ),
            FakeDatabricksTable(
                "shop",
                "core",
                "customer",
                [("c_custkey", "bigint", False), ("c_email", "string", True)],
                rows=100,
                bytes=2 * GB,
            ),
            FakeDatabricksTable(
                "shop",
                "core",
                "nation",
                [("n_nationkey", "bigint", False)],
                rows=25,
                bytes=1_000,
            ),
        ]
        for fq in deployed:
            catalog, schema, name = fq.split(".")
            tables.append(
                FakeDatabricksTable(catalog, schema, name, [], kind="metric_view")
            )
        self.workspace = FakeWorkspaceClient(
            tables=tables,
            warehouse=FakeWarehouse(
                id="fake-wh", state="RUNNING" if running else "STOPPED"
            ),
        )
        self.connection = FakeDatabricksConnection(
            tables=tables, row_resolver=self.resolve, startup_pending=not running
        )
        self.fake = FakeDatabricks(
            workspace=self.workspace, connection=self.connection, tables=tables
        )

    def resolve(self, _sql: str) -> FakeResult:
        return FakeResult(rows=list(self.rows), seconds=2.0)

    @property
    def queries(self) -> list[str]:
        """Every SELECT sent, in order."""
        return [s.sql for s in self.connection.data_statements]

    @property
    def statements(self) -> list[str]:
        """Every statement sent, session parameters included."""
        return [s.sql for s in self.connection.statements]


def route_to_fake_workspace(monkeypatch) -> dict:
    """Route dex's one adapter funnel at the fake workspace. Swap
    ``holder["workspace"]`` to change it."""

    from exmergo_dex_core.adapters.databricks import DatabricksAdapter
    from exmergo_dex_core.config import DatabricksTarget, load_config
    from exmergo_dex_core.storage import FilesystemStore

    holder: dict[str, Workspace] = {"workspace": Workspace()}

    def opener(**opened):
        root = Path(opened["repo_root"])
        config = opened.get("config") or load_config(root)
        store = opened.get("store") or FilesystemStore(root)
        found = holder["workspace"]
        target = connect_mod.narrow_target(
            config.databricks or DatabricksTarget(),
            "databricks",
            opened.get("scopes"),
        )
        return DatabricksAdapter(
            workspace=found.workspace,
            sql_connect=found.fake.sql_connect,
            cost_gate=connect_mod.new_cost_gate(
                "databricks",
                config,
                store,
                budget=opened.get("budget"),
                confirmed=opened.get("confirmed", False),
                command=opened.get("command"),
            ),
            target=target,
            host="test.cloud.databricks.com",
            auth_method="default_profile:oauth_user",
            clock=found.connection.clock,
        )

    monkeypatch.setattr(connect_mod, "open_adapter", opener)
    return holder
