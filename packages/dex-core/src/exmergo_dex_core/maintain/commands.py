"""Maintain command orchestrators.

Each ``cmd_*`` loads the baseline and the project, drives the drift engine, and
shapes the result into the sanitized envelope. Only this layer opens adapters or
touches ``.dex/``; the detectors in ``drift.py`` stay pure comparisons so they
are testable without a warehouse. Detection commands write their findings to
``.dex/drift.json`` so the stateless ``reconcile`` has a report to read.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import command_args
from .. import envelope as env
from ..cache import DexStore
from ..config import DexConfig, load_config, pii_override_paths
from ..dbt_project import DbtProjectError
from ..dbt_project import load as load_project
from . import drift as drift_mod
from . import snapshot as snapshot_mod

_SNAPSHOT_HINT = (
    "commit .dex/snapshot.json like a lockfile, and re-run `maintain snapshot` "
    "after each known-good build so drift is measured against a state someone "
    "vouched for"
)

_NO_SNAPSHOT_ERROR = (
    "no .dex/snapshot.json baseline; run `maintain snapshot` first (ideally "
    "right after a known-good build)"
)


def cmd_snapshot(args: argparse.Namespace) -> env.Envelope:
    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    config = load_config(repo_root) or DexConfig()
    warnings: list[str] = []

    cache = store.load_cache()
    requested = getattr(args, "connector", None) or config.connector
    usable = cache is not None and bool(cache.datasets)
    if usable and cache.provenance.connector not in (None, requested):
        warnings.append(
            f"the .dex cache was mapped on '{cache.provenance.connector}' but "
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
        adapter = command_args.open_from_args(args)
        try:
            warehouse = snapshot_mod.warehouse_from_metadata(adapter)
            connector = adapter.name
        finally:
            adapter.close()
        warehouse_from = "metadata"
        cache_updated_at = None
        if cache is None or not cache.datasets:
            warnings.append(
                "no .dex/cache.json to pin, so this baseline is metadata-only "
                "(schema and volume axes); run `explore map` and re-snapshot "
                "to give the grain and cardinality axes a baseline"
            )

    transform_layer = semantic_layer = None
    try:
        view = load_project(command_args.project_dir(args))
        transform_layer = snapshot_mod.transform_layer(view)
        semantic_layer = snapshot_mod.semantic_layer(view)
    except DbtProjectError as exc:
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
    path = store.save_snapshot(snap)

    return env.ok(
        {
            "snapshot_path": str(path),
            "baseline": {
                "from": warehouse_from,
                "dataset_count": len(warehouse.datasets),
                "relationship_count": len(warehouse.relationships),
                "grain_baseline_count": sum(
                    1 for d in warehouse.datasets if d.candidate_keys
                ),
                "cache_updated_at": cache_updated_at,
            },
            "transform_layer": (
                {
                    "file_count": len(transform_layer.files),
                    "model_count": len(transform_layer.models),
                    "source_count": len(transform_layer.sources),
                }
                if transform_layer is not None
                else None
            ),
            "semantic_layer": (
                {
                    "semantic_model_count": len(semantic_layer.semantic_models),
                    "metric_count": len(semantic_layer.metrics),
                }
                if semantic_layer is not None
                else None
            ),
            "hint": _SNAPSHOT_HINT,
        },
        warnings=warnings,
    )


def cmd_schema(args: argparse.Namespace) -> env.Envelope:
    return _detect_free_axis(args, "schema", drift_mod.schema_drift)


def cmd_volume(args: argparse.Namespace) -> env.Envelope:
    return _detect_free_axis(args, "volume", drift_mod.volume_drift)


def cmd_grain(args: argparse.Namespace) -> env.Envelope:
    """Grain drift scans (exact distinct counts, overlap probes), so on a billed
    connector it goes through the confirm handshake with a dry-run estimate of
    exactly the statements it would run. Free connectors run immediately."""

    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    snap = store.load_snapshot()
    if snap is None:
        return env.error(_NO_SNAPSHOT_ERROR)
    config = load_config(repo_root) or DexConfig()
    scope_names = list(getattr(args, "objects", []) or [])

    adapter = command_args.open_from_args(args)
    try:
        connector = adapter.name
        scope = None
        if scope_names:
            identifiers = {m.identifier for m in adapter.list_objects()} | {
                d.identifier for d in snap.warehouse.datasets
            }
            scope = drift_mod.resolve_scope(scope_names, identifiers)
        plan = drift_mod.grain_plan(adapter, snap, scope)
        if plan.key_checks or plan.fanout_pairs or plan.composite_checks:
            estimate, per_table = drift_mod.grain_estimate(adapter, plan)
            unconfirmed = command_args.billed_handshake(
                "maintain grain", adapter, estimate, per_table=per_table
            )
            if unconfirmed is not None:
                return unconfirmed
        findings = drift_mod.grain_drift(
            adapter, plan, timeout_seconds=config.query.timeout_seconds
        )
        noted = {dataset.identifier for dataset, _keys, _rows in plan.key_checks} | {
            dataset.identifier for dataset, _combos, _rows in plan.composite_checks
        }
        notes = _adapter_notes(adapter, sorted(noted))
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()

    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)
    _record_axes(store, snap, connector, {"grain": (ranked, scope_names)})
    envelope.data.update(_findings_data({"grain": ranked}, snap, store))
    envelope.warnings = (
        _grain_baseline_warnings(snap) + _staleness_warnings(store, snap) + notes
    )
    return envelope


def cmd_semantic(args: argparse.Namespace) -> env.Envelope:
    """The semantic axis is two-phase on billed connectors: definition and
    reference checks are free and run immediately; the dimension-cardinality
    scan waits behind the handshake, and an unconfirmed call still returns the
    complete free findings alongside the estimate."""

    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    snap = store.load_snapshot()
    if snap is None:
        return env.error(_NO_SNAPSHOT_ERROR)
    try:
        view = load_project(command_args.project_dir(args))
    except DbtProjectError as exc:
        return env.error(f"the semantic axis needs a dbt project: {exc}")

    warnings: list[str] = []
    if snap.semantic_layer is None:
        warnings.append(
            "the baseline has no semantic fingerprint, so every definition "
            "reads as new; re-run `maintain snapshot` to fix the baseline"
        )
    current_transform = snapshot_mod.transform_layer(view)
    current_semantic = snapshot_mod.semantic_layer(view)
    scope_names = list(getattr(args, "objects", []) or [])

    adapter = command_args.open_from_args(args)
    try:
        connector = adapter.name
        current_datasets = snapshot_mod.warehouse_from_metadata(adapter).datasets
        free_findings = _semantic_scope(
            drift_mod.semantic_free_drift(
                current_transform, current_semantic, current_datasets, snap
            ),
            scope_names,
        )
        checks = drift_mod.cardinality_plan(current_semantic, snap)
        if checks:
            estimate, per_table = drift_mod.cardinality_estimate(adapter, checks)
            unconfirmed = command_args.billed_handshake(
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
            if unconfirmed is not None:
                ranked = drift_mod.rank_findings(free_findings)
                _record_axes(
                    store, snap, connector, {"semantic": (ranked, scope_names)}
                )
                unconfirmed.data["findings"] = [
                    f.model_dump(mode="json") for f in ranked
                ]
                unconfirmed.data["free_finding_count"] = len(ranked)
                unconfirmed.warnings = warnings
                return unconfirmed
        billed_findings = _semantic_scope(
            drift_mod.cardinality_drift(adapter, checks, current_semantic),
            scope_names,
        )
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()

    ranked = drift_mod.rank_findings(free_findings + billed_findings)
    _record_axes(store, snap, connector, {"semantic": (ranked, scope_names)})
    envelope.data.update(_findings_data({"semantic": ranked}, snap, store))
    envelope.warnings = warnings + _staleness_warnings(store, snap)
    return envelope


def cmd_check(args: argparse.Namespace) -> env.Envelope:
    """The everyday sweep, two-phase by construction: the free axes (schema,
    volume, semantic references) always run and their findings always return;
    the scanning axes (grain, cardinality) run immediately on free connectors
    and behind one combined estimate on billed ones."""

    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    snap = store.load_snapshot()
    if snap is None:
        return env.error(_NO_SNAPSHOT_ERROR)
    config = load_config(repo_root) or DexConfig()

    warnings = _grain_baseline_warnings(snap)
    current_transform = current_semantic = None
    project_available = True
    try:
        view = load_project(command_args.project_dir(args))
        current_transform = snapshot_mod.transform_layer(view)
        current_semantic = snapshot_mod.semantic_layer(view)
    except DbtProjectError as exc:
        project_available = False
        warnings.append(f"semantic axis skipped (no dbt project: {exc})")

    adapter = command_args.open_from_args(args)
    try:
        connector = adapter.name
        current_datasets = snapshot_mod.warehouse_from_metadata(adapter).datasets
        schema_findings = drift_mod.schema_drift(current_datasets, snap)
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
        if scans_needed and command_args.cost_gate(adapter) is not None:
            grain_total, grain_per = drift_mod.grain_estimate(adapter, plan)
            card_total, card_per = drift_mod.cardinality_estimate(adapter, checks)
            per_table = dict(grain_per)
            for identifier, estimate in card_per.items():
                per_table[identifier] = per_table.get(identifier, 0.0) + estimate
            unconfirmed = command_args.billed_handshake(
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
            if unconfirmed is not None:
                drift_mod.annotate_impacts(schema_findings + volume_findings, snap)
                free_by_axis = {
                    "schema": drift_mod.rank_findings(schema_findings),
                    "volume": drift_mod.rank_findings(volume_findings),
                }
                if project_available:
                    free_by_axis["semantic"] = drift_mod.rank_findings(
                        semantic_findings
                    )
                _record_axes(
                    store,
                    snap,
                    connector,
                    {axis: (f, []) for axis, f in free_by_axis.items()},
                )
                unconfirmed.data.update(_findings_data(free_by_axis, snap, store))
                unconfirmed.warnings = warnings
                return unconfirmed
        grain_findings = drift_mod.grain_drift(
            adapter, plan, timeout_seconds=config.query.timeout_seconds
        )
        semantic_findings = semantic_findings + drift_mod.cardinality_drift(
            adapter, checks, current_semantic
        )
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()

    drift_mod.annotate_impacts(schema_findings + volume_findings + grain_findings, snap)
    by_axis = {
        "schema": drift_mod.rank_findings(schema_findings),
        "volume": drift_mod.rank_findings(volume_findings),
        "grain": drift_mod.rank_findings(grain_findings),
    }
    if project_available:
        by_axis["semantic"] = drift_mod.rank_findings(semantic_findings)
    _record_axes(store, snap, connector, {axis: (f, []) for axis, f in by_axis.items()})
    envelope.data.update(_findings_data(by_axis, snap, store))
    envelope.warnings = warnings + _staleness_warnings(store, snap)
    return envelope


def cmd_reconcile(args: argparse.Namespace) -> env.Envelope:
    """Propose the dbt edits that reconcile detected drift, as a stored plan of
    reviewable diffs. Reads the last `.dex/drift.json`, never re-scans, and
    writes nothing to the project: applying is `transform apply <plan-id>`, so
    the human-edit conflict handshake is inherited unchanged."""

    from ..transform import plans as plans_mod
    from . import reconcile as reconcile_mod

    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    snap = store.load_snapshot()
    if snap is None:
        return env.error(_NO_SNAPSHOT_ERROR)
    report = store.load_drift()
    if report is None:
        return env.error(
            "no .dex/drift.json; run `maintain check` (or a focused detector) "
            "first so reconcile has detected drift to propose fixes for"
        )

    warnings: list[str] = []
    if report.snapshot_created_at != snap.created_at:
        warnings.append(
            "the drift report was computed against an older snapshot; re-run "
            "`maintain check` before reconciling so the proposals match the "
            "current baseline"
        )

    drift_class = getattr(args, "drift_class", None)
    findings = [
        finding
        for axis, result in report.axes.items()
        if drift_class is None or axis == drift_class
        for finding in result.findings
    ]
    findings = drift_mod.rank_findings(findings)
    if not findings:
        scope = f" for the '{drift_class}' axis" if drift_class else ""
        return env.ok(
            {
                "proposals": [],
                "proposal_count": 0,
                "hint": f"no drift{scope} to reconcile",
            },
            warnings=warnings,
        )

    try:
        view = load_project(command_args.project_dir(args))
    except DbtProjectError as exc:
        return env.error(f"reconcile edits a dbt project: {exc}")

    config = load_config(repo_root) or DexConfig()
    proposals, edits, build_warnings = reconcile_mod.build(
        findings,
        snap,
        store.load_cache(),
        view,
        pii_overrides=pii_override_paths(config.pii_overrides),
    )
    warnings.extend(build_warnings)

    data: dict = {
        "proposals": [p.model_dump(mode="json") for p in proposals],
        "proposal_count": len(proposals),
        "mechanical_count": sum(1 for p in proposals if p.kind == "mechanical"),
        "advisory_count": sum(1 for p in proposals if p.kind == "advisory"),
    }
    envelope = env.ok(data, warnings=warnings)
    if edits:
        intent = (
            f"maintain reconcile {drift_class}" if drift_class else "maintain reconcile"
        )
        plan, diffs, plan_warnings = plans_mod.plan(
            intent, edits, project_dir=view.root, repo_root=repo_root
        )
        envelope.diffs = diffs
        envelope.warnings.extend(plan_warnings)
        envelope.data["plan_id"] = plan.plan_id
        envelope.data["hint"] = (
            f"review the diffs, then apply with `transform apply {plan.plan_id}` "
            "(human edits since detection surface as a conflict, never a silent "
            "overwrite)"
        )
    else:
        envelope.data["hint"] = (
            "every proposal is advisory (a decision for you); nothing to apply, "
            "act on the actions above"
        )
    return envelope


def _detect_free_axis(args: argparse.Namespace, axis: str, detector) -> env.Envelope:
    """One metadata-only detector: free on every connector, so no handshake.
    The cost stamp still reflects the connector's paradigm for the agent."""

    store = DexStore(command_args.repo_root(args))
    snap = store.load_snapshot()
    if snap is None:
        return env.error(_NO_SNAPSHOT_ERROR)

    adapter = command_args.open_from_args(args)
    try:
        current = snapshot_mod.warehouse_from_metadata(adapter).datasets
        gate = getattr(adapter, "cost_gate", None)
        cost = gate.cost() if gate is not None else env.Cost()
        connector = adapter.name
    finally:
        adapter.close()

    scope_names = list(getattr(args, "objects", []) or [])
    scope = _resolve_scope(scope_names, current, snap)
    findings = detector(current, snap, scope)
    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)

    _record_axes(store, snap, connector, {axis: (ranked, scope_names)})
    return env.ok(
        _findings_data({axis: ranked}, snap, store),
        cost=cost,
        warnings=_staleness_warnings(store, snap),
    )


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
    store: DexStore,
    snap: snapshot_mod.Snapshot,
    connector: str | None,
    results: dict[str, tuple[list[drift_mod.DriftFinding], list[str]]],
) -> None:
    """Merge this run's axes into `.dex/drift.json`. Axes merge across runs so
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


def _findings_data(
    by_axis: dict[str, list[drift_mod.DriftFinding]],
    snap: snapshot_mod.Snapshot,
    store: DexStore,
) -> dict:
    findings = drift_mod.rank_findings(
        [finding for axis_findings in by_axis.values() for finding in axis_findings]
    )
    data = {
        "findings": [f.model_dump(mode="json") for f in findings],
        "finding_count": len(findings),
        "axes_run": sorted(by_axis),
        "axes": {axis: len(axis_findings) for axis, axis_findings in by_axis.items()},
        "baseline": {
            "snapshot_created_at": snap.created_at,
            "from": snap.warehouse_from,
        },
        "drift_path": str(store.dex_dir / "drift.json"),
    }
    if findings:
        data["hint"] = (
            "run `maintain reconcile [<class>]` for proposed fixes as reviewable diffs"
        )
    return data


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


def _staleness_warnings(store: DexStore, snap: snapshot_mod.Snapshot) -> list[str]:
    warnings: list[str] = []
    cache = store.load_cache()
    if (
        cache is not None
        and cache.provenance.updated_at
        and cache.provenance.updated_at > snap.created_at
    ):
        warnings.append(
            "the .dex cache is newer than the snapshot baseline; if the current "
            "state is known-good, re-run `maintain snapshot` so drift is not "
            "measured against a stale baseline"
        )
    return warnings
