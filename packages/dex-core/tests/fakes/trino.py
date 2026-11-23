"""A stateful fake of the trino DBAPI surface dex uses.

Behavioral, not a mock: it records every statement in order with the session
properties in force when it ran, applies ``SET SESSION`` and ``RESET SESSION``
to the connection the way the client does (the server answers with a header and
the client keeps the property for every later statement), serves the
information_schema and ``system.metadata.catalogs`` lookups from a table
registry, answers ``EXPLAIN (TYPE IO, FORMAT JSON)`` with per-table byte
estimates (``"NaN"`` for a table registered without statistics, the server's own
spelling), and enforces the caps the way the server does: a real
``trino.exceptions.TrinoQueryError`` carrying the server's error name and code,
with the virtual clock advanced only to the limit, because a killed statement
still ran that long.

A cursor's ``stats`` behave like the client's: ``FINISHED`` with the server's
elapsed time once a query completes, and frozen at ``RUNNING`` when it errors or
is cancelled, which is why the adapter settles those on its own clock.

Tests assert against observable behavior (statement order, the properties each
statement ran under, ledger effects), not call signatures. Anything the adapter
reaches for that is not modelled here raises, which is the point.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from trino.exceptions import TrinoConnectionError, TrinoQueryError, TrinoUserError

# Statements with no registered duration "run" this long.
DEFAULT_STATEMENT_SECONDS = 0.2

# The server's own names and codes, so the adapter's translation is exercised
# against what it will actually see.
EXCEEDED_TIME_LIMIT = ("EXCEEDED_TIME_LIMIT", 131075, "INSUFFICIENT_RESOURCES")
EXCEEDED_SCAN_LIMIT = ("EXCEEDED_SCAN_LIMIT", 131081, "INSUFFICIENT_RESOURCES")
PERMISSION_DENIED = ("PERMISSION_DENIED", 4, "USER_ERROR")
TABLE_NOT_FOUND = ("TABLE_NOT_FOUND", 46, "USER_ERROR")
SYNTAX_ERROR = ("SYNTAX_ERROR", 1, "USER_ERROR")

_UNITS = {"B": 1, "kB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def server_error(kind: tuple[str, int, str], message: str) -> TrinoQueryError:
    """A driver exception shaped the way the client raises a failed query:
    USER_ERROR arrives as ``TrinoUserError``, everything else as the base
    ``TrinoQueryError``, both carrying the server's error name."""

    name, code, error_type = kind
    error = {
        "errorName": name,
        "errorCode": code,
        "errorType": error_type,
        "message": message,
    }
    cls = TrinoUserError if error_type == "USER_ERROR" else TrinoQueryError
    return cls(error, query_id="fake-failed")


@dataclass
class FakeTrinoTable:
    catalog: str
    schema: str
    name: str
    # (column_name, Trino type, nullable)
    columns: list[tuple[str, str, bool]]
    # What EXPLAIN (TYPE IO) reports for a full scan; None means the catalog
    # has no statistics, which the server spells "NaN".
    estimate_bytes: float | None = 0.0
    estimate_rows: float | None = 0.0
    table_type: str = "BASE TABLE"

    @property
    def identifier(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"


@dataclass
class FakeQuery:
    """One statement as issued, with the session properties that governed it.
    The properties are the assertion surface for every cap test: dex's whole
    server-side backstop is 'the right caps were in force for the statement'."""

    sql: str
    properties: dict[str, str]
    query_id: str

    @property
    def is_session(self) -> bool:
        return self.sql.upper().startswith(
            ("SET SESSION", "RESET SESSION", "SET TIME ZONE")
        )

    @property
    def is_data(self) -> bool:
        """Whether this statement scans data, as opposed to reading metadata or
        changing the session. ``data_queries == []`` is how nearly every "this
        was free" assertion is written."""

        upper = self.sql.upper()
        if self.is_session or upper.startswith("EXPLAIN"):
            return False
        return (
            "INFORMATION_SCHEMA." not in upper
            and "SYSTEM.METADATA." not in upper
            and "VERSION()" not in upper
        )


@dataclass
class FakeResult:
    """What one executed query returns: rows keyed by alias, plus how long it
    'runs' and how many physical bytes it 'reads'."""

    rows: list[dict]
    seconds: float = DEFAULT_STATEMENT_SECONDS
    physical_bytes: int = 0
    types: dict[str, str] = field(default_factory=dict)


class FakeTrinoCursor:
    def __init__(self, connection: FakeTrinoConnection):
        self._conn = connection
        self._rows: list[tuple] = []
        self._position = 0
        self.description: list[tuple] | None = None
        self.stats: dict[str, Any] = {}
        self.query_id: str | None = None
        self.cancelled = False

    def execute(self, sql: str) -> FakeTrinoCursor:
        conn = self._conn
        conn.counter += 1
        self.query_id = f"fake_{conn.counter:05d}"
        stripped = sql.strip()
        conn.queries.append(
            FakeQuery(
                sql=stripped, properties=dict(conn.properties), query_id=self.query_id
            )
        )
        if conn.unreachable:
            raise TrinoConnectionError("failed to connect to the coordinator")
        upper = stripped.upper()
        if upper.startswith("SET SESSION"):
            match = re.match(r"SET SESSION (\w+) = '([^']*)'", stripped, re.IGNORECASE)
            assert match, f"unparsed session statement: {stripped}"
            conn.properties[match.group(1)] = match.group(2)
            return self._answer([], seconds=0.0)
        if upper == "SET TIME ZONE 'UTC'":
            # The server answers with the `time_zone_id` property set.
            conn.properties["time_zone_id"] = "UTC"
            return self._answer([], seconds=0.0)
        if upper.startswith("RESET SESSION"):
            conn.properties.pop(stripped.split()[-1], None)
            return self._answer([], seconds=0.0)
        for needle, error in conn.refusals.items():
            if needle.upper() in upper:
                raise error
        if upper.startswith("EXPLAIN (TYPE IO, FORMAT JSON)"):
            return self._answer([{"Query Plan": conn.explain(stripped)}], seconds=0.0)
        if "SYSTEM.METADATA.CATALOGS" in upper:
            return self._answer(
                [
                    {"catalog_name": name, "connector_name": kind}
                    for name, kind in sorted(conn.catalogs.items())
                ],
                seconds=0.0,
            )
        if "VERSION()" in upper:
            return self._answer([{"server_version": conn.server_version}], seconds=0.0)
        if "INFORMATION_SCHEMA.SCHEMATA" in upper:
            return self._answer(conn.schemata(stripped), seconds=0.0)
        if "INFORMATION_SCHEMA.TABLES" in upper:
            return self._answer(conn.information_tables(stripped), seconds=0.0)
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            return self._answer(conn.information_columns(stripped), seconds=0.0)
        return self._data_query(stripped)

    def _data_query(self, sql: str) -> FakeTrinoCursor:
        conn = self._conn
        result = conn.row_resolver(sql) if conn.row_resolver is not None else None
        if result is None:
            result = FakeResult(rows=[_default_row(sql)])
        # The server enforces the caps in force, time first. A killed query ran
        # until the kill, and the client's stats never saw it finish.
        run_time = _seconds(conn.properties.get("query_max_run_time"))
        if run_time is not None and result.seconds > run_time:
            conn.now += run_time
            self.stats = {"state": "RUNNING", "elapsedTimeMillis": 1}
            raise server_error(
                EXCEEDED_TIME_LIMIT,
                f"Query exceeded maximum time limit of {run_time:.2f}s",
            )
        scan_cap = _bytes(conn.properties.get("query_max_scan_physical_bytes"))
        if scan_cap is not None and result.physical_bytes > scan_cap:
            conn.now += result.seconds
            self.stats = {"state": "RUNNING", "elapsedTimeMillis": 1}
            raise server_error(EXCEEDED_SCAN_LIMIT, "Exceeded scan limit")
        return self._answer(
            result.rows,
            seconds=result.seconds,
            physical_bytes=result.physical_bytes,
            types=result.types,
        )

    def _answer(
        self,
        rows: list[dict],
        *,
        seconds: float,
        physical_bytes: int = 0,
        types: dict[str, str] | None = None,
    ) -> FakeTrinoCursor:
        self._conn.now += seconds
        names = list(rows[0]) if rows else []
        self.description = [
            (name, (types or {}).get(name) or _type_of(rows[0].get(name))) + (None,) * 5
            for name in names
        ]
        self._rows = [tuple(row.get(name) for name in names) for row in rows]
        self._position = 0
        self.stats = {
            "state": "FINISHED",
            "elapsedTimeMillis": round(seconds * 1000),
            "physicalInputBytes": physical_bytes,
            "queryId": self.query_id,
        }
        return self

    def fetchall(self) -> list[tuple]:
        rows = self._rows[self._position :]
        self._position = len(self._rows)
        return [list(row) for row in rows]

    def fetchmany(self, size: int) -> list[tuple]:
        rows = self._rows[self._position : self._position + size]
        self._position += len(rows)
        if self._position < len(self._rows):
            # More rows remain server-side: the query is still running.
            self.stats = {**self.stats, "state": "RUNNING"}
        return [list(row) for row in rows]

    def cancel(self) -> None:
        if self.stats.get("state") == "FINISHED":
            return
        self.cancelled = True
        self._conn.cancelled.append(self.query_id)


class FakeTrinoConnection:
    """Simulates exactly the DBAPI surface the adapter touches; anything else
    raises AttributeError, which is the point."""

    def __init__(
        self,
        tables: list[FakeTrinoTable] | None = None,
        *,
        catalogs: dict[str, str] | None = None,
        empty_schemas: dict[str, list[str]] | None = None,
        server_version: str = "483",
    ):
        self.tables = list(tables or [])
        self.catalogs = dict(
            catalogs
            if catalogs is not None
            else {t.catalog: "memory" for t in self.tables} | {"system": "system"}
        )
        self.empty_schemas = {k: list(v) for k, v in (empty_schemas or {}).items()}
        self.server_version = server_version
        self.properties: dict[str, str] = {}
        self.queries: list[FakeQuery] = []
        self.cancelled: list[str | None] = []
        self.closed = False
        self.now = 0.0
        self.counter = 0
        # Per-test hooks. `row_resolver` answers a data query; `explain_override`
        # replaces what EXPLAIN claims a statement reads ({identifier: bytes or
        # None}); `refusals` makes any statement containing a key raise its
        # error; `unreachable` fails every statement at the transport.
        self.row_resolver = None
        self.explain_override: dict[str, float | None] | None = None
        self.refusals: dict[str, Exception] = {}
        self.unreachable = False

    # --- the surface the adapter uses -----------------------------------------

    def cursor(self) -> FakeTrinoCursor:
        return FakeTrinoCursor(self)

    def close(self) -> None:
        self.closed = True

    def clock(self) -> float:
        return self.now

    # --- helpers ----------------------------------------------------------------

    @property
    def data_queries(self) -> list[FakeQuery]:
        return [q for q in self.queries if q.is_data]

    @property
    def session_statements(self) -> list[str]:
        return [q.sql for q in self.queries if q.is_session]

    def table(self, identifier: str) -> FakeTrinoTable:
        for t in self.tables:
            if t.identifier == identifier:
                return t
        raise KeyError(identifier)

    def explain(self, sql: str) -> str:
        """The EXPLAIN (TYPE IO, FORMAT JSON) document for one statement: every
        registered table the statement names, with its estimate. The predicate
        ranges carry a literal so a test can prove the adapter never reads
        them."""

        if self.explain_override is not None:
            named = list(self.explain_override.items())
        else:
            named = [
                (t.identifier, t.estimate_bytes)
                for t in self.tables
                if f'"{t.schema}"."{t.name}"' in sql or f"{t.schema}.{t.name}" in sql
            ]
        infos = []
        for identifier, size in named:
            catalog, schema, table = identifier.split(".")
            infos.append(
                {
                    "table": {
                        "catalog": catalog,
                        "schemaTable": {"schema": schema, "table": table},
                    },
                    "constraint": {
                        "none": False,
                        "columnConstraints": [
                            {
                                "columnName": "email",
                                "type": "varchar",
                                "domain": {
                                    "ranges": [{"low": {"value": "secret@example.com"}}]
                                },
                            }
                        ],
                    },
                    "estimate": {
                        "outputRowCount": "NaN" if size is None else 1000.0,
                        "outputSizeInBytes": "NaN" if size is None else float(size),
                        "cpuCost": "NaN" if size is None else float(size),
                        "maxMemory": 0.0,
                        "networkCost": 0.0,
                    },
                }
            )
        return json.dumps({"inputTableColumnInfos": infos, "estimate": {}}, indent=2)

    def _catalog_of(self, sql: str) -> str:
        match = re.search(r'"([^"]+)"\.information_schema', sql)
        assert match, f"information_schema read without a catalog: {sql}"
        return match.group(1)

    def schemata(self, sql: str) -> list[dict]:
        catalog = self._catalog_of(sql)
        names = {t.schema for t in self.tables if t.catalog == catalog}
        names |= set(self.empty_schemas.get(catalog, []))
        return [{"schema_name": n} for n in sorted(names | {"information_schema"})]

    def information_tables(self, sql: str) -> list[dict]:
        catalog = self._catalog_of(sql)
        wanted = re.search(r"table_schema = '([^']+)'", sql)
        return [
            {
                "table_schema": t.schema,
                "table_name": t.name,
                "table_type": t.table_type,
            }
            for t in self.tables
            if t.catalog == catalog and (wanted is None or t.schema == wanted.group(1))
        ]

    def information_columns(self, sql: str) -> list[dict]:
        catalog = self._catalog_of(sql)
        wanted = re.search(r"table_schema = '([^']+)'", sql)
        rows = []
        for t in self.tables:
            if t.catalog != catalog or (wanted and t.schema != wanted.group(1)):
                continue
            for position, (name, data_type, nullable) in enumerate(t.columns, start=1):
                rows.append(
                    {
                        "table_schema": t.schema,
                        "table_name": t.name,
                        "column_name": name,
                        "data_type": data_type,
                        "is_nullable": "YES" if nullable else "NO",
                        "ordinal_position": position,
                    }
                )
        return rows


def _seconds(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", value)
    assert match, f"unparsed duration: {value}"
    return float(match.group(1))


def _bytes(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+)(B|kB|MB|GB|TB)", value)
    assert match, f"unparsed data size: {value}"
    return int(match.group(1)) * _UNITS[match.group(2)]


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double"
    return "varchar"


def _default_row(sql: str) -> dict:
    """One row keyed by whatever aliases the statement selected.

    A profiling batch reads its results back by alias, so a fake that returned
    a bare empty row would fail on the first lookup. Values are deliberately
    dull (count aliases are 0, everything else None) because tests that care
    about a value set `row_resolver`.
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
