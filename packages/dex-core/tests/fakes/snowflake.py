"""A stateful fake of the snowflake-connector-python surface dex uses.

Behavioral, not a mock: it records every statement in order, serves SHOW
metadata from a table registry, simulates per-statement duration by advancing
an injectable clock (the adapter measures wall-clock elapsed through the same
clock, so timing assertions are deterministic), tracks the session parameters
the adapter sets, and enforces ``STATEMENT_TIMEOUT_IN_SECONDS`` the way the
service does: a real ``ProgrammingError`` naming the statement timeout, raised
at execute, with the elapsed time capped at the timeout (a killed statement
still bills what ran). A data statement resumes a suspended warehouse the way
the service does, flipping its state and moving ``resumed_on`` at a moment's
wall time; what a resume bills is the adapter's model, not the clock. Tests
assert against observable behavior (statement ordering, session state, ledger
effects), not call signatures.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from snowflake.connector import errors as sf_errors

# Statements with no registered duration run this long on the fake clock.
DEFAULT_STATEMENT_SECONDS = 0.5


def _object_does_not_exist() -> sf_errors.ProgrammingError:
    """What the service actually says when a SHOW names a database or schema that
    is not there. Verbatim, because the whole point of dex's scope resolver is
    that this message names neither the object nor the fix, and a fake that
    quietly returned no rows here would hide the bug being guarded against."""

    return sf_errors.ProgrammingError(
        msg=(
            "002043 (02000): SQL compilation error: Object does not exist, or "
            "operation cannot be performed."
        ),
        errno=2043,
    )


class FakeClock:
    """The adapter's injectable clock; the fake connection advances it by each
    statement's simulated duration. ``wall`` is the same instant in epoch
    seconds, the clock warehouse claims and ``resumed_on`` are stamped in."""

    # 2026-01-01T00:00:00Z, so a fresh fake's wall clock and the default
    # `resumed_on` below sit on the same timeline.
    EPOCH = 1767225600.0

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.EPOCH + self.now


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
    """One warehouse as SHOW WAREHOUSES reports it. A data statement on one
    that is not STARTED resumes it the way the service does: the state flips to
    STARTED and ``resumed_on`` moves. ``running`` and ``queued`` are other
    sessions' load, which a test sets to model a shared warehouse."""

    name: str
    size: str = "X-Small"
    state: str = "SUSPENDED"
    resource_constraint: str = ""
    auto_suspend: int | None = 60
    running: int = 0
    queued: int = 0
    # Before the fake clock starts, so the first resume always moves it.
    resumed_on: str | None = "2025-12-31T12:00:00.000-08:00"
    resumes: int = 0
    # Whether the session's role holds OPERATE, which ALTER WAREHOUSE ...
    # SUSPEND needs; and how many times a suspend took.
    operate_granted: bool = True
    auto_resume: bool = True
    suspends: int = 0


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
    # False for a statement the service answers without a running warehouse
    # (a result-cache hit, a count served from metadata), which bills nothing
    # and leaves a suspended warehouse suspended.
    wakes_warehouse: bool = True


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
        if upper.startswith("ALTER WAREHOUSE"):
            self._record(sql, timeout)
            self._alter_warehouse(sql)
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
        warehouse = self._conn.current_warehouse()
        if result.wakes_warehouse and warehouse.state != "STARTED":
            # A resume takes a moment of wall time; what it bills is the
            # adapter's business, not the clock's.
            seconds += self._conn.resume_seconds
            warehouse.state = "STARTED"
            warehouse.resumes += 1
            warehouse.resumed_on = _iso(self._conn.clock.wall())
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

    def _alter_warehouse(self, sql: str) -> None:
        """ALTER WAREHOUSE "<name>" SUSPEND, the only ALTER WAREHOUSE dex
        issues: refused without OPERATE, refused on a warehouse that is not
        running, both in the service's own words."""

        match = re.match(r'\s*ALTER WAREHOUSE\s+"?([^"\s]+)"?\s+SUSPEND\s*$', sql, re.I)
        if match is None:
            raise sf_errors.ProgrammingError(msg="fake: unsupported ALTER WAREHOUSE")
        name = match.group(1).upper()
        warehouse = next(
            (w for w in self._conn.warehouses if w.name.upper() == name), None
        )
        if warehouse is None:
            raise _object_does_not_exist()
        if not warehouse.operate_granted:
            raise sf_errors.ProgrammingError(
                msg=(
                    "003001 (42501): SQL access control error: Insufficient "
                    f"privileges to operate on warehouse '{warehouse.name}'"
                ),
                errno=3001,
            )
        if warehouse.state != "STARTED":
            raise sf_errors.ProgrammingError(
                msg=(
                    f"090064 (22000): Invalid state. Warehouse '{warehouse.name}' "
                    "cannot be suspended."
                ),
                errno=90064,
            )
        warehouse.state = "SUSPENDED"
        warehouse.suspends += 1

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
            like = self._like_pattern(sql)
            names = sorted(
                name
                for name in self._conn.databases
                if like is None or name.upper() == like.upper()
            )
            self._emit([{"name": name} for name in names])
        elif upper.startswith("SHOW SCHEMAS"):
            like = self._like_pattern(sql)
            database = self._in_database(sql)
            empty = {
                schema
                for db, _, schema in (
                    e.partition(".") for e in self._conn.empty_schemas
                )
                if database is None or db == database.upper()
            }
            names = sorted(
                {t.schema for t in self._scoped(sql)} | empty | {"INFORMATION_SCHEMA"}
            )
            self._emit(
                [
                    {"name": name}
                    for name in names
                    if like is None or name.upper() == like.upper()
                ]
            )
        elif upper.startswith("SHOW WAREHOUSES"):
            like = self._like_pattern(sql)
            rows = [
                {
                    "name": w.name,
                    "size": w.size,
                    "state": w.state,
                    "resource_constraint": w.resource_constraint,
                    "auto_suspend": w.auto_suspend,
                    "auto_resume": "true" if w.auto_resume else "false",
                    "running": w.running,
                    "queued": w.queued,
                    "resumed_on": w.resumed_on,
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
            if db.upper() not in {d.upper() for d in self._conn.databases}:
                raise _object_does_not_exist()
            if (
                schema.upper()
                not in {
                    t.schema.upper() for t in tables if t.database.upper() == db.upper()
                }
                and f"{db}.{schema}".upper() not in self._conn.empty_schemas
            ):
                raise _object_does_not_exist()
            return [
                t
                for t in tables
                if t.database.upper() == db.upper()
                and t.schema.upper() == schema.upper()
            ]
        if " IN DATABASE " in upper:
            scope = sql[upper.index(" IN DATABASE ") + len(" IN DATABASE ") :].strip()
            db = scope.strip('"')
            if db.upper() not in {d.upper() for d in self._conn.databases}:
                raise _object_does_not_exist()
            return [t for t in tables if t.database.upper() == db.upper()]
        if " IN TABLE " in upper:
            scope = sql[upper.index(" IN TABLE ") + len(" IN TABLE ") :].strip()
            ident = scope.replace('"', "")
            return [t for t in tables if t.identifier.upper() == ident.upper()]
        return tables

    @staticmethod
    def _in_database(sql: str) -> str | None:
        upper = sql.upper()
        if " IN DATABASE " not in upper:
            return None
        return sql[upper.index(" IN DATABASE ") + len(" IN DATABASE ") :].strip('" ')

    @staticmethod
    def _like_pattern(sql: str) -> str | None:
        # Only the quoted literal, so a trailing `IN DATABASE x` never leaks into
        # the pattern the way a naive split on LIKE would let it.
        match = re.search(r"\bLIKE\s+'([^']*)'", sql, re.IGNORECASE)
        return match.group(1) if match else None

    def _emit(self, rows: list[dict]) -> None:
        keys = list(rows[0].keys()) if rows else ["name"]
        self.description = [(key, "TEXT", None, None, None, None, True) for key in keys]
        self._rows = [tuple(row[key] for key in keys) for row in rows]


def _iso(epoch: float) -> str:
    """An epoch instant as SHOW WAREHOUSES prints ``resumed_on`` in the
    account's (Pacific) time zone."""

    from datetime import UTC, datetime, timedelta, timezone

    pacific = timezone(timedelta(hours=-8))
    return (
        datetime.fromtimestamp(epoch, UTC)
        .astimezone(pacific)
        .isoformat(timespec="milliseconds")
    )


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
        resume_seconds: float = 0.0,
        empty_databases: list[str] | None = None,
        empty_schemas: list[str] | None = None,
    ):
        self.tables = tables or []
        # Databases exist independently of their contents. `empty_databases` is
        # how a scratch database that holds nothing yet gets modeled, which is
        # exactly the state a dbt dev target is in before its first build.
        self.databases = sorted(
            {t.database for t in self.tables} | set(empty_databases or [])
        )
        # Schemas, otherwise derived from the tables in them, likewise:
        # `DATABASE.SCHEMA` entries that exist and hold nothing, the state of a
        # dev schema created by hand, or by a dbt build that wrote no relation.
        # A set, so a test can create one mid-command the way dbt would.
        self.empty_schemas = {entry.upper() for entry in empty_schemas or []}
        self.warehouses = warehouses or [FakeWarehouse(name="DEX_WH")]
        self.clock = clock or FakeClock()
        self.row_resolver = row_resolver
        self.resume_seconds = resume_seconds
        self.statements: list[FakeStatement] = []
        self.used: list[str] = []
        self.session_parameters: dict[str, Any] = {}
        self.execute_count = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def current_warehouse(self) -> FakeWarehouse:
        """The warehouse the session last chose with USE WAREHOUSE, else the
        first one registered."""

        for statement in reversed(self.used):
            name = statement.split(None, 2)[-1].strip().strip('"').upper()
            for warehouse in self.warehouses:
                if warehouse.name.upper() == name:
                    return warehouse
        return self.warehouses[0]

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
