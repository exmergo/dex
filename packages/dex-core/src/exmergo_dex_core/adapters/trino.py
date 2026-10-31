"""The Trino adapter: self-hosted Trino, Starburst Enterprise and Trino on EMR.

Self-hosted Trino bills no dollars, but a scan is real load on a cluster that is
usually shared, and through a federated catalog it is load on the remote
database behind it too. The guarded quantity is therefore that load, in
cluster-seconds, exactly as Postgres and self-hosted ClickHouse guard theirs:
every billed statement is estimated, gated, capped server-side and settled.

- **Estimate.** ``EXPLAIN (TYPE IO, FORMAT JSON)`` is free (the coordinator plans,
  nothing executes) and reports each input table's estimated output size after
  column pruning and predicate pushdown. Bytes become seconds through a
  conservative throughput constant. A catalog with no statistics answers
  ``"NaN"``; such a table is priced at a fixed floor, named in a table note, and
  the handshake's ``estimate_basis`` says ``floor`` so nobody mistakes a guess
  for a plan. ``SHOW STATS`` is deliberately never used: its ``low_value`` and
  ``high_value`` columns are raw column values, PII included.
- **Cap.** Every billed statement runs under ``query_max_run_time`` wound down to
  what remains of the budget, plus ``query_max_scan_physical_bytes`` derived from
  the same throughput constant. Both are session properties, applied with
  ``SET SESSION`` through one door, so they hold on a host-supplied connection
  too. Run time rather than execution time because it includes queueing and
  planning, which is what the settled elapsed time measures; time is the cap that
  always binds, since the byte cap is checked on an interval and some catalogs
  (tpch) report no physical input at all.
- **Settlement.** The server's own ``elapsedTimeMillis`` once a query finishes,
  and the client wall clock when it does not (the client stops polling stats on
  an error, so the last figure it holds is stale). A partially fetched query is
  cancelled, so nothing keeps running unrecorded.

Identifiers are three-part, ``catalog.schema.table``, and dex never sets a
session catalog or schema. The query firewall resolves an agent's partial name
(``app.orders``) to a cached relation by suffix, then sends the agent's spelling;
with a session catalog Trino could resolve that spelling to a different table
than the one whose PII verdict was applied. Without one, Trino itself refuses a
partial name, and :func:`guard` refuses it first with the fix named.

Read-only is enforced in depth: :func:`guard` on the billed door and the explain
door (a single SELECT, fully qualified relations, no table functions, no hidden
``$`` columns or metadata tables, no time travel), an adapter that issues only
SELECT, EXPLAIN and ``SET``/``RESET SESSION``, and the access-control rule
``references/trino.md`` documents. Trino has no session-level read-only mode a
client can hold, so ``capabilities()`` says so rather than claiming one.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from ..budget import effective_session_ceiling
from ..config import TrinoTarget
from ..envelope import EstimateQuality, Paradigm
from ..errors import ConnectorError
from ..guards.cost_guard import CostGate, OverCeilingError
from ..guards.sql_guard import NotSelectOnlyError, RefusalReason, assert_select_only
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
    warehouse_refusal,
    with_dev_target,
)

PARADIGM = "db_load"
DIALECT = "trino"

_BIGINT_TYPE = "BIGINT"

# Trino types a literal 1.0 as decimal(2,1) and avg(decimal) keeps the scale, so
# the shared builders' fractions would come back rounded to one decimal place.
# Double literals make every fraction a double.
_FRACTION_LITERALS = ("1e0", "0e0")

# Columns are profiled in batches so one statement against a very wide table
# does not balloon.
_COLUMN_BATCH = 50

# The estimate heuristic: a deliberately conservative scan rate for one
# cluster's share of work. Trino on warm data is faster than this, which is the
# direction an estimate should err, and the byte cap is derived from the same
# number, so a generous rate would loosen the backstop as well as the quote.
_SCAN_BYTES_PER_SECOND = 200 * 1024 * 1024

# Every billed statement estimates at least this much.
_MIN_STATEMENT_SECONDS = 0.5

# What one table is priced at when its catalog reports no statistics (EXPLAIN
# answers NaN). A guess, labeled as one in the handshake: the run-time cap is
# what actually bounds such a table.
_UNSTATISTICAL_TABLE_SECONDS = 5.0

# The wall cap on the free doors (metadata reads and EXPLAIN). They run on the
# coordinator and touch no data, so this only guards against a hung catalog;
# set explicitly so a billed cap left in the session can never kill them.
_METADATA_SECONDS = 60

# Session properties this adapter owns. Each door sets the full set it wants
# and resets the rest, so no door inherits another's caps.
_CAP_PROPERTIES = ("query_max_run_time", "query_max_scan_physical_bytes")

# Server error names translated rather than surfaced raw.
_TIME_LIMIT = "EXCEEDED_TIME_LIMIT"
_SCAN_LIMIT = "EXCEEDED_SCAN_LIMIT"
_PERMISSION_DENIED = "PERMISSION_DENIED"
_NOT_FOUND = frozenset(
    {"TABLE_NOT_FOUND", "SCHEMA_NOT_FOUND", "CATALOG_NOT_FOUND", "MISSING_CATALOG_NAME"}
)

# Types where a distinct count, an extreme, or any regexp_like statistic is
# invalid or meaningless: only a non-null count is computed. The nested types
# matter most, because their spellings contain the substrings the shared type
# predicates look for (array(varchar) reads as a string, map(varchar, integer)
# as numeric) and a regexp_like over one fails the whole profiling batch with
# FUNCTION_NOT_FOUND. Matched as prefixes of the lower-cased declared type.
_DEGRADED_TYPE_PREFIXES = (
    "array(",
    "map(",
    "row(",
    "json",
    "varbinary",
    "hyperloglog",
    "p4hyperloglog",
    "khyperloglog",
    "setdigest",
    "qdigest",
    "tdigest",
    "geometry",
    "sphericalgeography",
    "bingtile",
    "kdbtree",
)

# Catalog kinds a dbt dev target can never write to: the dev-target preflight
# refuses them before dbt runs. blackhole and memory accept writes but lose
# them (immediately, or on restart), which is worth a warning, not a refusal.
_READ_ONLY_CONNECTORS = frozenset({"tpch", "tpcds", "jmx", "system", "faker"})
_UNDURABLE_CONNECTORS = {
    "blackhole": "discards every row written to it, so a dev build there checks "
    "nothing about the data",
    "memory": "keeps its tables in the cluster's memory, so a restart loses "
    "every dev build",
}

_ESTIMATE_QUALITY_NOTE = (
    "Self-hosted Trino bills no dollars; the guarded quantity is load on the "
    "cluster (and through a federated catalog, on the database behind it), "
    "expressed as cluster-seconds. Byte estimates come from the free, "
    "non-executing EXPLAIN (TYPE IO) after column pruning; seconds are those "
    "bytes over a conservative scan rate. A table whose catalog reports no "
    "statistics is priced at a fixed floor instead. The confirmed budget is "
    "enforced per statement by the server-side query_max_run_time and "
    "query_max_scan_physical_bytes session properties"
)


def _regexp_predicate(qcol: str, pattern: str) -> str:
    # regexp_like searches; the shared patterns' own anchors make it a full
    # match. A char(n) column is accepted as is, without its padding.
    return f"regexp_like({qcol}, '{pattern}')"


def _date_trunc_expr(qcol: str, unit: str) -> str:
    return f"date_trunc('{unit}', {qcol})"


def _date_diff_expr(unit: str, later: str, earlier: str) -> str:
    # date_diff(unit, a, b) is b - a, so the earlier operand goes first.
    return f"date_diff('{unit}', {earlier}, {later})"


def is_degraded_type(data_type: str) -> bool:
    """Whether a Trino column type gets only a non-null count when profiled."""

    return data_type.strip().lower().startswith(_DEGRADED_TYPE_PREFIXES)


# The table arguments that turn a read of a table into a read of its past; the
# same node shapes the query firewall refuses for agent SQL.
_HISTORICAL_READ_ARGS = ("version", "when", "changes")


def guard(sql: str) -> str:
    """Return ``sql`` unchanged if Trino may run it for dex; else raise.

    Everything :func:`~exmergo_dex_core.guards.sql_guard.assert_select_only`
    proves (one read-only SELECT, no write anywhere in the tree), plus what only
    Trino needs:

    - **Every relation is ``catalog.schema.table``.** dex never sets a session
      catalog or schema, so a partial name either fails or, on a connection a
      host configured with one, resolves somewhere dex never checked. A bare
      name is allowed only where the statement defines it as a CTE.
    - **No table functions.** ``TABLE(catalog.system.query(...))`` sends opaque
      SQL to a federated database, and the polymorphic table functions reshape
      relations the firewall never saw; neither is a relation dex resolved.
    - **No hidden columns or metadata tables.** ``"$path"`` and the Iceberg
      ``"t$files"`` family are not in any cache, and their contents (file
      paths, column bounds) are values.
    - **No time travel**, for the reason the firewall gives.
    - **No DESCRIBE.** The base guard admits it as introspection; this adapter
      never issues one, and the doors are where that is decided.

    ``EXPLAIN ANALYZE``, ``CALL``, ``EXECUTE``, ``SET``/``RESET SESSION``,
    ``USE`` and ``ANALYZE`` are all refused by the base guard already (sqlglot
    models them as commands or non-query statements); the safety spine pins
    each one so a parser upgrade cannot quietly admit it.
    """

    checked = assert_select_only(sql, dialect=DIALECT)
    root = sqlglot.parse_one(checked, read=DIALECT)
    if isinstance(root, exp.Describe):
        raise NotSelectOnlyError(
            "DESCRIBE is not issued on Trino; dex reads metadata from "
            "information_schema",
            reason=RefusalReason.NOT_A_QUERY,
        )
    ctes = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            called = table.this.sql(DIALECT)
            raise NotSelectOnlyError(
                f"table functions are not permitted on Trino: {called} is not a "
                "relation dex resolved. Query catalog.schema.table directly",
                reason=RefusalReason.UNAPPROVED_FUNCTION,
            )
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        if len(parts) == 1 and table.name.lower() in ctes:
            continue
        dotted = ".".join(parts)
        if len(parts) != 3:
            raise NotSelectOnlyError(
                f"relation {dotted} is not fully qualified. dex connects to Trino "
                "with no session catalog or schema, so every relation is written "
                "catalog.schema.table"
            )
        if "$" in table.name:
            raise NotSelectOnlyError(
                f"{dotted} is a connector metadata table, which is not a source "
                "dex profiled; query the table itself"
            )
        if any(table.args.get(key) for key in _HISTORICAL_READ_ARGS):
            raise NotSelectOnlyError(
                f"time travel is not permitted: {dotted} is read as of an earlier "
                "version or point in time. Query the current table"
            )
    for column in root.find_all(exp.Column):
        if column.name.startswith("$"):
            raise NotSelectOnlyError(
                f"hidden column {column.name} is not permitted: it is a connector "
                "value (a file path, a partition) rather than a profiled column"
            )
    return checked


class TrinoConnectionError(ConnectorError):
    """Raised when a queried object cannot be resolved in the configured scope,
    or the cluster refuses what dex asked of it. The message always names the
    fix, never a credential."""


class TrinoAdapter:
    """Holds one Trino DBAPI connection plus the cost gate for one command.

    ``connection`` is injectable (class DI) so unit tests drive a fake; the real
    connection is built by ``connect.py`` from discovered parameters, with no
    session catalog or schema. ``clock`` is injectable too: it measures what a
    statement that did not finish actually ran for. Credentials live only inside
    this process and are never surfaced.

    ``owns_connection`` is False when the connection came from outside dex (a
    host supplying its own principal). Then :meth:`close` leaves it open. The
    session properties dex sets still apply to it, because the caps are the
    point of the gate.
    """

    name = "trino"
    # EXPLAIN (TYPE IO) is a plan estimate from catalog statistics, a good model
    # of the scan where they exist and a floor where they do not.
    estimate_quality = EstimateQuality.APPROXIMATE
    dialect = DIALECT

    def __init__(
        self,
        *,
        connection: Any,
        cost_gate: CostGate,
        target: TrinoTarget | None = None,
        host: str | None = None,
        auth_method: str = "unknown",
        scope_origin: str | None = None,
        owns_connection: bool = True,
        connection_warnings: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.target = target or TrinoTarget()
        if self.target.deployment != "self_hosted":
            raise TrinoConnectionError(
                f"trino.deployment '{self.target.deployment}' is not supported by "
                "this connector yet; only self_hosted clusters can be guarded"
            )
        if cost_gate.paradigm is not Paradigm.DB_LOAD:
            raise ValueError(
                "Trino cost gate does not match trino.deployment: expected "
                f"db_load, got {cost_gate.paradigm.value}"
            )
        self._conn = connection
        self._owns_connection = owns_connection
        self.cost_gate = cost_gate
        self.paradigm = cost_gate.paradigm
        self.host = host or self.target.host
        self.auth_method = auth_method
        self._connection_warnings = list(connection_warnings or [])
        self._clock = clock
        # What the scope entries in the target came from, so a refusal names the
        # thing the user has to go edit: a per-command flag or the committed
        # allowlist.
        self._scope_origin = scope_origin or "trino.catalogs in .dex/config.yml"
        # Imported lazily (the caller constructed the connection, so the library
        # is present); the error types drive refusal translation.
        import trino.exceptions as trino_errors

        self._errors = trino_errors
        self._dev_target: str | None = None
        self.forget_metadata()
        self._notes: dict[str, list[str]] = {}
        # The session properties this adapter last set, or None when they are
        # unknown (a fresh or host-supplied connection, or a SET that failed),
        # in which case the next door sets every one of them explicitly.
        self._session_props: dict[str, str] | None = None
        self._floored: set[str] = set()
        self._unplanned = 0
        self._catalog_kinds_cache: dict[str, str] | None = None

    # --- capabilities (free) ---------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # The probe round-trips to the coordinator, so a stale or underprivileged
        # credential fails here instead of reporting a healthy connection.
        probe = self._catalog("SELECT version() AS server_version")[0]
        cost = self.cost_gate.cost()
        ceiling_in_force, ceiling_source = effective_session_ceiling(
            self.cost_gate.configured_session_ceiling
        )
        caps: dict[str, object] = {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            # Reported honestly: Trino has no read-only session mode a client can
            # hold. Read-only rests on the guard, on an adapter that issues no
            # writes, and on the cluster's access control for the dex identity.
            "session_read_only": False,
            "paradigm": self.paradigm.value,
            "auth_method": self.auth_method,
            "deployment": self.target.deployment,
            "host": self.host,
            "server_version": str(probe["server_version"]),
            "catalog_count": len({catalog for catalog, _ in self._scopes()}),
            "required_grants": [
                "read-only access to the source catalogs in the cluster's access "
                "control (file-based rules, Ranger, or OPA)",
                "write access only to the dbt dev schema (transform build)",
            ],
        }
        if self._connection_warnings:
            caps["warnings"] = list(self._connection_warnings)
        caps["budget"] = {
            "ceiling_seconds": cost.ceiling,
            "session_spent_today_seconds": self.cost_gate.session_spent_now(),
            "session_ceiling_in_force": ceiling_in_force,
            "session_ceiling_source": ceiling_source,
        }
        return caps

    # --- introspection (free information_schema reads; no scans) ---------------

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
            raise TrinoConnectionError(
                f"object '{identifier}' not found in the configured scope; check "
                "trino.catalogs in .dex/config.yml (identifiers are "
                "catalog.schema.table)"
            )
        return self._object_meta(entry), list(self._columns.get(identifier, []))

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the run accumulated for one object (estimate
        floors, sampling, skipped escalations). Merged into the dataset's
        ``data_quality`` by the profile engine. Read without listing anything:
        no note here comes from the inventory."""

        return list(self._notes.get(identifier, []))

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        for catalog, schema in self._scopes():
            where = "table_schema <> 'information_schema'"
            if schema:
                where += f" AND table_schema = '{_escape_literal(schema)}'"
            source = f"{_quote_ident(catalog)}.information_schema"
            tables = self._catalog(
                f"SELECT table_schema, table_name, table_type FROM {source}.tables "  # noqa: S608
                f"WHERE {where}"
            )
            for row in tables:
                name = str(row["table_name"])
                # Connector metadata tables (Iceberg's "t$files" and friends) are
                # not listed here today; the filter keeps it that way if a
                # catalog ever starts listing them.
                if "$" in name:
                    continue
                table_schema = str(row["table_schema"])
                identifier = f"{catalog}.{table_schema}.{name}"
                self._objects[identifier] = {
                    "identifier": identifier,
                    "object_type": (
                        "view" if "VIEW" in str(row["table_type"]).upper() else "table"
                    ),
                    "schema": table_schema,
                    "name": name,
                    # information_schema carries no row or byte counts and
                    # nothing else is free; the profile's own COUNT(*) supplies
                    # the exact figure once the table is scanned.
                    "row_count": None,
                    "byte_size": None,
                    "column_count": 0,
                }
            columns = self._catalog(
                "SELECT table_schema, table_name, column_name, data_type, "  # noqa: S608
                f"is_nullable, ordinal_position FROM {source}.columns WHERE {where} "
                "ORDER BY table_schema, table_name, ordinal_position"
            )
            for row in columns:
                identifier = f"{catalog}.{row['table_schema']}.{row['table_name']}"
                entry = self._objects.get(identifier)
                if entry is None:
                    continue
                metas = self._columns.setdefault(identifier, [])
                metas.append(
                    ColumnMeta(
                        name=str(row["column_name"]),
                        data_type=str(row["data_type"]),
                        nullable=str(row["is_nullable"]).upper() == "YES",
                        ordinal=len(metas),
                    )
                )
                entry["column_count"] = len(metas)
        self._inventory_loaded = True

    def _object_meta(self, entry: dict) -> ObjectMeta:
        # An exact count from a profiling scan supersedes the absent metadata
        # figure for the rest of the command, so uniqueness proofs and drift
        # verdicts compare against real rows.
        return ObjectMeta(
            identifier=entry["identifier"],
            object_type=entry["object_type"],
            schema=entry["schema"],
            name=entry["name"],
            row_count=self._exact_rows.get(entry["identifier"], entry["row_count"]),
            byte_size=entry["byte_size"],
            column_count=entry["column_count"],
        )

    def _scopes(self) -> list[tuple[str, str | None]]:
        """Every source scope this command reads, as ``(catalog, schema-or-None)``
        pairs, resolved and proven to exist.

        Resolution is free (coordinator metadata, no scan) and cached for the
        command. It runs before anything is estimated, because a scope that
        resolves to nothing and silently falls back to the whole allowlist is a
        cost-safety bug: the estimate the user confirms would cover tables they
        never named.
        """

        if self._resolved_scopes is None:
            self._resolved_scopes = self._resolve_scopes()
        return self._resolved_scopes

    def _resolve_scopes(self) -> list[tuple[str, str | None]]:
        if not self.target.catalogs:
            # Nothing committed: every catalog the identity can see is the
            # allowlist by definition, and the dev target is already inside it.
            return [
                (catalog, None)
                for catalog in sorted(self._catalog_kinds())
                if catalog != "system"
            ]
        with blame(self._scope_origin, TrinoConnectionError):
            scopes = [
                self._resolve_scope(entry)
                for entry in sorted({e.strip().lower() for e in self.target.catalogs})
            ]

        def exists(scope: str) -> bool:
            catalog, _, schema = scope.partition(".")
            if catalog not in self._catalog_kinds():
                return False
            return schema in self._schemas(catalog)

        dotted = [f"{c}.{s}" if s else c for c, s in scopes]
        return [
            (catalog, schema or None)
            for catalog, _, schema in (
                entry.partition(".")
                for entry in with_dev_target(dotted, self._dev_target, exists)
            )
        ]

    def _resolve_scope(self, entry: str) -> tuple[str, str | None]:
        """One scope entry, proven to exist. Every Trino relation is rooted in a
        catalog, so an entry is ``<catalog>`` or ``<catalog>.<schema>`` and either
        names something or does not."""

        token = entry.strip().lower()
        if not token:
            raise TrinoConnectionError("empty scope entry")
        catalog, _, schema = token.partition(".")
        if "." in schema:
            raise TrinoConnectionError(
                f"scope '{entry}' has too many parts; a source scope is <catalog> "
                "or <catalog>.<schema>, never a table"
            )
        visible = self._catalog_kinds()
        if catalog not in visible:
            raise TrinoConnectionError(
                f"scope '{entry}' names no catalog on this cluster; catalogs "
                f"there: {name_list(sorted(visible))}"
            )
        if not schema:
            return (catalog, None)
        schemas = self._schemas(catalog)
        if schema not in schemas:
            raise TrinoConnectionError(
                f"scope '{entry}' does not exist: catalog {catalog} has no schema "
                f"{schema}; schemas there: {name_list(sorted(schemas))}"
            )
        return (catalog, schema)

    def _catalog_kinds(self) -> dict[str, str]:
        """Every catalog the identity can see, keyed to its connector (memory,
        iceberg, postgresql). Free, cached, and the live credential probe."""

        if self._catalog_kinds_cache is None:
            rows = self._catalog(
                "SELECT catalog_name, connector_name FROM system.metadata.catalogs"
            )
            self._catalog_kinds_cache = {
                str(row["catalog_name"]).lower(): str(row["connector_name"]).lower()
                for row in rows
            }
        return self._catalog_kinds_cache

    def _schemas(self, catalog: str) -> set[str]:
        """Every schema in one catalog. Free, cached per catalog.
        ``information_schema`` is never a source."""

        if catalog not in self._schemas_by_catalog:
            rows = self._catalog(
                f"SELECT schema_name FROM {_quote_ident(catalog)}"  # noqa: S608
                ".information_schema.schemata"
            )
            self._schemas_by_catalog[catalog] = {
                str(row["schema_name"]).lower()
                for row in rows
                if str(row["schema_name"]).lower() != "information_schema"
            }
        return self._schemas_by_catalog[catalog]

    def forget_metadata(self) -> None:
        """Drop everything this command has listed, because the cluster changed.

        Catalog results are cached per command, which holds while the cluster
        holds still. A build is the one command where it does not: dbt writes
        between the pricing pass and the verification that judges its output,
        and dbt-trino creates the dev schema. What survives is what this command
        has paid for or prepared: the session state and its notes.
        """

        self._objects: dict[str, dict] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._exact_rows: dict[str, int] = {}
        self._scan_bytes: dict[str, float | None] = {}
        self._inventory_loaded = False
        self._resolved_scopes: list[tuple[str, str | None]] | None = None
        self._schemas_by_catalog: dict[str, set[str]] = {}

    def read_dev_target(self, scope: str | None) -> None:
        """Also read ``dev_catalog.dev_schema``, the schema this command's build
        writes into, or stop reading it with ``None``.

        Kept apart from the committed allowlist because it is not a source the
        user committed: it is dex reading its own output for one command, and it
        may not exist yet (see :func:`~exmergo_dex_core.adapters.base.with_dev_target`).
        """

        scope = scope.strip().lower() if scope else None
        if scope != self._dev_target:
            self._dev_target = scope
            self.forget_metadata()

    def missing_dev_namespaces(self, namespace: str, *, role: str) -> list[str]:
        """What stops a build from writing into ``dev_catalog.dev_schema``. Free:
        one coordinator metadata read, no scan.

        dbt-trino creates a missing schema but never a catalog, and a catalog is
        only as writable as its connector. So the catalog must exist and must
        not be one of the read-only kinds; a missing schema is left for dbt.

        ``role`` is accepted for the shared signature and not consulted. Trino
        authorization is pluggable (file rules, Ranger, OPA) and exposes no
        grant table to introspect, so whether the dbt identity may write is
        found out by dbt, never guessed here.
        """

        catalog, _, _schema = namespace.strip().lower().partition(".")
        kinds = self._catalog_kinds()
        kind = kinds.get(catalog)
        if kind is None:
            return [
                f'dev_catalog "{catalog}" (it does not exist on this cluster, and '
                "dbt cannot create a catalog; catalogs there: "
                f"{name_list(sorted(kinds))})"
            ]
        if kind in _READ_ONLY_CONNECTORS:
            return [
                f'dev_catalog "{catalog}" (a {kind} catalog, which is read-only; '
                "point dev_catalog at a writable catalog such as iceberg, hive "
                "or delta_lake)"
            ]
        if kind in _UNDURABLE_CONNECTORS:
            self._note(
                f"{namespace}.*",
                f"dev_catalog {catalog} is a {kind} catalog, which "
                f"{_UNDURABLE_CONNECTORS[kind]}",
            )
        return []

    def list_namespace_objects(self, namespace: str) -> list[str]:
        """Table and view names already in ``catalog.schema``. Free: one
        information_schema read, no scan. A schema or catalog that does not
        exist yields nothing to collide with."""

        catalog, _, schema = namespace.strip().lower().partition(".")
        if catalog not in self._catalog_kinds():
            return []
        rows = self._catalog(
            f"SELECT table_name FROM {_quote_ident(catalog)}"  # noqa: S608
            ".information_schema.tables "
            f"WHERE table_schema = '{_escape_literal(schema)}'"
        )
        return sorted(str(row["table_name"]) for row in rows)

    # --- estimation (free; feeds the confirm handshake) -------------------------

    def profile_estimate(
        self, identifiers: list[str], *, include_blobs: set[str] | None = None
    ) -> tuple[float, dict[str, float]]:
        """The heuristic cluster-seconds estimate for profiling: per table, the
        bytes EXPLAIN (TYPE IO) expects a scan of its profiled columns to read,
        over a conservative scan rate, times the number of aggregate batches.

        Blob-type columns are excluded the same way ``explore.profile.profile``
        excludes them from the scan itself (``include_blobs`` names the
        ``identifier.column`` paths a human opted back in), so the columns
        priced are the columns the run reads, and column pruning counts.
        """

        blob_paths = include_blobs or set()
        per_table: dict[str, float] = {}
        for identifier in identifiers:
            _meta, columns = self.table_metadata(identifier)
            scan_columns = [
                c
                for c in columns
                if not is_blob_type(c.data_type)
                or f"{identifier}.{c.name}".lower() in blob_paths
            ]
            batches = max((len(scan_columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = batches * self._table_seconds(
                identifier, [c.name for c in scan_columns]
            )
        return sum(per_table.values()), per_table

    def query_estimate(self, sql: str) -> float:
        """The estimate for one firewall-approved query, from the free
        non-executing EXPLAIN (TYPE IO), which prices the statement after
        pruning and pushdown rather than as a full scan of what it names."""

        return self._statement_seconds(guard(sql))

    def _table_seconds(self, identifier: str, columns: list[str]) -> float:
        """One full scan of ``columns`` in one table, priced and cached."""

        if identifier not in self._scan_bytes:
            projection = ", ".join(_quote_ident(c) for c in columns) or "COUNT(*)"
            sql = f"SELECT {projection} FROM {self._quote(identifier)}"  # noqa: S608
            inputs = self._explain_io(sql)
            known = [b for _, b in inputs or [] if b is not None]
            self._scan_bytes[identifier] = sum(known) if known else None
            if inputs is None:
                self._unplanned += 1
            elif len(known) < len(inputs) or not inputs:
                self._floor(identifier)
        scan = self._scan_bytes[identifier]
        if scan is None:
            return _UNSTATISTICAL_TABLE_SECONDS
        return max(scan / _SCAN_BYTES_PER_SECOND, _MIN_STATEMENT_SECONDS)

    def _statement_seconds(self, sql: str) -> float:
        inputs = self._explain_io(sql)
        if inputs is None:
            # Almost always a build statement over a relation the build has not
            # created yet. Priced at the minimum, as every connector prices a
            # cold dev target: a lower bound the handshake names, with the
            # server-side run-time cap binding each statement regardless. The
            # stats-less table floor would price a first build of thirty tests
            # at two and a half minutes before a single row exists.
            self._unplanned += 1
            return _MIN_STATEMENT_SECONDS
        seconds = 0.0
        for identifier, size in inputs:
            if size is None:
                self._floor(identifier)
                seconds += _UNSTATISTICAL_TABLE_SECONDS
            else:
                seconds += size / _SCAN_BYTES_PER_SECOND
        return max(seconds, _MIN_STATEMENT_SECONDS)

    def _floor(self, identifier: str) -> None:
        self._floored.add(identifier)
        self._note(
            identifier,
            "priced at the floor estimate: its catalog reports no statistics for "
            "EXPLAIN (TYPE IO) to read, so the quote is a guess and the "
            "server-side query_max_run_time is what bounds a scan of it",
        )

    def _explain_io(self, sql: str) -> list[tuple[str, float | None]] | None:
        """Each input table and the bytes the plan expects to read from it
        (``None`` where the catalog has no statistics), or ``None`` when the
        plan cannot be produced.

        The JSON also carries each table's pushed-down predicate ranges, which
        are literal values from the statement; only the estimates are read.
        """

        try:
            rows = self._explain(sql)
            plan = json.loads(rows[0][0])
        except (NotSelectOnlyError, OverCeilingError):
            raise
        except Exception:
            return None
        inputs: list[tuple[str, float | None]] = []
        for info in plan.get("inputTableColumnInfos") or []:
            table = info.get("table") or {}
            schema_table = table.get("schemaTable") or {}
            identifier = ".".join(
                str(p)
                for p in (
                    table.get("catalog"),
                    schema_table.get("schema"),
                    schema_table.get("table"),
                )
            )
            inputs.append(
                (
                    identifier,
                    _finite((info.get("estimate") or {}).get("outputSizeInBytes")),
                )
            )
        return inputs

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The seconds handshake.

        ``estimate_quality`` stays "heuristic" because the number being
        confirmed is seconds from a throughput constant. ``estimate_basis`` says
        whether every table was priced from plan statistics (``explain_io``) or
        at least one was a floor guess (``floor``); the floored tables are named
        in the notes, so a caller knows which part of the quote to distrust.
        """

        notes = [_ESTIMATE_QUALITY_NOTE]
        if self._floored:
            notes.append(
                "priced at the floor because their catalogs report no statistics: "
                + name_list(sorted(self._floored))
            )
        if self._unplanned:
            notes.append(
                f"{self._unplanned} statement(s) could not be planned for an "
                "estimate (EXPLAIN failed, for example over a relation this build "
                "has not created yet) and were priced at the minimum, so the "
                "estimate is a lower bound for them"
            )
        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "estimate_basis": (
                "floor" if self._floored or self._unplanned else "explain_io"
            ),
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in cluster-seconds; the same number "
                "becomes the server-side query_max_run_time, and the bytes it "
                "implies become query_max_scan_physical_bytes)"
            ),
            "notes": notes,
        }
        if per_table:
            data["per_table_seconds"] = per_table
        return data

    def spend_display(self) -> dict:
        """No currency translation exists for cluster load; the seconds in the
        spend summary are the whole story."""

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
        _meta, all_columns = self.table_metadata(identifier)
        estimate = self._table_seconds(
            identifier, [c.name for c in all_columns if not is_blob_type(c.data_type)]
        )
        sample = self._sample_clause(identifier)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(
                identifier,
                batch,
                safe_min_max or set(),
                shape_stats or set(),
                type_stats or set(),
                key_shape_stats or set(),
                temporal_stats or set(),
                sample=sample,
            )
            rows, labels = self._execute(sql, estimate=estimate)
            values = dict(zip(labels, rows[0], strict=True))
            if sample is None:
                self._exact_rows[identifier] = int(values["n_total"])
            results.extend(
                self._read_aggregates(values, plan, sampled=sample is not None)
            )
        return results

    def _sample_clause(self, identifier: str) -> str | None:
        """``TABLESAMPLE BERNOULLI (pct)`` for a table over the threshold, else
        None for a full scan.

        Authored natively: sqlglot's transpile of the shared sampler emits a
        ``ROWS`` form Trino rejects. BERNOULLI keeps each row with the given
        probability, which is uniform but still reads every split, so the note
        says what it saves (CPU and the aggregation's work) and what it does
        not (the scan).
        """

        threshold = self.target.max_full_profile_bytes
        scan = self._scan_bytes.get(identifier)
        if threshold is None or not scan or scan <= threshold:
            return None
        percent = min(max(threshold / scan * 100, 0.01), 100.0)
        self._note(
            identifier,
            f"profiled from a ~{round(percent, 2)}% BERNOULLI sample (the table's "
            "estimated scan exceeds trino.max_full_profile_bytes): BERNOULLI still "
            "reads every split, so the sample reduces CPU rather than the scan; "
            "counts and extremes are approximate and uniqueness is not judged",
        )
        return f"TABLESAMPLE BERNOULLI ({percent:.4g})"

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
        sample: str | None = None,
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]]]:
        # One aggregate statement per batch, a single pass: COUNT(*) once, then
        # per column a non-null count, an approximate distinct (approx_distinct
        # is a HyperLogLog that rides the same pass), min/max only where
        # allowed, and the fraction statistics only where requested. Pure (no
        # connection), so the SELECT-only property is testable offline.
        source = self._quote(identifier)
        if sample is not None:
            source += f" {sample}"
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            degraded = is_degraded_type(col.data_type)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not degraded
            if wants_distinct:
                select_parts.append(f"approx_distinct({qcol}) AS d_{i}")
            wants_min_max = (col.name in safe) and not degraded
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            wants_shape = (col.name in shape) and not degraded
            if wants_shape:
                select_parts.extend(
                    shape_stat_expressions(
                        qcol,
                        i,
                        _regexp_predicate,
                        fraction_literals=_FRACTION_LITERALS,
                    )
                )
            wants_type = (col.name in type_req) and not degraded
            if wants_type:
                select_parts.extend(
                    type_contradiction_expressions(
                        qcol,
                        i,
                        is_string=is_string_type(col.data_type),
                        is_integer=is_integer_type(col.data_type),
                        regexp_predicate=_regexp_predicate,
                        bigint_type=_BIGINT_TYPE,
                        fraction_literals=_FRACTION_LITERALS,
                    )
                )
            wants_key_shape = (col.name in key_shape_req) and not degraded
            if wants_key_shape:
                select_parts.extend(
                    key_shape_expressions(
                        qcol,
                        i,
                        _regexp_predicate,
                        fraction_literals=_FRACTION_LITERALS,
                    )
                )
            wants_temporal = (col.name in temporal_req) and not degraded
            if wants_temporal:
                select_parts.extend(
                    temporal_alignment_expressions(
                        qcol,
                        i,
                        _date_trunc_expr,
                        fraction_literals=_FRACTION_LITERALS,
                    )
                )
                for unit in temporal_units_for(col.data_type):
                    select_parts.extend(
                        temporal_continuity_sql(
                            qcol, i, unit, source, _date_trunc_expr, _date_diff_expr
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
        # keywords, never values; the result is guarded before it runs.
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"  # noqa: S608
        return guard(sql), plan

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
            # approx_distinct is a HyperLogLog sketch, so it is never a
            # uniqueness verdict on its own; the near-unique escalation proves
            # keys with an exact scan. A sampled scan cannot judge uniqueness.
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
        """Exact COUNT(DISTINCT) for near-unique columns, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scan, return nothing and let uniqueness verdicts stay approximate.
        A metered adapter never self-escalates past its ceiling."""

        if not columns:
            return {}
        estimate = self._table_seconds(identifier, columns)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "distinct-count escalation skipped: the remaining budget could "
                "not cover the extra scan; uniqueness verdicts stay approximate",
            )
            return {}
        # COUNT(*) rides along so the same scan also supplies the exact row
        # count grain verdicts compare against.
        select_parts = ["COUNT(*) AS n_total"] + [
            f"COUNT(DISTINCT {_quote_ident(name)}) AS d_{i}"
            for i, name in enumerate(columns)
        ]
        sql = guard(
            f"SELECT {', '.join(select_parts)} FROM {self._quote(identifier)}"  # noqa: S608
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
        can afford (they arrive best-ranked first) and says so."""

        if not combinations:
            return {}
        _meta, columns = self.table_metadata(identifier)
        unit = self._table_seconds(
            identifier, [c.name for c in columns if not is_blob_type(c.data_type)]
        )
        probed, note = affordable_combinations(
            combinations,
            lambda prefix: unit * len(prefix),
            self.cost_gate.try_charge,
        )
        if note:
            self._note(identifier, note)
        if not probed:
            return {}
        table = self._quote(identifier)
        probe = distinct_combination_sql(table, probed, _quote_ident)
        # The row count rides along, because nothing free supplies one on
        # Trino and a distinct count means nothing without the rows it is out
        # of: a grain verdict compares the two.
        sql = guard(
            f"SELECT (SELECT COUNT(*) FROM {table}) AS n_total, "  # noqa: S608
            + probe.removeprefix("SELECT ")
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        self._exact_rows[identifier] = int(values["n_total"])
        return {tuple(combo): int(values[f"d_{i}"]) for i, combo in enumerate(probed)}

    def value_domain_counts(
        self, identifier: str, columns: list[str], *, limit: int
    ) -> dict[str, ValueDomainSample]:
        """Top ``limit`` values by frequency per column, plus each column's exact
        distinct-group count, spent only within the already-confirmed budget."""

        if not columns:
            return {}
        estimate = self._table_seconds(identifier, columns)
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
            # array_agg carries its own ORDER BY because the subquery's order is
            # not one Trino promises to keep through an aggregate.
            parts.append(
                "(SELECT array_agg(ARRAY[CAST(v AS varchar), CAST(c AS varchar)] "  # noqa: S608
                "ORDER BY c DESC, v) FROM "
                f"(SELECT {qcol} AS v, COUNT(*) AS c FROM {table} "
                f"WHERE {qcol} IS NOT NULL GROUP BY {qcol} "
                f"ORDER BY c DESC, v LIMIT {limit}) AS q_{i}) AS d_{i}"
            )
            parts.append(
                f"(SELECT COUNT(DISTINCT {qcol}) FROM {table} "  # noqa: S608
                f"WHERE {qcol} IS NOT NULL) AS n_{i}"
            )
        sql = guard(f"SELECT {', '.join(parts)}")
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        result = {}
        for i, name in enumerate(columns):
            domain = values[f"d_{i}"] or []
            result[name] = ValueDomainSample(
                values=[(entry[0], int(entry[1])) for entry in domain],
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
        cluster-seconds (client preflight plus the server-side caps, whichever
        is tighter)."""

        checked = guard(sql)
        self.cost_gate.charge(self._statement_seconds(checked))
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
        """Guard, heuristic charge, then the capped run."""

        guard(sql)
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
        """The single billed door past the gate: the session caps are wound
        down to what remains of the budget (or the wall-clock limit when that is
        tighter), the statement runs, and what it ran for is settled to the
        ledger whether it finished, failed, or was cancelled."""

        caps, budget_bound = self._billed_caps(timeout_seconds)
        self._session(caps)
        cursor = self._conn.cursor()
        started = self._clock()
        try:
            cursor.execute(sql)
            if fetch_rows is None:
                rows = cursor.fetchall()
            else:
                rows = cursor.fetchmany(fetch_rows)
                # Fetching stops early, the query does not: it keeps reading
                # server-side, and nothing would ever record it. Cancelling a
                # query that already finished is a no-op.
                cursor.cancel()
        except Exception as exc:
            self._settle(cursor, started, sql)
            raise self._translate(exc, budget_bound, timeout_seconds) from exc
        self._settle(cursor, started, sql)
        description = cursor.description or []
        labels = [str(d[0]) for d in description]
        types = [str(d[1]) for d in description]
        return rows, labels, types

    def _billed_caps(
        self, timeout_seconds: float | None
    ) -> tuple[dict[str, str], bool]:
        """The session caps that make the confirmed budget binding, and whether
        the budget (rather than the wall clock) is the time bound.

        ``statement_cap`` refuses outright when less than a second remains, so
        no cap is ever rendered as ``0s`` (which Trino reads as zero, failing
        the statement at once, rather than as unlimited). The byte cap inverts
        the estimate's own throughput constant, so the cap and the quote are
        two views of one number.
        """

        remaining = self.cost_gate.statement_cap(unit="cluster-second")
        caps: dict[str, str] = {}
        seconds: int | None = None
        budget_bound = False
        if remaining is not None:
            seconds = int(remaining)
            budget_bound = True
            caps["query_max_scan_physical_bytes"] = (
                f"{int(remaining * _SCAN_BYTES_PER_SECOND)}B"
            )
        if timeout_seconds is not None:
            wall = int(max(timeout_seconds, 1))
            if seconds is None or wall < seconds:
                seconds = wall
                budget_bound = False
        if seconds is not None:
            caps["query_max_run_time"] = f"{seconds}s"
        return caps, budget_bound

    def _settle(self, cursor: Any, started: float, sql: str) -> None:
        """Record what one statement ran for: the server's elapsed time when it
        finished, the client's wall clock when it did not (the client stops
        polling on an error or a cancel, so its last stats figure is stale)."""

        stats = getattr(cursor, "stats", None) or {}
        elapsed_ms = stats.get("elapsedTimeMillis")
        if stats.get("state") == "FINISHED" and elapsed_ms is not None:
            seconds = float(elapsed_ms) / 1000.0
        else:
            seconds = max(self._clock() - started, 0.0)
        self.cost_gate.record_billed(
            seconds, job_id=getattr(cursor, "query_id", None), statement=sql
        )

    def _session(self, wanted: dict[str, str]) -> None:
        """Bring this adapter's session properties to exactly ``wanted``.

        The one place ``SET SESSION`` and ``RESET SESSION`` are issued. The
        client keeps the properties for every later statement on the
        connection, so each door states the whole set it runs under: a free
        metadata read never inherits a billed statement's tight cap, and a
        billed statement never inherits a free door's loose one. Properties
        already at the wanted value are not re-sent; after a failure the state
        is unknown and the next call sends all of them.
        """

        current = self._session_props
        cursor = None
        try:
            for name in _CAP_PROPERTIES:
                value = wanted.get(name)
                if current is not None and current.get(name) == value:
                    continue
                cursor = cursor or self._conn.cursor()
                # dex-built session statements, not agent SQL: the names are
                # fixed and the values are computed integers with a unit.
                if value is None:
                    cursor.execute(f"RESET SESSION {name}")
                else:
                    cursor.execute(f"SET SESSION {name} = '{value}'")
                cursor.fetchall()
        except Exception as exc:
            self._session_props = None
            raise self._translate(exc) from exc
        self._session_props = dict(wanted)

    def _timeout_refusal(
        self, budget_bound: bool, timeout_seconds: float | None
    ) -> Exception:
        if budget_bound:
            return OverCeilingError(
                "the statement hit the server-side query_max_run_time derived "
                "from the remaining budget; raise --budget or narrow the work"
            )
        limit = f"{timeout_seconds:g}s" if timeout_seconds is not None else "its limit"
        return TimeoutError(
            f"query exceeded {limit} and was cancelled; narrow it (tighter "
            "filter, fewer columns) and retry"
        )

    def _translate(
        self,
        exc: Exception,
        budget_bound: bool = False,
        timeout_seconds: float | None = None,
    ) -> Exception:
        name = getattr(exc, "error_name", None)
        if name == _TIME_LIMIT:
            return self._timeout_refusal(budget_bound, timeout_seconds)
        if name == _SCAN_LIMIT:
            return OverCeilingError(
                "the statement hit the server-side query_max_scan_physical_bytes "
                "derived from the remaining budget; raise --budget or narrow the "
                "work"
            )
        if name == _PERMISSION_DENIED:
            # The server's own sentence is kept: it names what was refused and
            # to whom, which separates the dex identity lacking a read from a
            # view whose owner may not pass one on. It carries relation and
            # identity names, never a value.
            said = str(getattr(exc, "message", "") or "").strip().splitlines()
            detail = f" ({said[0][:300]})" if said else ""
            return TrinoConnectionError(
                f"the cluster's access control refused the statement{detail}. "
                "dex reads only; check that the identity it connects as may read "
                "the source catalogs, and that the dbt identity holds "
                "GRANT_SELECT on what its views read (see references/trino.md "
                "for the rule)"
            )
        if name in _NOT_FOUND:
            return TrinoConnectionError(
                "the statement names a catalog, schema or table that does not "
                "exist on this cluster, or that the identity dex connects as "
                "cannot see; check trino.catalogs in .dex/config.yml"
            )
        if isinstance(exc, self._errors.TrinoQueryError):
            return warehouse_refusal(str(exc.message or exc), code=name)
        if isinstance(exc, self._errors.TrinoAuthError):
            return ConnectorError(
                "the Trino cluster refused the credential dex connected with; "
                "check the user and password or token in its environment"
            )
        if isinstance(
            exc, (self._errors.OperationalError, self._errors.HttpError, OSError)
        ):
            return ConnectorError(
                "the Trino cluster could not be reached; the coordinator may be "
                "down or unreachable from here"
            )
        return exc

    # --- helpers ------------------------------------------------------------------

    def _catalog(self, sql: str) -> list[dict]:
        """Free metadata door: engine-built information_schema and system
        catalog SELECTs only, run on the coordinator with no scan. Results come
        back as dicts keyed by the column names."""

        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("only SELECT statements pass through the catalog door")
        self._session({"query_max_run_time": f"{_METADATA_SECONDS}s"})
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as exc:
            raise self._translate(exc) from exc
        labels = [str(d[0]) for d in cursor.description or []]
        return [dict(zip(labels, row, strict=True)) for row in rows]

    def _explain(self, sql: str) -> list:
        """Free planner door: the statement is guarded first, then prefixed with
        EXPLAIN (TYPE IO, FORMAT JSON) here, so nothing but a plan request can
        pass. Planning reads statistics, never data."""

        checked = guard(sql)
        self._session({"query_max_run_time": f"{_METADATA_SECONDS}s"})
        cursor = self._conn.cursor()
        try:
            cursor.execute(f"EXPLAIN (TYPE IO, FORMAT JSON) {checked}")
            return cursor.fetchall()
        except Exception as exc:
            raise self._translate(exc) from exc

    def _note(self, identifier: str, note: str) -> None:
        notes = self._notes.setdefault(identifier, [])
        if note not in notes:
            notes.append(note)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.split(".")
        if len(parts) != 3:
            raise ValueError(f"expected catalog.schema.table, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        # A host-supplied connection stays open: dex closes only what it opened.
        if not self._owns_connection:
            return
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()


def _finite(value: Any) -> float | None:
    """One EXPLAIN estimate as a float, or None where the catalog has none.
    Trino writes an unknown estimate as the JSON string ``"NaN"``."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quote_ident(name: str) -> str:
    """Quote one identifier component with double quotes, doubling embedded
    quotes. Trino folds unquoted names to lower case and stores them that way,
    so quoting the stored spelling names exactly that object."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_literal(value: str) -> str:
    # Standard SQL string literals: only the quote is special, and a backslash
    # is an ordinary character (doubling it, as ClickHouse needs, would change
    # the value).
    return value.replace("'", "''")
