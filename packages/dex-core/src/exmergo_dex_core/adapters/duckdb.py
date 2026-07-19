"""The DuckDB adapter: first-class product connector and the eval/benchmark
engine. One implementation, three uses.

DuckDB is always opened read-only and bounded by memory/thread limits rather than
cost, because the work is free and local. This is the only adapter with real logic
today; it is what makes the whole loop buildable with no cloud accounts and
deterministic in CI.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..envelope import Paradigm
from ..guards.sql_guard import assert_select_only
from .base import (
    ColumnAggregate,
    ColumnMeta,
    ObjectMeta,
    QueryResult,
    distinct_combination_sql,
    json_safe,
    shape_stat_expressions,
    shape_stat_value,
)


def _regexp_predicate(qcol: str, pattern: str) -> str:
    # regexp_full_match ignores anchors' redundancy; the shared patterns carry
    # them for the substring-matching dialects.
    return f"regexp_full_match({qcol}, '{pattern}')"


# Conservative defaults so auto-invoked profiling cannot exhaust the machine.
# Overridable from .dex/config.yml.
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4

# Columns are profiled in batches so a single statement against a very wide table
# does not balloon (4 expressions per column).
_COLUMN_BATCH = 50

# Nested types DuckDB cannot apply approx_count_distinct / min / max to cleanly.
_NESTED_TYPE_PREFIXES = ("STRUCT", "MAP", "LIST", "UNION")


class DuckDBReadOnlyError(Exception):
    """Raised when a DuckDB path cannot be opened read-only.

    A read-only open is non-negotiable: rather than silently falling back to a
    writable connection, we fail loudly so the safety guarantee cannot erode.
    """


class DuckDBAdapter:
    """Holds a read-only DuckDB connection for the lifetime of one command.

    Opening a brand-new (nonexistent) file read-only fails in DuckDB, which is the
    correct behavior for dex: we attach to an existing analytical store, we never
    create one.
    """

    name = "duckdb"
    dialect = "duckdb"
    paradigm = Paradigm.FREE_LOCAL

    def __init__(
        self,
        path: str | Path,
        *,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        threads: int = DEFAULT_THREADS,
    ):
        self.path = str(path)
        self._memory_limit = memory_limit
        self._threads = threads
        self._conn = self._connect()

    def _connect(self):
        # Imported lazily so the base package import does not require the [duckdb]
        # extra; only this adapter pulls it in.
        import duckdb

        try:
            conn = duckdb.connect(
                self.path,
                read_only=True,
                config={
                    "memory_limit": self._memory_limit,
                    "threads": self._threads,
                },
            )
        except Exception as exc:  # duckdb raises various IO/Catalog errors
            raise DuckDBReadOnlyError(
                f"could not open '{self.path}' read-only: {exc}"
            ) from exc
        return conn

    def capabilities(self) -> dict[str, object]:
        version = self._conn.sql("SELECT version()").fetchone()[0]
        return {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "paradigm": self.paradigm.value,
            "engine_version": version,
            "resource_bounds": {
                "memory_limit": self._memory_limit,
                "threads": self._threads,
            },
        }

    # --- introspection (read-only; backs the explore engine) ----------------

    def list_objects(self, *, include_views: bool = True) -> list[ObjectMeta]:
        # One catalog round-trip, no table scans. estimated_size is DuckDB's own
        # row estimate; column_count is catalog metadata. Both are free.
        sql = """
            SELECT database_name, schema_name, table_name,
                   estimated_size, column_count, 'table' AS object_type
            FROM duckdb_tables() WHERE NOT internal
        """
        if include_views:
            sql += """
            UNION ALL
            SELECT database_name, schema_name, view_name,
                   NULL, column_count, 'view'
            FROM duckdb_views() WHERE NOT internal
            """
        sql += " ORDER BY schema_name, table_name"
        rows = self._run_select(sql)
        objects: list[ObjectMeta] = []
        for db, schema, name, est_size, col_count, obj_type in rows:
            objects.append(
                ObjectMeta(
                    identifier=f"{db}.{schema}.{name}",
                    object_type=obj_type,
                    schema=schema,
                    name=name,
                    # byte_size stays None: DuckDB has no cheap per-object byte
                    # size and a fabricated number would mislead ranking.
                    row_count=int(est_size) if est_size is not None else None,
                    byte_size=None,
                    column_count=int(col_count) if col_count is not None else 0,
                )
            )
        return objects

    def dev_namespace_objects(self, schema: str) -> list[str]:
        """Table and view names already in one schema of the attached file.
        Free: one catalog round-trip, local. A schema that does not exist
        yields no rows, i.e. nothing to collide with."""

        rows = self._run_select(
            """
            SELECT table_name FROM duckdb_tables()
            WHERE NOT internal AND schema_name = ?
            UNION ALL
            SELECT view_name FROM duckdb_views()
            WHERE NOT internal AND schema_name = ?
            """,
            [schema, schema],
        )
        return sorted(str(name) for (name,) in rows)

    def table_metadata(self, identifier: str) -> tuple[ObjectMeta, list[ColumnMeta]]:
        db, schema, name = self._split(identifier)
        col_rows = self._run_select(
            """
            SELECT column_name, data_type, is_nullable, column_index
            FROM duckdb_columns()
            WHERE database_name = ? AND schema_name = ? AND table_name = ?
            ORDER BY column_index
            """,
            [db, schema, name],
        )
        columns = [
            ColumnMeta(
                name=cname,
                data_type=str(dtype),
                nullable=bool(is_nullable),
                ordinal=int(idx),
            )
            for cname, dtype, is_nullable, idx in col_rows
        ]
        # Exact row count (one cheap aggregate), unlike the estimate in inventory.
        # The only interpolation is the quoted+escaped identifier (never a value);
        # the statement is parsed and refused if not a read-only SELECT.
        (row_count,) = self._run_select(
            f"SELECT COUNT(*) FROM {self._quote(identifier)}"  # noqa: S608
        )[0]
        meta = ObjectMeta(
            identifier=identifier,
            object_type="table",
            schema=schema,
            name=name,
            row_count=int(row_count),
            byte_size=None,
            column_count=len(columns),
        )
        return meta, columns

    def column_aggregates(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        *,
        safe_min_max: set[str] | None = None,
        shape_stats: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        safe = safe_min_max or set()
        shape = shape_stats or set()
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            results.extend(
                self._aggregate_batch(
                    identifier, columns[start : start + _COLUMN_BATCH], safe, shape
                )
            )
        return results

    def _aggregate_batch(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        shape: set[str],
    ) -> list[ColumnAggregate]:
        sql, plan = self._build_aggregate_sql(identifier, columns, safe, shape)
        row = self._run_select(sql)[0]
        # Re-read by alias name via the cursor description so we never rely on
        # column position arithmetic.
        labels = [d[0] for d in self._conn.description]
        values = dict(zip(labels, row, strict=True))

        n_total = int(values["n_total"])
        aggregates: list[ColumnAggregate] = []
        for i, col, wants_distinct, wants_min_max, wants_shape in plan:
            nn = int(values[f"nn_{i}"])
            null_fraction = (1 - nn / n_total) if n_total > 0 else None
            distinct = (
                int(values[f"nd_{i}"]) if wants_distinct and n_total > 0 else None
            )
            is_unique = (
                (distinct == nn == n_total and n_total > 0)
                if distinct is not None
                else None
            )
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=is_unique,
                    min_value=values.get(f"mn_{i}") if wants_min_max else None,
                    max_value=values.get(f"mx_{i}") if wants_min_max else None,
                    upper_vocab_fraction=shape_stat_value(
                        values, f"su_{i}", wants_shape
                    ),
                    person_shape_fraction=shape_stat_value(
                        values, f"sp_{i}", wants_shape
                    ),
                    avg_token_count=shape_stat_value(values, f"st_{i}", wants_shape),
                )
            )
        return aggregates

    def exact_distinct_counts(
        self, identifier: str, columns: list[str]
    ) -> dict[str, int]:
        """Exact COUNT(DISTINCT) per named column, batched into one statement
        per _COLUMN_BATCH group (roughly one scan each)."""

        results: dict[str, int] = {}
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            select_parts = [
                f"COUNT(DISTINCT {_quote_ident(name)}) AS d_{i}"
                for i, name in enumerate(batch)
            ]
            # Interpolated parts are quoted+escaped identifiers only; the result
            # is guarded as a read-only SELECT.
            sql = (
                f"SELECT {', '.join(select_parts)} "  # noqa: S608
                f"FROM {self._quote(identifier)}"
            )
            row = self._run_select(assert_select_only(sql, dialect=self.dialect))[0]
            labels = [d[0] for d in self._conn.description]
            values = dict(zip(labels, row, strict=True))
            for i, name in enumerate(batch):
                results[name] = int(values[f"d_{i}"])
        return results

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ) -> dict[tuple[str, ...], int]:
        """Exact distinct count per column combination, all in one statement
        (one scalar subquery each)."""

        if not combinations:
            return {}
        sql = distinct_combination_sql(
            self._quote(identifier), combinations, _quote_ident
        )
        row = self._run_select(assert_select_only(sql, dialect=self.dialect))[0]
        labels = [d[0] for d in self._conn.description]
        values = dict(zip(labels, row, strict=True))
        return {
            tuple(combo): int(values[f"d_{i}"]) for i, combo in enumerate(combinations)
        }

    def _build_aggregate_sql(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        shape: set[str],
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool]]]:
        # One aggregate query for the whole batch: COUNT(*) once, plus per column a
        # non-null count, an approximate distinct, min/max only where allowed, and
        # value-shape fractions only where requested.
        # Returns the SQL and a plan mapping each column to which aggregates it got,
        # so results read back by alias unambiguously. Pure: builds no connection,
        # so it is unit-testable (SELECT-only) without touching the database.
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            nested = self._is_nested(col.data_type)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not nested
            if wants_distinct:
                select_parts.append(f"approx_count_distinct({qcol}) AS nd_{i}")
            wants_min_max = (col.name in safe) and not nested
            if wants_min_max:
                select_parts.append(f"min({qcol}) AS mn_{i}")
                select_parts.append(f"max({qcol}) AS mx_{i}")
            wants_shape = (col.name in shape) and not nested
            if wants_shape:
                select_parts.extend(shape_stat_expressions(qcol, i, _regexp_predicate))
            plan.append((i, col, wants_distinct, wants_min_max, wants_shape))
        # Interpolated parts are quoted+escaped identifiers and fixed aggregate
        # keywords, never values; the result is guarded as a read-only SELECT.
        cols_sql = ", ".join(select_parts)
        sql = f"SELECT {cols_sql} FROM {self._quote(identifier)}"  # noqa: S608
        return assert_select_only(sql, dialect=self.dialect), plan

    @staticmethod
    def _is_nested(data_type: str) -> bool:
        upper = data_type.upper()
        return upper.endswith("[]") or upper.startswith(_NESTED_TYPE_PREFIXES)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(f"expected database.schema.name, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT, bounded in rows and wall time.

        SELECT-only is re-asserted as defense in depth; the PII policy already
        ran in the firewall. The watchdog interrupts the connection if the query
        outlives its budget, so a runaway scan cannot hold the session hostage.
        """

        assert_select_only(sql, dialect=self.dialect)
        expired = threading.Event()

        def _interrupt() -> None:
            expired.set()
            self._conn.interrupt()

        watchdog = threading.Timer(timeout_seconds, _interrupt)
        watchdog.start()
        try:
            cursor = self._conn.execute(sql)
            rows = cursor.fetchmany(max_rows + 1)
        except Exception as exc:
            if expired.is_set():
                raise TimeoutError(
                    f"query exceeded {timeout_seconds:g}s and was interrupted; "
                    "narrow it (tighter filter, fewer columns) and retry"
                ) from exc
            raise
        finally:
            watchdog.cancel()

        columns = [d[0] for d in self._conn.description]
        types = [str(d[1]) for d in self._conn.description]
        return QueryResult(
            columns=columns,
            types=types,
            cells=[[json_safe(v) for v in row] for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )

    def _run_select(self, sql: str, params: list | None = None):
        # Single read-only door for every query: parsed and refused if it is not a
        # SELECT, on top of the read-only connection.
        assert_select_only(sql, dialect=self.dialect)
        return self._conn.execute(sql, params or []).fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _quote_ident(name: str) -> str:
    """Quote one identifier component for DuckDB, doubling embedded quotes."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'
