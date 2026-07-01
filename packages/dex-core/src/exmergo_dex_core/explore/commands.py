"""Explore command orchestrators.

Each ``cmd_*`` opens the adapter, drives the explore engine, and shapes the result
into the sanitized envelope. Keeping this here (not in ``cli.py``) keeps dispatch
thin and keeps ``map``'s composition (it runs inventory, profile, and relationships
together) out of the CLI layer. These are the only explore commands that hold an
adapter; ``map`` is the only one that writes, and only to the ``.dex/`` cache.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from .. import command_args
from .. import envelope as env
from ..adapters.base import Adapter, ObjectMeta
from ..cache import Dataset, DexCache, DexStore
from ..config import DexConfig, load_config
from . import inventory as inventory_mod
from . import profile as profile_mod
from . import rank as rank_mod
from . import relationships as rel_mod

# Below this many objects, profile everything: enumeration is cheap and complete.
# Above it, profile only the top-ranked unless --full is passed.
_AUTO_PROFILE_ALL = 50


def cmd_inventory(args: argparse.Namespace) -> env.Envelope:
    adapter = command_args.open_from_args(args)
    try:
        metas = inventory_mod.inventory(adapter)
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
    return env.ok({"object_count": len(objects), "objects": objects, "ranked": ranked})


def cmd_profile(args: argparse.Namespace) -> env.Envelope:
    adapter = command_args.open_from_args(args)
    try:
        identifiers = _resolve_identifiers(adapter, args.objects)
        datasets = profile_mod.profile(adapter, identifiers)
    finally:
        adapter.close()
    _annotate_grain(datasets)
    return env.ok({"datasets": [d.model_dump(mode="json") for d in datasets]})


def cmd_relationships(args: argparse.Namespace) -> env.Envelope:
    adapter = command_args.open_from_args(args)
    try:
        # Relationship inference needs uniqueness signals, so profile every object
        # first (free and local on DuckDB), then infer across the full set.
        metas = inventory_mod.inventory(adapter)
        datasets = profile_mod.profile(adapter, [m.identifier for m in metas])
    finally:
        adapter.close()

    inferred = rel_mod.infer_relationships(datasets)
    declared = rel_mod.declared_relationships(command_args.repo_root(args))
    rels = declared + inferred
    return env.ok(
        {
            "relationships": [r.model_dump(mode="json") for r in rels],
            "declared_count": len(declared),
            "inferred_count": len(inferred),
            "notes": _relationship_notes(datasets, declared, inferred),
        }
    )


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
        profiled = profile_mod.profile(adapter, [m.identifier for m in selected])
    finally:
        adapter.close()

    _annotate_grain(profiled)

    inferred = rel_mod.infer_relationships(profiled)
    declared = rel_mod.declared_relationships(repo_root)
    relationships = declared + inferred

    final_scores = rank_mod.rank(metas, relationships, config.ranking_hints)
    datasets = _compose_datasets(metas, profiled, final_scores)

    store = DexStore(repo_root)
    now = datetime.now(UTC)
    prior = store.load_cache()
    cache = DexCache(datasets=datasets, relationships=relationships)
    cache.provenance.connector = adapter.name
    cache.provenance.created_at = (
        prior.provenance.created_at
        if prior and prior.provenance.created_at
        else now.isoformat()
    )
    path = store.save_cache(cache, now=now)

    pii_columns = sum(1 for d in profiled for c in d.columns if c.pii is not None)
    quality_notes = sum(len(d.data_quality) for d in profiled)
    top = sorted(datasets, key=lambda d: d.rank_score or 0.0, reverse=True)[:5]
    return env.ok(
        {
            "cache_path": str(path),
            "object_count": len(metas),
            "profiled_count": len(profiled),
            "relationship_count": len(relationships),
            "pii_column_count": pii_columns,
            "data_quality_note_count": quality_notes,
            "top_objects": [
                {"identifier": d.identifier, "rank_score": d.rank_score} for d in top
            ],
            "notes": _relationship_notes(profiled, declared, inferred),
            "updated_at": now.isoformat(),
        }
    )


# --- helpers -----------------------------------------------------------------


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
) -> list[Dataset]:
    by_id = {d.identifier: d for d in profiled}
    datasets: list[Dataset] = []
    for meta in metas:
        ds = by_id.get(meta.identifier)
        if ds is None:
            # Not profiled this run: keep it as an inventory-only entry so the
            # landscape stays complete without scanning every object.
            ds = Dataset(
                identifier=meta.identifier,
                object_type=meta.object_type,
                row_count=meta.row_count,
                byte_size=meta.byte_size,
            )
        ds.rank_score = scores.get(meta.identifier)
        datasets.append(ds)
    return datasets


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
        matches = [
            ident
            for ident in known
            if ident == name
            or ident.endswith(f".{name}")
            or ident.split(".")[-1] == name
        ]
        unique = sorted(set(matches))
        if not unique:
            raise ValueError(f"no object named '{name}' in this connection")
        if len(unique) > 1:
            raise ValueError(f"'{name}' is ambiguous: {', '.join(unique)}; qualify it")
        resolved.append(unique[0])
    return resolved
