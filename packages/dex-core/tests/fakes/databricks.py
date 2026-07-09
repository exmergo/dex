"""Stateful fakes of the two Databricks surfaces dex uses.

Behavioral, not mocks, mirroring the Snowflake fake's contract: the SDK fake
serves Unity Catalog metadata (catalogs, schemas, tables, columns, warehouse
facts) from a table registry and counts every REST call; the DBAPI fake
records every statement in order, simulates per-statement duration by
advancing an injectable clock, tracks the ``SET STATEMENT_TIMEOUT`` session
parameter, serves ``DESCRIBE DETAIL`` from the registry, and enforces the
statement timeout the way the service does: a real ``ServerOperationError``
with the live service's message shape, raised at execute, with elapsed capped
at the timeout (a killed statement still bills what ran). The SQL connection
factory counts how often it is invoked so tests can assert the lazy-open
invariant: free metadata paths must never build a SQL session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from databricks.sql import exc as dbsql_exc

# Statements with no registered duration run this long on the fake clock.
DEFAULT_STATEMENT_SECONDS = 0.5


class FakeClock:
    """The adapter's injectable clock; the fake connection advances it by each
    statement's simulated duration."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeDatabricksTable:
    catalog: str
    schema: str
    name: str
    # (column_name, type_text, nullable), type_text as Unity Catalog reports
    # it, e.g. "bigint", "string", "array<string>", "struct<a:int>".
    columns: list[tuple[str, str, bool]]
    rows: int = 0
    bytes: int = 0
    kind: str = "table"

    @property
    def identifier(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"

    @property
    def quoted(self) -> str:
        return ".".join(f"`{part}`" for part in (self.catalog, self.schema, self.name))


@dataclass
class FakeWarehouse:
    id: str = "fake-wh"
    name: str = "DEX Fake Warehouse"
    cluster_size: str = "2X-Small"
    state: str = "STOPPED"
    enable_serverless_compute: bool = True


@dataclass
class FakeStatement:
    sql: str
    session_timeout: int | None


@dataclass
class FakeResult:
    """What one executed SELECT returns: dict rows keyed by (lowercase) alias,
    plus how long the statement 'runs' on the fake clock."""

    rows: list[dict]
    seconds: float = DEFAULT_STATEMENT_SECONDS


class FakeWorkspaceClient:
    """The Unity Catalog REST surface the adapter touches (and nothing more):
    ``catalogs.list``, ``schemas.list``, ``tables.list``, ``tables.get``, and
    ``warehouses.get``. ``omit_list_columns`` reproduces the shared-catalog
    behavior seen live (the samples catalog): the list call carries no
    columns, only the per-table GET does."""

    def __init__(
        self,
        *,
        tables: list[FakeDatabricksTable] | None = None,
        warehouse: FakeWarehouse | None = None,
        omit_list_columns: bool = False,
    ):
        self._tables = tables or []
        self.warehouse = warehouse or FakeWarehouse()
        self.omit_list_columns = omit_list_columns
        self.metadata_calls: list[str] = []
        self.catalogs = SimpleNamespace(list=self._list_catalogs)
        self.schemas = SimpleNamespace(list=self._list_schemas)
        self.tables = SimpleNamespace(list=self._list_tables, get=self._get_table)
        self.warehouses = SimpleNamespace(get=self._get_warehouse)

    def _list_catalogs(self):
        self.metadata_calls.append("catalogs.list")
        names = sorted({t.catalog for t in self._tables})
        return [SimpleNamespace(name=name) for name in names]

    def _list_schemas(self, catalog_name: str):
        self.metadata_calls.append(f"schemas.list:{catalog_name}")
        names = sorted({t.schema for t in self._tables if t.catalog == catalog_name})
        return [SimpleNamespace(name=name) for name in names]

    def _list_tables(
        self, catalog_name: str, schema_name: str, *, include_browse: bool = False
    ):
        self.metadata_calls.append(f"tables.list:{catalog_name}.{schema_name}")
        return [
            self._table_info(t, with_columns=not self.omit_list_columns)
            for t in sorted(self._tables, key=lambda t: t.identifier)
            if t.catalog == catalog_name and t.schema == schema_name
        ]

    def _get_table(self, full_name: str, *, include_browse: bool = False):
        self.metadata_calls.append(f"tables.get:{full_name}")
        for t in self._tables:
            if t.identifier == full_name:
                return self._table_info(t, with_columns=True)
        raise KeyError(f"table not found: {full_name}")

    def _get_warehouse(self, warehouse_id: str):
        self.metadata_calls.append(f"warehouses.get:{warehouse_id}")
        if warehouse_id != self.warehouse.id:
            raise KeyError(f"warehouse not found: {warehouse_id}")
        return self.warehouse

    @staticmethod
    def _table_info(t: FakeDatabricksTable, *, with_columns: bool):
        columns = (
            [
                SimpleNamespace(
                    name=name, type_text=type_text, nullable=nullable, position=i
                )
                for i, (name, type_text, nullable) in enumerate(t.columns)
            ]
            if with_columns
            else None
        )
        return SimpleNamespace(
            full_name=t.identifier,
            name=t.name,
            catalog_name=t.catalog,
            schema_name=t.schema,
            table_type="VIEW" if t.kind == "view" else "MANAGED",
            columns=columns,
        )


class FakeCursor:
    def __init__(self, conn: FakeDatabricksConnection):
        self._conn = conn
        self._rows: list[tuple] = []
        self.description: list[tuple] = []
        self.query_id = None

    def execute(self, sql: str):
        self._conn.execute_count += 1
        self.query_id = f"fake-query-{self._conn.execute_count}"
        stripped = sql.strip()
        upper = stripped.upper()

        if upper.startswith("SET "):
            self._record(sql)
            self._apply_session_parameter(stripped)
            return self
        if upper.startswith("DESCRIBE DETAIL"):
            self._record(sql)
            self._serve_detail(stripped)
            return self

        # A data statement: simulate duration, enforce the session timeout the
        # way the service does (kill and bill what ran).
        result = self._conn.resolve(sql)
        self._record(sql)
        session_timeout = self._conn.session_parameters.get("STATEMENT_TIMEOUT")
        seconds = result.seconds
        if self._conn.startup_pending:
            # The first statement on a stopped warehouse pays the wake.
            seconds += self._conn.startup_seconds
            self._conn.startup_pending = False
        if session_timeout is not None and seconds > session_timeout:
            self._conn.clock.now += float(session_timeout)
            raise dbsql_exc.ServerOperationError(
                f"Statement has timed out after {session_timeout} seconds."
            )
        self._conn.clock.now += seconds
        keys = list(result.rows[0].keys()) if result.rows else []
        self.description = [
            (key, "string", None, None, None, None, True) for key in keys
        ]
        self._rows = [tuple(row[key] for key in keys) for row in result.rows]
        return self

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchmany(self, size: int) -> list[tuple]:
        return list(self._rows[:size])

    # --- internals -------------------------------------------------------------

    def _record(self, sql: str) -> None:
        self._conn.statements.append(
            FakeStatement(
                sql=sql,
                session_timeout=self._conn.session_parameters.get("STATEMENT_TIMEOUT"),
            )
        )

    def _apply_session_parameter(self, sql: str) -> None:
        # SET <PARAM> = <value>
        try:
            key, value = sql[4:].split("=", 1)
            key = key.strip().upper()
            value = value.strip().strip("'")
            self._conn.session_parameters[key] = (
                int(value) if value.isdigit() else value
            )
        except (IndexError, ValueError):
            pass

    def _serve_detail(self, sql: str) -> None:
        if self._conn.detail_error:
            self._conn.clock.now += DEFAULT_STATEMENT_SECONDS
            raise dbsql_exc.ServerOperationError(
                "DESCRIBE DETAIL is not supported for this table."
            )
        ident = sql[len("DESCRIBE DETAIL") :].strip().replace("`", "")
        for t in self._conn.tables:
            if t.identifier.lower() == ident.lower():
                self._conn.clock.now += DEFAULT_STATEMENT_SECONDS
                self.description = [
                    ("format", "string", None, None, None, None, True),
                    ("numRows", "bigint", None, None, None, None, True),
                    ("sizeInBytes", "bigint", None, None, None, None, True),
                ]
                self._rows = [("delta", t.rows, t.bytes)]
                return
        raise dbsql_exc.ServerOperationError(f"Table not found: {ident}")


class FakeDatabricksConnection:
    """Simulates exactly the DBAPI surface the adapter touches; anything else
    raises AttributeError, which is the point (the adapter must not grow calls
    the fake does not vouch for)."""

    def __init__(
        self,
        *,
        tables: list[FakeDatabricksTable] | None = None,
        clock: FakeClock | None = None,
        row_resolver: Callable[[str], FakeResult | list[dict]] | None = None,
        startup_seconds: float = 10.0,
        startup_pending: bool = True,
        detail_error: bool = False,
    ):
        self.tables = tables or []
        self.clock = clock or FakeClock()
        self.row_resolver = row_resolver
        self.startup_seconds = startup_seconds
        # Set when the fake warehouse starts stopped: the first data statement
        # pays the wake on the clock.
        self.startup_pending = startup_pending
        self.detail_error = detail_error
        self.statements: list[FakeStatement] = []
        self.session_parameters: dict[str, Any] = {}
        self.execute_count = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def resolve(self, sql: str) -> FakeResult:
        if self.row_resolver is None:
            return FakeResult(rows=[])
        resolved = self.row_resolver(sql)
        return resolved if isinstance(resolved, FakeResult) else FakeResult(resolved)

    def close(self) -> None:
        self.closed = True

    # --- convenience for assertions ---------------------------------------------

    @property
    def data_statements(self) -> list[FakeStatement]:
        return [
            s for s in self.statements if s.sql.strip().upper().startswith("SELECT")
        ]


@dataclass
class FakeDatabricks:
    """The pair of fakes one adapter consumes, plus the counting connection
    factory: ``connect_count`` is how many SQL sessions were opened, which the
    lazy-open tests assert stays 0 on free paths."""

    workspace: FakeWorkspaceClient
    connection: FakeDatabricksConnection
    connect_count: int = 0
    tables: list[FakeDatabricksTable] = field(default_factory=list)

    def sql_connect(self):
        self.connect_count += 1
        return self.connection

    @property
    def clock(self) -> FakeClock:
        return self.connection.clock
