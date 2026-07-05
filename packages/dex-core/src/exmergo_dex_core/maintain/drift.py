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

from ..adapters.base import Adapter
from ..cache import Dataset, Relationship, match_identifier
from ..explore import relationships as rel_mod
from .snapshot import SemanticLayer, Snapshot, TransformLayer

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
    current: list[Dataset], snap: Snapshot, scope: set[str] | None = None
) -> list[DriftFinding]:
    """Structural drift: metadata only, free on every connector."""

    baseline = {d.identifier: d for d in snap.warehouse.datasets}
    now = {d.identifier: d for d in current}
    findings: list[DriftFinding] = []

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
        if scoped(identifier)
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

    for identifier in sorted(baseline.keys() & now.keys()):
        if not scoped(identifier):
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
                        data={"declared_in": source.path},
                    )
                )
    return findings


def volume_drift(
    current: list[Dataset], snap: Snapshot, scope: set[str] | None = None
) -> list[DriftFinding]:
    """Freshness drift from free metadata: row counts (and byte sizes) that
    moved beyond load chatter. Structure unchanged, keys intact, but the data
    stopped flowing correctly: the axis the other three cannot see."""

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

    key_checks: list[tuple[Dataset, list[str], int]]
    fanout_pairs: list[tuple[Relationship, Relationship]]


def grain_plan(
    adapter: Adapter, snap: Snapshot, scope: set[str] | None = None
) -> GrainPlan:
    described: dict[str, tuple[set[str], int | None]] = {}
    current = {meta.identifier for meta in adapter.list_objects()}

    def describe(identifier: str) -> tuple[set[str], int | None]:
        if identifier not in described:
            meta, columns = adapter.table_metadata(identifier)
            described[identifier] = ({c.name for c in columns}, meta.row_count)
        return described[identifier]

    key_checks: list[tuple[Dataset, list[str], int]] = []
    for dataset in snap.warehouse.datasets:
        if scope is not None and dataset.identifier not in scope:
            continue
        if dataset.identifier not in current:
            continue  # disappearance is the schema axis's story
        keys = sorted(
            {key[0] for key in dataset.candidate_keys if len(key) == 1}
            | set(dataset.grain or [])
        )
        if not keys:
            continue
        columns, row_count = describe(dataset.identifier)
        live_keys = [k for k in keys if k in columns]
        if live_keys and row_count:
            key_checks.append((dataset, live_keys, row_count))

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
    return GrainPlan(key_checks, fanout_pairs)


def grain_estimate(adapter: Adapter, plan: GrainPlan) -> tuple[float, dict[str, float]]:
    """Free dry-run pricing of the plan's scans; zero on free adapters."""

    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None:
        return 0.0, {}
    per_table: dict[str, float] = {}
    for dataset, keys, _row_count in plan.key_checks:
        per_table[dataset.identifier] = query_estimate(
            _distinct_count_sql(dataset.identifier, keys, adapter.dialect)
        )
    probes = rel_mod.probe_statements(
        [live for _baseline, live in plan.fanout_pairs], adapter.dialect
    )
    if probes:
        per_table["(join overlap probes)"] = sum(query_estimate(sql) for sql in probes)
    return sum(per_table.values()), per_table


def grain_drift(
    adapter: Adapter, plan: GrainPlan, *, timeout_seconds: float = 30.0
) -> list[DriftFinding]:
    """Cardinality and identity drift, from aggregates only: exact distinct
    counts against the baseline's proven keys, and the overlap probes re-run
    against their baseline orphan fractions. Billed on metered connectors; the
    command layer holds the handshake."""

    findings: list[DriftFinding] = []
    for dataset, keys, row_count in plan.key_checks:
        distinct = adapter.exact_distinct_counts(dataset.identifier, keys)
        for key in keys:
            count = distinct.get(key)
            if count is None or count >= row_count:
                continue
            duplicates = row_count - count
            findings.append(
                DriftFinding(
                    axis="grain",
                    code="key_lost_uniqueness",
                    identifier=dataset.identifier,
                    column=key,
                    severity="high",
                    detail=(
                        f"{key} on {dataset.identifier} is no longer unique: "
                        f"{count} distinct over {row_count} rows "
                        f"(~{duplicates} duplicate rows); joins on it will fan out"
                    ),
                    data={
                        "distinct_count": count,
                        "row_count": row_count,
                        "was_grain": bool(dataset.grain and key in dataset.grain),
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
    return findings


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

    base_defs = {("semantic_model", d.name): d for d in baseline.semantic_models}
    base_defs.update({("metric", m.name): m for m in baseline.metrics})
    cur_defs = {("semantic_model", d.name): d for d in current.semantic_models}
    cur_defs.update({("metric", m.name): m for m in current.metrics})

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
        if base_defs[key].content_sha256 != cur_defs[key].content_sha256:
            kind, name = key
            findings.append(
                DriftFinding(
                    axis="semantic",
                    code="definition_changed",
                    severity="low",
                    detail=(
                        f"the definition of {kind.replace('_', ' ')} '{name}' "
                        "changed since the baseline"
                    ),
                    data={"kind": kind, "name": name, "path": cur_defs[key].path},
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
        matches = match_identifier(sm.model_ref, dataset_ids) if sm.model_ref else []
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
                        impacted_models=[sm.model_ref],
                        impacted_metrics=_metrics_from_measures(current, impacted),
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


class CardinalityCheck(NamedTuple):
    identifier: str
    column: str
    dimension: str
    semantic_model: str
    baseline_distinct: int
    baseline_exact: bool


def cardinality_plan(
    current_semantic: SemanticLayer | None, snap: Snapshot
) -> list[CardinalityCheck]:
    """Which categorical dimension columns have a cardinality baseline to diff:
    the current semantic definitions intersected with the snapshot's distinct
    counts. Only counts are ever compared; no dimension value is read."""

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
        columns = {c.name: c for c in snap_by_id[matches[0]].columns}
        for dimension, column in sm.categorical_dimensions.items():
            profile = columns.get(column)
            if profile is None or profile.distinct_count is None:
                continue
            checks.append(
                CardinalityCheck(
                    identifier=matches[0],
                    column=column,
                    dimension=dimension,
                    semantic_model=sm.name,
                    baseline_distinct=profile.distinct_count,
                    baseline_exact=profile.distinct_count_exact,
                )
            )
    return checks


def cardinality_estimate(
    adapter: Adapter, checks: list[CardinalityCheck]
) -> tuple[float, dict[str, float]]:
    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not checks:
        return 0.0, {}
    per_table: dict[str, float] = {}
    for identifier, columns in _checks_by_table(checks).items():
        per_table[identifier] = query_estimate(
            _distinct_count_sql(identifier, columns, adapter.dialect)
        )
    return sum(per_table.values()), per_table


def cardinality_drift(
    adapter: Adapter,
    checks: list[CardinalityCheck],
    current_semantic: SemanticLayer | None,
) -> list[DriftFinding]:
    """The billed half of the semantic axis: exact distinct counts on the
    categorical dimension columns versus the baseline. A moved count means the
    dimension widened or narrowed underneath its metrics; naming the new value
    is deliberately left to a firewalled `explore query` if the user asks."""

    findings: list[DriftFinding] = []
    if not checks:
        return findings
    current = {meta.identifier for meta in adapter.list_objects()}
    semantic = current_semantic or SemanticLayer()
    models_by_name = {sm.name: sm for sm in semantic.semantic_models}

    for identifier in _checks_by_table(checks):
        if identifier not in current:
            continue
        _meta, live_columns = adapter.table_metadata(identifier)
        live_names = {c.name for c in live_columns}
        live = [
            check
            for check in checks
            if check.identifier == identifier and check.column in live_names
        ]
        counts = adapter.exact_distinct_counts(
            identifier, sorted({check.column for check in live})
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
