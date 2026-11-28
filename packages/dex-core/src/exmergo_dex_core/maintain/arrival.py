"""When a source's rows landed, against the runs that bound the break (#1623).

A source's rows can arrive after the run that reads them: the pipeline runs on
time, the loader is late, the run succeeds without the rows that had not landed,
and a completeness or freshness test fails. The evidence is each row's
**capture time** (when it landed), read against the start of the failing run and
of the last passing one, over the rows each run judged by their **event time**
(when the event happened). :func:`.cause_kinds.arrival_candidate` decides the
answer; this module finds the two columns and reads the counts.

- **The capture column.** The source's dbt ``loaded_at_field`` (top level, or
  under ``config`` or ``config.freshness`` as dbt 1.9+ writes it), else a column
  a model reading the source directly declares with ``freshness.basis:
  capture``. A business timestamp is never read as an arrival time.
- **The event column.** The failing test's own time argument (Elementary's
  ``timestamp_column``, ``dex_freshness``'s column), else the one column the
  tested model declares with role ``time`` on another basis. Either has to be
  a column of the source that the lineage reads from it, and not the capture
  column itself.
- **Counts only, through the firewall.** One aggregate statement per source,
  both columns read as whole epoch seconds (:func:`.freshness.epoch_sql`, a value
  with no time zone read as UTC), sent through the runner ``explore query``
  uses and priced by :func:`price` into the checks' one handshake.

The source is read as it stands now: a row deleted or rewritten since the break
is not seen. Nothing is written to the source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from ..adapters import get_dialect
from ..command_args import billed_handshake
from ..config import QueryLimits
from ..guards.pii_meta import entry_meta
from . import cause_kinds as ck
from . import diagnosis as diag
from .freshness import _quote, _relation, epoch_sql, temporal_kind

if TYPE_CHECKING:
    from .diagnose_kinds import KindWork

#: The tests a late source can fail, by ``test_metadata`` name: completeness
#: (a parent's rows missing, a reference that did not resolve, a volume that
#: dropped) and freshness. A uniqueness failure is the duplicate delivery's.
ARRIVAL_TESTS = frozenset(
    {
        "relationships",
        "dex_parent_population",
        "dex_freshness",
        "volume_anomalies",
        "freshness_anomalies",
        "data_freshness_sla",
    }
)

#: The tests whose failure in the same range corroborates a late source.
FRESHNESS_TESTS = frozenset(
    {"dex_freshness", "freshness_anomalies", "data_freshness_sla"}
)

#: The keyword that names a test's own time column.
_TIME_ARGUMENT = {
    "dex_freshness": "column_name",
    "volume_anomalies": "timestamp_column",
    "freshness_anomalies": "timestamp_column",
    "data_freshness_sla": "timestamp_column",
}

#: The most sources one incident's arrival is read from.
MAX_SOURCES = 25

#: A run's tests are recorded after the run starts: a failure observed within a
#: day of the failing run's start belongs to it.
_GRACE = timedelta(days=1)

_COUNTS = ("bad_window", "bad_late", "bad_null", "good_window", "good_late")


@dataclass(frozen=True)
class ArrivalProbe:
    """One source's statement, and what its answer is decided with."""

    work: Any
    relation: str
    identifier: str
    columns: ck.ArrivalColumns
    statement: str
    corroborated_by: tuple[dict[str, Any], ...] = ()


# --- the tests and the columns ------------------------------------------------------


def test_name(node: Mapping[str, Any]) -> str | None:
    meta = node.get("test_metadata")
    name = meta.get("name") if isinstance(meta, Mapping) else None
    return str(name) if name else None


def scoped_tests(manifest: Mapping[str, Any], tests: Sequence[str]) -> list[str]:
    """The tests of an incident this kind answers, in the incident's order."""

    nodes = manifest.get("nodes") or {}
    return [
        uid
        for uid in tests
        if isinstance(nodes.get(uid), Mapping)
        and test_name(nodes[uid]) in ARRIVAL_TESTS
    ]


def _declared(node: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Each column's ``config.meta.dex`` declaration, by its name."""

    out: dict[str, dict[str, Any]] = {}
    columns = node.get("columns")
    for key, column in columns.items() if isinstance(columns, Mapping) else ():
        if not isinstance(column, Mapping):
            continue
        dex = entry_meta(dict(column)).get("dex")
        name = column.get("name") or key
        if isinstance(dex, Mapping) and isinstance(name, str) and name:
            out[name] = dict(dex)
    return out


def _basis(declared: Mapping[str, Any]) -> str | None:
    freshness = declared.get("freshness")
    basis = freshness.get("basis") if isinstance(freshness, Mapping) else None
    return str(basis).lower() if isinstance(basis, str) else None


def _readers(manifest: Mapping[str, Any], found: ck.Lineage, source: str) -> list[str]:
    """The lineage's models that read ``source`` directly."""

    nodes = manifest.get("nodes") or {}
    return [
        uid
        for uid in found.models
        if source in ((nodes.get(uid) or {}).get("depends_on") or {}).get("nodes", ())
    ]


def _spell(name: str, live: Mapping[str, tuple[str, str | None]]) -> str | None:
    held = live.get(name.casefold())
    return held[0] if held is not None else None


def capture_column(
    manifest: Mapping[str, Any],
    source: str,
    found: ck.Lineage,
    live: Mapping[str, tuple[str, str | None]],
) -> tuple[str, str] | tuple[None, str]:
    """The source's capture column as the relation spells it, and where it was
    named; or ``None`` and why none was."""

    held = (manifest.get("sources") or {}).get(source) or {}
    config = held.get("config") if isinstance(held.get("config"), Mapping) else {}
    freshness = (
        config.get("freshness") if isinstance(config.get("freshness"), Mapping) else {}
    )
    for named in (
        held.get("loaded_at_field"),
        config.get("loaded_at_field"),
        freshness.get("loaded_at_field"),
    ):
        if isinstance(named, str) and named.strip():
            spelled = _spell(named.strip(), live)
            if spelled is None:
                return None, (
                    f"the source's loaded_at_field ({named.strip()}) is not a "
                    "column of the relation, or is an expression rather than a "
                    "column"
                )
            return spelled, "loaded_at_field"
    nodes = manifest.get("nodes") or {}
    for uid in _readers(manifest, found, source):
        for column, declared in _declared(nodes.get(uid) or {}).items():
            if _basis(declared) != "capture":
                continue
            spelled = _spell(column, live)
            if spelled is not None and ck.reads_column(found, column, source):
                return spelled, f"declaration {found.models[uid]}.{column}"
    return None, (
        "the source names no loaded_at_field and no model reading it declares a "
        "column with freshness basis capture, so when its rows landed is not "
        "recorded anywhere Dex reads; a business timestamp says when an event "
        "happened, not when its row arrived"
    )


def event_column(
    manifest: Mapping[str, Any],
    test: str,
    found: ck.Lineage,
    source: str,
    capture: str,
    live: Mapping[str, tuple[str, str | None]],
) -> tuple[str, str] | tuple[None, str]:
    """The column the failing test's window is read from, as the source spells
    it, and where it was named; or ``None`` and why the window is unknown."""

    nodes = manifest.get("nodes") or {}
    node = nodes.get(test) or {}
    name = test_name(node)
    kwargs = (node.get("test_metadata") or {}).get("kwargs") or {}
    keyword = _TIME_ARGUMENT.get(name or "")
    argument = kwargs.get(keyword) if keyword else None
    if not isinstance(argument, str) and keyword == "column_name":
        argument = node.get("column_name")
    own = argument.strip() if isinstance(argument, str) and argument.strip() else None
    if own is not None and own.casefold() != capture.casefold():
        column, origin = own, f"the test's {keyword}"
    else:
        # No time column of its own, or the capture time itself (a freshness
        # monitor on when rows landed): the tested model's event time places
        # the rows instead.
        model = nodes.get(found.model or "") or {}
        times = [
            column
            for column, declared in _declared(model).items()
            if str(declared.get("role") or "").lower() == "time"
            and _basis(declared) != "capture"
            and column.casefold() != capture.casefold()
        ]
        if len(times) != 1:
            what = (
                f"the test's time column ({own}) is the capture time itself"
                if own is not None
                else "the test names no time column"
            )
            held = (
                f"the tested model declares {len(times)} time columns "
                f"({', '.join(sorted(times))}), so which one places a row in the "
                "window cannot be told"
                if times
                else "the tested model declares no other column with role time"
            )
            return None, f"{what}, and {held}, so the window cannot be read"
        model_name = found.models.get(found.model or "", found.model or "")
        column, origin = times[0], f"declaration {model_name}.{times[0]}"
    spelled = _spell(column, live)
    if spelled is None or ck.reads_column(found, column, source) is False:
        return None, (
            f"{column} is not a column the lineage reads from this source, so "
            "the source's rows cannot be placed in the test's window"
        )
    return spelled, origin


# --- the statement ------------------------------------------------------------------


def write(
    dialect: str,
    identifier: str,
    capture: tuple[str, str],
    event: tuple[str, str],
    bounds: tuple[int, int, int],
) -> str:
    """The one statement over a source: five counts, never a value.

    ``capture`` and ``event`` are (column, temporal kind); ``bounds`` the start
    of the last pass's window, the last pass's start and the failing run's, in
    whole epoch seconds."""

    from ..guards.sql_guard import assert_select_only

    good_from, good_at, bad_at = bounds
    c = epoch_sql(dialect, _quote(dialect, capture[0]), capture[1])
    e = epoch_sql(dialect, _quote(dialect, event[0]), event[1])
    bad = f"({e}) > {good_at} AND ({e}) <= {bad_at}"
    good = f"({e}) > {good_from} AND ({e}) <= {good_at}"
    parts = [
        f"COUNT(CASE WHEN {bad} THEN 1 END) AS bad_window",
        f"COUNT(CASE WHEN {bad} AND ({c}) > {bad_at} THEN 1 END) AS bad_late",
        f"COUNT(CASE WHEN {bad} AND {_quote(dialect, capture[0])} IS NULL "
        "THEN 1 END) AS bad_null",
        f"COUNT(CASE WHEN {good} THEN 1 END) AS good_window",
        f"COUNT(CASE WHEN {good} AND ({c}) > {good_at} THEN 1 END) AS good_late",
    ]
    sql = f"SELECT {', '.join(parts)} FROM {_relation(dialect, identifier)}"  # noqa: S608
    return assert_select_only(sql, dialect=dialect)


def _epoch(moment: Any) -> int:
    import math

    return math.floor(moment.timestamp())


def corroboration(
    work: KindWork, found: ck.Lineage, manifest: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """The other incidents a freshness test opened on the lineage in the
    break's range: listed beside the answer, never deciding it."""

    span = work.span
    if span.after == "unknown":
        return ()
    low, high = span.after.at, span.through.at
    nodes = manifest.get("nodes") or {}
    reads = set(found.nodes) - {found.test}
    held: list[dict[str, Any]] = []
    for other in work.others:
        if other is work.incident:
            continue
        for uid in other.tests:
            node = nodes.get(uid) or {}
            if test_name(node) not in FRESHNESS_TESTS:
                continue
            on = str(node.get("attached_node") or "")
            deps = set((node.get("depends_on") or {}).get("nodes") or ())
            if on not in reads and not (deps & set(found.sources)):
                continue
            failed = [
                o
                for o in other.observations
                if o.outcome == "bad"
                and o.at is not None
                and (low is None or o.at > low)
                and (high is None or o.at <= high + _GRACE)
            ]
            if failed:
                held.append(
                    {
                        "subject": other.subject.key,
                        "test": test_name(node),
                        "first_failed_run": failed[0].run,
                    }
                )
                break
    return tuple(held)


def plan(
    engine: Any,
    adapter: Any,
    work: KindWork,
    manifest: Mapping[str, Any],
    identifiers: Sequence[str],
) -> list[ArrivalProbe]:
    """Each source's statement for one incident, or nothing: every source that
    cannot be read gets its ``not_checked`` candidate on ``work`` instead.

    Reads catalog metadata only (the source's columns and their types)."""

    from .diagnose_kinds import _match

    found = work.found
    tests = scoped_tests(manifest, work.incident.tests)
    if not tests or found is None:
        return []
    window = ck.arrival_window(work.span)
    if isinstance(window, diag.Candidate):
        work.candidates.append(window)
        return []
    if not found.sources:
        work.candidates.append(
            ck.arrival_unread(
                "no_capture_time",
                "the failing test and the models it reads read no source of the "
                "project, so there is no loader whose rows could have arrived late",
            )
        )
        return []
    bounds = tuple(_epoch(t) for t in window)
    dialect = get_dialect(engine.connector or engine.config.connector)
    sources = manifest.get("sources") or {}
    probes: list[ArrivalProbe] = []
    for uid in found.sources[:MAX_SOURCES]:
        source = sources.get(uid) or {}
        name = str(source.get("identifier") or source.get("name") or uid)
        label = f"{source.get('schema')}.{name}"
        identifier = _match(
            identifiers,
            str(source.get("schema") or ""),
            name,
            str(source.get("database") or "") or None,
        )
        if identifier is None:
            work.candidates.append(
                ck.arrival_unread(
                    "source_not_listed",
                    f"the connection does not list {label} (outside the source "
                    "allowlist, or not loaded), so when its rows landed was not "
                    "read",
                    relation=label,
                )
            )
            continue
        _, columns = adapter.table_metadata(identifier)
        live = {c.name.casefold(): (c.name, c.data_type) for c in columns}
        capture, why = capture_column(manifest, uid, found, live)
        if capture is None:
            work.candidates.append(
                ck.arrival_unread("no_capture_time", why, relation=identifier)
            )
            continue
        capture_kind = temporal_kind(dialect, live[capture.casefold()][1])
        if capture_kind is None:
            work.candidates.append(
                ck.arrival_unread(
                    "no_capture_time",
                    f"{capture} is no date or timestamp, so no arrival time can be "
                    "read from it",
                    relation=identifier,
                )
            )
            continue
        event, origin = event_column(manifest, tests[0], found, uid, capture, live)
        if event is None:
            work.candidates.append(
                ck.arrival_unread("window_unknown", origin, relation=identifier)
            )
            continue
        event_kind = temporal_kind(dialect, live[event.casefold()][1])
        if event_kind is None:
            work.candidates.append(
                ck.arrival_unread(
                    "window_unknown",
                    f"{event} is no date or timestamp, so the test's window cannot "
                    "be read from it",
                    relation=identifier,
                )
            )
            continue
        probes.append(
            ArrivalProbe(
                work=work,
                relation=identifier,
                identifier=identifier,
                columns=ck.ArrivalColumns(capture, why, event, origin),
                statement=write(
                    dialect,
                    identifier,
                    (capture, capture_kind),
                    (event, event_kind),
                    bounds,  # type: ignore[arg-type]
                ),
                corroborated_by=corroboration(work, found, manifest),
            )
        )
    return probes


def price(
    engine: Any, adapter: Any, probes: Sequence[ArrivalProbe]
) -> tuple[float, dict[str, float]]:
    """The statements' estimate and their lines, with a profile of each source
    the firewall has none to judge by, as :func:`.run_times.price` prices."""

    from ..explore.commands import _object_gap, _profile_estimate
    from ..storage import readable_cache

    estimate_one = getattr(adapter, "query_estimate", None)
    lines: dict[str, float] = {}
    total = 0.0
    for probe in probes:
        cost = float(estimate_one(probe.statement)) if estimate_one else 0.0
        key = f"count {probe.identifier} rows by capture time"
        lines[key] = lines.get(key, 0.0) + cost
        total += cost
    wanted = list(dict.fromkeys(p.identifier for p in probes))
    if wanted:
        gap = _object_gap(adapter, readable_cache(engine.store), wanted)
        if gap.to_profile:
            profiled, per_table = _profile_estimate(adapter, list(gap.to_profile))
            total += profiled
            for key, value in per_table.items():
                lines[key] = lines.get(key, 0.0) + value
    return total, lines


def _limits(engine: Any) -> QueryLimits:
    return QueryLimits(
        max_rows=1,
        max_cell_chars=64,
        max_payload_bytes=16 * 1024,
        timeout_seconds=engine.config.query.timeout_seconds,
    )


def read(
    engine: Any, adapter: Any, probes: Sequence[ArrivalProbe], command: str
) -> list[ck.ArrivalCounts | BaseException]:
    """Run every statement in one batch, and return each one's counts or the
    error that stopped it, in ``probes``' order.

    Raises a cost refusal as the runner does; the caller reports any other
    error as that source's ``not_checked`` reason.
    """

    from ..explore.commands import _run_statements

    def handshake(_command: str, gate: Any, estimate: float, **kwargs: Any) -> None:
        billed_handshake(command, gate, estimate, **kwargs)

    batch, _shared = _run_statements(
        engine,
        [(p.statement, None) for p in probes],
        adapter=adapter,
        handshake=handshake,
        limits=_limits(engine),
        result_mode=engine.result_mode,
    )
    out: list[ck.ArrivalCounts | BaseException] = []
    for statement in batch:
        if statement.error is not None:
            out.append(statement.error)
            continue
        rows = statement.result.cells if statement.result is not None else []
        row = list(rows[0]) if rows else []
        values = [_count(v) for v in (row + [None] * len(_COUNTS))[: len(_COUNTS)]]
        if any(v is None for v in values):
            out.append(ValueError("the count statement returned no counts"))
            continue
        out.append(ck.ArrivalCounts(*values))  # type: ignore[arg-type]
    return out


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    try:
        from decimal import Decimal

        if isinstance(value, Decimal):
            return int(value)
    except (ValueError, ArithmeticError):
        return None
    return None


__all__ = [
    "ARRIVAL_TESTS",
    "FRESHNESS_TESTS",
    "ArrivalProbe",
    "capture_column",
    "event_column",
    "plan",
    "price",
    "read",
    "scoped_tests",
    "write",
]
