"""The DuckDB adapter: first-class product connector and the eval/benchmark
engine. One implementation, three uses.

DuckDB is always opened read-only and bounded by memory/thread limits rather than
cost, because the work is free and local. This is the only adapter with real logic
today; it is what makes the whole loop buildable with no cloud accounts and
deterministic in CI.
"""

from __future__ import annotations

from pathlib import Path

from ..envelope import Paradigm
from ..guards.sql_guard import assert_select_only
from .base import ColumnAggregate, ColumnMeta, ObjectMeta

# Conservative defaults so auto-invoked profiling cannot exhaust the machine.
# Overridable from .dex/config.yml.
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4

# Columns are profiled in batches so a single statement against a very wide table
# does not balloon (4 expressions per column).
_COLUMN_BATCH = 50

# Nested types DuckDB cannot apply approx_count_distinct / min / max to cleanly.
_NESTED_TYPE_PREFIXES = ("STRUCT", "MAP", "LIST", "UNION")

# Text-like types eligible for value sketching. UUID is excluded: it reads as text
# but is an identifier, never a category.
_TEXTUAL_TYPE_HINTS = ("VARCHAR", "CHAR", "TEXT", "STRING", "ENUM")


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
    ) -> list[ColumnAggregate]:
        safe = safe_min_max or set()
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            results.extend(
                self._aggregate_batch(
                    identifier, columns[start : start + _COLUMN_BATCH], safe
                )
            )
        return results

    def column_top_values(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        *,
        k: int = 50,
    ) -> dict[str, list[tuple[object, int]]]:
        # One GROUP BY per column. The engine has already gated these to non-PII,
        # low-cardinality, short-valued text columns, so each scan is bounded; this
        # knowingly forgoes the batching the aggregate pass uses, because top-K
        # cannot share a single statement across columns without grouping-set
        # gymnastics. NULLs are excluded (null_fraction already conveys them). The
        # value is the deterministic tie-break, which is well-defined because
        # GROUP BY makes the values distinct.
        out: dict[str, list[tuple[object, int]]] = {}
        table = self._quote(identifier)
        limit = int(k)
        for col in columns:
            if self._is_nested(col.data_type):
                continue
            qcol = _quote_ident(col.name)
            # Interpolated parts are quoted identifiers and an int limit, never
            # values; _run_select refuses anything but a read-only SELECT.
            sql = (
                f"SELECT {qcol} AS sketch_value, COUNT(*) AS sketch_count "  # noqa: S608
                f"FROM {table} WHERE {qcol} IS NOT NULL "
                f"GROUP BY {qcol} ORDER BY sketch_count DESC, sketch_value ASC "
                f"LIMIT {limit}"
            )
            rows = self._run_select(sql)
            if rows:
                out[col.name] = [(value, int(count)) for value, count in rows]
        return out

    def _aggregate_batch(
        self, identifier: str, columns: list[ColumnMeta], safe: set[str]
    ) -> list[ColumnAggregate]:
        sql, plan = self._build_aggregate_sql(identifier, columns, safe)
        row = self._run_select(sql)[0]
        # Re-read by alias name via the cursor description so we never rely on
        # column position arithmetic.
        labels = [d[0] for d in self._conn.description]
        values = dict(zip(labels, row, strict=True))

        n_total = int(values["n_total"])
        aggregates: list[ColumnAggregate] = []
        for i, col, wants_distinct, wants_min_max, wants_length in plan:
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
            # NULL when the column is all-null (max of no rows); leave it None then.
            ml = values.get(f"ml_{i}") if wants_length else None
            max_length = int(ml) if ml is not None else None
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=is_unique,
                    min_value=values.get(f"mn_{i}") if wants_min_max else None,
                    max_value=values.get(f"mx_{i}") if wants_min_max else None,
                    max_length=max_length,
                )
            )
        return aggregates

    def _build_aggregate_sql(
        self, identifier: str, columns: list[ColumnMeta], safe: set[str]
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool]]]:
        # One aggregate query for the whole batch: COUNT(*) once, plus per column a
        # non-null count, an approximate distinct, min/max only where allowed, and
        # for text columns the longest value's length (so the engine can gate value
        # sketching without an extra scan). Returns the SQL and a plan mapping each
        # column to which aggregates it got, so results read back by alias
        # unambiguously. Pure: builds no connection, so it is unit-testable
        # (SELECT-only) without touching the database.
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
            wants_length = self._is_textual(col.data_type) and not nested
            if wants_length:
                select_parts.append(f"max(length(CAST({qcol} AS VARCHAR))) AS ml_{i}")
            plan.append((i, col, wants_distinct, wants_min_max, wants_length))
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
    def _is_textual(data_type: str) -> bool:
        upper = data_type.upper()
        if "UUID" in upper:
            return False
        return any(h in upper for h in _TEXTUAL_TYPE_HINTS)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(f"expected database.schema.name, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

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
