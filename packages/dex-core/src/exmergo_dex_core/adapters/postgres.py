"""The postgres adapter: the operational-database connector (db-load paradigm).

Postgres bills no dollars; the guarded quantity is load on the database dex is
pointed at, which is often a production OLTP primary. The budget is therefore
denominated in database-seconds, and every billed statement is capped
server-side by a ``statement_timeout`` wound down to what remains of the
budget, so a wrong estimate cannot overrun the ceiling: Postgres kills the
statement. Actual spend is wall-clock seconds per statement, recorded to the
ledger as ``billed_seconds``.

The cost split mirrors Snowflake's inversion, not BigQuery's: metadata is
cheap (``pg_catalog`` lookups, no table scans), while any data scan loads the
server. Inventory, ``connect test``, ``pg_stats`` reads, and ``EXPLAIN`` stay
free; profiling aggregates, distinct-count escalations, and agent queries are
metered. Unlike Snowflake, Postgres has a genuinely free planner preflight:
query estimates come from ``EXPLAIN (FORMAT JSON)`` (which knows about
indexes), translated to seconds through a conservative scan-rate heuristic and
labeled as such.

Profiling is deliberately light on the primary: the billed batch is one cheap
single-pass scan (COUNT(*), per-column non-null counts, min/max only for
engine-cleared safe columns), and distinct counts come free from the planner's
own statistics (``pg_stats.n_distinct``, never the value-carrying histogram
columns). Near-unique keys then escalate to an exact COUNT(DISTINCT) inside
the confirmed budget through the standard escalation flow.

Read-only is enforced in depth: ``default_transaction_read_only = on`` on the
session, the SELECT-only guard in the postgres dialect on every data statement
through one execution door, an adapter that issues no mutating statements
(catalog SELECTs / EXPLAIN / session SETs only), and the documented
least-privilege read-only role.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ..config import PostgresTarget
from ..envelope import Paradigm
from ..guards.cost_guard import CostGate, OverCeilingError
from ..guards.sql_guard import assert_select_only
from .base import ColumnAggregate, ColumnMeta, ObjectMeta, QueryResult, json_safe

PARADIGM = "db_load"
DIALECT = "postgres"

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 3 expressions per column).
_COLUMN_BATCH = 50

# The estimate heuristic: a deliberately conservative sequential scan rate.
_SCAN_BYTES_PER_SECOND = 50 * 1024 * 1024

# The planner's cost unit is calibrated so seq_page_cost = 1.0 is one page
# read; translating plan cost through the page size errs conservative (CPU
# cost components inflate the byte figure).
_BYTES_PER_PLANNER_PAGE = 8192

# Every billed statement estimates at least this much.
_MIN_STATEMENT_SECONDS = 0.5

# Types where distinct counts and min/max are invalid or meaningless: only a
# non-null count is computed. Matched against format_type output (lowercase),
# plus any array type by its [] suffix. The geometric types matter because
# "point" contains "int", which the engine's numeric hint would otherwise
# clear for min/max Postgres cannot order.
_DEGRADED_TYPE_PREFIXES = (
    "json",
    "jsonb",
    "bytea",
    "xml",
    "tsvector",
    "tsquery",
    "geometry",
    "geography",
    "point",
    "line",
    "lseg",
    "box",
    "path",
    "polygon",
    "circle",
)

_ESTIMATE_QUALITY_NOTE = (
    "Postgres bills no dollars; the guarded quantity is load on the database, "
    "expressed as database-seconds. Query estimates come from the free "
    "planner (EXPLAIN); profile estimates from relation sizes. The confirmed "
    "budget is hard-enforced per statement by a server-side statement_timeout"
)


class PostgresConnectionError(Exception):
    """Raised when a queried object cannot be resolved in the configured
    scope. The message always names the fix, never a credential."""


class PostgresAdapter:
    """Holds one Postgres connection plus the cost gate for one command.

    ``connection`` is injectable (class DI) so unit tests drive a fake; the
    real connection is built by ``connect.py`` from discovered parameters
    (autocommit, ``application_name=dex``). Credentials live only inside this
    process and are never surfaced. ``clock`` is injectable so the fake can
    simulate statement duration; it is what actual billed seconds are
    measured with.
    """

    name = "postgres"
    dialect = DIALECT
    paradigm = Paradigm.DB_LOAD

    def __init__(
        self,
        *,
        connection: Any,
        cost_gate: CostGate,
        target: PostgresTarget | None = None,
        auth_method: str = "unknown",
        clock: Callable[[], float] = time.monotonic,
    ):
        self._conn = connection
        self.cost_gate = cost_gate
        self.target = target or PostgresTarget()
        self.auth_method = auth_method
        self._clock = clock
        # Imported lazily (the caller constructed the connection, so the
        # library is present); the error types drive refusal translation.
        from psycopg import errors as pg_errors

        self._pg_errors = pg_errors
        # Catalog results are cached per command: the estimate pass and the
        # confirmed run share table facts, and each lookup is free but a
        # round-trip.
        self._objects: dict[str, dict] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._stats: dict[str, dict[str, float]] = {}
        self._exact_rows: dict[str, int] = {}
        self._inventory_loaded = False
        self._database: str | None = None
        self._notes: dict[str, list[str]] = {}
        self._session_prepared = False

    # --- capabilities (free) ---------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # The probe round-trips to the server (current settings, not cached
        # facts), so a stale or underprivileged credential fails here instead
        # of reporting a healthy connection.
        probe = self._catalog(
            "SELECT current_database() AS db, "
            "current_setting('server_version') AS server_version, "
            "current_setting('default_transaction_read_only') AS read_only"
        )[0]
        self._database = str(probe["db"])
        cost = self.cost_gate.cost()
        return {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "session_read_only": str(probe["read_only"]).lower() == "on",
            "paradigm": self.paradigm.value,
            "auth_method": self.auth_method,
            "database": self._database,
            "server_version": str(probe["server_version"]).split()[0],
            "schema_count": len(self._schema_scope()),
            "required_grants": [
                "USAGE on source schemas and SELECT on their tables",
                "write only on the dedicated dbt dev schema (transform build)",
            ],
            "budget": {
                "ceiling_seconds": cost.ceiling,
                "session_spent_today_seconds": self.cost_gate.session_spent,
            },
        }

    # --- introspection (free pg_catalog metadata; no scans) --------------------

    def list_objects(self, *, include_views: bool = True) -> list[ObjectMeta]:
        self._load_inventory()
        objects = [
            self._object_meta(entry)
            for entry in self._objects.values()
            if include_views or entry["object_type"] != "view"
        ]
        objects.sort(key=lambda o: o.identifier)
        return objects

    def table_metadata(self, identifier: str) -> tuple[ObjectMeta, list[ColumnMeta]]:
        self._load_inventory()
        entry = self._objects.get(identifier)
        if entry is None:
            raise PostgresConnectionError(
                f"object '{identifier}' not found in the configured scope; "
                "check postgres.schemas in .dex/config.yml (identifiers are "
                "database.schema.table against the connected database)"
            )
        return self._object_meta(entry), list(self._columns.get(identifier, []))

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the profiling run accumulated for one object
        (sampling degradation, skipped escalations, missing planner stats).
        Merged into the dataset's ``data_quality`` by the profile engine."""

        return list(self._notes.get(identifier, []))

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        database = self._current_database()
        allowed = set(self.target.schemas)
        # relkind: r table, p partitioned table, f foreign table, m
        # materialized view, v view. reltuples is the planner's estimate (-1
        # means never analyzed); pg_total_relation_size is a catalog lookup,
        # not a scan.
        rows = self._catalog(
            "SELECT n.nspname AS schema_name, c.relname AS object_name, "
            "c.relkind AS kind, c.reltuples AS reltuples, "
            "pg_catalog.pg_total_relation_size(c.oid) AS total_bytes "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'f', 'm', 'v') "
            "AND n.nspname <> 'information_schema' "
            "AND n.nspname NOT LIKE 'pg\\_%'"
        )
        for row in rows:
            schema = str(row["schema_name"])
            if allowed and schema not in allowed:
                continue
            object_type = "view" if str(row["kind"]) in ("v", "m") else "table"
            reltuples = float(row["reltuples"])
            size = int(row["total_bytes"]) if row["total_bytes"] else None
            identifier = f"{database}.{schema}.{row['object_name']}"
            self._objects[identifier] = {
                "identifier": identifier,
                "object_type": object_type,
                "schema": schema,
                "name": str(row["object_name"]),
                "row_count": int(reltuples) if reltuples >= 0 else None,
                "byte_size": size if object_type == "table" or size else None,
                "column_count": 0,
            }
        columns = self._catalog(
            "SELECT n.nspname AS schema_name, c.relname AS object_name, "
            "a.attname AS column_name, "
            "pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type, "
            "NOT a.attnotnull AS nullable "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE a.attnum > 0 AND NOT a.attisdropped "
            "AND c.relkind IN ('r', 'p', 'f', 'm', 'v') "
            "AND n.nspname <> 'information_schema' "
            "AND n.nspname NOT LIKE 'pg\\_%' "
            "ORDER BY n.nspname, c.relname, a.attnum"
        )
        for row in columns:
            identifier = f"{database}.{row['schema_name']}.{row['object_name']}"
            entry = self._objects.get(identifier)
            if entry is None:
                continue
            metas = self._columns.setdefault(identifier, [])
            metas.append(
                ColumnMeta(
                    name=str(row["column_name"]),
                    data_type=str(row["data_type"]),
                    nullable=bool(row["nullable"]),
                    ordinal=len(metas),
                )
            )
            entry["column_count"] = len(metas)
        self._inventory_loaded = True

    def _object_meta(self, entry: dict) -> ObjectMeta:
        # An exact count from a profiling scan supersedes the planner estimate
        # for the rest of the command, so uniqueness proofs and drift verdicts
        # compare against real rows, not reltuples.
        row_count = self._exact_rows.get(entry["identifier"], entry["row_count"])
        return ObjectMeta(
            identifier=entry["identifier"],
            object_type=entry["object_type"],
            schema=entry["schema"],
            name=entry["name"],
            row_count=row_count,
            byte_size=entry["byte_size"],
            column_count=entry["column_count"],
        )

    def _current_database(self) -> str:
        if self._database is None:
            row = self._catalog("SELECT current_database() AS db")[0]
            self._database = str(row["db"])
        return self._database

    def _schema_scope(self) -> list[str]:
        """The allowlisted schemas, or every visible non-system schema when no
        allowlist is configured."""

        if self.target.schemas:
            return sorted(set(self.target.schemas))
        rows = self._catalog(
            "SELECT nspname FROM pg_catalog.pg_namespace "
            "WHERE nspname <> 'information_schema' "
            "AND nspname NOT LIKE 'pg\\_%'"
        )
        return sorted(str(row["nspname"]) for row in rows)

    def _table_stats(self, identifier: str) -> dict[str, float]:
        """Free per-table distinct estimates from the planner's statistics.

        Reads only ``attname`` and ``n_distinct``, never the value-carrying
        statistics columns (most_common_vals, histogram_bounds), so no row
        value can cross this door.
        """

        if identifier not in self._stats:
            _db, schema, table = self._split(identifier)
            # Interpolated parts are escaped identifiers that came from the
            # catalog itself, never values; the door only accepts SELECTs.
            rows = self._catalog(
                "SELECT attname, n_distinct FROM pg_catalog.pg_stats "  # noqa: S608
                f"WHERE schemaname = '{_escape_literal(schema)}' "
                f"AND tablename = '{_escape_literal(table)}'"
            )
            self._stats[identifier] = {
                str(row["attname"]): float(row["n_distinct"])
                for row in rows
                if row["n_distinct"] is not None
            }
        return self._stats[identifier]

    # --- estimation (free; feeds the confirm handshake) -------------------------

    def profile_estimate(
        self, identifiers: list[str]
    ) -> tuple[float, dict[str, float]]:
        """The heuristic database-seconds estimate for profiling: per table,
        its bytes over a conservative scan rate times the number of aggregate
        batches. Free: everything comes from catalog metadata."""

        per_table: dict[str, float] = {}
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            batches = max((len(columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = batches * self._scan_seconds(meta.byte_size)
        return sum(per_table.values()), per_table

    def query_estimate(self, sql: str) -> float:
        """The estimate for one firewall-approved query, from the free planner
        preflight (EXPLAIN knows about indexes, so a point lookup on a huge
        table is not quoted as a full scan), falling back to summed
        referenced-table bytes when the plan cannot be read."""

        checked = assert_select_only(sql, dialect=self.dialect)
        return self._statement_estimate(checked)

    def _statement_estimate(self, sql: str) -> float:
        cost = self._plan_cost(sql)
        if cost is not None:
            return max(
                cost * _BYTES_PER_PLANNER_PAGE / _SCAN_BYTES_PER_SECOND,
                _MIN_STATEMENT_SECONDS,
            )
        total_bytes = 0
        known = 0
        self._load_inventory()
        for identifier in self._referenced_tables(sql):
            entry = self._objects.get(identifier)
            if entry and entry["byte_size"] is not None:
                total_bytes += entry["byte_size"]
                known += 1
        if known == 0:
            return _MIN_STATEMENT_SECONDS
        return self._scan_seconds(total_bytes)

    def _plan_cost(self, sql: str) -> float | None:
        """Total planner cost of one SELECT via the free EXPLAIN door, or
        ``None`` when the plan cannot be produced or read (the caller then
        falls back to the size heuristic)."""

        try:
            rows = self._explain(sql)
            document = rows[0][0]
            if isinstance(document, str):
                document = json.loads(document)
            return float(document[0]["Plan"]["Total Cost"])
        except Exception:
            return None

    def _scan_seconds(self, byte_size: int | None) -> float:
        if not byte_size:
            return _MIN_STATEMENT_SECONDS
        return max(byte_size / _SCAN_BYTES_PER_SECOND, _MIN_STATEMENT_SECONDS)

    def _referenced_tables(self, sql: str) -> set[str]:
        try:
            import sqlglot
            from sqlglot import expressions as sqlglot_exp

            parsed = sqlglot.parse_one(sql, read=self.dialect)
        except Exception:
            return set()
        database = self._current_database()
        identifiers = set()
        for table in parsed.find_all(sqlglot_exp.Table):
            parts = [p for p in (table.catalog, table.db, table.name) if p]
            if len(parts) == 2:
                parts.insert(0, database)
            identifiers.add(".".join(parts))
        return identifiers

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The db-load handshake payload: database-seconds are the binding
        number; there is no currency translation because nothing is billed."""

        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in database-seconds; the same number "
                "becomes the server-side statement_timeout)"
            ),
            "notes": [_ESTIMATE_QUALITY_NOTE],
        }
        if per_table:
            data["per_table_seconds"] = per_table
        return data

    def spend_display(self) -> dict:
        """No currency translation exists for database load; the seconds in
        the spend summary are the whole story."""

        return {}

    # --- profiling (billed; every statement estimated and gated) ----------------

    def column_aggregates(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        *,
        safe_min_max: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        safe = safe_min_max or set()
        meta, _ = self.table_metadata(identifier)
        sample_percent = self._sample_percent(identifier, meta.byte_size)
        stats = self._table_stats(identifier)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(
                identifier, batch, safe, sample_percent=sample_percent
            )
            rows, labels = self._execute(
                sql, estimate=self._scan_seconds(meta.byte_size)
            )
            values = dict(zip(labels, rows[0], strict=True))
            n_total = int(values["n_total"])
            if sample_percent is None:
                self._exact_rows[identifier] = n_total
            results.extend(
                self._read_aggregates(
                    values,
                    plan,
                    stats,
                    row_basis=self._distinct_row_basis(identifier, meta),
                    sampled=sample_percent is not None,
                )
            )
        self._note_missing_stats(identifier, columns, stats)
        return results

    def _distinct_row_basis(self, identifier: str, meta: ObjectMeta) -> int | None:
        """The whole-table row count negative ``n_distinct`` ratios scale by:
        the exact count when a full scan produced one, else the planner
        estimate."""

        return self._exact_rows.get(identifier, meta.row_count)

    def _sample_percent(self, identifier: str, byte_size: int | None) -> float | None:
        threshold = self.target.max_full_profile_bytes
        if threshold is None or not byte_size or byte_size <= threshold:
            return None
        percent = max(round(100.0 * threshold / byte_size, 2), 0.01)
        self._note(
            identifier,
            f"profiled from a ~{percent}% block sample (table exceeds "
            "postgres.max_full_profile_bytes); counts and extremes are "
            "approximate and uniqueness is not judged",
        )
        return percent

    def _build_aggregate_sql(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        *,
        sample_percent: float | None = None,
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool]]]:
        # One aggregate statement per batch, a single cheap pass: COUNT(*)
        # once, then per column a non-null count and min/max only where
        # allowed. Distinct counts deliberately do NOT scan here (they come
        # free from pg_stats); COUNT(DISTINCT) across every column is exactly
        # the sort/hash load a production primary should not carry. Pure (no
        # connection), so the SELECT-only property is testable offline.
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            degraded = self._is_degraded(col.data_type)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not degraded
            wants_min_max = (col.name in safe) and not degraded
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            plan.append((i, col, wants_distinct, wants_min_max))
        source = self._quote(identifier)
        if sample_percent is not None:
            source += f" TABLESAMPLE SYSTEM ({sample_percent})"
        # Interpolated parts are quoted identifiers and fixed aggregate
        # keywords, never values; the result is guarded as a read-only SELECT.
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"  # noqa: S608
        return assert_select_only(sql, dialect=self.dialect), plan

    @staticmethod
    def _is_degraded(data_type: str) -> bool:
        lowered = data_type.lower()
        return lowered.endswith("[]") or lowered.startswith(_DEGRADED_TYPE_PREFIXES)

    @staticmethod
    def _read_aggregates(
        values: dict,
        plan: list[tuple[int, ColumnMeta, bool, bool]],
        stats: dict[str, float],
        *,
        row_basis: int | None,
        sampled: bool,
    ) -> list[ColumnAggregate]:
        n_total = int(values["n_total"])
        aggregates: list[ColumnAggregate] = []
        for i, col, wants_distinct, wants_min_max in plan:
            nn = values.get(f"nn_{i}")
            null_fraction = (
                (1 - int(nn) / n_total) if nn is not None and n_total > 0 else None
            )
            distinct: int | None = None
            if wants_distinct and n_total > 0:
                n_distinct = stats.get(col.name)
                if n_distinct is not None:
                    if n_distinct >= 0:
                        distinct = int(n_distinct)
                    elif row_basis is not None:
                        # Negative n_distinct is a ratio of the row count
                        # (-1 means "all rows distinct").
                        distinct = round(-n_distinct * row_basis)
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    # pg_stats estimates are never a uniqueness verdict; the
                    # near-unique escalation proves keys with an exact scan.
                    distinct_count=distinct,
                    is_unique=None,
                    min_value=(
                        json_safe(values.get(f"mn_{i}")) if wants_min_max else None
                    ),
                    max_value=(
                        json_safe(values.get(f"mx_{i}")) if wants_min_max else None
                    ),
                )
            )
        return aggregates

    def _note_missing_stats(
        self, identifier: str, columns: list[ColumnMeta], stats: dict[str, float]
    ) -> None:
        missing = [
            col.name
            for col in columns
            if not self._is_degraded(col.data_type) and col.name not in stats
        ]
        if missing:
            self._note(
                identifier,
                f"no planner statistics for {len(missing)} column(s); distinct "
                "counts are unavailable until ANALYZE runs on this table",
            )

    def exact_distinct_counts(
        self, identifier: str, columns: list[str]
    ) -> dict[str, int]:
        """Exact COUNT(DISTINCT) for near-unique columns, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scan, return nothing and let uniqueness verdicts stay
        approximate. A metered adapter never self-escalates past its ceiling.
        """

        if not columns:
            return {}
        meta, _ = self.table_metadata(identifier)
        estimate = self._scan_seconds(meta.byte_size)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "distinct-count escalation skipped: the remaining budget could "
                "not cover the extra scan; uniqueness verdicts stay approximate",
            )
            return {}
        # COUNT(*) rides along so the same scan also upgrades the planner's
        # row estimate to an exact count (grain verdicts compare against it).
        select_parts = ["COUNT(*) AS n_total"] + [
            f"COUNT(DISTINCT {_quote_ident(name)}) AS d_{i}"
            for i, name in enumerate(columns)
        ]
        sql = assert_select_only(
            f"SELECT {', '.join(select_parts)} FROM {self._quote(identifier)}",  # noqa: S608
            dialect=self.dialect,
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        self._exact_rows[identifier] = int(values["n_total"])
        return {name: int(values[f"d_{i}"]) for i, name in enumerate(columns)}

    # --- execution (the single billed door) --------------------------------------

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT, bounded in rows, wall time,
        and database-seconds (client preflight plus the server-side statement
        timeout, whichever is tighter)."""

        checked = assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(self._statement_estimate(checked))
        rows, labels, types = self._run_rows(
            checked, timeout_seconds=timeout_seconds, fetch_rows=max_rows + 1
        )
        return QueryResult(
            columns=labels,
            types=types,
            cells=[[json_safe(v) for v in row] for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )

    def _execute(self, sql: str, *, estimate: float) -> tuple[list, list[str]]:
        """SELECT-only guard, heuristic charge, then the timeout-capped run."""

        assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(estimate)
        return self._run(sql)

    def _run(self, sql: str) -> tuple[list, list[str]]:
        rows, labels, _types = self._run_rows(sql)
        return rows, labels

    def _run_rows(
        self,
        sql: str,
        *,
        timeout_seconds: float | None = None,
        fetch_rows: int | None = None,
    ) -> tuple[list, list[str], list[str]]:
        """The single billed door past the gate: the server-side
        statement_timeout is wound down to what remains of the budget (or the
        wall-clock limit when that is tighter), and wall-clock elapsed is
        recorded to the ledger."""

        cursor, budget_bound = self._billed_cursor(timeout_seconds)
        started = self._clock()
        try:
            cursor.execute(sql)
            rows = (
                cursor.fetchmany(fetch_rows)
                if fetch_rows is not None
                else cursor.fetchall()
            )
        except self._pg_errors.QueryCanceled as exc:
            self._record_elapsed(started, sql)
            raise self._timeout_refusal(budget_bound, timeout_seconds) from exc
        except self._pg_errors.ReadOnlySqlTransaction as exc:
            self._record_elapsed(started, sql)
            raise PostgresConnectionError(
                "the statement attempted a write and the session is read-only "
                "by construction; dex never mutates the database"
            ) from exc
        self._record_elapsed(started, sql)
        labels = [str(d.name) for d in cursor.description]
        types = [self._description_type(d) for d in cursor.description]
        return rows, labels, types

    def _billed_cursor(self, timeout_seconds: float | None) -> tuple[Any, bool]:
        """A cursor prepared for spend: session read-only asserted, and the
        server-side statement timeout wound down to what remains of the
        budget, so even a wrong heuristic cannot overrun the ceiling. Returns
        the cursor and whether the budget (not the wall clock) is the binding
        bound."""

        remaining = self.cost_gate.remaining_for_statement()
        if remaining is not None and remaining < 1:
            raise OverCeilingError(
                "the remaining budget is under one database-second; raise "
                "--budget or narrow the work"
            )
        self._ensure_session()
        cursor = self._conn.cursor()
        timeout_ms: int | None = None
        budget_bound = False
        if remaining is not None:
            timeout_ms = int(remaining) * 1000
            budget_bound = True
        if timeout_seconds is not None:
            wall_ms = int(max(timeout_seconds, 1) * 1000)
            if timeout_ms is None or wall_ms < timeout_ms:
                timeout_ms = wall_ms
                budget_bound = False
        if timeout_ms is not None:
            # Engine-built session statement, not agent SQL: the value is a
            # computed integer of milliseconds.
            cursor.execute(f"SET statement_timeout = {timeout_ms}")
        return cursor, budget_bound

    def _timeout_refusal(
        self, budget_bound: bool, timeout_seconds: float | None
    ) -> Exception:
        if budget_bound:
            return OverCeilingError(
                "the statement hit the server-side statement_timeout derived "
                "from the remaining budget; raise --budget or narrow the work"
            )
        limit = f"{timeout_seconds:g}s" if timeout_seconds is not None else "its limit"
        return TimeoutError(
            f"query exceeded {limit} and was cancelled; narrow it (tighter "
            "filter, fewer columns) and retry"
        )

    def _record_elapsed(self, started: float, sql: str) -> None:
        elapsed = max(self._clock() - started, 0.0)
        self.cost_gate.record_billed(elapsed, job_id=None, statement=sql)

    def _ensure_session(self) -> None:
        """Session preparation, once per command: reads stay reads even if a
        later statement tried otherwise. Engine-built constants, not agent
        SQL."""

        if self._session_prepared:
            return
        cursor = self._conn.cursor()
        cursor.execute("SET default_transaction_read_only = on")
        self._session_prepared = True

    @staticmethod
    def _description_type(description: Any) -> str:
        type_code = getattr(description, "type_code", None)
        try:
            from psycopg import postgres

            info = postgres.types.get(type_code)
        except Exception:
            info = None
        if info is not None:
            return str(info.name)
        return str(getattr(type_code, "name", type_code))

    # --- helpers ------------------------------------------------------------------

    def _catalog(self, sql: str) -> list[dict]:
        """Free metadata door: engine-built catalog SELECTs only (pg_catalog
        lookups and session settings, no table scans). Results come back as
        dicts keyed by the column names."""

        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("only SELECT statements pass through the catalog door")
        self._ensure_session()
        cursor = self._conn.cursor()
        cursor.execute(sql)
        labels = [str(d.name) for d in cursor.description]
        return [dict(zip(labels, row, strict=True)) for row in cursor.fetchall()]

    def _explain(self, sql: str) -> list:
        """Free planner door: the statement is guarded SELECT-only, then
        prefixed with EXPLAIN here, so nothing but a plan request can pass."""

        checked = assert_select_only(sql, dialect=self.dialect)
        self._ensure_session()
        cursor = self._conn.cursor()
        cursor.execute(f"EXPLAIN (FORMAT JSON) {checked}")
        return cursor.fetchall()

    def _note(self, identifier: str, note: str) -> None:
        notes = self._notes.setdefault(identifier, [])
        if note not in notes:
            notes.append(note)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(f"expected database.schema.table, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        # A three-part reference is valid Postgres only against the connected
        # database, which is exactly what the namespace guarantees.
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()


def _quote_ident(name: str) -> str:
    """Quote one identifier component with double quotes (preserving case,
    which unquoted Postgres identifiers would fold to lower), doubling
    embedded quotes."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_literal(value: str) -> str:
    return value.replace("'", "''")
