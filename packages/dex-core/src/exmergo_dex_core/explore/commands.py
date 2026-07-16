"""Explore command orchestrators.

Each ``cmd_*`` opens the adapter, drives the explore engine, and shapes the result
into the sanitized envelope. Keeping this here (not in ``cli.py``) keeps dispatch
thin and keeps ``map``'s composition (it runs inventory, profile, and relationships
together) out of the CLI layer. These are the only explore commands that hold an
adapter; ``map``, ``profile``, and ``relationships`` all persist what they learned,
and only to the ``.dex/`` cache, so a scan is never paid for twice.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .. import command_args, dbt_project
from .. import envelope as env
from ..adapters import get_dialect
from ..adapters.base import Adapter, ObjectMeta, QueryResult
from ..cache import Dataset, DexCache, DexStore, Relationship, match_identifier
from ..config import DexConfig, QueryLimits, load_config
from ..guards.query_firewall import (
    InspectedQuery,
    QueryRefusedError,
    inspect_query,
)
from ..progress import ProgressReporter
from . import cluster as cluster_mod
from . import inventory as inventory_mod
from . import profile as profile_mod
from . import rank as rank_mod
from . import relationships as rel_mod

# Below this many objects, profile everything: enumeration is cheap and complete.
# Above it, profile only the top-ranked unless --full is passed.
_AUTO_PROFILE_ALL = 50


def _profile_estimate(
    adapter: Adapter, identifiers: list[str]
) -> tuple[float, dict[str, float]]:
    estimate = getattr(adapter, "profile_estimate", None)
    if estimate is None:
        return 0.0, {}
    return estimate(identifiers)


def _reporter(total: int, label: str, noun: str) -> ProgressReporter:
    """A stderr progress reporter for one long explore loop.

    Constructed at the call site after the billed handshake's early return, so an
    unconfirmed preflight never even builds one. Construction emits nothing (no
    "starting..." line), so a 0/1-object run stays silent by the reporter's own
    gating.
    """

    return ProgressReporter(total, label, noun)


def _dev_schemas(config: DexConfig) -> frozenset[str]:
    """Dev/replica namespaces declared per connector (where dbt dev builds write)."""
    return frozenset(
        name
        for name in [
            config.bigquery.dev_dataset if config.bigquery else None,
            config.snowflake.dev_schema if config.snowflake else None,
            config.databricks.dev_schema if config.databricks else None,
            config.postgres.dev_schema if config.postgres else None,
            config.redshift.dev_schema if config.redshift else None,
        ]
        if name
    )


def cmd_inventory(args: argparse.Namespace) -> env.Envelope:
    adapter = command_args.open_from_args(args)
    try:
        # Inventory is metadata-only on every connector (free API calls on
        # BigQuery), so it never needs the confirm handshake.
        metas = inventory_mod.inventory(adapter)
        gate = command_args.cost_gate(adapter)
        cost = gate.cost() if gate is not None else env.Cost()
    finally:
        adapter.close()

    ranked = getattr(args, "rank", False)
    if ranked:
        # Honor the same configured ranking_hints as `map`; without them, an
        # inventory --rank would silently ignore the user's bias. Connectivity is
        # absent here by design (no relationship pass), so only naming/size/shape
        # signals contribute.
        config = load_config(command_args.repo_root(args)) or DexConfig()
        scores = rank_mod.rank(metas, None, config.ranking_hints)
        metas = sorted(metas, key=lambda m: scores.get(m.identifier, 0.0), reverse=True)
    else:
        scores = {}

    objects = [
        {
            "identifier": m.identifier,
            "object_type": m.object_type,
            "row_estimate": m.row_count,
            "column_count": m.column_count,
            "rank_score": scores.get(m.identifier) if ranked else None,
        }
        for m in metas
    ]
    return env.ok(
        {"object_count": len(objects), "objects": objects, "ranked": ranked},
        cost=cost,
    )


def cmd_profile(args: argparse.Namespace) -> env.Envelope:
    repo_root = command_args.repo_root(args)
    config = load_config(repo_root) or DexConfig()
    defs = _project_definitions(args, config)
    adapter = command_args.open_from_args(args)
    try:
        identifiers = _resolve_identifiers(adapter, args.objects)
        estimate, per_table = _profile_estimate(adapter, identifiers)
        unconfirmed = command_args.billed_handshake(
            "explore profile", adapter, estimate, per_table=per_table
        )
        if unconfirmed is not None:
            return unconfirmed
        reporter = _reporter(len(identifiers), "profiled", "objects")
        datasets = profile_mod.profile(adapter, identifiers, progress=reporter)
        reporter.done()
        connector = adapter.name
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()
    _annotate_grain(datasets, defs)

    # Persist what the scan already paid for: after profiling a table, `explore
    # query` on that table must work without a second warehouse scan (the query
    # firewall's own refusal messages promise exactly this). Prior relationships
    # are preserved because profile runs no inference pass.
    store = DexStore(repo_root)
    now = datetime.now(UTC)
    cache, stats = _merge_profiles(store.load_cache(), datasets, connector, now)
    path = store.save_cache(cache, now=now)

    envelope.data.update(
        {
            "datasets": [d.model_dump(mode="json") for d in datasets],
            "cache_path": str(path),
            "updated_at": now.isoformat(),
            "notes": [_persist_note(stats, len(datasets), keeps_relationships=True)],
        }
    )
    return envelope


def cmd_relationships(args: argparse.Namespace) -> env.Envelope:
    verify = getattr(args, "verify", False)
    repo_root = command_args.repo_root(args)
    config = load_config(repo_root) or DexConfig()
    defs = _project_definitions(args, config)

    adapter = command_args.open_from_args(args)
    try:
        # Relationship inference needs uniqueness signals, so profile every object
        # first (free and local on DuckDB), then infer across the full set.
        metas = inventory_mod.inventory(adapter)
        identifiers = [m.identifier for m in metas]
        estimate, per_table = _profile_estimate(adapter, identifiers)
        handshake_notes = [
            "relationship inference profiles every object; on a metered "
            "connector `explore map` (top-ranked objects only) is usually the "
            "cheaper way in"
        ]
        if verify:
            handshake_notes.append(
                "--verify overlap probes depend on what inference finds, so "
                "they are billed within the confirmed budget, not in this "
                "upfront estimate"
            )
        unconfirmed = command_args.billed_handshake(
            "explore relationships",
            adapter,
            estimate,
            per_table=per_table,
            notes=handshake_notes,
        )
        if unconfirmed is not None:
            return unconfirmed
        profile_reporter = _reporter(len(identifiers), "profiled", "objects")
        datasets = profile_mod.profile(adapter, identifiers, progress=profile_reporter)
        profile_reporter.done()
        connector = adapter.name
        inferred = rel_mod.infer_relationships(datasets)
        if verify:
            verify_reporter = _reporter(len(inferred), "verified", "joins")
            rel_mod.verify_relationships(
                adapter,
                inferred,
                timeout_seconds=config.query.timeout_seconds,
                progress=verify_reporter,
            )
            verify_reporter.done()
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()
    # Annotate before persisting so cached datasets carry candidate_keys and
    # grain, the same shape a `map`-written cache has.
    _annotate_grain(datasets, defs)

    # Fold same-lineage duplicates before the merge, as `map` does, so the folded
    # set flows into both the cache and the envelope. Relationships profiles the
    # full inventory, so it is even more likely than map to pull a dev/replica
    # schema into scope alongside its source.
    inferred, folded_edges, mirrored_objects = rel_mod.fold_replica_relationships(
        datasets, inferred, _dev_schemas(config)
    )

    declared, declared_notes = rel_mod.declared_relationships(
        defs, [d.identifier for d in datasets]
    )
    rels, confirmed = _merge_relationships(declared, inferred)
    notes = _relationship_notes(datasets, declared, inferred, defs)
    notes.extend(declared_notes)
    notes.extend(defs.notes)
    if confirmed:
        notes.append(
            f"{confirmed} inferred join(s) match declared tests; kept as declared"
        )
    if verify and inferred:
        notes.append(
            f"verified {len(inferred)} inferred join(s) with aggregate overlap probes"
        )
    if folded_edges > 0:
        notes.append(
            f"folded {folded_edges} same-lineage duplicate relationship(s); "
            f"{mirrored_objects} object(s) mirror source lineage (a dev/replica "
            "dataset mapped alongside its source)"
        )

    # Persist the profiles this run already paid for. Because relationships
    # inventories and profiles the full set and infers across all of it, its
    # relationship set is authoritative for this run and replaces the prior one.
    store = DexStore(repo_root)
    now = datetime.now(UTC)
    cache, stats = _merge_profiles(
        store.load_cache(), datasets, connector, now, relationships=rels
    )
    path = store.save_cache(cache, now=now)
    notes.append(_persist_note(stats, len(datasets), keeps_relationships=False))

    envelope.data.update(
        {
            "relationships": [r.model_dump(mode="json") for r in rels],
            "declared_count": len(declared),
            "inferred_count": len(rels) - len(declared),
            "cache_path": str(path),
            "updated_at": now.isoformat(),
            "notes": notes,
        }
    )
    return envelope


def cmd_query(args: argparse.Namespace) -> env.Envelope:
    """Run one agent-authored SELECT through the query firewall.

    The cache gate comes first: the PII policy is computed from `.dex/` flags,
    so probing requires profiling. Every decision, allowed or refused, lands in
    `.dex/queries.jsonl`.
    """

    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    cache = store.load_cache()
    at = datetime.now(UTC).isoformat()
    if cache is None:
        return env.error(
            "no .dex/cache.json in this repo; run `explore map` first so the "
            "query firewall knows the schema and the PII flags"
        )

    config = load_config(repo_root) or DexConfig()
    limits = config.query
    # The firewall parses in the active connector's dialect, so BigQuery SQL
    # (backticks, COUNTIF) is inspected as BigQuery, not as DuckDB.
    dialect = get_dialect(getattr(args, "connector", None) or config.connector)
    try:
        inspected = inspect_query(args.sql, cache, limits, dialect=dialect)
    except QueryRefusedError as exc:
        store.append_query_log(
            {"at": at, "sql": args.sql, "decision": "refused", "reason": str(exc)}
        )
        return env.error(f"query refused: {exc}")

    adapter = command_args.open_from_args(args)
    try:
        query_estimate = getattr(adapter, "query_estimate", None)
        estimate = query_estimate(inspected.sql) if query_estimate else 0.0
        unconfirmed = command_args.billed_handshake("explore query", adapter, estimate)
        if unconfirmed is not None:
            store.append_query_log(
                {
                    "at": at,
                    "sql": inspected.sql,
                    "decision": "needs_confirmation",
                    "estimated_bytes": estimate,
                }
            )
            return unconfirmed
        result = adapter.run_query(
            inspected.sql,
            max_rows=inspected.row_cap,
            timeout_seconds=limits.timeout_seconds,
        )
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    except Exception as exc:
        store.append_query_log(
            {
                "at": at,
                "sql": inspected.sql,
                "decision": "failed",
                "reason": env.redact(str(exc)),
            }
        )
        raise
    finally:
        adapter.close()

    data = _shape_query_payload(result, inspected, limits)
    envelope.data.update(data)
    store.append_query_log(
        {
            "at": at,
            "sql": inspected.sql,
            "decision": "allowed",
            "tables": inspected.tables,
            "row_count": data["row_count"],
            "truncated": data["truncated"],
        }
    )
    return envelope


def cmd_map(args: argparse.Namespace) -> env.Envelope:
    repo_root = command_args.repo_root(args)
    config = load_config(repo_root) or DexConfig()
    defs = _project_definitions(args, config)
    hints = _merged_hints(config.ranking_hints, defs.metric_models)
    full = getattr(args, "full", False)

    adapter = command_args.open_from_args(args)
    try:
        metas = inventory_mod.inventory(adapter)
        # First-pass rank on cheap signals (no connectivity yet) to choose what to
        # profile; re-ranked with connectivity once relationships are known.
        first_pass = rank_mod.rank(metas, None, hints)
        selected = _select_for_profiling(metas, first_pass, config, full)
        # Inventory and ranking are free, so an unconfirmed billed run repeats
        # them on re-issue; only the profiling scans below need the handshake.
        estimate, per_table = _profile_estimate(
            adapter, [m.identifier for m in selected]
        )
        handshake_notes = None
        if getattr(args, "verify", False):
            handshake_notes = [
                "--verify overlap probes depend on what inference finds, so "
                "they are billed within the confirmed budget, not in this "
                "upfront estimate"
            ]
        unconfirmed = command_args.billed_handshake(
            "explore map", adapter, estimate, per_table=per_table, notes=handshake_notes
        )
        if unconfirmed is not None:
            return unconfirmed
        profile_reporter = _reporter(len(selected), "profiled", "objects")
        profiled = profile_mod.profile(
            adapter, [m.identifier for m in selected], progress=profile_reporter
        )
        profile_reporter.done()
        _annotate_grain(profiled, defs)
        inferred = rel_mod.infer_relationships(profiled)
        if getattr(args, "verify", False):
            verify_reporter = _reporter(len(inferred), "verified", "joins")
            rel_mod.verify_relationships(
                adapter,
                inferred,
                timeout_seconds=config.query.timeout_seconds,
                progress=verify_reporter,
            )
            verify_reporter.done()
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()
    # Fold same-lineage duplicates before they reach the cache: a dev/replica
    # dataset mapped alongside its source otherwise inflates one real foreign key
    # into source, replica, and cross-dataset lookalike edges.
    inferred, folded_edges, mirrored_objects = rel_mod.fold_replica_relationships(
        profiled, inferred, _dev_schemas(config)
    )

    declared, declared_notes = rel_mod.declared_relationships(
        defs, [m.identifier for m in metas]
    )
    relationships, confirmed = _merge_relationships(declared, inferred)

    store = DexStore(repo_root)
    now = datetime.now(UTC)
    prior = store.load_cache()
    # Prior profiles are only reusable when they came from the same connector.
    reusable = prior if prior and prior.provenance.connector == adapter.name else None
    final_scores = rank_mod.rank(metas, relationships, hints)
    datasets, carried = _compose_datasets(metas, profiled, final_scores, reusable)

    cache = DexCache(datasets=datasets, relationships=relationships)
    cache.provenance.connector = adapter.name
    cache.provenance.created_at = (
        prior.provenance.created_at
        if prior and prior.provenance.created_at
        else now.isoformat()
    )
    path = store.save_cache(cache, now=now)

    notes = _relationship_notes(profiled, declared, inferred, defs)
    notes.extend(declared_notes)
    notes.extend(defs.notes)
    if confirmed:
        notes.append(
            f"{confirmed} inferred join(s) match declared tests; kept as declared"
        )
    metric_hint_count = len(hints) - len(config.ranking_hints)
    if metric_hint_count > 0:
        notes.append(
            f"{metric_hint_count} model(s) back metric definitions; ranking "
            "favors them alongside configured hints"
        )
    skipped = len(metas) - len(profiled)
    if skipped > 0:
        notes.append(
            f"profiled top {len(profiled)} of {len(metas)} objects by rank "
            f"(profile_top_n={config.profile_top_n}; all objects are profiled "
            f"automatically at {_AUTO_PROFILE_ALL} or fewer); pass --full to "
            "profile everything"
        )
    if carried > 0:
        notes.append(
            f"carried forward {carried} prior profile(s) for objects not "
            "re-profiled this run; per-dataset profiled_at marks their age"
        )
    if folded_edges > 0:
        notes.append(
            f"folded {folded_edges} same-lineage duplicate relationship(s); "
            f"{mirrored_objects} object(s) mirror source lineage (a dev/replica "
            "dataset mapped alongside its source)"
        )

    pii_columns = sum(1 for d in profiled for c in d.columns if c.pii is not None)
    quality_notes = sum(len(d.data_quality) for d in profiled)
    top = sorted(datasets, key=lambda d: d.rank_score or 0.0, reverse=True)[:5]
    envelope.data.update(
        {
            "cache_path": str(path),
            "object_count": len(metas),
            "profiled_count": len(profiled),
            "skipped_count": skipped,
            "carried_forward_count": carried,
            "relationship_count": len(relationships),
            "pii_column_count": pii_columns,
            "data_quality_note_count": quality_notes,
            "top_objects": [
                {"identifier": d.identifier, "rank_score": d.rank_score} for d in top
            ],
            "notes": notes,
            "updated_at": now.isoformat(),
        }
    )
    return envelope


def cmd_cluster(args: argparse.Namespace) -> env.Envelope:
    """k-means clustering over a bounded sample of one object's numeric columns.

    Cache-gated like `explore query`: profiling is what tells us which columns
    are numeric and which are PII, so `explore map`/`profile` must have run. Only
    the feature columns are scanned, only a bounded sample is fetched into the
    engine for scikit-learn, and only aggregates (cluster sizes and centroids)
    reach the envelope. The sample query goes through the same cost-before-spend
    handshake as every other scanning command.
    """

    repo_root = command_args.repo_root(args)
    store = DexStore(repo_root)
    cache = store.load_cache()
    if cache is None:
        return env.error(
            "no .dex/cache.json in this repo; run `explore map` (or `explore "
            "profile <object>`) first so clustering knows which columns are "
            "numeric and which are PII"
        )

    # Fail fast if the [cluster] extra is missing: no connection, no spend.
    try:
        cluster_mod.ensure_available()
    except cluster_mod.ClusterDependencyError as exc:
        return env.error(str(exc))

    config = load_config(repo_root) or DexConfig()
    limits = config.cluster

    known = [d.identifier for d in cache.datasets if d.columns]
    matches = match_identifier(args.object, known)
    if not matches:
        return env.error(
            f"'{args.object}' is not a profiled object in the .dex cache; run "
            f"`explore profile {args.object}` (or `explore map`) first"
        )
    if len(matches) > 1:
        return env.error(
            f"'{args.object}' is ambiguous: {', '.join(matches)}; qualify it"
        )
    dataset = next(d for d in cache.datasets if d.identifier == matches[0])

    requested = _split_features(getattr(args, "features", None))
    try:
        features, selection_notes = _select_cluster_features(
            dataset, requested, limits.max_features
        )
    except ValueError as exc:
        return env.error(str(exc))

    k = getattr(args, "k", None)

    adapter = command_args.open_from_args(args)
    try:
        sample_sql, sample_method = cluster_mod.build_sample_sql(
            dataset.identifier,
            features,
            dialect=adapter.dialect,
            sample_rows=limits.sample_rows,
            row_count=dataset.row_count,
        )
        query_estimate = getattr(adapter, "query_estimate", None)
        estimate = query_estimate(sample_sql) if query_estimate else 0.0
        unconfirmed = command_args.billed_handshake(
            "explore cluster",
            adapter,
            estimate,
            notes=[
                f"clusters a sample of up to {limits.sample_rows} rows over "
                f"{len(features)} feature column(s); sampling: {sample_method}"
            ],
        )
        if unconfirmed is not None:
            return unconfirmed
        result = adapter.run_query(
            sample_sql,
            max_rows=limits.sample_rows,
            timeout_seconds=limits.timeout_seconds,
        )
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()

    try:
        cluster_result = cluster_mod.cluster_features(
            features,
            result.cells,
            k=k,
            k_min=limits.k_min,
            k_max=limits.k_max,
            silhouette_sample=limits.silhouette_sample,
            random_state=limits.random_state,
        )
    except cluster_mod.ClusterError as exc:
        return env.error(str(exc))

    data = cluster_result.to_data()
    notes = [*selection_notes, *data.pop("notes", [])]
    if result.truncated:
        notes.append(
            f"the sample hit the {limits.sample_rows}-row cap (the table has more "
            "rows); raise cluster.sample_rows in .dex/config.yml to widen it"
        )
    envelope.data.update(
        {
            "object": dataset.identifier,
            "total_rows": dataset.row_count,
            "sample_method": sample_method,
            **data,
            "notes": notes,
        }
    )
    return envelope


# --- helpers -----------------------------------------------------------------


def _split_features(raw: list[str] | None) -> list[str] | None:
    """Flatten repeated/comma-joined --features into a clean name list. Both
    `--features a,b --features c` and `--features "a, b, c"` are natural."""

    if not raw:
        return None
    names = [part.strip() for entry in raw for part in entry.split(",")]
    return [name for name in names if name] or None


def _is_constant_column(col) -> bool:
    """A column proven to hold a single value contributes nothing to a distance
    and only dilutes the standardization; an unknown distinct count is kept."""

    return col.distinct_count is not None and col.distinct_count <= 1


def _select_cluster_features(
    dataset: Dataset, requested: list[str] | None, max_features: int
) -> tuple[list[str], list[str]]:
    """Resolve the feature columns for clustering plus notes explaining the set.

    Explicit ``--features`` are honored as given (validated numeric); a PII
    column may be named deliberately, and only its per-cluster mean, an
    aggregate, is ever reported. Auto-selection is conservative: numeric columns
    that are not PII-flagged, not a proven unique key (an identifier is not a
    feature), and not constant. Raises ``ValueError`` with an actionable message
    when the request cannot be satisfied.
    """

    by_lower = {col.name.lower(): col for col in dataset.columns}
    notes: list[str] = []

    if requested is not None:
        chosen = []
        for name in requested:
            col = by_lower.get(name.lower())
            if col is None:
                raise ValueError(
                    f"column '{name}' is not among the profiled columns of "
                    f"{dataset.identifier}"
                )
            if not profile_mod.is_numeric_type(col.data_type):
                raise ValueError(
                    f"column '{name}' is {col.data_type}, not numeric; k-means "
                    "clusters numeric features only"
                )
            chosen.append(col)
        pii_named = [c.name for c in chosen if c.pii is not None]
        if pii_named:
            notes.append(
                f"included {len(pii_named)} PII-flagged feature(s) "
                f"({', '.join(pii_named)}) at your request; only each cluster's "
                "mean (an aggregate) is reported, never a row value"
            )
        features = [c.name for c in chosen]
    else:
        numeric = [
            c for c in dataset.columns if profile_mod.is_numeric_type(c.data_type)
        ]
        excluded_pii = [c.name for c in numeric if c.pii is not None]
        remaining = [c for c in numeric if c.pii is None]
        excluded_id = [c.name for c in remaining if c.is_unique is True]
        remaining = [c for c in remaining if c.is_unique is not True]
        excluded_const = [c.name for c in remaining if _is_constant_column(c)]
        candidates = [c for c in remaining if not _is_constant_column(c)]
        features = [c.name for c in candidates]
        if excluded_pii:
            notes.append(
                f"excluded {len(excluded_pii)} PII-flagged numeric column(s) from "
                f"auto-selection ({', '.join(excluded_pii)}); name one in "
                "--features to include it (its centroid is a mean)"
            )
        if excluded_id:
            notes.append(
                f"excluded {len(excluded_id)} unique-key column(s) "
                f"({', '.join(excluded_id)}); an identifier is not a feature"
            )
        if excluded_const:
            notes.append(f"excluded {len(excluded_const)} constant column(s)")

    if len(features) > max_features:
        dropped = len(features) - max_features
        features = features[:max_features]
        notes.append(
            f"using the first {max_features} feature(s); {dropped} more available "
            "(raise cluster.max_features or pass --features)"
        )
    if len(features) < 2:
        raise ValueError(
            f"found {len(features)} usable numeric feature column(s) for "
            f"{dataset.identifier}; k-means needs at least 2. Pass --features to "
            "choose columns, or profile a table with more numeric columns"
        )
    return features, notes


def _shape_query_payload(
    result: QueryResult,
    inspected: InspectedQuery,
    limits: QueryLimits,
) -> dict:
    """Cap the result for agent context: columnar cells, cell-width truncation,
    and a total payload byte cap, each announced in `notes` so a cut result is
    never mistaken for a complete one."""

    notes: list[str] = []

    clipped = 0
    cells: list[list] = []
    for row in result.cells:
        shaped: list = []
        for value in row:
            if isinstance(value, str) and len(value) > limits.max_cell_chars:
                shaped.append(value[: limits.max_cell_chars] + "...")
                clipped += 1
            else:
                shaped.append(value)
        cells.append(shaped)
    if clipped:
        notes.append(f"{clipped} cell(s) truncated to {limits.max_cell_chars} chars")

    dropped = 0
    while cells and len(json.dumps(cells)) > limits.max_payload_bytes:
        cells.pop()
        dropped += 1
    if dropped:
        notes.append(
            f"dropped {dropped} row(s) to fit the {limits.max_payload_bytes}-byte "
            "payload cap; aggregate further or select fewer columns"
        )

    truncated = (result.truncated and inspected.capped_by_engine) or dropped > 0
    if result.truncated and inspected.capped_by_engine:
        notes.append(
            f"result truncated to {inspected.row_cap} rows (engine cap); refine "
            "the query, or raise query.max_rows in .dex/config.yml"
        )

    return {
        "columns": result.columns,
        "types": result.types,
        "cells": cells,
        "row_count": len(cells),
        "truncated": truncated,
        "tables": inspected.tables,
        "notes": notes,
    }


def _project_definitions(
    args: argparse.Namespace, config: DexConfig
) -> dbt_project.ProjectDefinitions:
    """The dbt project's declared definitions, honoring the config pin.

    Exploration starts bare: warehouse observations stay independent of
    whatever repo dex happens to run from, so the declared definitions fold in
    only when ``--use-project`` asks for them. Without the flag, a present
    project earns a discovery note instead of influence. With it, a repo
    without a project (or with an ambiguous choice) degrades to the empty
    view, so explore keeps working on raw warehouses.
    """

    repo_root = command_args.repo_root(args)
    if not getattr(args, "use_project", False):
        defs = dbt_project.ProjectDefinitions()
        discovered = dbt_project.discover_projects(repo_root)
        if discovered:
            # project_dir marks "found but unused" so the empty-declared note
            # can say so instead of claiming there is no project.
            defs.project_dir = str(discovered[0])
            defs.notes.append(
                "a dbt project is present but unused; pass --use-project to "
                "fold its declared joins, grain, and metric definitions into "
                "exploration"
            )
        return defs
    pin = Path(repo_root) / config.dbt_project_dir if config.dbt_project_dir else None
    return dbt_project.definitions(repo_root, pin)


def _merge_relationships(
    declared: list[Relationship], inferred: list[Relationship]
) -> tuple[list[Relationship], int]:
    """Declared joins win over the same inferred edge.

    Returns the merged list plus how many inferred edges the declared set
    absorbed: inference independently agreeing with a declared test is worth a
    note, and double-reporting the edge would inflate connectivity ranking.
    """

    def edge_key(rel: Relationship) -> tuple:
        return (
            rel.from_dataset.lower(),
            tuple(c.lower() for c in rel.from_columns),
            rel.to_dataset.lower(),
            tuple(c.lower() for c in rel.to_columns),
        )

    declared_keys = {edge_key(rel) for rel in declared}
    merged = list(declared)
    confirmed = 0
    for rel in inferred:
        if edge_key(rel) in declared_keys:
            confirmed += 1
            continue
        merged.append(rel)
    return merged, confirmed


def _merged_hints(user_hints: list[str], metric_models: list[str]) -> list[str]:
    """User-configured ranking hints plus the models metric definitions ground
    in. User hints come first and are never displaced; metric-backed models are
    appended so the naming signal favors what the project measures."""

    merged = list(user_hints)
    seen = {h.strip().lower() for h in user_hints if isinstance(h, str)}
    for model in metric_models:
        if model.lower() not in seen:
            merged.append(model)
            seen.add(model.lower())
    return merged


def _annotate_grain(
    datasets: list[Dataset], defs: dbt_project.ProjectDefinitions | None = None
) -> None:
    """Attach the interpretation layer to raw profiles: candidate keys, the likely
    grain, and the data-quality warnings an analyst would write (non-unique own
    key, unknown grain). Shared by profile and map so a single-table profile
    surfaces a broken grain without requiring a full map.

    With project definitions, the declared truth refines the heuristics: a
    semantic model's primary entity overrides the detected grain (noting any
    disagreement), and a profiled column contradicting its declared ``unique``
    test gets a data-quality note. ``candidate_keys`` stays measurement-only:
    an unmeasured declared key is a claim, and the cache is a drift baseline.
    """

    declared_grain: dict[str, str] = {}
    declared_unique: dict[str, set[str]] = {}
    if defs is not None and (defs.primary_entities or defs.declared_keys):
        identifiers = [d.identifier for d in datasets]
        for model, column in defs.primary_entities.items():
            ident, _ambiguous = rel_mod.resolve_declared(
                defs.model_relations.get(model), model, identifiers
            )
            if ident is not None:
                declared_grain[ident.lower()] = column
        for key in defs.declared_keys:
            if not key.unique:
                continue
            ident, _ambiguous = rel_mod.resolve_declared(
                key.relation, key.model, identifiers
            )
            if ident is not None:
                declared_unique.setdefault(ident.lower(), set()).add(key.column.lower())

    for ds in datasets:
        ds.candidate_keys = rel_mod.candidate_keys(ds)
        ds.grain = rel_mod.detect_grain(ds)
        ds.data_quality.extend(rel_mod.data_quality_notes(ds))

        grain_column = declared_grain.get(ds.identifier.lower())
        if grain_column is not None:
            profiled = next(
                (c for c in ds.columns if c.name.lower() == grain_column.lower()),
                None,
            )
            if profiled is not None:
                declared = [profiled.name]
                if ds.grain and ds.grain != declared:
                    ds.data_quality.append(
                        f"grain {profiled.name} comes from the project's declared "
                        f"primary entity (heuristic suggested {', '.join(ds.grain)})"
                    )
                ds.grain = declared
        for col in ds.columns:
            if (
                col.name.lower() in declared_unique.get(ds.identifier.lower(), set())
                and col.is_unique is False
            ):
                ds.data_quality.append(
                    f"{col.name} is declared unique in the dbt project but "
                    "profiling found duplicates"
                )


def _relationship_notes(
    datasets: list[Dataset],
    declared: list,
    inferred: list,
    defs: dbt_project.ProjectDefinitions | None = None,
) -> list[str]:
    """Explain the inference result so an empty array is distinguishable from
    'no relationships exist': what was examined and why nothing survived."""

    fk_columns = rel_mod.fk_candidate_count(datasets)
    notes = [
        f"inference examined {fk_columns} id-shaped column(s) "
        f"across {len(datasets)} profiled object(s)"
    ]
    if not declared:
        if defs is not None and defs.present and defs.foreign_keys:
            # The project declares foreign keys, but none resolved here; the
            # per-join notes from resolution say which and why.
            notes.append("no declared relationships resolved against this connection")
        elif defs is not None and not defs.present and defs.project_dir:
            notes.append("no declared relationships (dbt project present but unused)")
        else:
            notes.append(
                "no declared relationships (no dbt project or no declared foreign keys)"
            )
    if fk_columns and not inferred:
        notes.append(
            "no id-shaped column matched a parent table by name; joins may exist "
            "that name-based inference cannot see"
        )
    if not fk_columns:
        notes.append("no id-shaped columns found, so there was nothing to infer from")
    return notes


def _select_for_profiling(
    metas: list[ObjectMeta],
    scores: dict[str, float],
    config: DexConfig,
    full: bool,
) -> list[ObjectMeta]:
    if full or len(metas) <= _AUTO_PROFILE_ALL:
        return metas
    ranked = sorted(metas, key=lambda m: scores.get(m.identifier, 0.0), reverse=True)
    return ranked[: config.profile_top_n]


def _compose_datasets(
    metas: list[ObjectMeta],
    profiled: list[Dataset],
    scores: dict[str, float],
    prior: DexCache | None,
) -> tuple[list[Dataset], int]:
    """Merge this run's profiles over the full inventory. Returns the composed
    datasets plus how many prior profiles were carried forward.

    An object not profiled this run reuses its prior profile wholesale (columns,
    keys, grain, notes, and its original ``profiled_at``, which marks the age)
    rather than silently degrading to an inventory-only entry; only the rank
    score is refreshed. ``row_count`` stays the prior one so the carried record
    is internally consistent with its own notes and counts. Carried profiles do
    not feed relationship inference, which runs on this run's profiles only.
    """

    by_id = {d.identifier: d for d in profiled}
    prior_by_id = (
        {d.identifier: d for d in prior.datasets if d.columns} if prior else {}
    )
    datasets: list[Dataset] = []
    carried = 0
    for meta in metas:
        ds = by_id.get(meta.identifier)
        if ds is None:
            previous = prior_by_id.get(meta.identifier)
            if previous is not None:
                ds = previous.model_copy(deep=True)
                carried += 1
            else:
                # Never profiled: an inventory-only entry keeps the landscape
                # complete without scanning every object.
                ds = Dataset(
                    identifier=meta.identifier,
                    object_type=meta.object_type,
                    row_count=meta.row_count,
                    byte_size=meta.byte_size,
                )
        ds.rank_score = scores.get(meta.identifier)
        datasets.append(ds)
    return datasets, carried


# Sentinel: preserve the prior cache's relationships (profile has no inference
# pass, so it has no business touching them).
_KEEP_RELATIONSHIPS = object()


def _merge_profiles(
    prior: DexCache | None,
    profiled: list[Dataset],
    connector: str,
    now: datetime,
    *,
    relationships=_KEEP_RELATIONSHIPS,
) -> tuple[DexCache, dict]:
    """Fold freshly profiled datasets into a prior cache, keyed by identifier.

    Unlike ``_compose_datasets`` (inventory-driven: it iterates metas and
    manufactures inventory-only stubs), this merges over prior datasets plus
    the freshly profiled set and never fabricates stubs or drops prior entries.

    A same-connector prior is reusable; a mismatched-connector prior is dropped
    wholesale (mirrors ``cmd_map``'s reuse gate: mixing connectors would poison
    the PII policy and the maintain baseline, and `.dex/` is non-canonical
    scratch that one `explore map` rebuilds). A refreshed dataset carries
    forward the prior ``rank_score``, because profile and relationships do not
    compute rank. Relationships are preserved by default (profile) or replaced
    with the passed set (relationships, whose full-inventory inference is
    authoritative for its run).
    """

    reusable = prior if prior and prior.provenance.connector == connector else None
    by_id = {d.identifier: d for d in profiled}
    datasets: list[Dataset] = []
    consumed: set[str] = set()
    if reusable is not None:
        for old in reusable.datasets:
            fresh = by_id.get(old.identifier)
            if fresh is not None:
                fresh.rank_score = old.rank_score  # keep map's connectivity ranking
                datasets.append(fresh)
                consumed.add(old.identifier)
            else:
                datasets.append(old)  # untouched; keeps its older profiled_at
    # Anything left over is newly profiled and inserted; rank_score stays None.
    datasets.extend(ds for ds in profiled if ds.identifier not in consumed)
    if relationships is _KEEP_RELATIONSHIPS:
        rels = list(reusable.relationships) if reusable else []
    else:
        rels = relationships
    cache = DexCache(datasets=datasets, relationships=rels)
    cache.provenance.connector = connector
    cache.provenance.created_at = (
        reusable.provenance.created_at
        if reusable and reusable.provenance.created_at
        else now.isoformat()
    )
    stats = {
        "connector": connector,
        "merged": reusable is not None,
        "refreshed": len(consumed),
        "added": len(profiled) - len(consumed),
        "replaced_connector": (
            prior.provenance.connector if prior and reusable is None else None
        ),
    }
    return cache, stats


def _persist_note(stats: dict, count: int, *, keeps_relationships: bool) -> str:
    """One sentence saying what the cache write did, driven by merge stats."""

    if stats["replaced_connector"]:
        return (
            f"prior cache was built for connector '{stats['replaced_connector']}'; "
            f"profiling on '{stats['connector']}' replaced it with a fresh cache "
            f"of the {count} profiled object(s); run `explore map` to rebuild "
            "the full landscape"
        )
    if stats["merged"]:
        preserved = (
            "other datasets and relationships preserved"
            if keeps_relationships
            else "other datasets preserved"
        )
        return (
            f"merged {count} profiled object(s) into the existing cache "
            f"({stats['refreshed']} refreshed, {stats['added']} added); {preserved}"
        )
    return (
        f"created .dex/cache.json with {count} profiled object(s); run "
        "`explore map` to add the full inventory and relationships"
    )


def _resolve_identifiers(adapter: Adapter, requested: list[str]) -> list[str]:
    """Map user-supplied object names (possibly bare) to full identifiers.

    Accepts an exact identifier, a ``schema.name`` suffix, or a bare object name,
    and fails cleanly on an unknown or ambiguous name rather than guessing.
    Comma-joined lists (``profile a,b,c``) are as natural a first guess as
    space-separated ones, so both are accepted.
    """

    known = [m.identifier for m in adapter.list_objects()]
    names = [part.strip() for raw in requested for part in raw.split(",")]
    resolved: list[str] = []
    for name in (n for n in names if n):
        unique = match_identifier(name, known)
        if not unique:
            raise ValueError(f"no object named '{name}' in this connection")
        if len(unique) > 1:
            raise ValueError(f"'{name}' is ambiguous: {', '.join(unique)}; qualify it")
        resolved.append(unique[0])
    return resolved
