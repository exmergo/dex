"""Exact comparison of two compiled SELECTs (#856).

``compare_refactors`` checks the output schema, then the row multiset, then an
optional declared key. Counts are the only values that come back. The statements
run through the query firewall, the adapter, and the cost handshake.

Both sides are stacked with ``UNION ALL`` and grouped, rather than grouped and
joined. ``GROUP BY`` already puts NULL with NULL on every warehouse, so there is
no NULL-safe join spelling to keep per dialect and no ``FULL OUTER JOIN`` to rely
on, and each side is read once. It is still exact: the statement holds no hash,
sample or approximate count. What a warehouse's grouping treats as one value
(NaN, -0.0, trailing blanks, a collation) is the warehouse's own equality, and
the connector notes in ``skills/transform/references/`` state it per connector.

An adapter without ``describe_query`` is reported, by name, as a connector the
comparison does not support. Compiling a dbt graph into the two statements stays
with :mod:`.refactor_compare`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import sqlglot.expressions as exp

from .. import command_args
from ..adapters.base import Adapter, ColumnMeta, QueryResult
from ..cache import DexCache
from ..config import QueryLimits
from ..errors import DexError
from ..guards.cost_guard import CostGuardError
from ..guards.query_firewall import QueryRefusedError, assert_query_shape, inspect_query
from ..results import ConfirmationRequest

#: Per dialect, the base type names a comparison cannot group, or cannot group
#: with a meaning anyone could call equality. A column of one is reported as
#: ``unsupported_type`` before anything runs. DuckDB 2 groups BLOB, BIT, lists,
#: structs, maps, UUID, INTERVAL, and JSON, so it has no entry to refuse.
_UNGROUPABLE_TYPES: Mapping[str, frozenset[str]] = {
    "bigquery": frozenset({"ARRAY", "STRUCT", "RECORD", "JSON", "GEOGRAPHY"}),
    "snowflake": frozenset({"VARIANT", "OBJECT", "ARRAY", "MAP"}),
    "redshift": frozenset({"SUPER", "GEOMETRY", "GEOGRAPHY", "HLLSKETCH"}),
    "databricks": frozenset({"MAP", "VARIANT"}),
    "postgres": frozenset(
        {"JSON", "XML", "POINT", "LINE", "LSEG", "BOX", "PATH", "POLYGON", "CIRCLE"}
    ),
    "trino": frozenset({"MAP", "JSON", "HYPERLOGLOG", "QDIGEST", "TDIGEST"}),
    "clickhouse": frozenset({"MAP", "JSON", "OBJECT", "AGGREGATEFUNCTION"}),
}

#: The largest count a float carries exactly.
_MAX_EXACT_FLOAT = 2**53

#: Wrappers a type name carries that say nothing about whether it groups.
_TYPE_WRAPPERS = frozenset({"NULLABLE", "LOWCARDINALITY"})

#: The working columns the statement adds. An output column of the same name
#: would collide with them on a warehouse that folds unquoted identifiers.
_RESERVED_COLUMNS = frozenset(
    {"dex_side_l", "dex_side_r", "dex_ln", "dex_rn", "dex_kl", "dex_kr", "dex_diff"}
)

ReasonCode = Literal[
    "equivalent_on_compared_inputs",
    "schemas_differ",
    "rows_differ",
    "keyed_rows_differ",
    "baseline_refused",
    "proposed_refused",
    "baseline_not_executable",
    "proposed_not_executable",
    "schema_unavailable",
    "firewall_refused",
    "unsupported_type",
    "duplicate_output_name",
    "invalid_mapping",
    "invalid_key",
    "awaiting_confirmation",
    "comparison_unreadable",
    "comparison_unsupported_on_connector",
    "baseline_not_compilable",
    "proposed_not_compilable",
    "nondeterministic_expression",
    "inputs_unsynchronized",
    "plan_applied",
    "plan_stale",
]

_VERDICT_CODES = frozenset(
    {
        "equivalent_on_compared_inputs",
        "schemas_differ",
        "rows_differ",
        "keyed_rows_differ",
    }
)
_NOT_CHECKED_CODES = frozenset(
    {
        "baseline_refused",
        "proposed_refused",
        "baseline_not_executable",
        "proposed_not_executable",
        "schema_unavailable",
        "firewall_refused",
        "unsupported_type",
        "duplicate_output_name",
        "invalid_mapping",
        "invalid_key",
        "awaiting_confirmation",
        "comparison_unreadable",
        "comparison_unsupported_on_connector",
        "baseline_not_compilable",
        "proposed_not_compilable",
        "nondeterministic_expression",
        "inputs_unsynchronized",
        "plan_applied",
        "plan_stale",
    }
)

KeyedReason = Literal["key_not_unique"]


@dataclass(frozen=True)
class MultisetDiff:
    """Aggregate multiset difference between two SELECT results.

    ``only_in_left`` / ``only_in_right`` count excess multiplicity, so a row
    that appears twice on the left and once on the right contributes 1 to
    ``only_in_left``. Equal multisets have both excesses at 0.
    """

    left_rows: int
    right_rows: int
    only_in_left: int
    only_in_right: int

    @property
    def equivalent(self) -> bool:
        return self.only_in_left == 0 and self.only_in_right == 0


@dataclass(frozen=True)
class SchemaColumn:
    """One output column, as the adapter's ``describe_query`` reports it."""

    name: str
    type_name: str


@dataclass(frozen=True)
class TypeMismatch:
    """A mapped pair whose reported types differ.

    Types are the adapter's type strings. ``INTEGER`` and ``BIGINT`` mismatch.
    There is no numeric widening and no value tolerance.
    """

    left: SchemaColumn
    right: SchemaColumn


@dataclass(frozen=True)
class SchemaDiff:
    """Name and type differences after the optional left-to-right mapping.

    Columns are matched by mapped name, not by position. Tuples keep describe
    order. A compatible diff has all three empty.
    """

    only_in_left: tuple[SchemaColumn, ...]
    only_in_right: tuple[SchemaColumn, ...]
    type_mismatches: tuple[TypeMismatch, ...]

    @property
    def compatible(self) -> bool:
        return not (self.only_in_left or self.only_in_right or self.type_mismatches)


@dataclass(frozen=True)
class KeyedCounts:
    """Key-level differences for a key that is unique on both sides.

    Each count is a number of keys, not rows. ``changed`` is a key present on
    both sides whose row multiset differs.
    """

    only_in_left: int
    only_in_right: int
    changed: int


@dataclass(frozen=True)
class RefactorCompareResult:
    """Verdict for two compiled SELECTs on one input scope.

    ``equivalent`` means the schema and the row multiset matched, and the
    declared key matched when ``keyed`` is present. ``reason_code`` is then
    ``equivalent_on_compared_inputs``. That is not a claim about a build, and
    not a claim about any input that was not compared. A declared key that is
    not unique leaves ``keyed`` empty and sets ``keyed_reason``. The multiset
    verdict still stands.

    ``different`` carries the schema diff and, when the schemas matched, the
    multiset and key counts. ``not_checked`` carries ``reason_code`` and no row
    verdict. ``detail`` is engine-authored text. A warehouse exception contributes
    its class name and nothing else. ``pending`` is set only while the cost
    handshake is waiting.
    """

    baseline_id: str | None
    proposed_id: str | None
    input_scope: str | None
    compared_at: str
    schema: SchemaDiff | None
    multiset: MultisetDiff | None
    status: Literal["equivalent", "different", "not_checked"]
    reason_code: ReasonCode
    detail: str | None = None
    keyed: KeyedCounts | None = None
    keyed_reason: KeyedReason | None = None
    pending: ConfirmationRequest | None = None

    def __post_init__(self) -> None:
        schema_ok = self.schema is not None and self.schema.compatible
        rows_ok = self.multiset is not None and self.multiset.equivalent
        keys_ok = self.keyed is not None and (
            self.keyed.only_in_left == 0
            and self.keyed.only_in_right == 0
            and self.keyed.changed == 0
        )
        if self.keyed is not None and self.keyed_reason is not None:
            raise ValueError("a keyed verdict does not also explain a dropped key")
        if self.status == "not_checked":
            if self.reason_code not in _NOT_CHECKED_CODES:
                raise ValueError("not_checked requires a not-checked reason code")
            if self.multiset is not None or self.keyed is not None or self.keyed_reason:
                raise ValueError("not_checked carries no row or key verdict")
            if (self.pending is None) == (self.reason_code == "awaiting_confirmation"):
                raise ValueError("awaiting_confirmation is the only pending result")
            return
        if self.pending is not None:
            raise ValueError("a finished comparison does not carry a pending request")
        if self.status == "equivalent":
            if self.reason_code != "equivalent_on_compared_inputs":
                raise ValueError("equivalent must say it holds for the compared inputs")
            if not (schema_ok and rows_ok):
                raise ValueError("equivalent requires a match on schema and rows")
            if self.keyed is None:
                if self.keyed_reason not in (None, "key_not_unique"):
                    raise ValueError("a dropped key must say the key was not unique")
                return
            if not keys_ok:
                raise ValueError("equivalent requires a matching reported key")
            return
        if self.status != "different":
            raise ValueError(f"unknown comparison status {self.status!r}")
        if self.schema is None or self.reason_code not in _VERDICT_CODES:
            raise ValueError("different requires a schema diff and a verdict code")
        if not self.schema.compatible:
            if (
                self.reason_code != "schemas_differ"
                or self.multiset is not None
                or self.keyed is not None
                or self.keyed_reason is not None
            ):
                raise ValueError("a schema difference does not carry a row verdict")
            return
        if self.multiset is None:
            raise ValueError("a row verdict requires the multiset counts")
        rows_differ = not rows_ok
        if self.keyed_reason == "key_not_unique":
            key_dropped = self.keyed is None and self.reason_code == "rows_differ"
            if not key_dropped or not rows_differ:
                raise ValueError("an invalid key keeps the multiset verdict only")
            return
        if self.keyed_reason is not None:
            raise ValueError("unknown keyed reason")
        if self.reason_code == "rows_differ":
            if not rows_differ:
                raise ValueError("rows_differ requires a row difference")
            return
        if self.reason_code == "keyed_rows_differ":
            if rows_differ or self.keyed is None or keys_ok:
                raise ValueError("keyed_rows_differ requires a key difference only")
            return
        raise ValueError("different requires a row or key difference")


@dataclass(frozen=True)
class _MatchedColumn:
    left_name: str
    right_name: str
    type_name: str


@dataclass(frozen=True)
class _Aligned:
    schema: SchemaDiff
    matched: tuple[_MatchedColumn, ...]


class _NotCheckedError(DexError):
    """The statements ran far enough to know they cannot be verdicted."""

    def __init__(
        self,
        reason_code: ReasonCode,
        detail: str | None = None,
        *,
        schema: SchemaDiff | None = None,
        pending: ConfirmationRequest | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail
        self.schema = schema
        self.pending = pending


def compare_selects(
    left_sql: str,
    right_sql: str,
    *,
    adapter: Adapter,
    cache: DexCache,
    limits: QueryLimits,
    command: str,
) -> MultisetDiff:
    """Compare two compiled SELECTs as multisets and return aggregate counts.

    ``left_sql`` and ``right_sql`` must be single SELECT statements the adapter
    can describe and the firewall can approve. An empty statement raises
    ``ValueError``. A comparison that cannot be verdicted raises ``ValueError``
    with the reason code or its detail.
    """

    result = compare_refactors(
        left_sql,
        right_sql,
        adapter=adapter,
        cache=cache,
        limits=limits,
        command=command,
    )
    if result.multiset is None:
        raise ValueError(result.detail or result.reason_code)
    return result.multiset


def compare_schemas(
    left_sql: str,
    right_sql: str,
    *,
    adapter: Adapter,
    cache: DexCache,
    limits: QueryLimits,
    command: str,
    column_mapping: Mapping[str, str] | None = None,
) -> SchemaDiff:
    """Compare output names and reported types.

    ``column_mapping`` maps a baseline output name to a proposed output name.
    Unmapped baseline columns match a proposed column of the same name.
    Column order is not a difference. An empty statement, a mapping that is
    not one-to-one, or a mapping that collapses two baseline columns raises
    ``ValueError``. A statement the firewall or the adapter cannot describe
    raises ``ValueError`` with the engine's detail.

    ``cache``, ``limits``, and ``command`` match ``compare_refactors``. This
    function stops after the schema, so it does not price a count.
    """

    left, right = _require_sql(left_sql, right_sql)
    mapping = _column_mapping(column_mapping)
    try:
        _assert_shapes(left, right, adapter)
        if not _describe_is_free(adapter):
            raise _NotCheckedError(
                "schema_unavailable",
                "describing a query costs compute on this connector, so it runs "
                "only behind compare_refactors' cost handshake",
            )
        left_cols, right_cols = _describe_pair(left, right, adapter)
        return _align(left_cols, right_cols, mapping).schema
    except _NotCheckedError as exc:
        raise ValueError(exc.detail or exc.reason_code) from exc


def compare_refactors(
    left_sql: str,
    right_sql: str,
    *,
    adapter: Adapter,
    cache: DexCache,
    limits: QueryLimits,
    command: str,
    baseline_id: str | None = None,
    proposed_id: str | None = None,
    input_scope: str | None = None,
    column_mapping: Mapping[str, str] | None = None,
    key_columns: Sequence[str] | None = None,
    prepriced: bool = False,
) -> RefactorCompareResult:
    """Compare two compiled SELECTs on schema, multiset, and an optional key.

    ``key_columns`` are baseline output names. Each is mapped with
    ``column_mapping`` when the proposed side renamed it. Counts are keys, and
    only a key that is unique on both sides is reported. Omit the key, or pass
    an empty sequence, for a multiset comparison only.

    ``equivalent`` means these statements matched on the compared inputs.
    It is not evidence that a build passed. A statement the firewall refuses,
    a statement the adapter cannot describe, a duplicate output name, a mapping
    key that is not a baseline output, or a declared key that is not an output
    column is ``not_checked``. An empty statement raises ``ValueError``.

    ``prepriced`` says the caller already took this comparison through one
    handshake, as a command that had to profile the inputs first does. The
    statements still charge the gate as they run.
    """

    left, right = _require_sql(left_sql, right_sql)
    mapping = _column_mapping(column_mapping)
    keys = _key_columns(key_columns)
    compared_at = datetime.now(UTC).isoformat()

    def finish(
        *,
        status: Literal["equivalent", "different", "not_checked"],
        reason_code: ReasonCode,
        detail: str | None = None,
        schema: SchemaDiff | None = None,
        multiset: MultisetDiff | None = None,
        keyed: KeyedCounts | None = None,
        keyed_reason: KeyedReason | None = None,
        pending: ConfirmationRequest | None = None,
    ) -> RefactorCompareResult:
        return RefactorCompareResult(
            baseline_id=baseline_id,
            proposed_id=proposed_id,
            input_scope=input_scope,
            compared_at=compared_at,
            schema=schema,
            multiset=multiset,
            status=status,
            reason_code=reason_code,
            detail=detail,
            keyed=keyed,
            keyed_reason=keyed_reason,
            pending=pending,
        )

    try:
        return _compare(
            left,
            right,
            mapping,
            keys,
            adapter,
            cache,
            limits,
            command,
            finish,
            prepriced=prepriced,
        )
    except _NotCheckedError as exc:
        return finish(
            status="not_checked",
            reason_code=exc.reason_code,
            detail=exc.detail,
            schema=exc.schema,
            pending=exc.pending,
        )


def _compare(
    left: str,
    right: str,
    mapping: Mapping[str, str],
    keys: tuple[str, ...] | None,
    adapter: Adapter,
    cache: DexCache,
    limits: QueryLimits,
    command: str,
    finish,
    *,
    prepriced: bool = False,
):
    _assert_shapes(left, right, adapter)
    if not callable(getattr(adapter, "describe_query", None)):
        raise _NotCheckedError(
            "comparison_unsupported_on_connector",
            f"the {adapter.name} connector does not describe query output",
        )
    # Reading the schema can wake paid compute on some connectors. There the
    # statements are priced as written, which is what the comparison reads, and
    # nothing reaches the warehouse until that is confirmed.
    if not prepriced and not _describe_is_free(adapter):
        raw_estimate = _raw_estimate(left, right, adapter, keyed=keys is not None)
        pending = command_args.confirmation_request(
            command,
            adapter,
            raw_estimate,
            per_table={"(refactor comparison)": raw_estimate},
            notes=[
                "reading the output schema runs on the warehouse here, so this "
                "estimate covers the schema read and the comparison together"
            ],
        )
        if pending is not None:
            raise _NotCheckedError("awaiting_confirmation", pending=pending)
        prepriced = True

    left_cols, right_cols = _describe_pair(left, right, adapter)
    aligned = _align(left_cols, right_cols, mapping)
    schema = aligned.schema
    if not schema.compatible:
        return finish(status="different", reason_code="schemas_differ", schema=schema)

    unsupported = _unsupported_column(aligned.matched, adapter.dialect)
    if unsupported is not None:
        raise _NotCheckedError(
            "unsupported_type",
            unsupported,
            schema=schema,
        )
    if not aligned.matched:
        raise _NotCheckedError(
            "comparison_unreadable",
            "query returns no columns",
            schema=schema,
        )

    if keys is not None:
        described = [column.left_name for column in aligned.matched]
        keys = tuple(_as_described(key, described) for key in keys)
        problem = _key_problem(keys, described)
        if problem is not None:
            raise _NotCheckedError("invalid_key", problem, schema=schema)

    reserved = [
        column.left_name
        for column in aligned.matched
        if column.left_name.lower() in _RESERVED_COLUMNS
    ]
    if reserved:
        raise _NotCheckedError(
            "comparison_unreadable",
            f"output column name is reserved by the comparison: {reserved[0]!r}",
            schema=schema,
        )

    left_names = [column.left_name for column in aligned.matched]
    right_names = [column.right_name for column in aligned.matched]
    dialect = adapter.dialect
    left_proj = _project(left, left_names, left_names, dialect)
    right_proj = _project(right, right_names, left_names, dialect)
    statements = [_multiset_sql(left_proj, right_proj, left_names, dialect)]
    if keys is not None:
        statements.append(_keyed_sql(left_proj, right_proj, left_names, keys, dialect))

    approved: list[str] = []
    for sql in statements:
        try:
            inspected = inspect_query(sql, cache, limits, dialect=dialect)
        except QueryRefusedError as exc:
            raise _NotCheckedError("firewall_refused", str(exc), schema=schema) from exc
        approved.append(inspected.sql)

    if not prepriced:
        estimates = _estimates(approved, adapter, schema)
        per_table = {"(refactor comparison)": estimates[0]}
        if len(estimates) == 2:
            per_table["(refactor key comparison)"] = estimates[1]
        pending = command_args.confirmation_request(
            command,
            adapter,
            sum(estimates),
            per_table=per_table,
            notes=["one confirmation covers the refactor comparison"],
        )
        if pending is not None:
            raise _NotCheckedError(
                "awaiting_confirmation",
                schema=schema,
                pending=pending,
            )

    try:
        multiset_row = adapter.run_query(
            approved[0], max_rows=1, timeout_seconds=limits.timeout_seconds
        )
    except CostGuardError:
        raise
    except Exception as exc:
        raise _NotCheckedError(
            "comparison_unreadable", type(exc).__name__, schema=schema
        ) from exc
    multiset = _multiset_from_counts(_read_counts(multiset_row, 4))

    keyed: KeyedCounts | None = None
    keyed_reason: KeyedReason | None = None
    if len(approved) == 2:
        try:
            keyed_row = adapter.run_query(
                approved[1], max_rows=1, timeout_seconds=limits.timeout_seconds
            )
        except CostGuardError:
            raise
        except Exception as exc:
            raise _NotCheckedError(
                "comparison_unreadable", type(exc).__name__, schema=schema
            ) from exc
        keyed, keyed_reason = _keyed_from_counts(_read_counts(keyed_row, 4))

    rows_differ = not multiset.equivalent
    keys_differ = keyed is not None and (
        keyed.only_in_left != 0 or keyed.only_in_right != 0 or keyed.changed != 0
    )
    if rows_differ or keys_differ:
        reason_code: ReasonCode = "rows_differ" if rows_differ else "keyed_rows_differ"
        return finish(
            status="different",
            reason_code=reason_code,
            schema=schema,
            multiset=multiset,
            keyed=keyed,
            keyed_reason=keyed_reason,
        )
    return finish(
        status="equivalent",
        reason_code="equivalent_on_compared_inputs",
        schema=schema,
        multiset=multiset,
        keyed=keyed,
        keyed_reason=keyed_reason,
    )


def _require_sql(left_sql: str, right_sql: str) -> tuple[str, str]:
    left = left_sql.strip().rstrip(";")
    right = right_sql.strip().rstrip(";")
    if not left or not right:
        raise ValueError("both SELECT statements are required")
    return left, right


def _column_mapping(column_mapping: Mapping[str, str] | None) -> dict[str, str]:
    if not column_mapping:
        return {}
    if len(set(column_mapping.values())) != len(column_mapping):
        raise ValueError("column mapping is not one-to-one")
    normalized: dict[str, str] = {}
    for left_name, right_name in column_mapping.items():
        if (
            not isinstance(left_name, str)
            or not isinstance(right_name, str)
            or not left_name
            or not right_name
        ):
            raise ValueError("column mapping names must be non-empty strings")
        normalized[left_name] = right_name
    return normalized


def _key_columns(key_columns: Sequence[str] | None) -> tuple[str, ...] | None:
    if key_columns is None:
        return None
    if isinstance(key_columns, str):
        raise ValueError("key_columns must be a sequence of column names")
    keys = tuple(key_columns)
    if not keys:
        return None
    for key in keys:
        if not isinstance(key, str) or not key:
            raise ValueError("key_columns must be a sequence of column names")
    return keys


def _assert_shapes(left: str, right: str, adapter: Adapter) -> None:
    try:
        assert_query_shape(left, dialect=adapter.dialect)
    except QueryRefusedError as exc:
        raise _NotCheckedError("baseline_refused", str(exc)) from exc
    try:
        assert_query_shape(right, dialect=adapter.dialect)
    except QueryRefusedError as exc:
        raise _NotCheckedError("proposed_refused", str(exc)) from exc


def _describe_is_free(adapter: Adapter) -> bool:
    """Whether reading a query's output schema can bill anything here."""

    return bool(getattr(adapter, "describe_query_free", True))


def _estimates(
    statements: Sequence[str], adapter: Adapter, schema: SchemaDiff | None = None
) -> list[float]:
    estimator = getattr(adapter, "query_estimate", None)
    if not callable(estimator):
        return [0.0 for _ in statements]
    try:
        return [float(estimator(sql)) for sql in statements]
    except CostGuardError:
        raise
    except Exception as exc:
        # An estimator that plans the statement can quote the warehouse's words.
        raise _NotCheckedError(
            "comparison_unreadable", type(exc).__name__, schema=schema
        ) from exc


def _raw_estimate(left: str, right: str, adapter: Adapter, *, keyed: bool) -> float:
    """The comparison priced from the two statements as written.

    The comparison reads each side once per statement, so this is what the schema
    was not yet available to build.
    """

    reads = sum(_estimates([left, right], adapter))
    return reads * (2 if keyed else 1)


def _describe_pair(
    left: str, right: str, adapter: Adapter
) -> tuple[tuple[ColumnMeta, ...], tuple[ColumnMeta, ...]]:
    describe = getattr(adapter, "describe_query", None)
    if not callable(describe):
        raise _NotCheckedError(
            "comparison_unsupported_on_connector",
            f"the {adapter.name} connector does not describe query output",
        )
    return (
        _describe_side(describe, left, "baseline_not_executable"),
        _describe_side(describe, right, "proposed_not_executable"),
    )


def _describe_side(describe, sql: str, code: ReasonCode) -> tuple[ColumnMeta, ...]:
    try:
        columns = tuple(describe(sql))
    except CostGuardError:
        raise
    except Exception as exc:
        # Class name only. DuckDB conversion errors quote the offending value.
        raise _NotCheckedError(code, type(exc).__name__) from exc
    return columns


def _as_described(name: str, described: Iterable[str]) -> str:
    """A caller's column name as the warehouse spells it.

    Snowflake reports ``TEAM_ID`` for what its author wrote as ``team_id``. An
    exact match wins, and a spelling that differs only in case is taken when it
    names one column. Two columns that differ only in case stay the caller's to
    tell apart.
    """

    names = list(described)
    if name in names:
        return name
    folded = [candidate for candidate in names if candidate.lower() == name.lower()]
    return folded[0] if len(folded) == 1 else name


def _reject_duplicate_names(columns: Sequence[ColumnMeta], side: str) -> None:
    seen: set[str] = set()
    for column in columns:
        if column.name in seen:
            raise _NotCheckedError(
                "duplicate_output_name",
                f"duplicate output column name {column.name!r} on {side}",
            )
        seen.add(column.name)


def _align(
    left_cols: Sequence[ColumnMeta],
    right_cols: Sequence[ColumnMeta],
    mapping: Mapping[str, str],
) -> _Aligned:
    _reject_duplicate_names(left_cols, "baseline")
    _reject_duplicate_names(right_cols, "proposed")
    if not left_cols or not right_cols:
        if not left_cols:
            raise _NotCheckedError(
                "baseline_not_executable", "query returns no columns"
            )
        raise _NotCheckedError("proposed_not_executable", "query returns no columns")
    left_names = {column.name for column in left_cols}
    right_names = [column.name for column in right_cols]
    mapping = {
        _as_described(left, left_names): _as_described(right, right_names)
        for left, right in mapping.items()
    }
    unknown = [name for name in mapping if name not in left_names]
    if unknown:
        shown = ", ".join(repr(name) for name in unknown)
        raise _NotCheckedError(
            "invalid_mapping",
            f"column mapping names a baseline column that is not an output: {shown}",
        )

    targets = [mapping.get(column.name, column.name) for column in left_cols]
    if len(targets) != len(set(targets)):
        raise ValueError(
            "column mapping resolves two baseline columns to one proposed column"
        )

    right_by_name = {column.name: column for column in right_cols}
    unmatched_right = {column.name for column in right_cols}
    only_left: list[SchemaColumn] = []
    mismatches: list[TypeMismatch] = []
    matched: list[_MatchedColumn] = []
    for column, target in zip(left_cols, targets, strict=True):
        right = right_by_name.get(target)
        left_schema = SchemaColumn(column.name, column.data_type)
        if right is None:
            only_left.append(left_schema)
            continue
        unmatched_right.discard(target)
        right_schema = SchemaColumn(right.name, right.data_type)
        if _normal_type(column.data_type) != _normal_type(right.data_type):
            mismatches.append(TypeMismatch(left=left_schema, right=right_schema))
            continue
        matched.append(_MatchedColumn(column.name, right.name, column.data_type))
    only_right = tuple(
        SchemaColumn(column.name, column.data_type)
        for column in right_cols
        if column.name in unmatched_right
    )
    return _Aligned(
        schema=SchemaDiff(
            only_in_left=tuple(only_left),
            only_in_right=only_right,
            type_mismatches=tuple(mismatches),
        ),
        matched=tuple(matched),
    )


def _normal_type(type_name: str) -> str:
    """One spelling of a type, so ``varchar`` and ``VARCHAR`` are not a change."""

    return " ".join(type_name.split()).upper()


def _base_type(type_name: str) -> str:
    """The outermost type a name states, past wrappers that do not affect grouping."""

    text = _normal_type(type_name)
    while True:
        base = re.split(r"[(<\[ ]", text, maxsplit=1)[0]
        if base not in _TYPE_WRAPPERS or "(" not in text:
            return base
        text = text.split("(", 1)[1].rstrip(") ")


def _unsupported_column(columns: Sequence[_MatchedColumn], dialect: str) -> str | None:
    refused = _UNGROUPABLE_TYPES.get(dialect, frozenset())
    for column in columns:
        base = _base_type(column.type_name)
        # A bare number is a type code the driver could not name, so nothing here
        # can say it groups.
        if base in refused or base.isdigit():
            return f"{column.left_name} ({column.type_name})"
    return None


def _quote(name: str, dialect: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _project(
    sql: str,
    source_names: Sequence[str],
    output_names: Sequence[str],
    dialect: str,
) -> str:
    """Select ``source_names`` under ``output_names``, in that order.

    Both sides are projected into one order and one set of names before the
    multiset comparison, so a rename or a reordering is not a changed row.
    """

    pairs = zip(source_names, output_names, strict=True)
    select_list = ", ".join(
        f"{_quote(source, dialect)} as {_quote(output, dialect)}"
        for source, output in pairs
    )
    # Names are quoted identifiers. sql already passed the SELECT shape check.
    return f"select {select_list} from ({sql}) as dex_proj"  # noqa: S608


def _rows_cte(left_proj: str, right_proj: str, column_list: str) -> str:
    """Each distinct row, with how many times each side holds it.

    Grouping is what makes this NULL-safe on every connector, and each side is
    read once. Every working column has its own name because ClickHouse resolves
    an alias inside the expressions beside it, which would nest the aggregates.
    The projections are quoted identifiers over statements the
    firewall has already shaped.
    """

    return (
        "dex_stacked as (\n"  # noqa: S608
        f"    select {column_list}, 1 as dex_side_l, 0 as dex_side_r\n"
        f"    from ({left_proj}) as dex_left\n"
        "    union all\n"
        f"    select {column_list}, 0 as dex_side_l, 1 as dex_side_r\n"
        f"    from ({right_proj}) as dex_right\n"
        "),\n"
        "dex_rows as (\n"
        f"    select {column_list},\n"
        "           sum(dex_side_l) as dex_ln, sum(dex_side_r) as dex_rn\n"
        "    from dex_stacked\n"
        f"    group by {column_list}\n"
        ")"
    )


def _count(expression: str, alias: str) -> str:
    # Cast because several warehouses widen a sum to a decimal, and json_safe
    # would carry a decimal as a float, which is not an exact count.
    return f"cast(coalesce({expression}, 0) as bigint) as {alias}"


def _multiset_sql(
    left_proj: str,
    right_proj: str,
    columns: Sequence[str],
    dialect: str,
) -> str:
    column_list = ", ".join(_quote(name, dialect) for name in columns)
    excess_left = "sum(case when dex_ln > dex_rn then dex_ln - dex_rn else 0 end)"
    excess_right = "sum(case when dex_rn > dex_ln then dex_rn - dex_ln else 0 end)"
    selected = ",\n    ".join(
        (
            _count("sum(dex_ln)", "left_rows"),
            _count("sum(dex_rn)", "right_rows"),
            _count(excess_left, "only_in_left"),
            _count(excess_right, "only_in_right"),
        )
    )
    return (
        f"with {_rows_cte(left_proj, right_proj, column_list)}\n"
        f"select\n    {selected}\n"
        "from dex_rows\n"
    )


def _keyed_sql(
    left_proj: str,
    right_proj: str,
    columns: Sequence[str],
    keys: Sequence[str],
    dialect: str,
) -> str:
    column_list = ", ".join(_quote(name, dialect) for name in columns)
    key_list = ", ".join(_quote(name, dialect) for name in keys)
    selected = ",\n    ".join(
        (
            _count(
                "sum(case when dex_kl > 1 or dex_kr > 1 then 1 else 0 end)",
                "non_unique_keys",
            ),
            _count(
                "sum(case when dex_kl > 0 and dex_kr = 0 then 1 else 0 end)",
                "only_in_left",
            ),
            _count(
                "sum(case when dex_kr > 0 and dex_kl = 0 then 1 else 0 end)",
                "only_in_right",
            ),
            _count(
                "sum(case when dex_kl > 0 and dex_kr > 0 and dex_diff = 1 "
                "then 1 else 0 end)",
                "changed",
            ),
        )
    )
    return (
        f"with {_rows_cte(left_proj, right_proj, column_list)},\n"  # noqa: S608
        "dex_keys as (\n"
        f"    select {key_list}, sum(dex_ln) as dex_kl, sum(dex_rn) as dex_kr,\n"
        "           max(case when dex_ln <> dex_rn then 1 else 0 end) as dex_diff\n"
        f"    from dex_rows group by {key_list}\n"
        ")\n"
        f"select\n    {selected}\n"
        "from dex_keys\n"
    )


def _key_problem(keys: Sequence[str], names: Sequence[str]) -> str | None:
    repeated = tuple(dict.fromkeys(key for key in keys if keys.count(key) > 1))
    if repeated:
        shown = ", ".join(repr(key) for key in repeated)
        return f"declared key repeats a column: {shown}"
    present = set(names)
    missing = tuple(key for key in keys if key not in present)
    if missing:
        shown = ", ".join(repr(key) for key in missing)
        return f"declared key is not an output column: {shown}"
    return None


def _read_counts(result: QueryResult, width: int) -> list[int]:
    if result.truncated or len(result.cells) != 1:
        raise _NotCheckedError(
            "comparison_unreadable",
            "comparison did not return one row",
        )
    row = result.cells[0]
    if len(row) != width:
        raise _NotCheckedError(
            "comparison_unreadable",
            "comparison returned the wrong number of counts",
        )
    values: list[int] = []
    for value in row:
        count = _exact_count(value)
        if count is None:
            raise _NotCheckedError(
                "comparison_unreadable",
                "comparison returned a count that is not a non-negative integer",
            )
        values.append(count)
    return values


def _exact_count(value: object) -> int | None:
    """A count as an int, or ``None`` when it cannot be read exactly.

    A driver can hand back a cast bigint as a string, or as a float through
    json_safe. A float is taken only inside the range it represents exactly.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value.is_integer() and 0 <= value <= _MAX_EXACT_FLOAT:
            return int(value)
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _multiset_from_counts(values: Sequence[int]) -> MultisetDiff:
    left_rows, right_rows, only_in_left, only_in_right = values
    if left_rows - right_rows != only_in_left - only_in_right:
        raise _NotCheckedError(
            "comparison_unreadable",
            "comparison counts are inconsistent",
        )
    return MultisetDiff(
        left_rows=left_rows,
        right_rows=right_rows,
        only_in_left=only_in_left,
        only_in_right=only_in_right,
    )


def _keyed_from_counts(
    values: Sequence[int],
) -> tuple[KeyedCounts | None, KeyedReason | None]:
    non_unique, only_in_left, only_in_right, changed = values
    if non_unique > 0:
        return None, "key_not_unique"
    return (
        KeyedCounts(
            only_in_left=only_in_left,
            only_in_right=only_in_right,
            changed=changed,
        ),
        None,
    )
