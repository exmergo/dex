"""A stateful fake of the clickhouse-connect client surface dex uses.

Behavioral, not a mock: it records every query in order with the settings that
were in force, serves the ``system.*`` lookups (inventory, columns, databases,
grants, the capabilities probe) from a table registry, answers
``EXPLAIN ESTIMATE`` with pruned row counts derived from the referenced tables
(overridable per test), returns a real ``X-ClickHouse-Summary`` shaped payload
with **string** values (the wire format, which is what the adapter has to cope
with), and enforces the caps the way the server does: a real
``clickhouse_connect`` ``DatabaseError`` carrying the right numeric ``code``,
with the elapsed time capped at the limit, because a killed statement still
bills what ran.

Tests assert against observable behavior (query ordering, the settings each
statement carried, ledger effects), not call signatures. Anything the adapter
reaches for that is not modelled here raises, which is the point: the adapter
must not grow calls the fake does not vouch for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError

# Statements with no registered duration "run" this long.
DEFAULT_STATEMENT_SECONDS = 0.2

# The server's own codes, so the adapter's translation is exercised against the
# numbers it will actually see rather than against a stand-in.
TIMEOUT_EXCEEDED = 159
TOO_MANY_ROWS = 158
TOO_MANY_BYTES = 307
TOO_MANY_ROWS_OR_BYTES = 396
UNKNOWN_TABLE = 60
ACCESS_DENIED = 497


def server_error(code: int, message: str) -> DatabaseError:
    """A driver exception shaped the way a server error arrives: the numeric
    ``code`` is the part consumers branch on, and the adapter reads it."""

    exc = DatabaseError(f"Received ClickHouse exception, code: {code}: {message}")
    exc.code = code
    return exc


@dataclass
class FakeClickHouseTable:
    database: str
    name: str
    # (column_name, ClickHouse type, is_in_primary_key)
    columns: list[tuple[str, str, bool]]
    total_rows: int | None = 0
    total_bytes: int | None = 0
    engine: str = "MergeTree"
    sampling_key: str = ""
    primary_key: str = "id"

    @property
    def identifier(self) -> str:
        return f"{self.database}.{self.name}"


@dataclass
class FakeGrant:
    """One row of system.grants. ``database`` of None is a global grant."""

    user_name: str
    access_type: str
    database: str | None = None
    role_name: str | None = None


@dataclass
class FakeQuery:
    """One query as issued, with the settings that governed it. The settings are
    the assertion surface for every cap test: dex's whole server-side backstop
    is 'the right numbers rode along with the statement'."""

    sql: str
    settings: dict[str, Any]

    @property
    def is_data(self) -> bool:
        """Whether this query scans data, as opposed to reading metadata.

        `data_queries == []` is how nearly every "this was free" assertion is
        written, so the split has to be by what the statement touches rather
        than by who called it.
        """

        upper = self.sql.upper()
        if upper.startswith("EXPLAIN"):
            return False
        return "SYSTEM." not in upper and "VERSION()" not in upper


@dataclass
class FakeResult:
    """What one executed query returns: rows keyed by alias, plus how long the
    statement 'runs' and how much it 'reads' for the summary header."""

    rows: list[dict]
    seconds: float = DEFAULT_STATEMENT_SECONDS
    read_rows: int = 0
    read_bytes: int = 0


class _QueryResult:
    """The clickhouse-connect QueryResult surface the adapter reads."""

    def __init__(self, rows: list[dict], summary: dict[str, str]):
        self.column_names = tuple(rows[0]) if rows else ()
        self.result_rows = [
            tuple(row.get(name) for name in self.column_names) for row in rows
        ]
        first = rows[0] if rows else {}
        self.column_types = tuple(
            _FakeType(_type_of(first.get(name))) for name in self.column_names
        )
        self.summary = summary
        self.query_id = summary.get("query_id", "")


@dataclass
class _FakeType:
    name: str


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "Bool"
    if isinstance(value, int):
        return "UInt64"
    if isinstance(value, float):
        return "Float64"
    return "String"


class FakeClickHouseConnection:
    """Simulates exactly the client surface the adapter touches; anything else
    raises AttributeError, which is the point."""

    def __init__(
        self,
        tables: list[FakeClickHouseTable] | None = None,
        *,
        grants: list[FakeGrant] | None = None,
        server_version: str = "25.3.14.14",
        readonly: str = "2",
        cloud_mode: str = "0",
        grants_readable: bool = True,
        role_grants_readable: bool = True,
    ):
        self.tables = list(tables or [])
        self.grants = list(grants or [])
        self.server_version = server_version
        self.readonly = readonly
        self.cloud_mode = cloud_mode
        self.grants_readable = grants_readable
        self.role_grants_readable = role_grants_readable
        self.queries: list[FakeQuery] = []
        self.closed = False
        self.now = 0.0
        # Per-test hooks. `row_resolver` answers a data query; `estimate_rows`
        # overrides what EXPLAIN ESTIMATE claims a statement would read.
        self.row_resolver = None
        self.estimate_rows: dict[str, int] | None = None
        self.unreachable = False

    # --- the surface the adapter uses -----------------------------------------

    def query(self, sql: str, settings: dict | None = None):
        settings = dict(settings or {})
        self.queries.append(FakeQuery(sql=sql, settings=settings))
        if self.unreachable:
            raise OperationalError("connection refused")

        stripped = sql.strip()
        upper = stripped.upper()

        if upper.startswith("EXPLAIN ESTIMATE"):
            return self._explain(stripped[len("EXPLAIN ESTIMATE") :].strip())
        if "SYSTEM.SETTINGS" in upper or "VERSION()" in upper:
            return self._probe(stripped)
        if "SYSTEM.TABLES" in upper:
            return self._system_tables(stripped)
        if "SYSTEM.COLUMNS" in upper:
            return self._system_columns()
        if "SYSTEM.DATABASES" in upper:
            return self._system_databases()
        if "SYSTEM.GRANTS" in upper:
            return self._system_grants(stripped)

        return self._data_query(stripped, settings)

    def close(self) -> None:
        self.closed = True

    # --- helpers ----------------------------------------------------------------

    @property
    def data_queries(self) -> list[FakeQuery]:
        return [q for q in self.queries if q.is_data]

    def table(self, identifier: str) -> FakeClickHouseTable:
        for t in self.tables:
            if t.identifier == identifier:
                return t
        raise KeyError(identifier)

    def _result(self, rows: list[dict], result: FakeResult | None = None):
        result = result or FakeResult(rows=rows, seconds=0.0)
        self.now += result.seconds
        summary = {
            "read_rows": str(result.read_rows),
            "read_bytes": str(result.read_bytes),
            "written_rows": "0",
            "written_bytes": "0",
            "result_rows": str(len(rows)),
            "elapsed_ns": str(int(result.seconds * 1_000_000_000)),
            "query_id": "fake-query-id",
        }
        return _QueryResult(rows, summary)

    def _probe(self, sql: str):
        if "CLOUD_MODE" in sql.upper():
            return self._result([{"value": self.cloud_mode}])
        return self._result(
            [
                {
                    "server_version": self.server_version,
                    "db": "app",
                    "readonly": self.readonly,
                }
            ]
        )

    def _system_tables(self, sql: str):
        match = re.search(r"database = '([^']+)'", sql)
        if match:
            # list_namespace_objects
            wanted = match.group(1)
            return self._result(
                [{"name": t.name} for t in self.tables if t.database == wanted]
            )
        return self._result(
            [
                {
                    "database": t.database,
                    "name": t.name,
                    "engine": t.engine,
                    "total_rows": t.total_rows,
                    "total_bytes": t.total_bytes,
                    "sampling_key": t.sampling_key,
                    "primary_key": t.primary_key,
                }
                for t in self.tables
            ]
        )

    def _system_columns(self):
        rows = []
        for t in self.tables:
            for position, (name, data_type, in_pk) in enumerate(t.columns):
                rows.append(
                    {
                        "database": t.database,
                        "table": t.name,
                        "name": name,
                        "type": data_type,
                        "position": position,
                        "is_in_primary_key": 1 if in_pk else 0,
                    }
                )
        return self._result(rows)

    def _system_databases(self):
        names = sorted({t.database for t in self.tables})
        return self._result([{"name": n} for n in names])

    def _system_grants(self, sql: str):
        if not self.grants_readable:
            raise server_error(ACCESS_DENIED, "not enough privileges for system.grants")
        if "ROLE_GRANTS" in sql.upper():
            if not self.role_grants_readable:
                raise server_error(
                    ACCESS_DENIED, "not enough privileges for system.role_grants"
                )
            return self._result(
                [
                    {"access_type": g.access_type, "database": g.database}
                    for g in self.grants
                    if g.role_name
                ]
            )
        match = re.search(r"user_name = '([^']+)'", sql)
        user = match.group(1) if match else ""
        return self._result(
            [
                {"access_type": g.access_type, "database": g.database}
                for g in self.grants
                if g.user_name == user and not g.role_name
            ]
        )

    def _explain(self, inner: str):
        if self.estimate_rows is not None:
            rows = [
                {
                    "database": ident.split(".")[0],
                    "table": ident.split(".")[1],
                    "parts": 1,
                    "rows": count,
                    "marks": 1,
                }
                for ident, count in self.estimate_rows.items()
            ]
            return self._result(rows)
        rows = []
        for t in self.tables:
            if t.name in inner and t.engine.endswith("MergeTree"):
                rows.append(
                    {
                        "database": t.database,
                        "table": t.name,
                        "parts": 1,
                        "rows": t.total_rows or 0,
                        "marks": 1,
                    }
                )
        return self._result(rows)

    def _data_query(self, sql: str, settings: dict):
        result = None
        if self.row_resolver is not None:
            result = self.row_resolver(sql)
        if result is None:
            result = FakeResult(rows=[_default_row(sql)])

        # The server enforces the caps dex set, in the order it would: time
        # first, then bytes, then result rows. Elapsed is capped at the limit,
        # because a killed statement still bills what ran up to the kill.
        limit = settings.get("max_execution_time")
        if limit is not None and result.seconds > float(limit):
            self.now += float(limit)
            raise server_error(TIMEOUT_EXCEEDED, "Timeout exceeded")
        byte_cap = settings.get("max_bytes_to_read")
        if byte_cap is not None and result.read_bytes > int(byte_cap):
            self.now += result.seconds
            raise server_error(TOO_MANY_BYTES, "Limit for bytes to read exceeded")
        row_cap = settings.get("max_result_rows")
        if row_cap is not None and len(result.rows) > int(row_cap):
            self.now += result.seconds
            raise server_error(TOO_MANY_ROWS_OR_BYTES, "Limit for result exceeded")
        return self._result(result.rows, result)


def _default_row(sql: str) -> dict:
    """One row keyed by whatever aliases the statement selected.

    A profiling batch reads its results back by alias, so a fake that returned a
    bare empty row would fail on the first lookup and every test would have to
    hand-write the alias set. Values are deliberately dull (row counts land on
    the count aliases, everything else is None) because tests that care about a
    value set `row_resolver`; the ones that do not are asserting on the SQL, the
    settings, or the ledger.
    """

    aliases = re.findall(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", sql)
    row: dict[str, Any] = {}
    for alias in aliases:
        if alias == "n_total" or alias.startswith(("nn_", "d_", "n_")):
            row[alias] = 0
        else:
            row[alias] = None
    row.setdefault("n_total", 0)
    return row
