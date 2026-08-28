"""The BigQuery adapter: the first billed cloud connector.

Reads are gated twice: every statement passes the SELECT-only guard with the
BigQuery dialect, and every billed statement is dry-run first (free) so the
injected :class:`~exmergo_dex_core.guards.cost_guard.CostGate` can refuse it
before a byte is billed. Execution then runs with a server-side
``maximum_bytes_billed`` cap, so even a wrong estimate cannot overrun the
budget. Metadata (datasets, tables, schemas, row and byte counts) comes from
free API calls, never ``INFORMATION_SCHEMA`` (which bills a 10 MB minimum per
query), so inventory and ``connect test`` stay free.

BigQuery has no read-only connection mode; on top of the SQL guard the adapter
simply calls no mutating client API, and the docs recommend read-only roles
(``roles/bigquery.dataViewer`` + ``roles/bigquery.jobUser``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import BigQueryTarget
from ..envelope import Paradigm
from ..errors import ConnectorError, PrerequisiteError
from ..guards.cost_guard import CostGate, OverCeilingError
from ..guards.sql_guard import assert_select_only
from .base import (
    VALUE_DOMAIN_MIN_ROWS,
    ColumnAggregate,
    ColumnMeta,
    ObjectMeta,
    QueryResult,
    ValueDomainSample,
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
)

PARADIGM = "bytes_scanned"
DIALECT = "bigquery"

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 4 expressions per column).
_COLUMN_BATCH = 50

_BIGINT_TYPE = "INT64"

# BigQuery bills at least this much for any on-demand query that scans data.
# A remaining budget below it can never cover a statement, so we refuse with
# the math instead of letting the server fail the job after the fact.
_MIN_BILLED_BYTES = 10 * 1024 * 1024

# BigQuery's own refusal names the exact byte count it needed (issue #320):
# "Query exceeded limit for bytes billed: <cap>. <required> or higher
# required." No dry run predicts this number, since it reflects the server's
# own execution-time rounding of bytes billed (observed per table scanned,
# not per query), which is exactly the gap between a dry-run-based estimate
# and what the job actually needed. Parsed rather than guessed at with a
# margin, so the retry below asks for precisely what BigQuery says it needs,
# whatever the underlying rounding rule turns out to be.
_BYTES_BILLED_REQUIRED_RE = re.compile(r"(\d+) or higher required")

# Field types whose values are nested or non-scalar: no approx-distinct, no
# min/max, and non-null counting via COUNTIF (COUNT DISTINCT is invalid on
# them and plain COUNT is not supported for every one of these types).
_NESTED_FIELD_TYPES = {"RECORD", "STRUCT", "JSON", "GEOGRAPHY", "RANGE", "INTERVAL"}

# The object kinds whose stored row and byte counts BigQuery maintains. See
# BigQueryAdapter._maintains_counts for why this is an allowlist.
_COUNTED_TABLE_TYPES = frozenset({"TABLE"})


@dataclass(frozen=True)
class _EstimateComposition:
    """What a profile estimate is made of: measured dry-run scan versus
    escalation reserve.

    The two halves answer different questions and a caller deciding whether to
    raise a budget needs them apart. The scan half is a dry-run of statements
    that will certainly run; the reserve half is headroom held for probes that
    may never be issued (see :meth:`BigQueryAdapter.profile_estimate`). Quoting
    only the sum is what left a refused nightly run unable to tell a warehouse
    that had grown from a release that had added a reserve (issue #299).
    """

    scan_bytes: float
    scan_queries: int
    reserved_bytes: float
    reserved_queries: int


def _regexp_predicate(qcol: str, pattern: str) -> str:
    # Raw string literal; REGEXP_CONTAINS matches substrings, so the shared
    # patterns' anchors make it a full match.
    return f"REGEXP_CONTAINS({qcol}, r'{pattern}')"


def _trunc_family(data_type: str) -> str:
    # BigQuery has three truncation functions, one per temporal type, unlike
    # every other dialect's single date_trunc: DATE_TRUNC only accepts DATE,
    # and only DATE_TRUNC has no HOUR unit -- which is moot here since
    # temporal_units_for already skips hour for a bare DATE column.
    upper = data_type.upper()
    if "TIMESTAMP" in upper:
        return "TIMESTAMP_TRUNC"
    if "DATETIME" in upper:
        return "DATETIME_TRUNC"
    return "DATE_TRUNC"


def _diff_family(data_type: str) -> str:
    upper = data_type.upper()
    if "TIMESTAMP" in upper:
        return "TIMESTAMP_DIFF"
    if "DATETIME" in upper:
        return "DATETIME_DIFF"
    return "DATE_DIFF"


def _date_trunc_expr(qcol: str, unit: str, data_type: str) -> str:
    # Reversed argument order and a bare unquoted unit keyword: the one
    # dialect that differs from every other adapter's date_trunc(unit, col).
    return f"{_trunc_family(data_type)}({qcol}, {unit.upper()})"


def _date_diff_expr(unit: str, later: str, earlier: str, data_type: str) -> str:
    return f"{_diff_family(data_type)}({later}, {earlier}, {unit.upper()})"


class BigQueryConnectionError(ConnectorError):
    """Raised when a source scope (or the dev project) cannot be resolved. The
    message always names the fix, never a credential."""


class BigQueryAdapter:
    """Holds one BigQuery client plus the cost gate for one command.

    ``client`` is injectable (class DI) so unit tests drive a fake; the real
    client is built lazily from the credentials that ``connect.py`` discovered
    via Application Default Credentials. Credentials live only inside this
    process and are never surfaced.
    """

    name = "bigquery"
    dialect = DIALECT
    paradigm = Paradigm.BYTES_SCANNED

    def __init__(
        self,
        *,
        project: str,
        cost_gate: CostGate,
        target: BigQueryTarget | None = None,
        credentials: Any | None = None,
        principal_type: str | None = None,
        scope_origin: str | None = None,
        client: Any | None = None,
    ):
        self.project = project
        self.cost_gate = cost_gate
        self.target = target or BigQueryTarget()
        self.principal_type = principal_type or "unknown"
        # What the scope entries in the target came from, so a refusal names the
        # thing the user has to go edit: a per-command flag or the committed
        # allowlist. `narrow_target` has already collapsed the two by the time
        # the adapter sees them, and the fix differs entirely.
        self._scope_origin = scope_origin or "bigquery.datasets in .dex/config.yml"
        # Imported lazily so the base package import does not require the
        # [bigquery] extra; only this adapter pulls it in.
        try:
            from google.api_core import exceptions as api_exceptions
            from google.auth import exceptions as auth_exceptions
            from google.cloud import bigquery
        except ImportError as exc:
            raise RuntimeError(
                "the BigQuery client is not installed; install the connector "
                "extra: exmergo-dex-core[bigquery]"
            ) from exc
        self._bq = bigquery
        self._api_exceptions = api_exceptions
        self._auth_exceptions = auth_exceptions
        self._client = client or bigquery.Client(
            project=project, credentials=credentials
        )
        # get_table results are cached per command so the estimate pass and the
        # confirmed profiling pass do not re-fetch (each fetch is a free API
        # call, but table facts also back the notes and sampling decisions).
        self._tables: dict[str, Any] = {}
        # Row counts learned from a profiling aggregate, which is the only place
        # a count exists for an object kind BigQuery keeps no metadata count for.
        # Per command, like `_tables`, and it supersedes the metadata rather than
        # merging with it: the aggregate counted, the metadata guessed or lied.
        self._exact_rows: dict[str, int] = {}
        self._resolved_datasets: list[str] | None = None
        self._notes: dict[str, list[str]] = {}
        # What the last profile estimate was made of, so the handshake and the
        # over-ceiling refusal can attribute the number they quote. Per command,
        # like `_notes`: one command builds at most one profile estimate.
        self._composition: _EstimateComposition | None = None

    # --- capabilities ---------------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # Resolving the scopes is also the live probe `connect test` needs: every
        # entry is proven with a metadata GET, so a stale ADC token cannot report
        # a healthy connection, and neither can an allowlist that names nothing.
        datasets = self._dataset_ids()
        cost = self.cost_gate.cost()
        return {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "paradigm": self.paradigm.value,
            "project": self.project,
            "location": self.target.location,
            "principal_type": self.principal_type,
            "dataset_count": len(datasets),
            "required_roles": [
                "roles/bigquery.dataViewer",
                "roles/bigquery.jobUser",
            ],
            "budget": {
                "ceiling": cost.ceiling,
                "session_spent_today": self.cost_gate.session_spent_now(),
            },
        }

    # --- introspection (free API metadata; no queries, no billing) ------------

    def list_objects(self, *, include_views: bool = True) -> list[ObjectMeta]:
        objects: list[ObjectMeta] = []
        for qualified in self._dataset_ids():
            for item in self._client.list_tables(qualified):
                object_type = self._object_type(item.table_type)
                if object_type == "view" and not include_views:
                    continue
                table = self._get_table(f"{qualified}.{item.table_id}")
                objects.append(self._object_meta(table, object_type))
        objects.sort(key=lambda o: o.identifier)
        return objects

    def table_metadata(self, identifier: str) -> tuple[ObjectMeta, list[ColumnMeta]]:
        table = self._get_table(identifier)
        object_type = self._object_type(getattr(table, "table_type", "TABLE"))
        columns = [
            ColumnMeta(
                name=field.name,
                data_type=self._render_type(field),
                nullable=(field.mode or "NULLABLE") != "REQUIRED",
                ordinal=index,
            )
            for index, field in enumerate(table.schema)
        ]
        return self._object_meta(table, object_type), columns

    def _object_meta(self, table: Any, object_type: str) -> ObjectMeta:
        identifier = f"{table.project}.{table.dataset_id}.{table.table_id}"
        # An exact count from a profiling scan supersedes the metadata for the
        # rest of the command, and for the object kinds below it is the only
        # count there will ever be, so it is consulted before the metadata is.
        exact = self._exact_rows.get(identifier)
        if exact is not None:
            num_rows: int | None = exact
        elif self._maintains_counts(getattr(table, "table_type", None)):
            num_rows = getattr(table, "num_rows", None)
        else:
            num_rows = None
        num_bytes = (
            getattr(table, "num_bytes", None)
            if self._maintains_counts(getattr(table, "table_type", None))
            else None
        )
        return ObjectMeta(
            identifier=identifier,
            object_type=object_type,
            schema=table.dataset_id,
            name=table.table_id,
            row_count=int(num_rows) if num_rows is not None else None,
            byte_size=int(num_bytes) if num_bytes is not None else None,
            column_count=len(table.schema or []),
        )

    @staticmethod
    def _object_type(table_type: str | None) -> str:
        return "view" if (table_type or "").upper().endswith("VIEW") else "table"

    @staticmethod
    def _maintains_counts(table_type: str | None) -> bool:
        """Whether BigQuery keeps a stored row and byte count for this kind of object.

        An allowlist rather than a list of exceptions, because the exceptions are
        the growing side. A base table has counts; a view, a materialized view, an
        external table over object storage, a snapshot, and whatever the API names
        next do not, and for all of them ``num_rows`` comes back ``0`` rather than
        absent. Storing that zero would be a claim the table is empty, which reads
        the same as a table that genuinely is, so the whole class is classified as
        unknown instead and the count arrives from the (already billed) profiling
        aggregate. Testing for the kinds that do have counts means a kind nobody
        anticipated is treated as unknown, which is the direction that cannot
        fabricate a number.

        An absent ``table_type`` is a base table: the client leaves the attribute
        unset until the server fills it in, so it means "not told yet" rather than
        a kind of its own.
        """

        return (table_type or "TABLE").upper() in _COUNTED_TABLE_TYPES

    @staticmethod
    def _render_type(field: Any) -> str:
        base = "STRUCT" if field.field_type == "RECORD" else field.field_type
        if (field.mode or "").upper() == "REPEATED":
            return f"ARRAY<{base}>"
        return str(base)

    def _dataset_ids(self) -> list[str]:
        """The datasets in scope, fully qualified as ``project.dataset``, resolved
        and proven to exist.

        Allowlist entries may name another project (``project.dataset``), which
        is how public datasets (``bigquery-public-data.samples``) are explored:
        reads go there, jobs still run in and bill to ``self.project``. Bare
        entries qualify against ``self.project``; no allowlist means every
        dataset of the configured project.

        Resolution is free (metadata GET, no query) and cached for the command.
        It runs before anything is estimated, because a scope that resolves to
        nothing and silently falls back to the whole allowlist is a cost-safety
        bug: the estimate the user confirms would cover tables they never named.
        """

        if self._resolved_datasets is None:
            self._resolved_datasets = self._resolve_datasets()
        return self._resolved_datasets

    def _resolve_datasets(self) -> list[str]:
        if not self.target.datasets:
            datasets = self._request(
                lambda: list(self._client.list_datasets(self.project))
            )
            return sorted(f"{self.project}.{item.dataset_id}" for item in datasets)
        with blame(self._scope_origin, BigQueryConnectionError):
            return sorted(
                {self._resolve_dataset(entry) for entry in self.target.datasets}
            )

    def _resolve_dataset(self, entry: str) -> str:
        """One scope entry, qualified and proven to exist.

        The GET is the proof, rather than listing the project and testing
        membership: a principal may legitimately be granted one dataset without
        the project-wide ``bigquery.datasets.list``, and public projects are far
        too large to enumerate for a containment check. The listing is only used
        to name the near misses once something has already failed.
        """

        token = entry.strip()
        if not token:
            raise BigQueryConnectionError("empty scope entry")
        if token.count(".") > 1:
            raise BigQueryConnectionError(
                f"scope '{entry}' has too many parts; a source scope is "
                "<dataset> or <project>.<dataset>, never a table"
            )
        qualified = token if "." in token else f"{self.project}.{token}"
        project, _, dataset = qualified.partition(".")
        try:
            self._request(lambda: self._client.get_dataset(qualified))
        except self._api_exceptions.NotFound as exc:
            raise BigQueryConnectionError(
                f"scope '{entry}' does not exist: project {project} has no "
                f"dataset {dataset}; {self._visible_hint(project)}"
            ) from exc
        except self._api_exceptions.Forbidden as exc:
            raise BigQueryConnectionError(
                f"scope '{entry}' is not readable by this principal; grant "
                "roles/bigquery.dataViewer on it, or point bigquery.datasets at "
                "a dataset the principal can read"
            ) from exc
        return qualified

    def _visible_hint(self, project: str) -> str:
        """The datasets that do exist, for a refusal message. Best effort: a
        principal that may GET a dataset without listing the project still gets
        the refusal, just without the near misses."""

        try:
            visible = sorted(
                item.dataset_id for item in self._client.list_datasets(project)
            )
        except Exception:  # a hint is never worth failing the refusal for
            return "and its datasets cannot be listed by this principal"
        return f"datasets there: {name_list(visible)}"

    def missing_dev_namespaces(self, dataset: str) -> list[str]:
        """Which parts of a dbt dev target do not exist yet. Free: metadata GET,
        no query, so this costs nothing on a bytes-billed connector.

        Unlike Snowflake and Databricks, dbt-bigquery *does* create its dev
        dataset (its ``create_schema`` issues ``CREATE SCHEMA IF NOT EXISTS``),
        so an absent dataset is normal on a first build and is reported for the
        caller to warn about, not to refuse. What dbt cannot create is the
        project, so an unreachable one is raised here.
        """

        qualified = dataset if "." in dataset else f"{self.project}.{dataset}"
        project = qualified.partition(".")[0]
        try:
            self._client.get_dataset(qualified)
        except self._api_exceptions.NotFound:
            # Distinguish "no such dataset" (dbt will create it) from "no such
            # project" (dbt cannot), because BigQuery answers both with NotFound.
            # list_datasets returns a lazy iterator, so it has to be drained for
            # the request to actually go out; without that, an unreachable project
            # reads as a reachable one.
            try:
                list(self._client.list_datasets(project, max_results=1))
            except Exception as exc:  # any failure to reach the project is fatal
                raise BigQueryConnectionError(
                    f"the dev project {project} is not reachable by this "
                    f"principal ({type(exc).__name__}); dbt creates datasets but "
                    "never projects, so point bigquery.dev_dataset at a dataset "
                    "in a project the principal can write"
                ) from exc
            return [f'dev_dataset "{qualified}"']
        return []

    def list_namespace_objects(self, dataset: str) -> list[str]:
        """Table and view names already in one dataset. Free: the tables.list
        metadata API, never INFORMATION_SCHEMA (which bills a 10 MB minimum).
        An absent dataset reads as empty: nothing is there to collide with."""

        qualified = dataset if "." in dataset else f"{self.project}.{dataset}"
        try:
            return sorted(item.table_id for item in self._client.list_tables(qualified))
        except self._api_exceptions.NotFound:
            return []

    def _get_table(self, identifier: str) -> Any:
        cached = self._tables.get(identifier)
        if cached is not None:
            return cached
        table = self._client.get_table(identifier)
        self._tables[identifier] = table
        if getattr(table, "require_partition_filter", False):
            self._note(
                identifier,
                "requires a partition filter; profiled from metadata only "
                "(aggregate scans would be refused by BigQuery)",
            )
        return table

    def _note(self, identifier: str, note: str) -> None:
        notes = self._notes.setdefault(identifier, [])
        if note not in notes:
            notes.append(note)

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the profiling run accumulated for one object
        (partition-filter degradation, block sampling). Merged into the
        dataset's ``data_quality`` by the profile engine."""

        return list(self._notes.get(identifier, []))

    # --- profiling (billed; every statement dry-run and gated) ----------------

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
        if self._unqueryable(identifier):
            return [self._empty_aggregate(col) for col in columns]
        safe = safe_min_max or set()
        shape = shape_stats or set()
        type_req = type_stats or set()
        key_shape_req = key_shape_stats or set()
        temporal_req = temporal_stats or set()
        sample_percent = self._sample_percent(identifier)
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
                sample_percent=sample_percent,
            )
            try:
                _job, iterator = self._execute(sql)
                rows = list(iterator)
            except self._api_exceptions.BadRequest as exc:
                # An unqueryable shape discovered only at query time (for
                # example an external table whose source is unreadable):
                # degrade to metadata-only rather than failing the profile.
                self._note(
                    identifier,
                    f"aggregate profiling failed and was skipped: {exc.message}"
                    if hasattr(exc, "message")
                    else "aggregate profiling failed and was skipped",
                )
                results.extend(self._empty_aggregate(col) for col in batch)
                continue
            if sample_percent is None:
                # The batch just counted the table exactly, and for a view or an
                # external table that is the only count anyone will ever have:
                # BigQuery maintains none, and a COUNT(*) issued to find one
                # would bill a second time for a number already in hand. Capture
                # it so the metadata re-read after this scan can hand it to the
                # uniqueness proof, the composite-key probe, and the grain
                # verdict, all of which decline to run without a row count. Not
                # under sampling, where the count describes the sample.
                self._exact_rows[identifier] = int(rows[0]["n_total"])
            results.extend(
                self._read_aggregates(rows[0], plan, sampled=sample_percent is not None)
            )
        return results

    def _unqueryable(self, identifier: str) -> bool:
        table = self._get_table(identifier)
        return bool(getattr(table, "require_partition_filter", False))

    def _sample_percent(self, identifier: str) -> float | None:
        threshold = self.target.max_full_profile_bytes
        if threshold is None:
            return None
        table = self._get_table(identifier)
        num_bytes = getattr(table, "num_bytes", None)
        if not num_bytes or num_bytes <= threshold:
            return None
        percent = max(round(100.0 * threshold / num_bytes, 2), 0.01)
        self._note(
            identifier,
            f"profiled from a ~{percent}% block sample (table exceeds "
            "bigquery.max_full_profile_bytes); counts and extremes are "
            "approximate and uniqueness is not judged",
        )
        return percent

    @staticmethod
    def _empty_aggregate(col: ColumnMeta) -> ColumnAggregate:
        return ColumnAggregate(
            name=col.name,
            null_fraction=None,
            distinct_count=None,
            is_unique=None,
            min_value=None,
            max_value=None,
        )

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
        sample_percent: float | None = None,
    ) -> tuple[
        str, list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool, bool]]
    ]:
        # One aggregate statement per batch: COUNT(*) once, then per column a
        # non-null count, an approximate distinct, min/max only where allowed,
        # and value-shape/type-contradiction/key-shape/temporal-continuity
        # fractions only where requested. Pure (no client), so the SELECT-only
        # property is testable without a connection. Repeated (ARRAY) columns
        # get no aggregates at all: they cannot be NULL in BigQuery and
        # COUNT/DISTINCT are invalid on them; other nested types get a
        # COUNTIF non-null count only.
        source = self._quote(identifier)
        if sample_percent is not None:
            source += f" TABLESAMPLE SYSTEM ({sample_percent} PERCENT)"
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[
            tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool, bool]
        ] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            repeated = col.data_type.upper().startswith("ARRAY")
            nested = repeated or self._is_nested(col.data_type)
            if repeated:
                plan.append((i, col, False, False, False, False, False, False, False))
                continue
            if nested:
                select_parts.append(f"COUNTIF({qcol} IS NOT NULL) AS nn_{i}")
                plan.append((i, col, True, False, False, False, False, False, False))
                continue
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            select_parts.append(f"APPROX_COUNT_DISTINCT({qcol}) AS nd_{i}")
            wants_min_max = col.name in safe
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            wants_shape = col.name in shape
            if wants_shape:
                select_parts.extend(shape_stat_expressions(qcol, i, _regexp_predicate))
            wants_type = col.name in type_req
            if wants_type:
                select_parts.extend(
                    type_contradiction_expressions(
                        qcol,
                        i,
                        is_string=is_string_type(col.data_type),
                        is_integer=is_integer_type(col.data_type),
                        regexp_predicate=_regexp_predicate,
                        bigint_type=_BIGINT_TYPE,
                    )
                )
            wants_key_shape = col.name in key_shape_req
            if wants_key_shape:
                select_parts.extend(key_shape_expressions(qcol, i, _regexp_predicate))
            wants_temporal = col.name in temporal_req
            if wants_temporal:
                dtype = col.data_type

                def date_trunc(q: str, u: str, dtype: str = dtype) -> str:
                    return _date_trunc_expr(q, u, dtype)

                def date_diff(
                    u: str, later: str, earlier: str, dtype: str = dtype
                ) -> str:
                    return _date_diff_expr(u, later, earlier, dtype)

                select_parts.extend(temporal_alignment_expressions(qcol, i, date_trunc))
                for unit in temporal_units_for(dtype):
                    select_parts.extend(
                        temporal_continuity_sql(
                            qcol, i, unit, source, date_trunc, date_diff
                        )
                    )
            plan.append(
                (
                    i,
                    col,
                    True,
                    True,
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
    def _is_nested(data_type: str) -> bool:
        upper = data_type.upper()
        return upper.startswith("ARRAY") or any(
            upper.startswith(t) for t in _NESTED_FIELD_TYPES
        )

    def _read_aggregates(
        self,
        row: Any,
        plan: list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool, bool]],
        *,
        sampled: bool,
    ) -> list[ColumnAggregate]:
        n_total = int(row["n_total"])
        aggregates: list[ColumnAggregate] = []
        for (
            i,
            col,
            has_count,
            wants_distinct,
            wants_min_max,
            wants_shape,
            wants_type,
            wants_key_shape,
            wants_temporal,
        ) in plan:
            nn = row[f"nn_{i}"] if has_count else None
            has_counts = nn is not None
            null_fraction = (
                (1 - int(nn) / n_total) if has_counts and n_total > 0 else None
            )
            distinct = int(row[f"nd_{i}"]) if wants_distinct and n_total > 0 else None
            # Under block sampling, counts describe the sample, so a uniqueness
            # verdict would be unfounded either way.
            is_unique = (
                (distinct == int(nn) == n_total and n_total > 0)
                if distinct is not None and has_counts and not sampled
                else None
            )
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=is_unique,
                    min_value=row[f"mn_{i}"] if wants_min_max else None,
                    max_value=row[f"mx_{i}"] if wants_min_max else None,
                    upper_vocab_fraction=shape_stat_value(row, f"su_{i}", wants_shape),
                    person_shape_fraction=shape_stat_value(row, f"sp_{i}", wants_shape),
                    avg_token_count=shape_stat_value(row, f"st_{i}", wants_shape),
                    **type_contradiction_aggregate_kwargs(row, i, wants_type),
                    **key_shape_aggregate_kwargs(row, i, wants_key_shape),
                    **temporal_continuity_aggregate_kwargs(row, i, wants_temporal),
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

        Charged at the floored bytes, not the raw dry-run number: this is one
        billed query like any other, so it bills (and must be budgeted
        against) at least the per-query minimum (issue #107).
        """

        if self._unqueryable(identifier) or not columns:
            return {}
        select_parts = [
            f"COUNT(DISTINCT {_quote_ident(name)}) AS d_{i}"
            for i, name in enumerate(columns)
        ]
        sql = assert_select_only(
            f"SELECT {', '.join(select_parts)} FROM {self._quote(identifier)}",  # noqa: S608
            dialect=self.dialect,
        )
        floored = max(self._dry_run(sql), float(_MIN_BILLED_BYTES))
        if not self.cost_gate.try_charge(floored):
            self._note(
                identifier,
                "distinct-count escalation skipped: the remaining budget could "
                "not cover the extra scan; uniqueness verdicts stay approximate",
            )
            return {}
        _job, iterator = self._run(sql, floored)
        rows = list(iterator)
        return {name: int(rows[0][f"d_{i}"]) for i, name in enumerate(columns)}

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ) -> dict[tuple[str, ...], int]:
        """Exact distinct count per column combination, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scan, return nothing and let the grain stay unknown. A metered
        adapter never self-escalates past its ceiling.

        Charged at the floored bytes, not the raw dry-run number: this is one
        billed query like any other, so it bills (and must be budgeted
        against) at least the per-query minimum (issue #107)."""

        if self._unqueryable(identifier) or not combinations:
            return {}
        sql = assert_select_only(
            distinct_combination_sql(
                self._quote(identifier), combinations, _quote_ident
            ),
            dialect=self.dialect,
        )
        floored = max(self._dry_run(sql), float(_MIN_BILLED_BYTES))
        if not self.cost_gate.try_charge(floored):
            self._note(
                identifier,
                "composite-key probe skipped: the remaining budget could not "
                "cover the extra scan; grain stays unknown",
            )
            return {}
        _job, iterator = self._run(sql, floored)
        rows = list(iterator)
        return {
            tuple(combo): int(rows[0][f"d_{i}"]) for i, combo in enumerate(combinations)
        }

    def value_domain_counts(
        self, identifier: str, columns: list[str], *, limit: int
    ) -> dict[str, ValueDomainSample]:
        """Top ``limit`` values by frequency per column, spent only within
        the already-confirmed budget: when the remaining budget cannot cover
        the extra scan, return nothing and report no value domain. A metered
        adapter never self-escalates past its ceiling.

        Charged at the floored bytes, not the raw dry-run number: this is one
        billed query like any other, so it bills (and must be budgeted
        against) at least the per-query minimum (issue #107)."""

        if self._unqueryable(identifier) or not columns:
            return {}
        table = self._quote(identifier)
        parts = []
        for i, name in enumerate(columns):
            qcol = _quote_ident(name)
            parts.append(
                "(SELECT ARRAY_AGG(STRUCT(v AS v, c AS c) ORDER BY c DESC, v "  # noqa: S608
                f"LIMIT {limit}) FROM (SELECT {qcol} AS v, COUNT(*) AS c "
                f"FROM {table} WHERE {qcol} IS NOT NULL GROUP BY {qcol}) ) AS d_{i}"
            )
            parts.append(
                f"(SELECT COUNT(*) FROM (SELECT DISTINCT {qcol} FROM {table} "  # noqa: S608
                f"WHERE {qcol} IS NOT NULL)) AS n_{i}"
            )
        sql = assert_select_only(f"SELECT {', '.join(parts)}", dialect=self.dialect)
        floored = max(self._dry_run(sql), float(_MIN_BILLED_BYTES))
        if not self.cost_gate.try_charge(floored):
            self._note(
                identifier,
                "value-domain probe skipped: the remaining budget could not "
                "cover the extra scan; no value domain reported",
            )
            return {}
        _job, iterator = self._run(sql, floored)
        rows = list(iterator)
        return {
            name: ValueDomainSample(
                values=[(entry["v"], entry["c"]) for entry in rows[0][f"d_{i}"]],
                total_distinct=int(rows[0][f"n_{i}"]),
            )
            for i, name in enumerate(columns)
        }

    # --- estimation (free dry-runs; feeds the confirm handshake) --------------

    def profile_estimate(
        self, identifiers: list[str], *, include_blobs: set[str] | None = None
    ) -> tuple[float, dict[str, float]]:
        """Dry-run every aggregate batch profiling would issue and sum the
        bytes, per table and in total. Free: metadata GETs and dry-run jobs
        bill nothing. Partition-filter tables contribute zero because they
        will not be queried.

        Each batch is one billed query over one table, so its cost is floored
        to the per-query minimum: on small tables the raw scan is a fraction of
        what BigQuery actually bills, and an unfloored estimate would send the
        agent into a ladder of budget rejections.

        The total also reserves one floor per table for each of the three
        escalation queries a profile may still issue after that batch
        (:meth:`exact_distinct_counts`, :meth:`value_domain_counts`,
        :meth:`distinct_combination_counts`). Whether any of them runs depends
        on the batch's own approximate results, which do not exist yet at
        estimate time, so there is no way to dry-run them here. Holding their
        floor keeps this total a ceiling profiling will not exceed rather than
        a number a run that does escalate blows past, which is the gap this
        estimator used to leave open (issue #107). See
        :meth:`_escalation_reserve` for what narrows each one, and
        :attr:`_composition` for how the reserve is reported apart from the
        scan it rides with.

        Blob-type columns are excluded from the batches the same way
        ``explore.profile.profile`` excludes them from the scan itself
        (``include_blobs`` names the ``identifier.column`` paths a human opted
        back in), so this estimate matches what the run will actually bill."""

        blob_paths = include_blobs or set()
        per_table: dict[str, float] = {}
        scan_bytes = 0.0
        scan_queries = 0
        reserved_queries = 0
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            if self._unqueryable(identifier):
                per_table[identifier] = 0.0
                continue
            scan_columns = [
                c
                for c in columns
                if not is_blob_type(c.data_type)
                or f"{identifier}.{c.name}".lower() in blob_paths
            ]
            # min/max, shape, type-contradiction, key-shape, and
            # temporal-continuity fractions add no scanned bytes: columnar
            # billing already charges the whole column.
            safe: set[str] = set()
            shape: set[str] = set()
            type_req: set[str] = set()
            key_shape_req: set[str] = set()
            temporal_req: set[str] = set()
            sample_percent = self._sample_percent(identifier)
            total = 0.0
            for start in range(0, len(scan_columns), _COLUMN_BATCH):
                sql, _plan = self._build_aggregate_sql(
                    identifier,
                    scan_columns[start : start + _COLUMN_BATCH],
                    safe,
                    shape,
                    type_req,
                    key_shape_req,
                    temporal_req,
                    sample_percent=sample_percent,
                )
                try:
                    batch = max(self._dry_run(sql), float(_MIN_BILLED_BYTES))
                except self._api_exceptions.BadRequest:
                    self._note(
                        identifier,
                        "could not estimate an aggregate scan (dry-run failed); "
                        "the object is skipped",
                    )
                    continue
                total += batch
                scan_bytes += batch
                scan_queries += 1
            reserved = self._escalation_reserve(meta, scan_columns)
            total += reserved * float(_MIN_BILLED_BYTES)
            reserved_queries += reserved
            per_table[identifier] = total
        self._composition = _EstimateComposition(
            scan_bytes=scan_bytes,
            scan_queries=scan_queries,
            reserved_bytes=reserved_queries * float(_MIN_BILLED_BYTES),
            reserved_queries=reserved_queries,
        )
        return sum(per_table.values()), per_table

    def _escalation_reserve(
        self, meta: ObjectMeta, scan_columns: list[ColumnMeta]
    ) -> int:
        """How many escalation queries to hold a billing floor for on one table.

        A reserve is dropped only where the probe's own guard already rules the
        query out from metadata alone, never as a guess about what is likely: an
        estimate that reserves for a query that cannot run is merely loose, while
        one that skips a query that can is the defect issue #107 closed. So each
        condition below mirrors one in ``explore.profile``, and moving one
        without the other is the bug to watch for.

        - Nothing at all for a table known to hold no rows. All three probes
          return early on a falsy count, so a provably empty table cannot
          escalate and the reserve would be money no run could spend.
        - An unknown count is not an empty one, and it reserves. For a view or
          an external table the aggregate's own ``COUNT(*)`` is what supplies the
          count, and it lands before the probes are asked, so every one of them
          can run. Reserving the maximum is the only honest read at estimate
          time, since the number that decides which probes are eligible does not
          exist yet. This is the half of issue #299's reasoning that inverted:
          the reserve was dropped for these objects because the probes provably
          could not run, and now they can.
        - Nothing for columns BigQuery cannot count distinctly. Nested and
          repeated fields get no approximate distinct in the aggregate batch, and
          every probe's eligibility starts from one, so they can no more trigger
          an escalation than a blob column already excluded from the scan.
        - No value domain below :data:`VALUE_DOMAIN_MIN_ROWS` rows, which is what
          the probe's row-relative fraction implies once a domain needs at least
          one distinct value.
        - No composite probe below two countable columns, since a combination
          needs two members. This was already conditioned, but on the raw column
          count, which counts columns that can never join a pair.
        """

        countable = [c for c in scan_columns if not self._is_nested(c.data_type)]
        if not countable or meta.row_count == 0:
            return 0
        reserved = 1  # exact_distinct_counts
        if meta.row_count is None or meta.row_count >= VALUE_DOMAIN_MIN_ROWS:
            reserved += 1  # value_domain_counts
        if len(countable) >= 2:
            reserved += 1  # distinct_combination_counts
        return reserved

    def query_estimate(self, sql: str) -> float:
        """The dry-run byte estimate for one firewall-approved query, floored to
        what BigQuery will actually bill (the per-referenced-table minimum), so
        the estimate the agent budgets against is not decorative on small data."""

        checked = assert_select_only(sql, dialect=self.dialect)
        return max(self._dry_run(checked), self._min_billed_floor(checked))

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The bytes-scanned handshake payload: names the per-query billing
        floor baked into every number here, so a small-table estimate reads as
        a trustworthy ceiling instead of one the actual bill will exceed
        (issue #107).

        Deliberately a pure function of its arguments. What a *profile* estimate
        is made of is answered by :meth:`profile_reserve` instead, because this
        method also describes estimates that carry no reserve (an ad-hoc query,
        a mid-command verify checkpoint) and it cannot tell which it was handed.
        """

        data: dict[str, object] = {
            "estimated_bytes": estimate,
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<bytes> (the ceiling in bytes; 10000000000 is 10 GB, about "
                "$0.06 on-demand)"
            ),
            "notes": [
                f"BigQuery bills at least {_MIN_BILLED_BYTES:,} bytes (10 MB) "
                "per query; every number here already reflects that floor"
            ],
        }
        if per_table:
            data["per_table_bytes"] = per_table
        return data

    def profile_reserve(self, estimate: float) -> dict | None:
        """How much of ``estimate`` is escalation reserve rather than measured
        scan, as a sentence plus the two numbers behind it. ``None`` when this
        command priced no profile, so there is nothing to attribute.

        The confirm handshake and the over-ceiling refusal both reach for this,
        which is the point of it existing: a refusal that quotes a number
        without saying what it is made of leaves the operator to reconstruct
        the split from the spend ledger by hand, which is what issue #299
        reported doing. Only the command-level handshake asks, because only
        that estimate is the one :meth:`profile_estimate` built.

        The remainder is described rather than the recorded scan half quoted,
        because a caller may have added statement estimates of its own to the
        total (``explore query`` prices an auto-profile and its statements in
        one handshake) and those are dry-run figures too.
        """

        composition = self._composition
        if composition is None or not composition.reserved_queries:
            return None
        return {
            "note": (
                f"{composition.reserved_bytes:,.0f} bytes of this estimate is "
                f"escalation reserve: {composition.reserved_queries} queries at "
                f"BigQuery's {_MIN_BILLED_BYTES:,}-byte per-query minimum, held "
                "for probes a profile may add after its aggregate scan and may "
                "never issue. The remaining "
                f"{estimate - composition.reserved_bytes:,.0f} bytes is dry-run "
                "scan"
            ),
            "reserved_bytes": composition.reserved_bytes,
            "reserved_queries": composition.reserved_queries,
        }

    def _min_billed_floor(self, sql: str) -> float:
        """BigQuery bills at least ``_MIN_BILLED_BYTES`` per table a query
        references. The floor for one query is that minimum times its distinct
        table references, so a two-table join floors at twice a single scan."""

        return float(self._referenced_table_count(sql) * _MIN_BILLED_BYTES)

    def _referenced_table_count(self, sql: str) -> int:
        """Distinct physical tables a query reads, for the billing floor. A parse
        failure falls back to one table (the estimate only ever floors upward, so
        under-counting is the safe direction to be wrong)."""

        try:
            import sqlglot
            from sqlglot import expressions as sqlglot_exp

            parsed = sqlglot.parse_one(sql, read=self.dialect)
        except Exception:
            return 1
        tables = {
            ".".join(part for part in (t.catalog, t.db, t.name) if part)
            for t in parsed.find_all(sqlglot_exp.Table)
        }
        return max(len(tables), 1)

    # --- execution (the single billed door) -----------------------------------

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT, bounded in rows, wall time,
        and billed bytes (client preflight plus server-side cap)."""

        _job, iterator = self._execute(
            sql, timeout_seconds=timeout_seconds, max_results=max_rows + 1
        )
        rows = list(iterator)
        schema = list(iterator.schema)
        return QueryResult(
            columns=[field.name for field in schema],
            types=[self._render_type(field) for field in schema],
            cells=[[json_safe(v) for v in row] for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )

    def _execute(
        self,
        sql: str,
        *,
        timeout_seconds: float | None = None,
        max_results: int | None = None,
    ) -> tuple[Any, Any]:
        """SELECT-only guard, free dry-run, gate charge, then the capped run."""

        assert_select_only(sql, dialect=self.dialect)
        estimate = self._dry_run(sql)
        self.cost_gate.charge(estimate)
        return self._run(
            sql, estimate, timeout_seconds=timeout_seconds, max_results=max_results
        )

    def _run(
        self,
        sql: str,
        dry_run_estimate: float,
        *,
        timeout_seconds: float | None = None,
        max_results: int | None = None,
        _retried: bool = False,
    ) -> tuple[Any, Any]:
        """The single billed door past the gate: run with the server-side byte
        cap, wait for completion (bounded when a timeout is given), account the
        actual billed bytes, and return (job, row iterator).

        ``dry_run_estimate`` is what :meth:`_execute` already charged for this
        exact statement; a bytes-billed refusal below widens the charge by the
        gap between that estimate and what BigQuery says it actually needed,
        rather than charging the full requirement a second time on top of it.
        """

        cap = self.cost_gate.remaining_for_statement()
        if cap is not None and cap < _MIN_BILLED_BYTES:
            raise OverCeilingError(
                f"the remaining budget ({cap} bytes) is below BigQuery's "
                f"{_MIN_BILLED_BYTES}-byte minimum billed per query; raise "
                "--budget or narrow the work"
            )
        job_config = self._bq.QueryJobConfig(
            maximum_bytes_billed=cap,
            use_query_cache=True,
            labels={"app": "dex"},
        )
        job = self._client.query(
            sql, job_config=job_config, location=self.target.location
        )
        try:
            iterator = job.result(timeout=timeout_seconds, max_results=max_results)
        except self._api_exceptions.BadRequest as exc:
            if "bytes billed" in str(exc) or "bytesBilledLimitExceeded" in str(exc):
                required = _parse_bytes_billed_required(str(exc))
                if required is not None and not _retried:
                    # No dry run predicts BigQuery's own execution-time
                    # rounding of bytes billed (issue #320), so the estimate
                    # this command already charged for the statement can be
                    # a genuine underestimate even though nothing was wrong
                    # with it at dry-run time. BigQuery's own refusal names
                    # exactly what it needed; widen the charge by the gap and
                    # retry once with that as the new cap. Raises the same
                    # OverCeilingError/ConfirmationRequiredError this would
                    # raise anyway if the confirmed ceiling itself can't
                    # cover the real requirement, so a genuine over-budget
                    # query still refuses, correctly, on the real number.
                    self.cost_gate.charge(required - dry_run_estimate)
                    return self._run(
                        sql,
                        required,
                        timeout_seconds=timeout_seconds,
                        max_results=max_results,
                        _retried=True,
                    )
                raise OverCeilingError(
                    "the query would bill more than the remaining budget "
                    "(server-side maximum_bytes_billed); raise --budget or "
                    "narrow the query"
                ) from exc
            # Every other BadRequest is BigQuery refusing the statement itself
            # (an invalid query, a type it will not coerce). Typed, so the
            # envelope carries `execution_failure` and BigQuery's own words
            # rather than the `internal` an untyped API exception falls
            # through to.
            raise warehouse_refusal(str(exc)) from exc
        except TimeoutError as exc:
            # concurrent.futures.TimeoutError is the builtin on Python 3.11+.
            self._cancel(job)
            raise TimeoutError(
                f"query exceeded {timeout_seconds:g}s and was cancelled; "
                "narrow it (tighter filter, fewer columns) and retry"
            ) from exc
        self.cost_gate.record_billed(
            float(getattr(job, "total_bytes_billed", 0) or 0),
            job_id=getattr(job, "job_id", None),
            statement=sql,
        )
        return job, iterator

    def _dry_run(self, sql: str) -> float:
        job_config = self._bq.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = self._client.query(
            sql, job_config=job_config, location=self.target.location
        )
        return float(getattr(job, "total_bytes_processed", 0) or 0)

    def _request(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except self._auth_exceptions.RefreshError as exc:
            raise PrerequisiteError(str(exc)) from exc

    def _cancel(self, job: Any) -> None:
        # Best-effort: the timeout is raised regardless, and a failed cancel
        # must not mask it.
        import contextlib

        with contextlib.suppress(Exception):
            self._client.cancel_job(job.job_id, location=getattr(job, "location", None))

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(f"expected project.dataset.table, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()


def _quote_ident(name: str) -> str:
    """Quote one identifier component with backticks (dashed project IDs make
    quoting mandatory), escaping embedded backticks."""

    escaped = name.replace("`", "\\`")
    return f"`{escaped}`"


def _parse_bytes_billed_required(message: str) -> float | None:
    """The byte count BigQuery's own bytes-billed refusal names as required,
    or ``None`` if the message doesn't have the expected shape (a future
    wording change should degrade to the old flat refusal, not a crash)."""

    match = _BYTES_BILLED_REQUIRED_RE.search(message)
    return float(match.group(1)) if match else None
