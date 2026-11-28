"""Gathering the evidence for the cause kinds beside the duplicate delivery (#1476).

:mod:`.cause_kinds` decides each kind from its evidence; this module reads that
evidence for one incident:

- the failing tests' lineage, from the manifest;
- the files the break's commit range touched, and a seed's or the package
  lock's bytes at the bounding commits, from the repository's history
  (:class:`~.commit_history.GitHistory`, read-only);
- the schema of each source the lineage reads, at the snapshot that bounds the
  break and after it (the snapshot after it, else the live catalog: metadata
  only, free);
- ``maintain verify`` on a model the range changed, in-process;
- both models' build times, from Elementary's ``dbt_run_results``
  (:mod:`.run_times`), priced into the checks' one handshake by the caller;
- each source's rows counted by when they landed against the bounding runs'
  starts (:mod:`.arrival`, #1623), priced into the same handshake.

Read-only: nothing here writes to the warehouse, the project or ``.dex/``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..cache import ColumnProfile, Dataset
from . import cause_kinds as ck
from . import diagnosis as diag

if TYPE_CHECKING:
    from ..adapters.base import Adapter
    from ..engine import DexEngine
    from .incidents import Incident
    from .snapshot import Snapshot
    from .snapshot_series import SeriesRead


#: The default directory dbt reads seeds from.
_SEED_DIRS = ("seeds", "data")


@dataclass
class KindWork:
    """One incident's lineage and the candidates gathered for it."""

    incident: Incident
    found: ck.Lineage | None
    span: diag.DiagnosisRange
    candidates: list[diag.Candidate] = field(default_factory=list)
    next_steps: list[dict[str, str]] = field(default_factory=list)
    #: Every incident of the same read, so a freshness failure in the range
    #: can corroborate a late source.
    others: tuple[Incident, ...] = ()


def merged_lineage(
    manifest: Mapping[str, Any], incident: Incident, columns: Sequence[str]
) -> ck.Lineage | None:
    """One lineage for every test of the incident, merged."""

    held = [
        found
        for test in incident.tests
        if (found := ck.lineage(manifest, test, columns=columns)) is not None
    ]
    if not held:
        return None
    if len(held) == 1:
        return held[0]
    first = held[0]
    files: dict[str, tuple[str, ...]] = {}
    for found in held:
        for path, uids in found.files.items():
            files[path] = tuple(dict.fromkeys([*files.get(path, ()), *uids]))
    return ck.Lineage(
        test=first.test,
        model=first.model,
        project=first.project,
        nodes=tuple(dict.fromkeys(n for f in held for n in f.nodes)),
        models={k: v for f in held for k, v in f.models.items()},
        seeds={k: v for f in held for k, v in f.seeds.items()},
        sources=tuple(dict.fromkeys(s for f in held for s in f.sources)),
        files=files,
        packages=tuple(dict.fromkeys(p for f in held for p in f.packages)),
        pairs=tuple(dict.fromkeys(p for f in held for p in f.pairs))[: ck.MAX_PAIRS],
        code=tuple(c for f in held for c in f.code),
        columns=tuple(dict.fromkeys(c for f in held for c in f.columns)),
    )


# --- the git-read kinds -------------------------------------------------------------


def git_kinds(
    engine: DexEngine,
    history: Any,
    work: KindWork,
    verified: dict[tuple[str, ...], Any] | None = None,
) -> None:
    """The project, the seed and the package candidates, from the files the
    break's commit range touched. ``verified`` is the command's cache of
    ``maintain verify`` sweeps by the models they selected."""

    found = work.found
    bounds = ck.range_bounds(work.span)
    if isinstance(bounds, diag.Candidate):
        for owner, kind in (
            ("project", "project_change"),
            ("seed", "seed_edited"),
            ("package", "package_upgraded"),
        ):
            work.candidates.append(ck.unknown_range(bounds, owner, kind))
        return
    if not bounds.after:
        # same_commit: no file changed across the break.
        commit = bounds.through
        work.candidates.append(ck.same_commit("project", "project_change", commit))
        work.candidates.append(ck.same_commit("seed", "seed_edited", commit))
        if _text(history, commit, ck.LOCK_FILE) is None and _declares(history, commit):
            work.candidates.append(
                ck.package_candidate(
                    found or ck.Lineage(test="", model=None, project=""),
                    None,
                    None,
                    declared=True,
                    bounds=bounds,
                )
            )
        else:
            work.candidates.append(
                ck.same_commit("package", "package_upgraded", commit)
            )
        return

    from .commit_history import HistoryUnreadableError

    try:
        touched, count = history.touching(bounds.through, bounds.after)
    except (HistoryUnreadableError, ValueError) as exc:
        for owner, kind in (
            ("project", "project_change"),
            ("seed", "seed_edited"),
            ("package", "package_upgraded"),
        ):
            work.candidates.append(
                diag.Candidate(
                    owner=owner,  # type: ignore[arg-type]
                    kind=kind,
                    status="not_checked",
                    reason="history_unreadable",
                    detail=f"the files the break's range touched could not be "
                    f"listed ({exc})",
                )
            )
        return
    by_file: dict[str, list[str]] = {}
    for sha, files in touched:
        for path in files:
            by_file.setdefault(path, []).append(sha)
    seed_paths = list(found.seeds.values()) if found is not None else []
    owned = ck.partition(by_file, seed_paths, _SEED_DIRS)

    def commits_of(paths: Sequence[str]) -> int:
        return len({sha for path in paths for sha in by_file.get(path, ())})

    cut = count > len(touched)

    def settle(candidate: diag.Candidate, *, by_range: bool = False) -> diag.Candidate:
        """A rule-out an incomplete read cannot support, as not checked."""

        if by_range and cut:
            candidate = ck.unproven(
                candidate,
                "range_truncated",
                f"the break's range holds {count} commits and {len(touched)} "
                "were listed",
            )
        if found is not None and found.truncated:
            candidate = ck.unproven(
                candidate,
                "lineage_truncated",
                "the walk upstream of the failing test stopped at its limit with "
                "nodes left unread",
            )
        return candidate

    # The project.
    if found is None:
        work.candidates.append(
            ck.project_not_in_lineage(
                ck.ProjectChange(commits_of(owned["project"]), (), (), False),
                len(owned["project"]),
            )
        )
    else:
        change = ck.project_change(
            found, owned["project"], commits_of(owned["project"])
        )
        if not change.files:
            work.candidates.append(
                settle(
                    ck.project_not_in_lineage(change, len(owned["project"])),
                    by_range=True,
                )
            )
        elif not change.models:
            work.candidates.append(ck.project_test_only(change))
        else:
            candidate = _verify(
                engine,
                found,
                change,
                {} if verified is None else verified,
                history=history,
                through=bounds.through,
            )
            work.candidates.append(candidate)
            if candidate.status == "confirmed":
                work.next_steps.append(_project_step(candidate))

    # The seed.
    lineage = found or ck.Lineage(test="", model=None, project="")
    hashes = {
        uid: (
            [history.blob_id(commit, path) for commit in bounds.after],
            history.blob_id(bounds.through, path),
        )
        for uid, path in lineage.seeds.items()
    }
    for candidate in ck.seed_candidates(
        lineage, hashes, bounds, {p: by_file.get(p, []) for p in owned["seed"]}
    ):
        candidate = settle(candidate)
        work.candidates.append(candidate)
        if candidate.status == "confirmed":
            work.next_steps.append(_seed_step(candidate))

    # The package.
    before = ck.parse_lock(_text(history, bounds.last_good, ck.LOCK_FILE))
    after = ck.parse_lock(_text(history, bounds.through, ck.LOCK_FILE))
    candidate = ck.package_candidate(
        lineage,
        before,
        after,
        declared=_declares(history, bounds.through)
        or _declares(history, bounds.last_good),
        bounds=bounds,
        commits=sorted(
            {sha for path in owned["package"] for sha in by_file.get(path, ())}
        ),
    )
    candidate = settle(candidate)
    work.candidates.append(candidate)
    if candidate.status == "confirmed":
        work.next_steps.append(_package_step(candidate))


def _text(history: Any, commit: str, path: str) -> str | None:
    from .commit_history import HistoryUnreadableError

    try:
        return history.blob(commit, path)
    except (HistoryUnreadableError, ValueError):
        return None


def _declares(history: Any, commit: str) -> bool:
    for name in ("packages.yml", "dependencies.yml"):
        text = _text(history, commit, name)
        if text is not None and "packages" in text:
            return True
    return False


def _verify(
    engine: DexEngine,
    found: ck.Lineage,
    change: ck.ProjectChange,
    verified: dict[tuple[str, ...], Any],
    *,
    history: Any = None,
    through: str | None = None,
) -> diag.Candidate:
    """``maintain verify`` on the changed models, in this process. ``verified``
    holds each sweep for the life of one command, so two incidents on one
    changed model are judged by one sweep.

    Verify reads the project as it stands. Where a changed file reads
    otherwise now than at the failing run's commit (a repair, or a later
    edit), its findings describe today's model, not the one that failed: they
    are kept as evidence and the candidate is not checked."""

    from ..errors import DexError

    names = {uid: found.models[uid] for uid in change.models if uid in found.models}
    selected = tuple(sorted(set(names.values())))
    try:
        if selected not in verified:
            verified[selected] = engine.verify(list(selected))
        result = verified[selected]
    except DexError as exc:
        return diag.Candidate(
            owner="project",
            kind="project_change",
            status="not_checked",
            check=ck.VERIFY_CHECK,
            reason="verify_refused",
            detail=f"maintain verify on the changed model was refused: {exc}",
            evidence={"models": sorted(names.values())},
        )
    candidate = ck.project_verified(
        change,
        names,
        result.findings,
        offered=getattr(result, "pending_offer", None) is not None,
        suppressed=result.suppressed,
    )
    moved = _moved_since(engine, history, through, change.files)
    if moved and candidate.status in ("confirmed", "ruled_out"):
        return candidate.model_copy(
            update={
                "status": "not_checked",
                "reason": "changed_since_failure",
                "detail": f"{', '.join(moved)} changed after the failing run's "
                "commit, so maintain verify judged today's model, not the one "
                f"that failed (it would read {candidate.status}"
                + (f": {candidate.finding}" if candidate.finding else "")
                + ")",
                "evidence": {
                    **candidate.evidence,
                    "changed_since_failure": moved,
                    "verify_today": candidate.status,
                },
            }
        )
    return candidate


def _moved_since(
    engine: DexEngine, history: Any, through: str | None, files: Sequence[str]
) -> list[str]:
    """The files that read otherwise in the working tree than at ``through``."""

    from pathlib import Path

    from ..errors import DexError

    if history is None or through is None:
        return []
    try:
        root = Path(engine.project_dir())
    except (DexError, ValueError):
        return []
    moved: list[str] = []
    for path in files:
        then = _text(history, through, path)
        try:
            now = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            now = None
        if then != now:
            moved.append(path)
    return moved


# --- the loader: a retyped source column ---------------------------------------------


def retype(
    work: KindWork,
    manifest: Mapping[str, Any],
    series_read: SeriesRead,
    adapter_for: Any,
) -> None:
    """The loader's retype candidates, from the schema axis between the
    snapshot that bounds the break and the warehouse after it."""

    from . import drift as drift_mod

    found = work.found
    if found is None or not found.sources:
        work.candidates.append(
            ck.retype_unmeasured(
                "no_source_in_lineage",
                "the failing test and the models it reads read no source of the "
                "project, so no loader's column can have been retyped under it",
            )
        )
        return
    bounds = diag.bound(series_read.series, work.span)
    before = (
        series_read.snapshots.get(bounds.before.commit)
        if bounds.before is not None
        else None
    )
    if before is None:
        work.candidates.append(
            ck.retype_unmeasured(
                "no_measured_before",
                "no committed snapshot measured the sources' columns before the "
                "break, so a retype across it cannot be shown: a column's type "
                "needs a baseline",
            )
        )
        return
    later: Snapshot | None = (
        series_read.snapshots.get(bounds.after.commit)
        if bounds.after is not None
        else None
    )
    sources = manifest.get("sources") or {}
    baseline = {d.identifier: d for d in before.warehouse.datasets}
    adapter: Adapter | None = None
    checked: list[str] = []
    unmeasured: list[str] = []
    retyped: dict[str, list[Any]] = {}
    relation_sources: dict[str, str] = {}
    for uid in found.sources:
        source = sources.get(uid) or {}
        identifier = _match(
            list(baseline),
            str(source.get("schema") or ""),
            str(source.get("identifier") or source.get("name") or ""),
            str(source.get("database") or "") or None,
        )
        if identifier is not None:
            relation_sources[identifier] = uid
        if identifier is None or not baseline[identifier].columns:
            name = source.get("identifier") or source.get("name")
            unmeasured.append(f"{source.get('schema')}.{name}")
            continue
        if later is not None:
            current = [
                d for d in later.warehouse.datasets if d.identifier == identifier
            ]
        else:
            if adapter is None:
                adapter = adapter_for()
            current = _live(adapter, identifier)
        if not current:
            # Gone since: the schema axis reports a dropped table, not a retype.
            unmeasured.append(identifier)
            continue
        findings = drift_mod.schema_drift(current, before, scope={identifier})
        retyped[identifier] = [f for f in findings if f.code == "column_retyped"]
        checked.append(identifier)
    against: dict[str, Any] = {"snapshot_before_created_at": before.created_at}
    against["after"] = (
        {"snapshot_created_at": later.created_at}
        if later is not None
        else "the live catalog"
    )
    for candidate in ck.retype_candidates(
        found,
        retyped,
        against,
        checked=checked,
        unmeasured=unmeasured,
        sources=relation_sources,
    ):
        work.candidates.append(candidate)
        if candidate.status == "confirmed":
            work.next_steps.append(_retype_step(candidate))


def _match(
    identifiers: Sequence[str], schema: str, name: str, database: str | None = None
) -> str | None:
    """The one identifier naming the source, by database where the manifest
    names one; ``None`` where none does, or several do and nothing tells them
    apart (never the first of them)."""

    want_schema, want_name = schema.casefold(), name.casefold()
    want_db = (database or "").casefold()
    found: list[str] = []
    for identifier in sorted(identifiers):
        parts = identifier.split(".")
        if len(parts) < 2:
            continue
        if parts[-1].casefold() != want_name or parts[-2].casefold() != want_schema:
            continue
        if want_db and len(parts) >= 3 and parts[-3].casefold() != want_db:
            continue
        found.append(identifier)
    return found[0] if len(found) == 1 else None


def _live(adapter: Adapter, identifier: str) -> list[Dataset]:
    listed = {meta.identifier for meta in adapter.list_objects()}
    if identifier not in listed:
        return []
    meta, columns = adapter.table_metadata(identifier)
    return [
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


# --- the schedule --------------------------------------------------------------------


@dataclass
class SchedulePlan:
    """The one run-times read every incident's schedule candidates share."""

    plan: Any
    estimate: float
    per_table: dict[str, float]
    works: list[KindWork]


def plan_schedule(
    engine: DexEngine,
    works: Sequence[KindWork],
    elementary: Mapping[str, Any] | None,
    adapter_for: Any,
) -> SchedulePlan | None:
    """Plan the run-times read, or record why the schedule is not checked.

    Returns ``None`` where nothing is read: each incident then already holds
    its schedule candidate.
    """

    wanting: list[KindWork] = []
    for work in works:
        found = work.found
        if found is None or not found.pairs:
            work.candidates.append(ck.no_pairs())
            continue
        if work.span.after == "unknown":
            work.candidates.extend(
                ck.schedule_candidate(pair, found.models, (), work.span)
                for pair in found.pairs
            )
            continue
        wanting.append(work)
    if not wanting:
        return None

    def unread(reason: str, detail: str) -> None:
        for work in wanting:
            work.candidates.append(ck.schedule_unread(reason, detail))

    if not elementary or elementary.get("status") != "read":
        unread(
            "no_run_times",
            "build times are read from Elementary's dbt_run_results, and "
            "Elementary's tables were not read ("
            + str((elementary or {}).get("reason") or "no source")
            + ")",
        )
        return None
    namespace = elementary.get("namespace")
    release = elementary.get("release")
    if not namespace:
        unread("no_run_times", "Elementary's schema was not located")
        return None

    from ..errors import DexError
    from . import run_times
    from .elementary_layouts import ElementaryLayoutError

    models = sorted(
        {uid for work in wanting for pair in work.found.pairs for uid in pair}  # type: ignore[union-attr]
    )
    latest = max(
        (w.span.through.at for w in wanting if w.span.through.at is not None),
        default=None,
    )
    try:
        adapter = adapter_for()
        held = run_times.plan(engine, adapter, namespace, release, models, latest)
        estimate, lines = run_times.price(engine, adapter, held)
    except ElementaryLayoutError as exc:
        unread(
            "no_run_times",
            f"Elementary's {run_times.TABLE} does not hold build times Dex can "
            f"read ({exc.code})",
        )
        return None
    except DexError as exc:
        unread("no_run_times", f"{run_times.TABLE} could not be planned: {exc}")
        return None
    return SchedulePlan(held, estimate, lines, wanting)


def run_schedule(
    engine: DexEngine, adapter: Any, planned: SchedulePlan, command: str
) -> None:
    """Run the planned read and decide each pair's candidate."""

    from ..envelope import failure_text
    from ..errors import DexError, WarehouseQueryError
    from ..guards.query_firewall import QueryRefusedError
    from . import run_times

    try:
        builds = run_times.read(engine, adapter, planned.plan, command)
    except QueryRefusedError as exc:
        _unread_all(planned, "firewall_refused", str(exc))
        return
    except WarehouseQueryError as exc:
        _unread_all(planned, "warehouse_refused", failure_text(exc))
        return
    except DexError as exc:
        from .incident_sources import _reraise_cost

        _reraise_cost(exc)
        _unread_all(planned, "no_run_times", str(exc))
        return
    for work in planned.works:
        found = work.found
        assert found is not None  # noqa: S101
        for pair in found.pairs:
            candidate = ck.schedule_candidate(pair, found.models, builds, work.span)
            work.candidates.append(candidate)
            if candidate.status == "confirmed":
                work.next_steps.append(_schedule_step(candidate))


def offer_schedule(planned: SchedulePlan) -> None:
    _unread_all(
        planned,
        "scan_offered",
        "the build-times read was priced at what the per-command ceiling has "
        "left after the incident read, and did not fit; it is offered under "
        "price.checks",
    )


def _unread_all(planned: SchedulePlan, reason: str, detail: str) -> None:
    for work in planned.works:
        work.candidates.append(ck.schedule_unread(reason, detail))


# --- the loader: rows that arrived after the run read them ---------------------------


@dataclass
class ArrivalPlan:
    """The count statements every incident's late-source candidates share."""

    probes: list[Any]
    estimate: float
    per_table: dict[str, float]


def plan_arrival(
    engine: DexEngine,
    works: Sequence[KindWork],
    manifest: Mapping[str, Any],
    adapter_for: Any,
) -> ArrivalPlan | None:
    """Plan each source's count, or record why it is not checked.

    Returns ``None`` where nothing is read: each incident then already holds
    its late-source candidates (or none, where its tests are not ones a late
    source fails).
    """

    from ..errors import DexError
    from . import arrival

    wanting = [w for w in works if arrival.scoped_tests(manifest, w.incident.tests)]
    if not wanting:
        return None
    try:
        adapter = adapter_for()
        identifiers = [meta.identifier for meta in adapter.list_objects()]
    except DexError as exc:
        for work in wanting:
            work.candidates.append(
                ck.arrival_unread(
                    "source_not_listed",
                    f"the source catalog could not be read: {exc}",
                )
            )
        return None
    probes = [
        probe
        for work in wanting
        for probe in arrival.plan(engine, adapter, work, manifest, identifiers)
    ]
    if not probes:
        return None
    try:
        estimate, lines = arrival.price(engine, adapter, probes)
    except DexError as exc:
        for probe in probes:
            probe.work.candidates.append(
                ck.arrival_unread(
                    "warehouse_refused",
                    f"the count over {probe.identifier} could not be priced: {exc}",
                    relation=probe.relation,
                )
            )
        return None
    return ArrivalPlan(probes, estimate, lines)


def run_arrival(
    engine: DexEngine, adapter: Any, planned: ArrivalPlan, command: str
) -> None:
    """Run the planned counts and decide each source's candidate."""

    from ..envelope import failure_text
    from ..errors import DexError, WarehouseQueryError
    from ..guards.query_firewall import QueryRefusedError
    from . import arrival

    try:
        answers = arrival.read(engine, adapter, planned.probes, command)
    except DexError as exc:
        from .incident_sources import _reraise_cost

        _reraise_cost(exc)
        _arrival_unread_all(planned, "warehouse_refused", failure_text(exc))
        return
    for probe, answer in zip(planned.probes, answers, strict=True):
        work = probe.work
        if isinstance(answer, QueryRefusedError):
            work.candidates.append(
                ck.arrival_unread("firewall_refused", str(answer), probe.relation)
            )
            continue
        if isinstance(answer, WarehouseQueryError):
            work.candidates.append(
                ck.arrival_unread(
                    "warehouse_refused", failure_text(answer), probe.relation
                )
            )
            continue
        if isinstance(answer, BaseException):
            from .incident_sources import _reraise_cost

            if isinstance(answer, DexError):
                _reraise_cost(answer)
            work.candidates.append(
                ck.arrival_unread(
                    "warehouse_refused",
                    f"the count over {probe.identifier} returned no answer: "
                    f"{failure_text(answer)}",
                    probe.relation,
                )
            )
            continue
        candidate = ck.arrival_candidate(
            probe.relation,
            probe.columns,
            answer,
            work.span,
            probe.corroborated_by,
            lag_seconds=probe.lag_seconds,
        )
        work.candidates.append(candidate)
        if candidate.status == "confirmed":
            work.next_steps.append(_arrival_step(candidate, work))


def offer_arrival(planned: ArrivalPlan) -> None:
    _arrival_unread_all(
        planned,
        "scan_offered",
        "the count of the source's rows by capture time was priced at what the "
        "per-command ceiling has left after the incident read, and did not fit; "
        "it is offered under price.checks",
    )


def _arrival_unread_all(planned: ArrivalPlan, reason: str, detail: str) -> None:
    for probe in planned.probes:
        probe.work.candidates.append(
            ck.arrival_unread(reason, detail, relation=probe.relation)
        )


# --- the repair steps ----------------------------------------------------------------


def _retype_step(candidate: diag.Candidate) -> dict[str, str]:
    column = candidate.columns[0] if candidate.columns else "the column"
    return {
        "command": f"dex explore profile {candidate.relation}",
        "purpose": (
            f"the loader retyped {column} on {candidate.relation}: profile it "
            "as it now stands, then hand the repair (a cast in the staging model, "
            "or a fix at the loader) to transform as a reviewable plan; "
            "diagnosis writes neither"
        ),
    }


def _project_step(candidate: diag.Candidate) -> dict[str, str]:
    models = " ".join(candidate.evidence.get("models") or [])
    return {
        "command": f"dex maintain verify {models}".strip(),
        "purpose": (
            "read the defects verify reports on the changed model in full, then "
            "hand the repair to transform as a reviewable plan "
            "(dex transform plan); diagnosis writes none"
        ),
    }


def _seed_step(candidate: diag.Candidate) -> dict[str, str]:
    path = candidate.evidence.get("path") or candidate.relation
    return {
        "command": f'dex transform plan "repair seed {candidate.relation}" '
        "--edits-file <edits.json>",
        "purpose": (
            f"the seed {path} was edited in the break's range: restore or correct "
            "its rows as a reviewable plan; diagnosis writes none"
        ),
    }


def _package_step(candidate: diag.Candidate) -> dict[str, str]:
    packages = ", ".join(candidate.evidence.get("packages") or [])
    return {
        "command": f'dex transform plan "pin {packages} to the version the last '
        'passing run used" --edits-file <edits.json>',
        "purpose": (
            f"package-lock.yml moved {packages} in the break's range: pin it back "
            "or adapt the project to the new release, as a reviewable plan "
            "(a packages_yml edit, then dex transform deps); diagnosis writes none"
        ),
    }


def _schedule_step(candidate: diag.Candidate) -> dict[str, str]:
    parent = candidate.evidence.get("parent")
    child = candidate.evidence.get("child")
    return {
        "command": f"dex maintain incidents {child}",
        "purpose": (
            f"the schedule built {parent} after {child}: order the jobs so "
            f"{parent} is built before {child} (or build both in one invocation), "
            "then read the incident again; the orchestrator's schedule is not "
            "a project file, so diagnosis writes nothing"
        ),
    }


def _arrival_step(candidate: diag.Candidate, work: KindWork) -> dict[str, str]:
    evidence = candidate.evidence
    late = evidence.get("late_rows")
    event = evidence.get("event_column")
    found = work.found
    model = (
        found.models.get(found.model, found.model.split(".")[-1])
        if found is not None and found.model
        else candidate.relation
    )
    return {
        "command": f"dex maintain incidents {model}",
        "purpose": (
            f"the loader delivered {late} row(s) of {candidate.relation} after the "
            f"run that read it started: once they have landed, rebuild {model} and "
            "read the incident again. If the delay recurs, declare a "
            f"freshness.deadline on {event} so the next late delivery is caught "
            "when it happens, or schedule the run later; the orchestrator's "
            "schedule is not a project file, so diagnosis writes nothing"
        ),
    }


__all__ = [
    "ArrivalPlan",
    "KindWork",
    "SchedulePlan",
    "git_kinds",
    "merged_lineage",
    "offer_arrival",
    "offer_schedule",
    "plan_arrival",
    "plan_schedule",
    "retype",
    "run_arrival",
    "run_schedule",
]
