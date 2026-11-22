"""Test outcomes from Elementary's tables, as the incident model's observations (#1471).

The second source adapter after dbt's ``run_results.json``. It reads
``dbt_invocations`` and ``elementary_test_results`` in the schema the package
writes, and hands :func:`~.incidents.fold_incidents` and
:func:`~.timeline.build_timeline` the same records every source hands them: one
:class:`~.observations.Observation` per subject, signal and run, and a
:class:`~.incidents.SourceHistory` naming every run read. It opens and closes no
incident: that is the fold's alone.

How it reads:

- **Layout first.** :func:`plan_read` holds both tables to the release's recorded
  layout (:func:`~.elementary_layouts.check_layouts`, #1469) before any statement
  is written, and quotes each column as the catalog spells it. A release with no
  recorded layout, a missing table (a namespace outside the source allowlist
  reads as every table missing) and a changed column each refuse there, with no
  query sent.
- **Through the firewall.** Every statement goes through the one runner
  ``explore query`` uses: the query firewall, the source allowlist, the profile
  on demand, the cost handshake and the query ledger. Only the result caps are
  the reader's own (:class:`ReadBounds`), since what it reads is engine input,
  not agent context. The first call on a cache that holds no profile of the two
  tables profiles them (``first_read``), priced with the statements.
- **Ids, statuses, counts and times.** It selects ids, a test's type, status and
  severity tokens, the column and metric an anomaly test names, the run's times,
  commit and package version, and counts. Never ``result_rows``,
  ``test_results_description``, ``test_results_query`` or ``test_params``, and
  nothing from ``test_result_rows`` or ``data_monitoring_metrics``.
- **Bounded.** Two counts come first. A call that would read more invocations or
  result rows than its bounds is refused before the rows are read, and so is a
  read that grew past them since it was counted.

How it maps:

- One ``sequence`` per invocation, ordered by ``dbt_invocations``: by the run's
  ``run_started_at`` (text at 0.26.1, parsed here), then the insert's
  ``created_at``, then the id. Never by ``created_at`` alone, which is the
  insert's own clock and can disagree with the order runs started in. Each run
  carries its ``git_sha`` as its commit; ordering by commit is #1467.
- Result rows are deduplicated on ``id``, the table's own dedup key, since it is
  append-only. The rows a subject and signal hold in one run are then one
  observation, a failure over a pass over nothing, so the fold sees one outcome
  per run whatever the table holds.
- A result whose invocation has no ``dbt_invocations`` row is not an
  observation, and is counted. A run with the package's hooks off writes neither
  table, so it is a gap in the heartbeat the timeline reads as missed.
- Statuses map as dbt's do (#1468): ``pass`` good; ``fail``, ``error`` and
  ``warn`` bad, with the row's severity token kept; ``skipped`` unobserved. A
  status outside those refuses.
- The signal is the column and metric of an anomaly test
  (``test_type: anomaly_detection``), which writes one row per column and metric
  in a run, and nothing for every other test. dbt reports one result per test,
  so a column filled for a column-level generic test would put one failure on
  two keys: one from this source and one from ``run_results.json``.
- Every invocation read must have been written by the release the layout check
  held. A table built at an older release keeps its rows, and those are refused,
  since nobody read what that release's rows mean; ``since`` reads past them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlglot import exp

from ..adapters import get_dialect
from ..command_args import billed_handshake
from ..config import QueryLimits
from ..errors import DexError
from ..transform.elementary import release_key
from ..values_free import require_allowed
from .elementary_layouts import CheckedTable, adapter_catalog, check_layouts
from .incidents import SourceHistory
from .observations import (
    SEVERITY_PATTERN,
    Observation,
    ObservedPoint,
    Outcome,
    RunRef,
    Signal,
    Subject,
    utc,
)

#: The source name every observation and run carries.
SOURCE = "elementary"

#: The command the reader opens the connection for, and the one #1472 adds.
COMMAND = "maintain incidents"

INVOCATIONS = "dbt_invocations"
RESULTS = "elementary_test_results"

#: The most ``dbt_invocations`` rows one call reads, and the most
#: ``elementary_test_results`` rows. A caller may narrow either, never widen.
MAX_INVOCATIONS = 2000
MAX_RESULT_ROWS = 100_000

READ_TOO_LARGE = "elementary_read_too_large"
NO_INVOCATIONS = "elementary_no_invocations"
INVOCATION_UNREADABLE = "elementary_invocation_unreadable"
INVOCATION_REPEATED = "elementary_invocation_repeated"
RELEASE_MIXED = "elementary_rows_untested_release"
STATUS_UNKNOWN = "elementary_status_unknown"
RESULT_CONFLICT = "elementary_result_id_conflict"
ID_TOO_LONG = "elementary_id_too_long"

#: dbt's test statuses, as Elementary lowers them, mapped as #1468 maps dbt's.
_OUTCOMES: Mapping[str, Outcome] = {
    "pass": "good",
    "fail": "bad",
    "error": "bad",
    "warn": "bad",
    "skipped": "unobserved",
}

#: Which row a run keeps when one subject and signal has several, by status:
#: a failure over a warning over a pass over nothing, as dbt ranks severity.
_PRECEDENCE: Mapping[str, int] = {
    "fail": 0,
    "error": 0,
    "warn": 1,
    "pass": 2,
    "skipped": 3,
}

#: The ``test_type`` of a test that writes one row per column and metric.
_ANOMALY = "anomaly_detection"

#: The longest test id, column or metric an observation holds, and run id.
_MAX_KEY_CHARS = 512
_MAX_RUN_CHARS = 128

#: A cell is clipped only past this, so a clipped id is always too long to hold
#: and refuses rather than reading as a shorter id.
_CELL_CHARS = _MAX_KEY_CHARS + 1

#: A token a refusal may quote: a status or a package version, never free text.
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,31}$")
_SEVERITY = re.compile(SEVERITY_PATTERN)
_COMMIT = re.compile(r"^[0-9A-Fa-f]{7,64}$")

_INVOCATION_COLUMNS = (
    "invocation_id",
    "run_started_at",
    "created_at",
    "git_sha",
    "elementary_version",
)
_RESULT_COLUMNS = (
    "id",
    "invocation_id",
    "test_unique_id",
    "column_name",
    "test_type",
    "test_sub_type",
    "status",
    "severity",
)


class ElementaryReadError(DexError):
    """The reader refused what the tables hold. ``code`` says why, ``counts``
    the rows or runs it applies to, and the message quotes no value from a row
    but a status or version token."""

    def __init__(
        self, code: str, message: str, counts: Mapping[str, int] | None = None
    ):
        self.code = code
        self.counts = dict(counts or {})
        super().__init__(f"{message} ({code})")


@dataclass(frozen=True)
class ReadBounds:
    """What one call reads at most, each refused past rather than cut."""

    invocations: int = MAX_INVOCATIONS
    result_rows: int = MAX_RESULT_ROWS

    def __post_init__(self) -> None:
        for name, value, ceiling in (
            ("invocations", self.invocations, MAX_INVOCATIONS),
            ("result_rows", self.result_rows, MAX_RESULT_ROWS),
        ):
            if not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be between 1 and {ceiling}")


@dataclass(frozen=True)
class ReadPlan:
    """The statements one read sends, written after the layout check held.

    ``measure`` counts what ``invocations`` and ``results`` would read, so a
    caller can price all three (#1472) before any runs. ``since`` limits the
    read to invocations whose ``created_at`` is at or after it.
    """

    release: str
    namespace: str
    since: datetime | None
    measure: tuple[str, str, str]
    invocations: str
    results: str


@dataclass(frozen=True)
class ReadMeasure:
    """The counts taken before the read: invocation rows and result rows in the
    window, and result rows in it whose invocation has no row."""

    invocations: int
    result_rows: int
    results_without_invocation: int


@dataclass(frozen=True)
class ElementaryRead:
    """What one call read, as the fold takes it, and how it got there."""

    observations: tuple[Observation, ...]
    history: SourceHistory
    release: str
    measure: ReadMeasure
    bounds: ReadBounds
    #: True when this call profiled Elementary's tables, which a fresh cache
    #: needs before the firewall can judge a statement over them.
    first_read: bool
    profiled: tuple[str, ...]
    #: What the call that profiled was priced at, in the connector's unit: the
    #: profiles and that call's three counts together, as one handshake asked
    #: for them (0.0 where both are free). ``None`` where nothing was profiled.
    profile_estimate: float | None
    #: Whether ``since`` cut the read: the retention bound is then the window's
    #: oldest run, not the oldest the table holds.
    windowed: bool
    result_rows_read: int
    #: Rows dropped as a repeat of an ``id`` already read.
    duplicate_ids: int
    #: Rows that are not the one a subject and signal keep in their run.
    merged_rows: int
    #: Rows with no ``id`` or no ``test_unique_id``, which no subject holds.
    unattributed: int
    #: Rows whose invocation the invocation read did not return (a run written
    #: between the two reads), which are not observations.
    unplaced: int
    commits_unread: int
    severities_unread: int


@dataclass(frozen=True)
class _Invocation:
    run: str
    started: datetime
    created: datetime | None
    commit: str | None
    version: str | None


@dataclass
class _Spend:
    """The handshake the runner calls, recorded and handed on."""

    delegate: Callable[..., None] | None
    asks: list[float] = field(default_factory=list)
    profiled: list[str] = field(default_factory=list)
    #: The estimate of each call that profiled. A connector's total can hold
    #: more than its per-table lines (BigQuery reserves escalation floors), so
    #: the whole ask is kept rather than the lines named for a table.
    profiling_asks: list[float] = field(default_factory=list)

    def handshake(self, _command: str, adapter: Any, estimate: float, **kwargs) -> None:
        self.asks.append(float(estimate))
        (self.delegate or billed_handshake)(COMMAND, adapter, estimate, **kwargs)

    def profile_estimate(self) -> float | None:
        return float(sum(self.profiling_asks)) if self.profiled else None


def plan_read(
    engine: Any,
    namespace: str,
    release: str | None,
    *,
    since: datetime | None = None,
) -> ReadPlan:
    """Hold the two tables to ``release``'s layout and write the statements.

    ``namespace`` is the schema the package writes, qualified as the adapter
    names relations (``dev.main_elementary`` on DuckDB). Reads catalog metadata
    only; refuses with :class:`~.elementary_layouts.ElementaryLayoutError`.
    """

    require_allowed(engine.result_mode, COMMAND)
    adapter = engine._adapter(COMMAND)
    check = check_layouts(
        release, adapter_catalog(adapter, namespace), tables=(INVOCATIONS, RESULTS)
    )
    tables = {table.table: table for table in check.tables}
    relations = _relations(adapter, namespace)
    dialect = get_dialect(engine.connector or engine.config.connector)
    sql = _Sql(tables[INVOCATIONS], tables[RESULTS], relations, since, dialect)
    return ReadPlan(
        release=check.release,
        namespace=namespace,
        since=since,
        measure=sql.measure(),
        invocations=sql.invocations(),
        results=sql.results(),
    )


def read_elementary(
    engine: Any,
    plan: ReadPlan,
    *,
    claims: Mapping[str, str] | None = None,
    bounds: ReadBounds | None = None,
    handshake: Callable[..., None] | None = None,
) -> ElementaryRead:
    """Run ``plan`` and turn what it reads into observations and a history.

    ``claims`` maps a test's ``unique_id`` to the claim id its
    ``config.meta.dex.claim`` carries, from the manifest. ``handshake`` stands
    in for the billed handshake, which it must call or replace.
    """

    require_allowed(engine.result_mode, COMMAND)
    limit = bounds or ReadBounds()
    spend = _Spend(handshake)
    adapter = engine._adapter(COMMAND)
    measured = _measure(engine, adapter, plan, limit, spend)
    invocation_rows = _rows(engine, adapter, plan.invocations, limit.invocations, spend)
    result_rows = _rows(engine, adapter, plan.results, limit.result_rows, spend)
    runs = _invocations(invocation_rows, plan.release)
    built = _Observations(runs, dict(claims or {}))
    built.add(result_rows)
    observations = built.observations()
    return ElementaryRead(
        observations=observations,
        history=_history(runs),
        release=plan.release,
        measure=measured,
        bounds=limit,
        first_read=bool(spend.profiled),
        profiled=tuple(spend.profiled),
        profile_estimate=spend.profile_estimate(),
        windowed=plan.since is not None,
        result_rows_read=len(result_rows),
        duplicate_ids=built.duplicates,
        merged_rows=built.merged,
        unattributed=built.unattributed,
        unplaced=built.unplaced,
        commits_unread=sum(1 for run in runs if run.commit is None),
        severities_unread=built.severities_unread,
    )


# --- statements ------------------------------------------------------------------


def _relations(adapter: Any, namespace: str) -> dict[str, str]:
    """Each table's identifier as the adapter's scoped inventory spells it."""

    listed = {
        meta.identifier.casefold(): meta.identifier for meta in adapter.list_objects()
    }
    found = {
        table: listed.get(f"{namespace}.{table}".casefold())
        for table in (INVOCATIONS, RESULTS)
    }
    # The layout check found both in the same inventory, so this cannot miss.
    if missing := sorted(
        table for table, identifier in found.items() if not identifier
    ):
        raise LookupError(f"not in the inventory: {missing}")
    return {table: str(identifier) for table, identifier in found.items()}


def _table(identifier: str, alias: str) -> exp.Table:
    parts = identifier.split(".")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"cannot name {identifier!r} as a relation")
    name, *namespace = reversed(parts)
    db = namespace[0] if namespace else None
    catalog = namespace[1] if len(namespace) > 1 else None
    table = exp.table_(name, db=db, catalog=catalog, quoted=True)
    # Quoted as the column references quote it: Snowflake folds an unquoted
    # alias to upper case, and `"i"."col"` would then name nothing.
    return exp.alias_(table, alias, table=True, quoted=True)


@dataclass(frozen=True)
class _Sql:
    """The reader's statements over the two checked tables."""

    invocations_table: CheckedTable
    results_table: CheckedTable
    relations: Mapping[str, str]
    since: datetime | None
    dialect: str

    def _inv(self, name: str) -> exp.Column:
        return exp.column(self.invocations_table.columns[name], table="i", quoted=True)

    def _res(self, name: str) -> exp.Column:
        return exp.column(self.results_table.columns[name], table="r", quoted=True)

    def _window(self, column: exp.Column) -> exp.Expression | None:
        if self.since is None:
            return None
        moment = utc(self.since).replace(tzinfo=None).isoformat(sep=" ")
        # Elementary's created_at is the warehouse's plain timestamp type, which
        # BigQuery calls TIMESTAMP and sqlglot's zoneless `timestamp` writes as
        # DATETIME there, a type BigQuery will not compare with it.
        kind = "timestamptz" if self.dialect == "bigquery" else "timestamp"
        return column >= exp.cast(exp.Literal.string(moment), kind)

    def _from(self, table: str, alias: str) -> exp.Select:
        return exp.select().from_(_table(self.relations[table], alias))

    def _window_ids(self) -> exp.Select:
        select = self._from(INVOCATIONS, "i").select(self._inv("invocation_id"))
        window = self._window(self._inv("created_at"))
        return select if window is None else select.where(window)

    def _in_window(self) -> exp.Expression:
        return self._res("invocation_id").isin(query=self._window_ids())

    def _render(self, select: exp.Select) -> str:
        return select.sql(dialect=self.dialect)

    def measure(self) -> tuple[str, str, str]:
        count = exp.alias_(exp.Count(this=exp.Star()), "n")
        invocations = self._from(INVOCATIONS, "i").select(count)
        window = self._window(self._inv("created_at"))
        if window is not None:
            invocations = invocations.where(window)
        results = self._from(RESULTS, "r").select(count).where(self._in_window())
        known = self._from(INVOCATIONS, "i").select(self._inv("invocation_id"))
        known = known.where(self._inv("invocation_id").is_(exp.null()).not_())
        orphaned = exp.or_(
            self._res("invocation_id").is_(exp.null()),
            exp.not_(self._res("invocation_id").isin(query=known)),
        )
        orphans = self._from(RESULTS, "r").select(count).where(orphaned)
        results_window = self._window(self._res("created_at"))
        if results_window is not None:
            orphans = orphans.where(results_window)
        return (
            self._render(invocations),
            self._render(results),
            self._render(orphans),
        )

    def invocations(self) -> str:
        select = self._from(INVOCATIONS, "i").select(
            *(self._inv(name) for name in _INVOCATION_COLUMNS)
        )
        window = self._window(self._inv("created_at"))
        return self._render(select if window is None else select.where(window))

    def results(self) -> str:
        select = self._from(RESULTS, "r").select(
            *(self._res(name) for name in _RESULT_COLUMNS)
        )
        return self._render(select.where(self._in_window()))


# --- running ---------------------------------------------------------------------


def _limits(engine: Any, rows: int) -> QueryLimits:
    """The caps for one statement of ``rows`` rows: the row bound, cells wide
    enough that only a too-long id is clipped, and no payload cut."""

    columns = max(len(_INVOCATION_COLUMNS), len(_RESULT_COLUMNS))
    return QueryLimits(
        max_rows=rows,
        max_cell_chars=_CELL_CHARS,
        # Six bytes a character covers JSON's \uXXXX escape of any character.
        max_payload_bytes=(rows + 1) * columns * (_CELL_CHARS * 6 + 16) + 1024,
        timeout_seconds=engine.config.query.timeout_seconds,
    )


def _run(
    engine: Any,
    adapter: Any,
    statements: Sequence[str],
    limits: QueryLimits,
    spend: _Spend,
) -> list[Any]:
    from ..explore.commands import _run_statements

    asked = len(spend.asks)
    batch, shared = _run_statements(
        engine,
        [(sql, None) for sql in statements],
        adapter=adapter,
        handshake=spend.handshake,
        limits=limits,
        result_mode=engine.result_mode,
    )
    if shared.profiled:
        # A call that profiled is priced once, profiles and statements together.
        spend.profiling_asks.extend(spend.asks[asked:])
    spend.profiled.extend(
        name for name in shared.profiled if name not in spend.profiled
    )
    for statement in batch:
        if statement.error is not None:
            raise statement.error
    return [statement.result for statement in batch]


def _measure(
    engine: Any, adapter: Any, plan: ReadPlan, bounds: ReadBounds, spend: _Spend
) -> ReadMeasure:
    answers = _run(engine, adapter, plan.measure, _limits(engine, 1), spend)
    invocations, results, orphans = (int(answer.cells[0][0]) for answer in answers)
    measured = ReadMeasure(invocations, results, orphans)
    if invocations == 0:
        raise ElementaryReadError(
            NO_INVOCATIONS,
            f"{plan.namespace}.{INVOCATIONS} holds no run in the window read; an "
            "empty history is not a clean one",
        )
    _within(INVOCATIONS, invocations, bounds.invocations, plan)
    _within(RESULTS, results, bounds.result_rows, plan)
    return measured


def _within(table: str, count: int, bound: int, plan: ReadPlan) -> None:
    if count <= bound:
        return
    raise ElementaryReadError(
        READ_TOO_LARGE,
        f"{table} holds {count} rows in the window read, past the bound of "
        f"{bound}; read a later window with since, nothing was read",
        {table: count},
    )


def _rows(
    engine: Any, adapter: Any, sql: str, bound: int, spend: _Spend
) -> list[list[Any]]:
    [answer] = _run(engine, adapter, [sql], _limits(engine, bound), spend)
    if answer.truncated or answer.row_count > bound:
        # Truncated is the row cap, or rows the payload budget dropped; the
        # budget is sized so that only an escaped cell far past any id's length
        # could reach it, and either way part of the answer is gone.
        raise ElementaryReadError(
            READ_TOO_LARGE,
            f"a read came back cut: it grew past its bound of {bound} rows after "
            "it was counted, or its rows passed the payload budget; read again",
        )
    return [list(row) for row in answer.cells]


# --- mapping ---------------------------------------------------------------------


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return utc(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _commit(value: Any) -> str | None:
    text = _text(value)
    return text if text is not None and _COMMIT.match(text) else None


def _invocation(row: Sequence[Any]) -> _Invocation | None:
    run, started, created, commit, version = row
    run, moment = _text(run), _time(started)
    if not run or moment is None or len(run) > _MAX_RUN_CHARS:
        return None
    return _Invocation(run, moment, _time(created), _commit(commit), _text(version))


def _token(value: str | None) -> str:
    return value if value is not None and _TOKEN.match(value) else "an unreadable value"


def _identity(invocation: _Invocation) -> tuple:
    """What places a run: its start, commit and version. ``created_at`` is the
    insert's own time, which a second write of the same run may differ in."""

    return (invocation.started, invocation.commit, invocation.version)


def _earlier(held: _Invocation, other: _Invocation) -> _Invocation:
    if held.created is None:
        return other
    if other.created is None or held.created <= other.created:
        return held
    return other


def _distinct(rows: Iterable[_Invocation]) -> list[_Invocation]:
    by_run: dict[str, _Invocation] = {}
    repeated = 0
    for invocation in rows:
        held = by_run.setdefault(invocation.run, invocation)
        if _identity(held) != _identity(invocation):
            repeated += 1
        else:
            by_run[invocation.run] = _earlier(held, invocation)
    if repeated:
        raise ElementaryReadError(
            INVOCATION_REPEATED,
            f"{repeated} {INVOCATIONS} row(s) repeat an invocation_id with another "
            "start, commit or version, so the run has no one place in the order",
            {"rows": repeated},
        )
    return list(by_run.values())


def _one_release(rows: Iterable[_Invocation], release: str) -> None:
    others: dict[str, int] = {}
    for invocation in rows:
        if release_key(invocation.version) != release_key(release):
            name = _token(invocation.version)
            others[name] = others.get(name, 0) + 1
    if others:
        named = ", ".join(
            f"{count} by {name}" for name, count in sorted(others.items())
        )
        raise ElementaryReadError(
            RELEASE_MIXED,
            f"runs written by a release other than {release} ({named}); the reader "
            f"reads only rows its layout check held, so read from after the "
            "upgrade with since",
            others,
        )


def _invocations(rows: Sequence[Sequence[Any]], release: str) -> list[_Invocation]:
    """Each run read, in invocation order."""

    parsed = [_invocation(row) for row in rows]
    unreadable = sum(1 for invocation in parsed if invocation is None)
    if unreadable:
        raise ElementaryReadError(
            INVOCATION_UNREADABLE,
            f"{unreadable} {INVOCATIONS} row(s) carry no readable invocation_id or "
            "run_started_at, so they have no place in the order",
            {"rows": unreadable},
        )
    runs = _distinct(invocation for invocation in parsed if invocation is not None)
    if not runs:
        raise ElementaryReadError(
            NO_INVOCATIONS,
            f"the {INVOCATIONS} read returned no run; an empty history is not a "
            "clean one",
        )
    _one_release(runs, release)
    return sorted(runs, key=_order)


def _order(invocation: _Invocation) -> tuple:
    created = invocation.created or invocation.started
    return (invocation.started, created, invocation.run)


def _history(runs: Sequence[_Invocation]) -> SourceHistory:
    oldest = runs[0]
    return SourceHistory(
        name=SOURCE,
        runs=tuple(
            RunRef(run=run.run, sequence=index, at=run.started, commit=run.commit)
            for index, run in enumerate(runs, start=1)
        ),
        retention_bound=ObservedPoint(run=oldest.run, at=oldest.started, source=SOURCE),
    )


@dataclass(frozen=True)
class _Row:
    """One result row, as read: its outcome and where it belongs."""

    id: str
    run: str
    test: str
    signal: Signal
    outcome: Outcome
    severity: str | None
    status: str
    #: The row named a severity that is not a token, so it carries none.
    severity_unread: bool = False


def _severity(value: str | None) -> tuple[str | None, bool]:
    """The severity token, and whether a severity was there and was not one."""

    if value is None or not value.strip():
        return None, False
    if _SEVERITY.match(value.strip()):
        return value.strip(), False
    return None, True


def _long(*values: str | None) -> bool:
    return any(value is not None and len(value) > _MAX_KEY_CHARS for value in values)


@dataclass
class _Observations:
    """Builds one observation per subject, signal and run from result rows."""

    runs: Sequence[_Invocation]
    claims: Mapping[str, str]
    duplicates: int = 0
    merged: int = 0
    unplaced: int = 0
    _seen: dict[str, _Row] = field(default_factory=dict)
    #: The ids of rows with no test id, and how many rows had no id at all:
    #: a repeated id is one row, as it is everywhere else here.
    _untested: set[str] = field(default_factory=set)
    _no_id: int = 0

    @property
    def unattributed(self) -> int:
        return self._no_id + len(self._untested)

    @property
    def severities_unread(self) -> int:
        return sum(1 for row in self._seen.values() if row.severity_unread)

    def add(self, rows: Iterable[Sequence[Any]]) -> None:
        unknown: dict[str, int] = {}
        for raw in rows:
            row = self._row(raw, unknown)
            if row is None:
                continue
            held = self._seen.setdefault(row.id, row)
            if held != row:
                raise ElementaryReadError(
                    RESULT_CONFLICT,
                    f"two {RESULTS} rows share one id and differ, so neither is "
                    "the table's own record of that result",
                )
            self.duplicates += held is not row
        if unknown:
            named = ", ".join(f"{n} {name}" for name, n in sorted(unknown.items()))
            raise ElementaryReadError(
                STATUS_UNKNOWN,
                f"result rows with a status the reader does not map ({named})",
                unknown,
            )

    def _row(self, raw: Sequence[Any], unknown: dict[str, int]) -> _Row | None:
        rid, run, test, column, kind, metric, status, severity = map(_text, raw)
        if not rid:
            self._no_id += 1
            return None
        if not test:
            self._untested.add(rid)
            return None
        if _long(rid, test, column, metric) or (run and len(run) > _MAX_RUN_CHARS):
            raise ElementaryReadError(
                ID_TOO_LONG,
                f"a {RESULTS} row carries an id, column or metric longer than "
                f"{_MAX_KEY_CHARS} characters",
            )
        word = (status or "").strip().lower()
        outcome = _OUTCOMES.get(word)
        if outcome is None:
            name = _token(word or None)
            unknown[name] = unknown.get(name, 0) + 1
            return None
        anomaly = (kind or "").strip().lower() == _ANOMALY
        signal = (
            Signal(column=column or None, metric=metric or None)
            if anomaly
            else Signal()
        )
        token, unread = _severity(severity)
        return _Row(rid, run or "", test, signal, outcome, token, word, unread)

    def observations(self) -> tuple[Observation, ...]:
        placed = {run.run: (index, run) for index, run in enumerate(self.runs, start=1)}
        kept: dict[tuple[Subject, Signal, str], _Row] = {}
        for row in sorted(self._seen.values(), key=lambda row: row.id):
            if row.run not in placed:
                self.unplaced += 1
                continue
            subject = Subject.of(row.test, self.claims.get(row.test))
            key = (subject, row.signal, row.run)
            held = kept.get(key)
            if held is not None:
                self.merged += 1
                if _PRECEDENCE[row.status] >= _PRECEDENCE[held.status]:
                    continue
            kept[key] = row
        return tuple(
            _observation(subject, row, *placed[row.run])
            for (subject, _, _), row in sorted(kept.items(), key=_kept_order(placed))
        )


def _kept_order(placed: Mapping[str, tuple[int, _Invocation]]) -> Callable[..., tuple]:
    def order(item: tuple[tuple[Subject, Signal, str], _Row]) -> tuple:
        (subject, signal, run), _ = item
        return (
            placed[run][0],
            subject.kind,
            subject.key,
            signal.column or "",
            signal.metric or "",
        )

    return order


def _observation(
    subject: Subject, row: _Row, sequence: int, run: _Invocation
) -> Observation:
    return Observation(
        subject=subject,
        signal=row.signal,
        test=row.test,
        outcome=row.outcome,
        source=SOURCE,
        run=run.run,
        sequence=sequence,
        at=run.started,
        commit=run.commit,
        severity=row.severity,
    )
