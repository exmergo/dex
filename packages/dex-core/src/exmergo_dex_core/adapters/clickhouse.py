"""The clickhouse adapter: the self-hosted analytical connector (db-load paradigm).

Self-hosted ClickHouse bills no dollars, but a scan is real load on a server that
is usually shared and usually serving something latency-sensitive. The guarded
quantity is therefore that load, denominated in database-seconds exactly as on
Postgres, and every billed statement is capped server-side by settings wound down
to what remains of the budget, so a wrong estimate cannot overrun the ceiling.

Three things make the cost lifecycle unusually good here, and they are why the
paradigm is db-load rather than free:

- The estimate is free and does not execute. ``EXPLAIN ESTIMATE`` reports the
  rows and marks a statement would read *after* primary-key and partition
  pruning, so a filtered probe on a huge table is not quoted as a full scan. It
  covers the MergeTree family only, so Log, Memory, Distributed and view
  relations fall back to ``system.tables`` sizes; ``estimate_basis`` on the
  handshake payload says which of the two priced a given command.
- The cap is layered. ``max_execution_time`` alone is checked at block
  boundaries and can be exceeded, so every billed statement also carries
  ``max_bytes_to_read`` derived by inverting the same throughput constant the
  estimate used. Bytes are the cap that actually binds on a fast scan.
- Settlement is free and exact. Every response carries the ``X-ClickHouse-Summary``
  header, so real elapsed nanoseconds, rows read and bytes read come back with
  the result rather than costing a second query or a delayed ``system.query_log``
  poll. Ledger seconds here are server-side elapsed, not the client wall clock.

Identifiers are two-part, ``database.table``. ClickHouse has no catalog level and
dbt-clickhouse's ``schema:`` is the ClickHouse database, so a synthesized third
component would be a name that appears in the cache, the inventory and every
drift finding while being untypeable in ``clickhouse-client``. Every shared
consumer of an identifier is arity-agnostic, so nothing downstream needs the
fiction.

Read-only is enforced in depth: ``readonly = 2`` and ``allow_ddl = 0`` on the
session (2 rather than 1 because 1 also forbids changing settings, which would
block dex from sending its own per-statement caps), the SELECT-only guard in the
clickhouse dialect on every data statement through one execution door, an adapter
that issues no mutating statements, and the documented least-privilege user. Note
that ``readonly = 2`` permits a session to raise its own settings again, so the
cap is self-imposed exactly as Postgres's ``SET statement_timeout`` is; the
unraisable form is a server-side settings constraint on the dex user, which
``references/clickhouse.md`` documents under required grants.

ClickHouse also has no foreign keys, so every relationship dex reports here is
name-and-shape inference; and ``ORDER BY`` is a sort key, not a uniqueness
constraint, so ``is_in_primary_key`` raises the prior on a candidate key without
ever declaring one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import ClickHouseTarget
from ..envelope import Paradigm
from ..errors import ConnectorError
from ..guards.cost_guard import CostGate, OverCeilingError
from ..guards.sql_guard import assert_select_only
from .base import (
    ColumnAggregate,
    ColumnMeta,
    ObjectMeta,
    QueryResult,
    ValueDomainSample,
    affordable_combinations,
    blame,
    distinct_combination_sql,
    is_blob_type,
    is_integer_type,
    is_string_type,
    json_safe,
    key_shape_aggregate_kwargs,
    key_shape_expressions,
    name_list,
    shape_stat_expressions,
    shape_stat_value,
    temporal_alignment_expressions,
    temporal_continuity_aggregate_kwargs,
    temporal_continuity_sql,
    temporal_units_for,
    type_contradiction_aggregate_kwargs,
    type_contradiction_expressions,
)

PARADIGM = "db_load"
DIALECT = "clickhouse"

_BIGINT_TYPE = "Int64"

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 3 expressions per column).
_COLUMN_BATCH = 50

# The estimate heuristic: a deliberately conservative scan rate. ClickHouse is
# considerably faster than this on warm data, which is the direction an estimate
# should err, and it is also what the byte cap is derived from, so a too-generous
# rate would loosen the backstop as well as the quote.
_SCAN_BYTES_PER_SECOND = 200 * 1024 * 1024

# Used when EXPLAIN ESTIMATE reports rows for a table whose average row width
# cannot be read (an empty table, or one whose totals the engine will not
# quickly determine). Wide enough to not under-quote a typical analytical row.
_FALLBACK_BYTES_PER_ROW = 64

# Every billed statement estimates at least this much.
_MIN_STATEMENT_SECONDS = 0.5

# Server error codes translated rather than surfaced raw. 159 TIMEOUT_EXCEEDED,
# 158 TOO_MANY_ROWS, 307 TOO_MANY_BYTES and 396 TOO_MANY_ROWS_OR_BYTES are the
# four ways a statement can hit a read limit, and they have to read as a budget
# refusal rather than as a server fault.
_TIMEOUT_CODE = 159
_LIMIT_CODES = frozenset({158, 307, 396})
_UNKNOWN_TABLE_CODE = 60
_ACCESS_DENIED_CODE = 497

# Types where distinct counts and min/max are invalid or meaningless: only a
# non-null count is computed. Matched against the unwrapped type name, so
# `Array(String)` and `Map(String, String)` are caught by their constructor.
_DEGRADED_TYPE_PREFIXES = (
    "array",
    "map",
    "tuple",
    "nested",
    "json",
    "object",
    "aggregatefunction",
    "simpleaggregatefunction",
    "point",
    "ring",
    "polygon",
    "multipolygon",
    "linestring",
    "multilinestring",
)

# Engines that keep more than one row per sorting key until a background merge
# collapses them. A uniqueness verdict on one of these is a statement about the
# stored parts, not about the modeled grain, and saying so is the difference
# between a real finding and a permanent false alarm.
_COLLAPSING_ENGINES = (
    "ReplacingMergeTree",
    "CollapsingMergeTree",
    "VersionedCollapsingMergeTree",
    "SummingMergeTree",
    "AggregatingMergeTree",
)

_ESTIMATE_QUALITY_NOTE = (
    "Self-hosted ClickHouse bills no dollars; the guarded quantity is load on "
    "the server, expressed as database-seconds. Row estimates come from the "
    "free, non-executing EXPLAIN ESTIMATE on MergeTree tables and from "
    "system.tables elsewhere; seconds are those rows over a conservative scan "
    "rate. The confirmed budget is enforced per statement by a server-side "
    "max_execution_time and max_bytes_to_read"
)

_CLOUD_REFUSAL = (
    "clickhouse.deployment is 'cloud', and dex does not yet model ClickHouse "
    "Cloud spend. Cloud bills compute-unit-hours; dex guards this connector in "
    "database-seconds, so a budget you confirmed here would not bound what the "
    "service charges you. Point the connector at a self-hosted server, or set "
    "deployment: self_hosted if this endpoint is in fact self-hosted"
)

_CLOUD_CORROBORATION_REFUSAL = (
    "this endpoint reports cloud_mode = 1 (ClickHouse Cloud) while "
    "clickhouse.deployment is 'self_hosted'. Cloud bills compute-unit-hours, "
    "which dex does not yet model, so the database-seconds budget this "
    "connector guards with would not bound what the service charges you. dex "
    "refuses rather than guarding in the wrong unit, because a ceiling that "
    "reports a number which did not bind is worse than no ceiling at all"
)

# Wrapper constructors that decorate a type without changing what it holds.
_TYPE_WRAPPERS = ("Nullable", "LowCardinality")
_WRAPPER_RE = re.compile(
    r"^(?:" + "|".join(_TYPE_WRAPPERS) + r")\((.*)\)$", re.IGNORECASE
)


def unwrap_type(data_type: str) -> str:
    """The innermost ClickHouse type under any number of ``Nullable`` and
    ``LowCardinality`` wrappers, in either nesting order.

    ClickHouse encodes nullability in the type rather than in a column flag, so
    ``system.columns`` has no ``is_nullable`` and every type predicate in
    ``adapters.base`` would otherwise be reading a constructor name. Getting
    this wrong is quiet in exactly the way that matters: a
    ``LowCardinality(Nullable(String))`` that is not unwrapped still contains
    the substring ``STRING`` and still profiles, so nothing fails, while
    ``Nullable(Date)`` looks temporal for the wrong reason and a degraded
    ``Nullable(Array(String))`` is never recognized as degraded at all.
    """

    current = data_type.strip()
    while True:
        match = _WRAPPER_RE.match(current)
        if match is None:
            return current
        current = match.group(1).strip()


def is_nullable_type(data_type: str) -> bool:
    """Whether a ClickHouse type admits NULL, at any wrapper depth."""

    return "nullable(" in data_type.lower()


def _regexp_predicate(qcol: str, pattern: str) -> str:
    # match() searches; the shared patterns' own anchors make it a full match.
    # ClickHouse regexes are RE2, which is the strictest flavor in the set, and
    # the shared patterns are POSIX-class-only with no lookaround for that
    # reason. toString() lets the same predicate apply to a LowCardinality or
    # Enum column without a separate case.
    return f"match(toString({qcol}), '{pattern}')"


def _date_trunc_expr(qcol: str, unit: str) -> str:
    return f"dateTrunc('{unit}', toDateTime({qcol}))"


def _date_diff_expr(unit: str, later: str, earlier: str) -> str:
    # ClickHouse takes the *earlier* operand first, the opposite of the
    # callback's own argument order; swapping here rather than at the call site
    # keeps the shared builder dialect-free. Getting it backwards yields
    # negative gaps that MAX then discards, i.e. a clean report.
    return f"dateDiff('{unit}', {earlier}, {later})"


def _lag_expr(value: str, order_by: str) -> str:
    """ClickHouse's previous-row idiom, with both corrections it needs.

    There is no ``LAG``. ``lagInFrame`` is the substitute and it differs twice:
    it returns the *type default* rather than NULL past the frame edge, so the
    first row would compare against the epoch and report a ~20,000 day gap on
    every column; and it respects the window frame, so it needs the explicit
    full frame to behave like standard ``LAG``. ``toNullable`` is what lets the
    NULL default be accepted at all, since the default's type must match the
    argument's.
    """

    return (
        f"lagInFrame(toNullable({value}), 1, NULL) OVER ("
        f"ORDER BY {order_by} ROWS BETWEEN UNBOUNDED PRECEDING "
        "AND UNBOUNDED FOLLOWING)"
    )


class ClickHouseConnectionError(ConnectorError):
    """Raised when a queried object cannot be resolved in the configured
    scope, or the endpoint is not one this connector can guard. The message
    always names the fix, never a credential."""


class ClickHouseAdapter:
    """Holds one ClickHouse client plus the cost gate for one command.

    ``connection`` is injectable (class DI) so unit tests drive a fake; the real
    client is built by ``connect.py`` from discovered parameters. Credentials
    live only inside this process and are never surfaced.

    ``owns_connection`` is False when the client came from outside dex (a host
    supplying its own principal). Then :meth:`close` leaves it open, because
    closing a handle the caller still holds would break the caller.
    """

    name = "clickhouse"
    dialect = DIALECT
    paradigm = Paradigm.DB_LOAD

    def __init__(
        self,
        *,
        connection: Any,
        cost_gate: CostGate,
        target: ClickHouseTarget | None = None,
        auth_method: str = "unknown",
        scope_origin: str | None = None,
        owns_connection: bool = True,
    ):
        self._client = connection
        self._owns_connection = owns_connection
        self.cost_gate = cost_gate
        self.target = target or ClickHouseTarget()
        self.auth_method = auth_method
        # What the scope entries in the target came from, so a refusal names the
        # thing the user has to go edit: a per-command flag or the committed
        # allowlist. `narrow_target` has already collapsed the two by the time
        # the adapter sees them, and the fix differs entirely.
        self._scope_origin = scope_origin or "clickhouse.databases in .dex/config.yml"
        # Imported lazily (the caller constructed the client, so the library is
        # present); the error type drives refusal translation.
        from clickhouse_connect.driver import exceptions as ch_exceptions

        self._ch_exceptions = ch_exceptions
        # Catalog results are cached per command: the estimate pass and the
        # confirmed run share table facts, and each lookup is free but a
        # round-trip.
        self._objects: dict[str, dict] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._exact_rows: dict[str, int] = {}
        self._inventory_loaded = False
        self._resolved_databases: list[str] | None = None
        self._visible_databases: set[str] | None = None
        self._server_version: str | None = None
        self._notes: dict[str, list[str]] = {}
        self._session_asserted = False

    # --- capabilities (free) ---------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # The probe round-trips to the server (current settings, not cached
        # facts), so a stale or underprivileged credential fails here instead of
        # reporting a healthy connection. It is also where a Cloud endpoint is
        # refused, before anything is estimated or spent.
        self._assert_deployment()
        probe = self._catalog(
            "SELECT version() AS server_version, currentDatabase() AS db, "
            "(SELECT value FROM system.settings WHERE name = 'readonly') AS readonly"
        )[0]
        cost = self.cost_gate.cost()
        return {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            # Reported, not assumed: the session settings are sent on every
            # statement, and this is what the server says it actually has.
            "session_read_only": str(probe["readonly"]) in ("1", "2"),
            "paradigm": self.paradigm.value,
            "auth_method": self.auth_method,
            "deployment": self.target.deployment,
            "database": str(probe["db"]),
            "server_version": str(probe["server_version"]),
            "database_count": len(self._database_scope()),
            "required_grants": [
                "SELECT on source databases",
                "SELECT on system.tables, system.columns and system.databases",
                "write only on the dedicated dbt dev database (transform build)",
            ],
            "budget": {
                "ceiling_seconds": cost.ceiling,
                "session_spent_today_seconds": self.cost_gate.session_spent_now(),
            },
        }

    def _assert_deployment(self) -> None:
        """Refuse an endpoint whose spend this connector cannot bound.

        The deployment is a committed declaration, never a sniff; the server
        check exists only to catch a declaration that does not match reality,
        which is the case where guarding in the wrong unit would be silent.
        """

        if self.target.deployment == "cloud":
            raise ClickHouseConnectionError(_CLOUD_REFUSAL)
        rows = self._catalog(
            "SELECT value FROM system.settings WHERE name = 'cloud_mode'"
        )
        looks_cloud = bool(rows) and str(rows[0]["value"]) == "1"
        if looks_cloud:
            raise ClickHouseConnectionError(_CLOUD_CORROBORATION_REFUSAL)

    # --- introspection (free system-table metadata; no scans) ------------------

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
            raise ClickHouseConnectionError(
                f"object '{identifier}' not found in the configured scope; "
                "check clickhouse.databases in .dex/config.yml (identifiers are "
                "database.table, two parts: ClickHouse has no catalog level)"
            )
        return self._object_meta(entry), list(self._columns.get(identifier, []))

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the run accumulated for one object (engine
        semantics, sampling degradation, skipped escalations). Merged into the
        dataset's ``data_quality`` by the profile engine."""

        self._load_inventory()
        return list(self._notes.get(identifier, []))

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        # The resolved scopes, not the raw config: an entry that names nothing
        # is refused before this filter drops it, so a typo can never present as
        # an empty warehouse.
        allowed = set(self._database_scope()) if self.target.databases else set()
        # total_rows and total_bytes are free metadata and are NULL where the
        # engine cannot answer quickly (a view, a Distributed table), which is
        # exactly what ObjectMeta's optional row_count and byte_size mean.
        rows = self._catalog(
            "SELECT database, name, engine, total_rows, total_bytes, "
            "sampling_key, primary_key FROM system.tables "
            "WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', "
            "'information_schema') AND NOT is_temporary"
        )
        for row in rows:
            database = str(row["database"])
            if allowed and database not in allowed:
                continue
            engine = str(row["engine"])
            object_type = "view" if engine.endswith("View") else "table"
            identifier = f"{database}.{row['name']}"
            self._objects[identifier] = {
                "identifier": identifier,
                "object_type": object_type,
                "schema": database,
                "name": str(row["name"]),
                "row_count": _optional_int(row["total_rows"]),
                "byte_size": _optional_int(row["total_bytes"]),
                "column_count": 0,
                "engine": engine,
                "sampling_key": str(row["sampling_key"] or ""),
                "primary_key": str(row["primary_key"] or ""),
            }
            if engine in _COLLAPSING_ENGINES:
                self._note(
                    identifier,
                    f"engine is {engine}: rows sharing the sorting key are kept "
                    "until a background merge collapses them, so a duplicate "
                    "count here describes the stored parts rather than the "
                    "modeled grain. Query with FINAL to see the collapsed view",
                )
        columns = self._catalog(
            "SELECT database, table, name, type, position, is_in_primary_key "
            "FROM system.columns "
            "WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', "
            "'information_schema') ORDER BY database, table, position"
        )
        for row in columns:
            identifier = f"{row['database']}.{row['table']}"
            entry = self._objects.get(identifier)
            if entry is None:
                continue
            metas = self._columns.setdefault(identifier, [])
            data_type = str(row["type"])
            metas.append(
                ColumnMeta(
                    name=str(row["name"]),
                    data_type=data_type,
                    # ClickHouse has no is_nullable column: nullability is the
                    # type constructor, so it is read from the type itself.
                    nullable=is_nullable_type(data_type),
                    ordinal=len(metas),
                )
            )
            entry["column_count"] = len(metas)
        self._inventory_loaded = True

    def _object_meta(self, entry: dict) -> ObjectMeta:
        # An exact count from a profiling scan supersedes the metadata figure
        # for the rest of the command, so uniqueness proofs and drift verdicts
        # compare against real rows.
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

    def _database_scope(self) -> list[str]:
        """Every source database this command reads, proven to exist.

        Resolution is free (one system-table SELECT, no scan) and cached for the
        command. It runs before anything is estimated, because a scope that
        resolves to nothing and silently falls back to the whole allowlist is a
        cost-safety bug: the estimate the user confirms would cover tables they
        never named.
        """

        if self._resolved_databases is None:
            self._resolved_databases = self._resolve_databases()
        return self._resolved_databases

    def _resolve_databases(self) -> list[str]:
        visible = self._databases()
        if not self.target.databases:
            return sorted(visible)
        with blame(self._scope_origin, ClickHouseConnectionError):
            return sorted(
                {self._resolve_database(e, visible) for e in self.target.databases}
            )

    def _resolve_database(self, entry: str, visible: set[str]) -> str:
        """One scope entry, proven to exist. A ClickHouse scope is always a bare
        database: there is no catalog above it to qualify with, and a dotted
        entry is a table, which is a narrower thing than a scope names."""

        token = entry.strip()
        if not token:
            raise ClickHouseConnectionError("empty scope entry")
        if "." in token:
            raise ClickHouseConnectionError(
                f"scope '{entry}' has too many parts; a ClickHouse source scope "
                "is a bare <database>, never a table (identifiers are two-part "
                "here: there is no catalog level above the database)"
            )
        if token not in visible:
            raise ClickHouseConnectionError(
                f"scope '{entry}' names no database on this server; databases "
                f"there: {name_list(sorted(visible))}"
            )
        return token

    def _databases(self) -> set[str]:
        """Every non-system database. Free (one system-table SELECT, no scan),
        cached, and the live credential probe."""

        if self._visible_databases is None:
            rows = self._catalog(
                "SELECT name FROM system.databases WHERE name NOT IN "
                "('system', 'INFORMATION_SCHEMA', 'information_schema')"
            )
            self._visible_databases = {str(row["name"]) for row in rows}
        return self._visible_databases

    def missing_dev_namespaces(self, database: str, *, role: str) -> list[str]:
        """What stops ``role`` from building into the dev database. Free:
        system-table lookups, no scan.

        dbt-clickhouse issues ``CREATE DATABASE IF NOT EXISTS`` in its own
        ``create_schema`` macro, so the database's absence is not fatal on its
        own: what is fatal is the privilege to create it. ClickHouse therefore
        lands with Postgres, where the *privilege* is the question, rather than
        with Snowflake and Databricks, where the missing object is.

        ``role`` is the user in the rendered dbt profile rather than the
        connected user, because dex may legitimately read as a read-only user
        while dbt writes as another. Unlike Postgres, ClickHouse will only show
        another user's grants to a caller holding SHOW ACCESS, so when the
        grants cannot be read this degrades to a note rather than inventing a
        verdict: a preflight that guesses is worse than one that abstains.
        """

        grants, complete = self._grants_for(role)
        if grants is not None:
            if database in self._databases():
                needed = {"INSERT", "CREATE TABLE", "SELECT"}
            else:
                needed = {"CREATE DATABASE", "INSERT", "CREATE TABLE", "SELECT"}
            missing = sorted(n for n in needed if not _granted(grants, n, database))
            if not missing:
                return []
            if complete:
                return [f'{", ".join(missing)} on dev_database "{database}"']
            # Privileges are missing from what dex could read, but role
            # membership could not be expanded, and a privilege held through a
            # role would look exactly like this. Refusing here would block a
            # build that works, so the partial read is only ever allowed to
            # clear a target, never to refuse one.
            self._note(
                f"{database}.*",
                f"dev-target privileges for {role} could only be read in part "
                f"({', '.join(missing)} not found among its direct grants, and "
                "role membership is not visible to the user dex connects as), "
                "so the build was not refused on grants; a missing privilege "
                "will surface at dbt run instead",
            )
            return []
        self._note(
            f"{database}.*",
            f"dev-target privileges for {role} could not be read (system.grants "
            "is not readable by the user dex connects as), so the build was not "
            "preflighted for grants; a missing CREATE DATABASE or INSERT will "
            "surface at dbt run instead",
        )
        return []

    def list_namespace_objects(self, database: str) -> list[str]:
        """Table and view names already in one database. Free: one system-table
        SELECT, no scan. A database that does not exist yields no rows, i.e.
        nothing to collide with. No role parameter: content is role-independent,
        unlike the privilege question ``missing_dev_namespaces`` asks."""

        rows = self._catalog(
            "SELECT name FROM system.tables "  # noqa: S608
            f"WHERE database = '{_escape_literal(database)}'"
        )
        return sorted(str(row["name"]) for row in rows)

    def _grants_for(self, role: str) -> tuple[list[dict] | None, bool]:
        """``(grants, complete)`` for ``role``: what dex could read, and whether
        that is the whole picture.

        Three states, not two, because they lead to three different preflight
        answers. ``(None, False)`` means the grant table itself is unreadable,
        so nothing can be said. ``(grants, False)`` means direct grants were
        read but role membership could not be expanded, so the list may
        under-report: it can clear a target and must never refuse one.
        ``(grants, True)`` is the whole picture and can do both. Collapsing the
        middle state into either neighbour is how a preflight starts either
        blocking working builds or waving through broken ones.
        """

        who = _escape_literal(role)
        try:
            direct = self._catalog(
                "SELECT access_type, database FROM system.grants "  # noqa: S608
                f"WHERE user_name = '{who}'"
            )
        except Exception:
            return None, False
        try:
            via_roles = self._catalog(
                "SELECT access_type, database FROM system.grants "  # noqa: S608
                "WHERE role_name IN (SELECT granted_role_name FROM "
                f"system.role_grants WHERE user_name = '{who}')"
            )
        except Exception:
            return direct, False
        return direct + via_roles, True

    # --- estimation (free; feeds the confirm handshake) -------------------------

    def profile_estimate(
        self, identifiers: list[str], *, include_blobs: set[str] | None = None
    ) -> tuple[float, dict[str, float]]:
        """The heuristic database-seconds estimate for profiling: per table, its
        bytes over a conservative scan rate times the number of aggregate
        batches. Free: everything comes from system-table metadata.

        Blob-type columns are excluded from the batch count the same way
        ``explore.profile.profile`` excludes them from the scan itself
        (``include_blobs`` names the ``identifier.column`` paths a human opted
        back in), so this estimate's batch count matches what the run will
        actually issue.
        """

        blob_paths = include_blobs or set()
        per_table: dict[str, float] = {}
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            scan_columns = [
                c
                for c in columns
                if not is_blob_type(unwrap_type(c.data_type))
                or f"{identifier}.{c.name}".lower() in blob_paths
            ]
            batches = max((len(scan_columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = batches * self._scan_seconds(meta.byte_size)
        return sum(per_table.values()), per_table

    def query_estimate(self, sql: str) -> float:
        """The estimate for one firewall-approved query, from the free
        non-executing EXPLAIN ESTIMATE (which reports rows after primary-key and
        partition pruning, so a filtered probe on a huge table is not quoted as a
        full scan), falling back to summed referenced-table bytes when the
        statement's relations are outside the MergeTree family."""

        checked = assert_select_only(sql, dialect=self.dialect)
        seconds, _basis = self._statement_estimate(checked)
        return seconds

    def _statement_estimate(self, sql: str) -> tuple[float, str]:
        """Seconds plus which source priced them, so the handshake can say."""

        estimated = self._explain_estimate(sql)
        if estimated is not None:
            return max(
                estimated / _SCAN_BYTES_PER_SECOND, _MIN_STATEMENT_SECONDS
            ), "explain_estimate"
        total_bytes = 0
        known = 0
        self._load_inventory()
        for identifier in self._referenced_tables(sql):
            entry = self._objects.get(identifier)
            if entry and entry["byte_size"] is not None:
                total_bytes += entry["byte_size"]
                known += 1
        if known == 0:
            return _MIN_STATEMENT_SECONDS, "floor"
        return self._scan_seconds(total_bytes), "system_tables"

    def _explain_estimate(self, sql: str) -> float | None:
        """Bytes one statement would read, from the free EXPLAIN ESTIMATE door,
        or ``None`` when the plan cannot be produced or read.

        EXPLAIN ESTIMATE reports rows and marks, not bytes, so each table's rows
        are widened by its own average row size from ``system.tables``. It
        covers the MergeTree family only; anything else returns no rows and the
        caller falls back to whole-relation sizes.
        """

        try:
            rows = self._explain(sql)
        except Exception:
            return None
        if not rows:
            return None
        self._load_inventory()
        total = 0.0
        for row in rows:
            identifier = f"{row['database']}.{row['table']}"
            entry = self._objects.get(identifier)
            estimated_rows = float(row["rows"] or 0)
            bytes_per_row = _FALLBACK_BYTES_PER_ROW
            if entry and entry["byte_size"] and entry["row_count"]:
                bytes_per_row = max(entry["byte_size"] / entry["row_count"], 1.0)
            total += estimated_rows * bytes_per_row
        return total

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
            # A bare table name resolves against the connected database, which
            # completes a one-part reference to the two parts dex keys on.
            if len(parts) == 1:
                parts.insert(0, database)
            identifiers.add(".".join(parts))
        return identifiers

    def _current_database(self) -> str:
        return self.target.database or "default"

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The db-load handshake payload: database-seconds are the binding
        number; there is no currency translation because nothing is billed.

        ``estimate_quality`` stays "heuristic" even though EXPLAIN ESTIMATE's
        row count is exact, because the number being confirmed is seconds and
        seconds come from a throughput constant. What is exact rides one field
        down, in ``estimate_basis``, so a caller can tell a pruned plan estimate
        from a whole-relation fallback without dex overclaiming the quote.
        """

        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in database-seconds; the same number "
                "becomes the server-side max_execution_time, and the bytes it "
                "implies become max_bytes_to_read)"
            ),
            "notes": [_ESTIMATE_QUALITY_NOTE],
        }
        if per_table:
            data["per_table_seconds"] = per_table
        return data

    def spend_display(self) -> dict:
        """No currency translation exists for database load; the seconds in the
        spend summary are the whole story. Rows and bytes actually read are
        reported as table notes rather than here, so nothing in the spend
        summary carries a magnitude in a unit the ceiling is not in."""

        return {}

    # --- profiling (billed; every statement estimated and gated) ----------------

    def column_aggregates(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        *,
        safe_min_max: set[str] | None = None,
        shape_stats: set[str] | None = None,
        type_stats: set[str] | None = None,
        key_shape_stats: set[str] | None = None,
        temporal_stats: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        safe = safe_min_max or set()
        shape = shape_stats or set()
        type_req = type_stats or set()
        key_shape_req = key_shape_stats or set()
        temporal_req = temporal_stats or set()
        meta, _ = self.table_metadata(identifier)
        sample_ratio = self._sample_ratio(identifier, meta.byte_size)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(
                identifier,
                batch,
                safe,
                shape,
                type_req,
                key_shape_req,
                temporal_req,
                sample_ratio=sample_ratio,
            )
            rows, labels = self._execute(
                sql, estimate=self._scan_seconds(meta.byte_size)
            )
            values = dict(zip(labels, rows[0], strict=True))
            n_total = int(values["n_total"])
            if sample_ratio is None:
                self._exact_rows[identifier] = n_total
            results.extend(
                self._read_aggregates(values, plan, sampled=sample_ratio is not None)
            )
        return results

    def _sample_ratio(self, identifier: str, byte_size: int | None) -> float | None:
        """The SAMPLE ratio for one table, or None for a full scan.

        ClickHouse's ``SAMPLE`` clause only works on a table that declared a
        sampling expression in its MergeTree key, which most tables do not. So
        the threshold is honored where it can be and *refused out loud* where it
        cannot, rather than silently producing a full scan the user believed was
        sampled, or an ``ORDER BY rand()`` that reads everything and then sorts
        it, which would cost more than the scan it replaced.
        """

        threshold = self.target.max_full_profile_bytes
        if threshold is None or not byte_size or byte_size <= threshold:
            return None
        self._load_inventory()
        entry = self._objects.get(identifier, {})
        if not entry.get("sampling_key"):
            self._note(
                identifier,
                "clickhouse.max_full_profile_bytes cannot be honored on this "
                "table: it declares no sampling key, and ClickHouse can only "
                "sample where one exists. The table was profiled in full "
                "within the confirmed budget",
            )
            return None
        ratio = min(max(threshold / byte_size, 0.0001), 1.0)
        self._note(
            identifier,
            f"profiled from a ~{round(ratio * 100, 2)}% SAMPLE (table exceeds "
            "clickhouse.max_full_profile_bytes); counts and extremes are "
            "approximate and uniqueness is not judged",
        )
        return ratio

    def _build_aggregate_sql(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        shape: set[str],
        type_req: set[str],
        key_shape_req: set[str],
        temporal_req: set[str],
        *,
        sample_ratio: float | None = None,
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]]]:
        # One aggregate statement per batch, a single pass: COUNT(*) once, then
        # per column a non-null count, an approximate distinct, min/max only
        # where allowed, and value-shape/type-contradiction/key-shape/temporal
        # fractions only where requested. Unlike Postgres this does compute
        # distinct counts in the scan, because ClickHouse's `uniq` is a
        # HyperLogLog that rides the same pass rather than a sort. Pure (no
        # connection), so the SELECT-only property is testable offline.
        source = self._quote(identifier)
        if sample_ratio is not None:
            source += f" SAMPLE {sample_ratio}"
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            inner = unwrap_type(col.data_type)
            degraded = self._is_degraded(inner)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not degraded
            if wants_distinct:
                select_parts.append(f"uniq({qcol}) AS d_{i}")
            wants_min_max = (col.name in safe) and not degraded
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            wants_shape = (col.name in shape) and not degraded
            if wants_shape:
                select_parts.extend(shape_stat_expressions(qcol, i, _regexp_predicate))
            wants_type = (col.name in type_req) and not degraded
            if wants_type:
                select_parts.extend(
                    type_contradiction_expressions(
                        qcol,
                        i,
                        is_string=is_string_type(inner),
                        is_integer=is_integer_type(inner),
                        regexp_predicate=_regexp_predicate,
                        bigint_type=_BIGINT_TYPE,
                    )
                )
            wants_key_shape = (col.name in key_shape_req) and not degraded
            if wants_key_shape:
                select_parts.extend(key_shape_expressions(qcol, i, _regexp_predicate))
            wants_temporal = (col.name in temporal_req) and not degraded
            if wants_temporal:
                select_parts.extend(
                    temporal_alignment_expressions(qcol, i, _date_trunc_expr)
                )
                for unit in temporal_units_for(inner):
                    select_parts.extend(
                        temporal_continuity_sql(
                            qcol,
                            i,
                            unit,
                            source,
                            _date_trunc_expr,
                            _date_diff_expr,
                            _lag_expr,
                        )
                    )
            plan.append(
                (
                    i,
                    col,
                    wants_distinct,
                    wants_min_max,
                    wants_shape,
                    wants_type,
                    wants_key_shape,
                    wants_temporal,
                )
            )
        # Interpolated parts are quoted identifiers and fixed aggregate
        # keywords, never values; the result is guarded as a read-only SELECT.
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"  # noqa: S608
        return assert_select_only(sql, dialect=self.dialect), plan

    @staticmethod
    def _is_degraded(inner_type: str) -> bool:
        return inner_type.lower().startswith(_DEGRADED_TYPE_PREFIXES)

    @staticmethod
    def _read_aggregates(
        values: dict,
        plan: list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]],
        *,
        sampled: bool,
    ) -> list[ColumnAggregate]:
        n_total = int(values["n_total"])
        aggregates: list[ColumnAggregate] = []
        for (
            i,
            col,
            wants_distinct,
            wants_min_max,
            wants_shape,
            wants_type,
            wants_key_shape,
            wants_temporal,
        ) in plan:
            nn = values.get(f"nn_{i}")
            null_fraction = (
                (1 - int(nn) / n_total) if nn is not None and n_total > 0 else None
            )
            distinct: int | None = None
            if wants_distinct and values.get(f"d_{i}") is not None:
                distinct = int(values[f"d_{i}"])
            # uniq() is a HyperLogLog sketch, so it is never a uniqueness
            # verdict on its own; the near-unique escalation proves keys with
            # an exact scan. A sampled scan cannot judge uniqueness at all.
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=None,
                    distinct_count_exact=False,
                    min_value=(
                        json_safe(values.get(f"mn_{i}"))
                        if wants_min_max and not sampled
                        else None
                    ),
                    max_value=(
                        json_safe(values.get(f"mx_{i}"))
                        if wants_min_max and not sampled
                        else None
                    ),
                    upper_vocab_fraction=shape_stat_value(
                        values, f"su_{i}", wants_shape
                    ),
                    person_shape_fraction=shape_stat_value(
                        values, f"sp_{i}", wants_shape
                    ),
                    avg_token_count=shape_stat_value(values, f"st_{i}", wants_shape),
                    **type_contradiction_aggregate_kwargs(values, i, wants_type),
                    **key_shape_aggregate_kwargs(values, i, wants_key_shape),
                    **temporal_continuity_aggregate_kwargs(values, i, wants_temporal),
                )
            )
        return aggregates

    def exact_distinct_counts(
        self, identifier: str, columns: list[str]
    ) -> dict[str, int]:
        """Exact uniqExact() for near-unique columns, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scan, return nothing and let uniqueness verdicts stay approximate.
        A metered adapter never self-escalates past its ceiling."""

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
        # COUNT(*) rides along so the same scan also upgrades the metadata row
        # figure to an exact count (grain verdicts compare against it).
        select_parts = ["COUNT(*) AS n_total"] + [
            f"uniqExact({_quote_ident(name)}) AS d_{i}"
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

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ) -> dict[tuple[str, ...], int]:
        """Exact distinct count per column combination, spent only within the
        already-confirmed budget. Each combination is a further scan, so when
        the budget cannot cover all of them the probe narrows to the pairs it
        can afford (they arrive best-ranked first) and says so, rather than
        giving up the grain wholesale. A metered adapter never self-escalates
        past its ceiling.
        """

        if not combinations:
            return {}
        meta, _ = self.table_metadata(identifier)
        unit = self._scan_seconds(meta.byte_size)
        probed, note = affordable_combinations(
            combinations,
            lambda prefix: unit * len(prefix),
            self.cost_gate.try_charge,
        )
        if note:
            self._note(identifier, note)
        if not probed:
            return {}
        sql = assert_select_only(
            distinct_combination_sql(self._quote(identifier), probed, _quote_ident),
            dialect=self.dialect,
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        return {tuple(combo): int(values[f"d_{i}"]) for i, combo in enumerate(probed)}

    def value_domain_counts(
        self, identifier: str, columns: list[str], *, limit: int
    ) -> dict[str, ValueDomainSample]:
        """Top ``limit`` values by frequency per column, plus each column's exact
        distinct-group count, spent only within the already-confirmed budget."""

        if not columns:
            return {}
        meta, _ = self.table_metadata(identifier)
        estimate = self._scan_seconds(meta.byte_size)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "value-domain probe skipped: the remaining budget could not "
                "cover the extra scan; no value domain reported",
            )
            return {}
        table = self._quote(identifier)
        parts = []
        for i, name in enumerate(columns):
            qcol = _quote_ident(name)
            # topK would be cheaper but is approximate and does not carry
            # counts; the grouped subquery is exact, which is what the value
            # domain is for.
            parts.append(
                "(SELECT groupArray(tuple(toString(v), c)) FROM "  # noqa: S608
                f"(SELECT {qcol} AS v, COUNT(*) AS c FROM {table} "
                f"WHERE {qcol} IS NOT NULL GROUP BY {qcol} "
                f"ORDER BY c DESC, v ASC LIMIT {limit}) AS q_{i}) AS d_{i}"
            )
            parts.append(
                f"(SELECT uniqExact({qcol}) FROM {table} "  # noqa: S608
                f"WHERE {qcol} IS NOT NULL) AS n_{i}"
            )
        sql = assert_select_only(f"SELECT {', '.join(parts)}", dialect=self.dialect)
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        result = {}
        for i, name in enumerate(columns):
            domain = values[f"d_{i}"]
            if isinstance(domain, str):
                domain = json.loads(domain)
            result[name] = ValueDomainSample(
                values=[(entry[0], int(entry[1])) for entry in (domain or [])],
                total_distinct=int(values[f"n_{i}"]),
            )
        return result

    # --- execution (the single billed door) --------------------------------------

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT, bounded in rows, wall time, and
        database-seconds (client preflight plus the server-side caps, whichever
        is tighter)."""

        checked = assert_select_only(sql, dialect=self.dialect)
        seconds, _basis = self._statement_estimate(checked)
        self.cost_gate.charge(seconds)
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
        """SELECT-only guard, heuristic charge, then the capped run."""

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
        """The single billed door past the gate: the server-side caps are wound
        down to what remains of the budget (or the wall-clock limit when that is
        tighter), and server-reported elapsed time is recorded to the ledger."""

        settings, budget_bound = self._billed_settings(timeout_seconds)
        try:
            result = self._client.query(sql, settings=settings)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == _TIMEOUT_CODE:
                raise self._timeout_refusal(budget_bound, timeout_seconds) from exc
            if code in _LIMIT_CODES:
                raise self._limit_refusal(budget_bound) from exc
            raise self._translate(exc) from exc
        self._record_summary(result, sql)
        labels = list(result.column_names)
        types = [getattr(t, "name", str(t)) for t in result.column_types]
        rows = list(result.result_rows)
        return rows, labels, types

    def _billed_settings(
        self, timeout_seconds: float | None
    ) -> tuple[dict[str, Any], bool]:
        """The per-statement settings that make the confirmed budget binding.

        Time alone is not enough: ``max_execution_time`` is checked at block
        boundaries, so a single fast block can overshoot it. ``max_bytes_to_read``
        is derived by inverting the same throughput constant the estimate used,
        which makes the cap and the quote two views of one number, and it is the
        one that actually binds on a scan. Both overflow modes are named
        explicitly so a server default of ``break`` could never turn a cap into a
        silently truncated result, which would be a wrong answer rather than a
        refusal.
        """

        remaining = self.cost_gate.remaining_for_statement()
        if remaining is not None and remaining < 1:
            raise OverCeilingError(
                "the remaining budget is under one database-second; raise "
                "--budget or narrow the work"
            )
        settings: dict[str, Any] = dict(_SESSION_SETTINGS)
        seconds: int | None = None
        budget_bound = False
        if remaining is not None:
            # int() truncates, and max_execution_time = 0 means *no limit* in
            # ClickHouse, so a sub-second remainder must never reach the server
            # as a cap; the guard above is what makes that unreachable.
            seconds = int(remaining)
            budget_bound = True
            settings["max_bytes_to_read"] = int(remaining * _SCAN_BYTES_PER_SECOND)
            settings["read_overflow_mode"] = "throw"
        if timeout_seconds is not None:
            wall = int(max(timeout_seconds, 1))
            if seconds is None or wall < seconds:
                seconds = wall
                budget_bound = False
        if seconds is not None:
            settings["max_execution_time"] = seconds
            settings["timeout_overflow_mode"] = "throw"
        # Deliberately no max_result_rows. It looks like the obvious third
        # backstop and it is not usable as one: with result_overflow_mode
        # 'throw' the server refuses the one-extra-row fetch that detects
        # truncation, turning an ordinary capped query into an error, and with
        # 'break' it is applied at block granularity and did not bind at all
        # when measured against a live server. A setting that either breaks a
        # legitimate query or silently does nothing is worse than no setting.
        # The result is already bounded twice over: the query firewall clamps
        # LIMIT into the statement, and run_query slices client-side.
        return settings, budget_bound

    def _record_summary(self, result: Any, sql: str) -> None:
        """Settle one statement from the response's own summary header.

        Free and server-side: no second query and no system.query_log poll,
        which also means the recorded seconds are what the server spent rather
        than what the client waited. Summary values arrive as strings.
        """

        summary = dict(getattr(result, "summary", {}) or {})
        elapsed = _optional_int(summary.get("elapsed_ns"))
        seconds = (elapsed / 1_000_000_000) if elapsed else 0.0
        self.cost_gate.record_billed(seconds, job_id=None, statement=sql)

    def _timeout_refusal(
        self, budget_bound: bool, timeout_seconds: float | None
    ) -> Exception:
        if budget_bound:
            return OverCeilingError(
                "the statement hit the server-side max_execution_time derived "
                "from the remaining budget; raise --budget or narrow the work"
            )
        limit = f"{timeout_seconds:g}s" if timeout_seconds is not None else "its limit"
        return TimeoutError(
            f"query exceeded {limit} and was cancelled; narrow it (tighter "
            "filter, fewer columns) and retry"
        )

    def _limit_refusal(self, budget_bound: bool) -> Exception:
        if budget_bound:
            return OverCeilingError(
                "the statement hit the server-side max_bytes_to_read derived "
                "from the remaining budget; raise --budget or narrow the work"
            )
        return ClickHouseConnectionError(
            "the statement exceeded its row or result limit; narrow it "
            "(tighter filter, fewer columns, a smaller LIMIT) and retry"
        )

    def _translate(self, exc: Exception) -> Exception:
        code = getattr(exc, "code", None)
        if code == _UNKNOWN_TABLE_CODE:
            return ClickHouseConnectionError(
                "the statement names a table that does not exist on this "
                "server, or that the user dex connects as cannot see; check "
                "clickhouse.databases in .dex/config.yml"
            )
        if code == _ACCESS_DENIED_CODE:
            return ClickHouseConnectionError(
                "the server refused the statement for lack of a grant. dex "
                "reads with SELECT only; check that the user holds SELECT on "
                "the source databases and on system.tables and system.columns"
            )
        if isinstance(exc, self._ch_exceptions.OperationalError):
            return ConnectorError(
                "the ClickHouse server could not be reached; the endpoint may "
                "be down or unreachable from here"
            )
        return exc

    # --- helpers ------------------------------------------------------------------

    def _catalog(self, sql: str) -> list[dict]:
        """Free metadata door: engine-built system-table SELECTs only, no scans.
        Results come back as dicts keyed by the column names."""

        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("only SELECT statements pass through the catalog door")
        try:
            result = self._client.query(sql, settings=dict(_SESSION_SETTINGS))
        except Exception as exc:
            raise self._translate(exc) from exc
        labels = list(result.column_names)
        return [dict(zip(labels, row, strict=True)) for row in result.result_rows]

    def _explain(self, sql: str) -> list[dict]:
        """Free planner door: the statement is guarded SELECT-only, then
        prefixed with EXPLAIN ESTIMATE here, so nothing but an estimate request
        can pass. Non-executing, and permitted under readonly = 2."""

        checked = assert_select_only(sql, dialect=self.dialect)
        result = self._client.query(
            f"EXPLAIN ESTIMATE {checked}", settings=dict(_SESSION_SETTINGS)
        )
        labels = list(result.column_names)
        return [dict(zip(labels, row, strict=True)) for row in result.result_rows]

    def _note(self, identifier: str, note: str) -> None:
        notes = self._notes.setdefault(identifier, [])
        if note not in notes:
            notes.append(note)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str]:
        """A ClickHouse identifier is two parts, ``database.table``.

        This is the one adapter whose split is not three, and deliberately so:
        ClickHouse has no catalog level, dbt-clickhouse's ``schema:`` *is* the
        ClickHouse database, and a synthesized third component would be a name
        that appears in the cache, in ``explore inventory`` and in every drift
        finding while being untypeable in ``clickhouse-client``. Shared code
        that builds SQL from an identifier (the relationship overlap probe, the
        cluster sampler, the grain stand-ins) is arity-agnostic, so nothing
        downstream needs the fiction.
        """

        parts = identifier.split(".")
        if len(parts) != 2:
            raise ValueError(f"expected database.table, got '{identifier}'")
        return parts[0], parts[1]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        # A host-supplied client stays open: dex closes only what it opened.
        if not self._owns_connection:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


# Sent with every statement, free and billed alike, so read-only and join
# semantics never depend on how the client happened to be constructed.
#
# join_use_nulls is not a preference. ClickHouse defaults it to 0, which fills
# an unmatched LEFT JOIN row with the column type's *default* (0, '') rather
# than NULL. The shared relationship overlap probe counts orphans with
# `IS NULL`, so with the default every inferred join reports as perfectly clean
# and maintain grain's join-fanout half never fires: a detector that looks
# active while doing nothing. Verified live against a table with known orphans.
_SESSION_SETTINGS: dict[str, Any] = {
    "readonly": 2,
    "allow_ddl": 0,
    "join_use_nulls": 1,
}


def _optional_int(value: Any) -> int | None:
    """Server metadata that is legitimately absent (a view's row count) or
    arrives as a string (every summary field) into an optional int."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _granted(grants: list[dict], access_type: str, database: str) -> bool:
    """Whether one grant list covers ``access_type`` on ``database``.

    A NULL database on a grant row means it is global, which covers every
    database; ClickHouse also implies the specific privileges from the broader
    ones, so CREATE covers CREATE TABLE and CREATE DATABASE.
    """

    implied = {
        "CREATE TABLE": ("CREATE TABLE", "CREATE"),
        "CREATE DATABASE": ("CREATE DATABASE", "CREATE"),
        "INSERT": ("INSERT",),
        "SELECT": ("SELECT",),
    }[access_type]
    for grant in grants:
        scope = grant.get("database")
        if scope not in (None, "", database):
            continue
        if str(grant.get("access_type", "")).upper() in implied:
            return True
    return False


def _quote_ident(name: str) -> str:
    """Quote one identifier component with double quotes (ClickHouse accepts
    both backticks and double quotes; double quotes match what SQLGlot renders
    for this dialect), doubling embedded quotes."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
