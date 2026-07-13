"""Explore command orchestrators.

Each ``cmd_*`` opens the adapter, drives the explore engine, and shapes the result
into the sanitized envelope. Keeping this here (not in ``cli.py``) keeps dispatch
thin and keeps ``map``'s composition (it runs inventory, profile, and relationships
together) out of the CLI layer. These are the only explore commands that hold an
adapter; ``map`` is the only one that writes, and only to the ``.dex/`` cache.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from .. import command_args
from .. import envelope as env
from ..adapters import get_dialect
from ..adapters.base import Adapter, ObjectMeta, QueryResult
from ..cache import Dataset, DexCache, DexStore, match_identifier
from ..config import DexConfig, QueryLimits, load_config
from ..guards.query_firewall import (
    InspectedQuery,
    QueryRefusedError,
    inspect_query,
)
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
    adapter = command_args.open_from_args(args)
    try:
        identifiers = _resolve_identifiers(adapter, args.objects)
        estimate, per_table = _profile_estimate(adapter, identifiers)
        unconfirmed = command_args.billed_handshake(
            "explore profile", adapter, estimate, per_table=per_table
        )
        if unconfirmed is not None:
            return unconfirmed
        datasets = profile_mod.profile(adapter, identifiers)
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()
    _annotate_grain(datasets)
    envelope.data["datasets"] = [d.model_dump(mode="json") for d in datasets]
    return envelope


def cmd_relationships(args: argparse.Namespace) -> env.Envelope:
    verify = getattr(args, "verify", False)
    config = load_config(command_args.repo_root(args)) or DexConfig()

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
        datasets = profile_mod.profile(adapter, identifiers)
        inferred = rel_mod.infer_relationships(datasets)
        if verify:
            rel_mod.verify_relationships(
                adapter, inferred, timeout_seconds=config.query.timeout_seconds
            )
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()

    declared = rel_mod.declared_relationships(command_args.repo_root(args))
    rels = declared + inferred
    notes = _relationship_notes(datasets, declared, inferred)
    if verify and inferred:
        notes.append(
            f"verified {len(inferred)} inferred join(s) with aggregate overlap probes"
        )
    envelope.data.update(
        {
            "relationships": [r.model_dump(mode="json") for r in rels],
            "declared_count": len(declared),
            "inferred_count": len(inferred),
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
    full = getattr(args, "full", False)

    adapter = command_args.open_from_args(args)
    try:
        metas = inventory_mod.inventory(adapter)
        # First-pass rank on cheap signals (no connectivity yet) to choose what to
        # profile; re-ranked with connectivity once relationships are known.
        first_pass = rank_mod.rank(metas, None, config.ranking_hints)
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
        profiled = profile_mod.profile(adapter, [m.identifier for m in selected])
        _annotate_grain(profiled)
        inferred = rel_mod.infer_relationships(profiled)
        if getattr(args, "verify", False):
            rel_mod.verify_relationships(
                adapter, inferred, timeout_seconds=config.query.timeout_seconds
            )
        envelope = env.ok({})
        command_args.stamp_spend(envelope, adapter)
    finally:
        adapter.close()
    # Fold same-lineage duplicates before they reach the cache: a dev/replica
    # dataset mapped alongside its source otherwise inflates one real foreign key
    # into source, replica, and cross-dataset lookalike edges.
    dev_schemas = frozenset(
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
    inferred, folded_edges, mirrored_objects = rel_mod.fold_replica_relationships(
        profiled, inferred, dev_schemas
    )

    declared = rel_mod.declared_relationships(repo_root)
    relationships = declared + inferred

    store = DexStore(repo_root)
    now = datetime.now(UTC)
    prior = store.load_cache()
    # Prior profiles are only reusable when they came from the same connector.
    reusable = prior if prior and prior.provenance.connector == adapter.name else None
    final_scores = rank_mod.rank(metas, relationships, config.ranking_hints)
    datasets, carried = _compose_datasets(metas, profiled, final_scores, reusable)

    cache = DexCache(datasets=datasets, relationships=relationships)
    cache.provenance.connector = adapter.name
    cache.provenance.created_at = (
        prior.provenance.created_at
        if prior and prior.provenance.created_at
        else now.isoformat()
    )
    path = store.save_cache(cache, now=now)

    notes = _relationship_notes(profiled, declared, inferred)
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


# --- helpers -----------------------------------------------------------------


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


def _annotate_grain(datasets: list[Dataset]) -> None:
    """Attach the interpretation layer to raw profiles: candidate keys, the likely
    grain, and the data-quality warnings an analyst would write (non-unique own
    key, unknown grain). Shared by profile and map so a single-table profile
    surfaces a broken grain without requiring a full map."""

    for ds in datasets:
        ds.candidate_keys = rel_mod.candidate_keys(ds)
        ds.grain = rel_mod.detect_grain(ds)
        ds.data_quality.extend(rel_mod.data_quality_notes(ds))


def _relationship_notes(
    datasets: list[Dataset],
    declared: list,
    inferred: list,
) -> list[str]:
    """Explain the inference result so an empty array is distinguishable from
    'no relationships exist': what was examined and why nothing survived."""

    fk_columns = rel_mod.fk_candidate_count(datasets)
    notes = [
        f"inference examined {fk_columns} id-shaped column(s) "
        f"across {len(datasets)} profiled object(s)"
    ]
    if not declared:
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
