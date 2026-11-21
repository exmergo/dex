"""Freshness: how old the newest value of a declared time column may be (#1460).

A declaration states it on the column with role ``time``::

    columns:
      ordered_at:
        role: time
        freshness: {max_lag: 6h, basis: capture}

``maintain verify`` (and ``transform build --verify``) reads the column's newest
value and the warehouse's own clock in one statement, and raises
``freshness_exceeded`` where the newest value trails the clock by more than
``max_lag``, or where the column holds no value at all: no rows is not fresh.
The generated test ``dex_freshness`` (``transform/assets/macros``) sends the
same expressions per warehouse, so the two agree on every connector.

The clock is the warehouse's, never the runner's: a test runs in a production
job Dex does not see, and the warehouse is the one clock both checks share.
Each side of the lag is a whole number of seconds since the Unix epoch, floored
on its own, so a value exactly ``max_lag`` behind the clock (a view computing
``current_timestamp - interval``) passes on every warehouse. A timestamp with no
time zone is read as UTC, as dbt's source freshness reads one, and a date as its
UTC midnight.

``basis`` does not change the verdict. It changes what the finding says and how
loud it is: a stale ``capture`` time means loading stopped (high), while a stale
``business`` time can be a quiet day, late events or a backfill (medium).

Counts only: the statement returns the lag in seconds and two counts, never a
value from the column, so the check runs in values-free mode unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .declared_facts import DeclaredFactsResult, Prepared, _Probe
    from .drift import DriftFinding

#: The finding code both checks agree on.
CODE = "freshness_exceeded"

#: Why a declared freshness was not measured, as a record's ``reason_code``.
NOT_TEMPORAL = "not_temporal"
COLUMN_MISSING = "column_missing"
LAG_UNREADABLE = "lag_unreadable"


@dataclass(frozen=True)
class FreshnessRule:
    """One declared ``freshness``: its column as declared, the lag in seconds
    and as written (normalized), and the basis."""

    column: str
    max_lag: str
    max_lag_seconds: int
    basis: str


def read_rule(column: str, value: Any) -> FreshnessRule | None:
    """The rule a column's ``dex.freshness`` states, or ``None`` where it states
    none or one the declaration schema would refuse (a hand edit): an unreadable
    rule is never guessed into a check."""

    from ..semantic_declarations import lag_seconds, normalized_lag

    if not isinstance(value, dict):
        return None
    lag, basis = value.get("max_lag"), value.get("basis")
    if not isinstance(lag, str) or basis not in ("business", "capture"):
        return None
    try:
        seconds = lag_seconds(lag)
    except ValueError:
        return None
    return FreshnessRule(column, normalized_lag(seconds), seconds, basis)


# --- the expressions, per warehouse ----------------------------------------------
#
# Each mirrors a `<adapter>__dex_freshness_epoch` / `_now` macro in
# transform/assets/macros/dex_freshness.sql; change one, change the other.


def temporal_kind(dialect: str, data_type: str | None) -> str | None:
    """``date``, ``ntz`` (a timestamp with no time zone), ``tz`` (an instant),
    ``unknown`` where no type was read, or ``None`` where the column is no date
    or timestamp (a time of day, an interval, text, a number)."""

    if data_type is None:
        return "unknown"
    upper = data_type.upper()
    if "INTERVAL" in upper:
        return None
    if "TIMESTAMP" in upper or "DATETIME" in upper:
        if "WITHOUT TIME ZONE" in upper or "NTZ" in upper:
            return "ntz"
        if (
            "WITH TIME ZONE" in upper
            or "WITH LOCAL TIME ZONE" in upper
            or "TIMESTAMPTZ" in upper
            or "TIMESTAMP_TZ" in upper
            or "TIMESTAMP_LTZ" in upper
        ):
            return "tz"
        # A bare TIMESTAMP is an instant on BigQuery and Databricks, and
        # ClickHouse's DateTime stores one; elsewhere it carries no zone.
        # BigQuery's DATETIME has none.
        if dialect in ("databricks", "clickhouse"):
            return "tz"
        if dialect == "bigquery":
            return "ntz" if "DATETIME" in upper else "tz"
        return "ntz"
    if "DATE" in upper:
        return "date"
    return None


def epoch_sql(dialect: str, expression: str, kind: str) -> str:
    """Whole seconds since the Unix epoch of ``expression`` (a date or
    timestamp), floored, with a value carrying no time zone read as UTC."""

    x = expression
    if dialect == "bigquery":
        # CAST reads a DATE or DATETIME as UTC and keeps a TIMESTAMP as it is.
        return f"UNIX_SECONDS(CAST({x} AS TIMESTAMP))"
    if dialect == "snowflake":
        if kind == "date":
            x = f"CAST({x} AS TIMESTAMP_NTZ)"
        return f"DATE_PART(EPOCH_SECOND, {x})"
    if dialect == "databricks":
        if kind == "date":
            return f"CAST(UNIX_DATE({x}) AS BIGINT) * 86400"
        if kind == "ntz":
            # A TIMESTAMP_NTZ's wall clock read as UTC, whatever the session's
            # time zone, which a cast to TIMESTAMP would apply.
            return (
                f"CAST(UNIX_DATE(CAST({x} AS DATE)) AS BIGINT) * 86400 "
                f"+ HOUR({x}) * 3600 + MINUTE({x}) * 60 "
                f"+ CAST(FLOOR(SECOND({x})) AS BIGINT)"
            )
        return f"UNIX_SECONDS(CAST({x} AS TIMESTAMP))"
    if dialect == "clickhouse":
        return f"toInt64(toUnixTimestamp(toDateTime({x}, 'UTC')))"
    if dialect == "trino":
        if kind == "date":
            x = f"WITH_TIMEZONE(CAST({x} AS TIMESTAMP), 'UTC')"
        elif kind == "ntz":
            x = f"WITH_TIMEZONE({x}, 'UTC')"
        return f"CAST(FLOOR(TO_UNIXTIME({x})) AS BIGINT)"
    if dialect in ("postgres", "redshift"):
        if kind == "date":
            x = f"CAST({x} AS TIMESTAMP)"
        return f"CAST(FLOOR(EXTRACT(EPOCH FROM {x})) AS BIGINT)"
    # DuckDB, and the default.
    if kind == "date":
        x = f"CAST({x} AS TIMESTAMP)"
    return f"CAST(FLOOR(EPOCH({x})) AS BIGINT)"


def now_sql(dialect: str) -> str:
    """The warehouse's clock in whole epoch seconds, read once per statement."""

    return {
        "bigquery": "UNIX_SECONDS(CURRENT_TIMESTAMP())",
        "snowflake": "DATE_PART(EPOCH_SECOND, CURRENT_TIMESTAMP())",
        "databricks": "UNIX_SECONDS(CURRENT_TIMESTAMP())",
        "clickhouse": "toInt64(toUnixTimestamp(now()))",
        "trino": "CAST(FLOOR(TO_UNIXTIME(CURRENT_TIMESTAMP)) AS BIGINT)",
        "postgres": "CAST(FLOOR(EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)) AS BIGINT)",
        # Redshift's clock, as dbt-redshift reads it (`current_timestamp()`):
        # the session's time zone, UTC unless a user or session sets another.
        "redshift": "CAST(FLOOR(EXTRACT(EPOCH FROM GETDATE())) AS BIGINT)",
    }.get(dialect, "CAST(FLOOR(EPOCH(CURRENT_TIMESTAMP)) AS BIGINT)")


def lag_sql(dialect: str, quoted_column: str, kind: str) -> str:
    """The lag, in seconds, of the column's newest value behind the clock."""

    return (
        f"({now_sql(dialect)}) - ({epoch_sql(dialect, f'MAX({quoted_column})', kind)})"
    )


def _quote(dialect: str, name: str) -> str:
    import sqlglot.expressions as exp

    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _relation(dialect: str, identifier: str) -> str:
    return ".".join(_quote(dialect, part) for part in identifier.split("."))


# --- the statement ------------------------------------------------------------------


@dataclass
class Written:
    """One model's freshness statement and which rule each alias measures."""

    sql: str
    #: (alias index, rule, the relation's spelling of the column, its type)
    rules: list[tuple[int, FreshnessRule, str, str | None]]


def write_probe(
    probe: _Probe,
    live: dict[str, tuple[str, str]] | None,
    dialect: str,
    prepared: Prepared,
) -> None:
    """Write the statement measuring every freshness ``probe``'s model declares,
    or record why a rule gets none. Free: the columns were already read."""

    from ..guards.sql_guard import assert_select_only

    model = probe.model
    parts = ["COUNT(*) AS dex_rows"]
    rules: list[tuple[int, FreshnessRule, str, str | None]] = []
    for index, rule in enumerate(model.freshness):
        if live is None:
            spelled, data_type = rule.column, model_type(model, rule.column)
        else:
            found = live.get(rule.column.lower())
            if found is None:
                why = (
                    f"the declared freshness on {model.model}.{rule.column} was not "
                    "checked: the built relation has no such column"
                )
                prepared.notes.append(why)
                prepared.dropped["freshness"].append(f"{model.model}: {why}")
                prepared.freshness_unwritten[(model.model, rule.column)] = (
                    COLUMN_MISSING,
                    "the built relation has no such column",
                )
                continue
            spelled, data_type = found
        kind = temporal_kind(dialect, data_type)
        if kind is None:
            why = (
                f"the declared freshness on {model.model}.{rule.column} was not "
                f"checked: the column is {data_type}, not a date or timestamp"
            )
            prepared.notes.append(why)
            prepared.dropped["freshness"].append(f"{model.model}: {why}")
            prepared.freshness_unwritten[(model.model, rule.column)] = (
                NOT_TEMPORAL,
                f"the column is {data_type}, not a date or timestamp",
            )
            continue
        q = _quote(dialect, spelled)
        parts.append(
            f"COUNT({q}) AS dex_fresh_n_{index}, "
            f"{lag_sql(dialect, q, kind)} AS dex_fresh_lag_{index}"
        )
        rules.append((index, rule, spelled, data_type))
    if not rules:
        return
    sql = f"SELECT {', '.join(parts)} FROM {_relation(dialect, probe.identifier)}"  # noqa: S608
    probe.freshness = Written(assert_select_only(sql, dialect=dialect), rules)


def model_type(model: Any, column: str) -> str | None:
    return dict(model.declared_types).get(column.lower())


# --- judging ------------------------------------------------------------------------


def run_probe(
    adapter,
    probe: _Probe,
    result: DeclaredFactsResult,
    checked: dict[str, int],
    prepared: Prepared,
    timeout_seconds: float | None,
    pii_overrides: Any = None,
) -> None:
    """Run ``probe``'s freshness statement and judge each rule it measures."""

    from ..guards.cost_guard import CostGuardError
    from .declared_facts import _refusal

    written = probe.freshness
    if written is None:  # pragma: no cover - callers check
        return
    model = probe.model.model
    try:
        answer = adapter.run_query(
            written.sql, max_rows=1, timeout_seconds=timeout_seconds
        )
    except CostGuardError:
        raise
    except Exception as exc:
        detail = _refusal(exc)
        result.notes.append(
            f"the declared freshness on {model} could not be measured: the "
            f"warehouse refused the statement ({detail})"
        )
        prepared.dropped["freshness"].append(
            f"{model}: the warehouse refused the check ({detail})"
        )
        for _index, rule, _spelled, _type in written.rules:
            result.freshness.append(
                record(
                    model,
                    rule,
                    "unchecked",
                    reason_code="probe_refused",
                    reason=f"the warehouse refused the statement ({detail})",
                )
            )
        return
    row = dict(zip(answer.columns, answer.cells[0], strict=True))
    row = {str(k).lower(): v for k, v in row.items()}
    rows = _int(row.get("dex_rows")) or 0
    for index, rule, spelled, data_type in written.rules:
        values = _int(row.get(f"dex_fresh_n_{index}")) or 0
        lag = _int(row.get(f"dex_fresh_lag_{index}"))
        # The lag is the clock minus the newest value, so it discloses that
        # value: a column that looks like personal data keeps its verdict and
        # withholds the number, as the unit check withholds a range.
        shown = _lag_shown(probe.identifier, spelled, data_type, pii_overrides)
        if rows == 0 or values == 0:
            checked["freshness"] += 1
            reason = "no_rows" if rows == 0 else "all_null"
            result.freshness.append(
                record(model, rule, "contradicted", reason_code=reason, rows=rows)
            )
            result.findings.append(finding(model, rule, None, reason, rows))
            continue
        if lag is None:
            # Values, but no lag a warehouse could compute (an infinite
            # timestamp reads as NULL on DuckDB): no verdict either way.
            why = (
                "the column holds values, but the warehouse returned no lag for "
                "its newest one (an infinite or out-of-range timestamp)"
            )
            result.notes.append(
                f"the declared freshness on {model}.{rule.column} was not "
                f"checked: {why}"
            )
            prepared.dropped["freshness"].append(f"{model}: {why}")
            result.freshness.append(
                record(
                    model,
                    rule,
                    "unchecked",
                    rows=rows,
                    reason_code=LAG_UNREADABLE,
                    reason=why,
                )
            )
            continue
        checked["freshness"] += 1
        if lag > rule.max_lag_seconds:
            result.freshness.append(
                record(
                    model, rule, "contradicted", lag_seconds=lag, rows=rows, shown=shown
                )
            )
            result.findings.append(finding(model, rule, lag, None, rows, shown=shown))
            continue
        result.freshness.append(
            record(model, rule, "satisfied", lag_seconds=lag, rows=rows, shown=shown)
        )


def _lag_shown(
    identifier: str, column: str, data_type: str | None, pii_overrides: Any
) -> bool:
    """Whether a column's lag may be shown: not where the column looks like
    personal data, unless a human cleared it in ``pii_overrides``."""

    from ..explore.profile import detect_pii, is_min_max_safe

    path = f"{identifier}.{column}".lower()
    if pii_overrides is not None and path in pii_overrides:
        return True
    kind = data_type or "TIMESTAMP"
    return is_min_max_safe(kind, detect_pii(column, kind))


def record(
    model: str,
    rule: FreshnessRule,
    status: str,
    *,
    lag_seconds: int | None = None,
    rows: int | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    shown: bool = True,
) -> dict[str, Any]:
    """A ``data.freshness`` record: the declaration, what was measured, and a
    status of ``satisfied``, ``contradicted`` or ``unchecked``."""

    out: dict[str, Any] = {
        "model": model,
        "column": rule.column,
        "max_lag": rule.max_lag,
        "max_lag_seconds": rule.max_lag_seconds,
        "basis": rule.basis,
        "status": status,
    }
    if status == "contradicted":
        out["finding"] = CODE
    if rows is not None:
        out["rows"] = rows
    if lag_seconds is not None and not shown:
        out["lag_withheld"] = True
    elif lag_seconds is not None:
        out["lag_seconds"] = lag_seconds
        if lag_seconds < 0:
            # The newest value sits ahead of the warehouse's clock: fresh, but a
            # capture time there is a clock the loader and warehouse disagree on.
            out["ahead_of_clock"] = True
    if reason_code is not None:
        out["reason_code"] = reason_code
    if reason is not None:
        out["reason"] = reason
    return out


def _lag_text(seconds: int) -> str:
    from ..semantic_declarations import LAG_UNITS

    for unit, size in LAG_UNITS:
        if seconds >= size:
            whole, rest = divmod(seconds, size)
            return f"{whole}{unit}" + (f" {rest}s" if rest and unit in "smh" else "")
    return f"{seconds}s"


def finding(
    model: str,
    rule: FreshnessRule,
    lag: int | None,
    reason: str | None,
    rows: int,
    *,
    shown: bool = True,
) -> DriftFinding:
    from .drift import DriftFinding

    path = f"'{model}.{rule.column}'"
    if reason == "no_rows":
        found = "the model holds no rows, and no rows is not fresh"
    elif reason == "all_null":
        found = (
            f"the column is NULL on all {rows} row(s), so it has no newest value, "
            "which is not fresh"
        )
    elif not shown:
        found = (
            "its newest value is further behind the warehouse's clock than that "
            "(the lag is withheld because the column looks like personal data)"
        )
    else:
        found = (
            f"its newest value is {_lag_text(lag or 0)} ({lag} seconds) behind the "
            "warehouse's clock"
        )
    if rule.basis == "capture":
        why = (
            "it is the time rows were loaded, so loading has stopped or fallen "
            "behind upstream"
        )
        severity = "high"
    else:
        why = (
            "it is the event's own time, which a quiet period, late-arriving "
            "events or a backfill can leave old; check the load before the source"
        )
        severity = "medium"
    detail = (
        f"{path} may lag the warehouse's clock by at most {rule.max_lag} "
        f"({rule.basis} time), but {found}; {why}"
    )
    data: dict[str, Any] = {
        "column": rule.column,
        "max_lag": rule.max_lag,
        "max_lag_seconds": rule.max_lag_seconds,
        "basis": rule.basis,
        "lag_seconds": lag if shown else None,
        "rows": rows,
        # The count dex_freshness fails with: one stale column.
        "stale": 1,
    }
    if reason is not None:
        data["reason"] = reason
    if not shown:
        data["lag_withheld"] = True
    return DriftFinding(
        axis="declaration",
        code=CODE,
        identifier=model,
        column=rule.column,
        severity=severity,
        detail=detail,
        data=data,
    )


def record_unmeasured(
    prepared: Prepared,
    result: DeclaredFactsResult,
    reason_code: str,
    reason: str | None,
) -> None:
    """A record for every declared freshness that has none, so one nothing
    measured reads as unchecked rather than as absent."""

    recorded = {(r["model"], r["column"]) for r in result.freshness}
    for model in prepared.freshness_models:
        for rule in model.freshness:
            key = (model.model, rule.column)
            if key in recorded:
                continue
            code, why = prepared.freshness_unwritten.get(key, (reason_code, reason))
            if why is None:
                code = "relation_not_found"
                why = next(
                    (
                        d.partition(": ")[2]
                        for d in prepared.dropped.get("freshness", [])
                        if d.startswith(f"{model.model}: ")
                    ),
                    "the check could not be written",
                )
            result.freshness.append(
                record(model.model, rule, "unchecked", reason_code=code, reason=why)
            )


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
