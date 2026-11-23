"""``dex maintain diagnose``: from each open incident to a cause (#1475).

The incidents are the ones ``maintain incidents`` returns, read the same way
(:func:`~.incident_sources.read`, one bounded, priced read), with the
repository's history handed to the fold so each onset carries the commits it
spans (#1467). Each open incident is then diagnosed (:mod:`.diagnosis`):

1. **Bisect.** The range is the onset: after the last observed-good run, up to
   and including the first observed-bad one. An unobserved run widens it.
2. **Bound.** The committed ``maintain snapshot`` series
   (:mod:`.snapshot_series`) is placed against the range: the newest snapshot at
   or before the last observed pass is the before, the oldest at or after the
   failure the after. With no series, the whole break is a window no recorded
   state of the warehouse covers, and the answer says so.
3. **Confirm.** Each candidate cause is confirmed by a check Dex already has,
   named in the answer. This release checks the cause kind ``dex demo`` seeds
   (#1477): a uniqueness test failing because the loader delivered a key twice.
   Its check is the grain axis of ``maintain check``, run on each source relation
   the tested model reads that carries the tested columns:

   - with a before snapshot that measured the key unique,
     ``key_lost_uniqueness`` against that snapshot, exactly as ``maintain
     check`` reads it, plus the volume axis's row counts as evidence;
   - otherwise the grain axis's declared-claim check, which needs no baseline:
     the failing test's own claim held to the source relation today
     (``declared_grain_not_unique``).

   The project is ruled out where the failing commit itself passed earlier
   (``onset.commits``); the other owners are listed as not checked (#1476).
4. **Answer.** One confirmed cause, several, or none. With none, the answer is
   what was ruled out and the window that could not be seen; the most likely
   candidate is never named. A confirmed cause's repair is handed to
   ``transform`` as a step; nothing here writes it.

Every scan goes through the grain axis's own probes and is priced first, in one
handshake for every check this call runs, after the incident read's own.
Read-only: nothing here writes to the warehouse, the project or ``.dex/``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import command_args
from ..cache import Dataset
from ..values_free import require_allowed
from . import diagnosis as diag
from . import incident_sources
from .results import DiagnoseResult

if TYPE_CHECKING:
    from ..adapters.base import Adapter
    from ..engine import DexEngine
    from .drift import GrainPlan
    from .incidents import Incident
    from .snapshot import Snapshot
    from .snapshot_series import SeriesRead

COMMAND = "maintain diagnose"

#: The check that confirms a loader's duplicate delivery, as the answer names it.
GRAIN_CHECK = "maintain check: grain axis"

#: The uniqueness tests a duplicate delivery fails, by their ``test_metadata``
#: name, and the keyword that names their columns.
_UNIQUENESS_TESTS: dict[str, str] = {
    "unique": "column_name",
    "unique_combination_of_columns": "combination_of_columns",
    "dex_unique_combination": "columns",
}

#: The most source relations one incident's candidates are read from.
_MAX_UPSTREAM = 25


def diagnose(
    engine: DexEngine,
    objects: list[str] | None = None,
    *,
    invocations: int | None = None,
) -> DiagnoseResult:
    """Each open incident on the selected models' tests, diagnosed to a cause.

    ``objects`` and ``invocations`` select and bound the incident read exactly
    as ``maintain incidents`` takes them.
    """

    require_allowed(engine.result_mode, COMMAND)
    history = _history(engine)
    got = incident_sources.read(
        engine,
        objects,
        invocations=invocations,
        history=history,
        command=COMMAND,
    )
    read = got.result
    warnings = list(read.warnings)
    notes = list(read.notes)
    if history is None:
        notes.append(
            "the dbt project is not in a git checkout, so no break carries the "
            "commits it spans and the project is not ruled out"
        )
    series_read = _series(engine)
    if series_read.skipped:
        warnings.append(
            f"{series_read.skipped} committed version(s) of .dex/snapshot.json do "
            "not parse as a snapshot and are left out of the series"
        )
    if series_read.elided:
        notes.append(
            f"the series holds {series_read.elided} older snapshot(s) past the "
            f"newest {len(series_read.series.points)}, which are not read"
        )

    fold = got.fold
    incidents = list(fold.incidents) if fold is not None else []
    open_incidents = [i for i in incidents if i.state == "open" and i.onset]
    skipped = [
        {
            "subject": i.subject.key,
            "state": i.state,
            "reason": "not_open" if i.state != "open" else "no_onset",
        }
        for i in incidents
        if i not in open_incidents
    ]

    diagnoses: list[dict[str, Any]] = []
    checks_price: dict[str, Any] | None = None
    adapter: Adapter | None = None
    if open_incidents:
        manifest = got.manifest or {}
        work = [
            _Work(incident, _specs(manifest, incident)) for incident in open_incidents
        ]
        if any(w.specs for w in work):
            adapter = engine._adapter(COMMAND)
            checks_price = _confirm_all(
                engine,
                adapter,
                work,
                series_read,
                spent_before=_billed(read.spend),
                warnings=warnings,
            )
        diagnoses.extend(
            diag.diagnosis(
                subject=item.incident.subject.key,
                tests=item.incident.tests,
                onset=item.incident.onset,  # type: ignore[arg-type]
                series=series_read.series,
                candidates=item.candidates,
                next_steps=item.next_steps,
            ).model_dump(mode="json")
            for item in work
        )

    result = DiagnoseResult(
        diagnoses=diagnoses,
        not_diagnosed=skipped,
        series={
            "status": series_read.series.status,
            "count": len(series_read.series.points),
            "reason": series_read.series.reason,
            "skipped": series_read.skipped,
            "removed": series_read.removed,
        },
        sources=read.sources,
        window=read.window,
        price={"incidents": read.price, "checks": checks_price},
        scope=read.scope,
        next_steps=list(read.next_steps),
        notes=notes,
        warnings=warnings,
    )
    if adapter is not None:
        command_args.stamp_spend(result, adapter)
        if checks_price is not None:
            checks_price["settled"] = (
                dict(result.spend) if result.spend is not None else None
            )
        result.spend = _combined(read.spend, result.spend)
    else:
        result.spend = read.spend
    return result


def _billed(spend: dict[str, Any] | None) -> float:
    """What one phase billed, in its connector's unit (its one numeric field
    besides the day's total)."""

    if not spend:
        return 0.0
    for key, value in spend.items():
        if key != "session_spent_today" and isinstance(value, int | float):
            return float(value)
    return 0.0


def _combined(
    first: dict[str, Any] | None, second: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The command's spend: both phases' billed amounts summed; the day's
    total is the later settlement's, which already includes the first."""

    if first is None or second is None:
        return second if first is None else first
    combined = dict(second)
    for key, value in first.items():
        if key == "session_spent_today" or not isinstance(value, int | float):
            continue
        held = combined.get(key)
        combined[key] = (held if isinstance(held, int | float) else 0) + value
    return combined


# --- the inputs -------------------------------------------------------------------


def _history(engine: DexEngine) -> Any:
    """The project's git history, or ``None`` outside a checkout."""

    from ..errors import DexError
    from .commit_history import GitHistory, HistoryUnreadableError

    try:
        root = Path(engine.project_dir())
    except (DexError, ValueError):
        return None
    history = GitHistory(root)
    try:
        # Reads whether the root is a repository at all; `resolve` takes only
        # a commit's object name, never a ref such as HEAD.
        _ = history.shallow
    except HistoryUnreadableError:
        return None
    return history


def _series(engine: DexEngine) -> SeriesRead:
    """The committed series: the versions of ``.dex/snapshot.json`` in the
    repository's history, wherever this engine keeps its own state (a host's
    in-memory store still sits on a repository someone committed baselines
    to)."""

    from .snapshot_series import SeriesRead, read_series

    dex_dir = getattr(engine.store, "dex_dir", None)
    if dex_dir is None and engine.repo_root is not None:
        dex_dir = Path(engine.repo_root) / ".dex"
    if dex_dir is None:
        return SeriesRead(
            diag.Series(
                "unreadable",
                reason="no repository root is set, so no committed snapshot "
                "series can be read",
            )
        )
    return read_series(dex_dir)


# --- the candidates ---------------------------------------------------------------


@dataclass(frozen=True)
class _Spec:
    """One candidate to check: a source relation the tested model reads, and
    the columns the failing uniqueness test claims unique."""

    test: str
    model: str
    source: str
    database: str | None
    schema: str
    name: str
    columns: tuple[str, ...]


@dataclass
class _Work:
    """One open incident, the candidates to check, and what they answered."""

    incident: Incident
    specs: list[_Spec]
    candidates: list[diag.Candidate] = field(default_factory=list)
    next_steps: list[dict[str, str]] = field(default_factory=list)


def _uniqueness_columns(node: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The columns a uniqueness test claims unique, or ``None`` for any other
    test."""

    meta = node.get("test_metadata") or {}
    name = str(meta.get("name") or "")
    keyword = _UNIQUENESS_TESTS.get(name)
    if keyword is None:
        return None
    kwargs = meta.get("kwargs") or {}
    value = kwargs.get(keyword)
    if value is None and keyword == "column_name":
        value = node.get("column_name")
    if isinstance(value, str):
        columns = [value]
    elif isinstance(value, list):
        columns = [str(v) for v in value]
    else:
        return None
    cleaned = tuple(c.strip().strip('"`[]') for c in columns if str(c).strip())
    return cleaned or None


def _tested_model(node: Mapping[str, Any]) -> str | None:
    attached = node.get("attached_node")
    if attached:
        return str(attached)
    for uid in (node.get("depends_on") or {}).get("nodes") or ():
        if str(uid).startswith("model."):
            return str(uid)
    return None


def _upstream_sources(manifest: Mapping[str, Any], model: str) -> list[str]:
    """The sources the model reads, through the models between, nearest first."""

    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    found: list[str] = []
    seen: set[str] = set()
    queue = [model]
    while queue and len(found) < _MAX_UPSTREAM:
        uid = queue.pop(0)
        if uid in seen:
            continue
        seen.add(uid)
        if uid.startswith("source."):
            if uid in sources:
                found.append(uid)
            continue
        node = nodes.get(uid)
        if not isinstance(node, Mapping) or node.get("resource_type") != "model":
            continue
        queue.extend(str(d) for d in (node.get("depends_on") or {}).get("nodes") or ())
    return found


def _specs(manifest: Mapping[str, Any], incident: Incident) -> list[_Spec]:
    """A spec per uniqueness test of the incident and source it reads."""

    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    specs: list[_Spec] = []
    for test in incident.tests:
        node = nodes.get(test)
        if not isinstance(node, Mapping):
            continue
        columns = _uniqueness_columns(node)
        model = _tested_model(node)
        if columns is None or model is None:
            continue
        for uid in _upstream_sources(manifest, model):
            source = sources[uid]
            name = str(source.get("identifier") or source.get("name") or "")
            schema = str(source.get("schema") or "")
            if not name or not schema:
                continue
            database = source.get("database")
            specs.append(
                _Spec(
                    test=test,
                    model=model,
                    source=uid,
                    database=str(database) if database else None,
                    schema=schema,
                    name=name,
                    columns=columns,
                )
            )
    return specs


# --- the checks -------------------------------------------------------------------


@dataclass
class _Planned:
    spec: _Spec
    identifier: str
    plan: GrainPlan
    measured: bool
    before: Snapshot | None


def _loader(status: str, reason: str, detail: str, **fields: Any) -> diag.Candidate:
    """A loader candidate the grain axis did not answer, or answered from the
    bounding snapshot alone."""

    return diag.Candidate(
        owner="loader",
        kind="duplicate_load",
        status=status,  # type: ignore[arg-type]
        reason=reason,
        detail=detail,
        **fields,
    )


def _refuted_before(
    before: Snapshot | None, identifier: str, columns: Sequence[str]
) -> bool:
    """Whether the bounding snapshot measured the tested column not unique on
    the relation: the duplicates were there while the test still passed."""

    if before is None or len(columns) != 1:
        return False
    for dataset in before.warehouse.datasets:
        if dataset.identifier != identifier:
            continue
        for column in dataset.columns:
            if column.name.casefold() == columns[0].casefold():
                return column.is_unique is False
    return False


def _confirm_all(
    engine: DexEngine,
    adapter: Adapter,
    work: Sequence[_Work],
    series_read: SeriesRead,
    *,
    spent_before: float = 0.0,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Plan every candidate's check, price them as one handshake, run them,
    and record each candidate's answer on its work item.

    ``spent_before`` is what the incident read billed under the same
    confirmation: the checks are admitted only within what the per-command
    ceiling has left after it, and offered (``scan_offered``) otherwise.
    """

    from . import drift as drift_mod

    identifiers = {meta.identifier: meta for meta in adapter.list_objects()}
    planned: list[tuple[_Work, _Planned]] = []
    for item in work:
        span = diag.bisect(item.incident.onset)  # type: ignore[arg-type]
        bounds = diag.bound(series_read.series, span)
        before = (
            series_read.snapshots.get(bounds.before.commit)
            if bounds.before is not None
            else None
        )
        carriers: dict[str, list[tuple[_Spec, str, tuple[str, ...]]]] = {}
        unlisted: list[_Spec] = []
        for spec in item.specs:
            identifier = _resolve(identifiers, spec)
            if identifier is None:
                unlisted.append(spec)
                continue
            _, columns = adapter.table_metadata(identifier)
            names = {c.name.casefold(): c.name for c in columns}
            if not all(c.casefold() in names for c in spec.columns):
                continue
            spelled = tuple(names[c.casefold()] for c in spec.columns)
            carriers.setdefault(spec.test, []).append((spec, identifier, spelled))
        for test in dict.fromkeys(spec.test for spec in item.specs):
            held = carriers.get(test, [])
            if not held:
                missing = [s for s in unlisted if s.test == test]
                first = next(s for s in item.specs if s.test == test)
                item.candidates.append(
                    _loader(
                        "not_checked",
                        "source_not_listed" if missing else "columns_not_upstream",
                        "the connection does not list "
                        + ", ".join(sorted({f"{s.schema}.{s.name}" for s in missing}))
                        + " (outside the source allowlist, or not built), so the "
                        "loader's delivery was not held to the test's claim"
                        if missing
                        else "no source relation the tested model reads carries "
                        "the tested columns under the same names, so the loader's "
                        "delivery could not be held to the test's claim",
                        columns=first.columns,
                    )
                )
                continue
            for spec, identifier, spelled in held:
                plan = _plan(adapter, identifier, spelled, spec, before)
                if plan.measured:
                    planned.append((item, plan))
                elif _refuted_before(before, identifier, spelled):
                    item.candidates.append(
                        _loader(
                            "ruled_out",
                            "not_unique_at_before_snapshot",
                            f"the snapshot that bounds the break measured "
                            f"({', '.join(spelled)}) not unique on {identifier} "
                            "while the test still passed, so a duplicate delivery "
                            "since is not what broke it",
                            relation=identifier,
                            columns=spelled,
                            check=GRAIN_CHECK,
                            evidence={
                                "measured_against": {
                                    "snapshot_created_at": before.created_at
                                    if before is not None
                                    else None
                                }
                            },
                        )
                    )
                elif len(held) > 1:
                    # With no measured before, the claim held to today's data
                    # cannot tell a delivery the loader duplicated from a source
                    # at another grain the model reads beside it.
                    item.candidates.append(
                        _loader(
                            "not_checked",
                            "several_sources",
                            f"{len(held)} sources the tested model reads carry "
                            f"({', '.join(spelled)}), and no committed snapshot "
                            "measured which of them held the key unique, so the "
                            "test's claim cannot be held to any one of them",
                            relation=identifier,
                            columns=spelled,
                        )
                    )
                else:
                    planned.append((item, plan))

    estimate = 0.0
    per_table: dict[str, float] = {}
    for _item, held_plan in planned:
        batched = drift_mod.batched_probes(adapter, held_plan.plan)
        total, per = drift_mod.grain_estimate(adapter, held_plan.plan, batched)
        estimate += total
        for key, value in per.items():
            per_table[key] = per_table.get(key, 0.0) + value
    price: dict[str, Any] = {"estimate": estimate, "per_table": per_table}
    if not planned:
        return price
    gate = command_args.cost_gate(adapter)
    left = (
        None
        if gate is None or gate.ceiling is None
        else max(gate.ceiling - spent_before, 0.0)
    )
    if left is not None and estimate > left:
        # One confirmation covers the whole command: what the incident read
        # spent comes out of the same per-command ceiling, and checks that no
        # longer fit are offered rather than asked for a second time.
        price["offered"] = True
        for item, held_plan in planned:
            item.candidates.append(
                _loader(
                    "not_checked",
                    "scan_offered",
                    f"the check on {held_plan.identifier} was priced at what the "
                    "per-command ceiling has left after the incident read, and did "
                    "not fit; it is offered under price.checks",
                    relation=held_plan.identifier,
                    columns=held_plan.spec.columns,
                    check=GRAIN_CHECK,
                )
            )
        return price
    command_args.billed_handshake(
        COMMAND,
        adapter,
        estimate,
        per_table=per_table,
        notes=[
            f"{len(planned)} uniqueness check(s) of the grain axis, one per "
            "source relation a failing uniqueness test's model reads"
        ],
        axes=["grain"],
    )

    config = engine.config
    for item, held_plan in planned:
        batched = drift_mod.batched_probes(adapter, held_plan.plan)
        probes = drift_mod.run_batched_probes(adapter, batched)
        noted = len(held_plan.plan.notes)
        findings = drift_mod.grain_drift(
            adapter,
            held_plan.plan,
            timeout_seconds=config.query.timeout_seconds,
            min_rows=config.maintain.grain_min_rows,
            probes=probes,
        )
        if warnings is not None:
            warnings.extend(held_plan.plan.notes[noted:])
        candidate = _answer(held_plan, findings, adapter)
        item.candidates.append(candidate)
        if candidate.status == "confirmed":
            item.next_steps.append(_repair_step(held_plan))
    return price


def _resolve(identifiers: Mapping[str, Any], spec: _Spec) -> str | None:
    """The source as the adapter spells it, where the connection lists it."""

    want_schema = spec.schema.casefold()
    want_name = spec.name.casefold()
    want_db = (spec.database or "").casefold()
    for identifier in sorted(identifiers):
        parts = identifier.split(".")
        if len(parts) < 2:
            continue
        if parts[-1].casefold() != want_name or parts[-2].casefold() != want_schema:
            continue
        if len(parts) >= 3 and want_db and parts[-3].casefold() != want_db:
            continue
        return identifier
    return None


def _plan(
    adapter: Adapter,
    identifier: str,
    columns: tuple[str, ...],
    spec: _Spec,
    before: Snapshot | None,
) -> _Planned:
    """The grain axis's plan for one source relation.

    With a before snapshot whose measurement proved the key unique, the plan
    ``maintain check`` itself builds from that snapshot, narrowed to this
    relation: a lost key is ``key_lost_uniqueness``. Otherwise the declared
    check, which needs no baseline: the test's claim held to the relation.
    """

    from . import drift as drift_mod

    if before is not None:
        plan = drift_mod.grain_plan(adapter, before, scope={identifier})
        wanted = [c.casefold() for c in columns]
        if len(columns) == 1:
            measured = any(
                wanted[0] in {k.casefold() for k in keys}
                for _ds, keys, _rows in plan.key_checks
            )
        else:
            measured = any(
                any(
                    sorted(k.casefold() for k in combo) == sorted(wanted)
                    for combo in combos
                )
                for _ds, combos, _rows in plan.composite_checks
            )
        if measured:
            # Only the tested key is scanned: the other keys the snapshot
            # proved on the relation are not this incident's.
            single = wanted[0] if len(columns) == 1 else None
            narrowed = plan._replace(
                key_checks=[
                    (ds, [k for k in keys if k.casefold() == single], rows)
                    for ds, keys, rows in plan.key_checks
                    if single is not None and single in {k.casefold() for k in keys}
                ],
                composite_checks=[
                    (
                        ds,
                        [
                            c
                            for c in combos
                            if sorted(k.casefold() for k in c) == sorted(wanted)
                        ],
                        rows,
                    )
                    for ds, combos, rows in plan.composite_checks
                    if single is None
                ],
                widening_checks=[
                    (ds, [c for c in combos if c[-1].casefold() == single], rows)
                    for ds, combos, rows in plan.widening_checks or []
                    if single is not None
                ],
                fanout_pairs=[],
                declared_composite_checks=[],
                declared_join_checks=[],
                join_cardinality_checks=[],
                null_checks=[],
            )
            return _Planned(spec, identifier, narrowed, True, before)
    meta, _ = adapter.table_metadata(identifier)
    plan = drift_mod.GrainPlan(
        key_checks=[],
        fanout_pairs=[],
        composite_checks=[],
        declared_composite_checks=[
            (Dataset(identifier=identifier), [list(columns)], meta.row_count)
        ],
        notes=[],
    )
    return _Planned(spec, identifier, plan, False, before)


def _answer(
    held: _Planned, findings: Iterable[Any], adapter: Adapter
) -> diag.Candidate:
    """The candidate as the grain axis answered it."""

    wanted = sorted(c.casefold() for c in held.spec.columns)
    code = "key_lost_uniqueness" if held.measured else "declared_grain_not_unique"
    hit = None
    for finding in findings:
        if finding.code != code or finding.identifier != held.identifier:
            continue
        named = finding.data.get("columns") or [finding.column]
        if sorted(str(c).casefold() for c in named) == wanted:
            hit = finding
            break
    columns = tuple(held.spec.columns)
    base: dict[str, Any] = {
        "owner": "loader",
        "kind": "duplicate_load",
        "relation": held.identifier,
        "columns": columns,
        "check": GRAIN_CHECK,
        "finding": code,
    }
    members = ", ".join(columns)
    against = (
        "the snapshot that bounds the break, whose measurement proved it unique"
        if held.measured
        else "the failing test's own claim, with no baseline needed"
    )
    if hit is None:
        meta, _ = adapter.table_metadata(held.identifier)
        if meta.row_count is None:
            # Nothing counted the relation (a view, an external table), so the
            # grain axis judged nothing: not checked, never read as unique.
            return diag.Candidate(
                **base,
                status="not_checked",
                reason="uncounted",
                detail=f"nothing counted {held.identifier}'s rows, so the grain "
                f"axis could not judge whether ({members}) is unique on it",
                evidence={"measured_against": _against(held)},
            )
        return diag.Candidate(
            **base,
            status="ruled_out",
            reason="still_unique" if meta.row_count else "no_rows",
            detail=f"({members}) is unique on {held.identifier}, held to {against}: "
            "the loader delivered each key once",
            evidence={"row_count": meta.row_count, "measured_against": _against(held)},
        )
    distinct = hit.data.get("distinct_count")
    rows = hit.data.get("row_count")
    evidence: dict[str, Any] = {
        "distinct_count": distinct,
        "row_count": rows,
        "duplicate_rows": (rows - distinct)
        if isinstance(rows, int) and isinstance(distinct, int)
        else None,
        "measured_against": _against(held),
    }
    if held.measured and held.before is not None:
        evidence["volume"] = _volume(adapter, held)
    return diag.Candidate(
        **base,
        status="confirmed",
        detail=f"({members}) is not unique on {held.identifier}, the source "
        f"{held.spec.model} reads: {distinct} distinct over {rows} rows, held to "
        f"{against}. The loader delivered keys more than once",
        evidence=evidence,
    )


def _against(held: _Planned) -> dict[str, Any]:
    if held.measured and held.before is not None:
        return {"snapshot_created_at": held.before.created_at}
    return {"claim": held.spec.test}


def _volume(adapter: Adapter, held: _Planned) -> dict[str, Any] | None:
    """The volume axis's row counts on the relation since the before snapshot:
    evidence beside the confirmation, never the confirmation itself."""

    from ..cache import ColumnProfile
    from . import drift as drift_mod

    if held.before is None:
        return None
    meta, columns = adapter.table_metadata(held.identifier)
    current = [
        Dataset(
            identifier=meta.identifier,
            object_type=meta.object_type,
            row_count=meta.row_count,
            byte_size=meta.byte_size,
            columns=[
                ColumnProfile(name=c.name, data_type=c.data_type, nullable=c.nullable)
                for c in columns
            ],
        )
    ]
    for finding in drift_mod.volume_drift(current, held.before, {held.identifier}):
        if finding.identifier == held.identifier:
            return {
                "finding": finding.code,
                "row_count_before": finding.data.get("row_count_before"),
                "row_count_after": finding.data.get("row_count_after"),
            }
    return None


def _repair_step(held: _Planned) -> dict[str, str]:
    """The ``transform`` step a confirmed duplicate delivery hands on."""

    model = held.spec.model.split(".")[-1]
    members = ", ".join(held.spec.columns)
    return {
        "command": (
            f'dex transform plan "keep one row per ({members}) from '
            f'{held.identifier} in {model}" --edits-file <edits.json>'
        ),
        "purpose": (
            "hand the repair to transform as a reviewable plan: the loader "
            f"delivered ({members}) more than once, so either the load is "
            "repaired at the loader or the model keeps one row per key; "
            "diagnosis writes neither"
        ),
    }


__all__ = ["COMMAND", "GRAIN_CHECK", "diagnose"]
