"""A stateful fake of the redshift_connector connection surface dex uses.

Behavioral, not a mock: it records every statement in order, serves the
catalog lookups (the ``pg_class`` census, ``SVV_TABLE_INFO`` size facts with
the real view's empty-table omission, ``SVV_COLUMNS``, namespace scope, the
capabilities probe, the dev-target privilege predicates) from a table
registry, simulates per-statement duration by advancing an injectable clock
(the adapter measures wall-clock elapsed through the same clock, so timing
assertions are deterministic), tracks the session parameters the adapter sets
(``statement_timeout``, ``query_group``, the best-effort read-only mode, with
a knob to decline it the way a server without the parameter would), and
enforces ``statement_timeout`` the way the server does: a real
``redshift_connector.error.ProgrammingError`` raised at execute with the
server's message shape, and the elapsed time capped at the timeout (a killed
statement still bills what ran). Tests assert against observable behavior
(statement ordering, session state, ledger effects), not call signatures.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from redshift_connector import error as rs_errors

# Statements with no registered duration run this long on the fake clock.
DEFAULT_STATEMENT_SECONDS = 0.2

_TEXT_TYPE_CODE = 25


class FakeClock:
    """The adapter's injectable clock; the fake connection advances it by each
    statement's simulated duration."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeRedshiftTable:
    schema: str
    name: str
    # (column_name, SVV_COLUMNS data_type, nullable), e.g. ("id", "bigint", False)
    columns: list[tuple[str, str, bool]]
    # SVV_TABLE_INFO facts. ``size_mb=None`` models the real view's documented
    # behavior of omitting tables that hold no data.
    size_mb: int | None = None
    tbl_rows: float | None = None
    kind: str = "table"  # "table" | "view"

    @property
    def relkind(self) -> str:
        return {"table": "r", "view": "v"}[self.kind]

    @property
    def total_bytes(self) -> int:
        return int(self.size_mb or 0) * 1024 * 1024


@dataclass
class FakeUser:
    """A database user and what it may do, for the dev-target privilege
    preflight. The preflight asks about the user in the rendered profile,
    which need not be the user the connection authenticated as, so the fake
    answers privilege questions per user rather than for one implicit current
    user."""

    name: str
    # CREATE on the database: what dbt needs to create a dev schema that is
    # not there yet.
    may_create_in_database: bool = False
    # schema name -> the privileges this user holds on it ({"USAGE", "CREATE"}).
    schema_privileges: dict[str, set[str]] = field(default_factory=dict)


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


def _timeout_error() -> rs_errors.ProgrammingError:
    """The server's statement_timeout kill, verbatim in shape (verified live
    2026-07-13): the driver surfaces the backend error dict, and Redshift
    words the server-side kill as a user cancel, so the SQLSTATE (57014) is
    what the adapter's refusal translation keys on."""

    return rs_errors.ProgrammingError(
        {
            "S": "ERROR",
            "C": "57014",
            "M": "Query cancelled on user's request",
        }
    )


_CATALOG_MARKERS = (
    "pg_catalog.",
    "svv_table_info",
    "svv_columns",
    "current_database()",
    "version()",
    "has_schema_privilege",
    "has_database_privilege",
)


def _is_catalog(sql: str) -> bool:
    lowered = sql.lower()
    return any(marker in lowered for marker in _CATALOG_MARKERS)


class FakeCursor:
    def __init__(self, conn: FakeRedshiftConnection):
        self._conn = conn
        self._rows: list[tuple] = []
        # DB-API description tuples: (name, type_code, ...), which is what
        # redshift_connector exposes and the adapter indexes into.
        self.description: list[tuple] = []

    def execute(self, sql: str):
        stripped = sql.strip()
        upper = stripped.upper()
        self._record(sql)

        if upper.startswith("SET "):
            self._apply_session_parameter(stripped)
            return self
        if _is_catalog(stripped):
            self._serve_catalog(stripped)
            return self

        # A data statement: simulate duration, enforce the session timeout the
        # way the server does (kill and bill what ran).
        result = self._conn.resolve(sql)
        timeout_ms = self._conn.session_parameters.get("statement_timeout")
        if timeout_ms is not None and result.seconds * 1000 > float(timeout_ms):
            self._conn.clock.now += float(timeout_ms) / 1000.0
            raise _timeout_error()
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
        # SET <param> = <value> (Redshift also accepts SET <param> TO <value>).
        try:
            assignment = sql.split(" ", 1)[1]
            separator = "=" if "=" in assignment else " TO "
            key, value = assignment.split(separator, 1)
            key = key.strip().lower()
            value = value.strip().strip("'")
        except (IndexError, ValueError):
            return
        if key == "default_transaction_read_only" and self._conn.reject_read_only:
            raise rs_errors.ProgrammingError(
                {
                    "S": "ERROR",
                    "C": "42704",
                    "M": (
                        "unrecognized configuration parameter "
                        '"default_transaction_read_only"'
                    ),
                }
            )
        self._conn.session_parameters[key] = (
            int(value) if value.lstrip("-").isdigit() else value
        )

    def _serve_catalog(self, sql: str) -> None:
        lowered = sql.lower()
        # The privilege predicates come first: has_database_privilege names
        # current_database() too, and would otherwise be answered as the
        # capabilities probe.
        if "has_database_privilege" in lowered:
            user = self._conn.users.get(_first_literal(sql))
            self._emit([{"may_create": bool(user and user.may_create_in_database)}])
        elif "has_schema_privilege" in lowered:
            # The adapter asks about both privileges in one round-trip, so the
            # answer carries both: SELECT ... AS may_create, ... AS may_use.
            literals = _literals(sql)
            user = self._conn.users.get(literals[0] if literals else "")
            held = user.schema_privileges.get(literals[1], set()) if user else set()
            self._emit([{"may_create": "CREATE" in held, "may_use": "USAGE" in held}])
        elif "version()" in lowered:
            self._emit(
                [
                    {
                        "db": self._conn.database,
                        "server_version": self._conn.server_version,
                    }
                ]
            )
        elif "svv_table_info" in lowered:
            rows = [
                {
                    "schema_name": t.schema,
                    "table_name": t.name,
                    "size": t.size_mb,
                    "tbl_rows": t.tbl_rows,
                }
                for t in sorted(self._conn.tables, key=lambda t: (t.schema, t.name))
                # The real view omits tables holding no data.
                if t.kind == "table" and t.size_mb is not None
            ]
            self._emit(rows)
        elif "svv_columns" in lowered:
            rows = []
            for t in sorted(self._conn.tables, key=lambda t: (t.schema, t.name)):
                for col, data_type, nullable in t.columns:
                    rows.append(
                        {
                            "schema_name": t.schema,
                            "object_name": t.name,
                            "column_name": col,
                            "data_type": data_type,
                            "is_nullable": "YES" if nullable else "NO",
                        }
                    )
            self._emit(rows)
        elif "pg_catalog.pg_class" in lowered:
            # A scoped census (nspname = '<x>') answers only that schema, the
            # way the real catalog would; the unscoped census answers everything.
            scoped = re.search(r"nspname = '([^']*)'", sql)
            rows = [
                {
                    "schema_name": t.schema,
                    "object_name": t.name,
                    "kind": t.relkind,
                }
                for t in sorted(self._conn.tables, key=lambda t: (t.schema, t.name))
                if scoped is None or t.schema == scoped.group(1)
            ]
            self._emit(rows)
        elif "pg_catalog.pg_user" in lowered:
            known = [name for name in self._conn.users if f"'{name}'" in sql]
            self._emit([{"present": 1} for _ in known])
        elif "pg_catalog.pg_namespace" in lowered:
            names = sorted(
                {t.schema for t in self._conn.tables} | self._conn.empty_schemas
            )
            self._emit([{"nspname": name} for name in names])
        elif "current_database()" in lowered:
            self._emit([{"db": self._conn.database}])
        else:
            self._emit([])

    def _emit(self, rows: list[dict]) -> None:
        # Every branch builds uniform dicts, so the first row names the
        # columns; with no rows the adapter zips labels over nothing, so the
        # placeholder is never observed.
        keys = list(rows[0].keys()) if rows else ["name"]
        self.description = [(key, _TEXT_TYPE_CODE) for key in keys]
        self._rows = [tuple(row[key] for key in keys) for row in rows]


def _literals(sql: str) -> list[str]:
    """The single-quoted literals of an engine-built catalog query, in order."""

    return re.findall(r"'([^']*)'", sql)


def _first_literal(sql: str) -> str:
    found = _literals(sql)
    return found[0] if found else ""


class FakeRedshiftConnection:
    """Simulates exactly the connection surface the adapter touches; anything
    else raises AttributeError, which is the point (the adapter must not grow
    calls the fake does not vouch for)."""

    def __init__(
        self,
        *,
        tables: list[FakeRedshiftTable] | None = None,
        database: str = "dexdb",
        server_version: str = (
            "PostgreSQL 8.0.2 on i686-pc-linux-gnu, compiled by GCC 3.4.2, "
            "Redshift 1.0.12345"
        ),
        clock: FakeClock | None = None,
        row_resolver: Callable[[str], FakeResult | list[dict]] | None = None,
        users: list[FakeUser] | None = None,
        empty_schemas: list[str] | None = None,
        reject_read_only: bool = False,
    ):
        self.tables = tables or []
        # Users the database knows, and their privileges: what the dev-target
        # preflight interrogates. Schemas holding no table (a scratch dev
        # schema before a first build) cannot come from the table registry, so
        # they are declared here.
        self.users = {user.name: user for user in (users or [])}
        self.empty_schemas = set(empty_schemas or [])
        self.database = database
        self.server_version = server_version
        self.clock = clock or FakeClock()
        self.row_resolver = row_resolver
        # A server that does not speak the session read-only parameter: the
        # adapter must tolerate the refusal and report it honestly.
        self.reject_read_only = reject_read_only
        self.statements: list[FakeStatement] = []
        self.session_parameters: dict[str, object] = {}
        self.closed = False
        self.autocommit = False

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
        """Statements that scan data: SELECTs that are not catalog lookups,
        the capabilities probe, or the dev-target privilege predicates (those
        answer from the catalog, scanning nothing)."""

        return [
            s
            for s in self.statements
            if s.sql.strip().upper().startswith("SELECT") and not _is_catalog(s.sql)
        ]
