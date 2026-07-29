"""Maintain orchestration, in two layers.

The lower layer is the run functions: they take an :class:`~..engine.DexEngine`,
load the baseline and the project, drive the drift engine, and return a record
from :mod:`.results`. The upper layer is the ``cmd_*`` shims: argparse in,
envelope out.

Only this layer reaches the store; the detectors in ``drift.py`` stay pure
comparisons so they are testable without a warehouse. Detection commands save
their findings as a drift report so the stateless ``reconcile`` has one to read.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .. import command_args
from .. import envelope as env
from ..config import pii_override_paths
from ..dbt_project import DbtProjectError
from ..dbt_project import load as load_project
from ..errors import PrerequisiteError
from ..results import ConfirmationRequest, to_envelope
from ..storage import Document, FilesystemStore, MaintainStore
from . import drift as drift_mod
from . import snapshot as snapshot_mod
from .results import DriftResult, LayerFingerprint, ReconcileResult, SnapshotResult

if TYPE_CHECKING:
    from ..engine import DexEngine

_SNAPSHOT_HINT = (
    "re-run `maintain snapshot` after each known-good build so drift is measured "
    "against a state someone vouched for"
)

#: Prepended when the baseline really is a file in the user's repo. Advice about
#: git only means something there, and telling a host whose baseline is a row to
#: commit a path would name a file it does not have.
_REVIEWABLE_SNAPSHOT_HINT = "commit .dex/snapshot.json like a lockfile, and "

_NO_SNAPSHOT_ERROR = (
    "no drift baseline yet; run `maintain snapshot` first (ideally right after "
    "a known-good build)"
)


class NoBaselineError(PrerequisiteError):
    """A detector was asked to measure drift with nothing to measure against.

    Named for the state rather than for a missing file: on the filesystem
    backend the baseline is `.dex/snapshot.json`, elsewhere it is a row, and the
    fix is the same either way.
    """


def snapshot(engine: DexEngine) -> SnapshotResult:
    """Capture the known-good baseline every drift axis is measured against.

    Prefers the exploration cache, which carries the grain and cardinality
    signals metadata alone cannot see; falls back to a metadata-only capture
    (free on every connector, so no handshake) and says so, because a
    metadata-only baseline silently disarms two of the four axes.
    """

    store = engine.store
    config = engine.config
    warnings: list[str] = []

    cache = store.load_cache()
    requested = engine.connector or config.connector
    usable = cache is not None and bool(cache.datasets)
    if usable and cache.provenance.connector not in (None, requested):
        warnings.append(
            f"the exploration cache was mapped on '{cache.provenance.connector}' but "
            f"the active connector is '{requested}'; capturing a fresh "
            "metadata-only baseline instead"
        )
        usable = False

    if usable:
        warehouse = snapshot_mod.warehouse_from_cache(cache)
        connector = cache.provenance.connector or requested
        warehouse_from = "cache"
        cache_updated_at = cache.provenance.updated_at
    else:
        # No cache to pin: capture directly. Metadata is free on every
        # connector, so this path needs no confirm handshake.
        adapter = engine._adapter("maintain snapshot")
        warehouse = snapshot_mod.warehouse_from_metadata(adapter)
        connector = adapter.name
        warehouse_from = "metadata"
        cache_updated_at = None
        if cache is None or not cache.datasets:
            warnings.append(
                "no exploration cache to pin, so this baseline is metadata-only "
                "(schema and volume axes); run `explore map` and re-snapshot "
                "to give the grain and cardinality axes a baseline"
            )

    transform_layer = semantic_layer = None
    try:
        view = load_project(engine.project_dir())
        transform_layer = snapshot_mod.transform_layer(view)
        semantic_layer = snapshot_mod.semantic_layer(view)
    except (DbtProjectError, ValueError) as exc:
        warnings.append(
            f"no dbt project fingerprinted ({exc}); the semantic axis and "
            "reconcile need one"
        )

    snap = snapshot_mod.Snapshot(
        created_at=datetime.now(UTC).isoformat(),
        connector=connector,
        warehouse=warehouse,
        warehouse_from=warehouse_from,
        cache_updated_at=cache_updated_at,
        transform_layer=transform_layer,
        semantic_layer=semantic_layer,
    )
    locator = store.save_snapshot(snap)

    return SnapshotResult(
        snapshot=snap,
        snapshot_path=locator,
        warehouse_from=warehouse_from,
        dataset_count=len(warehouse.datasets),
        relationship_count=len(warehouse.relationships),
        grain_baseline_count=sum(1 for d in warehouse.datasets if d.candidate_keys),
        cache_updated_at=cache_updated_at,
        transform_layer=(
            LayerFingerprint(
                file_count=len(transform_layer.files),
                model_count=len(transform_layer.models),
                source_count=len(transform_layer.sources),
            )
            if transform_layer is not None
            else None
        ),
        semantic_layer=(
            LayerFingerprint(
                semantic_model_count=len(semantic_layer.semantic_models),
                metric_count=len(semantic_layer.metrics),
            )
            if semantic_layer is not None
            else None
        ),
        warnings=warnings,
    )


def cmd_snapshot(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    # Which advice is true depends on where the baseline landed, so the hint is
    # built from the store rather than fixed. The shipped filesystem backend is
    # the only one dex knows puts a reviewable file in the repo; a backend it
    # does not ship gets the half that holds everywhere, which is better than
    # confidently naming a file that does not exist. `snapshot_path` carries the
    # real location either way.
    reviewable = isinstance(engine.store, FilesystemStore)
    hint = (_REVIEWABLE_SNAPSHOT_HINT if reviewable else "") + _SNAPSHOT_HINT
    return to_envelope(snapshot(engine), hints={"hint": hint})


def schema_drift(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """Source columns and tables added, dropped, retyped, or renamed.

    Unlike ``volume_drift``, this loads the live project (degrading quietly,
    never raising, when none is available) so ``orphan_relation`` can compare
    the baseline's project fingerprint against the current one; that need is
    schema-only, so this has its own body instead of going through
    ``_detect_free_axis``.
    """

    store = engine.store
    snap = store.load_snapshot()
    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)

    warnings: list[str] = []
    current_transform = None
    try:
        view = load_project(engine.project_dir())
        current_transform = snapshot_mod.transform_layer(view)
    except (DbtProjectError, ValueError) as exc:
        warnings.append(
            f"orphan-relation classification skipped (no dbt project: {exc})"
        )

    adapter = engine._adapter("maintain schema")
    current = snapshot_mod.warehouse_from_metadata(adapter).datasets
    cost = command_args.preflight_cost(adapter)
    connector = adapter.name

    scope_names = list(objects or [])
    scope = _resolve_scope(scope_names, current, snap)
    findings = drift_mod.schema_drift(current, snap, scope, current_transform)
    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)

    _record_axes(store, snap, connector, {"schema": (ranked, scope_names)})
    result = _drift_result(
        {"schema": ranked},
        snap,
        store,
        warnings=warnings + _staleness_warnings(store, snap),
    )
    result.cost = cost
    return result


def volume_drift(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """A row count that collapsed, a table that emptied, a load that half-failed."""

    return _detect_free_axis(engine, "volume", drift_mod.volume_drift, objects)


def cmd_schema(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(schema_drift(engine, getattr(args, "objects", None)))


def cmd_volume(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(volume_drift(engine, getattr(args, "objects", None)))


def grain_drift(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """A key that lost uniqueness, a changed row-per-entity cardinality, fanout.

    This axis scans (exact distinct counts, overlap probes), so on a billed
    connector it goes through the confirm handshake with a dry-run estimate of
    exactly the statements it would run. Free connectors run immediately.
    """

    store = engine.store
    snap = store.load_snapshot()
    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)
    scope_names = list(objects or [])

    adapter = engine._adapter("maintain grain")
    connector = adapter.name
    # Guarded rather than passed straight through: `list_objects` is a metadata
    # round trip, and an unscoped run has no use for it.
    scope = (
        _resolve_scope(scope_names, adapter.list_objects(), snap)
        if scope_names
        else None
    )
    plan = drift_mod.grain_plan(adapter, snap, scope)
    if plan.key_checks or plan.fanout_pairs or plan.composite_checks:
        estimate, per_table = drift_mod.grain_estimate(adapter, plan)
        command_args.billed_handshake(
            "maintain grain", adapter, estimate, per_table=per_table
        )
    findings = drift_mod.grain_drift(
        adapter, plan, timeout_seconds=engine.config.query.timeout_seconds
    )
    noted = {dataset.identifier for dataset, _keys, _rows in plan.key_checks} | {
        dataset.identifier for dataset, _combos, _rows in plan.composite_checks
    }
    notes = _adapter_notes(adapter, sorted(noted))

    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)
    _record_axes(store, snap, connector, {"grain": (ranked, scope_names)})
    result = _drift_result(
        {"grain": ranked},
        snap,
        store,
        warnings=_grain_baseline_warnings(snap)
        + _staleness_warnings(store, snap)
        + notes,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_grain(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(grain_drift(engine, getattr(args, "objects", None)))


def semantic_drift(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """Definitions that no longer match: dangling references, new categoricals.

    Two-phase on billed connectors: definition and reference checks are free and
    run immediately; the dimension-cardinality scan waits behind the handshake,
    and an unconfirmed call still returns the complete free findings alongside
    the estimate rather than throwing away work that cost nothing but is real.
    """

    store = engine.store
    snap = store.load_snapshot()
    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)
    try:
        view = load_project(engine.project_dir())
    except (DbtProjectError, ValueError) as exc:
        raise DbtProjectError(f"the semantic axis needs a dbt project: {exc}") from exc

    warnings: list[str] = []
    if snap.semantic_layer is None:
        warnings.append(
            "the baseline has no semantic fingerprint, so every definition "
            "reads as new; re-run `maintain snapshot` to fix the baseline"
        )
    current_transform = snapshot_mod.transform_layer(view)
    current_semantic = snapshot_mod.semantic_layer(view)
    scope_names = list(objects or [])

    adapter = engine._adapter("maintain semantic")
    connector = adapter.name
    current_datasets = snapshot_mod.warehouse_from_metadata(adapter).datasets
    free_findings = _semantic_scope(
        drift_mod.semantic_free_drift(
            current_transform, current_semantic, current_datasets, snap
        ),
        scope_names,
    )
    checks = drift_mod.cardinality_plan(current_semantic, snap)
    pending: ConfirmationRequest | None = None
    billed_findings: list[drift_mod.DriftFinding] = []
    if checks:
        estimate, per_table = drift_mod.cardinality_estimate(adapter, checks)
        pending = command_args.confirmation_request(
            "maintain semantic",
            adapter,
            estimate,
            per_table=per_table,
            notes=[
                "the definition and reference checks are free and already "
                "complete (their findings are included in this envelope); "
                "the estimate covers only the dimension-cardinality scan"
            ],
        )
    if pending is None:
        billed_findings = _semantic_scope(
            drift_mod.cardinality_drift(adapter, checks, current_semantic),
            scope_names,
        )

    ranked = drift_mod.rank_findings(free_findings + billed_findings)
    _record_axes(store, snap, connector, {"semantic": (ranked, scope_names)})
    if pending is not None:
        # The free half is complete and real, so it returns alongside the ask
        # for the scanning half rather than being discarded and re-derived.
        result = _drift_result({"semantic": ranked}, snap, store, warnings=warnings)
        result.pending_confirmation = pending
        return result
    result = _drift_result(
        {"semantic": ranked},
        snap,
        store,
        warnings=warnings + _staleness_warnings(store, snap),
    )
    return command_args.stamp_spend(result, adapter)


def cmd_semantic(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(semantic_drift(engine, getattr(args, "objects", None)))


def check(engine: DexEngine) -> DriftResult:
    """The everyday sweep across every axis.

    Two-phase by construction: the free axes (schema, volume, semantic
    references) always run and their findings always return; the scanning axes
    (grain, cardinality) run immediately on free connectors and behind one
    combined estimate on billed ones.
    """

    store = engine.store
    snap = store.load_snapshot()
    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)
    config = engine.config

    warnings = _grain_baseline_warnings(snap)
    current_transform = current_semantic = None
    project_available = True
    try:
        view = load_project(engine.project_dir())
        current_transform = snapshot_mod.transform_layer(view)
        current_semantic = snapshot_mod.semantic_layer(view)
    except (DbtProjectError, ValueError) as exc:
        project_available = False
        warnings.append(f"semantic axis skipped (no dbt project: {exc})")

    adapter = engine._adapter("maintain check")
    connector = adapter.name
    current_datasets = snapshot_mod.warehouse_from_metadata(adapter).datasets
    schema_findings = drift_mod.schema_drift(
        current_datasets, snap, current_transform=current_transform
    )
    volume_findings = drift_mod.volume_drift(current_datasets, snap)
    semantic_findings = (
        drift_mod.semantic_free_drift(
            current_transform, current_semantic, current_datasets, snap
        )
        if project_available
        else []
    )

    plan = drift_mod.grain_plan(adapter, snap)
    checks = drift_mod.cardinality_plan(current_semantic, snap)
    scans_needed = bool(
        plan.key_checks or plan.fanout_pairs or plan.composite_checks or checks
    )
    pending: ConfirmationRequest | None = None
    if scans_needed and command_args.cost_gate(adapter) is not None:
        grain_total, grain_per = drift_mod.grain_estimate(adapter, plan)
        card_total, card_per = drift_mod.cardinality_estimate(adapter, checks)
        per_table = dict(grain_per)
        for identifier, estimate in card_per.items():
            per_table[identifier] = per_table.get(identifier, 0.0) + estimate
        pending = command_args.confirmation_request(
            "maintain check",
            adapter,
            grain_total + card_total,
            per_table=per_table,
            notes=[
                "the schema, volume, and semantic reference checks are free "
                "and already complete (their findings are included in this "
                "envelope); the estimate covers the grain and "
                "dimension-cardinality scans"
            ],
        )

    if pending is not None:
        drift_mod.annotate_impacts(schema_findings + volume_findings, snap)
        by_axis = {
            "schema": drift_mod.rank_findings(schema_findings),
            "volume": drift_mod.rank_findings(volume_findings),
        }
        if project_available:
            by_axis["semantic"] = drift_mod.rank_findings(semantic_findings)
        _record_axes(store, snap, connector, {a: (f, []) for a, f in by_axis.items()})
        result = _drift_result(by_axis, snap, store, warnings=warnings)
        result.pending_confirmation = pending
        return result

    grain_findings = drift_mod.grain_drift(
        adapter, plan, timeout_seconds=config.query.timeout_seconds
    )
    semantic_findings = semantic_findings + drift_mod.cardinality_drift(
        adapter, checks, current_semantic
    )

    drift_mod.annotate_impacts(schema_findings + volume_findings + grain_findings, snap)
    by_axis = {
        "schema": drift_mod.rank_findings(schema_findings),
        "volume": drift_mod.rank_findings(volume_findings),
        "grain": drift_mod.rank_findings(grain_findings),
    }
    if project_available:
        by_axis["semantic"] = drift_mod.rank_findings(semantic_findings)
    _record_axes(store, snap, connector, {a: (f, []) for a, f in by_axis.items()})
    result = _drift_result(
        by_axis, snap, store, warnings=warnings + _staleness_warnings(store, snap)
    )
    return command_args.stamp_spend(result, adapter)


def cmd_check(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(check(engine))


def reconcile(engine: DexEngine, drift_class: str | None = None) -> ReconcileResult:
    """Propose the dbt edits that reconcile detected drift.

    Reads the last drift report, never re-scans, and writes nothing to the
    project: applying is ``transform apply <plan-id>``, so the human-edit
    conflict handshake is inherited unchanged.
    """

    from ..transform import plans as plans_mod
    from . import reconcile as reconcile_mod

    store = engine.store
    snap = store.load_snapshot()
    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)
    report = store.load_drift()
    if report is None:
        raise NoBaselineError(
            "no drift report yet; run `maintain check` (or a focused detector) "
            "first so reconcile has detected drift to propose fixes for"
        )

    warnings: list[str] = []
    if report.snapshot_created_at != snap.created_at:
        warnings.append(
            "the drift report was computed against an older snapshot; re-run "
            "`maintain check` before reconciling so the proposals match the "
            "current baseline"
        )

    findings = drift_mod.rank_findings(
        [
            finding
            for axis, result in report.axes.items()
            if drift_class is None or axis == drift_class
            for finding in result.findings
        ]
    )
    if not findings:
        return ReconcileResult(warnings=warnings)

    try:
        view = load_project(engine.project_dir())
    except (DbtProjectError, ValueError) as exc:
        raise DbtProjectError(f"reconcile edits a dbt project: {exc}") from exc

    repo_root = engine.require_repo_root("reconciling a dbt project")
    proposals, edits, build_warnings = reconcile_mod.build(
        findings,
        snap,
        store.load_cache(),
        view,
        pii_overrides=pii_override_paths(engine.config.pii_overrides),
    )
    warnings.extend(build_warnings)

    result = ReconcileResult(proposals=proposals, warnings=warnings)
    if edits:
        intent = (
            f"maintain reconcile {drift_class}" if drift_class else "maintain reconcile"
        )
        plan, diffs, plan_warnings = plans_mod.plan(
            intent, edits, project_dir=view.root, repo_root=repo_root, store=store
        )
        result.diffs = diffs
        result.warnings.extend(plan_warnings)
        result.plan_id = plan.plan_id
    return result


def cmd_reconcile(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    drift_class = getattr(args, "drift_class", None)
    try:
        result = reconcile(engine, drift_class)
    except (NoBaselineError, DbtProjectError) as exc:
        return env.error(str(exc))
    if not result.proposals:
        scope = f" for the '{drift_class}' axis" if drift_class else ""
        return to_envelope(result, hints={"hint": f"no drift{scope} to reconcile"})
    if result.plan_id is not None:
        hint = (
            f"review the diffs, then apply with `transform apply {result.plan_id}` "
            "(human edits since detection surface as a conflict, never a silent "
            "overwrite)"
        )
    else:
        hint = (
            "every proposal is advisory (a decision for you); nothing to apply, "
            "act on the actions above"
        )
    return to_envelope(result, hints={"hint": hint})


def _detect_free_axis(
    engine: DexEngine, axis: str, detector, objects: list[str] | None
) -> DriftResult:
    """One metadata-only detector: free on every connector, so no handshake.
    The cost stamp still reflects the connector's paradigm for the caller."""

    store = engine.store
    snap = store.load_snapshot()
    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)

    adapter = engine._adapter(f"maintain {axis}")
    current = snapshot_mod.warehouse_from_metadata(adapter).datasets
    cost = command_args.preflight_cost(adapter)
    connector = adapter.name

    scope_names = list(objects or [])
    scope = _resolve_scope(scope_names, current, snap)
    findings = detector(current, snap, scope)
    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)

    _record_axes(store, snap, connector, {axis: (ranked, scope_names)})
    result = _drift_result(
        {axis: ranked}, snap, store, warnings=_staleness_warnings(store, snap)
    )
    result.cost = cost
    return result


def _drift_envelope(result: DriftResult) -> env.Envelope:
    """The one CLI-shaped extra a drift result earns: what to run next, and only
    when there is something to run it for."""

    hints = (
        {
            "hint": "run `maintain reconcile [<class>]` for proposed fixes as "
            "reviewable diffs"
        }
        if result.findings
        else None
    )
    return to_envelope(result, hints=hints)


# --- shared plumbing -----------------------------------------------------------


def _resolve_scope(
    scope_names: list[str],
    current: list,
    snap: snapshot_mod.Snapshot,
) -> set[str] | None:
    if not scope_names:
        return None
    identifiers = {d.identifier for d in current} | {
        d.identifier for d in snap.warehouse.datasets
    }
    return drift_mod.resolve_scope(scope_names, identifiers)


def _record_axes(
    store: MaintainStore,
    snap: snapshot_mod.Snapshot,
    connector: str | None,
    results: dict[str, tuple[list[drift_mod.DriftFinding], list[str]]],
) -> None:
    """Merge this run's axes into the stored drift report. Axes merge across runs so
    a focused detector refreshes only itself, but never across baselines:
    findings measured against an older snapshot are dropped wholesale."""

    report = store.load_drift()
    if report is None or report.snapshot_created_at != snap.created_at:
        report = drift_mod.DriftReport()
    report.connector = connector
    report.snapshot_created_at = snap.created_at
    run_at = datetime.now(UTC).isoformat()
    for axis, (findings, scope_names) in results.items():
        report.axes[axis] = drift_mod.AxisResult(
            run_at=run_at, scope=scope_names or None, findings=findings
        )
    store.save_drift(report)


def _drift_result(
    by_axis: dict[str, list[drift_mod.DriftFinding]],
    snap: snapshot_mod.Snapshot,
    store: MaintainStore,
    *,
    warnings: list[str],
) -> DriftResult:
    return DriftResult(
        findings=drift_mod.rank_findings(
            [finding for findings in by_axis.values() for finding in findings]
        ),
        by_axis={axis: len(findings) for axis, findings in by_axis.items()},
        snapshot_created_at=snap.created_at,
        warehouse_from=snap.warehouse_from,
        drift_path=store.locator(Document.DRIFT),
        warnings=warnings,
    )


def _grain_baseline_warnings(snap: snapshot_mod.Snapshot) -> list[str]:
    if snap.warehouse_from != "metadata":
        return []
    return [
        "the baseline is metadata-only, so the grain and cardinality axes have "
        "nothing to diff against; run `explore map` and re-run `maintain "
        "snapshot` to give them a baseline"
    ]


def _adapter_notes(adapter, identifiers: list[str]) -> list[str]:
    """Surface the adapter's per-table notes (e.g. a skipped distinct-count
    escalation on a tight budget) so a silent skip never reads as a clean bill."""

    hook = getattr(adapter, "table_notes", None)
    if hook is None:
        return []
    notes: list[str] = []
    for identifier in identifiers:
        notes.extend(f"{identifier}: {note}" for note in hook(identifier) or [])
    return notes


def _semantic_scope(
    findings: list[drift_mod.DriftFinding], scope_names: list[str]
) -> list[drift_mod.DriftFinding]:
    """Scope semantic findings by definition name or by the physical object.

    Semantic findings hang off definitions rather than only warehouse objects,
    so scope names match either: a semantic model, metric, dimension, or
    measure name, or the referenced table/column.
    """

    if not scope_names:
        return findings
    names = {
        part.strip().lower()
        for raw in scope_names
        for part in raw.split(",")
        if part.strip()
    }

    def in_scope(finding: drift_mod.DriftFinding) -> bool:
        candidates = {
            finding.column,
            finding.identifier,
            finding.identifier.rsplit(".", 1)[-1] if finding.identifier else None,
        }
        candidates.update(
            value for value in finding.data.values() if isinstance(value, str)
        )
        return any(c is not None and c.lower() in names for c in candidates)

    return [finding for finding in findings if in_scope(finding)]


def _staleness_warnings(store: MaintainStore, snap: snapshot_mod.Snapshot) -> list[str]:
    warnings: list[str] = []
    cache = store.load_cache()
    if (
        cache is not None
        and cache.provenance.updated_at
        and cache.provenance.updated_at > snap.created_at
    ):
        warnings.append(
            "the exploration cache is newer than the drift baseline; if the current "
            "state is known-good, re-run `maintain snapshot` so drift is not "
            "measured against a stale baseline"
        )
    return warnings
