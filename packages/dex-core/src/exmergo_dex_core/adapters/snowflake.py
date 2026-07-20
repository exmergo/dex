"""The Snowflake adapter: the first compute-time billed connector.

The cost inversion from BigQuery: metadata is cheap (SHOW commands run on the
cloud-services layer with no warehouse), while any data scan costs warehouse
runtime. So inventory, ``connect test``, and all schema facts stay free, and
the guarded quantity is warehouse-seconds, not bytes.

Snowflake has no dry-run, so estimates are a documented heuristic (table bytes
over a conservative per-size scan rate, floored at the 60-second resume
minimum when the warehouse is suspended) and every envelope that carries one
says so. The server-side backstop is a per-statement
``STATEMENT_TIMEOUT_IN_SECONDS`` set to the remaining budget before each billed
statement, so a wrong estimate cannot overrun the ceiling: Snowflake kills the
statement. Actual spend is wall-clock seconds per statement (which includes
any resume time this statement caused, the honest attribution), recorded to
the ledger as ``billed_seconds``.

Read-only is enforced in depth: the SELECT-only guard in the snowflake dialect
on every data statement through one execution door, an adapter that issues no
mutating statements (SHOW / SELECT / session parameters only), and the
documented read-only role grants. Billed statements run only on the warehouse
``.dex/config.yml`` pins; a connection-level default warehouse is never
spent on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ..config import SnowflakeTarget
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
    scope_within,
    shape_stat_expressions,
    shape_stat_value,
)

PARADIGM = "compute_time"
DIALECT = "snowflake"

# Snowflake keeps a read-only INFORMATION_SCHEMA in every database and an
# account-internal SNOWFLAKE database. Neither is ever a source, and neither may
# be reached by a scope entry.
_RESERVED_SCHEMA = "INFORMATION_SCHEMA"
_RESERVED_DATABASE = "SNOWFLAKE"

# How many databases a bare schema name may be searched across before dex asks
# for a qualified <database>.<schema> instead. Each candidate costs one free SHOW
# SCHEMAS round-trip, and an unscoped enterprise account has hundreds.
_BARE_SCOPE_SEARCH_LIMIT = 20

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 4 expressions per column).
_COLUMN_BATCH = 50

# Snowflake bills at least 60 seconds each time a suspended warehouse resumes,
# regardless of query duration. Estimates against a suspended warehouse carry
# this floor once so the quoted number is what the account will actually see.
_RESUME_MINIMUM_SECONDS = 60.0

# The estimate heuristic: a deliberately conservative X-Small scan rate.
# Larger sizes scale it by their credit multiple (they bill proportionally
# more per second but scan proportionally faster, so estimated *seconds*
# shrink while estimated *credits* stay comparable).
_XSMALL_SCAN_BYTES_PER_SECOND = 50 * 1024 * 1024

# Every billed statement estimates at least this much: even a metadata-served
# result costs a moment of a running warehouse.
_MIN_STATEMENT_SECONDS = 1.0

# Credits per hour by warehouse size (Gen1 standard). Keyed by the normalized
# size token from SHOW WAREHOUSES. Gen2 warehouses bill a multiplier on top
# (1.35 on AWS/GCP, 1.25 on Azure); the cloud is not cheaply knowable, so the
# conservative 1.35 is applied and the translation is labeled approximate.
_CREDITS_PER_HOUR = {
    "XSMALL": 1.0,
    "SMALL": 2.0,
    "MEDIUM": 4.0,
    "LARGE": 8.0,
    "XLARGE": 16.0,
    "2XLARGE": 32.0,
    "3XLARGE": 64.0,
    "4XLARGE": 128.0,
    "5XLARGE": 256.0,
    "6XLARGE": 512.0,
}
_GEN2_MULTIPLIER = 1.35

# Semi-structured and spatial types: no distinct estimate, no min/max, only a
# non-null count (COUNT works on all of them; the others do not).
_NESTED_TYPE_PREFIXES = (
    "VARIANT",
    "OBJECT",
    "ARRAY",
    "GEOGRAPHY",
    "GEOMETRY",
    "VECTOR",
)

_ESTIMATE_QUALITY_NOTE = (
    "Snowflake has no dry-run: the estimate is a heuristic (table bytes over a "
    "conservative scan rate); the confirmed budget is still hard-enforced by a "
    "per-statement server-side timeout"
)


def _regexp_predicate(qcol: str, pattern: str) -> str:
    return f"RLIKE({qcol}, '{pattern}')"


class SnowflakeConnectionError(Exception):
    """Raised when the pinned warehouse (or a queried object) cannot be
    resolved. The message always names the fix, never a credential."""


class SnowflakeAdapter:
    """Holds one Snowflake connection plus the cost gate for one command.

    ``connection`` is injectable (class DI) so unit tests drive a fake; the
    real connection is built by ``connect.py`` from discovered parameters.
    Credentials live only inside this process and are never surfaced.
    ``clock`` is injectable so the fake can simulate statement duration; it is
    what actual billed seconds are measured with.
    """

    name = "snowflake"
    dialect = DIALECT
    paradigm = Paradigm.COMPUTE_TIME

    def __init__(
        self,
        *,
        connection: Any,
        cost_gate: CostGate,
        target: SnowflakeTarget | None = None,
        account: str | None = None,
        auth_method: str = "unknown",
        scope_override: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._conn = connection
        self.cost_gate = cost_gate
        self.target = target or SnowflakeTarget()
        self.account = account
        self.auth_method = auth_method
        # A per-command `--scope`, kept apart from the committed allowlist in the
        # target: it may only narrow that allowlist, and deciding whether it does
        # requires resolving bare schema names against the account first.
        self._scope_override = list(scope_override or [])
        self._clock = clock
        # Imported lazily (the caller constructed the connection, so the
        # library is present); the error types drive refusal translation.
        from snowflake.connector import errors as sf_errors

        self._sf_errors = sf_errors
        # SHOW results are cached per command: the estimate pass and the
        # confirmed run share table facts, and each SHOW is free but a
        # round-trip.
        self._objects: dict[str, dict] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._inventory_loaded = False
        self._resolved_scopes: list[str] | None = None
        self._visible_databases: set[str] | None = None
        self._schemas_by_database: dict[str, set[str]] = {}
        self._warehouse_info: dict | None = None
        self._notes: dict[str, list[str]] = {}
        # The 60s resume minimum is charged once per command, by whichever
        # billed statement runs first.
        self._resume_floor_pending: bool | None = None
        self._session_prepared = False

    # --- capabilities (free) ---------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # SHOW DATABASES is the live probe: cloud-services layer, no warehouse,
        # and it fails on a stale or underprivileged credential.
        databases = self._database_scope()
        caps: dict[str, object] = {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "paradigm": self.paradigm.value,
            "account": self.account,
            "auth_method": self.auth_method,
            "database_count": len(databases),
            "required_grants": [
                "USAGE on the pinned warehouse",
                "USAGE + SELECT (or IMPORTED PRIVILEGES) on source databases",
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
                "credits_per_hour": info["credits_per_hour"],
                "state": info["state"],
            }
            if cost.ceiling is not None:
                budget["ceiling_credits"] = self._to_credits(cost.ceiling)
        else:
            caps["warnings"] = [
                "no snowflake.warehouse pinned in .dex/config.yml; metadata "
                "commands work, but nothing that scans will run until one is set"
            ]
        caps["budget"] = budget
        return caps

    # --- introspection (free SHOW metadata; no warehouse, no billing) ----------

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
        entry = self._objects.get(identifier) or self._objects.get(identifier.upper())
        if entry is None:
            raise SnowflakeConnectionError(
                f"object '{identifier}' not found in the configured scope; "
                "check snowflake.databases in .dex/config.yml"
            )
        return self._object_meta(entry), self._column_metas(entry["identifier"])

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the profiling run accumulated for one object
        (sampling degradation, skipped escalations). Merged into the dataset's
        ``data_quality`` by the profile engine."""

        return list(self._notes.get(identifier, []))

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        for scope in self._scopes():
            in_clause = self._scope_clause(scope)
            for row in self._show(f"SHOW TABLES {in_clause}"):
                self._register(row, "table")
            for row in self._show(f"SHOW VIEWS {in_clause}"):
                self._register(row, "view")
            # One SHOW COLUMNS per scope (not per table) keeps inventory a
            # handful of round-trips. SHOW output is capped at 10K rows, which
            # a db.schema allowlist keeps comfortably distant.
            by_table: dict[str, list[ColumnMeta]] = {}
            for row in self._show(f"SHOW COLUMNS {in_clause}"):
                identifier = self._identifier_of(row)
                columns = by_table.setdefault(identifier, [])
                columns.append(
                    ColumnMeta(
                        name=str(row["column_name"]),
                        data_type=self._render_type(str(row["data_type"])),
                        nullable=self._column_nullable(str(row["data_type"])),
                        ordinal=len(columns),
                    )
                )
            self._columns.update(by_table)
        for identifier, columns in self._columns.items():
            if identifier in self._objects:
                self._objects[identifier]["column_count"] = len(columns)
        self._inventory_loaded = True

    def _register(self, row: dict, object_type: str) -> None:
        identifier = self._identifier_of(row)
        # SHOW TABLES row/byte counts are metadata-maintained and free; views
        # have no stored rows (their count arrives via profiling).
        rows = row.get("rows") if object_type == "table" else None
        size = row.get("bytes") if object_type == "table" else None
        self._objects[identifier] = {
            "identifier": identifier,
            "object_type": object_type,
            "schema": str(row["schema_name"]),
            "name": str(row["name"]),
            "row_count": int(rows) if rows is not None else None,
            "byte_size": int(size) if size is not None else None,
            "column_count": 0,
        }

    @staticmethod
    def _identifier_of(row: dict) -> str:
        name = row.get("table_name") or row.get("name")
        return f"{row['database_name']}.{row['schema_name']}.{name}"

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

    def _column_metas(self, identifier: str) -> list[ColumnMeta]:
        columns = self._columns.get(identifier)
        if columns is None:
            columns = []
            rows = self._show(f"SHOW COLUMNS IN TABLE {self._quote(identifier)}")
            for ordinal, row in enumerate(rows):
                columns.append(
                    ColumnMeta(
                        name=str(row["column_name"]),
                        data_type=self._render_type(str(row["data_type"])),
                        nullable=self._column_nullable(str(row["data_type"])),
                        ordinal=ordinal,
                    )
                )
            self._columns[identifier] = columns
        return columns

    @staticmethod
    def _render_type(data_type_json: str) -> str:
        """SHOW COLUMNS carries the type as a JSON document; surface its type
        token (TEXT, FIXED, VARIANT, ...) rather than the raw JSON."""

        try:
            parsed = json.loads(data_type_json)
            return str(parsed.get("type", data_type_json))
        except (json.JSONDecodeError, AttributeError):
            return data_type_json

    @staticmethod
    def _column_nullable(data_type_json: str) -> bool:
        try:
            return bool(json.loads(data_type_json).get("nullable", True))
        except (json.JSONDecodeError, AttributeError):
            return True

    def _scopes(self) -> list[str]:
        """Every source scope this command reads, resolved and proven to exist.

        Resolution is free (SHOW only, no warehouse) and cached for the command.
        It runs before anything is estimated, because an unresolvable scope that
        silently falls back to the whole allowlist is a cost-safety bug: the
        estimate the user confirms would cover tables they never named.
        """

        if self._resolved_scopes is None:
            self._resolved_scopes = self._resolve_scopes()
        return self._resolved_scopes

    def _resolve_scopes(self) -> list[str]:
        committed = self.target.databases
        if committed:
            origin = "snowflake.databases in .dex/config.yml"
            with blame(origin, SnowflakeConnectionError):
                configured = sorted(
                    {
                        self._resolve_scope(entry, sorted(self._databases()))
                        for entry in committed
                    }
                )
        else:
            configured = sorted(self._databases())
        if not self._scope_override:
            return configured

        # A --scope searches only the databases the config already allows, so a
        # bare schema name can never resolve into a database outside the committed
        # boundary; a qualified one that tries is refused below.
        searchable = sorted({scope.split(".")[0] for scope in configured})
        with blame("--scope", SnowflakeConnectionError):
            requested = sorted(
                {
                    self._resolve_scope(entry, searchable)
                    for entry in self._scope_override
                }
            )
        outside = [scope for scope in requested if not scope_within(scope, configured)]
        if outside:
            raise SnowflakeConnectionError(
                f"scope {name_list(outside)} is outside the committed allowlist "
                f"(snowflake.databases: {name_list(configured)}); --scope narrows "
                "the configured scope, it never widens it"
            )
        return requested

    def _resolve_scope(self, entry: str, searchable: list[str]) -> str:
        """One scope entry, resolved to ``DATABASE`` or ``DATABASE.SCHEMA`` and
        proven to exist. ``searchable`` bounds where a bare schema may be found.

        Every failure path here names the entry and lists the near misses, because
        Snowflake's own answer to a bad scope is ``002043: Object does not exist``,
        which names neither the object nor the fix.
        """

        token = entry.strip().upper()
        if not token:
            raise SnowflakeConnectionError("empty scope entry")

        if "." in token:
            database, _, schema = token.partition(".")
            if "." in schema:
                raise SnowflakeConnectionError(
                    f"scope '{entry}' has too many parts; a source scope is "
                    "<database> or <database>.<schema>, never a table"
                )
            if database not in self._databases():
                reserved = (
                    f" (the account-internal {_RESERVED_DATABASE} database is never "
                    "a source)"
                    if database == _RESERVED_DATABASE
                    else ""
                )
                raise SnowflakeConnectionError(
                    f"scope '{entry}' names no database this role can see{reserved}; "
                    f"{self._visible_hint()}"
                )
            schemas = self._schemas(database)
            if schema not in schemas:
                raise SnowflakeConnectionError(
                    f"scope '{entry}' does not exist: database {database} has no "
                    f"schema {schema}; schemas there: {name_list(sorted(schemas))}"
                )
            return f"{database}.{schema}"

        if token in self._databases():
            return token

        # Qualifying a bare schema costs one SHOW SCHEMAS per candidate database.
        # That is free but not instant, so a wide-open account is asked to qualify
        # rather than made to wait through hundreds of round-trips.
        if len(searchable) > _BARE_SCOPE_SEARCH_LIMIT:
            raise SnowflakeConnectionError(
                f"scope '{entry}' is a bare schema name and this role can see "
                f"{len(searchable)} databases; qualify it as <database>.{token}, or "
                "narrow snowflake.databases in .dex/config.yml first"
            )

        matches = sorted(db for db in searchable if token in self._schemas(db))
        if len(matches) == 1:
            return f"{matches[0]}.{token}"
        if not matches:
            schemas = sorted({s for db in searchable for s in self._schemas(db)})
            raise SnowflakeConnectionError(
                f"scope '{entry}' names no database and no schema in "
                f"{name_list(searchable)}; schemas there: {name_list(schemas)}; "
                f"{self._visible_hint()}"
            )
        raise SnowflakeConnectionError(
            f"scope '{entry}' is ambiguous: it names a schema in "
            f"{name_list(matches)}; qualify it as <database>.{token}"
        )

    def _visible_hint(self) -> str:
        return f"visible databases: {name_list(sorted(self._databases()))}"

    def _databases(self) -> set[str]:
        """Every database the role can see. Free, and doubles as the live
        credential probe: SHOW DATABASES fails on a stale or underprivileged
        credential."""

        if self._visible_databases is None:
            self._visible_databases = {
                str(row["name"]).upper() for row in self._show("SHOW DATABASES")
            } - {_RESERVED_DATABASE}
        return self._visible_databases

    def _schemas(self, database: str) -> set[str]:
        """Every source schema of one database. Free, and cached: a bare scope
        entry searches several databases and must not re-ask for each."""

        if database not in self._schemas_by_database:
            rows = self._show(f"SHOW SCHEMAS IN DATABASE {_quote_ident(database)}")
            self._schemas_by_database[database] = {
                str(row["name"]).upper() for row in rows
            } - {_RESERVED_SCHEMA}
        return self._schemas_by_database[database]

    def _database_scope(self) -> list[str]:
        """Distinct databases across the resolved scopes."""

        return sorted({scope.split(".")[0] for scope in self._scopes()})

    def missing_dev_namespaces(self, database: str) -> list[str]:
        """Which parts of a dbt dev target do not exist yet. Free: SHOW only.

        dbt creates schemas but never databases, so ``dev_schema`` is deliberately
        not checked: its absence is normal on a first build. A missing database is
        fatal, and left to dbt it surfaces from the ``list_schemas`` macro as
        ``002043: Object does not exist``, naming neither the database nor the fix.
        The list shape (rather than a bool) is what the catalog-plus-schema
        connectors will want when they grow the same preflight.
        """

        rows = self._show(f"SHOW DATABASES LIKE '{_escape_literal(database.upper())}'")
        return [] if rows else [f'dev_database "{database}"']

    def list_namespace_objects(self, database: str, schema: str) -> list[str]:
        """Table and view names already in one schema. Free: SHOW only, no
        warehouse. A schema (or database) that does not exist holds nothing to
        collide with, so the ProgrammingError it raises reads as empty."""

        scope = f"{_quote_ident(database.upper())}.{_quote_ident(schema.upper())}"
        names: set[str] = set()
        for kind in ("TABLES", "VIEWS"):
            try:
                rows = self._show(f"SHOW {kind} IN SCHEMA {scope}")
            except self._sf_errors.ProgrammingError:
                return []
            names.update(str(row["name"]) for row in rows)
        return sorted(names)

    def _scope_clause(self, scope: str) -> str:
        if "." in scope:
            db, schema = scope.split(".", 1)
            return f"IN SCHEMA {_quote_ident(db)}.{_quote_ident(schema)}"
        return f"IN DATABASE {_quote_ident(scope)}"

    # --- the warehouse and the credit translation -------------------------------

    def _warehouse(self) -> dict:
        """The pinned warehouse's facts (size, state, credit rate), fetched
        free via SHOW WAREHOUSES and cached per command. Refuses when the
        config pins nothing or the pin does not resolve: dex never spends on a
        warehouse the config does not name."""

        if self._warehouse_info is not None:
            return self._warehouse_info
        pinned = self.target.warehouse
        if not pinned:
            raise SnowflakeConnectionError(
                "no warehouse pinned: set snowflake.warehouse in "
                ".dex/config.yml (dex never spends on a connection-default "
                "warehouse)"
            )
        rows = self._show(f"SHOW WAREHOUSES LIKE '{_escape_literal(pinned)}'")
        if not rows:
            raise SnowflakeConnectionError(
                f"warehouse '{pinned}' not found or not granted; create it or "
                "grant USAGE to this role, or pin a different "
                "snowflake.warehouse in .dex/config.yml"
            )
        row = rows[0]
        size_token = str(row.get("size", "")).upper().replace("-", "").replace(" ", "")
        credits = _CREDITS_PER_HOUR.get(size_token)
        generation = str(row.get("resource_constraint") or "")
        gen2 = "GEN_2" in generation.upper()
        if credits is not None and gen2:
            credits *= _GEN2_MULTIPLIER
        self._warehouse_info = {
            "name": str(row.get("name", pinned)),
            "size": str(row.get("size", "unknown")),
            "state": str(row.get("state", "unknown")).upper(),
            "credits_per_hour": credits,
            "gen2": gen2,
        }
        return self._warehouse_info

    def _to_credits(self, seconds: float) -> float | None:
        info = self._warehouse_info or (
            self._warehouse() if self.target.warehouse else None
        )
        if not info or info.get("credits_per_hour") is None:
            return None
        return round(seconds * info["credits_per_hour"] / 3600.0, 6)

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The compute-time handshake payload: seconds are the binding number,
        credits (and dollars when the price is configured) ride alongside as
        the familiar translation."""

        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in warehouse-seconds; the same number "
                "becomes the server-side statement timeout)"
            ),
            "notes": [_ESTIMATE_QUALITY_NOTE],
        }
        if per_table:
            data["per_table_seconds"] = per_table
        credits = self._to_credits(estimate)
        if credits is not None:
            info = self._warehouse_info or {}
            data["estimated_credits"] = credits
            data["credit_rate"] = {
                "warehouse": info.get("name"),
                "size": info.get("size"),
                "credits_per_hour": info.get("credits_per_hour"),
                "approximate": bool(info.get("gen2")),
            }
            if self.target.credit_price_usd is not None:
                data["estimated_usd"] = round(credits * self.target.credit_price_usd, 4)
        return data

    def spend_display(self) -> dict:
        """Credit translation of actual seconds, merged into ``data.spend``."""

        summary: dict[str, object] = {}
        credits = self._to_credits(self.cost_gate.spend_summary()["seconds_billed"])
        if credits is not None:
            summary["credits_billed"] = credits
            if self.target.credit_price_usd is not None:
                summary["usd_billed"] = round(credits * self.target.credit_price_usd, 4)
        return summary

    # --- estimation (free; feeds the confirm handshake) -------------------------

    def profile_estimate(
        self, identifiers: list[str]
    ) -> tuple[float, dict[str, float]]:
        """The heuristic warehouse-seconds estimate for profiling: per table,
        its bytes over the size-scaled scan rate times the number of aggregate
        batches; plus the 60-second resume minimum once when the warehouse is
        currently suspended. Free: everything comes from SHOW metadata."""

        per_table: dict[str, float] = {}
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            batches = max((len(columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = batches * self._scan_seconds(meta.byte_size)
        total = sum(per_table.values()) + self._resume_floor()
        return total, per_table

    def query_estimate(self, sql: str) -> float:
        """The heuristic estimate for one firewall-approved query: the summed
        bytes of every referenced table over the scan rate, plus the resume
        minimum when the warehouse is suspended."""

        checked = assert_select_only(sql, dialect=self.dialect)
        return self._statement_estimate(checked) + self._resume_floor()

    def _statement_estimate(self, sql: str) -> float:
        total_bytes = 0
        known = 0
        self._load_inventory()
        for identifier in self._referenced_tables(sql):
            entry = self._objects.get(identifier.upper())
            if entry and entry["byte_size"] is not None:
                total_bytes += entry["byte_size"]
                known += 1
        if known == 0:
            return _MIN_STATEMENT_SECONDS
        return self._scan_seconds(total_bytes)

    def _scan_seconds(self, byte_size: int | None) -> float:
        if not byte_size:
            return _MIN_STATEMENT_SECONDS
        # Bigger warehouses scan proportionally faster; the size multiple is
        # the credit rate stripped of any Gen2 billing markup (Gen2 bills more
        # per second without scanning proportionally more).
        size_factor = 1.0
        info = self._warehouse_info
        if info and info.get("credits_per_hour"):
            size_factor = info["credits_per_hour"]
            if info.get("gen2"):
                size_factor /= _GEN2_MULTIPLIER
        rate = _XSMALL_SCAN_BYTES_PER_SECOND * max(size_factor, 1.0)
        return max(byte_size / rate, _MIN_STATEMENT_SECONDS)

    def _resume_floor(self) -> float:
        """60 once when the pinned warehouse is suspended (each resume bills a
        60-second minimum), else 0. Cached: one command resumes at most once."""

        if self._resume_floor_pending is None:
            if not self.target.warehouse:
                self._resume_floor_pending = False
            else:
                self._resume_floor_pending = self._warehouse()["state"] != "STARTED"
        return _RESUME_MINIMUM_SECONDS if self._resume_floor_pending else 0.0

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
        sample_percent = self._sample_percent(identifier, meta.byte_size)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(
                identifier, batch, safe, shape, sample_percent=sample_percent
            )
            rows, labels = self._execute(
                sql, estimate=self._scan_seconds(meta.byte_size)
            )
            values = dict(zip(labels, rows[0], strict=True))
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
            f"profiled from a ~{percent}% block sample (table exceeds "
            "snowflake.max_full_profile_bytes); counts and extremes are "
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
        # connection), so the SELECT-only property is testable offline.
        # Semi-structured columns get the non-null count only
        # (APPROX_COUNT_DISTINCT and MIN/MAX are invalid or meaningless on
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
            source += f" SAMPLE SYSTEM ({sample_percent})"
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
        meta, _ = self.table_metadata(identifier)
        estimate = self._scan_seconds(meta.byte_size)
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
        meta, _ = self.table_metadata(identifier)
        estimate = self._scan_seconds(meta.byte_size) * len(combinations)
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
        """Execute one firewall-approved SELECT, bounded in rows, wall time,
        and warehouse-seconds (client preflight plus the server-side statement
        timeout)."""

        rows, labels, types = self._execute_query(
            sql,
            estimate=self._statement_estimate(sql) + self._resume_floor(),
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
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
        self.cost_gate.charge(estimate + self._consume_resume_floor())
        return self._run(sql)

    def _execute_query(
        self, sql: str, *, estimate: float, timeout_seconds: float, max_rows: int
    ) -> tuple[list, list[str], list[str]]:
        assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(estimate)
        self._consume_resume_floor()
        cursor = self._billed_cursor()
        started = self._clock()
        try:
            cursor.execute(sql, timeout=int(max(timeout_seconds, 1)))
            rows = cursor.fetchmany(max_rows + 1)
        except self._sf_errors.ProgrammingError as exc:
            self._record_elapsed(started, cursor, sql)
            raise self._translate(exc, timeout_seconds) from exc
        self._record_elapsed(started, cursor, sql)
        labels = [d[0].lower() for d in cursor.description]
        types = [self._description_type(d) for d in cursor.description]
        return rows, labels, types

    def _run(self, sql: str) -> tuple[list, list[str]]:
        """The single billed door past the gate: statement timeout set to the
        remaining budget, wall-clock elapsed recorded to the ledger."""

        cursor = self._billed_cursor()
        started = self._clock()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except self._sf_errors.ProgrammingError as exc:
            self._record_elapsed(started, cursor, sql)
            raise self._translate(exc, None) from exc
        self._record_elapsed(started, cursor, sql)
        labels = [d[0].lower() for d in cursor.description]
        return rows, labels

    def _billed_cursor(self):
        """A cursor prepared for spend: warehouse pinned (strict), query tag
        set, and the server-side statement timeout wound down to what remains
        of the budget, so even a wrong heuristic cannot overrun the ceiling."""

        info = self._warehouse()  # refuses when config pins no warehouse
        remaining = self.cost_gate.remaining_for_statement()
        if remaining is not None and remaining < 1:
            raise OverCeilingError(
                "the remaining budget is under one warehouse-second; raise "
                "--budget or narrow the work"
            )
        cursor = self._conn.cursor()
        if not self._session_prepared:
            # Engine-built session statements, not agent SQL: the warehouse
            # name comes from validated SHOW output and the tag is a constant.
            cursor.execute(f"USE WAREHOUSE {_quote_ident(info['name'])}")
            cursor.execute("ALTER SESSION SET QUERY_TAG = 'dex'")
            self._session_prepared = True
        if remaining is not None:
            cursor.execute(
                "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = "
                f"{max(int(remaining), 1)}"
            )
        return cursor

    def _record_elapsed(self, started: float, cursor: Any, sql: str) -> None:
        elapsed = max(self._clock() - started, 0.0)
        self.cost_gate.record_billed(
            elapsed,
            job_id=getattr(cursor, "sfqid", None),
            statement=sql,
        )

    def _consume_resume_floor(self) -> float:
        """The first billed statement of a command carries the 60-second
        resume charge when the warehouse was suspended; later ones do not."""

        floor = self._resume_floor()
        self._resume_floor_pending = False
        return floor

    def _translate(self, exc: Exception, timeout_seconds: float | None) -> Exception:
        message = str(exc)
        if "statement or warehouse timeout" in message.lower():
            return OverCeilingError(
                "the statement hit the server-side timeout derived from the "
                "remaining budget (STATEMENT_TIMEOUT_IN_SECONDS); raise "
                "--budget or narrow the work"
            )
        if timeout_seconds is not None and (
            "canceled" in message.lower() or "cancelled" in message.lower()
        ):
            return TimeoutError(
                f"query exceeded {timeout_seconds:g}s and was cancelled; "
                "narrow it (tighter filter, fewer columns) and retry"
            )
        return exc

    @staticmethod
    def _description_type(description: Any) -> str:
        type_code = description[1]
        return str(getattr(type_code, "name", type_code))

    # --- helpers ------------------------------------------------------------------

    def _show(self, sql: str) -> list[dict]:
        """Free metadata door: SHOW commands only, engine-built, served by the
        cloud-services layer with no warehouse. Results come back as dicts
        keyed by the (lowercase) SHOW column names."""

        if not sql.upper().startswith("SHOW "):
            raise ValueError("only SHOW statements pass through the metadata door")
        cursor = self._conn.cursor()
        cursor.execute(sql)
        labels = [d[0].lower() for d in cursor.description]
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
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()


def _quote_ident(name: str) -> str:
    """Quote one identifier component with double quotes (preserving case,
    which unquoted Snowflake identifiers would fold to upper), doubling
    embedded quotes."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_literal(value: str) -> str:
    return value.replace("'", "''")
