"""The Redshift adapter: compute-time billing over the Postgres catalog model.

Amazon Redshift is Postgres-derived on the wire and in the catalog, but bills
like a compute-time warehouse: Redshift Serverless charges RPU-hours whenever
compute is active, with a 60-second minimum each time an idle workgroup wakes.
So the guarded quantity is **seconds**, translated to RPU-hours (and dollars
when ``redshift.rpu_price_usd`` is configured) through the workgroup's base
capacity, and every billed statement is capped server-side by a
``statement_timeout`` wound down to what remains of the budget, so a wrong
estimate cannot overrun the ceiling: Redshift kills the statement. Actual
spend is wall-clock seconds per statement, recorded to the ledger as
``billed_seconds``. Provisioned clusters ride the same paths with seconds-only
accounting (node-hours bill flat, so there is nothing to translate).

Redshift has no dry-run, and on Serverless even ``EXPLAIN`` and catalog
lookups count as billable activity (AWS bills every incoming query as user
activity), so there is no genuinely free planner door to quote from. Estimates
are therefore a documented heuristic: table bytes over a conservative
capacity-scaled scan rate, floored once per command at the 60-second wake
minimum on Serverless. The metadata paths (inventory, ``connect test``) stay
ungated - they are engine-built catalog lookups measured in milliseconds - but
``capabilities`` says plainly that touching an idle Serverless workgroup can
incur the wake minimum.

Profiling batches aggregates in single passes (COUNT(*), non-null counts,
APPROXIMATE COUNT(DISTINCT), min/max only for engine-cleared safe columns):
Redshift keeps no usable planner distincts, so approximate distincts ride the
billed batch, and near-unique keys escalate to an exact COUNT(DISTINCT) inside
the confirmed budget. There is no sampled-profiling knob: Redshift has no
TABLESAMPLE, so the budget is the only bound.

Read-only is enforced in depth: the SELECT-only guard in the redshift dialect
on every data statement through one execution door, an adapter that issues no
mutating statements (catalog SELECTs and session SETs only), a best-effort
session read-only mode (probed, surfaced honestly when Redshift declines it),
and the documented least-privilege grants.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from ..config import RedshiftTarget
from ..envelope import Paradigm
from ..guards.cost_guard import CostGate, OverCeilingError
from ..guards.sql_guard import assert_select_only
from .base import (
    ColumnAggregate,
    ColumnMeta,
    ObjectMeta,
    QueryResult,
    blame,
    distinct_combination_sql,
    json_safe,
    name_list,
    shape_stat_expressions,
    shape_stat_value,
)

PARADIGM = "compute_time"
DIALECT = "redshift"

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 4 expressions per column).
_COLUMN_BATCH = 50

# Redshift Serverless bills a minimum of 60 seconds of compute each time an
# idle workgroup wakes. There is no API that says whether compute is currently
# warm (and probing with a query is itself billable activity), so estimates on
# Serverless carry this floor once per command, honestly labeled an upper
# bound that actuals waive when compute was already active.
_WAKE_MINIMUM_SECONDS = 60.0

# The estimate heuristic: a deliberately conservative scan rate at the
# reference capacity, scaled linearly by the workgroup's base RPUs (bigger
# workgroups scan proportionally faster, so estimated seconds shrink while
# estimated RPU-hours stay comparable).
_REFERENCE_CAPACITY_RPUS = 8.0
_BASE_SCAN_BYTES_PER_SECOND = 50 * 1024 * 1024

# Every billed statement estimates at least this much.
_MIN_STATEMENT_SECONDS = 1.0

# SVV_TABLE_INFO reports size in 1 MB blocks.
_TABLE_INFO_BLOCK_BYTES = 1024 * 1024

# Types where distinct counts and min/max are invalid or meaningless: only a
# non-null count is computed. Matched against SVV_COLUMNS data_type
# (lowercase). VARBYTE surfaces as "binary varying" in the information-schema
# vocabulary; both spellings are kept in case the catalog changes its mind.
_DEGRADED_TYPE_PREFIXES = (
    "super",
    "varbyte",
    "binary varying",
    "geometry",
    "geography",
    "hllsketch",
)

# Schemas that are never sources and never count as visible.
_SYSTEM_SCHEMAS = ("information_schema", "catalog_history", "pg_auto_copy")

# redshift_connector reports column types as bare Postgres OIDs (verified
# live: 1043, 20, ...); the common ones get their names back for the
# envelope, unknowns stay the numeric token.
_OID_TYPE_NAMES = {
    16: "bool",
    20: "int8",
    21: "int2",
    23: "int4",
    25: "text",
    700: "float4",
    701: "float8",
    1042: "bpchar",
    1043: "varchar",
    1082: "date",
    1083: "time",
    1114: "timestamp",
    1184: "timestamptz",
    1266: "timetz",
    1700: "numeric",
    2950: "uuid",
    3802: "super",
}

_ESTIMATE_QUALITY_NOTE = (
    "Redshift has no dry-run: the estimate is a heuristic (table bytes over a "
    "conservative capacity-scaled scan rate); the confirmed budget is still "
    "hard-enforced by a per-statement server-side statement_timeout"
)

_SIZE_FACTS_NOTE = (
    "this user may not read SVV_TABLE_INFO, so table sizes and row counts are "
    "unknown and scan estimates degrade to minimums (the budget still binds "
    "via the server-side statement_timeout); grant it with: GRANT SELECT ON "
    "svv_table_info TO <user>"
)


def _regexp_predicate(qcol: str, pattern: str) -> str:
    # ~ matches substrings; the shared patterns' anchors make it a full match.
    return f"{qcol} ~ '{pattern}'"


class RedshiftConnectionError(Exception):
    """Raised when a queried object cannot be resolved in the configured
    scope. The message always names the fix, never a credential."""


class RedshiftAdapter:
    """Holds one Redshift connection plus the cost gate for one command.

    ``connection`` is injectable (class DI) so unit tests drive a fake; the
    real connection is built by ``connect.py`` from discovered parameters
    (autocommit, ``application_name=dex``). ``compute`` carries the facts
    connect.py resolved from the control plane (serverless vs provisioned,
    workgroup, base capacity); the adapter itself never talks to boto3, so
    the fake stays a pure database double. ``clock`` is injectable so the
    fake can simulate statement duration; it is what actual billed seconds
    are measured with.
    """

    name = "redshift"
    dialect = DIALECT
    paradigm = Paradigm.COMPUTE_TIME

    def __init__(
        self,
        *,
        connection: Any,
        cost_gate: CostGate,
        target: RedshiftTarget | None = None,
        compute: dict | None = None,
        auth_method: str = "unknown",
        scope_origin: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._conn = connection
        self.cost_gate = cost_gate
        self.target = target or RedshiftTarget()
        # {"kind": "serverless"|"provisioned", "workgroup": str|None,
        #  "base_capacity_rpus": float|None}; None means connect.py could not
        # resolve the control-plane facts (translation degrades, gating holds).
        self.compute = compute or {}
        self.auth_method = auth_method
        # What the scope entries in the target came from, so a refusal names the
        # thing the user has to go edit: a per-command flag or the committed
        # allowlist. `narrow_target` has already collapsed the two by the time
        # the adapter sees them, and the fix differs entirely.
        self._scope_origin = scope_origin or "redshift.schemas in .dex/config.yml"
        self._clock = clock
        # Imported lazily (the caller constructed the connection, so the
        # library is present); the error types drive refusal translation.
        from redshift_connector import error as rs_errors

        self._rs_errors = rs_errors
        # Catalog results are cached per command: the estimate pass and the
        # confirmed run share table facts, and each lookup is cheap but a
        # round-trip.
        self._objects: dict[str, dict] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._exact_rows: dict[str, int] = {}
        self._inventory_loaded = False
        self._resolved_schemas: list[str] | None = None
        self._visible_schemas: set[str] | None = None
        self._database: str | None = None
        self._notes: dict[str, list[str]] = {}
        # The 60s wake minimum is charged once per command, by whichever
        # billed statement runs first; only Serverless bills it.
        self._wake_floor_pending = self._is_serverless()
        self._wake_floor_quoted = False
        self._session_prepared = False
        self._session_read_only: bool | None = None
        self._size_facts_denied = False

    # --- capabilities (free-tier metadata) --------------------------------------

    def capabilities(self) -> dict[str, object]:
        # The probe round-trips to the server (current facts, not cached
        # ones), so a stale or underprivileged credential fails here instead
        # of reporting a healthy connection.
        probe = self._catalog(
            "SELECT current_database() AS db, version() AS server_version"
        )[0]
        self._database = str(probe["db"])
        cost = self.cost_gate.cost()
        serverless = self._is_serverless()
        caps: dict[str, object] = {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            # Honest: Redshift may decline the session read-only mode; the
            # SELECT-only guard and grants still hold either way.
            "session_read_only": bool(self._session_read_only),
            "paradigm": self.paradigm.value,
            "auth_method": self.auth_method,
            "database": self._database,
            "server_version": self._version_token(str(probe["server_version"])),
            "schema_count": len(self._schema_scope()),
            "compute": {
                "kind": self.compute.get("kind", "unknown"),
                "workgroup": self.compute.get("workgroup"),
                "base_capacity_rpus": self.compute.get("base_capacity_rpus"),
            },
            "required_grants": [
                "USAGE on source schemas and SELECT on their tables",
                "write only on the dedicated dbt dev schema (transform build)",
            ],
        }
        budget: dict[str, object] = {
            "ceiling_seconds": cost.ceiling,
            "session_spent_today_seconds": self.cost_gate.session_spent,
        }
        if cost.ceiling is not None:
            rpu_hours = self._to_rpu_hours(cost.ceiling)
            if rpu_hours is not None:
                budget["ceiling_rpu_hours"] = rpu_hours
        caps["budget"] = budget
        if serverless:
            caps["warnings"] = [
                "Redshift Serverless bills every incoming query as compute "
                "activity, with a 60-second minimum each time an idle "
                "workgroup wakes; even metadata commands can incur that "
                "minimum when compute is idle"
            ]
        return caps

    @staticmethod
    def _version_token(raw: str) -> str:
        # version() reads like "PostgreSQL 8.0.2 ... Redshift 1.0.12345";
        # surface the Redshift token when present, else the first word pair.
        # The driver hands the string back NUL-terminated (verified live), so
        # strip control characters before tokenizing.
        raw = raw.replace("\x00", " ").strip()
        lowered = raw.lower()
        if "redshift" in lowered:
            tail = raw[lowered.index("redshift") :]
            return " ".join(tail.split()[:2])
        return " ".join(raw.split()[:2])

    # --- introspection (cheap catalog metadata; no scans) -----------------------

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
            raise RedshiftConnectionError(
                f"object '{identifier}' not found in the configured scope; "
                "check redshift.schemas in .dex/config.yml (identifiers are "
                "database.schema.table against the connected database)"
            )
        return self._object_meta(entry), list(self._columns.get(identifier, []))

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the profiling run accumulated for one object
        (skipped escalations, missing size facts). Merged into the dataset's
        ``data_quality`` by the profile engine."""

        notes = list(self._notes.get(identifier, []))
        if self._size_facts_denied:
            notes.append(_SIZE_FACTS_NOTE)
        return notes

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        database = self._current_database()
        # The resolved scopes, not the raw config: an entry that names nothing is
        # refused before this filter drops it, which is the silent-empty-inventory
        # bug the Postgres-shaped connectors are most exposed to. The scope is
        # pushed into the census SQL (each name proven to exist by resolution,
        # quote-escaped) so a one-schema scope in a wide warehouse does not
        # transfer every other schema's rows just to drop them client-side;
        # the client-side check stays as the belt to that suspender.
        allowed = set(self._schema_scope()) if self.target.schemas else set()
        scope_names = ", ".join(f"'{_escape_literal(s)}'" for s in sorted(allowed))

        def scoped(column: str, lead: str = " AND") -> str:
            return f"{lead} {column} IN ({scope_names})" if scope_names else ""

        # pg_class is the object census (SVV_TABLE_INFO omits tables that hold
        # no data, so it cannot be the census without silently losing empty
        # tables); SVV_TABLE_INFO then contributes size and row facts.
        # Interpolated parts are fixed system-schema names and resolved,
        # escaped scope names, never agent values.
        rows = self._catalog(
            "SELECT n.nspname AS schema_name, c.relname AS object_name, "  # noqa: S608
            "c.relkind AS kind "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'v') "
            f"AND {_schema_filter('n.nspname')}{scoped('n.nspname')}"
        )
        for row in rows:
            schema = str(row["schema_name"])
            if allowed and schema not in allowed:
                continue
            name = str(row["object_name"])
            # Materialized views surface as a view plus an mv_tbl__ backing
            # table; the backing table is an implementation detail, not a
            # source object.
            if name.startswith("mv_tbl__"):
                continue
            object_type = "view" if str(row["kind"]) == "v" else "table"
            identifier = f"{database}.{schema}.{name}"
            self._objects[identifier] = {
                "identifier": identifier,
                "object_type": object_type,
                "schema": schema,
                "name": name,
                "row_count": None,
                "byte_size": None,
                "column_count": 0,
            }
        # Size and row facts: catalog-maintained, no scan. Absence means the
        # table holds no data (SVV_TABLE_INFO omits empty tables by design).
        # An auto-created IAM user can lack SELECT on the view (verified
        # live); inventory then degrades to sizeless entries with a note
        # rather than failing, and the server-side statement_timeout is what
        # keeps the now-floored estimates from ever overrunning a budget.
        try:
            size_predicate = scoped('"schema"', lead=" WHERE")
            size_rows = self._catalog(
                'SELECT "schema" AS schema_name, "table" AS table_name, '  # noqa: S608
                f"size, tbl_rows FROM svv_table_info{size_predicate}"
            )
        except Exception:
            self._size_facts_denied = True
            size_rows = []
        for row in size_rows:
            identifier = f"{database}.{row['schema_name']}.{row['table_name']}"
            entry = self._objects.get(identifier)
            if entry is None:
                continue
            size = row.get("size")
            tbl_rows = row.get("tbl_rows")
            entry["byte_size"] = (
                int(size) * _TABLE_INFO_BLOCK_BYTES if size is not None else None
            )
            entry["row_count"] = int(float(tbl_rows)) if tbl_rows is not None else None
        if not self._size_facts_denied:
            for entry in self._objects.values():
                if entry["object_type"] == "table" and entry["byte_size"] is None:
                    entry["row_count"] = 0
                    entry["byte_size"] = 0
        # SVV_COLUMNS covers ordinary and late-binding relations alike, which
        # pg_attribute cannot for late-binding views.
        columns = self._catalog(
            "SELECT table_schema AS schema_name, table_name AS object_name, "  # noqa: S608
            "column_name, data_type, is_nullable "
            "FROM svv_columns "
            f"WHERE {_schema_filter('table_schema')}{scoped('table_schema')} "
            "ORDER BY table_schema, table_name, ordinal_position"
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
                    nullable=str(row["is_nullable"]).upper() != "NO",
                    ordinal=len(metas),
                )
            )
            entry["column_count"] = len(metas)
        self._inventory_loaded = True

    def _object_meta(self, entry: dict) -> ObjectMeta:
        # An exact count from a profiling scan supersedes the catalog estimate
        # for the rest of the command, so uniqueness proofs and drift verdicts
        # compare against real rows, not tbl_rows.
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
        """Every source schema this command reads, proven to exist.

        Resolution is one catalog SELECT (no scan) and cached for the command.
        It runs before anything is estimated, because a scope that resolves to
        nothing and silently falls back to the whole allowlist is a
        cost-safety bug: the estimate the user confirms would cover tables
        they never named.
        """

        if self._resolved_schemas is None:
            self._resolved_schemas = self._resolve_schemas()
        return self._resolved_schemas

    def _resolve_schemas(self) -> list[str]:
        visible = self._schemas()
        if not self.target.schemas:
            return sorted(visible)
        with blame(self._scope_origin, RedshiftConnectionError):
            return sorted(
                {self._resolve_schema(entry, visible) for entry in self.target.schemas}
            )

    def _resolve_schema(self, entry: str, visible: set[str]) -> str:
        """One scope entry, proven to exist. A Redshift scope is always a bare
        schema in the connected database (dbt refuses cross-database
        references outright), so there is nothing to qualify and nothing to
        disambiguate."""

        token = entry.strip()
        if not token:
            raise RedshiftConnectionError("empty scope entry")
        if "." in token:
            raise RedshiftConnectionError(
                f"scope '{entry}' has too many parts; a Redshift source scope is "
                "a bare <schema> in the connected database "
                f"({self._current_database()}), never a database or a table"
            )
        if token not in visible:
            raise RedshiftConnectionError(
                f"scope '{entry}' names no schema in database "
                f"{self._current_database()}; schemas there: "
                f"{name_list(sorted(visible))}"
            )
        return token

    def _schemas(self) -> set[str]:
        """Every non-system schema in the connected database. Cheap (one
        catalog SELECT, no scan), cached, and the live credential probe."""

        if self._visible_schemas is None:
            rows = self._catalog(
                "SELECT nspname FROM pg_catalog.pg_namespace "  # noqa: S608
                f"WHERE {_schema_filter('nspname')}"
            )
            self._visible_schemas = {str(row["nspname"]) for row in rows}
        return self._visible_schemas

    def missing_dev_namespaces(self, schema: str, *, role: str) -> list[str]:
        """What stops ``role`` from building into the dev schema. Catalog
        lookups and privilege predicates, no scan.

        dbt creates the schema itself, so its absence is not fatal on its own:
        what is fatal is the privilege to create it. ``role`` is the user in
        the rendered profile rather than the connected user, because dex may
        legitimately read as a read-only user while dbt writes as another, and
        Redshift will answer a privilege question about any user.
        """

        if not self._user_exists(role):
            raise RedshiftConnectionError(
                f"the dbt user {role} does not exist in database "
                f"{self._current_database()}; create it, or point the profile at "
                "a user that exists"
            )
        who = f"'{_escape_literal(role)}'"
        if schema not in self._schemas():
            # dbt will issue CREATE SCHEMA IF NOT EXISTS on the first build,
            # which needs CREATE on the database. Absent that, the build dies
            # with a bare permission error naming neither the schema nor the
            # grant.
            row = self._catalog(
                f"SELECT has_database_privilege({who}, current_database(), "
                "'CREATE') AS may_create"
            )[0]
            return [] if _truthy(row["may_create"]) else [f'dev_schema "{schema}"']
        where = f"{who}, '{_escape_literal(schema)}'"
        row = self._catalog(
            f"SELECT has_schema_privilege({where}, 'CREATE') AS may_create, "
            f"has_schema_privilege({where}, 'USAGE') AS may_use"
        )[0]
        missing = [
            privilege
            for privilege, granted in (
                ("USAGE", row["may_use"]),
                ("CREATE", row["may_create"]),
            )
            if not _truthy(granted)
        ]
        return [f'{", ".join(missing)} on dev_schema "{schema}"'] if missing else []

    def dev_namespace_objects(self, schema: str) -> list[str]:
        """Table and view names already in one schema. Cheap: one catalog
        SELECT, no scan. A schema that does not exist yields no rows, i.e.
        nothing to collide with. ``mv_tbl__`` backing tables are dropped for
        the same reason inventory drops them: implementation detail, not an
        object anyone named. No role parameter: content is role-independent."""

        rows = self._catalog(
            "SELECT c.relname AS object_name "  # noqa: S608
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'v') "
            f"AND n.nspname = '{_escape_literal(schema)}'"
        )
        return sorted(
            str(row["object_name"])
            for row in rows
            if not str(row["object_name"]).startswith("mv_tbl__")
        )

    def _user_exists(self, user: str) -> bool:
        rows = self._catalog(
            "SELECT 1 AS present FROM pg_catalog.pg_user "  # noqa: S608
            f"WHERE usename = '{_escape_literal(user)}'"
        )
        return bool(rows)

    # --- the RPU translation ------------------------------------------------------

    def _is_serverless(self) -> bool:
        return self.compute.get("kind") == "serverless"

    def _to_rpu_hours(self, seconds: float) -> float | None:
        """Seconds of active compute translated to RPU-hours through the
        workgroup's base capacity, or ``None`` when the control-plane facts
        were unavailable or the target is provisioned (flat node-hours have
        nothing to translate)."""

        capacity = self.compute.get("base_capacity_rpus")
        if not self._is_serverless() or not capacity:
            return None
        return round(seconds * float(capacity) / 3600.0, 6)

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The compute-time handshake payload: seconds are the binding number,
        RPU-hours (and dollars when the price is configured) ride alongside as
        the familiar translation."""

        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in compute-seconds; the same number "
                "becomes the server-side statement_timeout)"
            ),
            "notes": [_ESTIMATE_QUALITY_NOTE],
        }
        if self._wake_floor() > 0:
            data["notes"] = [
                _ESTIMATE_QUALITY_NOTE,
                "includes the 60-second Serverless wake minimum once (an "
                "upper bound: actuals waive it when compute was already "
                "active)",
            ]
        if per_table:
            data["per_table_seconds"] = per_table
        rpu_hours = self._to_rpu_hours(estimate)
        if rpu_hours is not None:
            data["estimated_rpu_hours"] = rpu_hours
            data["rpu_rate"] = {
                "workgroup": self.compute.get("workgroup"),
                "base_capacity_rpus": self.compute.get("base_capacity_rpus"),
                # Serverless can scale above base capacity, so the translation
                # is a floor, not a promise.
                "approximate": True,
            }
            if self.target.rpu_price_usd is not None:
                data["estimated_usd"] = round(rpu_hours * self.target.rpu_price_usd, 4)
        return data

    def spend_display(self) -> dict:
        """RPU translation of actual seconds, merged into ``data.spend``."""

        summary: dict[str, object] = {}
        rpu_hours = self._to_rpu_hours(self.cost_gate.spend_summary()["seconds_billed"])
        if rpu_hours is not None:
            summary["rpu_hours_billed"] = rpu_hours
            if self.target.rpu_price_usd is not None:
                summary["usd_billed"] = round(rpu_hours * self.target.rpu_price_usd, 4)
        return summary

    # --- estimation (feeds the confirm handshake; no scans) -----------------------

    def profile_estimate(
        self, identifiers: list[str]
    ) -> tuple[float, dict[str, float]]:
        """The heuristic compute-seconds estimate for profiling: per table,
        its bytes over the capacity-scaled scan rate times the number of
        aggregate batches; plus the 60-second wake minimum once on
        Serverless. Everything comes from catalog metadata."""

        per_table: dict[str, float] = {}
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            batches = max((len(columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = batches * self._scan_seconds(meta.byte_size)
        return sum(per_table.values()) + self._estimate_floor(), per_table

    def query_estimate(self, sql: str) -> float:
        """The heuristic estimate for one firewall-approved query: the summed
        bytes of every referenced table over the scan rate, plus the wake
        minimum once on Serverless."""

        checked = assert_select_only(sql, dialect=self.dialect)
        return self._statement_estimate(checked) + self._estimate_floor()

    def _statement_estimate(self, sql: str) -> float:
        # Unknown or empty referenced tables contribute nothing; _scan_seconds
        # floors the zero-byte case to the per-statement minimum.
        total_bytes = 0
        self._load_inventory()
        for identifier in self._referenced_tables(sql):
            entry = self._objects.get(identifier)
            if entry and entry["byte_size"] is not None:
                total_bytes += entry["byte_size"]
        return self._scan_seconds(total_bytes)

    def _scan_seconds(self, byte_size: int | None) -> float:
        if not byte_size:
            return _MIN_STATEMENT_SECONDS
        # Bigger workgroups scan proportionally faster; provisioned clusters
        # (no capacity fact) use the reference rate unscaled.
        capacity = self.compute.get("base_capacity_rpus")
        factor = (
            max(float(capacity) / _REFERENCE_CAPACITY_RPUS, 1.0) if capacity else 1.0
        )
        rate = _BASE_SCAN_BYTES_PER_SECOND * factor
        return max(byte_size / rate, _MIN_STATEMENT_SECONDS)

    def _wake_floor(self) -> float:
        """60 once per command on Serverless (each wake bills a 60-second
        minimum and warmth is unknowable without spending), else 0."""

        return _WAKE_MINIMUM_SECONDS if self._wake_floor_pending else 0.0

    def _estimate_floor(self) -> float:
        """The wake minimum's share of an estimate: included exactly once per
        command. A command that runs many statements wakes compute at most
        once, so a sum of per-statement estimates (maintain sweeps a probe
        per table) must not multiply the floor into it (verified live: a
        twelve-probe sweep quoted 738 seconds of which 720 were floors)."""

        if self._wake_floor() <= 0 or self._wake_floor_quoted:
            return 0.0
        self._wake_floor_quoted = True
        return _WAKE_MINIMUM_SECONDS

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

    # --- profiling (billed; every statement estimated and gated) ----------------

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
        meta, _ = self.table_metadata(identifier)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(identifier, batch, safe, shape)
            rows, labels = self._execute(
                sql, estimate=self._scan_seconds(meta.byte_size)
            )
            values = dict(zip(labels, rows[0], strict=True))
            self._exact_rows[identifier] = int(values["n_total"])
            results.extend(self._read_aggregates(values, plan))
        return results

    def _build_aggregate_sql(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        shape: set[str],
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool]]]:
        # One aggregate statement per batch, a single pass: COUNT(*) once,
        # then per column a non-null count, an approximate distinct (HLL),
        # min/max only where allowed, and value-shape fractions only where
        # requested. Pure (no connection), so the SELECT-only property is
        # testable offline. Degraded types get the non-null count only: a
        # distinct count over serialized SUPER or geometry values is not a
        # meaningful cardinality even where the server accepts it (verified
        # live: it does), and MIN/MAX would carry values.
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            degraded = self._is_degraded(col.data_type)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not degraded
            if wants_distinct:
                # HLL(), not APPROXIMATE COUNT(DISTINCT): the same estimator,
                # but Redshift refuses more than 3 APPROXIMATE COUNT branches
                # per statement (verified live) while HLL() carries no such
                # limit, so a wide batch stays a single pass.
                select_parts.append(f"HLL({qcol}) AS nd_{i}")
            wants_min_max = (col.name in safe) and not degraded
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            wants_shape = (col.name in shape) and not degraded
            if wants_shape:
                select_parts.extend(shape_stat_expressions(qcol, i, _regexp_predicate))
            plan.append((i, col, wants_distinct, wants_min_max, wants_shape))
        # Interpolated parts are quoted identifiers and fixed aggregate
        # keywords, never values; the result is guarded as a read-only SELECT.
        sql = f"SELECT {', '.join(select_parts)} FROM {self._quote(identifier)}"  # noqa: S608
        return assert_select_only(sql, dialect=self.dialect), plan

    @staticmethod
    def _is_degraded(data_type: str) -> bool:
        return data_type.lower().startswith(_DEGRADED_TYPE_PREFIXES)

    @staticmethod
    def _read_aggregates(
        values: dict,
        plan: list[tuple[int, ColumnMeta, bool, bool, bool]],
    ) -> list[ColumnAggregate]:
        n_total = int(values["n_total"])
        aggregates: list[ColumnAggregate] = []
        for i, col, wants_distinct, wants_min_max, wants_shape in plan:
            nn = values.get(f"nn_{i}")
            null_fraction = (
                (1 - int(nn) / n_total) if nn is not None and n_total > 0 else None
            )
            distinct = (
                int(values[f"nd_{i}"]) if wants_distinct and n_total > 0 else None
            )
            # An approximate distinct is never a uniqueness verdict on its
            # own; the near-unique escalation proves keys with an exact scan.
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=None,
                    min_value=(
                        json_safe(values.get(f"mn_{i}")) if wants_min_max else None
                    ),
                    max_value=(
                        json_safe(values.get(f"mx_{i}")) if wants_min_max else None
                    ),
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
        """Exact COUNT(DISTINCT) for near-unique columns, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scan, return nothing and let uniqueness verdicts stay
        approximate. A metered adapter never self-escalates past its ceiling.
        """

        if not columns:
            return {}
        meta, _ = self.table_metadata(identifier)
        # The escalation can be a command's first billed statement (maintain
        # grain runs key checks before any probe), so the pending Serverless
        # wake minimum rides the charge; on refusal it stays pending.
        estimate = self._scan_seconds(meta.byte_size) + self._wake_floor()
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "distinct-count escalation skipped: the remaining budget could "
                "not cover the extra scan; uniqueness verdicts stay approximate",
            )
            return {}
        self._consume_wake_floor()
        # COUNT(*) rides along so the same scan also upgrades the catalog's
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

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ) -> dict[tuple[str, ...], int]:
        """Exact distinct count per column combination, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scans (one per combination), return nothing and let the grain
        stay unknown. A metered adapter never self-escalates past its ceiling.
        """

        if not combinations:
            return {}
        meta, _ = self.table_metadata(identifier)
        # Like the exact-distinct escalation, this can be a command's first
        # billed statement, so the pending Serverless wake minimum rides the
        # charge; on refusal it stays pending.
        estimate = (
            self._scan_seconds(meta.byte_size) * len(combinations) + self._wake_floor()
        )
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "composite-key probe skipped: the remaining budget could not "
                "cover the extra scan; grain stays unknown",
            )
            return {}
        self._consume_wake_floor()
        sql = assert_select_only(
            distinct_combination_sql(
                self._quote(identifier), combinations, _quote_ident
            ),
            dialect=self.dialect,
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        return {
            tuple(combo): int(values[f"d_{i}"]) for i, combo in enumerate(combinations)
        }

    # --- execution (the single billed door) --------------------------------------

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT, bounded in rows, wall time,
        and compute-seconds (client preflight plus the server-side statement
        timeout, whichever is tighter)."""

        checked = assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(
            self._statement_estimate(checked) + self._consume_wake_floor()
        )
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
        self.cost_gate.charge(estimate + self._consume_wake_floor())
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
        except self._rs_errors.ProgrammingError as exc:
            raise self._translate(exc, budget_bound, timeout_seconds) from exc
        finally:
            # In a finally, not per-branch: a dropped connection or an
            # interrupt mid-statement still billed the seconds that ran, and
            # spend the ledger never sees erodes the session ceiling.
            self._record_elapsed(started, sql)
        labels = [self._label(d) for d in cursor.description]
        types = [self._description_type(d) for d in cursor.description]
        return rows, labels, types

    def _billed_cursor(self, timeout_seconds: float | None = None) -> tuple[Any, bool]:
        """A cursor prepared for spend: the session prepared once, and the
        server-side statement timeout wound down to what remains of the
        budget, so even a wrong heuristic cannot overrun the ceiling. Returns
        the cursor and whether the budget (not the wall clock) is the binding
        bound."""

        remaining = self.cost_gate.remaining_for_statement()
        if remaining is not None and remaining < 1:
            raise OverCeilingError(
                "the remaining budget is under one compute-second; raise "
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

    def _translate(
        self, exc: Exception, budget_bound: bool, timeout_seconds: float | None
    ) -> Exception:
        # A statement_timeout kill surfaces as SQLSTATE 57014 (query_canceled)
        # with the message "Query cancelled on user's request" (verified live:
        # Redshift words the server-side kill as a user cancel), so the
        # SQLSTATE in the error payload is the signal, with "statement
        # timeout" as the fallback when no payload dict is present. A bare
        # cancel-word match would be wrong: WLM query-monitoring rules and
        # admin kills also say "canceled", and blaming those on the budget
        # sends the user to raise --budget for a refusal it cannot fix.
        payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        message = str(exc).lower()
        timed_out = payload.get("C") == "57014" or "statement timeout" in message
        if not timed_out:
            return exc
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

    def _consume_wake_floor(self) -> float:
        """The first billed statement of a command carries the 60-second wake
        charge on Serverless; later ones do not."""

        floor = self._wake_floor()
        self._wake_floor_pending = False
        return floor

    def _ensure_session(self) -> None:
        """Session preparation, once per command: a best-effort read-only mode
        and the attribution tag. Engine-built constants, not agent SQL. Both
        are tolerated when Redshift declines them (the SELECT-only guard and
        grants still enforce read-only; ``capabilities`` reports the truth).
        """

        if self._session_prepared:
            return
        self._session_prepared = True
        try:
            self._conn.cursor().execute("SET default_transaction_read_only = on")
            self._session_read_only = True
        except Exception:
            self._session_read_only = False
        with contextlib.suppress(Exception):
            self._conn.cursor().execute("SET query_group = 'dex'")

    @staticmethod
    def _label(description: Any) -> str:
        name = description[0]
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        return str(name)

    @classmethod
    def _description_type(cls, description: Any) -> str:
        type_code = description[1]
        if isinstance(type_code, int) and type_code in _OID_TYPE_NAMES:
            return _OID_TYPE_NAMES[type_code]
        return str(getattr(type_code, "name", type_code))

    # --- helpers ------------------------------------------------------------------

    def _catalog(self, sql: str) -> list[dict]:
        """Cheap metadata door: engine-built catalog SELECTs only (pg_catalog
        and SVV lookups plus session settings, no table scans). Results come
        back as dicts keyed by the column names."""

        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("only SELECT statements pass through the catalog door")
        self._ensure_session()
        cursor = self._conn.cursor()
        cursor.execute(sql)
        labels = [self._label(d) for d in cursor.description]
        return [dict(zip(labels, row, strict=True)) for row in cursor.fetchall()]

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
        # A three-part reference is valid Redshift only against the connected
        # database, which is exactly what the namespace guarantees.
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()


def _schema_filter(column: str) -> str:
    """The catalog predicate excluding system schemas, shared by every
    inventory query so the object census, the column census, and the visible
    set can never disagree about what a source schema is."""

    names = ", ".join(f"'{name}'" for name in _SYSTEM_SCHEMAS)
    return f"{column} NOT IN ({names}) AND {column} NOT LIKE 'pg\\_%'"


def _truthy(value: Any) -> bool:
    """Privilege predicates come back as bool from the driver but as 't'/'f'
    strings from some fakes and older wire paths; both spell the same fact."""

    if isinstance(value, str):
        return value.lower() in ("t", "true", "on", "1")
    return bool(value)


def _quote_ident(name: str) -> str:
    """Quote one identifier component with double quotes (preserving case,
    which unquoted Redshift identifiers would fold to lower), doubling
    embedded quotes."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_literal(value: str) -> str:
    return value.replace("'", "''")
