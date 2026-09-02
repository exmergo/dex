"""Maintain orchestration, in two layers.

The lower layer is the run functions: they take an :class:`~..engine.DexEngine`,
load the baseline and the project, drive the drift engine, and return a record
from :mod:`.results`. The upper layer is the ``cmd_*`` shims: argparse in,
envelope out.

Only this layer reaches the store; the detectors in ``drift.py`` stay pure
comparisons so they are testable without a warehouse. Detection commands save
their findings as a drift report so the stateless ``reconcile`` has one to read.

**The project is read through its tier, never through a format's module.** The
four detection commands ask ``MaintainProject`` for the two snapshot layers, which
is what lets a format that is not a dbt project be a drift baseline. They share
``_read_layers`` for that, so there is one definition of what counts as reading a
project and one place that decides which failures degrade and which refuse.

``reconcile`` is the exception, and deliberately so. It does not want a layer, it
wants the project's file surface: the two mechanical write paths look up
``models/staging/stg_<table>.*`` in it, and the plan store records the directory
the edits were pinned against. Neither is expressible in tier 2, and widening tier
2 to carry them would put a bag of file paths and file contents on the contract,
which a format with no files could not produce. A tier no non-dbt format can reach
is a tier that format does not have, so ``reconcile`` asks the *write* tier
instead and degrades to proposal-only when a format declines it.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .. import command_args
from .. import envelope as env
from ..adapters.base import name_list
from ..config import pii_override_paths
from ..errors import DexError, PrerequisiteError, ProjectError, RepoRootRequiredError
from ..results import ConfirmationRequest, to_envelope
from ..storage import Document, FilesystemStore, MaintainStore, readable_cache
from . import drift as drift_mod
from . import snapshot as snapshot_mod
from . import verify as verify_mod
from .results import (
    DriftResult,
    LayerFingerprint,
    ReconcileResult,
    SnapshotResult,
    VerifyResult,
)

if TYPE_CHECKING:
    from ..engine import DexEngine
    from .snapshot import SemanticLayer, TransformLayer

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


def _read_layers(
    engine: DexEngine, *, semantic: bool = True
) -> tuple[TransformLayer | None, SemanticLayer | None, str | None]:
    """The current project's snapshot layers, or why there are none.

    Returns ``(transform, semantic, reason)``. ``reason`` is ``None`` on success
    and otherwise a clause each command folds into its own warning, so the four
    detection commands share one definition of "read the project" while keeping
    the sentence that says what *this* command loses without it.

    ``semantic=False`` skips the second layer rather than reading and discarding
    it. `maintain schema` needs only the transform half, and on the dbt format
    each accessor runs its own YAML pass over the model tree.

    Three states degrade to a reason and are not errors: no project, an
    unreadable one, and a format narrower than tier 2. Two do not, and are
    deliberately left to propagate. A format that could not be *built* is a
    wiring mistake in a committed file, so every command will hit it and hiding
    it behind a warning would bury the only thing worth fixing. And a repo root
    that was never supplied is caught here rather than left to propagate only
    because a host exploring with no repository at all is an ordinary state, the
    same reason ``definitions()`` may not raise.
    """

    try:
        project = engine.maintain_project()
        if project is None:
            return None, None, engine.project_tier_note()
        transform = project.transform_layer()
        current = project.semantic_layer() if semantic else None
    except (ProjectError, RepoRootRequiredError, ValidationError) as exc:
        return None, None, str(exc)
    return transform, current, None


class NoBaselineError(PrerequisiteError):
    """A detector was asked to measure drift with nothing to measure against.

    Named for the state rather than for a missing file: on the filesystem
    backend the baseline is `.dex/snapshot.json`, elsewhere it is a row, and the
    fix is the same either way.
    """


class BaselineUnreadableError(PrerequisiteError):
    """A baseline exists and this engine cannot read it.

    Distinct from :class:`NoBaselineError` on purpose. Both remediate the same
    way, so the status is the same, but "you never took a baseline" and "the
    baseline you have is unreadable" are different facts about the deployment,
    and only one of them suggests something went wrong. A host that wants to
    page on the second and not the first can now tell them apart without
    matching on prose.

    Two states reach this one class, and :attr:`schema_version` is how a caller
    separates *them* without matching on prose either. It carries the stored
    version when the document parsed and this engine does not read that version,
    and is ``None`` when the document did not parse at all. Worth separating,
    because they are different operational events: a version this engine does
    not know usually means the deployment rolled forward or back, while a
    document that will not parse is an integrity problem. One class rather than
    two because the remediation is identical, and a host that does not care
    should not have to catch two names to stay correct.
    """

    def __init__(self, message: str, *, schema_version: int | None = None) -> None:
        super().__init__(message)
        #: The stored schema version when the baseline parsed and this engine
        #: does not read that version; ``None`` when it failed to parse at all.
        self.schema_version = schema_version


#: Appended wherever the remedy is a fresh baseline. `maintain snapshot` is free
#: on every connector and cannot really fail, which makes "just re-snapshot"
#: sound costless. The cost is not in dollars: a replacement pins *current* state
#: as known-good, so whatever drifted between the last readable baseline and now
#: is absorbed and never reported again, on any axis. An operator deciding
#: whether to investigate before replacing needs that said before they run it.
_REPLACEMENT_ABSORBS_DRIFT = (
    "note that a replacement pins current state as known-good, so anything that "
    "drifted since the last readable baseline will not be reported"
)


def _require_baseline(store: MaintainStore) -> snapshot_mod.Snapshot:
    """The stored baseline, or a refusal that names the command producing one.

    Every drift axis went through the same three lines, and two of the three
    states they could reach were wrong.

    **A corrupt baseline reported as a bad request.** ``load_snapshot`` raises,
    and pydantic's ``ValidationError`` subclasses ``ValueError``, so it fell to
    the CLI catch-all and was classified as a *request* error. That tells an
    operator they typed something wrong when the fix is `maintain snapshot`, and
    it is exactly the retry-versus-stop distinction ``PrerequisiteError`` exists
    to carry.

    **An incompatible baseline was not detected at all.** ``schema_version`` was
    stamped on every write and read by nothing, so a document from a future or
    unknown schema was handed to the detectors as though it were current. The
    failure that produces is the bad kind: not an error, but a drift report
    measured against a shape the engine misunderstood.

    **What the version check does not reach**, stated here because the guarantee
    is narrower than "an incompatible baseline is refused" sounds. The check runs
    on a *parsed* :class:`~.snapshot.Snapshot`, because the store hands back a
    model and the raw document is not the engine's to see (``storage/base.py``).
    So a future version whose shape this model still validates is named exactly,
    and a future version whose shape it rejects arrives as a parse failure
    instead: same refusal, same remediation, but the message cannot say which
    version it came from. Both messages therefore name the possibility. Reading
    the version off the document before validating it would take a contract
    change on ``Store``, which is a wider change than this one.
    """

    try:
        snap = store.load_snapshot()
    except ValueError as exc:
        # ValueError rather than pydantic's ValidationError, which it subclasses.
        # `references/storage.md` requires a backend to raise on a document it
        # cannot parse, and has never named the exception; the store contract is
        # public, and a backend deserializing its own rows raises whatever `json`
        # or its driver raises. Catching only pydantic's error would leave every
        # third-party backend reproducing the exact defect this function fixes,
        # which is the kind of gap that looks fixed from inside this repo.
        detail = (
            f"{exc.error_count()} validation error(s)"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise BaselineUnreadableError(
            f"the stored drift baseline could not be read ({detail}). It is "
            "either corrupt or written by a newer dex whose shape this engine "
            "does not know. Re-run `maintain snapshot` to replace it, or move to "
            "the dex that wrote it if that is what happened; "
            f"{_REPLACEMENT_ABSORBS_DRIFT}"
        ) from exc

    if snap is None:
        raise NoBaselineError(_NO_SNAPSHOT_ERROR)

    if snap.schema_version not in snapshot_mod.SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        supported = ", ".join(
            str(v) for v in sorted(snapshot_mod.SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS)
        )
        # Older and newer are opposite situations with opposite first moves, and
        # one sentence covering both sends half of them the wrong way. dex tells
        # operators to commit `.dex/snapshot.json` like a lockfile, so a baseline
        # newer than the engine reading it is usually a colleague on a newer dex
        # rather than a broken file, and "re-run `maintain snapshot`" resolves it
        # by overwriting *their* baseline with one they can no longer read.
        remedy = (
            "upgrade dex to a version that reads it, or re-run `maintain "
            "snapshot` to replace it at this engine's version, which makes it "
            "unreadable to the newer dex that wrote it: prefer upgrading when "
            "the baseline is committed and shared"
            if snap.schema_version
            > max(snapshot_mod.SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS)
            else "re-run `maintain snapshot` to take a fresh one"
        )
        raise BaselineUnreadableError(
            f"the stored drift baseline is schema version {snap.schema_version}, "
            f"and this engine reads {supported}: {remedy}; "
            f"{_REPLACEMENT_ABSORBS_DRIFT}",
            schema_version=snap.schema_version,
        )

    return snap


def _stored_drift(store: MaintainStore) -> drift_mod.DriftReport | None:
    """The stored drift report, treating one that will not parse as absent.

    The opposite policy from :func:`_require_baseline`, and the reason is the one
    already written down beside ``DRIFT_SCHEMA_VERSION`` in :mod:`.drift`: the
    baseline is *vouched for* and nothing else reproduces it, while a drift
    report is *derived* and `maintain check` regenerates it from the baseline on
    demand. So a document this engine cannot read has a cheap correct answer here
    that the baseline does not have, and that note pre-committed to this one:
    "treat it as absent and rebuild, rather than refuse".

    Both callers already have an absent path, so this widens a handled state
    rather than adding one. ``_record_axes`` discards a report measured against a
    different baseline wholesale, which is the same "throw it away and remeasure"
    it now does for an unparseable one; ``reconcile`` raises
    :class:`NoBaselineError` naming `maintain check`, which is the correct remedy
    for a report that cannot be read as much as for one that was never written.

    ⚠️ Deliberately silent about *which* of the two happened, unlike the cache and
    the baseline. Rebuilding is free and automatic here, so an operator has
    nothing to decide and a warning would be noise on a path that self-heals.
    """

    try:
        return store.load_drift()
    except ValueError:
        # Same convention as `_require_baseline` and `readable_cache`: a backend
        # signals "this document will not parse" with a ValueError, whatever its
        # own deserializer raises. Swallowed rather than classified because the
        # remedy runs itself on the very next line of both callers.
        return None


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

    cache = readable_cache(store)
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

    transform_layer, semantic_layer, no_project = _read_layers(engine)
    if no_project is not None:
        warnings.append(
            f"no project fingerprinted ({no_project}); the semantic axis and "
            "reconcile need one"
        )

    now = datetime.now(UTC)
    snap = snapshot_mod.Snapshot(
        created_at=now.isoformat(),
        connector=connector,
        warehouse=warehouse,
        warehouse_from=warehouse_from,
        cache_updated_at=cache_updated_at,
        transform_layer=transform_layer,
        semantic_layer=semantic_layer,
    )
    # Said at the moment of pinning, not only at detection: this is the command a
    # host wires to "accept current state", and a thin or aging cache makes that
    # accept partial in a way nothing else surfaces. Both checks read the
    # baseline just built, so they describe what was actually written.
    warnings.extend(_column_detail_warnings(snap))
    warnings.extend(
        _cache_age_warnings(snap, config.profile_freshness_hours, now),
    )
    warnings.extend(_layer_notes(transform_layer, semantic_layer))
    locator = store.save_snapshot(snap)

    return SnapshotResult(
        snapshot=snap,
        snapshot_path=locator,
        warehouse_from=warehouse_from,
        dataset_count=len(warehouse.datasets),
        relationship_count=len(warehouse.relationships),
        grain_baseline_count=sum(1 for d in warehouse.datasets if d.candidate_keys),
        column_detail_count=len(warehouse.datasets)
        - len(warehouse.without_column_detail()),
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
    snap = _require_baseline(store)

    warnings: list[str] = []
    current_transform, _, no_project = _read_layers(engine, semantic=False)
    if no_project is not None:
        warnings.append(
            f"orphan-relation classification skipped (no project: {no_project})"
        )

    adapter = engine._adapter("maintain schema")
    current = snapshot_mod.warehouse_from_metadata(adapter).datasets
    cost = command_args.preflight_cost(adapter)
    connector = adapter.name

    scope_names = list(objects or [])
    scope = _resolve_scope(scope_names, current, snap)
    findings = drift_mod.schema_drift(current, snap, scope, current_transform)
    # Not scoped by `objects`: a model's identifier is its project name, not a
    # warehouse identifier, and `scope` above is resolved against the latter.
    findings.extend(drift_mod.transform_drift(current_transform, snap))
    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)

    by_axis = _record_axes(store, snap, connector, {"schema": (ranked, scope_names)})
    result = _drift_result(
        by_axis,
        snap,
        store,
        warnings=warnings
        + _column_detail_warnings(snap)
        + _baseline_warnings(store, snap, engine.config.profile_freshness_hours)
        + _layer_notes(snap.transform_layer, current_transform),
    )
    result.cost = cost
    return result


def volume_drift(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """A row count that collapsed, a table that emptied, a load that half-failed."""

    return _detect_free_axis(
        engine,
        "volume",
        drift_mod.volume_drift,
        objects,
        noter=drift_mod.uncomparable_volume,
    )


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
    snap = _require_baseline(store)
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
    plan = drift_mod.grain_plan(
        adapter, snap, scope, engine.project_format().definitions()
    )
    if (
        plan.key_checks
        or plan.fanout_pairs
        or plan.composite_checks
        or plan.declared_composite_checks
    ):
        estimate, per_table = drift_mod.grain_estimate(adapter, plan)
        command_args.billed_handshake(
            "maintain grain", adapter, estimate, per_table=per_table
        )
    findings = drift_mod.grain_drift(
        adapter,
        plan,
        timeout_seconds=engine.config.query.timeout_seconds,
        min_rows=engine.config.maintain.grain_min_rows,
    )
    noted = {dataset.identifier for dataset, _keys, _rows in plan.key_checks} | {
        dataset.identifier
        for dataset, _combos, _rows in plan.composite_checks
        + plan.declared_composite_checks
    }
    notes_by_identifier = _adapter_notes_by_identifier(adapter, sorted(noted))
    _qualify_uniqueness_findings(findings, notes_by_identifier)
    notes = _flatten_adapter_notes(notes_by_identifier)

    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)
    by_axis = _record_axes(store, snap, connector, {"grain": (ranked, scope_names)})
    result = _drift_result(
        by_axis,
        snap,
        store,
        warnings=_grain_baseline_warnings(snap)
        + _column_detail_warnings(snap)
        + _baseline_warnings(store, snap, engine.config.profile_freshness_hours)
        + plan.notes
        + notes,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_grain(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(grain_drift(engine, getattr(args, "objects", None)))


def verify(engine: DexEngine, objects: list[str] | None = None) -> VerifyResult:
    """Is the project correct right now, with no baseline required (#224).

    The first finding class (#225): build-status gaps read from the compiled
    manifest and the last run's ``run_results.json`` (failed nodes, nodes
    skipped by a failed parent), plus models the project declares that have
    no relation in the warehouse. Entirely free: the manifest/run-results
    read touches no connection, and the relation check reads only cheap
    object metadata, never a scan.

    A project that does not compile is reported first and suppresses every
    other check here, since a finding computed from a manifest a broken
    project could not have produced honestly is not a finding at all
    (#172's inertness requirement, #225's third acceptance bullet).
    """

    from pathlib import Path

    suppressed: dict[str, str] = {}
    try:
        project_dir = Path(engine.project_dir())
    except (ProjectError, RepoRootRequiredError) as exc:
        return VerifyResult(
            suppressed={
                "build_status": str(exc),
                "no_relation": str(exc),
                "compile": str(exc),
            },
            warnings=[f"maintain verify needs a dbt project: {exc}"],
        )

    findings: list = []
    compile_finding, compile_notes = verify_mod.compile_check(project_dir)
    if compile_finding is not None:
        findings.append(compile_finding)
        reason = "the project does not compile"
        result = VerifyResult(
            findings=drift_mod.rank_findings(findings),
            suppressed={"build_status": reason, "no_relation": reason},
            warnings=[
                "build-status and no-relation findings suppressed: the project "
                "does not compile, so its manifest cannot be trusted"
            ],
        )
        return result

    warnings = list(compile_notes)
    build_findings, build_notes = verify_mod.build_status_findings(project_dir)
    findings.extend(build_findings)
    warnings.extend(build_notes)
    if build_notes:
        suppressed["build_status"] = build_notes[0]

    definitions = engine.project_format().definitions()
    cost = None
    if not definitions.present:
        suppressed["no_relation"] = "no dbt project found"
    else:
        model_relations = {
            name: relation
            for name, relation in definitions.model_relations.items()
            if "." not in name
        }
        try:
            adapter = engine._adapter("maintain verify")
        except DexError as exc:
            suppressed["no_relation"] = f"warehouse unreachable: {exc}"
        else:
            cost = command_args.preflight_cost(adapter)
            live = [o.identifier for o in adapter.list_objects()]
            already = {f.identifier for f in findings if f.identifier}
            findings.extend(
                verify_mod.missing_relation_findings(model_relations, live, already)
            )

    if objects:
        wanted = {
            name.strip().lower()
            for raw in objects
            for name in raw.split(",")
            if name.strip()
        }
        findings = [f for f in findings if (f.identifier or "").lower() in wanted]

    result = VerifyResult(
        findings=drift_mod.rank_findings(findings),
        suppressed=suppressed,
        warnings=warnings,
    )
    if cost is not None:
        result.cost = cost
    return result


def cmd_verify(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(verify(engine, getattr(args, "objects", None)))


def semantic_drift(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """Definitions that no longer match: dangling references, new categoricals.

    Two-phase on billed connectors: definition and reference checks are free and
    run immediately; the dimension-cardinality scan waits behind the handshake,
    and an unconfirmed call still returns the complete free findings alongside
    the estimate rather than throwing away work that cost nothing but is real.
    """

    store = engine.store
    snap = _require_baseline(store)
    current_transform, current_semantic, no_project = _read_layers(engine)
    if no_project is not None:
        raise ProjectError(f"the semantic axis needs a project: {no_project}")

    warnings: list[str] = []
    if snap.semantic_layer is None:
        warnings.append(
            "the baseline has no semantic fingerprint, so every definition "
            "reads as new; re-run `maintain snapshot` to fix the baseline"
        )
    # Extended here rather than at the two returns so the confirmation path
    # carries them too: the free findings it returns are bounded by whatever the
    # format could not supply, exactly as the settled ones are.
    warnings.extend(_layer_notes(snap.semantic_layer, current_semantic))
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
    checks = drift_mod.cardinality_plan(
        current_semantic, snap, _semantic_names(scope_names) if scope_names else None
    )
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
    by_axis = _record_axes(store, snap, connector, {"semantic": (ranked, scope_names)})
    if pending is not None:
        # The free half is complete and real, so it returns alongside the ask
        # for the scanning half rather than being discarded and re-derived.
        result = _drift_result(by_axis, snap, store, warnings=warnings)
        result.pending_confirmation = pending
        return result
    result = _drift_result(
        by_axis,
        snap,
        store,
        warnings=warnings
        + _baseline_warnings(store, snap, engine.config.profile_freshness_hours),
    )
    return command_args.stamp_spend(result, adapter)


def cmd_semantic(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(semantic_drift(engine, getattr(args, "objects", None)))


def check(engine: DexEngine, objects: list[str] | None = None) -> DriftResult:
    """The everyday sweep across every axis.

    Two-phase by construction: the free axes (schema, volume, semantic
    references) always run and their findings always return; the scanning axes
    (grain, cardinality) run immediately on free connectors and behind one
    combined estimate on billed ones.

    ``objects`` narrows every axis exactly like the focused detectors do:
    schema/volume/grain resolve it against known identifiers (raising if a
    name matches nothing), while the semantic axis matches names against
    definitions too (a metric, dimension, or measure name, not only a table).
    Unscoped, the paid grain/cardinality estimate covers everything the
    baseline knows about; a narrowed run prices and bills only the scoped
    subset, not just the reported findings.
    """

    store = engine.store
    snap = _require_baseline(store)
    config = engine.config
    scope_names = list(objects or [])

    warnings = _grain_baseline_warnings(snap) + _column_detail_warnings(snap)
    current_transform, current_semantic, no_project = _read_layers(engine)
    project_available = no_project is None
    if no_project is not None:
        warnings.append(f"semantic axis skipped (no project: {no_project})")
    # All four layers: `check` sweeps every axis, so both the baseline's limits
    # and the current read's bound what it reports. Added before the two returns
    # so the confirmation path carries them as well.
    warnings.extend(
        _layer_notes(
            snap.transform_layer,
            snap.semantic_layer,
            current_transform,
            current_semantic,
        )
    )

    adapter = engine._adapter("maintain check")
    connector = adapter.name
    current_datasets = snapshot_mod.warehouse_from_metadata(adapter).datasets
    scope = _resolve_scope(scope_names, current_datasets, snap) if scope_names else None
    names = _semantic_names(scope_names) if scope_names else None

    schema_findings = drift_mod.schema_drift(
        current_datasets, snap, scope, current_transform
    )
    # Not scoped by `objects`, same reason as in schema_drift(): a model's
    # identifier is its project name, not a warehouse identifier.
    schema_findings.extend(drift_mod.transform_drift(current_transform, snap))
    volume_findings = drift_mod.volume_drift(current_datasets, snap, scope)
    warnings.extend(drift_mod.uncomparable_volume(current_datasets, snap, scope))
    semantic_findings = (
        _semantic_scope(
            drift_mod.semantic_free_drift(
                current_transform, current_semantic, current_datasets, snap
            ),
            scope_names,
        )
        if project_available
        else []
    )

    plan = drift_mod.grain_plan(
        adapter, snap, scope, engine.project_format().definitions()
    )
    # Added before both returns, so a declared grain the survey could not reach
    # is reported whether the scans run or stop at the handshake.
    warnings.extend(plan.notes)
    checks = drift_mod.cardinality_plan(current_semantic, snap, names)
    scans_needed = bool(
        plan.key_checks
        or plan.fanout_pairs
        or plan.composite_checks
        or plan.declared_composite_checks
        or checks
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
        axis_results = _record_axes(
            store, snap, connector, {a: (f, scope_names) for a, f in by_axis.items()}
        )
        result = _drift_result(axis_results, snap, store, warnings=warnings)
        result.pending_confirmation = pending
        return result

    grain_findings = drift_mod.grain_drift(
        adapter,
        plan,
        timeout_seconds=config.query.timeout_seconds,
        min_rows=config.maintain.grain_min_rows,
    )
    semantic_findings = semantic_findings + _semantic_scope(
        drift_mod.cardinality_drift(adapter, checks, current_semantic), scope_names
    )

    drift_mod.annotate_impacts(schema_findings + volume_findings + grain_findings, snap)
    by_axis = {
        "schema": drift_mod.rank_findings(schema_findings),
        "volume": drift_mod.rank_findings(volume_findings),
        "grain": drift_mod.rank_findings(grain_findings),
    }
    if project_available:
        by_axis["semantic"] = drift_mod.rank_findings(semantic_findings)
    axis_results = _record_axes(
        store, snap, connector, {a: (f, scope_names) for a, f in by_axis.items()}
    )
    result = _drift_result(
        axis_results,
        snap,
        store,
        warnings=warnings
        + _baseline_warnings(store, snap, engine.config.profile_freshness_hours),
    )
    return command_args.stamp_spend(result, adapter)


def cmd_check(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return _drift_envelope(check(engine, getattr(args, "objects", None)))


def reconcile(engine: DexEngine, drift_class: str | None = None) -> ReconcileResult:
    """Propose the edits that reconcile detected drift.

    Reads the last drift report, never re-scans, and writes nothing to the
    project: applying is ``transform apply <plan-id>``, so the human-edit
    conflict handshake is inherited unchanged.

    **Whether a mechanical edit may be proposed at all is the project's to
    declare.** A format that cannot receive an edit, because its source of truth
    is the code that generated it and writing into the reduction would edit an
    artifact regenerated on the next run, declines the write tier, and every
    finding here comes back advisory. That used to be true by accident: both
    mechanical paths key on the ``models/staging/stg_<table>.*`` scaffold
    convention and fail closed, so a generated tree was safe as long as its
    directories happened not to use that vocabulary. Asking the tier makes it
    safe because the format said so. The convention checks stay as a second line;
    the declaration replaces the coincidence, not the check that made the
    coincidence survivable.
    """

    from ..adapters.project import PlacingProject, placement_gap
    from ..transform import plans as plans_mod
    from . import reconcile as reconcile_mod

    store = engine.store
    snap = _require_baseline(store)
    report = _stored_drift(store)
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

    editable = engine.editable_project()
    view = None
    if editable is None:
        named = getattr(engine.project_format(), "name", "this")
        warnings.append(
            f"the '{named}' project format does not implement the write tier, so "
            "every proposal below is advisory and no plan is stored: dex will not "
            "author an edit into a project the format says cannot receive one. "
            "Reconcile what these findings describe wherever your models are "
            "actually defined"
        )
    elif not isinstance(editable, PlacingProject):
        # A format holding some of `PlacingProject` and not the rest gets told
        # which member is missing, because the general message below would send
        # it to the ones it already wrote. That is the difference between the
        # gap being reported here and it surfacing as `AttributeError` from
        # inside the reconcile the tier check already let through.
        warnings.append(
            placement_gap(editable)
            or (
                f"the '{editable.name}' project format implements the write tier, "
                "but does not say where a proposed edit lands, so reconcile has no "
                "path to plan an edit against and every proposal below is "
                "advisory. A format reaches this path by implementing "
                "`PlacingProject`: `load` returns the view an edit is pinned "
                "against, `edit_path` answers where an edit of a given kind goes "
                "and may decline a kind by answering None, and `editing_surface` "
                "declares the region those paths must stay inside"
            )
        )
    else:
        try:
            view = editable.load()
        except (ProjectError, ValueError) as exc:
            raise ProjectError(f"reconcile edits a project: {exc}") from exc

    # Hoisted rather than inlined into the call: this load can now refuse, and a
    # refusal raised from inside an argument list reads as though `build` did it.
    cache = readable_cache(store)

    # What the project declares, which is a different question from what its
    # files contain. `view` carries the bytes an edit is pinned against; this
    # carries the grain, and an edit that contradicts a declared grain is one no
    # format is obliged to keep. Tier 1, so it cannot raise.
    definitions = engine.project_format().definitions()
    proposals, edits, build_warnings = reconcile_mod.build(
        findings,
        snap,
        cache,
        view,
        pii_overrides=pii_override_paths(engine.config.pii_overrides),
        placement=editable if isinstance(editable, PlacingProject) else None,
        definitions=definitions,
    )
    warnings.extend(build_warnings)
    # Reconcile was the one project-reading command that dropped these, and the
    # declarations channel is one no command read at all: a format that says it
    # could not supply something says it here too, where an edit is at stake.
    warnings.extend(
        _layer_notes(definitions, snap.transform_layer, snap.semantic_layer)
    )

    result = ReconcileResult(proposals=proposals, warnings=warnings)
    # A plan is pinned to the directory its edits were planned against, so there is
    # no plan without a project to pin to. `build` returns no edits without one,
    # which is the same statement from the other side.
    if edits and view is not None:
        # Only the plan store needs the repo root, so asking for it here keeps the
        # advisory-only path reachable for an engine that has none.
        repo_root = engine.require_repo_root("storing a reconcile plan")
        intent = (
            f"maintain reconcile {drift_class}" if drift_class else "maintain reconcile"
        )
        plan, diffs, plan_warnings = plans_mod.plan(
            intent,
            edits,
            project_dir=view.root,
            repo_root=repo_root,
            store=store,
            # The format that placed these edits also validates them. Passing it
            # is what keeps the two halves of the check on the same project: the
            # surface each path must land in, and the file each edit is pinned
            # against. dbt reaches the identical checks through this argument as
            # it did without it, and reuses the view it already loaded.
            project_format=editable,
        )
        result.diffs = diffs
        result.warnings.extend(plan_warnings)
        result.plan_id = plan.plan_id
    return result


def cmd_reconcile(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    drift_class = getattr(args, "drift_class", None)
    try:
        result = reconcile(engine, drift_class)
    except (NoBaselineError, BaselineUnreadableError, ProjectError) as exc:
        return env.error_for(exc)
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
    engine: DexEngine, axis: str, detector, objects: list[str] | None, noter=None
) -> DriftResult:
    """One metadata-only detector: free on every connector, so no handshake.
    The cost stamp still reflects the connector's paradigm for the caller.

    ``noter`` reports what the detector could not examine, over the same inputs.
    An axis that silently declines to check an object is indistinguishable from
    one that checked and found nothing, and the two mean opposite things.
    """

    store = engine.store
    snap = _require_baseline(store)

    adapter = engine._adapter(f"maintain {axis}")
    current = snapshot_mod.warehouse_from_metadata(adapter).datasets
    cost = command_args.preflight_cost(adapter)
    connector = adapter.name

    scope_names = list(objects or [])
    scope = _resolve_scope(scope_names, current, snap)
    findings = detector(current, snap, scope)
    drift_mod.annotate_impacts(findings, snap)
    ranked = drift_mod.rank_findings(findings)

    by_axis = _record_axes(store, snap, connector, {axis: (ranked, scope_names)})
    result = _drift_result(
        by_axis,
        snap,
        store,
        warnings=_baseline_warnings(store, snap, engine.config.profile_freshness_hours)
        + (noter(current, snap, scope) if noter is not None else []),
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
) -> dict[str, drift_mod.AxisResult]:
    """Merge this run's axes into the stored drift report. Axes merge across runs so
    a focused detector refreshes only itself, but never across baselines:
    findings measured against an older snapshot are dropped wholesale."""

    report = _stored_drift(store)
    if report is None or report.snapshot_created_at != snap.created_at:
        report = drift_mod.DriftReport()
    report.connector = connector
    report.snapshot_created_at = snap.created_at
    run_at = datetime.now(UTC).isoformat()
    current: dict[str, drift_mod.AxisResult] = {}
    for axis, (findings, scope_names) in results.items():
        current[axis] = drift_mod.AxisResult(
            run_at=run_at, scope=scope_names or None, findings=findings
        )
        report.axes[axis] = current[axis]
    store.save_drift(report)
    return current


def _drift_result(
    by_axis: dict[str, drift_mod.AxisResult],
    snap: snapshot_mod.Snapshot,
    store: MaintainStore,
    *,
    warnings: list[str],
) -> DriftResult:
    return DriftResult(
        findings=drift_mod.rank_findings(
            [finding for result in by_axis.values() for finding in result.findings]
        ),
        by_axis=by_axis,
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


def _column_detail_warnings(snap: snapshot_mod.Snapshot) -> list[str]:
    """What the schema axis could not compare, and how to make it able to.

    The axis reports nothing for an object the baseline holds no columns for,
    which is right (unknown is not empty) and would otherwise be indistinguishable
    from a clean bill. Naming the count is what keeps the silence honest.
    """

    thin = snap.warehouse.without_column_detail()
    if not thin:
        return []
    return [
        f"{len(thin)} of {len(snap.warehouse.datasets)} baseline object(s) were "
        "pinned without column detail, so the schema axis compared no columns "
        f"for them and the grain axis has no keys to probe: {name_list(thin)}. "
        "Run `explore map --full` and re-run `maintain snapshot` to cover them"
    ]


def _cache_age_warnings(
    snap: snapshot_mod.Snapshot, freshness_hours: float, now: datetime
) -> list[str]:
    """Whether the baseline's warehouse side still describes the warehouse.

    Deliberately not a write-time comparison. Comparing the cache's timestamp to
    the baseline's goes quiet the moment a re-pin makes the baseline the newer
    file, which is the wrong moment to go quiet: the contents are still that
    same old cache, and the caller has just been told their accept succeeded.
    This reads the age of the contents instead (``cache_updated_at``, recorded
    at pin time), so re-pinning cannot silence it.

    ``profile_freshness_hours`` is the threshold rather than a new setting of its
    own: it already defines how old a cached profile may be before `explore`
    re-scans it, and a baseline pinned from profiles `explore` would refuse to
    reuse is the same judgement applied one layer up.
    """

    if snap.warehouse_from != "cache" or not snap.cache_updated_at:
        return []
    try:
        captured = datetime.fromisoformat(snap.cache_updated_at)
    except ValueError:
        return []
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)
    age_hours = (now - captured).total_seconds() / 3600
    if age_hours <= freshness_hours:
        return []
    return [
        "this baseline's warehouse side was pinned from an exploration cache "
        f"captured {age_hours:.0f}h ago ({snap.cache_updated_at}), beyond the "
        f"{freshness_hours:g}h freshness window; anything created or changed in "
        "the warehouse since then is not in it and will report as drift. Run "
        "`explore map` first, then `maintain snapshot`, to pin current state"
    ]


def _layer_notes(*layers: object) -> list[str]:
    """What the project format said it could not supply, from every layer read.

    Both sides are collected, baseline and freshly read, because the two payload
    sites read different ones: ``dangling_source`` compares against the
    baseline's declared sources, while ``definition_changed`` reads the current
    project. Surfacing one side would leave the other axis's limits unexplained.
    In practice both come from the same format and say the same thing, so the
    common case deduplicates to one line.

    Anything carrying ``notes`` is accepted, not only the two snapshot layers.
    ``ProjectDefinitions`` carries them on the declarations channel, which is
    where a format explains an ambiguous or unreadable project, and reconcile
    reads that one because a declaration it could not see is the difference
    between declining an edit and proposing a wrong one.
    """

    seen: list[str] = []
    for layer in layers:
        for note in getattr(layer, "notes", None) or []:
            if note not in seen:
                seen.append(note)
    return seen


def _adapter_notes(adapter, identifiers: list[str]) -> list[str]:
    """Surface the adapter's per-table notes (e.g. a skipped distinct-count
    escalation on a tight budget) so a silent skip never reads as a clean bill."""

    return _flatten_adapter_notes(_adapter_notes_by_identifier(adapter, identifiers))


def _adapter_notes_by_identifier(
    adapter, identifiers: list[str]
) -> dict[str, list[str]]:
    """Collect adapter notes by table so command payloads can attach qualifying
    facts to the findings they qualify, not only to envelope warnings."""

    hook = getattr(adapter, "table_notes", None)
    if hook is None:
        return {}
    notes: dict[str, list[str]] = {}
    for identifier in identifiers:
        table_notes = list(hook(identifier) or [])
        if table_notes:
            notes[identifier] = table_notes
    return notes


def _flatten_adapter_notes(notes: dict[str, list[str]]) -> list[str]:
    return [
        f"{identifier}: {note}"
        for identifier, table_notes in notes.items()
        for note in table_notes
    ]


def _qualify_uniqueness_findings(
    findings: list[drift_mod.DriftFinding], notes_by_identifier: dict[str, list[str]]
) -> None:
    for finding in findings:
        if (
            finding.code != "key_lost_uniqueness"
            or finding.identifier not in notes_by_identifier
        ):
            continue
        merge_notes = [
            note
            for note in notes_by_identifier[finding.identifier]
            if _final_note(note)
        ]
        if not merge_notes:
            continue
        finding.severity = "medium"
        finding.data["table_notes"] = merge_notes
        finding.detail = (
            f"{finding.detail}; this count is over stored parts before "
            "ClickHouse FINAL, so the adapter note qualifies whether this is "
            "merge timing or modeled-grain drift"
        )


def _final_note(note: str) -> bool:
    return "FINAL" in note and "MergeTree" in note


def _semantic_names(scope_names: list[str]) -> set[str]:
    """Split/lower repeatable, comma-joinable scope arguments into name tokens.

    Shared by ``_semantic_scope`` (filters reported findings) and
    ``cardinality_plan``'s ``scope`` (filters *before* the paid scan runs), so
    a name that narrows the report also narrows the bill.
    """

    return {
        part.strip().lower()
        for raw in scope_names
        for part in raw.split(",")
        if part.strip()
    }


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
    names = _semantic_names(scope_names)

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


def _baseline_warnings(
    store: MaintainStore, snap: snapshot_mod.Snapshot, freshness_hours: float
) -> list[str]:
    """Every reason this baseline may not describe the warehouse, in one place.

    A present baseline is not a valid one, and the ways it can be invalid are
    axis-independent: it can be superseded by a newer cache, or pinned from a
    cache that was already old. Both belong wherever a baseline is read, so this
    is called by every detector rather than reimplemented per axis. What a thin
    baseline cannot compare is axis-specific and stays in
    :func:`_column_detail_warnings`.
    """

    warnings: list[str] = []
    cache = readable_cache(store)
    if (
        cache is not None
        and cache.provenance.updated_at
        and cache.provenance.updated_at > snap.created_at
    ):
        # Names both commands on purpose. `maintain snapshot` alone re-pins
        # whatever the cache already holds, so on a warehouse past `explore
        # map`'s rank cutoff the cheap path and the correct path diverge: it
        # would pin the same partial coverage and leave the schema axis unable
        # to compare most objects. The advice has to point at the one that ends
        # with a baseline worth measuring against.
        warnings.append(
            "the exploration cache is newer than the drift baseline; if the current "
            "state is known-good, re-run `explore map` (or `explore map --full` to "
            "cover objects past the rank cutoff) and then `maintain snapshot`, so "
            "drift is not measured against a stale baseline"
        )
    warnings.extend(
        _cache_age_warnings(snap, freshness_hours, datetime.now(UTC)),
    )
    return warnings
