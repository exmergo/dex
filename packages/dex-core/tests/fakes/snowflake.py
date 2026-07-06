"""A stateful fake of the snowflake-connector-python surface dex uses.

Behavioral, not a mock: it records every statement in order, serves SHOW
metadata from a table registry, simulates per-statement duration by advancing
an injectable clock (the adapter measures wall-clock elapsed through the same
clock, so timing assertions are deterministic), tracks the session parameters
the adapter sets, and enforces ``STATEMENT_TIMEOUT_IN_SECONDS`` the way the
service does: a real ``ProgrammingError`` naming the statement timeout, raised
at execute, with the elapsed time capped at the timeout (a killed statement
still bills what ran). Tests assert against observable behavior (statement
ordering, session state, ledger effects), not call signatures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from snowflake.connector import errors as sf_errors

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
class FakeSnowflakeTable:
    database: str
    schema: str
    name: str
    # (column_name, type_token, nullable); type_token as in SHOW COLUMNS
    # data_type JSON, e.g. "FIXED", "TEXT", "VARIANT".
    columns: list[tuple[str, str, bool]]
    rows: int = 0
    bytes: int = 0
    kind: str = "table"

    @property
    def identifier(self) -> str:
        return f"{self.database}.{self.schema}.{self.name}"

    @property
    def quoted(self) -> str:
        return ".".join(f'"{part}"' for part in (self.database, self.schema, self.name))


@dataclass
class FakeWarehouse:
    name: str
    size: str = "X-Small"
    state: str = "SUSPENDED"
    resource_constraint: str = ""


@dataclass
class FakeStatement:
    sql: str
    timeout: int | None
    session_timeout: int | None


@dataclass
class FakeResult:
    """What one executed SELECT returns: dict rows keyed by (lowercase) alias,
    plus how long the statement 'runs' on the fake clock."""

    rows: list[dict]
    seconds: float = DEFAULT_STATEMENT_SECONDS


class FakeCursor:
    def __init__(self, conn: FakeSnowflakeConnection):
        self._conn = conn
        self._rows: list[tuple] = []
        self.description: list[tuple] = []
        self.sfqid = None

    def execute(self, sql: str, timeout: int | None = None):
        self._conn.execute_count += 1
        self.sfqid = f"fake-query-{self._conn.execute_count}"
        upper = sql.strip().upper()

        if upper.startswith("ALTER SESSION SET"):
            self._record(sql, timeout)
            self._apply_session_parameter(sql)
            return self
        if upper.startswith("USE "):
            self._record(sql, timeout)
            self._conn.used.append(sql)
            return self
        if upper.startswith("SHOW "):
            self._record(sql, timeout)
            self._serve_show(sql)
            return self

        # A data statement: simulate duration, enforce the session timeout the
        # way the service does (kill and bill what ran).
        result = self._conn.resolve(sql)
        self._record(sql, timeout)
        session_timeout = self._conn.session_parameters.get(
            "STATEMENT_TIMEOUT_IN_SECONDS"
        )
        seconds = result.seconds
        if self._conn.warehouse_resumes_pending:
            # First data statement after a suspended start pays the resume.
            seconds += self._conn.resume_seconds
            self._conn.warehouse_resumes_pending = False
        if session_timeout is not None and seconds > session_timeout:
            self._conn.clock.now += float(session_timeout)
            raise sf_errors.ProgrammingError(
                msg=(
                    f"Statement reached its statement or warehouse timeout of "
                    f"{session_timeout} second(s) and was canceled."
                ),
                errno=604,
            )
        if timeout is not None and seconds > timeout:
            self._conn.clock.now += float(timeout)
            raise sf_errors.ProgrammingError(msg="SQL execution canceled", errno=604)
        self._conn.clock.now += seconds
        keys = list(result.rows[0].keys()) if result.rows else []
        self.description = [
            (key, "FIXED", None, None, None, None, True) for key in keys
        ]
        self._rows = [tuple(row[key] for key in keys) for row in result.rows]
        return self

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchmany(self, size: int) -> list[tuple]:
        return list(self._rows[:size])

    # --- internals -------------------------------------------------------------

    def _record(self, sql: str, timeout: int | None) -> None:
        self._conn.statements.append(
            FakeStatement(
                sql=sql,
                timeout=timeout,
                session_timeout=self._conn.session_parameters.get(
                    "STATEMENT_TIMEOUT_IN_SECONDS"
                ),
            )
        )

    def _apply_session_parameter(self, sql: str) -> None:
        # ALTER SESSION SET <PARAM> = <value>
        try:
            assignment = sql.split("SET", 1)[1]
            key, value = assignment.split("=", 1)
            key = key.strip().upper()
            value = value.strip().strip("'")
            self._conn.session_parameters[key] = (
                int(value) if value.isdigit() else value
            )
        except (IndexError, ValueError):
            pass

    def _serve_show(self, sql: str) -> None:
        upper = sql.strip().upper()
        if upper.startswith("SHOW DATABASES"):
            names = sorted({t.database for t in self._conn.tables})
            self._emit([{"name": name} for name in names])
        elif upper.startswith("SHOW WAREHOUSES"):
            like = self._like_pattern(sql)
            rows = [
                {
                    "name": w.name,
                    "size": w.size,
                    "state": w.state,
                    "resource_constraint": w.resource_constraint,
                }
                for w in self._conn.warehouses
                if like is None or w.name.upper() == like.upper()
            ]
            self._emit(rows)
        elif upper.startswith("SHOW TABLES") or upper.startswith("SHOW VIEWS"):
            kind = "table" if upper.startswith("SHOW TABLES") else "view"
            rows = [
                {
                    "name": t.name,
                    "database_name": t.database,
                    "schema_name": t.schema,
                    "rows": t.rows,
                    "bytes": t.bytes,
                }
                for t in self._scoped(sql)
                if t.kind == kind
            ]
            self._emit(rows)
        elif upper.startswith("SHOW COLUMNS"):
            rows = []
            for t in self._scoped(sql):
                for col, type_token, nullable in t.columns:
                    rows.append(
                        {
                            "table_name": t.name,
                            "database_name": t.database,
                            "schema_name": t.schema,
                            "column_name": col,
                            "data_type": (
                                f'{{"type":"{type_token}",'
                                f'"nullable":{str(nullable).lower()}}}'
                            ),
                        }
                    )
            self._emit(rows)
        else:
            self._emit([])

    def _scoped(self, sql: str) -> list[FakeSnowflakeTable]:
        upper = sql.upper()
        tables = sorted(self._conn.tables, key=lambda t: t.identifier)
        if " IN SCHEMA " in upper:
            scope = sql[upper.index(" IN SCHEMA ") + len(" IN SCHEMA ") :].strip()
            db, schema = (part.strip('"') for part in scope.split(".", 1))
            return [
                t
                for t in tables
                if t.database.upper() == db.upper()
                and t.schema.upper() == schema.upper()
            ]
        if " IN DATABASE " in upper:
            scope = sql[upper.index(" IN DATABASE ") + len(" IN DATABASE ") :].strip()
            db = scope.strip('"')
            return [t for t in tables if t.database.upper() == db.upper()]
        if " IN TABLE " in upper:
            scope = sql[upper.index(" IN TABLE ") + len(" IN TABLE ") :].strip()
            ident = scope.replace('"', "")
            return [t for t in tables if t.identifier.upper() == ident.upper()]
        return tables

    @staticmethod
    def _like_pattern(sql: str) -> str | None:
        upper = sql.upper()
        if "LIKE" not in upper:
            return None
        return sql[upper.index("LIKE") + 4 :].strip().strip("'")

    def _emit(self, rows: list[dict]) -> None:
        keys = list(rows[0].keys()) if rows else ["name"]
        self.description = [(key, "TEXT", None, None, None, None, True) for key in keys]
        self._rows = [tuple(row[key] for key in keys) for row in rows]


class FakeSnowflakeConnection:
    """Simulates exactly the connection surface the adapter touches; anything
    else raises AttributeError, which is the point (the adapter must not grow
    calls the fake does not vouch for)."""

    def __init__(
        self,
        *,
        tables: list[FakeSnowflakeTable] | None = None,
        warehouses: list[FakeWarehouse] | None = None,
        clock: FakeClock | None = None,
        row_resolver: Callable[[str], FakeResult | list[dict]] | None = None,
        resume_seconds: float = 60.0,
    ):
        self.tables = tables or []
        self.warehouses = warehouses or [FakeWarehouse(name="DEX_WH")]
        self.clock = clock or FakeClock()
        self.row_resolver = row_resolver
        self.resume_seconds = resume_seconds
        self.statements: list[FakeStatement] = []
        self.used: list[str] = []
        self.session_parameters: dict[str, Any] = {}
        self.execute_count = 0
        self.closed = False
        # Set when the (single) fake warehouse starts suspended: the first
        # data statement pays the resume on the clock.
        self.warehouse_resumes_pending = any(
            w.state != "STARTED" for w in self.warehouses
        )

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
