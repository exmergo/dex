"""Model build times from Elementary's ``dbt_run_results`` (#1476).

The schedule is a cause owner: a test can start failing because a parent model
was built after the child that reads it. The evidence is both models' build
times at the runs that bound the break, which the Elementary package records,
one row per node per invocation, in ``dbt_run_results``.

The read follows the incident reader's rules (:mod:`.elementary_incidents`):

- **Layout first.** The table is held to the release's recorded layout before
  any statement is written; a missing table or a changed column refuses there,
  with no query sent, and the schedule is then not checked.
- **Through the firewall.** The one statement goes through the runner
  ``explore query`` uses (firewall, allowlist, profile on demand, ledger), and
  is priced by :func:`price` so the caller can fold it into one handshake.
- **Ids and times only.** It selects each successful model row's
  ``unique_id``, ``invocation_id`` and execution times, for the models named,
  newest first and bounded. Never ``compiled_code``, ``message`` or
  ``adapter_response``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlglot import exp

from ..adapters import get_dialect
from ..command_args import billed_handshake
from ..config import QueryLimits
from .cause_kinds import Build
from .elementary_layouts import adapter_catalog, check_layouts
from .observations import utc

TABLE = "dbt_run_results"

#: The most build rows one read returns.
MAX_BUILD_ROWS = 2000

#: How far past the failing run's start a build row may have been inserted and
#: still be read: the package inserts a run's rows when the run ends.
_MARGIN_SECONDS = 2 * 24 * 3600

_COLUMNS = ("unique_id", "invocation_id", "execute_started_at", "execute_completed_at")


@dataclass(frozen=True)
class RunTimesPlan:
    """The one statement the read sends, written after the layout check."""

    release: str
    identifier: str
    statement: str
    models: tuple[str, ...]


def plan(
    engine: Any,
    adapter: Any,
    namespace: str,
    release: str | None,
    models: Sequence[str],
    until: datetime | None,
) -> RunTimesPlan:
    """Hold ``dbt_run_results`` to ``release``'s layout and write the read.

    Raises :class:`~.elementary_layouts.ElementaryLayoutError` where the table
    is missing or its columns changed. Reads catalog metadata only.
    """

    check = check_layouts(release, adapter_catalog(adapter, namespace), tables=(TABLE,))
    [table] = check.tables
    listed = {
        meta.identifier.casefold(): meta.identifier for meta in adapter.list_objects()
    }
    identifier = listed[f"{namespace}.{TABLE}".casefold()]
    dialect = get_dialect(engine.connector or engine.config.connector)

    def col(name: str) -> exp.Column:
        return exp.column(table.columns[name], table="b", quoted=True)

    parts = identifier.split(".")
    name, *rest = reversed(parts)
    relation = exp.alias_(
        exp.table_(
            name,
            db=rest[0] if rest else None,
            catalog=rest[1] if len(rest) > 1 else None,
            quoted=True,
        ),
        "b",
        table=True,
        quoted=True,
    )
    wanted = sorted(dict.fromkeys(models))
    condition = exp.and_(
        col("resource_type").eq(exp.Literal.string("model")),
        col("status").eq(exp.Literal.string("success")),
        col("unique_id").isin(*(exp.Literal.string(m) for m in wanted)),
    )
    if until is not None:
        moment = utc(until) + timedelta(seconds=_MARGIN_SECONDS)
        text = moment.replace(tzinfo=None).isoformat(sep=" ")
        kind = "timestamptz" if dialect == "bigquery" else "timestamp"
        condition = exp.and_(
            condition,
            col("created_at") <= exp.cast(exp.Literal.string(text), kind),
        )
    select = (
        exp.select(*(col(c) for c in _COLUMNS))
        .from_(relation)
        .where(condition)
        .order_by(
            exp.Ordered(this=col("created_at"), desc=True),
            exp.Ordered(this=col("invocation_id"), desc=True),
        )
        .limit(MAX_BUILD_ROWS)
    )
    return RunTimesPlan(
        release=check.release,
        identifier=identifier,
        statement=select.sql(dialect=dialect),
        models=tuple(wanted),
    )


def price(
    engine: Any, adapter: Any, held: RunTimesPlan
) -> tuple[float, dict[str, float]]:
    """The read's estimate and its lines: the statement, and a profile of the
    table where the firewall has none to judge it by."""

    from ..explore.commands import _object_gap, _profile_estimate
    from ..storage import readable_cache

    estimate_one = getattr(adapter, "query_estimate", None)
    statement = float(estimate_one(held.statement)) if estimate_one else 0.0
    lines: dict[str, float] = {"read dbt_run_results build times": statement}
    gap = _object_gap(adapter, readable_cache(engine.store), [held.identifier])
    total = statement
    if gap.to_profile:
        profiled, per_table = _profile_estimate(adapter, list(gap.to_profile))
        total += profiled
        lines.update(per_table)
    return total, lines


def _limits(engine: Any) -> QueryLimits:
    return QueryLimits(
        max_rows=MAX_BUILD_ROWS,
        max_cell_chars=513,
        max_payload_bytes=(MAX_BUILD_ROWS + 1) * len(_COLUMNS) * (513 * 6 + 16) + 1024,
        timeout_seconds=engine.config.query.timeout_seconds,
    )


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def read(
    engine: Any, adapter: Any, held: RunTimesPlan, command: str
) -> tuple[Build, ...]:
    """Run the read and return each successful build it found.

    Raises the firewall's or the warehouse's error as the runner does; the
    caller reports it as the schedule's ``not_checked`` reason.
    """

    from ..explore.commands import _run_statements

    def handshake(_command: str, gate: Any, estimate: float, **kwargs: Any) -> None:
        billed_handshake(command, gate, estimate, **kwargs)

    batch, _shared = _run_statements(
        engine,
        [(held.statement, None)],
        adapter=adapter,
        handshake=handshake,
        limits=_limits(engine),
        result_mode=engine.result_mode,
    )
    [statement] = batch
    if statement.error is not None:
        raise statement.error
    builds: list[Build] = []
    for row in statement.result.cells if statement.result is not None else ():
        uid, invocation, started, completed = (list(row) + [None] * 4)[:4]
        if not uid or not invocation:
            continue
        builds.append(
            Build(
                model=str(uid),
                invocation=str(invocation),
                started=_time(started),
                completed=_time(completed),
            )
        )
    return tuple(builds)


__all__ = ["MAX_BUILD_ROWS", "TABLE", "RunTimesPlan", "plan", "price", "read"]
