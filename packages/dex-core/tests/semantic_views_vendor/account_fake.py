"""The fake Snowflake account the governed Semantic View tests run against.

Shared by the `local` deployment's tests (`test_query.py`, #1290) and the
`snowflake_account` deployment's (`test_account.py`, #1357): base tables, the
semantic views deployed on them, and what each statement returns, behind the real
adapter and cost gate through dex's one adapter funnel.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fakes.snowflake import (
    FakeResult,
    FakeSnowflakeConnection,
    FakeSnowflakeTable,
    FakeWarehouse,
)

from exmergo_dex_core import connect as connect_mod

from .conftest import relationship, sv, table

GB = 1_000_000_000

SALES = sv(
    "sales",
    table(
        "orders",
        "SHOP.PUBLIC.ORDERS",
        ["o_orderkey"],
        dimensions=[
            {"name": "status", "expr": "o_orderstatus"},
            {"name": "priority", "expr": "o_orderpriority"},
        ],
        time_dimensions=[
            {"name": "order_date", "expr": "o_orderdate", "data_type": "DATE"}
        ],
        facts=[
            {"name": "total_price", "expr": "o_totalprice"},
            {
                "name": "discount",
                "expr": "o_discount",
                "access_modifier": "private_access",
            },
        ],
        metrics=[
            {"name": "order_count", "expr": "COUNT(o_orderkey)"},
            {"name": "revenue", "expr": "SUM(orders.total_price)"},
            {"name": "biggest", "expr": "MAX(o_totalprice)"},
            {
                "name": "gross",
                "expr": "SUM(orders.discount)",
                "access_modifier": "private_access",
            },
            {
                "name": "running",
                "expr": "SUM(orders.total_price) OVER (PARTITION BY "
                "EXCLUDING orders.order_date ORDER BY orders.order_date)",
            },
        ],
    ),
    table(
        "customers",
        "SHOP.PUBLIC.CUSTOMERS",
        ["c_custkey"],
        dimensions=[
            {"name": "segment", "expr": "c_mktsegment"},
            {"name": "email", "expr": "c_email"},
            {"name": "contact", "expr": "c_ref"},
        ],
        facts=[{"name": "total_price", "expr": "c_acctbal"}],
        metrics=[
            {"name": "customer_count", "expr": "COUNT(c_custkey)"},
            {"name": "last_email", "expr": "MAX(customers.email)"},
            {"name": "top_ref", "expr": "MAX(c_ref)"},
        ],
    ),
    table(
        "nations",
        "SHOP.PUBLIC.NATIONS",
        ["n_nationkey"],
        dimensions=[{"name": "country", "expr": "n_name"}],
    ),
    relationships=[
        relationship(
            "orders_customers", "orders", "customers", ("o_custkey", "c_custkey")
        ),
        relationship(
            "customers_nations", "customers", "nations", ("c_nationkey", "n_nationkey")
        ),
    ],
    metrics=[
        {
            "name": "orders_per_customer",
            "expr": "orders.order_count / customers.customer_count",
        }
    ],
)


def _deployed_yaml(view: dict) -> str:
    """What `SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW` returns: names upper-cased,
    `access_modifier` written out, `data_type` re-inferred."""

    def upper(entry: dict) -> dict:
        found = dict(entry, name=entry["name"].upper())
        found.setdefault("access_modifier", "public_access")
        found.setdefault("data_type", "VARCHAR(16777216)")
        return found

    tables = []
    for t in view["tables"]:
        copy = dict(t, name=t["name"].upper())
        for kind in ("dimensions", "time_dimensions", "facts", "metrics"):
            if kind in t:
                copy[kind] = [upper(m) for m in t[kind]]
        tables.append(copy)
    return yaml.safe_dump(
        dict(view, name=view["name"].upper(), tables=tables), sort_keys=True
    )


class Account:
    """The fake account: tables, the semantic views deployed on them, and what
    each SELECT returns.

    ``deployed`` is the one view at ``SHOP.PUBLIC.<NAME>`` the `local` tests
    hold the repository to; ``views`` adds more, keyed by ``DB.SCHEMA.NAME``
    (#1357). ``unreadable`` names views whose read-back is refused;
    ``readable=False`` refuses every one.
    """

    def __init__(
        self,
        deployed: dict | None = SALES,
        *,
        readable: bool = True,
        views: dict[str, dict] | None = None,
        unreadable: set[str] | None = None,
    ):
        self.deployed = deployed
        self.readable = readable
        self.unreadable = {name.upper() for name in unreadable or ()}
        self.views: dict[str, dict] = {}
        if deployed is not None:
            self.views[f"SHOP.PUBLIC.{deployed['name'].upper()}"] = deployed
        self.views.update({fq.upper(): view for fq, view in (views or {}).items()})
        self.rows: list[dict] = [{"c0": "F", "c1": 7}]
        tables = [
            FakeSnowflakeTable(
                "SHOP",
                "PUBLIC",
                "ORDERS",
                [("O_ORDERKEY", "FIXED", False)],
                rows=1000,
                bytes=40 * GB,
            ),
            FakeSnowflakeTable(
                "SHOP",
                "PUBLIC",
                "CUSTOMERS",
                [("C_CUSTKEY", "FIXED", False)],
                rows=100,
                bytes=2 * GB,
            ),
            FakeSnowflakeTable(
                "SHOP",
                "PUBLIC",
                "NATIONS",
                [("N_NATIONKEY", "FIXED", False)],
                rows=25,
                bytes=1_000,
            ),
        ]
        for fq in self.views:
            database, schema, name = fq.split(".")
            tables.append(
                FakeSnowflakeTable(database, schema, name, [], kind="semantic_view")
            )
        self.connection = FakeSnowflakeConnection(
            tables=tables,
            warehouses=[FakeWarehouse(name="DEX_WH", state="STARTED")],
            row_resolver=self.resolve,
        )

    def resolve(self, sql: str):
        from snowflake.connector import errors as sf_errors

        if "SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW" in sql:
            fq = self._read_target(sql)
            if not self.readable or fq in self.unreadable:
                raise sf_errors.ProgrammingError(
                    msg="003001 (42501): SQL access control error: Insufficient "
                    f"privileges to operate on semantic view '{fq.rsplit('.', 1)[-1]}'",
                    errno=3001,
                )
            return FakeResult(
                rows=[{"y": _deployed_yaml(self.views[fq])}], wakes_warehouse=False
            )
        return FakeResult(rows=list(self.rows), seconds=2.0)

    def _read_target(self, sql: str) -> str:
        """The ``DB.SCHEMA.NAME`` a read-back names, as the adapter quotes it."""
        quoted = sql.split("SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW('", 1)[1]
        quoted = quoted.split("')", 1)[0]
        return ".".join(part.strip('"') for part in quoted.split('"."')).upper()

    @property
    def reads(self) -> list[str]:
        """Each read-back's ``DB.SCHEMA.NAME``, in order."""
        return [self._read_target(s) for s in self.sent if "READ_YAML" in s]

    @property
    def sent(self) -> list[str]:
        return [s.sql for s in self.connection.statements]

    @property
    def queries(self) -> list[str]:
        return [s for s in self.sent if "SEMANTIC_VIEW(" in s and "SYSTEM$" not in s]


def route_to_fake_account(monkeypatch) -> dict:
    """Route dex's one adapter funnel at the fake account; the `account`
    fixture (`conftest.py`). Swap ``holder["account"]`` to change the account."""

    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter
    from exmergo_dex_core.config import load_config
    from exmergo_dex_core.storage import FilesystemStore

    holder: dict[str, Account] = {"account": Account()}

    def opener(**opened):
        # The engine's own store and repository, never the working directory:
        # the spend ledger belongs to the test's repository.
        root = Path(opened["repo_root"])
        config = opened.get("config") or load_config(root)
        store = opened.get("store") or FilesystemStore(root)
        fake = holder["account"]
        return SnowflakeAdapter(
            connection=fake.connection,
            cost_gate=connect_mod.new_cost_gate(
                "snowflake",
                config,
                store,
                budget=opened.get("budget"),
                confirmed=opened.get("confirmed", False),
                command=opened.get("command"),
            ),
            target=config.snowflake,
            scope_override=opened.get("scopes"),
            clock=fake.connection.clock,
            wall_clock=fake.connection.clock.wall,
        )

    monkeypatch.setattr(connect_mod, "open_adapter", opener)
    return holder
