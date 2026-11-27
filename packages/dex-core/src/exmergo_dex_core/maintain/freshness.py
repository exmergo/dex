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

Counts only: the statement returns two counts and the lag in seconds, never a
value from the column. On a column that looks like personal data the lag would
disclose its newest value, so there the statement computes neither: it counts
the rows within ``max_lag`` of the clock (the form ``dex_freshness`` always
uses), and the finding and record say the lag is withheld.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


#: The two checks a declared freshness can state, one record each.
MAX_LAG = "max_lag"
DEADLINE = "deadline"

#: The finding a missed daily deadline raises (#1486).
DEADLINE_CODE = "deadline_missed"

#: Why a declared deadline had nothing to judge on this run.
NOT_SCHEDULED = "not_scheduled"
BEFORE_DEADLINE = "before_deadline"


@dataclass(frozen=True)
class FreshnessRule:
    """One check a declared ``freshness`` states, on its column as declared.

    ``check`` is ``max_lag`` (#1460: the lag in seconds and as written,
    normalized) or ``deadline`` (#1486: a time of day in ``timezone``, on
    every day or only on ``days``, three-letter, Monday first). A column
    declaring both gives two rules. ``basis`` is the column's either way."""

    column: str
    max_lag: str | None
    max_lag_seconds: int | None
    basis: str
    check: str = MAX_LAG
    deadline: str | None = None
    timezone: str | None = None
    days: tuple[str, ...] | None = None


def read_rules(column: str, value: Any) -> list[FreshnessRule]:
    """Each check a column's ``dex.freshness`` states (a lag, a deadline, or
    both), or none where it states one the declaration schema would refuse (a
    hand edit): an unreadable rule is never guessed into a check."""

    from pydantic import ValidationError

    from ..semantic_declarations import Freshness

    if not isinstance(value, dict):
        return []
    try:
        declared = Freshness.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        return []
    basis = declared.basis.value
    out: list[FreshnessRule] = []
    if declared.max_lag is not None:
        out.append(
            FreshnessRule(
                column, declared.max_lag, declared.max_lag_seconds, basis, MAX_LAG
            )
        )
    if declared.deadline is not None:
        out.append(
            FreshnessRule(
                column,
                None,
                None,
                basis,
                DEADLINE,
                deadline=declared.deadline,
                timezone=declared.timezone,
                days=tuple(declared.days) if declared.days else None,
            )
        )
    return out


def read_rule(column: str, value: Any) -> FreshnessRule | None:
    """The lag rule a column's ``dex.freshness`` states, or ``None`` where it
    states none or one the declaration schema would refuse."""

    return next((r for r in read_rules(column, value) if r.check == MAX_LAG), None)


# --- the deadline's day ---------------------------------------------------------


def clock() -> Any:
    """The runner's clock, as an aware UTC datetime: the seam tests freeze."""

    from datetime import UTC, datetime

    return datetime.now(UTC)


@dataclass(frozen=True)
class DeadlineWindow:
    """Today in a deadline's time zone, as Elementary's ``data_freshness_sla``
    computes it: the local date, whether ``days`` schedules it, and, as whole
    UTC epoch seconds, the local midnight starting it and the deadline."""

    target_date: str
    weekday: str
    scheduled: bool
    day_start: int
    deadline: int


def deadline_window(rule: FreshnessRule, now: Any = None) -> DeadlineWindow:
    """The window ``rule``'s deadline judges on the runner's ``now``.

    The day is the runner's, read in the declared zone, as the package reads
    it when it compiles the test; whether the deadline has passed is the
    warehouse's clock, compared in the statement, as the package compares it.
    A local time a clock change skips or repeats resolves as ``zoneinfo``
    resolves it (the earlier offset)."""

    from datetime import UTC, datetime, time
    from zoneinfo import ZoneInfo

    from ..semantic_declarations import WEEKDAYS, deadline_parts

    zone = ZoneInfo(str(rule.timezone))
    current = (now or clock()).astimezone(zone)
    local_day = current.date()
    hour, minute = deadline_parts(str(rule.deadline))
    start = datetime.combine(local_day, time(0, 0), tzinfo=zone).astimezone(UTC)
    due = datetime.combine(local_day, time(hour, minute), tzinfo=zone).astimezone(UTC)
    weekday = WEEKDAYS[local_day.weekday()][0]
    return DeadlineWindow(
        target_date=local_day.isoformat(),
        weekday=weekday,
        scheduled=rule.days is None or weekday in rule.days,
        day_start=int(start.timestamp()),
        deadline=int(due.timestamp()),
    )


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


def within_sql(dialect: str, quoted_column: str, kind: str, max_lag: int) -> str:
    """The count of rows whose value sits within ``max_lag`` seconds of the
    clock, each row's lag floored as :func:`lag_sql` floors the newest one. A
    column is fresh exactly where this is above zero, and nothing computes its
    newest value, so a flagged column is judged this way, and so is every
    column in ``dex_freshness``."""

    row_epoch = epoch_sql(dialect, quoted_column, kind)
    return (
        f"COUNT(CASE WHEN ({now_sql(dialect)}) - ({row_epoch}) <= {int(max_lag)} "
        "THEN 1 END)"
    )


def today_sql(dialect: str, quoted_column: str, kind: str, day_start: int) -> str:
    """The count of rows whose value falls on or after the local midnight
    ``day_start`` (whole UTC epoch seconds): the column holds today's data
    exactly where this is above zero. A count, never a value, so a column
    that looks like personal data is judged the same way."""

    row_epoch = epoch_sql(dialect, quoted_column, kind)
    return f"COUNT(CASE WHEN ({row_epoch}) >= {int(day_start)} THEN 1 END)"


def due_sql(dialect: str, deadline: int) -> str:
    """1 where the warehouse's clock has reached ``deadline`` (whole UTC epoch
    seconds), else 0: a constant, read once with the counts."""

    return f"CASE WHEN ({now_sql(dialect)}) >= {int(deadline)} THEN 1 ELSE 0 END"


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
    #: and whether its lag may be shown (``False`` on a column that looks like
    #: personal data, measured by a count that never computes its newest value)
    rules: list[tuple[int, FreshnessRule, str, str | None, bool]]
    #: Each deadline rule's window (#1486), by alias index.
    windows: dict[int, DeadlineWindow] = field(default_factory=dict)


def write_probe(
    probe: _Probe,
    live: dict[str, tuple[str, str]] | None,
    dialect: str,
    prepared: Prepared,
    pii_overrides: Any = None,
) -> None:
    """Write the statement measuring every freshness ``probe``'s model declares,
    or record why a rule gets none. Free: the columns were already read.

    A column that looks like personal data (and is not cleared in
    ``pii_overrides``) is measured without computing its newest value, which
    the PII policy never computes for a flagged column: the statement counts
    the rows within ``max_lag`` of the clock, as ``dex_freshness`` does, and
    the column is stale where none is."""

    from ..guards.sql_guard import assert_select_only

    model = probe.model
    parts = ["COUNT(*) AS dex_rows"]
    rules: list[tuple[int, FreshnessRule, str, str | None, bool]] = []
    windows: dict[int, DeadlineWindow] = {}
    now = clock()
    for index, rule in enumerate(model.freshness):
        if live is None:
            spelled, data_type = rule.column, model_type(model, rule.column)
        else:
            found = live.get(rule.column.lower())
            if found is None:
                why = (
                    f"the declared {_what(rule)} on {model.model}.{rule.column} "
                    "was not checked: the built relation has no such column"
                )
                prepared.notes.append(why)
                prepared.dropped["freshness"].append(f"{model.model}: {why}")
                prepared.freshness_unwritten[(model.model, rule.column, rule.check)] = (
                    COLUMN_MISSING,
                    "the built relation has no such column",
                )
                continue
            spelled, data_type = found
        kind = temporal_kind(dialect, data_type)
        if kind is None:
            why = (
                f"the declared {_what(rule)} on {model.model}.{rule.column} was not "
                f"checked: the column is {data_type}, not a date or timestamp"
            )
            prepared.notes.append(why)
            prepared.dropped["freshness"].append(f"{model.model}: {why}")
            prepared.freshness_unwritten[(model.model, rule.column, rule.check)] = (
                NOT_TEMPORAL,
                f"the column is {data_type}, not a date or timestamp",
            )
            continue
        q = _quote(dialect, spelled)
        if rule.check == DEADLINE:
            # Counts and a constant only, so no value is computed, on a
            # column that looks like personal data too.
            window = deadline_window(rule, now)
            windows[index] = window
            parts.append(
                f"COUNT({q}) AS dex_fresh_n_{index}, "
                f"{today_sql(dialect, q, kind, window.day_start)} "
                f"AS dex_sla_today_{index}, "
                f"{due_sql(dialect, window.deadline)} AS dex_sla_due_{index}"
            )
            rules.append((index, rule, spelled, data_type, True))
            continue
        shown = _lag_shown(probe.identifier, spelled, data_type, pii_overrides)
        if shown:
            measure = f"{lag_sql(dialect, q, kind)} AS dex_fresh_lag_{index}"
        else:
            measure = (
                f"{within_sql(dialect, q, kind, int(rule.max_lag_seconds or 0))} "
                f"AS dex_fresh_in_{index}"
            )
        parts.append(f"COUNT({q}) AS dex_fresh_n_{index}, {measure}")
        rules.append((index, rule, spelled, data_type, shown))
    if not rules:
        return
    sql = f"SELECT {', '.join(parts)} FROM {_relation(dialect, probe.identifier)}"  # noqa: S608
    probe.freshness = Written(assert_select_only(sql, dialect=dialect), rules, windows)


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
        for _index, rule, _spelled, _type, _shown in written.rules:
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
    for index, rule, _spelled, _type, shown in written.rules:
        if rule.check == DEADLINE:
            _judge_deadline(
                model, rule, written.windows[index], row, index, rows, result, checked
            )
            continue
        values = _int(row.get(f"dex_fresh_n_{index}")) or 0
        lag = _int(row.get(f"dex_fresh_lag_{index}"))
        if rows == 0 or values == 0:
            checked["freshness"] += 1
            reason = "no_rows" if rows == 0 else "all_null"
            result.freshness.append(
                record(model, rule, "contradicted", reason_code=reason, rows=rows)
            )
            result.findings.append(finding(model, rule, None, reason, rows))
            continue
        if not shown:
            # A flagged column: no lag was computed, only the rows within it.
            checked["freshness"] += 1
            within = _int(row.get(f"dex_fresh_in_{index}")) or 0
            status = "satisfied" if within > 0 else "contradicted"
            result.freshness.append(
                record(model, rule, status, rows=rows, lag_withheld=True)
            )
            if within == 0:
                result.findings.append(
                    finding(model, rule, None, None, rows, shown=False)
                )
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
        if lag > int(rule.max_lag_seconds or 0):
            result.freshness.append(
                record(model, rule, "contradicted", lag_seconds=lag, rows=rows)
            )
            result.findings.append(finding(model, rule, lag, None, rows))
            continue
        result.freshness.append(
            record(model, rule, "satisfied", lag_seconds=lag, rows=rows)
        )


def _judge_deadline(
    model: str,
    rule: FreshnessRule,
    window: DeadlineWindow,
    row: dict[str, Any],
    index: int,
    rows: int,
    result: DeclaredFactsResult,
    checked: dict[str, int],
) -> None:
    """Judge one declared deadline (#1486) as Elementary's
    ``data_freshness_sla`` does: nothing is due on a day ``days`` leaves out or
    before the deadline; once it has passed, the column must hold a value from
    today (local midnight onward), and no rows or no value is no data."""

    checked["freshness"] += 1
    if not window.scheduled:
        result.freshness.append(
            record(
                model,
                rule,
                "not_applicable",
                reason_code=NOT_SCHEDULED,
                rows=rows,
                window=window,
            )
        )
        return
    if not _int(row.get(f"dex_sla_due_{index}")):
        result.freshness.append(
            record(
                model,
                rule,
                "not_applicable",
                reason_code=BEFORE_DEADLINE,
                rows=rows,
                window=window,
            )
        )
        return
    values = _int(row.get(f"dex_fresh_n_{index}")) or 0
    today = _int(row.get(f"dex_sla_today_{index}")) or 0
    if rows and values and today:
        result.freshness.append(
            record(model, rule, "satisfied", rows=rows, window=window)
        )
        return
    reason = "no_rows" if rows == 0 else "all_null" if values == 0 else "not_today"
    result.freshness.append(
        record(
            model, rule, "contradicted", reason_code=reason, rows=rows, window=window
        )
    )
    result.findings.append(deadline_finding(model, rule, window, reason, rows))


def _what(rule: FreshnessRule) -> str:
    return "deadline" if rule.check == DEADLINE else "freshness"


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
    lag_withheld: bool = False,
    window: DeadlineWindow | None = None,
) -> dict[str, Any]:
    """A ``data.freshness`` record: which ``check`` (``max_lag`` or
    ``deadline``), the declaration, what was measured, and a status of
    ``satisfied``, ``contradicted``, ``unchecked``, or, for a deadline with
    nothing due on this run, ``not_applicable``."""

    out: dict[str, Any] = {
        "model": model,
        "column": rule.column,
        "check": rule.check,
    }
    if rule.check == DEADLINE:
        out["deadline"] = rule.deadline
        out["timezone"] = rule.timezone
        if rule.days is not None:
            out["days"] = list(rule.days)
    else:
        out["max_lag"] = rule.max_lag
        out["max_lag_seconds"] = rule.max_lag_seconds
    out["basis"] = rule.basis
    out["status"] = status
    if status == "contradicted":
        out["finding"] = DEADLINE_CODE if rule.check == DEADLINE else CODE
    if window is not None:
        # The local day judged: a date, never a value from the column.
        out["target_date"] = window.target_date
    if rows is not None:
        out["rows"] = rows
    if lag_withheld:
        out["lag_withheld"] = True
    if lag_seconds is not None:
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


def deadline_finding(
    model: str,
    rule: FreshnessRule,
    window: DeadlineWindow,
    reason: str,
    rows: int,
) -> DriftFinding:
    """``deadline_missed`` (#1486): the deadline has passed today and the
    column holds no value from today. Never a timestamp: the counts and the
    day judged."""

    from .drift import DriftFinding

    path = f"'{model}.{rule.column}'"
    if reason == "no_rows":
        found = "the model holds no rows"
    elif reason == "all_null":
        found = f"the column is NULL on all {rows} row(s)"
    else:
        found = "its newest value is from an earlier day"
    days = f" on {', '.join(rule.days)}" if rule.days else ""
    if rule.basis == "capture":
        why = "it is the time rows were loaded, so today's load is late or missing"
        severity = "high"
    else:
        why = (
            "it is the event's own time, so either today's load is late or the "
            "events loaded by the deadline are from earlier days (a nightly load "
            "of yesterday's events never meets a same-day deadline on a business "
            "time; declare it on the load time instead)"
        )
        severity = "medium"
    detail = (
        f"{path} must hold a value from today by {rule.deadline} {rule.timezone}"
        f"{days} ({rule.basis} time), but on {window.target_date} the deadline has "
        f"passed and {found}; {why}"
    )
    data: dict[str, Any] = {
        "column": rule.column,
        "check": DEADLINE,
        "deadline": rule.deadline,
        "timezone": rule.timezone,
        "days": list(rule.days) if rule.days else None,
        "basis": rule.basis,
        "target_date": window.target_date,
        "reason": reason,
        "rows": rows,
    }
    return DriftFinding(
        axis="declaration",
        code=DEADLINE_CODE,
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

    recorded = {
        (r["model"], r["column"], r.get("check", MAX_LAG)) for r in result.freshness
    }
    for model in prepared.freshness_models:
        for rule in model.freshness:
            key = (model.model, rule.column, rule.check)
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
