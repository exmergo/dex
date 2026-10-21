"""Drift detection: compare current reality against the snapshot baseline.

Detectors are pure comparisons over the snapshot and a freshly captured current
state, so they are testable without a warehouse; the command layer owns the
adapter and the handshake. Findings carry names, types, and aggregate numbers
only, never a data value: the structured facts live in ``data`` and the
analyst-readable sentence in ``detail``.

The per-axis cost model is deliberate and connector-honest. Schema and volume
read metadata, free on every connector. Grain re-runs the exact-distinct and
join-overlap aggregates, billed on metered connectors. The semantic axis splits:
definition changes, dangling references, and impact analysis are computed from
the snapshot and the project (free), while categorical-cardinality drift is a
distinct-count scan (billed where scans bill).

Detection results persist in ``.dex/drift.json`` per axis, so the stateless
``reconcile`` reads what detection found rather than re-scanning (and
re-spending) on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from pydantic import BaseModel, Field

from ..adapters.base import Adapter, ColumnMeta, TableProbe, distinct_combination_sql
from ..cache import Dataset, Relationship, match_identifier
from ..dbt_project import ProjectDefinitions
from ..explore import relationships as rel_mod
from .snapshot import SemanticLayer, SemanticModelDef, Snapshot, TransformLayer

#: Deliberately without the supported-set gate `SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS`
#: gives the baseline, and this is where that decision is recorded rather than
#: left for the next reader to re-derive.
#:
#: The two documents are not the same kind of thing. The baseline is *vouched
#: for*: someone ran `maintain snapshot` against a state they accepted, nothing
#: else can reproduce it, and drift measured against a version the engine
#: misreads is a wrong answer that looks like a right one. The drift report is
#: *derived*: it is the output of the command that reads it, `maintain check`
#: regenerates it from the baseline on demand, `_record_axes` already discards
#: one measured against a different baseline wholesale, and `reconcile` already
#: has an absent-report path that names the command producing one. A version it
#: does not recognize therefore has a cheap correct answer (rebuild) that the
#: baseline does not have.
#:
#: Corruption is handled separately from versioning, and the remedy the paragraph
#: above predicted is the one that shipped. A drift report that fails to parse
#: used to raise out of `load_drift` uncaught and be classified as a request
#: error, telling an operator they typed something wrong; `_stored_drift` now
#: catches that and treats it as absent, so both callers take the rebuild path
#: they already had for a missing report. Deliberately NOT the baseline's
#: refusal: the argument above is exactly why the same input deserves opposite
#: answers on the two documents.
DRIFT_SCHEMA_VERSION = 1

FREE_AXES = ("schema", "volume")
BILLED_AXES = ("grain", "semantic")
AXES = FREE_AXES + BILLED_AXES

# Relative row-count changes below this are load chatter, not drift; a shrink
# beyond _VOLUME_HIGH_DROP (or a table emptying) is the classic half-failed load.
_VOLUME_REPORT_FRACTION = 0.10
_VOLUME_HIGH_DROP = 0.50

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# A dimension-cardinality baseline is often an approximate (HyperLogLog) count:
# explore escalates only near-unique columns to an exact COUNT(DISTINCT), so a
# low-cardinality categorical dimension keeps its sketch estimate in the
# snapshot. A delta within the sketch's error band is noise, not drift, and must
# not fire a finding against an exact current count. Retroactively re-measuring
# the historical baseline is impossible, so deltas inside this band are gated.
_APPROX_CARDINALITY_TOLERANCE = 0.02


class DriftFinding(BaseModel):
    """One detected drift.

    ``exact`` is the honesty flag: False when the verdict rests on an
    approximate baseline (an unescalated distinct count, an estimated row
    count), so a wobble is never presented as a proof.
    """

    axis: str
    code: str
    identifier: str | None = None
    column: str | None = None
    severity: str = "medium"
    detail: str
    exact: bool = True
    data: dict = Field(default_factory=dict)
    impacted_models: list[str] = Field(default_factory=list)
    impacted_metrics: list[str] = Field(default_factory=list)


class AxisResult(BaseModel):
    run_at: str
    scope: list[str] | None = None
    findings: list[DriftFinding] = Field(default_factory=list)


class DriftReport(BaseModel):
    """What `.dex/drift.json` holds: the last detection result per axis.

    Axes merge across runs (a focused ``maintain grain`` refreshes only its
    axis), but never across baselines: findings measured against an older
    snapshot are dropped wholesale when the baseline changes.
    """

    schema_version: int = DRIFT_SCHEMA_VERSION
    connector: str | None = None
    snapshot_created_at: str | None = None
    axes: dict[str, AxisResult] = Field(default_factory=dict)


def resolve_scope(names: list[str], identifiers: Iterable[str]) -> set[str]:
    """Expand user-supplied object names into snapshot/warehouse identifiers.

    Ambiguity is fine here (scoping is a filter, not a probe), but a name that
    matches nothing is an error rather than a silently empty report.
    """

    known = list(identifiers)
    resolved: set[str] = set()
    for raw in names:
        for name in (part.strip() for part in raw.split(",")):
            if not name:
                continue
            matches = match_identifier(name, known)
            if not matches:
                raise ValueError(
                    f"no object named '{name}' in the snapshot or the warehouse"
                )
            resolved.update(matches)
    return resolved


def schema_drift(
    current: list[Dataset],
    snap: Snapshot,
    scope: set[str] | None = None,
    current_transform: TransformLayer | None = None,
) -> list[DriftFinding]:
    """Structural drift: metadata only, free on every connector.

    ``current_transform`` (the live project's fingerprint, not the baseline's)
    enables ``orphan_relation``: a table present at the baseline and now, that
    the baseline's project built or sourced but the live project no longer
    does. Without it (no project available), that check is skipped.

    ``transform_drift`` is the same transform layer compared a second way (a
    model added, removed, or content-changed since the baseline) and is its
    own function; every caller here runs both and reports them as one axis.
    """

    baseline = {d.identifier: d for d in snap.warehouse.datasets}
    now = {d.identifier: d for d in current}
    findings: list[DriftFinding] = []
    built = _built_tables(current_transform)

    def scoped(identifier: str) -> bool:
        return scope is None or identifier in scope

    findings.extend(
        DriftFinding(
            axis="schema",
            code="table_added",
            identifier=identifier,
            severity="low",
            detail=f"{identifier} is new since the snapshot",
        )
        for identifier in sorted(now.keys() - baseline.keys())
        if scoped(identifier) and not _built_by_project(identifier, built)
    )
    findings.extend(
        DriftFinding(
            axis="schema",
            code="table_dropped",
            identifier=identifier,
            severity="high",
            detail=f"{identifier} is in the snapshot but gone from the warehouse",
        )
        for identifier in sorted(baseline.keys() - now.keys())
        if scoped(identifier)
    )

    baseline_backed = _backed_tables(snap.transform_layer)
    current_backed = _backed_tables(current_transform)

    for identifier in sorted(baseline.keys() & now.keys()):
        if not scoped(identifier):
            continue
        if snap.transform_layer is not None and current_transform is not None:
            table = identifier.rsplit(".", 1)[-1].lower()
            if table in baseline_backed and table not in current_backed:
                findings.append(
                    DriftFinding(
                        axis="schema",
                        code="orphan_relation",
                        identifier=identifier,
                        severity="medium",
                        detail=(
                            f"{identifier} was built by the dbt project at the "
                            "last snapshot but no model or source currently "
                            "backs it; the warehouse still holds it as residue "
                            "of a rename or removal"
                        ),
                        data={"drop_statement": f"DROP TABLE {identifier};"},
                    )
                )
        if not baseline[identifier].columns:
            # The baseline never captured this object's columns (past `explore
            # map`'s rank cutoff, so it entered the cache as metadata only), and
            # unknown is not empty: diffing against an empty set would report
            # every live column as newly added. Measured on one warehouse, that
            # was 1,548 false `column_added` from 158 unprofiled objects, which
            # made a fresh baseline noisier than the week-stale one it replaced.
            # Table-level findings above still stand, because identity does not
            # need a profile; the command layer names the gap so the silence
            # here is reported rather than merely quiet.
            continue
        old_cols = {c.name: c for c in baseline[identifier].columns}
        new_cols = {c.name: c for c in now[identifier].columns}
        added = sorted(new_cols.keys() - old_cols.keys())
        dropped = sorted(old_cols.keys() - new_cols.keys())
        findings.extend(
            DriftFinding(
                axis="schema",
                code="column_added",
                identifier=identifier,
                column=name,
                severity="low",
                detail=f"{name} was added to {identifier} ({new_cols[name].data_type})",
                data={"data_type": new_cols[name].data_type},
            )
            for name in added
        )
        findings.extend(
            DriftFinding(
                axis="schema",
                code="column_dropped",
                identifier=identifier,
                column=name,
                severity="high",
                detail=(
                    f"{name} was dropped from {identifier} "
                    f"(was {old_cols[name].data_type})"
                ),
                data={"data_type": old_cols[name].data_type},
            )
            for name in dropped
        )
        # A drop and an add of the same type is what a rename looks like from
        # metadata; surfaced as a hint so reconcile and the human can decide.
        findings.extend(
            DriftFinding(
                axis="schema",
                code="possible_rename",
                identifier=identifier,
                column=came,
                severity="low",
                detail=(
                    f"{gone} -> {came} on {identifier} may be a "
                    f"rename (same type {old_cols[gone].data_type})"
                ),
                data={"renamed_from": gone, "renamed_to": came},
            )
            for gone in dropped
            for came in added
            if old_cols[gone].data_type == new_cols[came].data_type
        )
        for name in sorted(old_cols.keys() & new_cols.keys()):
            old, new = old_cols[name], new_cols[name]
            if old.data_type != new.data_type:
                findings.append(
                    DriftFinding(
                        axis="schema",
                        code="column_retyped",
                        identifier=identifier,
                        column=name,
                        severity="high",
                        detail=(
                            f"{name} on {identifier} changed type: "
                            f"{old.data_type} -> {new.data_type}"
                        ),
                        data={
                            "type_before": old.data_type,
                            "type_after": new.data_type,
                        },
                    )
                )
            elif old.nullable != new.nullable:
                findings.append(
                    DriftFinding(
                        axis="schema",
                        code="nullability_changed",
                        identifier=identifier,
                        column=name,
                        severity="medium",
                        detail=(
                            f"{name} on {identifier} is now "
                            f"{'nullable' if new.nullable else 'NOT NULL'} "
                            f"(was {'nullable' if old.nullable else 'NOT NULL'})"
                        ),
                        data={
                            "nullable_before": old.nullable,
                            "nullable_after": new.nullable,
                        },
                    )
                )

    if snap.transform_layer is not None:
        current_identifiers = list(now.keys())
        scoped_tables = (
            {identifier.rsplit(".", 1)[-1].lower() for identifier in scope}
            if scope is not None
            else None
        )
        for source in snap.transform_layer.sources:
            if scoped_tables is not None and source.table.lower() not in scoped_tables:
                continue
            if not match_identifier(source.table, current_identifiers):
                findings.append(
                    DriftFinding(
                        axis="schema",
                        code="dangling_source",
                        identifier=f"{source.source_name}.{source.table}",
                        severity="high",
                        detail=(
                            f"declared source {source.source_name}.{source.table} "
                            "has no matching warehouse object"
                        ),
                        # The key is omitted rather than null when the format has
                        # no file to name: a null provenance forces every reader
                        # to branch, and both branches fall back to the
                        # identifier above. An absent key lets `.get(default)`
                        # do that on its own.
                        data=({"declared_in": source.path} if source.path else {}),
                    )
                )
    return findings


def transform_drift(
    current_transform: TransformLayer | None,
    snap: Snapshot,
) -> list[DriftFinding]:
    """A model added, removed, or content-changed since the baseline.

    Symmetric with ``semantic_free_drift``'s ``definition_added``/``_removed``/
    ``_changed``, over ``TransformLayer.models``/``.files`` instead of
    semantic model/metric definitions. Folded into the ``schema`` axis rather
    than a new one of its own: ``schema_drift`` already loads the project's
    transform layer for ``orphan_relation``, and this is the same fingerprint
    compared a second way.

    ``model_changed`` diffs content hashes through ``model_paths``, so it also
    catches a rewired ``ref()``/``source()`` call: rewiring one changes the
    file's text, and so its hash, with no second comparison over
    ``model_refs``/``model_sources`` needed. A model missing from either
    side's ``model_paths`` (a baseline pinned before that field existed) is
    skipped for the content comparison rather than reported changed: an
    unknown is not a diff.
    """

    findings: list[DriftFinding] = []
    baseline = snap.transform_layer
    if baseline is None or current_transform is None:
        return findings

    base_models = set(baseline.models)
    cur_models = set(current_transform.models)

    findings.extend(
        DriftFinding(
            axis="schema",
            code="model_added",
            identifier=name,
            severity="low",
            detail=f"model '{name}' is new since the baseline",
        )
        for name in sorted(cur_models - base_models)
    )
    findings.extend(
        DriftFinding(
            axis="schema",
            code="model_removed",
            identifier=name,
            severity="low",
            detail=f"model '{name}' was removed since the baseline",
        )
        for name in sorted(base_models - cur_models)
    )
    for name in sorted(base_models & cur_models):
        base_path = baseline.model_paths.get(name)
        cur_path = current_transform.model_paths.get(name)
        if base_path is None or cur_path is None:
            continue
        base_hash = baseline.files.get(base_path)
        cur_hash = current_transform.files.get(cur_path)
        if base_hash is not None and cur_hash is not None and base_hash != cur_hash:
            findings.append(
                DriftFinding(
                    axis="schema",
                    code="model_changed",
                    identifier=name,
                    severity="low",
                    detail=f"model '{name}' changed since the baseline",
                    data={"path": cur_path},
                )
            )
    return findings


def uncomparable_volume(
    current: list[Dataset], snap: Snapshot, scope: set[str] | None = None
) -> list[str]:
    """Objects this axis had to skip because no live row count exists for them.

    The axis runs on free metadata, and for a view or an external table the
    warehouse maintains no count to read, so there is nothing to compare the
    baseline against. :func:`volume_drift` skipping those is correct; skipping
    them in silence is not, because an absent finding reads as "checked, and
    nothing moved". A baseline with a real count on one side and nothing on the
    other is the case worth naming: the object was profiled once, so a reader has
    every reason to expect the axis covers it.
    """

    baseline = {d.identifier: d for d in snap.warehouse.datasets}
    now = {d.identifier: d for d in current}
    skipped = sorted(
        identifier
        for identifier in baseline.keys() & now.keys()
        if (scope is None or identifier in scope)
        and baseline[identifier].row_count is not None
        and now[identifier].row_count is None
    )
    if not skipped:
        return []
    return [
        "no live row count for "
        + ", ".join(skipped)
        + ", so volume was not compared for "
        + ("it" if len(skipped) == 1 else "them")
        + " (the warehouse maintains no count for this kind of object; "
        "`explore profile` counts it, at a scan's cost)"
    ]


def _dataset_of(identifier: str) -> str:
    """The namespace an identifier sits in: everything before its last segment
    (BigQuery ``project.dataset``, DuckDB, Snowflake and Postgres
    ``database.schema``). An identifier with no dot is its own namespace."""

    head, sep, _ = identifier.rpartition(".")
    return head if sep else identifier


def scope_mismatch_warnings(
    current: list[Dataset], snap: Snapshot, scope: set[str] | None = None
) -> list[str]:
    """Datasets the live read and the baseline do not both cover (#543).

    :func:`schema_drift` reports every object in a dataset the baseline never
    covered as ``table_added``, and every object in a dataset the live read no
    longer lists as ``table_dropped``. Both are right as comparisons and wrong
    as diagnoses when the difference is one of scope: a baseline pinned from a
    map that covered one environment's datasets, then a read that lists
    another's, is a block of findings that says nothing about the warehouse
    moving, sitting in the same report as the drift that does. The findings
    stay, because the comparison did see those objects. This names the datasets
    behind them and how many findings they account for, so a reader can tell a
    scope artifact from drift and re-pin if the datasets belong in the baseline.

    ``scope`` narrows the count the way :func:`schema_drift` narrows the
    findings, so the number quoted is the number reported; a mismatch whose
    objects all fall outside the scope reports nothing, like the findings.
    """

    baseline_ids = {d.identifier for d in snap.warehouse.datasets}
    now_ids = {d.identifier for d in current}
    baseline_datasets = {_dataset_of(i) for i in baseline_ids}
    now_datasets = {_dataset_of(i) for i in now_ids}

    def counted(identifiers: set[str], datasets: list[str]) -> int:
        return sum(
            1
            for identifier in identifiers
            if _dataset_of(identifier) in datasets
            and (scope is None or identifier in scope)
        )

    warnings: list[str] = []
    outside = sorted(now_datasets - baseline_datasets)
    added = counted(now_ids - baseline_ids, outside)
    if outside and added:
        warnings.append(
            f"{len(outside)} dataset(s) are in the warehouse but outside the "
            f"baseline's scope ({', '.join(outside)}); the {added} object(s) in "
            f"{'it' if len(outside) == 1 else 'them'} are reported as table_added. "
            "Re-pin with `maintain snapshot` after `explore map --full` if they "
            "belong in the baseline"
        )
    gone = sorted(baseline_datasets - now_datasets)
    dropped = counted(baseline_ids - now_ids, gone)
    if gone and dropped:
        warnings.append(
            f"{len(gone)} dataset(s) are in the baseline but not in the warehouse "
            f"read ({', '.join(gone)}); the {dropped} object(s) in "
            f"{'it' if len(gone) == 1 else 'them'} are reported as table_dropped. "
            "If the dataset was removed, re-pin with `maintain snapshot`; if the "
            "connector's dataset list was narrowed, widen it or re-pin"
        )
    return warnings


def volume_drift(
    current: list[Dataset], snap: Snapshot, scope: set[str] | None = None
) -> list[DriftFinding]:
    """Freshness drift from free metadata: row counts (and byte sizes) that
    moved beyond load chatter. Structure unchanged, keys intact, but the data
    stopped flowing correctly: the axis the other three cannot see.

    An object with no count on either side is not drift and not a finding;
    :func:`uncomparable_volume` is what says so out loud.
    """

    baseline = {d.identifier: d for d in snap.warehouse.datasets}
    now = {d.identifier: d for d in current}
    findings: list[DriftFinding] = []

    for identifier in sorted(baseline.keys() & now.keys()):
        if scope is not None and identifier not in scope:
            continue
        old_ds, new_ds = baseline[identifier], now[identifier]
        old, new = old_ds.row_count, new_ds.row_count
        if old is None or new is None or old == new:
            continue
        if old == 0:
            fraction = None
            severity = "low"
            detail = f"{identifier}: rows 0 -> {new} (was empty at the snapshot)"
        else:
            fraction = (new - old) / old
            if abs(fraction) < _VOLUME_REPORT_FRACTION:
                continue
            if new == 0:
                severity = "high"
                detail = f"{identifier}: emptied since snapshot ({old} rows -> 0)"
            elif fraction <= -_VOLUME_HIGH_DROP:
                severity = "high"
                detail = (
                    f"{identifier}: rows collapsed {old} to {new} ({fraction:+.0%})"
                )
            elif fraction < 0:
                severity = "medium"
                detail = f"{identifier}: rows {old} -> {new} ({fraction:+.1%})"
            else:
                severity = "low"
                detail = f"{identifier}: rows {old} -> {new} ({fraction:+.1%})"
        data: dict = {"row_count_before": old, "row_count_after": new}
        if fraction is not None:
            data["change_fraction"] = round(fraction, 4)
        if old_ds.byte_size is not None and new_ds.byte_size is not None:
            data["byte_size_before"] = old_ds.byte_size
            data["byte_size_after"] = new_ds.byte_size
        findings.append(
            DriftFinding(
                axis="volume",
                code="row_count_changed",
                identifier=identifier,
                severity=severity,
                detail=detail,
                # An unprofiled baseline row count is an inventory-time estimate.
                exact=old_ds.profiled_at is not None,
                data=data,
            )
        )
    return findings


class GrainPlan(NamedTuple):
    """What the grain axis would scan, surveyed from free metadata: uniqueness
    re-checks per keyed baseline dataset, and re-runs of the verified overlap
    probes. Built once so the cost estimate and the confirmed run price and
    execute exactly the same statements."""

    key_checks: list[tuple[Dataset, list[str], int | None]]
    fanout_pairs: list[tuple[Relationship, Relationship]]
    # Proven multi-column keys re-verified as combinations: their members are
    # not individually unique, so they never enter key_checks.
    composite_checks: list[tuple[Dataset, list[list[str]], int | None]]
    # Grains the *project* declares (model-level unique_combination_of_columns)
    # that measurement never proved. Kept apart from `composite_checks` because
    # a failure here is a different fact with a different finding code: nothing
    # lapsed, the declaration was never true. Priced identically.
    declared_composite_checks: list[tuple[Dataset, list[list[str]], int | None]]
    # What the plan could not survey, so an unchecked declaration never reads as
    # a clean bill.
    notes: list[str]
    # Every selected relation gets a null-fraction survey.  This does not need a
    # baseline measurement: a column that is wholly NULL in a non-empty result
    # is a defect in its present shape, not a before-and-after drift claim.
    # Last/defaulted to retain compatibility with focused detector test plans.
    # The count is ``None`` where the connector has no free row count (Trino,
    # Databricks before a sized scan): unknown, which is not the same as empty.
    null_checks: list[tuple[Dataset, list[ColumnMeta], int | None]] | None = None
    # Joins the semantic layer declares an arity for, each side that arity says
    # is a key re-measured as one. Defaulted for the same reason as above.
    join_cardinality_checks: list[JoinCardinalityCheck] | None = None

    def scans(self) -> bool:
        """Whether the plan holds any statement that scans, and so is priced.

        The null survey is left out deliberately: it rides along with whatever
        else the plan scans and never on its own decides that a handshake is
        owed, which is how the axis has always priced it.
        """

        return bool(
            self.key_checks
            or self.fanout_pairs
            or self.composite_checks
            or self.declared_composite_checks
            or self.join_cardinality_checks
        )


class JoinCardinalityCheck(NamedTuple):
    """One side of a declared join whose declared arity makes it a key.

    ``many_to_one`` says the parent's join columns are unique, ``one_to_one``
    says both sides' are. When they are not, every row on the other side can
    match several here, and whatever is aggregated across the join is counted
    more than once: the fanout the declaration rules out.
    """

    relationship: str
    cardinality: str
    model: str
    to_model: str
    side_model: str
    identifier: str
    columns: list[str]
    row_count: int


def grain_plan(
    adapter: Adapter,
    snap: Snapshot,
    scope: set[str] | None = None,
    definitions: ProjectDefinitions | None = None,
    semantic: SemanticLayer | None = None,
) -> GrainPlan:
    """Survey the grain scans from free metadata, measured and declared.

    ``definitions`` is the project speaking for itself. Measurement and
    declaration disagree more often than they look like they should: explore
    lets a measurement-proven single column win the grain verdict over a
    declared composite, and ``candidate_keys`` stays measurement-only because an
    unmeasured declared key is a claim rather than a baseline. Both are the right
    calls for a cache, and together they mean the grain a project actually
    declares was never re-verified on exactly the tables where it matters most.
    Reading the declaration here, at plan time, closes that without putting a
    claim into the measurement store: the declared combinations are surveyed and
    priced with the measured ones, and the cache is untouched.

    ``semantic`` is the current semantic-layer fingerprint, read for what a
    layer that sits straight on warehouse relations declares about them: every
    key a semantic model declares, single-column ones included (nothing else
    re-verifies those for a layer that is not the transformation project), and
    every join whose declared arity makes one side a key. A layer that captured
    neither (dbt's, whose keys arrive through ``definitions``) adds nothing.
    """

    described: dict[str, tuple[set[str], int | None]] = {}
    current = {meta.identifier for meta in adapter.list_objects()}

    def describe(identifier: str) -> tuple[set[str], int | None]:
        if identifier not in described:
            meta, columns = adapter.table_metadata(identifier)
            described[identifier] = ({c.name for c in columns}, meta.row_count)
        return described[identifier]

    declared_by_identifier, notes = _declared_composites(
        definitions, sorted(current), scope
    )
    _fold_semantic_keys(declared_by_identifier, notes, semantic, sorted(current), scope)
    # A count of None is unknown (no free row count on this connector), not
    # zero: the check is planned, and the scan that runs it supplies the count.
    key_checks: list[tuple[Dataset, list[str], int | None]] = []
    composite_checks: list[tuple[Dataset, list[list[str]], int | None]] = []
    declared_checks: list[tuple[Dataset, list[list[str]], int | None]] = []
    for dataset in snap.warehouse.datasets:
        if scope is not None and dataset.identifier not in scope:
            continue
        if dataset.identifier not in current:
            continue  # disappearance is the schema axis's story
        # A composite grain contributes nothing to the single-column set: its
        # members are not individually unique, and checking them one at a time
        # would fabricate key_lost_uniqueness findings on every run.
        keys = sorted(
            {key[0] for key in dataset.candidate_keys if len(key) == 1}
            | (
                set(dataset.grain)
                if dataset.grain and len(dataset.grain) == 1
                else set()
            )
        )
        composites: list[list[str]] = []
        for combo in [k for k in dataset.candidate_keys if len(k) > 1] + (
            [dataset.grain] if dataset.grain and len(dataset.grain) > 1 else []
        ):
            if list(combo) not in composites:
                composites.append(list(combo))
        if not keys and not composites:
            continue
        columns, row_count = describe(dataset.identifier)
        live_keys = [k for k in keys if k in columns]
        # A combination with a vanished member is the schema axis's story,
        # same as a vanished single key.
        live_composites = [c for c in composites if set(c) <= columns]
        if live_keys and row_count != 0:
            key_checks.append((dataset, live_keys, row_count))
        if live_composites and row_count != 0:
            composite_checks.append((dataset, live_composites, row_count))

    # Declared grains are surveyed against the *current* warehouse rather than
    # against the baseline, which is the one way they differ structurally from
    # everything above. A measured check needs a before to compare with, so it can
    # only speak about an object the baseline captured. A declaration needs
    # nothing of the kind: the project's claim is the standard, and the question
    # is whether the data meets it today. Driving these off the baseline too would
    # go quiet on a model built since the last snapshot, which is exactly when a
    # freshly declared grain is most likely to be wrong.
    measured_composites = {
        dataset.identifier: list(combos) for dataset, combos, _rows in composite_checks
    }
    # A declared single-column key measurement already proved is checked once
    # too, for the same reason: a semantic layer's primary key is usually the
    # very column explore found unique.
    for dataset, keys, _rows in key_checks:
        measured_composites.setdefault(dataset.identifier, []).extend(
            [key] for key in keys
        )
    baselined = {dataset.identifier: dataset for dataset in snap.warehouse.datasets}
    for identifier, declared in sorted(declared_by_identifier.items()):
        columns, row_count = describe(identifier)
        # A declared combination measurement also proved is checked once, as a
        # measured one: it has a baseline, so `key_lost_uniqueness` is the true
        # statement about it and the weaker declared code would be a downgrade.
        known = measured_composites.get(identifier, [])
        live_declared = [
            combo
            for combo in declared
            if set(combo) <= columns
            and not any(_same_combo(combo, seen) for seen in known)
        ]
        missing = [combo for combo in declared if not set(combo) <= columns]
        if missing:
            notes.append(
                f"the declared grain on {identifier} names columns it does not "
                "have ("
                + "; ".join(
                    ", ".join(sorted(set(combo) - columns)) for combo in missing
                )
                + "), so it was not verified"
            )
        if not live_declared:
            continue
        if row_count == 0:
            notes.append(
                f"{identifier} has no rows to compare against, so its declared "
                "grain was not verified"
            )
            continue
        # A `Dataset` carrying only the identifier where the baseline has none:
        # the declared pass reads nothing else off it, and inventing measurements
        # for an object nobody profiled would put a claim where a baseline goes.
        declared_checks.append(
            (
                baselined.get(identifier) or Dataset(identifier=identifier),
                live_declared,
                row_count,
            )
        )

    fanout_pairs: list[tuple[Relationship, Relationship]] = []
    for rel in snap.warehouse.relationships:
        if not rel.verified or rel.orphan_fraction is None:
            continue
        if scope is not None and not {rel.from_dataset, rel.to_dataset} & scope:
            continue
        if rel.from_dataset not in current or rel.to_dataset not in current:
            continue
        from_columns, _ = describe(rel.from_dataset)
        to_columns, _ = describe(rel.to_dataset)
        if rel.from_columns[0] in from_columns and rel.to_columns[0] in to_columns:
            fanout_pairs.append((rel, rel.model_copy(deep=True)))
    unsurveyed = [
        dataset.identifier
        for dataset, _combos, _rows in declared_checks
        if getattr(adapter, "distinct_combination_counts", None) is None
    ]
    if unsurveyed:
        # Named rather than dropped: an adapter with no combination probe cannot
        # verify a declared grain, and a plan that quietly omitted the scan would
        # report the same empty result a holding grain does.
        notes = [
            *notes,
            "this connector cannot probe column combinations, so the declared "
            "grains on " + ", ".join(sorted(set(unsurveyed))) + " were not verified",
        ]
        declared_checks = []
    null_checks: list[tuple[Dataset, list[ColumnMeta], int | None]] = []
    if callable(getattr(adapter, "column_aggregates", None)):
        for dataset in snap.warehouse.datasets:
            if scope is not None and dataset.identifier not in scope:
                continue
            if dataset.identifier not in current:
                continue
            meta, columns = adapter.table_metadata(dataset.identifier)
            null_checks.append((dataset, columns, meta.row_count))

    cardinality_checks = _join_cardinality_checks(
        semantic, sorted(current), scope, describe, notes
    )
    if (
        cardinality_checks
        and getattr(adapter, "distinct_combination_counts", None) is None
    ):
        notes.append(
            "this connector cannot probe column combinations, so the declared "
            "cardinality of "
            + ", ".join(sorted({check.relationship for check in cardinality_checks}))
            + " was not verified"
        )
        cardinality_checks = []

    return GrainPlan(
        key_checks,
        fanout_pairs,
        composite_checks,
        declared_checks,
        notes,
        null_checks,
        cardinality_checks,
    )


def _fold_semantic_keys(
    by_identifier: dict[str, list[list[str]]],
    notes: list[str],
    semantic: SemanticLayer | None,
    known: list[str],
    scope: set[str] | None,
) -> None:
    """Add the keys a semantic layer declares to the declared-grain survey.

    Every arity, because the survey measures a combination and a one-column
    combination is a key. A declaration already in the survey (a composite the
    declarations channel carried too) is not added twice. Only for a layer that
    captured its keys: one that did not leaves ``keys`` empty because it has
    nowhere to declare them, not because it declares none.
    """

    if semantic is None or not semantic.relationships_and_keys_captured:
        return
    for sm in semantic.semantic_models:
        if not sm.keys:
            continue
        matches = _matched_identifier(sm, known)
        if len(matches) != 1:
            notes.append(
                f"the keys declared on semantic model '{sm.name}' "
                + (
                    "match several warehouse objects"
                    if matches
                    else "match no warehouse object"
                )
                + ", so they were not verified"
            )
            continue
        identifier = matches[0]
        if scope is not None and identifier not in scope:
            continue
        bucket = by_identifier.setdefault(identifier, [])
        for combo in sm.keys:
            if not any(_same_combo(combo, seen) for seen in bucket):
                bucket.append(list(combo))


def _join_cardinality_checks(
    semantic: SemanticLayer | None,
    known: list[str],
    scope: set[str] | None,
    describe,
    notes: list[str],
) -> list[JoinCardinalityCheck]:
    """The key each declared join's arity implies, as checks to run.

    A join the layer could have given an arity and did not is named as
    unchecked, because the silence a holding join produces is the same silence
    an unchecked one would. ``many_to_many`` declares no bound, so it has
    nothing to exceed and is not mentioned.
    """

    if semantic is None or not semantic.relationship_cardinality_captured:
        return []
    models = {sm.name: sm for sm in semantic.semantic_models}
    checks: list[JoinCardinalityCheck] = []
    unchecked: list[str] = []
    for rel in semantic.relationships:
        if rel.cardinality is None:
            unchecked.append(rel.name)
            continue
        sides: list[tuple[str, list[str]]] = []
        if rel.cardinality in ("many_to_one", "one_to_one"):
            sides.append((rel.to_model, [to for _frm, to in rel.column_pairs]))
        if rel.cardinality == "one_to_one":
            sides.append((rel.model, [frm for frm, _to in rel.column_pairs]))
        for side_model, columns in sides:
            sm = models.get(side_model)
            matches = _matched_identifier(sm, known) if sm is not None else []
            if len(matches) != 1:
                notes.append(
                    f"join '{rel.name}' is declared {rel.cardinality}, and "
                    f"'{side_model}' "
                    + (
                        "matches several warehouse objects"
                        if matches
                        else "matches no warehouse object"
                    )
                    + ", so its fanout was not verified"
                )
                continue
            identifier = matches[0]
            if scope is not None and identifier not in scope:
                continue
            live_columns, row_count = describe(identifier)
            missing = [c for c in columns if c not in live_columns]
            if missing:
                # A vanished join column is the schema and semantic axes' story.
                notes.append(
                    f"join '{rel.name}' keys on {', '.join(missing)}, which "
                    f"{identifier} no longer has, so its fanout was not verified"
                )
                continue
            if not row_count:
                continue
            checks.append(
                JoinCardinalityCheck(
                    relationship=rel.name,
                    cardinality=rel.cardinality,
                    model=rel.model,
                    to_model=rel.to_model,
                    side_model=side_model,
                    identifier=identifier,
                    columns=columns,
                    row_count=row_count,
                )
            )
    if unchecked:
        notes.append(
            "no cardinality is declared on join(s) "
            + ", ".join(sorted(unchecked))
            + ", so their fanout was not checked; declare one (many_to_one, "
            "one_to_one, one_to_many or many_to_many) to have it verified"
        )
    return checks


def _same_combo(left: list[str], right: list[str]) -> bool:
    """Two column combinations naming the same grain: order and case are the
    project's spelling choices, not part of what the combination asserts."""

    return {c.lower() for c in left} == {c.lower() for c in right}


def _declared_composites(
    definitions: ProjectDefinitions | None,
    known: list[str],
    scope: set[str] | None,
) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Declared composite grains keyed by warehouse identifier, and what could
    not be resolved.

    A declaration names a *model*; the plan scans an *identifier*. Resolution
    goes through the same ``resolve_declared`` explore uses, so a declaration
    that maps cleanly for the diagram maps cleanly here. Both failure modes are
    noted rather than skipped, because "no finding" is what a holding grain also
    looks like.
    """

    if definitions is None or not definitions.declared_composite_keys:
        return {}, []

    by_identifier: dict[str, list[list[str]]] = {}
    notes: list[str] = []
    for composite in definitions.declared_composite_keys:
        identifier, ambiguous = rel_mod.resolve_declared(
            composite.relation, composite.model, known
        )
        if identifier is None:
            notes.append(
                f"the declared grain on '{composite.model}' "
                f"({', '.join(composite.columns)}) "
                + (
                    "matches several warehouse objects"
                    if ambiguous
                    else "matches no warehouse object"
                )
                + ", so it was not verified"
            )
            continue
        if scope is not None and identifier not in scope:
            continue
        bucket = by_identifier.setdefault(identifier, [])
        if not any(_same_combo(composite.columns, seen) for seen in bucket):
            bucket.append(list(composite.columns))
    return by_identifier, notes


class ProbeNeeds(NamedTuple):
    """The probes one table owes a drift run, gathered into one statement."""

    distinct: list[str]
    combinations: list[list[str]]
    null_columns: list[ColumnMeta]


def batched_probes(
    adapter: Adapter,
    plan: GrainPlan,
    cardinality_checks: list[CardinalityCheck] | None = None,
) -> dict[str, ProbeNeeds]:
    """The tables whose drift probes run as one statement each (#275).

    A table can owe the run several separate statements: a distinct count over
    its proven keys, a combination probe per composite list and per declared
    join arity, a null survey, and, under ``maintain check``, a distinct count
    for the dimensions the semantic layer reads from it. A warehouse with a
    per-statement billing minimum (BigQuery's 10 MB, charged per statement and
    per table referenced) bills each of those its own minimum, so the cost of a
    sweep tracked how many probes it asked rather than what they read.

    Only an adapter offering ``table_probe_sql``/``table_probe`` is batched, and
    only a table that would otherwise issue two or more statements, so a table
    with one probe runs exactly what it ran before. A table the adapter cannot
    probe in one statement keeps its separate probes; one whose null survey
    cannot ride along (a block-sampled table) is batched without it. The
    estimate and the run both read this map, so they price and execute the
    same statements.
    """

    probe_sql = getattr(adapter, "table_probe_sql", None)
    if not callable(probe_sql) or not callable(getattr(adapter, "table_probe", None)):
        return {}
    distinct: dict[str, list[str]] = {}
    combinations: dict[str, list[list[str]]] = {}
    null_columns: dict[str, list[ColumnMeta]] = {}
    statements: dict[str, int] = {}

    def add_distinct(identifier: str, columns: list[str]) -> None:
        target = distinct.setdefault(identifier, [])
        target.extend(column for column in columns if column not in target)
        statements[identifier] = statements.get(identifier, 0) + 1

    def add_combinations(identifier: str, combos: list[list[str]]) -> None:
        target = combinations.setdefault(identifier, [])
        target.extend(list(combo) for combo in combos if list(combo) not in target)
        statements[identifier] = statements.get(identifier, 0) + 1

    # Mirrors what `grain_drift` and `cardinality_drift` would each issue, list
    # by list, including the statements they never issue.
    for dataset, keys, _rows in plan.key_checks:
        if keys:
            add_distinct(dataset.identifier, keys)
    if getattr(adapter, "distinct_combination_counts", None) is not None:
        for dataset, combos, _rows in (
            plan.composite_checks + plan.declared_composite_checks
        ):
            if combos:
                add_combinations(dataset.identifier, combos)
        for check in plan.join_cardinality_checks or []:
            add_combinations(check.identifier, [check.columns])
    if callable(getattr(adapter, "column_aggregates", None)):
        for dataset, columns, row_count in plan.null_checks or []:
            if row_count == 0 or not columns:
                continue  # reported as no_rows without a scan
            null_columns[dataset.identifier] = list(columns)
            statements[dataset.identifier] = statements.get(dataset.identifier, 0) + 1
    for identifier, columns in _live_cardinality_columns(
        adapter, cardinality_checks or []
    ).items():
        if columns:
            add_distinct(identifier, columns)

    batched: dict[str, ProbeNeeds] = {}
    for identifier, count in statements.items():
        if count < 2:
            continue
        needs = ProbeNeeds(
            distinct.get(identifier, []),
            combinations.get(identifier, []),
            null_columns.get(identifier, []),
        )
        if probe_sql(identifier, **needs._asdict()) is None:
            if not needs.null_columns or count - 1 < 2:
                continue
            needs = needs._replace(null_columns=[])
            if probe_sql(identifier, **needs._asdict()) is None:
                continue
        batched[identifier] = needs
    return batched


def run_batched_probes(
    adapter: Adapter, batched: dict[str, ProbeNeeds]
) -> dict[str, TableProbe]:
    """Run each batched table's one statement. A table the adapter declined at
    run time (a statement it would not plan, a budget that could not cover it)
    is absent, so its probes run separately and degrade on their own terms."""

    results: dict[str, TableProbe] = {}
    for identifier, needs in batched.items():
        probe = adapter.table_probe(identifier, **needs._asdict())
        if probe is not None:
            results[identifier] = probe
    return results


def grain_estimate(
    adapter: Adapter,
    plan: GrainPlan,
    batched: dict[str, ProbeNeeds] | None = None,
) -> tuple[float, dict[str, float]]:
    """Free dry-run pricing of the plan's scans; zero on free adapters.

    Every list on the plan is priced, declared combinations included: the plan is
    the single survey both this and ``grain_drift`` read, so a scan that reaches
    execution without appearing here would be spend the operator never saw.

    ``batched`` is :func:`batched_probes` for the same plan (computed here when
    omitted): a batched table is priced as its one statement, and its separate
    probes are not priced again. It may carry cardinality columns as well, and
    :func:`cardinality_estimate` is handed the same map so neither prices them
    twice.
    """

    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None:
        return 0.0, {}
    if batched is None:
        batched = batched_probes(adapter, plan)
    per_table: dict[str, float] = {}
    for identifier, needs in batched.items():
        per_table[identifier] = query_estimate(
            adapter.table_probe_sql(identifier, **needs._asdict())
        )
    for dataset, keys, _row_count in plan.key_checks:
        if dataset.identifier in batched:
            continue
        per_table[dataset.identifier] = query_estimate(
            _distinct_count_sql(dataset.identifier, keys, adapter.dialect)
        )
    for dataset, combos, _row_count in (
        plan.composite_checks + plan.declared_composite_checks
    ):
        if dataset.identifier in batched:
            continue
        per_table[dataset.identifier] = per_table.get(
            dataset.identifier, 0.0
        ) + query_estimate(
            _distinct_combination_stand_in(dataset.identifier, combos, adapter.dialect)
        )
    for check in plan.join_cardinality_checks or []:
        if check.identifier in batched:
            continue
        per_table[check.identifier] = per_table.get(
            check.identifier, 0.0
        ) + query_estimate(
            _distinct_combination_stand_in(
                check.identifier, [check.columns], adapter.dialect
            )
        )
    probes = rel_mod.probe_statements(
        [live for _baseline, live in plan.fanout_pairs], adapter.dialect
    )
    if probes:
        per_table["(join overlap probes)"] = sum(query_estimate(sql) for sql in probes)
    profile_estimate = getattr(adapter, "profile_estimate", None)
    # A survey riding a batched statement is priced with it above.
    null_checks = [
        (dataset, columns, rows)
        for dataset, columns, rows in plan.null_checks or []
        if not (
            dataset.identifier in batched and batched[dataset.identifier].null_columns
        )
    ]
    if profile_estimate is not None and null_checks:
        identifiers = [dataset.identifier for dataset, _columns, _rows in null_checks]
        # A NULL fraction is meaningful for binary columns too, unlike an
        # explore value profile; opt blobs back into the survey so the estimate
        # and the aggregate execution cover exactly the same columns.
        include_blobs = {
            f"{dataset.identifier}.{column.name}"
            for dataset, columns, _rows in null_checks
            for column in columns
        }
        _total, null_per_table = profile_estimate(
            identifiers, include_blobs=include_blobs
        )
        for identifier, estimate in null_per_table.items():
            per_table[identifier] = per_table.get(identifier, 0.0) + estimate
    return sum(per_table.values()), per_table


def _combination_counts(
    probes: dict[str, TableProbe],
    combo_counts,
    identifier: str,
    combos: list[list[str]],
) -> dict[tuple[str, ...], int]:
    """``combos``' distinct counts from the table's batched statement when it
    ran, else from the adapter's own combination probe."""

    probe = probes.get(identifier)
    if probe is None:
        return combo_counts(identifier, combos)
    return {
        tuple(combo): probe.combinations[tuple(combo)]
        for combo in combos
        if tuple(combo) in probe.combinations
    }


def _grain_severity(row_count: int, min_rows: int) -> tuple[str, dict, str]:
    """A lost-uniqueness finding's severity, damped below ``min_rows`` (#280).

    A handful of rows is exactly the shape where losing uniqueness means the
    least: a 4-row table with a boolean column "loses" a uniqueness it never
    meaningfully had once a fifth row repeats a value. Damped to ``low``
    rather than dropped, so a real defect on a small table is still visible,
    just not the first thing a triager reads; the damping is named in the
    finding's own ``data`` and prose, so nothing about the run is silent.
    """

    if row_count < min_rows:
        return (
            "low",
            {"severity_floor_applied": True, "grain_min_rows": min_rows},
            f" (severity capped at low: fewer than {min_rows} rows)",
        )
    return "high", {}, ""


def grain_drift(
    adapter: Adapter,
    plan: GrainPlan,
    *,
    timeout_seconds: float = 30.0,
    min_rows: int = 100,
    probes: dict[str, TableProbe] | None = None,
) -> list[DriftFinding]:
    """Cardinality and identity drift, from aggregates only: exact distinct
    counts against the baseline's proven keys, the grains the project declares
    re-verified, and the overlap probes re-run against their baseline orphan
    fractions. Billed on metered connectors; the command layer holds the
    handshake.

    Two codes come out of the uniqueness checks and the difference is the
    baseline, not the SQL. ``key_lost_uniqueness`` needs a measured before: a key
    explore proved unique and no longer is. ``declared_grain_not_unique`` has no
    before at all, because the combination comes from the project rather than
    from a measurement, so the honest reading of a failure is that the
    declaration is false rather than that anything changed.

    ``min_rows`` damps a uniqueness-regression finding to ``low`` below that row
    count (see :func:`_grain_severity`); it does not apply to
    ``join_orphans_increased``, which already grades its own severity from the
    measured orphan fraction rather than asserting ``high`` unconditionally.

    ``probes`` holds what :func:`run_batched_probes` already measured, one
    statement per table; run here from the plan when omitted. A table it covers
    is read from it and every other table is probed separately, so the findings
    are the same either way and only the statement count differs.
    """

    if probes is None:
        probes = run_batched_probes(adapter, batched_probes(adapter, plan))
    findings: list[DriftFinding] = []
    for dataset, keys, row_count in plan.key_checks:
        probe = probes.get(dataset.identifier)
        distinct = (
            probe.distinct
            if probe is not None
            else adapter.exact_distinct_counts(dataset.identifier, keys)
        )
        # Re-read the metadata after the distinct scan: adapters whose
        # inventory row counts are planner estimates (Postgres reltuples)
        # upgrade to the exact COUNT(*) that scan carried, so an over-estimate
        # can never fabricate duplicates on a genuinely unique key. Free
        # everywhere (cached or trivially cheap on the other adapters).
        live_meta, _ = adapter.table_metadata(dataset.identifier)
        if live_meta.row_count is not None:
            row_count = live_meta.row_count
        if row_count is None:
            continue  # nothing counted the table, so uniqueness cannot be judged
        for key in keys:
            count = distinct.get(key)
            if count is None or count >= row_count:
                continue
            duplicates = row_count - count
            severity, floor_data, floor_note = _grain_severity(row_count, min_rows)
            findings.append(
                DriftFinding(
                    axis="grain",
                    code="key_lost_uniqueness",
                    identifier=dataset.identifier,
                    column=key,
                    severity=severity,
                    detail=(
                        f"{key} on {dataset.identifier} is no longer unique: "
                        f"{count} distinct over {row_count} rows "
                        f"(~{duplicates} duplicate rows); joins on it will fan out"
                        f"{floor_note}"
                    ),
                    data={
                        "distinct_count": count,
                        "row_count": row_count,
                        "was_grain": bool(dataset.grain and key in dataset.grain),
                        **floor_data,
                    },
                )
            )

    combo_counts = getattr(adapter, "distinct_combination_counts", None)
    for dataset, combos, row_count in plan.composite_checks:
        if combo_counts is None:
            break  # an adapter without combination probes cannot re-verify
        counts = _combination_counts(probes, combo_counts, dataset.identifier, combos)
        live_meta, _ = adapter.table_metadata(dataset.identifier)
        if live_meta.row_count is not None:
            row_count = live_meta.row_count
        if row_count is None:
            continue  # nothing counted the table, so uniqueness cannot be judged
        for combo in combos:
            count = counts.get(tuple(combo))
            if count is None or count >= row_count:
                continue
            duplicates = row_count - count
            members = ", ".join(combo)
            severity, floor_data, floor_note = _grain_severity(row_count, min_rows)
            findings.append(
                DriftFinding(
                    axis="grain",
                    code="key_lost_uniqueness",
                    identifier=dataset.identifier,
                    column=members,
                    severity=severity,
                    detail=(
                        f"({members}) on {dataset.identifier} is no longer "
                        f"unique: {count} distinct combinations over "
                        f"{row_count} rows (~{duplicates} duplicate rows); "
                        f"joins on it will fan out{floor_note}"
                    ),
                    data={
                        "columns": list(combo),
                        "distinct_count": count,
                        "row_count": row_count,
                        "was_grain": bool(
                            dataset.grain and list(combo) == list(dataset.grain)
                        ),
                        **floor_data,
                    },
                )
            )

    # The declared grains, reported under their own code. Nothing lapsed here:
    # measurement never proved these unique, so there is no before-and-after and
    # "no longer unique" would be a false account of the same numbers. What the
    # numbers say is that the project asserts a grain the data does not have.
    for dataset, combos, row_count in plan.declared_composite_checks:
        if combo_counts is None:
            break
        counts = _combination_counts(probes, combo_counts, dataset.identifier, combos)
        live_meta, _ = adapter.table_metadata(dataset.identifier)
        if live_meta.row_count is not None:
            row_count = live_meta.row_count
        if row_count is None:
            continue  # nothing counted the table, so uniqueness cannot be judged
        for combo in combos:
            count = counts.get(tuple(combo))
            if count is None or count >= row_count:
                continue
            duplicates = row_count - count
            members = ", ".join(combo)
            severity, floor_data, floor_note = _grain_severity(row_count, min_rows)
            findings.append(
                DriftFinding(
                    axis="grain",
                    code="declared_grain_not_unique",
                    identifier=dataset.identifier,
                    column=members,
                    severity=severity,
                    detail=(
                        f"the project declares ({members}) as the grain of "
                        f"{dataset.identifier}, and the combination is not "
                        f"unique: {count} distinct combinations over "
                        f"{row_count} rows (~{duplicates} duplicate rows). "
                        "Builds asserting this grain will fail and joins on it "
                        f"will fan out{floor_note}"
                    ),
                    data={
                        "columns": list(combo),
                        "distinct_count": count,
                        "row_count": row_count,
                        "declared": True,
                        **floor_data,
                    },
                )
            )

    for check in plan.join_cardinality_checks or []:
        if combo_counts is None:
            break
        count = _combination_counts(
            probes, combo_counts, check.identifier, [check.columns]
        ).get(tuple(check.columns))
        row_count = check.row_count
        live_meta, _ = adapter.table_metadata(check.identifier)
        if live_meta.row_count is not None:
            row_count = live_meta.row_count
        if count is None or count >= row_count:
            continue
        members = ", ".join(check.columns)
        other = check.model if check.side_model == check.to_model else check.to_model
        severity, floor_data, floor_note = _grain_severity(row_count, min_rows)
        findings.append(
            DriftFinding(
                axis="grain",
                code="join_cardinality_exceeded",
                identifier=check.identifier,
                column=members,
                severity=severity,
                detail=(
                    f"join '{check.relationship}' is declared {check.cardinality}, "
                    f"and ({members}) on {check.identifier} is not unique: "
                    f"{count} distinct over {row_count} rows "
                    f"(~{row_count - count} duplicate rows). Each '{other}' row can "
                    f"match several '{check.side_model}' rows, so anything "
                    f"aggregated across this join is counted more than once"
                    f"{floor_note}"
                ),
                data={
                    "relationship": check.relationship,
                    "declared_cardinality": check.cardinality,
                    "model": check.model,
                    "to_model": check.to_model,
                    "side_model": check.side_model,
                    "columns": list(check.columns),
                    "distinct_count": count,
                    "row_count": row_count,
                    **floor_data,
                },
            )
        )

    live = [copy for _baseline, copy in plan.fanout_pairs]
    rel_mod.verify_relationships(adapter, live, timeout_seconds=timeout_seconds)
    for baseline_rel, current_rel in plan.fanout_pairs:
        before = baseline_rel.orphan_fraction or 0.0
        after = current_rel.orphan_fraction
        if after is None or after <= before:
            continue
        findings.append(
            DriftFinding(
                axis="grain",
                code="join_orphans_increased",
                identifier=current_rel.from_dataset,
                column=current_rel.from_columns[0],
                severity="high" if after >= 0.2 else "medium",
                detail=(
                    f"{current_rel.from_dataset}.{current_rel.from_columns[0]} -> "
                    f"{current_rel.to_dataset}.{current_rel.to_columns[0]}: "
                    f"orphaned foreign keys {before:.1%} -> {after:.1%}"
                ),
                data={
                    "orphan_fraction_before": before,
                    "orphan_fraction_after": after,
                    "to_dataset": current_rel.to_dataset,
                },
            )
        )
    aggregate_columns = getattr(adapter, "column_aggregates", None)

    def no_rows(dataset: Dataset) -> DriftFinding:
        return DriftFinding(
            axis="grain",
            code="no_rows",
            identifier=dataset.identifier,
            severity="low",
            detail=(
                f"{dataset.identifier} has no rows; null fractions were not reported"
            ),
            data={"row_count": 0},
        )

    for dataset, columns, row_count in plan.null_checks or []:
        if not callable(aggregate_columns):
            continue
        if row_count == 0:
            findings.append(no_rows(dataset))
            continue
        probe = probes.get(dataset.identifier)
        aggregates = list(
            probe.aggregates
            if probe is not None and probe.aggregates is not None
            else aggregate_columns(dataset.identifier, columns)
        )
        if row_count is None:
            # No free count before the scan; the scan's own COUNT(*) is the
            # count, and adapters keep it for the rest of the command.
            row_count = adapter.table_metadata(dataset.identifier)[0].row_count
            if row_count == 0:
                findings.append(no_rows(dataset))
                continue
        joins = [
            {
                "from_dataset": relation.from_dataset,
                "from_columns": relation.from_columns,
                "to_dataset": relation.to_dataset,
                "to_columns": relation.to_columns,
            }
            for _baseline, relation in plan.fanout_pairs
            if relation.from_dataset == dataset.identifier
        ]
        for aggregate in aggregates:
            fraction = aggregate.null_fraction
            if fraction is None or fraction < 0.95:
                continue
            fully_null = fraction == 1.0
            finding_data = {"null_fraction": fraction, "row_count": row_count}
            if fully_null and joins:
                # A right-side projection that is NULL for every result row is
                # the observable form of a failed join.  Multiple joins remain
                # explicit rather than guessing which one supplied the column.
                finding_data["joins"] = joins
            findings.append(
                DriftFinding(
                    axis="grain",
                    code="fully_null_column" if fully_null else "mostly_null_column",
                    identifier=dataset.identifier,
                    column=aggregate.name,
                    severity="high" if fully_null else "low",
                    detail=(
                        f"{aggregate.name} on {dataset.identifier} is "
                        + ("100% NULL" if fully_null else f"{fraction:.1%} NULL")
                        + (
                            "; its joined relation may have matched nothing"
                            if fully_null and joins
                            else ""
                        )
                    ),
                    data=finding_data,
                )
            )
    return findings


def _matched_identifier(sm: SemanticModelDef, dataset_ids: list[str]) -> list[str]:
    """The live warehouse identifier(s) this semantic model resolves to, by
    whichever of ``model_ref``/``relation`` it states (#409): the same
    fallback :func:`semantic_free_drift` applies inline for its own column
    check, shared here so the relationship check resolves each endpoint the
    same way rather than by a second rule."""

    target = sm.model_ref or sm.relation
    return match_identifier(target, dataset_ids) if target else []


def semantic_free_drift(
    current_transform: TransformLayer | None,
    current_semantic: SemanticLayer | None,
    current_datasets: list[Dataset],
    snap: Snapshot,
) -> list[DriftFinding]:
    """The free half of the semantic axis: definition changes against the
    baseline, and references that no longer resolve. All of it is computed from
    the project and warehouse metadata; no scan, no spend."""

    findings: list[DriftFinding] = []
    baseline = snap.semantic_layer or SemanticLayer()
    current = current_semantic or SemanticLayer()

    # Which reader produced each side comes first, because it decides whether
    # the two sides can be compared definition by definition at all.
    provenance = _provenance_change(baseline, current)
    if provenance is not None:
        findings.append(provenance)
    vendor_changed = provenance is not None and provenance.code == (
        "semantic_vendor_changed"
    )
    hashes_comparable = provenance is None

    base_defs = {("semantic_model", d.name): d for d in baseline.semantic_models}
    base_defs.update({("metric", m.name): m for m in baseline.metrics})
    cur_defs = {("semantic_model", d.name): d for d in current.semantic_models}
    cur_defs.update({("metric", m.name): m for m in current.metrics})
    if vendor_changed:
        # Another vendor's names are another vocabulary: every definition on
        # both sides would read as removed and added, and none of it is drift.
        base_defs, cur_defs = {}, {}
    # Relationships join the same generic added/removed/changed diff below,
    # but only when both sides actually captured them (#409): a baseline
    # pinned before this field existed has none, and comparing that against a
    # populated current layer would report every real relationship as
    # freshly added rather than as a baseline gap.
    relationships_comparable = (
        baseline.relationships_and_keys_captured
        and current.relationships_and_keys_captured
    )
    if relationships_comparable:
        base_defs.update({("relationship", r.name): r for r in baseline.relationships})
        cur_defs.update({("relationship", r.name): r for r in current.relationships})

    for kind, name in sorted(cur_defs.keys() - base_defs.keys()):
        findings.append(
            DriftFinding(
                axis="semantic",
                code="definition_added",
                severity="low",
                detail=f"{kind.replace('_', ' ')} '{name}' is new since the baseline",
                data={"kind": kind, "name": name},
            )
        )
    for kind, name in sorted(base_defs.keys() - cur_defs.keys()):
        findings.append(
            DriftFinding(
                axis="semantic",
                code="definition_removed",
                severity="low",
                detail=f"{kind.replace('_', ' ')} '{name}' was removed since baseline",
                data={"kind": kind, "name": name},
            )
        )
    for key in sorted(base_defs.keys() & cur_defs.keys()):
        if not hashes_comparable:
            break
        if base_defs[key].content_sha256 != cur_defs[key].content_sha256:
            kind, name = key
            path = cur_defs[key].path
            findings.append(
                DriftFinding(
                    axis="semantic",
                    code="definition_changed",
                    severity="low",
                    detail=(
                        f"the definition of {kind.replace('_', ' ')} '{name}' "
                        "changed since the baseline"
                    ),
                    # `path` omitted rather than null where the definition has no
                    # file; `kind` and `name` already identify it.
                    data={"kind": kind, "name": name}
                    | ({"path": path} if path else {}),
                )
            )

    model_names = set(current_transform.models) if current_transform else set()
    dataset_ids = [d.identifier for d in current_datasets]
    columns_by_id = {
        d.identifier: {c.name for c in d.columns} for d in current_datasets
    }
    all_measures = {name for sm in current.semantic_models for name in sm.measures}
    all_metrics = {m.name for m in current.metrics}

    for sm in current.semantic_models:
        if (
            sm.model_ref
            and current_transform is not None
            and sm.model_ref not in model_names
        ):
            findings.append(
                DriftFinding(
                    axis="semantic",
                    code="dangling_reference",
                    severity="high",
                    detail=(
                        f"semantic model '{sm.name}' references model "
                        f"'{sm.model_ref}', which is not in the project"
                    ),
                    data={"semantic_model": sm.name, "missing_model": sm.model_ref},
                    impacted_metrics=_metrics_from_measures(current, set(sm.measures)),
                )
            )
            continue
        # A model whose semantic layer states its own physical relation
        # directly (#409: `relation`, populated by a format with no build
        # step between a semantic model and a warehouse relation) is matched
        # against the warehouse the same way `model_ref` is, once a model_ref
        # is absent to try first: the two name the same kind of thing to this
        # comparison, a physical relation this semantic model sits on.
        match_target = sm.model_ref or sm.relation
        matches = match_identifier(match_target, dataset_ids) if match_target else []
        if len(matches) != 1:
            continue  # not built (or ambiguous): nothing to check columns against
        available = columns_by_id[matches[0]]
        for role, mapping in (
            ("entity", sm.entities),
            ("dimension", sm.dimensions),
            ("measure", sm.measures),
        ):
            for name, column in mapping.items():
                if column is None or column in available:
                    continue
                impacted = (
                    set(sm.measures)
                    if role != "measure"
                    else {m for m, c in sm.measures.items() if c == column}
                )
                findings.append(
                    DriftFinding(
                        axis="semantic",
                        code="dangling_reference",
                        identifier=matches[0],
                        column=column,
                        severity="high",
                        detail=(
                            f"{role} '{name}' on semantic model '{sm.name}' "
                            f"references column '{column}', which is gone from "
                            f"{matches[0]}"
                        ),
                        data={"semantic_model": sm.name, "role": role, "name": name},
                        impacted_models=[sm.model_ref] if sm.model_ref else [],
                        impacted_metrics=_metrics_from_measures(current, impacted),
                    )
                )
        if not current.relationships_and_keys_captured:
            continue
        for columns in sm.keys:
            missing = [c for c in columns if c not in available]
            if not missing:
                continue
            findings.append(
                DriftFinding(
                    axis="semantic",
                    code="dangling_reference",
                    identifier=matches[0],
                    severity="high",
                    detail=(
                        f"a declared key on semantic model '{sm.name}' names "
                        f"column(s) {', '.join(missing)}, which "
                        f"{'is' if len(missing) == 1 else 'are'} gone from "
                        f"{matches[0]}"
                    ),
                    data={"semantic_model": sm.name, "role": "key", "columns": columns},
                )
            )

    if current.relationships_and_keys_captured:
        models_by_name = {sm.name: sm for sm in current.semantic_models}
        for rel in current.relationships:
            left = models_by_name.get(rel.model)
            right = models_by_name.get(rel.to_model)
            if left is None or right is None:
                missing_side = rel.model if left is None else rel.to_model
                findings.append(
                    DriftFinding(
                        axis="semantic",
                        code="broken_relationship",
                        severity="high",
                        detail=(
                            f"relationship '{rel.name}' names '{missing_side}', "
                            "which is no longer a semantic model"
                        ),
                        data={"relationship": rel.name, "missing_model": missing_side},
                    )
                )
                continue
            left_matches = _matched_identifier(left, dataset_ids)
            right_matches = _matched_identifier(right, dataset_ids)
            if len(left_matches) != 1 or len(right_matches) != 1:
                continue  # not built (or ambiguous): nothing to check columns against
            left_available = columns_by_id[left_matches[0]]
            right_available = columns_by_id[right_matches[0]]
            broken = [
                [frm, to]
                for frm, to in rel.column_pairs
                if frm not in left_available or to not in right_available
            ]
            if broken:
                findings.append(
                    DriftFinding(
                        axis="semantic",
                        code="broken_relationship",
                        identifier=left_matches[0],
                        severity="high",
                        detail=(
                            f"relationship '{rel.name}' from '{rel.model}' to "
                            f"'{rel.to_model}' names column pair(s) that no "
                            f"longer resolve: {broken}"
                        ),
                        data={"relationship": rel.name, "missing_pairs": broken},
                    )
                )

    for metric in current.metrics:
        findings.extend(
            DriftFinding(
                axis="semantic",
                code="dangling_reference",
                severity="high",
                detail=(
                    f"metric '{metric.name}' references measure "
                    f"'{measure}', which no longer exists"
                ),
                data={"metric": metric.name, "missing_measure": measure},
                impacted_metrics=[metric.name],
            )
            for measure in metric.input_measures
            if measure not in all_measures
        )
        # A create_metric measure is addressable as a metric, so measures count
        # as valid metric references here.
        findings.extend(
            DriftFinding(
                axis="semantic",
                code="dangling_reference",
                severity="high",
                detail=(
                    f"metric '{metric.name}' references metric "
                    f"'{input_metric}', which no longer exists"
                ),
                data={"metric": metric.name, "missing_metric": input_metric},
                impacted_metrics=[metric.name],
            )
            for input_metric in metric.input_metrics
            if input_metric not in all_metrics | all_measures
        )
    return findings


def _provenance_change(
    baseline: SemanticLayer, current: SemanticLayer
) -> DriftFinding | None:
    """A change in the reader behind the fingerprint, reported as itself.

    A content hash is a hash of what one reader produced from a file, so a
    different vendor, a different vendor release, or a different model format
    version changes hashes nobody's edit changed. Reported once, ahead of the
    definition diff, so an upgrade reads as an upgrade rather than as every
    model in the layer redefined. Only fields both sides recorded are compared:
    a baseline pinned before they existed records none, and an unrecorded side
    is not a change.
    """

    def differs(field: str) -> tuple[str, str] | None:
        before, after = getattr(baseline, field), getattr(current, field)
        if before is None or after is None or before == after:
            return None
        return before, after

    vendor = differs("vendor")
    if vendor is not None:
        return DriftFinding(
            axis="semantic",
            code="semantic_vendor_changed",
            severity="medium",
            detail=(
                f"the baseline's semantic layer was read as {vendor[0]} and the "
                f"current one as {vendor[1]}, so no definition was compared: "
                "the two name their definitions in different vocabularies. "
                "Re-run `maintain snapshot` once the move is the state you vouch "
                "for"
            ),
            data={"vendor_before": vendor[0], "vendor_after": vendor[1]},
        )
    changes = {
        field: change
        for field in ("vendor_release", "format_version")
        if (change := differs(field)) is not None
    }
    if not changes:
        return None
    described = "; ".join(
        f"{field.replace('_', ' ')} {before} -> {after}"
        for field, (before, after) in changes.items()
    )
    data: dict = {"vendor": current.vendor}
    for field, (before, after) in changes.items():
        data[f"{field}_before"] = before
        data[f"{field}_after"] = after
    return DriftFinding(
        axis="semantic",
        code="semantic_format_changed",
        severity="medium",
        detail=(
            f"the {current.vendor or 'semantic'} layer was fingerprinted under a "
            f"different release or model format than the baseline ({described}), "
            "so definitions were not compared by content: an upgrade changes "
            "every hash without changing a definition. Added and removed "
            "definitions are still reported. Re-run `maintain snapshot` to "
            "baseline the upgraded format"
        ),
        data=data,
    )


class CardinalityCheck(NamedTuple):
    identifier: str
    column: str
    dimension: str
    semantic_model: str
    baseline_distinct: int
    baseline_exact: bool


def cardinality_plan(
    current_semantic: SemanticLayer | None,
    snap: Snapshot,
    scope: set[str] | None = None,
) -> list[CardinalityCheck]:
    """Which categorical dimension columns have a cardinality baseline to diff:
    the current semantic definitions intersected with the snapshot's distinct
    counts. Only counts are ever compared; no dimension value is read.

    ``scope`` (lowered name tokens) filters *before* estimation/execution, so a
    narrowed scan is priced and billed for less, not just reported as less: a
    check matches when its identifier, bare table name, column, dimension, or
    semantic model name is in scope -- the same vocabulary ``_semantic_scope``
    already matches findings against, since a semantic finding hangs off a
    definition name as often as a physical object.
    """

    checks: list[CardinalityCheck] = []
    if current_semantic is None:
        return checks
    snap_by_id = {d.identifier: d for d in snap.warehouse.datasets}
    identifiers = list(snap_by_id)
    for sm in current_semantic.semantic_models:
        if not sm.model_ref:
            continue
        matches = match_identifier(sm.model_ref, identifiers)
        if len(matches) != 1:
            continue
        identifier = matches[0]
        columns = {c.name: c for c in snap_by_id[identifier].columns}
        for dimension, column in sm.categorical_dimensions.items():
            profile = columns.get(column)
            if profile is None or profile.distinct_count is None:
                continue
            if scope is not None and not _cardinality_in_scope(
                identifier, column, dimension, sm.name, scope
            ):
                continue
            checks.append(
                CardinalityCheck(
                    identifier=identifier,
                    column=column,
                    dimension=dimension,
                    semantic_model=sm.name,
                    baseline_distinct=profile.distinct_count,
                    baseline_exact=profile.distinct_count_exact,
                )
            )
    return checks


def _cardinality_in_scope(
    identifier: str, column: str, dimension: str, semantic_model: str, scope: set[str]
) -> bool:
    candidates = {
        identifier.lower(),
        identifier.rsplit(".", 1)[-1].lower(),
        column.lower(),
        dimension.lower(),
        semantic_model.lower(),
    }
    return bool(candidates & scope)


def cardinality_estimate(
    adapter: Adapter,
    checks: list[CardinalityCheck],
    batched: dict[str, ProbeNeeds] | None = None,
) -> tuple[float, dict[str, float]]:
    """Free dry-run pricing of the dimension-cardinality scan. A table in
    ``batched`` (see :func:`batched_probes`) is priced by
    :func:`grain_estimate` as part of its one statement, so it is left out."""

    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not checks:
        return 0.0, {}
    per_table: dict[str, float] = {}
    for identifier, columns in _checks_by_table(checks).items():
        if batched and identifier in batched:
            continue
        per_table[identifier] = query_estimate(
            _distinct_count_sql(identifier, columns, adapter.dialect)
        )
    return sum(per_table.values()), per_table


def cardinality_drift(
    adapter: Adapter,
    checks: list[CardinalityCheck],
    current_semantic: SemanticLayer | None,
    probes: dict[str, TableProbe] | None = None,
) -> list[DriftFinding]:
    """The billed half of the semantic axis: exact distinct counts on the
    categorical dimension columns versus the baseline. A moved count means the
    dimension widened or narrowed underneath its metrics; naming the new value
    is deliberately left to a firewalled `explore query` if the user asks.

    ``probes`` is what ``maintain check`` already measured in each table's one
    batched statement (see :func:`batched_probes`), read in place of a
    separate distinct count where present."""

    findings: list[DriftFinding] = []
    if not checks:
        return findings
    semantic = current_semantic or SemanticLayer()
    models_by_name = {sm.name: sm for sm in semantic.semantic_models}

    for identifier, columns in _live_cardinality_columns(adapter, checks).items():
        live = [
            check
            for check in checks
            if check.identifier == identifier and check.column in columns
        ]
        probe = (probes or {}).get(identifier)
        counts = (
            probe.distinct
            if probe is not None
            else adapter.exact_distinct_counts(identifier, columns)
        )
        for check in live:
            after = counts.get(check.column)
            if after is None or after == check.baseline_distinct:
                continue
            # An approximate baseline wobbling within its sketch error is noise:
            # the current count is exact, but the historical one was not, so a
            # small delta is indistinguishable from HLL variance and would be a
            # phantom finding. A delta beyond the band still fires (its direction
            # and rough size are real), keeping the ~ marker and exact: false.
            if not check.baseline_exact:
                # The band scales with the baseline (the sketch's error is
                # relative), with no floor: at a handful of distinct values it is
                # zero, so a genuine new category still fires; at thousands it is
                # tens, absorbing HLL wobble.
                band = round(_APPROX_CARDINALITY_TOLERANCE * check.baseline_distinct)
                if abs(after - check.baseline_distinct) <= band:
                    continue
            marker = "" if check.baseline_exact else "~"
            direction = "widened" if after > check.baseline_distinct else "narrowed"
            sm = models_by_name.get(check.semantic_model)
            findings.append(
                DriftFinding(
                    axis="semantic",
                    code="dimension_cardinality_changed",
                    identifier=identifier,
                    column=check.column,
                    severity="medium",
                    detail=(
                        f"dimension '{check.dimension}' on semantic model "
                        f"'{check.semantic_model}' {direction}: "
                        f"{marker}{check.baseline_distinct} -> {after} distinct "
                        f"values in {identifier}.{check.column}"
                    ),
                    exact=check.baseline_exact,
                    data={
                        "distinct_before": check.baseline_distinct,
                        "distinct_after": after,
                        "dimension": check.dimension,
                        "semantic_model": check.semantic_model,
                    },
                    impacted_models=[sm.model_ref] if sm and sm.model_ref else [],
                    impacted_metrics=_metrics_from_measures(
                        semantic, set(sm.measures) if sm else set()
                    ),
                )
            )
    return findings


def annotate_impacts(findings: list[DriftFinding], snap: Snapshot) -> None:
    """Trace warehouse-level findings to the models and metrics they land on.

    Impact flows source -> model (via recorded ``source()`` calls, closed over
    ``ref()`` dependents) and model -> semantic model -> metric (via the
    snapshot's semantic references). Column-level findings impact a semantic
    model only when it references that column; table-level findings impact
    every definition on the table.
    """

    transform = snap.transform_layer
    semantic = snap.semantic_layer
    if transform is None:
        return
    by_table = _models_by_table(transform)

    for finding in findings:
        if finding.identifier is None or finding.code == "dangling_source":
            continue
        table = finding.identifier.rsplit(".", 1)[-1].lower()
        models = sorted(by_table.get(table, set()))
        finding.impacted_models = models
        if semantic is None or not models:
            continue
        model_set = set(models)
        impacted_measures: set[str] = set()
        for sm in semantic.semantic_models:
            if sm.model_ref not in model_set:
                continue
            if finding.column is None or finding.column in sm.structural_columns():
                # A table-level change, or a hit on an entity/dimension column,
                # puts the whole semantic model (so all its measures) at risk.
                impacted_measures.update(sm.measures)
            else:
                impacted_measures.update(
                    name
                    for name, column in sm.measures.items()
                    if column == finding.column
                )
        finding.impacted_metrics = _metrics_from_measures(semantic, impacted_measures)


def rank_findings(findings: list[DriftFinding]) -> list[DriftFinding]:
    """Blast-radius order: severity first, then how much of the project a
    finding touches, then a stable name order so reports diff cleanly."""

    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, len(_SEVERITY_ORDER)),
            -(len(f.impacted_models) + len(f.impacted_metrics)),
            f.axis,
            f.identifier or "",
            f.column or "",
        ),
    )


# --- helpers -----------------------------------------------------------------


def _live_cardinality_columns(
    adapter: Adapter, checks: list[CardinalityCheck]
) -> dict[str, list[str]]:
    """Per table that still exists, the checked columns it still has, sorted:
    exactly what :func:`cardinality_drift` counts. A vanished table or column
    is the schema axis's story."""

    if not checks:
        return {}
    current = {meta.identifier for meta in adapter.list_objects()}
    live: dict[str, list[str]] = {}
    for identifier in _checks_by_table(checks):
        if identifier not in current:
            continue
        _meta, live_columns = adapter.table_metadata(identifier)
        live_names = {c.name for c in live_columns}
        live[identifier] = sorted(
            {
                check.column
                for check in checks
                if check.identifier == identifier and check.column in live_names
            }
        )
    return live


def _checks_by_table(checks: list[CardinalityCheck]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for check in checks:
        columns = grouped.setdefault(check.identifier, [])
        if check.column not in columns:
            columns.append(check.column)
    return grouped


def _distinct_count_sql(identifier: str, columns: list[str], dialect: str) -> str:
    """The estimation stand-in for ``adapter.exact_distinct_counts``: the same
    columns over the same table, so a dry-run prices what the confirmed run
    scans. Never executed by dex; only dry-run."""

    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    table = ".".join(quote(part) for part in identifier.split("."))
    selects = ", ".join(
        f"COUNT(DISTINCT {quote(column)}) AS d_{i}" for i, column in enumerate(columns)
    )
    sql = f"SELECT {selects} FROM {table}"  # noqa: S608
    if dialect == "duckdb":
        return sql
    import sqlglot

    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


def _distinct_combination_stand_in(
    identifier: str, combinations: list[list[str]], dialect: str
) -> str:
    """The estimation stand-in for ``adapter.distinct_combination_counts``:
    the same combinations over the same table, so a dry-run prices what the
    confirmed run scans. Never executed by dex; only dry-run."""

    def quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    table = ".".join(quote(part) for part in identifier.split("."))
    sql = distinct_combination_sql(table, combinations, quote)
    if dialect == "duckdb":
        return sql
    import sqlglot

    return sqlglot.transpile(sql, read="duckdb", write=dialect)[0]


def _built_tables(transform: TransformLayer | None) -> set[str]:
    """Table names (lowered) the live project builds: its models only.

    Unlike :func:`_backed_tables` this leaves out declared ``source()`` tables,
    so a new table a ``sources:`` block already names is still reported."""

    if transform is None:
        return set()
    return {model.lower() for model in transform.models}


def _built_by_project(identifier: str, built: set[str]) -> bool:
    return identifier.rsplit(".", 1)[-1].lower() in built


def built_relation_warnings(
    current: list[Dataset],
    snap: Snapshot,
    scope: set[str] | None = None,
    current_transform: TransformLayer | None = None,
) -> list[str]:
    """Name the new relations :func:`schema_drift` did not report as drift.

    On DuckDB the dev target is the source file, so ``transform build --target
    dev`` writes the project's own models into the warehouse dex reads. Those
    are not new source tables. Skipping them silently would read as a clean
    sweep, so the count is named."""

    built = _built_tables(current_transform)
    if not built:
        return []
    baseline_ids = {d.identifier for d in snap.warehouse.datasets}
    skipped = sum(
        1
        for d in current
        if d.identifier not in baseline_ids
        and (scope is None or d.identifier in scope)
        and _built_by_project(d.identifier, built)
    )
    if not skipped:
        return []
    return [
        f"{skipped} relation(s) built by this project were not reported as "
        "source drift (table_added)"
    ]


def _backed_tables(transform: TransformLayer | None) -> set[str]:
    """Table-name suffixes (lowered) this transform layer claims: a model's
    own name (it *is* the relation), or a declared ``source()`` table. Reads
    ``.sources`` directly rather than going through ``_models_by_table``'s
    regex-derived ``model_sources``, so a declared source with no model
    currently calling ``source()`` on it still counts as known to the
    project (the same data ``dangling_source`` already reads)."""

    if transform is None:
        return set()
    names = {model.lower() for model in transform.models}
    names.update(source.table.lower() for source in transform.sources)
    return names


def _models_by_table(transform: TransformLayer) -> dict[str, set[str]]:
    """Warehouse table name (lowered) -> every model built on it.

    A model is on a table when it sources it, when it *is* it (the built
    relation carries the model's name), or when it refs a model that is,
    transitively.
    """

    dependents: dict[str, set[str]] = {}
    for model, refs in transform.model_refs.items():
        for ref in refs:
            dependents.setdefault(ref, set()).add(model)

    def closure(seed: set[str]) -> set[str]:
        seen = set(seed)
        stack = list(seed)
        while stack:
            model = stack.pop()
            for dependent in dependents.get(model, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    stack.append(dependent)
        return seen

    by_table: dict[str, set[str]] = {}
    for model, sources in transform.model_sources.items():
        for source in sources:
            table = source.split(".", 1)[1].lower()
            by_table.setdefault(table, set()).add(model)
    for model in transform.models:
        by_table.setdefault(model.lower(), set()).add(model)
    return {table: closure(models) for table, models in by_table.items()}


def _metrics_from_measures(semantic, measures: set[str]) -> list[str]:
    """Metrics drawing on the given measures, closed over metric-on-metric
    references (ratio and derived metrics)."""

    if not measures:
        return []
    impacted = {
        metric.name
        for metric in semantic.metrics
        if set(metric.input_measures) & measures
    }
    grew = True
    while grew:
        grew = False
        for metric in semantic.metrics:
            if metric.name not in impacted and set(metric.input_metrics) & impacted:
                impacted.add(metric.name)
                grew = True
    return sorted(impacted)
