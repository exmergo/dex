"""The Databricks adapter: compute-time billed, Unity Catalog metadata.

Free metadata comes from the Unity Catalog REST API through the SDK
(catalogs, schemas, tables, columns): no warehouse is involved, so inventory,
``connect test``, and all schema facts cost nothing. The SQL session lives on
a warehouse and opening one can wake it, so the DBAPI connection is built
lazily by the first billed statement, never at adapter construction.

The estimate story is weaker than Snowflake's by platform necessity: there is
no dry-run and no free table size (the catalog API carries neither row counts
nor bytes), so a first estimate is a conservative per-statement floor plus a
startup floor when the pinned warehouse is not running, labeled honestly as
low quality. Once a run is confirmed, an engine-built ``DESCRIBE DETAIL``
(charged inside the confirmed budget, never before it) learns ``sizeInBytes``
and ``numRows``, which sharpens the estimates of subsequent statements,
drives the sampling decision, and feeds the per-table notes. The server-side
backstop is the session's ``STATEMENT_TIMEOUT`` wound down to the remaining
budget before each billed statement, so a weak estimate cannot overrun the
ceiling: Databricks kills the statement.

Read-only is enforced in depth: the SELECT-only guard in the databricks
dialect on every data statement through one execution door, an adapter that
issues no mutating statements (REST metadata reads, ``SET STATEMENT_TIMEOUT``,
``DESCRIBE DETAIL``, and guarded SELECTs only), and documented least-privilege
grants. Billed statements run only on the warehouse ``.dex/config.yml`` pins;
dex never spends on a warehouse the config does not name.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..config import DatabricksTarget
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
    is_blob_type,
    json_safe,
    name_list,
    shape_stat_expressions,
    shape_stat_value,
)

PARADIGM = "compute_time"
DIALECT = "databricks"

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 4 expressions per column).
_COLUMN_BATCH = 50

# With no free size metadata, an unrefined statement is estimated at this
# conservative floor. Deliberately generous for a running SQL warehouse so the
# quoted total errs high, never low.
_MIN_STATEMENT_SECONDS = 5.0

# One DESCRIBE DETAIL is a metadata-served statement: quick, but it still
# holds a moment of a running warehouse.
_DETAIL_SECONDS = 2.0

# Startup floors, charged once per command when the pinned warehouse is not
# RUNNING: serverless warehouses come up in seconds, classic clusters in
# minutes, and that time bills to whichever statement caused it.
_STARTUP_SERVERLESS_SECONDS = 10.0
_STARTUP_CLASSIC_SECONDS = 180.0

# The refined estimate heuristic once DESCRIBE DETAIL has produced a size: a
# deliberately conservative 2X-Small scan rate, scaled by the warehouse's DBU
# multiple (bigger warehouses scan proportionally faster).
_2XSMALL_SCAN_BYTES_PER_SECOND = 50 * 1024 * 1024

# DBUs per hour by SQL-warehouse size, keyed by the normalized cluster_size
# token. Published rates move and differ by warehouse type, so the translation
# is labeled approximate; seconds stay the binding number.
_DBU_PER_HOUR = {
    "2XSMALL": 4.0,
    "XSMALL": 6.0,
    "SMALL": 12.0,
    "MEDIUM": 24.0,
    "LARGE": 40.0,
    "XLARGE": 80.0,
    "2XLARGE": 144.0,
    "3XLARGE": 272.0,
    "4XLARGE": 528.0,
}
_2XSMALL_DBU_PER_HOUR = 4.0

# Nested, semi-structured, and spatial types: no distinct estimate, no
# min/max, only a non-null count (COUNT works on all of them; the others are
# invalid or meaningless). Matched against the upper-cased type_text.
_NESTED_TYPE_PREFIXES = (
    "ARRAY",
    "MAP",
    "STRUCT",
    "VARIANT",
    "OBJECT",
    "BINARY",
    "GEOGRAPHY",
    "GEOMETRY",
)

_ESTIMATE_QUALITY_NOTE = (
    "Databricks has no dry-run and no free table sizes: the estimate is a "
    "conservative floor (refined in-budget by DESCRIBE DETAIL once confirmed); "
    "the confirmed budget is still hard-enforced by a per-statement "
    "server-side STATEMENT_TIMEOUT"
)


def _regexp_predicate(qcol: str, pattern: str) -> str:
    # RLIKE matches substrings; the shared patterns' anchors make it a full
    # match.
    return f"{qcol} RLIKE '{pattern}'"


def warehouse_http_path(value: str) -> str:
    """The SQL driver's HTTP path for a pinned warehouse, which the config may
    name by ID or by full path."""

    trimmed = value.strip()
    if trimmed.startswith("/"):
        return trimmed
    return f"/sql/1.0/warehouses/{trimmed}"


def warehouse_id_of(value: str) -> str:
    """The bare warehouse ID for the REST API, from an ID or an HTTP path."""

    return value.strip().rstrip("/").rsplit("/", 1)[-1]


class DatabricksConnectionError(Exception):
    """Raised when the pinned warehouse (or a queried object) cannot be
    resolved. The message always names the fix, never a credential."""


class DatabricksAdapter:
    """Holds the SDK workspace client, the lazy SQL connection, and the cost
    gate for one command.

    ``workspace`` (the Unity Catalog metadata door) and ``sql_connect`` (a
    factory for the billed DBAPI connection) are injectable (class DI) so unit
    tests drive fakes; the real ones are built by ``connect.py`` from the
    discovered SDK config. Credentials live only inside this process and are
    never surfaced. ``clock`` is injectable so the fake can simulate statement
    duration; it is what actual billed seconds are measured with.
    """

    name = "databricks"
    dialect = DIALECT
    paradigm = Paradigm.COMPUTE_TIME

    def __init__(
        self,
        *,
        workspace: Any,
        sql_connect: Callable[[], Any],
        cost_gate: CostGate,
        target: DatabricksTarget | None = None,
        host: str | None = None,
        auth_method: str = "unknown",
        scope_origin: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._workspace = workspace
        self._sql_connect = sql_connect
        self.cost_gate = cost_gate
        self.target = target or DatabricksTarget()
        self.host = host
        self.auth_method = auth_method
        # What the scope entries in the target came from, so a refusal names the
        # thing the user has to go edit: a per-command flag or the committed
        # allowlist. `narrow_target` has already collapsed the two by the time
        # the adapter sees them, and the fix differs entirely.
        self._scope_origin = scope_origin or "databricks.catalogs in .dex/config.yml"
        self._clock = clock
        self._conn: Any = None
        self._objects: dict[str, dict] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._inventory_loaded = False
        self._resolved_scopes: list[tuple[str, str | None]] | None = None
        self._visible_catalogs: set[str] | None = None
        self._schemas_by_catalog: dict[str, set[str]] = {}
        self._warehouse_info: dict | None = None
        self._notes: dict[str, list[str]] = {}
        # The startup floor is charged once per command, by whichever billed
        # statement runs first.
        self._startup_floor_pending: bool | None = None
        # Identifiers DESCRIBE DETAIL has been attempted for (successfully or
        # not), so a refused or unaffordable probe is not retried per batch.
        self._detail_attempted: set[str] = set()
        self._cap_was_wall = False

    # --- capabilities (free) ---------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # Listing catalogs is the live probe: a REST metadata read that fails
        # on a stale or underprivileged credential, with no warehouse touched.
        catalogs = self._catalog_scope()
        caps: dict[str, object] = {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "paradigm": self.paradigm.value,
            "host": self.host,
            "auth_method": self.auth_method,
            "catalog_count": len(catalogs),
            "required_grants": [
                "CAN USE on the pinned SQL warehouse",
                "USE CATALOG + USE SCHEMA + SELECT on source catalogs",
            ],
        }
        cost = self.cost_gate.cost()
        budget: dict[str, object] = {
            "ceiling_seconds": cost.ceiling,
            "session_spent_today_seconds": self.cost_gate.session_spent,
        }
        if self.target.warehouse:
            info = self._warehouse()
            budget["warehouse"] = {
                "name": info["name"],
                "size": info["size"],
                "serverless": info["serverless"],
                "state": info["state"],
                "dbu_per_hour": info["dbu_per_hour"],
            }
            if cost.ceiling is not None:
                budget["ceiling_dbus"] = self._to_dbus(cost.ceiling)
        else:
            caps["warnings"] = [
                "no databricks.warehouse pinned in .dex/config.yml; metadata "
                "commands work, but nothing that scans will run until one is set"
            ]
        caps["budget"] = budget
        return caps

    # --- introspection (free Unity Catalog REST; no warehouse, no billing) -----

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
        entry = self._entry(identifier)
        return self._object_meta(entry), self._column_metas(entry["identifier"])

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the profiling run accumulated for one object
        (sampling degradation, skipped escalations, size-probe outcomes).
        Merged into the dataset's ``data_quality`` by the profile engine."""

        return list(self._notes.get(identifier, []))

    def _entry(self, identifier: str) -> dict:
        self._load_inventory()
        entry = self._objects.get(identifier) or self._objects.get(identifier.lower())
        if entry is None:
            raise DatabricksConnectionError(
                f"object '{identifier}' not found in the configured scope; "
                "check databricks.catalogs in .dex/config.yml"
            )
        return entry

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        for catalog, schema_filter in self._scopes():
            for schema in self._schema_names(catalog, schema_filter):
                for table in self._workspace.tables.list(
                    catalog_name=catalog, schema_name=schema, include_browse=True
                ):
                    self._register(table)
        self._inventory_loaded = True
        # Shared/browse-only catalogs omit columns from the list call; the
        # per-table GET (still free REST metadata) backfills them so ranking
        # sees real column counts instead of zeros.
        for identifier, entry in self._objects.items():
            if identifier not in self._columns:
                entry["column_count"] = len(self._column_metas(identifier))

    def _schema_names(self, catalog: str, schema_filter: str | None) -> list[str]:
        if schema_filter is not None:
            return [schema_filter]
        return sorted(self._schemas(catalog))

    def _register(self, table: Any) -> None:
        identifier = str(table.full_name)
        table_type = str(getattr(table, "table_type", "") or "")
        columns = self._column_metas_from(getattr(table, "columns", None))
        if columns is not None:
            self._columns[identifier] = columns
        self._objects[identifier] = {
            "identifier": identifier,
            "object_type": "view" if "VIEW" in table_type.upper() else "table",
            "schema": str(table.schema_name),
            "name": str(table.name),
            # The catalog API has no free row or byte counts; both stay None
            # until an in-budget DESCRIBE DETAIL learns them.
            "row_count": None,
            "byte_size": None,
            "column_count": len(columns) if columns is not None else 0,
        }

    @staticmethod
    def _column_metas_from(columns: Any) -> list[ColumnMeta] | None:
        """SDK ColumnInfo objects to ColumnMeta, or ``None`` when the listing
        omitted columns (shared/browse-only catalogs do), so the per-table GET
        fallback knows to fire."""

        if not columns:
            return None
        metas = []
        for ordinal, column in enumerate(columns):
            position = getattr(column, "position", None)
            metas.append(
                ColumnMeta(
                    name=str(column.name),
                    data_type=str(
                        getattr(column, "type_text", None)
                        or getattr(column, "type_name", "")
                        or ""
                    ),
                    nullable=bool(
                        getattr(column, "nullable", True)
                        if getattr(column, "nullable", None) is not None
                        else True
                    ),
                    ordinal=int(position) if position is not None else ordinal,
                )
            )
        return metas

    def _column_metas(self, identifier: str) -> list[ColumnMeta]:
        columns = self._columns.get(identifier)
        if columns is None:
            # Shared catalogs (e.g. samples) omit columns from the list call;
            # the per-table GET carries them. Still free REST metadata.
            table = self._workspace.tables.get(identifier, include_browse=True)
            columns = self._column_metas_from(getattr(table, "columns", None)) or []
            self._columns[identifier] = columns
            if identifier in self._objects:
                self._objects[identifier]["column_count"] = len(columns)
        return columns

    @staticmethod
    def _object_meta(entry: dict) -> ObjectMeta:
        return ObjectMeta(
            identifier=entry["identifier"],
            object_type=entry["object_type"],
            schema=entry["schema"],
            name=entry["name"],
            row_count=entry["row_count"],
            byte_size=entry["byte_size"],
            column_count=entry["column_count"],
        )

    def _scopes(self) -> list[tuple[str, str | None]]:
        """Every source scope this command reads, as ``(catalog, schema-or-None)``
        pairs, resolved and proven to exist.

        Resolution is free (Unity Catalog REST, no warehouse) and cached for the
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
            # Nothing committed: every catalog the principal can see is the
            # allowlist by definition, so there is nothing to prove.
            return [
                (catalog, None)
                for catalog in sorted(self._catalogs())
                if catalog != "system"
            ]
        with blame(self._scope_origin, DatabricksConnectionError):
            return [
                self._resolve_scope(entry)
                for entry in sorted({e.lower() for e in self.target.catalogs})
            ]

    def _resolve_scope(self, entry: str) -> tuple[str, str | None]:
        """One scope entry, proven to exist. Unity Catalog names are always
        rooted in a catalog, so unlike Snowflake there is no bare schema to
        qualify and no ambiguity to resolve: an entry either names something or
        it does not."""

        token = entry.strip().lower()
        if not token:
            raise DatabricksConnectionError("empty scope entry")
        catalog, _, schema = token.partition(".")
        if "." in schema:
            raise DatabricksConnectionError(
                f"scope '{entry}' has too many parts; a source scope is "
                "<catalog> or <catalog>.<schema>, never a table"
            )
        visible = self._catalogs()
        if catalog not in {name.lower() for name in visible}:
            raise DatabricksConnectionError(
                f"scope '{entry}' names no catalog this principal can see; "
                f"visible catalogs: {name_list(sorted(visible))}"
            )
        if not schema:
            return (catalog, None)
        schemas = self._schemas(catalog)
        if schema not in {name.lower() for name in schemas}:
            raise DatabricksConnectionError(
                f"scope '{entry}' does not exist: catalog {catalog} has no "
                f"schema {schema}; schemas there: {name_list(sorted(schemas))}"
            )
        return (catalog, schema)

    def _catalogs(self) -> set[str]:
        """Every catalog the principal can see, as Unity Catalog spells them. Free
        (REST, no warehouse), cached, and the live credential probe: the listing
        fails on a stale or underprivileged credential."""

        if self._visible_catalogs is None:
            self._visible_catalogs = {
                str(catalog.name) for catalog in self._workspace.catalogs.list()
            }
        return self._visible_catalogs

    def _schemas(self, catalog: str) -> set[str]:
        """Every schema in one catalog, as Unity Catalog spells them. Free (REST),
        cached per catalog. ``information_schema`` is never a source."""

        if catalog not in self._schemas_by_catalog:
            self._schemas_by_catalog[catalog] = {
                str(schema.name)
                for schema in self._workspace.schemas.list(catalog_name=catalog)
                if str(schema.name) != "information_schema"
            }
        return self._schemas_by_catalog[catalog]

    def _catalog_scope(self) -> list[str]:
        """Distinct catalogs across the resolved scopes."""

        return sorted({catalog for catalog, _schema in self._scopes()})

    def missing_dev_namespaces(self, catalog: str) -> list[str]:
        """Which parts of a dbt dev target do not exist yet. Free: REST only, so
        the billed SQL session is never opened.

        dbt creates schemas but never catalogs, so ``dev_schema`` is deliberately
        not checked: its absence is normal on a first build. A missing catalog is
        fatal, and left to dbt it surfaces from deep inside the first build as a
        ``CREATE SCHEMA`` failure naming neither the catalog nor the fix.
        """

        visible = {name.lower() for name in self._catalogs()}
        return [] if catalog.lower() in visible else [f'dev_catalog "{catalog}"']

    def list_namespace_objects(self, catalog: str, schema: str) -> list[str]:
        """Table and view names already in one schema. Free: Unity Catalog REST
        only, so the billed SQL warehouse is never woken. A catalog or schema
        that does not exist holds nothing to collide with, so it reads as empty.

        Names resolve to Unity Catalog's own spelling first (identifiers are
        case-insensitive but the REST list calls are not).
        """

        actual_catalog = next(
            (name for name in self._catalogs() if name.lower() == catalog.lower()),
            None,
        )
        if actual_catalog is None:
            return []
        actual_schema = next(
            (
                name
                for name in self._schemas(actual_catalog)
                if name.lower() == schema.lower()
            ),
            None,
        )
        if actual_schema is None:
            return []
        return sorted(
            str(table.name)
            for table in self._workspace.tables.list(
                catalog_name=actual_catalog,
                schema_name=actual_schema,
                include_browse=True,
            )
        )

    def dev_write_grants(self, catalog: str, schema: str) -> list[str]:
        """The privileges the principal is missing to build into the dev namespace,
        as far as Unity Catalog can prove. Free: REST only.

        A dev catalog that exists but cannot be written is the other half of the
        preflight, and the one dbt reports worst: the first model dies with
        ``PERMISSION_DENIED: User does not have CREATE TABLE and USE SCHEMA``,
        after the warehouse has already woken and the budget has already been
        spent.

        Reported, never raised, because Unity Catalog cannot prove the negative.
        Ownership does not appear in the effective-privilege API (a catalog owner
        reads back as holding nothing at all), and a metastore admin bypasses
        grants entirely, so an empty answer is not evidence of no access. Refusing
        on it would break builds dbt could run, which this preflight must never do.
        Ownership is therefore checked first, and what is left is a warning.
        """

        principal = str(self._workspace.current_user.me().user_name)
        owns_catalog = self._owns(self._workspace.catalogs.get(catalog), principal)
        catalog_privileges = (
            set()
            if owns_catalog
            else self._effective_privileges("CATALOG", catalog, principal)
        )

        qualified = f"{catalog}.{schema}"
        if schema.lower() not in {name.lower() for name in self._schemas(catalog)}:
            # The schema is not there yet, and dbt creates it: what that needs is
            # the right to create inside the catalog, which its owner always has.
            if owns_catalog:
                return []
            return [
                f"{privilege} ON CATALOG {catalog}"
                for privilege in ("USE CATALOG", "CREATE SCHEMA")
                if privilege not in catalog_privileges
            ]

        # The schema exists, and owning the catalog is not enough to write inside a
        # schema someone else owns: Unity Catalog answers that with the very
        # PERMISSION_DENIED this check exists to predict.
        if self._owns(self._workspace.schemas.get(qualified), principal):
            return []
        held = self._effective_privileges("SCHEMA", qualified, principal)
        missing = [
            f"{privilege} ON SCHEMA {qualified}"
            for privilege in ("USE SCHEMA", "CREATE TABLE")
            if privilege not in held
        ]
        if missing and not owns_catalog and "USE CATALOG" not in catalog_privileges:
            missing.insert(0, f"USE CATALOG ON CATALOG {catalog}")
        return missing

    @staticmethod
    def _owns(securable: Any, principal: str) -> bool:
        return str(getattr(securable, "owner", "") or "").lower() == principal.lower()

    def _effective_privileges(
        self, securable_type: str, full_name: str, principal: str
    ) -> set[str]:
        """Everything ``principal`` effectively holds on one securable, including
        what it inherits through its groups. Free (REST). An error here is not a
        refusal: it degrades to "nothing proven", and the caller only warns."""

        try:
            effective = self._workspace.grants.get_effective(
                securable_type=securable_type, full_name=full_name, principal=principal
            )
        except Exception:  # a grant we cannot read is not a grant we can deny on
            return set()
        return {
            str(privilege.privilege)
            for assignment in (effective.privilege_assignments or [])
            for privilege in (assignment.privileges or [])
        }

    # --- the warehouse and the DBU translation ----------------------------------

    def _warehouse(self) -> dict:
        """The pinned warehouse's facts (size, state, serverless, DBU rate),
        fetched free via the REST API and cached per command. Refuses when the
        config pins nothing or the pin does not resolve: dex never spends on a
        warehouse the config does not name."""

        if self._warehouse_info is not None:
            return self._warehouse_info
        pinned = self.target.warehouse
        if not pinned:
            raise DatabricksConnectionError(
                "no warehouse pinned: set databricks.warehouse in "
                ".dex/config.yml (dex never spends on an unpinned SQL "
                "warehouse)"
            )
        try:
            info = self._workspace.warehouses.get(warehouse_id_of(pinned))
        except Exception as exc:
            raise DatabricksConnectionError(
                f"warehouse '{pinned}' not found or not granted; check the ID, "
                "or grant CAN USE to this principal, or pin a different "
                "databricks.warehouse in .dex/config.yml"
            ) from exc
        size_token = (
            str(getattr(info, "cluster_size", "") or "")
            .upper()
            .replace("-", "")
            .replace(" ", "")
        )
        state = str(getattr(info, "state", "") or "unknown")
        self._warehouse_info = {
            "id": warehouse_id_of(pinned),
            "name": str(getattr(info, "name", pinned)),
            "size": str(getattr(info, "cluster_size", "unknown")),
            "state": state.rsplit(".", 1)[-1].upper(),
            "serverless": bool(getattr(info, "enable_serverless_compute", False)),
            "dbu_per_hour": _DBU_PER_HOUR.get(size_token),
        }
        return self._warehouse_info

    def _to_dbus(self, seconds: float) -> float | None:
        info = self._warehouse_info or (
            self._warehouse() if self.target.warehouse else None
        )
        if not info or info.get("dbu_per_hour") is None:
            return None
        return round(seconds * info["dbu_per_hour"] / 3600.0, 6)

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The compute-time handshake payload: seconds are the binding number,
        DBUs (and dollars when the price is configured) ride alongside as the
        familiar translation."""

        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "low",
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in warehouse-seconds; the same number "
                "becomes the server-side statement timeout)"
            ),
            "notes": [_ESTIMATE_QUALITY_NOTE],
        }
        if per_table:
            data["per_table_seconds"] = per_table
        dbus = self._to_dbus(estimate)
        if dbus is not None:
            info = self._warehouse_info or {}
            data["estimated_dbus"] = dbus
            data["dbu_rate"] = {
                "warehouse": info.get("name"),
                "size": info.get("size"),
                "serverless": info.get("serverless"),
                "dbu_per_hour": info.get("dbu_per_hour"),
                "approximate": True,
            }
            if self.target.dbu_price_usd is not None:
                data["estimated_usd"] = round(dbus * self.target.dbu_price_usd, 4)
        return data

    def spend_display(self) -> dict:
        """DBU translation of actual seconds, merged into ``data.spend``."""

        summary: dict[str, object] = {}
        dbus = self._to_dbus(self.cost_gate.spend_summary()["seconds_billed"])
        if dbus is not None:
            summary["dbus_billed"] = dbus
            if self.target.dbu_price_usd is not None:
                summary["usd_billed"] = round(dbus * self.target.dbu_price_usd, 4)
        return summary

    # --- estimation (free; feeds the confirm handshake) -------------------------

    def profile_estimate(
        self, identifiers: list[str], *, include_blobs: set[str] | None = None
    ) -> tuple[float, dict[str, float]]:
        """The floor estimate for profiling: per table, its aggregate-batch
        count times the per-statement floor (sharpened by any size already
        learned this command), plus one DESCRIBE DETAIL per table, plus the
        startup floor when the warehouse is not running. Free: everything
        comes from REST metadata.

        Blob-type columns are excluded from the batch count the same way
        ``explore.profile.profile`` excludes them from the scan itself
        (``include_blobs`` names the ``identifier.column`` paths a human
        opted back in), so this estimate's batch count matches what the run
        will actually issue."""

        blob_paths = include_blobs or set()
        per_table: dict[str, float] = {}
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            scan_columns = [
                c
                for c in columns
                if not is_blob_type(c.data_type)
                or f"{identifier}.{c.name}".lower() in blob_paths
            ]
            batches = max((len(scan_columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = (
                batches * self._statement_seconds(meta.byte_size) + _DETAIL_SECONDS
            )
        total = sum(per_table.values()) + self._startup_floor()
        return total, per_table

    def query_estimate(self, sql: str) -> float:
        """The floor estimate for one firewall-approved query: the summed
        known sizes of every referenced table over the scan rate (the floor
        where unknown), plus the startup floor when the warehouse is not
        running."""

        checked = assert_select_only(sql, dialect=self.dialect)
        return self._statement_estimate(checked) + self._startup_floor()

    def _statement_estimate(self, sql: str) -> float:
        total_bytes = 0
        known = 0
        self._load_inventory()
        for identifier in self._referenced_tables(sql):
            entry = self._objects.get(identifier.lower())
            if entry and entry["byte_size"] is not None:
                total_bytes += entry["byte_size"]
                known += 1
        if known == 0:
            return _MIN_STATEMENT_SECONDS
        return self._statement_seconds(total_bytes)

    def _statement_seconds(self, byte_size: int | None) -> float:
        if not byte_size:
            return _MIN_STATEMENT_SECONDS
        # Bigger warehouses scan proportionally faster; the size multiple is
        # the DBU rate over the 2X-Small baseline.
        size_factor = 1.0
        info = self._warehouse_info
        if info and info.get("dbu_per_hour"):
            size_factor = info["dbu_per_hour"] / _2XSMALL_DBU_PER_HOUR
        rate = _2XSMALL_SCAN_BYTES_PER_SECOND * max(size_factor, 1.0)
        return max(byte_size / rate, 1.0)

    def _startup_floor(self) -> float:
        """The wake charge once when the pinned warehouse is not running (a
        serverless warehouse comes up in seconds, a classic one in minutes),
        else 0. Cached: one command wakes at most once."""

        if self._startup_floor_pending is None:
            if not self.target.warehouse:
                self._startup_floor_pending = False
            else:
                self._startup_floor_pending = self._warehouse()["state"] != "RUNNING"
        if not self._startup_floor_pending:
            return 0.0
        if self._warehouse()["serverless"]:
            return _STARTUP_SERVERLESS_SECONDS
        return _STARTUP_CLASSIC_SECONDS

    def _referenced_tables(self, sql: str) -> set[str]:
        try:
            import sqlglot
            from sqlglot import expressions as sqlglot_exp

            parsed = sqlglot.parse_one(sql, read=self.dialect)
        except Exception:
            return set()
        return {
            ".".join(part for part in (t.catalog, t.db, t.name) if part)
            for t in parsed.find_all(sqlglot_exp.Table)
        }

    # --- in-budget size refinement ----------------------------------------------

    def _ensure_detail(self, identifier: str) -> None:
        """Learn one table's size and row count via DESCRIBE DETAIL, spent
        only within the already-confirmed budget: when the remaining budget
        cannot cover the probe (or the table's format does not support it,
        e.g. some shared tables), keep the floor estimates and note why.
        Attempted at most once per table per command."""

        entry = self._entry(identifier)
        if entry["byte_size"] is not None or identifier in self._detail_attempted:
            return
        self._detail_attempted.add(identifier)
        # The wake charge stays pending if the probe cannot be afforded, so
        # the first statement that does run still carries it.
        if not self.cost_gate.try_charge(_DETAIL_SECONDS + self._startup_floor()):
            self._note(
                identifier,
                "size probe skipped: the remaining budget could not cover "
                "DESCRIBE DETAIL; estimates for this table stay at the floor",
            )
            return
        self._startup_floor_pending = False
        try:
            rows, labels = self._run(f"DESCRIBE DETAIL {self._quote(identifier)}")
        except OverCeilingError:
            raise
        except Exception:
            self._note(
                identifier,
                "size probe unavailable (DESCRIBE DETAIL was refused for this "
                "table); estimates stay at the floor and sampling is not applied",
            )
            return
        if not rows:
            return
        values = dict(zip(labels, rows[0], strict=True))
        size = values.get("sizeinbytes")
        count = values.get("numrows")
        if size is not None:
            entry["byte_size"] = int(size)
        if count is not None:
            entry["row_count"] = int(count)

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
        self._ensure_detail(identifier)
        meta, _ = self.table_metadata(identifier)
        sample_percent = self._sample_percent(identifier, meta.byte_size)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(
                identifier, batch, safe, shape, sample_percent=sample_percent
            )
            rows, labels = self._execute(
                sql, estimate=self._statement_seconds(meta.byte_size)
            )
            values = dict(zip(labels, rows[0], strict=True))
            # DESCRIBE DETAIL's numRows is often null (shared tables), but the
            # aggregate batch just counted exactly; capture it so the engine's
            # near-unique escalation and grain verdicts have a real row count.
            if sample_percent is None:
                self._entry(identifier)["row_count"] = int(values["n_total"])
            results.extend(
                self._read_aggregates(values, plan, sampled=sample_percent is not None)
            )
        return results

    def _sample_percent(self, identifier: str, byte_size: int | None) -> float | None:
        threshold = self.target.max_full_profile_bytes
        if threshold is None or not byte_size or byte_size <= threshold:
            return None
        percent = max(round(100.0 * threshold / byte_size, 2), 0.01)
        self._note(
            identifier,
            f"profiled from a ~{percent}% sample (table exceeds "
            "databricks.max_full_profile_bytes); counts and extremes are "
            "approximate and uniqueness is not judged",
        )
        return percent

    def _build_aggregate_sql(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        shape: set[str],
        *,
        sample_percent: float | None = None,
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool]]]:
        # One aggregate statement per batch: COUNT(*) once, then per column a
        # non-null count, an approximate distinct, min/max only where allowed,
        # and value-shape fractions only where requested. Pure (no
        # connection), so the SELECT-only property is testable offline. Nested
        # and semi-structured columns get the non-null count only
        # (approx_count_distinct and MIN/MAX are invalid or meaningless on
        # them).
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            nested = self._is_nested(col.data_type)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not nested
            if wants_distinct:
                select_parts.append(f"APPROX_COUNT_DISTINCT({qcol}) AS nd_{i}")
            wants_min_max = (col.name in safe) and not nested
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            wants_shape = (col.name in shape) and not nested
            if wants_shape:
                select_parts.extend(shape_stat_expressions(qcol, i, _regexp_predicate))
            plan.append((i, col, wants_distinct, wants_min_max, wants_shape))
        source = self._quote(identifier)
        if sample_percent is not None:
            source += f" TABLESAMPLE ({sample_percent} PERCENT)"
        # Interpolated parts are quoted identifiers and fixed aggregate
        # keywords, never values; the result is guarded as a read-only SELECT.
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"  # noqa: S608
        return assert_select_only(sql, dialect=self.dialect), plan

    @staticmethod
    def _is_nested(data_type: str) -> bool:
        return data_type.upper().startswith(_NESTED_TYPE_PREFIXES)

    @staticmethod
    def _read_aggregates(
        values: dict,
        plan: list[tuple[int, ColumnMeta, bool, bool, bool]],
        *,
        sampled: bool,
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
            # Under sampling, counts describe the sample, so a uniqueness
            # verdict would be unfounded either way.
            is_unique = (
                (distinct == int(nn) == n_total and n_total > 0)
                if distinct is not None and nn is not None and not sampled
                else None
            )
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=is_unique,
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
        self._ensure_detail(identifier)
        meta, _ = self.table_metadata(identifier)
        estimate = self._statement_seconds(meta.byte_size)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "distinct-count escalation skipped: the remaining budget could "
                "not cover the extra scan; uniqueness verdicts stay approximate",
            )
            return {}
        select_parts = [
            f"COUNT(DISTINCT {_quote_ident(name)}) AS d_{i}"
            for i, name in enumerate(columns)
        ]
        sql = assert_select_only(
            f"SELECT {', '.join(select_parts)} FROM {self._quote(identifier)}",  # noqa: S608
            dialect=self.dialect,
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
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
        self._ensure_detail(identifier)
        meta, _ = self.table_metadata(identifier)
        estimate = self._statement_seconds(meta.byte_size) * len(combinations)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "composite-key probe skipped: the remaining budget could not "
                "cover the extra scan; grain stays unknown",
            )
            return {}
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
        """Execute one firewall-approved SELECT, bounded in rows and seconds:
        the client preflight charges the estimate, and the server-side
        statement timeout is the tighter of the remaining budget and the
        per-query wall limit."""

        assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(
            self._statement_estimate(sql) + self._consume_startup_floor()
        )
        cursor = self._billed_cursor(cap_seconds=timeout_seconds)
        started = self._clock()
        try:
            cursor.execute(sql)
            rows = cursor.fetchmany(max_rows + 1)
        except Exception as exc:
            self._record_elapsed(started, cursor, sql)
            raise self._translate(exc, timeout_seconds) from exc
        self._record_elapsed(started, cursor, sql)
        labels = [d[0].lower() for d in cursor.description]
        types = [str(d[1]) for d in cursor.description]
        return QueryResult(
            columns=labels,
            types=types,
            cells=[[json_safe(v) for v in row] for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )

    def _execute(self, sql: str, *, estimate: float) -> tuple[list, list[str]]:
        """SELECT-only guard, floor/refined charge, then the timeout-capped run."""

        assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(estimate + self._consume_startup_floor())
        return self._run(sql)

    def _run(self, sql: str) -> tuple[list, list[str]]:
        """The single billed door past the gate: statement timeout set to the
        remaining budget, wall-clock elapsed recorded to the ledger."""

        cursor = self._billed_cursor()
        started = self._clock()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except Exception as exc:
            self._record_elapsed(started, cursor, sql)
            raise self._translate(exc, None) from exc
        self._record_elapsed(started, cursor, sql)
        labels = [d[0].lower() for d in cursor.description]
        return rows, labels

    def _billed_cursor(self, cap_seconds: float | None = None):
        """A cursor prepared for spend: warehouse pinned (strict), the SQL
        session opened lazily on first use, and the server-side statement
        timeout wound down to what remains of the budget (or the per-query
        wall limit, whichever is tighter), so even a wrong floor estimate
        cannot overrun the ceiling."""

        self._warehouse()  # refuses when config pins no warehouse
        remaining = self.cost_gate.remaining_for_statement()
        if remaining is not None and remaining < 1:
            raise OverCeilingError(
                "the remaining budget is under one warehouse-second; raise "
                "--budget or narrow the work"
            )
        bounds = [b for b in (remaining, cap_seconds) if b is not None]
        # Remember which bound won so a timeout kill is reported as the wall
        # limit or the budget, whichever actually fired.
        self._cap_was_wall = cap_seconds is not None and (
            remaining is None or cap_seconds < remaining
        )
        if self._conn is None:
            self._conn = self._sql_connect()
        cursor = self._conn.cursor()
        if bounds:
            # An engine-built session statement, not agent SQL. STATEMENT_TIMEOUT
            # treats 0 as "no timeout", so the cap never rounds below 1.
            cursor.execute(f"SET STATEMENT_TIMEOUT = {max(int(min(bounds)), 1)}")
        return cursor

    def _record_elapsed(self, started: float, cursor: Any, sql: str) -> None:
        elapsed = max(self._clock() - started, 0.0)
        self.cost_gate.record_billed(
            elapsed,
            job_id=getattr(cursor, "query_id", None),
            statement=sql,
        )

    def _consume_startup_floor(self) -> float:
        """The first billed statement of a command carries the warehouse wake
        charge when it was not running; later ones do not."""

        floor = self._startup_floor()
        self._startup_floor_pending = False
        return floor

    def _translate(self, exc: Exception, timeout_seconds: float | None) -> Exception:
        message = str(exc).lower()
        if "timeout" in message or "timed out" in message:
            if timeout_seconds is not None and self._cap_was_wall:
                return TimeoutError(
                    f"query exceeded {timeout_seconds:g}s and was cancelled; "
                    "narrow it (tighter filter, fewer columns) and retry"
                )
            return OverCeilingError(
                "the statement hit the server-side timeout derived from the "
                "remaining budget (STATEMENT_TIMEOUT); raise --budget or "
                "narrow the work"
            )
        return exc

    # --- helpers ------------------------------------------------------------------

    def _note(self, identifier: str, note: str) -> None:
        notes = self._notes.setdefault(identifier, [])
        if note not in notes:
            notes.append(note)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(f"expected catalog.schema.table, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        if self._conn is not None:
            close = getattr(self._conn, "close", None)
            if close is not None:
                close()
            self._conn = None


def _quote_ident(name: str) -> str:
    """Quote one identifier component with backticks (the Databricks SQL
    delimited-identifier form), doubling embedded backticks."""

    escaped = name.replace("`", "``")
    return f"`{escaped}`"
