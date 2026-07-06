"""A stateful fake of the psycopg connection surface dex uses.

Behavioral, not a mock: it records every statement in order, serves the
``pg_catalog`` lookups (inventory, columns, ``pg_stats``, namespace scope, the
capabilities probe) from a table registry, answers ``EXPLAIN (FORMAT JSON)``
with a plan cost derived from the referenced tables' sizes (overridable per
test), simulates per-statement duration by advancing an injectable clock (the
adapter measures wall-clock elapsed through the same clock, so timing
assertions are deterministic), tracks the session parameters the adapter sets,
and enforces ``statement_timeout`` the way the server does: a real
``psycopg.errors.QueryCanceled`` raised at execute, with the elapsed time
capped at the timeout (a killed statement still bills what ran). Tests assert
against observable behavior (statement ordering, session state, ledger
effects), not call signatures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from psycopg import errors as pg_errors

# Statements with no registered duration run this long on the fake clock.
DEFAULT_STATEMENT_SECONDS = 0.2

_PLANNER_PAGE_BYTES = 8192


class FakeClock:
    """The adapter's injectable clock; the fake connection advances it by each
    statement's simulated duration."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeColumnDescription:
    name: str
    type_code: int = 25  # text


@dataclass
class FakePostgresTable:
    schema: str
    name: str
    # (column_name, format_type output, nullable), e.g. ("id", "bigint", False)
    columns: list[tuple[str, str, bool]]
    reltuples: float = -1.0
    total_bytes: int = 0
    kind: str = "table"  # "table" | "view" | "materialized_view"
    # attname -> pg_stats.n_distinct (positive = count, negative = ratio)
    stats: dict[str, float] = field(default_factory=dict)

    @property
    def relkind(self) -> str:
        return {"table": "r", "view": "v", "materialized_view": "m"}[self.kind]


@dataclass
class FakeStatement:
    sql: str
    session_timeout_ms: int | None


@dataclass
class FakeResult:
    """What one executed SELECT returns: dict rows keyed by alias, plus how
    long the statement 'runs' on the fake clock."""

    rows: list[dict]
    seconds: float = DEFAULT_STATEMENT_SECONDS


class FakeCursor:
    def __init__(self, conn: FakePostgresConnection):
        self._conn = conn
        self._rows: list[tuple] = []
        self.description: list[FakeColumnDescription] = []

    def execute(self, sql: str):
        stripped = sql.strip()
        upper = stripped.upper()
        self._record(sql)

        if upper.startswith("SET "):
            self._apply_session_parameter(stripped)
            return self
        if upper.startswith("EXPLAIN"):
            inner = stripped.split(")", 1)[1].strip()
            cost = self._conn.plan_cost(inner)
            self._emit([{"QUERY PLAN": [{"Plan": {"Total Cost": cost}}]}])
            return self
        if "pg_catalog." in sql or "current_database()" in sql:
            self._serve_catalog(stripped)
            return self

        # A data statement: simulate duration, enforce the session timeout the
        # way the server does (kill and bill what ran).
        result = self._conn.resolve(sql)
        timeout_ms = self._conn.session_parameters.get("statement_timeout")
        if timeout_ms is not None and result.seconds * 1000 > float(timeout_ms):
            self._conn.clock.now += float(timeout_ms) / 1000.0
            raise pg_errors.QueryCanceled(
                "canceling statement due to statement timeout"
            )
        self._conn.clock.now += result.seconds
        self._emit(result.rows)
        return self

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchmany(self, size: int) -> list[tuple]:
        return list(self._rows[:size])

    # --- internals -------------------------------------------------------------

    def _record(self, sql: str) -> None:
        timeout = self._conn.session_parameters.get("statement_timeout")
        self._conn.statements.append(
            FakeStatement(
                sql=sql,
                session_timeout_ms=int(timeout) if timeout is not None else None,
            )
        )

    def _apply_session_parameter(self, sql: str) -> None:
        # SET <param> = <value>
        try:
            assignment = sql.split(" ", 1)[1]
            key, value = assignment.split("=", 1)
            key = key.strip().lower()
            value = value.strip().strip("'")
            self._conn.session_parameters[key] = (
                int(value) if value.lstrip("-").isdigit() else value
            )
        except (IndexError, ValueError):
            pass

    def _serve_catalog(self, sql: str) -> None:
        if "current_database()" in sql and "server_version" in sql:
            read_only = self._conn.session_parameters.get(
                "default_transaction_read_only", "off"
            )
            self._emit(
                [
                    {
                        "db": self._conn.database,
                        "server_version": self._conn.server_version,
                        "read_only": str(read_only),
                    }
                ]
            )
        elif "current_database()" in sql:
            self._emit([{"db": self._conn.database}])
        elif "pg_catalog.pg_stats" in sql:
            table = self._find_stats_table(sql)
            rows = (
                [
                    {"attname": name, "n_distinct": value}
                    for name, value in sorted(table.stats.items())
                ]
                if table is not None
                else []
            )
            self._emit(rows, columns=["attname", "n_distinct"])
        elif "pg_catalog.pg_attribute" in sql:
            rows = []
            for t in sorted(self._conn.tables, key=lambda t: (t.schema, t.name)):
                for col, data_type, nullable in t.columns:
                    rows.append(
                        {
                            "schema_name": t.schema,
                            "object_name": t.name,
                            "column_name": col,
                            "data_type": data_type,
                            "nullable": nullable,
                        }
                    )
            self._emit(rows)
        elif "pg_catalog.pg_class" in sql:
            rows = [
                {
                    "schema_name": t.schema,
                    "object_name": t.name,
                    "kind": t.relkind,
                    "reltuples": t.reltuples,
                    "total_bytes": t.total_bytes,
                }
                for t in sorted(self._conn.tables, key=lambda t: (t.schema, t.name))
            ]
            self._emit(rows)
        elif "pg_catalog.pg_namespace" in sql:
            names = sorted({t.schema for t in self._conn.tables})
            self._emit([{"nspname": name} for name in names])
        else:
            self._emit([])

    def _find_stats_table(self, sql: str) -> FakePostgresTable | None:
        for t in self._conn.tables:
            if f"schemaname = '{t.schema}'" in sql and f"tablename = '{t.name}'" in sql:
                return t
        return None

    def _emit(self, rows: list[dict], columns: list[str] | None = None) -> None:
        keys = columns or (list(rows[0].keys()) if rows else ["name"])
        self.description = [FakeColumnDescription(name=key) for key in keys]
        self._rows = [tuple(row[key] for key in keys) for row in rows]


class FakePostgresConnection:
    """Simulates exactly the connection surface the adapter touches; anything
    else raises AttributeError, which is the point (the adapter must not grow
    calls the fake does not vouch for)."""

    def __init__(
        self,
        *,
        tables: list[FakePostgresTable] | None = None,
        database: str = "dexdb",
        server_version: str = "16.4",
        clock: FakeClock | None = None,
        row_resolver: Callable[[str], FakeResult | list[dict]] | None = None,
        plan_costs: Callable[[str], float | None] | None = None,
    ):
        self.tables = tables or []
        self.database = database
        self.server_version = server_version
        self.clock = clock or FakeClock()
        self.row_resolver = row_resolver
        self.plan_costs = plan_costs
        self.statements: list[FakeStatement] = []
        self.session_parameters: dict[str, object] = {}
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def resolve(self, sql: str) -> FakeResult:
        if self.row_resolver is None:
            return FakeResult(rows=[])
        resolved = self.row_resolver(sql)
        return resolved if isinstance(resolved, FakeResult) else FakeResult(resolved)

    def plan_cost(self, sql: str) -> float:
        """The Total Cost EXPLAIN reports for ``sql``: a per-test override, or
        the referenced tables' page counts (the planner's own calibration)."""

        if self.plan_costs is not None:
            override = self.plan_costs(sql)
            if override is not None:
                return override
        pages = sum(
            t.total_bytes / _PLANNER_PAGE_BYTES for t in self.tables if t.name in sql
        )
        return pages if pages > 0 else 100.0

    def close(self) -> None:
        self.closed = True

    # --- convenience for assertions ---------------------------------------------

    @property
    def data_statements(self) -> list[FakeStatement]:
        """Statements that scan data: SELECTs that are not catalog lookups,
        the capabilities probe, or EXPLAIN plan requests."""

        return [
            s
            for s in self.statements
            if s.sql.strip().upper().startswith("SELECT")
            and "pg_catalog." not in s.sql
            and "current_database()" not in s.sql
        ]
