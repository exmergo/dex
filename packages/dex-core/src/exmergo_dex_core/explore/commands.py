"""Explore orchestration, in two layers.

The lower layer is the run functions (``inventory``, ``profile``, ``map``, and so
on): they take an :class:`~..engine.DexEngine` and plain arguments, drive the explore
engine, and return a record from :mod:`.results`. They are what
``DexEngine.profile()`` and friends call, so a library caller and the CLI execute
exactly the same code.

The upper layer is the ``cmd_*`` shims: argparse in, envelope out, nothing else.
Keeping ``map``'s composition (it runs inventory, profile, and relationships
together) down here rather than in ``cli.py`` is what keeps dispatch thin.

``map``, ``profile``, and ``relationships`` all persist what they learned, and
only to the exploration cache, so a scan is never paid for twice.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .. import command_args, dbt_project
from .. import envelope as env
from ..adapters import get_dialect
from ..adapters.base import Adapter, ObjectMeta, name_list

# Aliased: `QueryResult` here is the explore record, and the adapter's
# same-named row carrier is only a type hint on one shaping helper.
from ..adapters.base import QueryResult as AdapterQueryResult  # noqa: F401
from ..cache import (
    Dataset,
    DexCache,
    Relationship,
    RelationshipKind,
    match_identifier,
    relation_verdict,
)
from ..config import (
    DexConfig,
    PIIOverride,
    QueryLimits,
    blob_override_paths,
    pii_override_paths,
)
from ..errors import DexError, PrerequisiteError, RequestError
from ..guards.cost_guard import (
    ConfirmationRequiredError,
    CostGuardError,
    OverCeilingError,
)
from ..guards.query_firewall import (
    InspectedQuery,
    QueryRefusedError,
    assert_query_shape,
    inspect_query,
)
from ..guards.sql_guard import referenced_relations, split_statements
from ..progress import ProgressReporter
from ..results import BudgetExhaustedError, ConfirmationRequest, to_envelope
from ..semantic_catalog import entity_joins
from ..storage import CacheUnreadableError, Document, ExploreStore, readable_cache
from . import cluster as cluster_mod
from . import cumulative as cumulative_mod
from . import diagram as diagram_mod
from . import inventory as inventory_mod
from . import profile as profile_mod
from . import rank as rank_mod
from . import relationships as rel_mod
from .results import (
    ClusterResult,
    DiagramResult,
    InventoryEntry,
    InventoryResult,
    MapResult,
    ProfileResult,
    QueryBatchResult,
    QueryResult,
    QueryStatementResult,
    RankedObject,
    RelationshipsResult,
)
from .summary import summarize_map

if TYPE_CHECKING:
    from ..engine import DexEngine

# Below this many objects, profile everything: enumeration is cheap and complete.
# Above it, profile only the top-ranked unless --full is passed.
_AUTO_PROFILE_ALL = 50


class CacheRequiredError(PrerequisiteError):
    """A command needs profiled objects and the exploration cache has none.

    The message names the command that fills the gap rather than a filename,
    because where the cache lives is the store's business: on the filesystem
    backend it is a file, in a hosted deployment it is a row, and the fix
    ("run `explore map` first") is the same either way.
    """


def _override_notes(datasets: list[Dataset]) -> list[str]:
    cleared = sum(1 for d in datasets for c in d.columns if c.pii_overridden)
    if not cleared:
        return []
    return [
        f"{cleared} column(s) cleared by pii_overrides in .dex/config.yml "
        "(recorded as overridden in the cache)"
    ]


def _override_mismatches(
    datasets: list[Dataset], overrides: list[PIIOverride]
) -> list[str]:
    """Warn when an override entry can't possibly do anything: an exact entry
    names a profiled table but no column of it, or a pattern entry's scope
    matches profiled tables but none carries the named column. Almost
    certainly a typo, and silence would read as the override working.

    A pattern matching zero tables stays silent, same escape hatch as an exact
    entry on a not-yet-profiled table: new entities landing later under the
    same scope are the whole point of the pattern form."""

    warnings = []
    for entry in overrides:
        if entry.column:
            path = entry.column.strip().lower()
            table, _, column = path.rpartition(".")
            for dataset in datasets:
                if dataset.identifier.lower() != table:
                    continue
                if not any(c.name.lower() == column for c in dataset.columns):
                    warnings.append(
                        f"pii_overrides entry '{path}' matches no column of "
                        f"{dataset.identifier}"
                    )
                break
        else:
            column_name = entry.column_name.strip().lower()  # type: ignore[union-attr]
            scope = entry.scope.strip().lower()  # type: ignore[union-attr]
            matched = [
                d for d in datasets if fnmatch.fnmatchcase(d.identifier.lower(), scope)
            ]
            if matched and not any(
                c.name.lower() == column_name for d in matched for c in d.columns
            ):
                warnings.append(
                    f"pii_overrides pattern entry (column_name='{entry.column_name}', "
                    f"scope='{entry.scope}') matches no column named "
                    f"'{entry.column_name}' in {len(matched)} matched dataset(s)"
                )
    return warnings


def _mask_overridden(cache: DexCache, override_paths: set[str]) -> DexCache:
    """Apply config overrides to a loaded cache in memory, so an override takes
    effect at query time immediately instead of demanding a re-profile (a billed
    scan on metered connectors). The persisted cache is untouched; the next
    profile writes the override through durably."""

    if not override_paths:
        return cache
    for dataset in cache.datasets:
        for column in dataset.columns:
            if (
                column.pii is not None
                and f"{dataset.identifier}.{column.name}".lower() in override_paths
            ):
                column.pii_overridden = column.pii.category
                column.pii = None
    return cache


def _profile_estimate(
    adapter: Adapter, identifiers: list[str], *, include_blobs: set[str] | None = None
) -> tuple[float, dict[str, float]]:
    estimate = getattr(adapter, "profile_estimate", None)
    if estimate is None:
        return 0.0, {}
    return estimate(identifiers, include_blobs=include_blobs or set())


def _verify_estimate(
    adapter: Adapter, relationships: list[Relationship]
) -> tuple[float, int, int]:
    """Free dry-run pricing of the overlap probes verify would run, plus the
    candidate/object counts for the checkpoint payload; zero-cost on free
    adapters or when nothing qualifies to probe.

    Selects through `probe_candidates`, the same function `verify_relationships`
    iterates, so the priced set and the run set are the same set by
    construction rather than by two filters agreeing."""

    candidates = rel_mod.probe_candidates(relationships)
    objects = {r.from_dataset for r in candidates} | {r.to_dataset for r in candidates}
    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not candidates:
        return 0.0, len(candidates), len(objects)
    total = sum(
        query_estimate(sql)
        for sql in rel_mod.probe_statements(candidates, adapter.dialect)
    )
    return total, len(candidates), len(objects)


def _overlap_estimate(
    adapter: Adapter, candidates: list[Relationship]
) -> tuple[float, int, int]:
    """Free dry-run pricing of the probes ``--infer-by-overlap`` would run
    against ``candidates`` (already capped by :func:`rel_mod.overlap_sweep_candidates`),
    plus the candidate/object counts for the checkpoint payload; zero-cost on
    free adapters or when nothing qualifies to probe.

    Mirrors :func:`_verify_estimate`'s shape; unlike that one, ``candidates``
    is already the exact priced-and-run set (the cap already applied), so
    there is no analogue of `probe_candidates` to select through here."""

    objects = {r.from_dataset for r in candidates} | {r.to_dataset for r in candidates}
    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not candidates:
        return 0.0, len(candidates), len(objects)
    total = sum(
        query_estimate(sql)
        for sql in rel_mod.overlap_sweep_statements(candidates, adapter.dialect)
    )
    return total, len(candidates), len(objects)


def _cumulative_candidates(
    datasets: list[Dataset],
) -> tuple[list[tuple[Dataset, cumulative_mod.CumulativeCandidate]], list[str]]:
    """Every dataset with an eligible entity/temporal/measure shape, paired
    with the one candidate :func:`cumulative.find_candidate` chose for it, plus
    the notes (skips, untested alternatives) each dataset contributed."""

    candidates: list[tuple[Dataset, cumulative_mod.CumulativeCandidate]] = []
    notes: list[str] = []
    for dataset in datasets:
        candidate, dataset_notes = cumulative_mod.find_candidate(dataset)
        notes.extend(f"{dataset.identifier}: {note}" for note in dataset_notes)
        if candidate is not None:
            candidates.append((dataset, candidate))
    return candidates, notes


def _cumulative_estimate(
    adapter: Adapter,
    candidates: list[tuple[Dataset, cumulative_mod.CumulativeCandidate]],
) -> tuple[float, int, int]:
    """Free dry-run pricing of the window-function probes ``--check-cumulative``
    would run, plus the candidate/object counts for the checkpoint payload;
    zero-cost on free adapters or when nothing qualifies to probe.

    Mirrors :func:`_verify_estimate`'s shape so the two opt-in probe phases
    report cost the same way."""

    objects = {dataset.identifier for dataset, _ in candidates}
    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not candidates:
        return 0.0, len(candidates), len(objects)
    total = sum(
        query_estimate(sql)
        for sql in cumulative_mod.probe_statements(candidates, adapter.dialect)
    )
    return total, len(candidates), len(objects)


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
            config.clickhouse.dev_database if config.clickhouse else None,
        ]
        if name
    )


def inventory(engine: DexEngine, *, rank: bool = False) -> InventoryResult:
    """Every object the connection can see, metadata only.

    Free on every connector (catalog reads, not scans), so it never needs the
    confirm handshake and is the cheapest way to find out what is out there.
    ``rank`` orders by the same signals ``map`` uses, minus connectivity: there
    is no relationship pass here, so only naming, size, and shape contribute.
    """

    adapter = engine._adapter("explore inventory")
    metas = inventory_mod.inventory(adapter)
    cost = command_args.preflight_cost(adapter)

    if rank:
        # Honor the same configured ranking_hints as `map`; without them, a
        # ranked inventory would silently ignore the user's bias.
        scores = rank_mod.rank(metas, None, engine.config.ranking_hints)
        metas = sorted(metas, key=lambda m: scores.get(m.identifier, 0.0), reverse=True)
    else:
        scores = {}

    return InventoryResult(
        objects=[
            InventoryEntry(
                identifier=m.identifier,
                object_type=m.object_type,
                row_estimate=m.row_count,
                column_count=m.column_count,
                rank_score=scores.get(m.identifier) if rank else None,
            )
            for m in metas
        ],
        ranked=rank,
        cost=cost,
    )


def cmd_inventory(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(inventory(engine, rank=getattr(args, "rank", False)))


class _ObjectGap:
    """What the exploration cache cannot yet say about the objects a command names.

    ``to_profile`` are real warehouse objects the cache cannot adjudicate: never
    profiled, inventoried without column detail, or profiled against a column
    signature the warehouse has since moved away from. ``absent`` are names the
    connection does not have at all, each paired with the reason the live listing
    settled on. ``resolved`` maps every name as written to the identifier it
    turned out to mean.

    Empty on both counts is the ordinary case and means the caller can proceed
    exactly as it did before any of this existed.
    """

    def __init__(
        self,
        to_profile: list[str],
        absent: list[tuple[str, str]],
        resolved: dict[str, str],
    ) -> None:
        self.to_profile = to_profile
        self.absent = absent
        self.resolved = resolved

    def refusal(self) -> str:
        """The message for names this connection does not have.

        Separated by verdict because they are different problems with different
        fixes: a namespace the connection cannot reach at all is a wiring mistake,
        while a name absent from a namespace that *was* listed is a typo or a model
        nobody built yet. Naming the exploration cache here would be the wrong fix
        entirely, since no amount of profiling puts an absent object into it.
        """

        by_verdict = {
            verdict: sorted({n for n, v in self.absent if v == verdict})
            for verdict in ("foreign", "missing", "unlisted")
        }
        if by_verdict["foreign"]:
            named = ", ".join(by_verdict["foreign"])
            return (
                f"'{named}' is in a namespace this connection does not reach; "
                "point dex at the connection that carries it, or qualify the name "
                "against the one you are connected to"
            )
        if by_verdict["missing"]:
            named = ", ".join(by_verdict["missing"])
            return (
                f"'{named}' is not in this connection: its namespace was listed "
                "and the relation was not in it. Check the name, or build it into "
                "the target you are querying"
            )
        named = ", ".join(by_verdict["unlisted"])
        return (
            f"no object named '{named}' in this connection; check the name, build "
            "it into the target you are querying, or qualify it if it lives "
            "outside the scoped namespaces"
        )


def _object_gap(
    adapter: Adapter, prior: DexCache | None, named: list[str]
) -> _ObjectGap:
    """Ask what would have to be profiled before a cache-resolved guard can run.

    Free on every connector and it executes no SQL: object listing and per-object
    column metadata are catalog reads everywhere, and on Databricks they come from
    the REST catalog rather than the SQL warehouse, so this never wakes one.

    The cache is a fast path and the *connection* is the authority. That ordering
    is the whole point: an object built minutes ago is in the warehouse and cannot
    be in a cache written before it existed, so concluding "no such object" from a
    cache miss would refuse exactly the relations an agent most wants to probe
    right after building them. The live listing is only consulted for what the
    cache could not resolve, so the ordinary case never reaches it.

    Deliberately *not* :func:`_split_fresh_stale`, which gates a profile the caller
    asked for and therefore also asks how old it is and which connector wrote it.
    The question here is narrower and it is the guard's question, not profiling's:
    can this cache entry decide what the guard is about to decide? Two things say
    no. An entry with no column detail cannot, and neither can one whose columns no
    longer match the warehouse, since flags describing a shape the object has moved
    away from are not flags for that object. Age says nothing about either, and
    re-scanning on it would bill a probe for statistics nothing is about to read.

    Anything unsettled is left alone rather than guessed at: a listing that cannot
    be read, a metadata call that fails, an ambiguous name, and a name
    :func:`~..cache.relation_verdict` has no opinion on all fall through to
    whatever the guard would have said anyway. Nothing here invents a refusal, and
    nothing here bills for a doubt.
    """

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    cached = {d.identifier: d for d in prior.datasets} if prior is not None else {}
    for name in named:
        matches = match_identifier(name, list(cached))
        if len(matches) == 1:
            resolved[name] = matches[0]
        else:
            unresolved.append(name)

    absent: list[tuple[str, str]] = []
    if unresolved:
        try:
            live = [meta.identifier for meta in adapter.list_objects()]
        except Exception:
            live = None
        if live is not None:
            for name in unresolved:
                matches = match_identifier(name, live)
                if len(matches) == 1:
                    resolved[name] = matches[0]
                elif not matches:
                    # An unqualified name is one dex resolves by suffix across
                    # everything it can see, so nothing matching means nothing it
                    # can see matches, which is the answer `explore profile`
                    # already gives for an unknown argument. A *qualified* name in
                    # a namespace the listing never covered stays unadjudicated:
                    # refusing it would answer a question dex never asked.
                    verdict = relation_verdict(name, live)
                    if verdict is None and "." not in name:
                        verdict = "unlisted"
                    if verdict is not None:
                        absent.append((name, verdict))

    to_profile: list[str] = []
    for identifier in sorted(set(resolved.values())):
        entry = cached.get(identifier)
        if entry is None or not entry.columns:
            to_profile.append(identifier)
            continue
        try:
            _meta, columns = adapter.table_metadata(identifier)
        except Exception:  # noqa: S112 -- an unreadable signature settles nothing
            continue
        if _column_signature(columns) != _column_signature(entry.columns):
            to_profile.append(identifier)
    return _ObjectGap(to_profile, absent, resolved)


def _profile_into_cache(
    store: ExploreStore,
    adapter: Adapter,
    config: DexConfig,
    defs,
    identifiers: list[str],
    prior: DexCache | None,
    now: datetime,
) -> tuple[list[Dataset], DexCache, str, str]:
    """Scan the named objects and fold the profiles into the cache.

    The billed half of profiling, shared by every command that profiles: the
    deliberate one, and the two that profile on demand so a guard has flags to
    read. Call it only after the cost handshake has admitted the work; it prices
    nothing itself.

    Returns the fresh profiles, the merged cache as written, its locator, and the
    one sentence saying what the write did. The cache is saved before the caller
    does anything else with it, because a scan the caller paid for must survive
    whatever happens next, including a refusal.

    Per-object checkpointing runs only on billed connectors: a free connector can
    never exhaust a budget mid-scan and its re-runs cost nothing, so the extra
    full-document writes would buy nothing.
    """

    checkpoint = None
    accumulated: list[Dataset] = []
    if command_args.cost_gate(adapter) is not None:
        checkpoint, accumulated = _profile_checkpointer(store, prior, adapter.name, now)
    reporter = _reporter(len(identifiers), "profiled", "objects")
    try:
        profiled = profile_mod.profile(
            adapter,
            identifiers,
            progress=reporter,
            on_complete=checkpoint,
            pii_overrides=pii_override_paths(config.pii_overrides),
            include_blobs=blob_override_paths(config.blob_overrides),
        )
        reporter.done()
    except OverCeilingError:
        raise _budget_exhausted(store, adapter, accumulated, len(identifiers)) from None

    _annotate_grain(profiled, defs)
    cache, stats = _merge_profiles(prior, profiled, adapter.name, now)
    locator = store.save_cache(cache, now=now)
    note = _persist_note(stats, len(profiled), keeps_relationships=True)
    return profiled, cache, locator, note


def profile(
    engine: DexEngine,
    objects: list[str],
    *,
    refresh: bool = False,
    use_project: bool = False,
    check_cumulative: bool = False,
) -> ProfileResult:
    """Profile the named objects, reusing fresh cached profiles where they exist.

    Raises :class:`~..results.ConfirmationRequired` on a billed connector before
    anything is scanned. ``refresh`` forces a re-scan of objects the cache would
    otherwise serve; ``use_project`` folds the dbt project's declared joins,
    grain, and metric lineage into the result. With ``check_cumulative``, every
    profiled dataset with an eligible entity/temporal shape is probed for
    measures that look like a running total or point-in-time snapshot; this
    probe is priced only after profiling knows what to test, which is why it
    can come back as ``pending_confirmation`` rather than raising.
    """

    store = engine.store
    config = engine.config
    defs = _project_definitions(engine, use_project)
    blob_paths = blob_override_paths(config.blob_overrides)
    adapter = engine._adapter("explore profile")
    # Capture pre-run cache state before any checkpoint write, so the success-path
    # compose reads the pre-run cache rather than a checkpoint this run wrote.
    now = datetime.now(UTC)
    prior = readable_cache(store)

    identifiers = _resolve_identifiers(adapter, objects)
    connector = adapter.name
    # Skip re-scanning a requested object whose cached profile is still fresh
    # (same connector, schema unchanged, within the freshness window); only the
    # stale remainder is estimated, confirmed, and profiled below. A profile
    # written seconds earlier by `explore map` is served free, the same reuse
    # `map` and `relationships` already honor.
    stale, fresh_reused = _split_fresh_stale(
        identifiers,
        prior,
        connector,
        adapter,
        timedelta(hours=config.profile_freshness_hours),
        now,
        refresh=refresh,
    )
    estimate, per_table = _profile_estimate(adapter, stale, include_blobs=blob_paths)
    handshake_notes = None
    if fresh_reused:
        handshake_notes = [
            f"{len(fresh_reused)} object(s) excluded from this estimate as "
            "fresh-cached (schema unchanged, profiled within the freshness "
            "window); pass --refresh to re-profile them"
        ]
    # Nothing stale means nothing to price or confirm: skip the handshake and
    # serve the cached profiles wholesale. The cache write below still happens,
    # so a no-op profile refreshes provenance rather than looking like a failure.
    if stale:
        command_args.billed_handshake(
            "explore profile",
            adapter,
            estimate,
            per_table=per_table,
            notes=handshake_notes,
        )
    # Persist what the scan already paid for: after profiling a table, `explore
    # query` on that table must work without a second warehouse scan. Only the
    # freshly profiled are merged; the reused already live in the cache untouched,
    # and prior relationships are preserved because profile runs no inference pass.
    profiled, cache, locator, persist_note = _profile_into_cache(
        store, adapter, config, defs, stale, prior, now
    )

    # Freshly profiled plus fresh-cached: the full requested set, whether scanned
    # this run or served from the cache. Only the freshly profiled needed
    # annotation; the fresh-cached carry their keys and grain from the write that
    # stored them.
    datasets = profiled + list(fresh_reused.values())

    notes = [persist_note]
    notes.extend(_override_notes(datasets))
    if fresh_reused:
        window = config.profile_freshness_hours
        notes.append(
            f"reused {len(fresh_reused)} fresh cached profile(s) (schema "
            f"unchanged, profiled within {window:g}h); pass --refresh to force "
            "re-profiling"
        )

    # Cumulative-measure check: opt-in and priced only once profiling knows
    # what to test, so it runs after the base profile is already saved and can
    # come back pending rather than discarding it.
    cumulative_pending: ConfirmationRequest | None = None
    if check_cumulative:
        candidates, candidate_notes = _cumulative_candidates(datasets)
        notes.extend(candidate_notes)
        if candidates:
            probe_cost, candidate_count, object_count = _cumulative_estimate(
                adapter, candidates
            )
            cumulative_pending = command_args.cumulative_handshake(
                "explore profile",
                adapter,
                probe_cost,
                candidate_count=candidate_count,
                object_count=object_count,
            )
            if cumulative_pending is None:
                # A fresh-cached dataset here is `_split_fresh_stale`'s deep
                # copy, not the object `cache.datasets` carries forward, so a
                # note appended only to `dataset` would show in this result
                # but never reach the save below. Resolve the cache's own
                # object by identifier and append there too, guarding against
                # a double append on the freshly profiled path, where they are
                # the same object.
                cache_by_id = {d.identifier: d for d in cache.datasets}
                measured = 0
                try:
                    for dataset, candidate in candidates:
                        fractions = cumulative_mod.measure_fractions(
                            adapter,
                            dataset.identifier,
                            candidate,
                            timeout_seconds=config.query.timeout_seconds,
                        )
                        cache_entry = cache_by_id.get(dataset.identifier)
                        for text in cumulative_mod.cumulative_measure_notes(
                            candidate, fractions
                        ):
                            # Idempotent: re-running the check on a dataset a
                            # prior run already flagged must not pile up the
                            # same sentence again.
                            if text not in dataset.data_quality:
                                dataset.data_quality.append(text)
                            if (
                                cache_entry is not None
                                and cache_entry is not dataset
                                and text not in cache_entry.data_quality
                            ):
                                cache_entry.data_quality.append(text)
                            notes.append(f"{dataset.identifier}: {text}")
                        measured += 1
                except OverCeilingError:
                    notes.append(
                        f"budget exhausted after checking {measured} of "
                        f"{len(candidates)} candidate(s) for cumulative "
                        "measures; checked results are saved; raise --budget "
                        "and re-run to finish (a re-run re-profiles first)"
                    )
                finally:
                    locator = store.save_cache(cache, now=now)
            else:
                notes.append(
                    "cumulative-measure check saved unverified; the check "
                    "awaits confirmation (see hint)"
                )

    result = ProfileResult(
        datasets=datasets,
        profiled_count=len(profiled),
        cache_hit_count=len(fresh_reused),
        cache_path=locator,
        updated_at=now.isoformat(),
        notes=notes,
        warnings=_override_mismatches(datasets, config.pii_overrides),
        pending_confirmation=cumulative_pending,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_profile(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(
        profile(
            engine,
            args.objects,
            refresh=getattr(args, "refresh", False),
            use_project=getattr(args, "use_project", False),
            check_cumulative=getattr(args, "check_cumulative", False),
        )
    )


def relationships(
    engine: DexEngine,
    *,
    verify: bool = False,
    infer_by_overlap: bool = False,
    refresh: bool = False,
    use_project: bool = False,
) -> RelationshipsResult:
    """Infer joins across every object in scope, optionally probing them.

    Inference needs uniqueness signals, so this profiles the full inventory
    first; on a metered connector ``map`` (top-ranked objects only) is usually
    the cheaper way in. With ``verify``, candidate joins are probed for real
    overlap, priced only after inference knows what the candidates are, which is
    why that phase can come back as ``pending_confirmation`` rather than raising.
    With ``infer_by_overlap``, key-shaped columns no name-based rule matched are
    swept for measured value containment (issue #220), a second and
    independent opt-in phase with the same priced-after-the-fact shape,
    which runs after ``verify`` so at most one checkpoint is ever pending at
    once.
    """

    store = engine.store
    config = engine.config
    defs = _project_definitions(engine, use_project)
    catalog = _semantic_catalog(engine, use_project)

    adapter = engine._adapter("explore relationships")
    # Capture pre-run cache state before any checkpoint write, so the success-path
    # compose reads the pre-run cache rather than a checkpoint this run wrote.
    now = datetime.now(UTC)
    prior = readable_cache(store)
    accumulated: list[Dataset] = []
    over_ceiling = False
    verify_pending: ConfirmationRequest | None = None
    verify_warning: str | None = None
    overlap_pending: ConfirmationRequest | None = None
    overlap_warning: str | None = None
    warnings: list[str] = []

    # Relationship inference needs uniqueness signals, so profile every object
    # first (free and local on DuckDB), then infer across the full set.
    metas = inventory_mod.inventory(adapter)
    connector = adapter.name
    # See map()'s identical computation: tells carry-forward "out of this
    # run's scope" apart from "gone from the warehouse" (issue #149).
    observed_namespaces = {
        m.identifier.rpartition(".")[0].lower() for m in metas if "." in m.identifier
    }
    # Skip re-scanning objects whose cached profile is still fresh (same
    # connector, schema unchanged, within the freshness window); only the stale
    # remainder is estimated, confirmed, and profiled below.
    stale, fresh_reused = _split_fresh_stale(
        [m.identifier for m in metas],
        prior,
        connector,
        adapter,
        timedelta(hours=config.profile_freshness_hours),
        now,
        refresh=refresh,
    )
    blob_paths = blob_override_paths(config.blob_overrides)
    estimate, per_table = _profile_estimate(adapter, stale, include_blobs=blob_paths)
    handshake_notes = [
        "relationship inference profiles every object; on a metered "
        "connector `explore map` (top-ranked objects only) is usually the "
        "cheaper way in"
    ]
    if verify:
        handshake_notes.append(
            "--verify overlap probes depend on what inference finds; they "
            "are priced after profiling, and if their estimate exceeds "
            "what remains of this budget a second confirmation checkpoint "
            "appears before any probe runs"
        )
    if fresh_reused:
        handshake_notes.append(
            f"{len(fresh_reused)} object(s) excluded from this estimate as "
            "fresh-cached (schema unchanged, profiled within the freshness "
            "window); pass --refresh to re-profile them"
        )
    profiled: list[Dataset] = []
    # Nothing stale means nothing to price or confirm: skip the handshake and
    # the scan entirely, and reuse the cached profiles wholesale.
    if stale:
        command_args.billed_handshake(
            "explore relationships",
            adapter,
            estimate,
            per_table=per_table,
            notes=handshake_notes,
        )

        # Billed-connector-gated per-object checkpointing (see profile).
        checkpoint = None
        if command_args.cost_gate(adapter) is not None:
            checkpoint, accumulated = _profile_checkpointer(
                store, prior, connector, now
            )

        profile_reporter = _reporter(len(stale), "profiled", "objects")
        try:
            profiled = profile_mod.profile(
                adapter,
                stale,
                progress=profile_reporter,
                on_complete=checkpoint,
                pii_overrides=pii_override_paths(config.pii_overrides),
                include_blobs=blob_paths,
            )
            profile_reporter.done()
        except OverCeilingError:
            over_ceiling = True

    if over_ceiling:
        raise _budget_exhausted(store, adapter, accumulated, len(stale))

    # Freshly profiled plus fresh-cached: the full inventory, whether scanned
    # this run or reused. Inference and merge fold by identifier over the union.
    datasets = profiled + list(fresh_reused.values())
    suppressed: list[rel_mod.SuppressedMatch] = []
    affix_matches: list[rel_mod.AffixMatch] = []
    inferred = rel_mod.infer_relationships(
        datasets,
        suppressed=suppressed,
        affixes=config.entity_affixes,
        affix_matches=affix_matches,
    )

    # Annotate before persisting so cached datasets carry candidate_keys and
    # grain, the same shape a `map`-written cache has. Only the freshly profiled
    # need it; the fresh-cached already carry theirs from the cache write.
    _annotate_grain(profiled, defs)

    # Fold same-lineage duplicates before the merge, as `map` does, so the folded
    # set flows into both the cache and the result. Relationships profiles the
    # full inventory, so it is even more likely than map to pull a dev/replica
    # schema into scope alongside its source.
    inferred, folded_edges, mirrored_objects = rel_mod.fold_replica_relationships(
        datasets, inferred, _dev_schemas(config)
    )

    identifiers = [d.identifier for d in datasets]
    declared, declared_notes = rel_mod.declared_relationships(defs, identifiers)
    semantic_edges, semantic_edge_notes = _semantic_edges(catalog, identifiers)
    declared, semantic_already_declared = _fold_semantic_edges(declared, semantic_edges)
    rels, confirmed = _merge_relationships(declared, inferred)

    # A prior overlap-derived edge (issue #220) is never rediscovered by
    # plain inference, so unlike everything else here it must be carried
    # forward unconditionally rather than only when out of scope; see
    # `_carry_forward_overlap_edges`. Before --verify, so a re-confirmed edge
    # is eligible for re-verification too, and before the sweep below, so it
    # never re-probes (and re-pays for) a pair already confirmed.
    rels, overlap_carried = _carry_forward_overlap_edges(
        prior, connector, {d.identifier for d in datasets}, rels
    )

    # Verify the *merged* set, not the inferred one. Declared joins are probe
    # candidates now (issue #163) and are only born at the merge above, so
    # verifying earlier would price and measure a set that excludes exactly the
    # joins a cooperative project cares most about. Merging first also means no
    # measurement can be lost to the "declared wins over the same inferred edge"
    # rule: at merge time nothing has been measured yet.
    if verify:
        probe_cost, candidates, objects = _verify_estimate(adapter, rels)
        verify_pending = command_args.verify_handshake(
            "explore relationships",
            adapter,
            probe_cost,
            candidate_count=candidates,
            object_count=objects,
        )
        if verify_pending is None:
            probed = rel_mod.probe_candidates(rels)
            verify_reporter = _reporter(len(probed), "verified", "joins")
            try:
                rel_mod.verify_relationships(
                    adapter,
                    rels,
                    timeout_seconds=config.query.timeout_seconds,
                    progress=verify_reporter,
                )
                verify_reporter.done()
            except OverCeilingError:
                # Estimate drift mid-loop; the relationship set itself is
                # complete, so finish with a warning (see map).
                done = sum(1 for r in probed if r.verified)
                verify_warning = (
                    f"budget exhausted after verifying {done} of "
                    f"{len(probed)} candidate join(s); verified "
                    "results are saved; raise --budget and re-run to "
                    "finish verification"
                )

    # The overlap sweep (issue #220): key-shaped columns no name-based rule
    # matched, probed for real value containment. Runs after --verify, on the
    # same merged set (`rels` now also carries anything --verify measured),
    # so at most one of the two phases is ever pending confirmation at once
    # -- a verify checkpoint the caller hasn't resolved yet defers the sweep
    # rather than pricing a second, simultaneous ask.
    overlap_proposed = 0
    overlap_rejected = 0
    overlap_elided = 0
    overlap_deferred = infer_by_overlap and verify_pending is not None
    if infer_by_overlap and verify_pending is None:
        matched = {
            (rel.from_dataset.lower(), col.lower())
            for rel in rels
            for col in rel.from_columns
        } | {
            (rel.to_dataset.lower(), col.lower())
            for rel in rels
            for col in rel.to_columns
        }
        overlap_candidates, overlap_elided, overlap_cap = (
            rel_mod.overlap_sweep_candidates(datasets, matched)
        )
        if overlap_candidates:
            overlap_cost, candidate_count, object_count = _overlap_estimate(
                adapter, overlap_candidates
            )
            overlap_pending = command_args.overlap_handshake(
                "explore relationships",
                adapter,
                overlap_cost,
                candidate_count=candidate_count,
                object_count=object_count,
                cap=overlap_cap,
                elided=overlap_elided,
            )
            if overlap_pending is None:
                overlap_reporter = _reporter(
                    len(overlap_candidates), "swept", "candidates"
                )
                try:
                    overlap_rejected = rel_mod.probe_overlap_candidates(
                        adapter,
                        overlap_candidates,
                        timeout_seconds=config.query.timeout_seconds,
                        progress=overlap_reporter,
                    )
                    overlap_reporter.done()
                except OverCeilingError:
                    # Mutation is in place (see probe_overlap_candidates), so
                    # whatever it decided before the ceiling hit survives on
                    # overlap_candidates regardless of this raise; only
                    # candidates the ceiling cut off before they were probed
                    # at all are simply absent from both counts below.
                    overlap_warning = (
                        "budget exhausted mid-sweep; a candidate not yet "
                        "probed when the ceiling hit is neither proposed nor "
                        "reported as rejected, since it was never measured. "
                        "Relationships proposed before the ceiling are "
                        "saved; raise --budget and re-run to finish the sweep"
                    )
                proposed = [rel for rel in overlap_candidates if rel.verified]
                rels = rels + proposed
                overlap_proposed = len(proposed)

    # Prior relationships are only reusable when they came from the same
    # connector, same as the profiles below.
    reusable = prior if prior and prior.provenance.connector == connector else None
    examined = {d.identifier for d in datasets}
    rels, carried_relationships = _carry_forward_relationships(
        reusable, examined, rels, observed_namespaces=observed_namespaces
    )
    notes = _relationship_notes(datasets, declared, inferred, defs)
    notes.extend(declared_notes)
    notes.extend(semantic_edge_notes)
    notes.extend(
        _semantic_join_notes(semantic_edges, semantic_already_declared, inferred)
    )
    notes.extend(defs.notes)
    if confirmed:
        notes.append(
            f"{confirmed} inferred join(s) match declared tests; kept as declared"
        )
    if verify and inferred and verify_pending is None and verify_warning is None:
        notes.append(
            f"verified {len(inferred)} inferred join(s) with aggregate overlap probes"
        )
    # A verified join at a catastrophic orphan rate is a finding, not just a
    # demoted confidence (issue #207): named here so a caller reading only
    # notes sees it, and mirrored onto the child dataset's data_quality so
    # it survives into the cache for anything reading profiles later.
    orphan_findings = rel_mod.orphan_findings(rels)
    notes.extend(text for _rel, text in orphan_findings)
    datasets_by_id = {d.identifier: d for d in datasets}
    for rel, text in orphan_findings:
        child = datasets_by_id.get(rel.from_dataset)
        if child is not None:
            child.data_quality.append(text)
    if fresh_reused:
        window = config.profile_freshness_hours
        notes.append(
            f"reused {len(fresh_reused)} fresh cached profile(s) (schema "
            f"unchanged, profiled within {window:g}h); pass --refresh to force "
            "re-profiling"
        )
    if verify_pending is not None:
        notes.append(
            "relationships saved unverified; verification awaits confirmation "
            "(see hint)"
        )
    if verify_warning:
        warnings.append(verify_warning)
    if folded_edges > 0:
        notes.append(
            f"folded {folded_edges} same-lineage duplicate relationship(s); "
            f"{mirrored_objects} object(s) mirror source lineage (a dev/replica "
            "dataset mapped alongside its source)"
        )
    notes.extend(_generic_name_notes(suppressed))
    notes.extend(_affix_match_notes(affix_matches))
    if carried_relationships > 0:
        notes.append(
            f"carried forward {carried_relationships} prior relationship(s) "
            "with an endpoint this run did not profile or reuse fresh (a "
            "--scope/--dataset narrower than a prior run)"
        )
    notes.extend(
        _overlap_sweep_notes(
            infer_by_overlap=infer_by_overlap,
            deferred=overlap_deferred,
            pending=overlap_pending,
            proposed=overlap_proposed,
            rejected=overlap_rejected,
            elided=overlap_elided,
            carried=overlap_carried,
        )
    )
    if overlap_warning:
        warnings.append(overlap_warning)

    # Persist the profiles this run already paid for. Relationships inventories
    # and profiles the full set and infers across all of it, so its relationship
    # set is authoritative for every identifier it examined; anything with an
    # endpoint outside that (a narrower --scope/--dataset than a prior run) is
    # carried forward above rather than dropped, same as the profiles are.
    cache, stats = _merge_profiles(prior, datasets, connector, now, relationships=rels)
    if catalog is not None:
        _annotate_semantic_exposure(cache.datasets, catalog)
    locator = store.save_cache(cache, now=now)
    notes.append(_persist_note(stats, len(datasets), keeps_relationships=False))

    result = RelationshipsResult(
        relationships=rels,
        declared_count=len(declared),
        semantic_join_count=len(semantic_edges),
        profiled_count=len(profiled),
        cache_hit_count=len(fresh_reused),
        carried_relationship_count=carried_relationships,
        cache_path=locator,
        updated_at=now.isoformat(),
        notes=notes,
        warnings=warnings,
        # `verify`'s checkpoint always resolves first (the sweep defers
        # behind it, see `overlap_deferred`), so at most one of the two is
        # ever non-None at once; the ternary is defensive, not load-bearing.
        pending_confirmation=verify_pending
        if verify_pending is not None
        else overlap_pending,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_relationships(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(
        relationships(
            engine,
            verify=getattr(args, "verify", False),
            infer_by_overlap=getattr(args, "infer_by_overlap", False),
            refresh=getattr(args, "refresh", False),
            use_project=getattr(args, "use_project", False),
        )
    )


@dataclass
class _Statement:
    """One statement on its way through a call, and what became of it.

    ``error`` holds the exception object rather than its message because the
    single-statement door re-raises it: a rebuilt exception would lose the type
    the caller branches on and the ``from`` chain a refusal carries.
    """

    index: int
    sql: str
    line: int | None = None
    status: str = "ok"
    inspected: InspectedQuery | None = None
    result: QueryResult | None = None
    error: Exception | None = None

    @property
    def live(self) -> bool:
        return self.error is None and self.status == "ok"

    def stop(self, exc: Exception, status: str) -> None:
        self.error = exc
        self.status = status


@dataclass
class _Batch:
    """What the statements of one call share: a connection, a scan, a spend."""

    adapter: Adapter | None = None
    profiled: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def query(
    engine: DexEngine, sql: str, *, auto_profile: bool | None = None
) -> QueryResult:
    """Run one caller-authored SELECT through the query firewall.

    The single-statement door onto :func:`query_batch`'s runner, and it stays a
    door rather than a second implementation so the envelope a lone statement
    produces cannot drift from the one it produced before batching existed. A
    refusal is raised here rather than reported, because with one statement there
    is no surviving neighbor for a status field to protect.
    """

    batch, shared = _run_statements(engine, [(sql, None)], auto_profile=auto_profile)
    statement = batch[0]
    if statement.error is not None:
        raise statement.error

    record = statement.result
    command_args.stamp_spend(record, shared.adapter)
    if shared.profiled and command_args.cost_gate(shared.adapter) is not None:
        record.notes.append(
            f"the spend reported here covers profiling {len(shared.profiled)} "
            "object(s) and running the query"
        )
    return record


def query_batch(
    engine: DexEngine,
    statements: list[tuple[str, int | None]] | list[str],
    *,
    auto_profile: bool | None = None,
) -> QueryBatchResult:
    """Run several caller-authored SELECTs through the query firewall, in order.

    An agent asking a chain of small questions is the common case, and each call
    it spends on dex is one it did not spend on the task. What batching buys is
    call count and nothing else: every statement is parsed, adjudicated, and
    ledgered on its own, and statements are never joined into one string, so a
    batch has exactly the reach a sequence of single calls had.

    What it does share is the expensive part. The objects the whole call needs
    profiled are resolved as one set and scanned once, and the spend is quoted
    once, so two statements over the same cold table pay for one scan and one
    handshake rather than two of each.

    A statement's own refusal is reported against that statement rather than
    raised, because raising would discard results the caller has already been
    billed for. Whole-call refusals (an unconfirmed spend, an absent cache) still
    raise: nothing ran, so there is nothing to protect.
    """

    prepared = [s if isinstance(s, tuple) else (s, None) for s in statements]
    batch, shared = _run_statements(engine, prepared, auto_profile=auto_profile)
    result = QueryBatchResult(
        results=[
            QueryStatementResult(
                index=statement.index,
                status=statement.status,
                line=statement.line,
                error=(
                    None
                    if statement.error is None
                    else env.redact(str(statement.error))
                ),
                reason=(
                    None
                    if statement.error is None
                    else env.reason_for(statement.error).value
                ),
                **(
                    {}
                    if statement.result is None
                    else {
                        "columns": statement.result.columns,
                        "types": statement.result.types,
                        "cells": statement.result.cells,
                        "row_count": statement.result.row_count,
                        "truncated": statement.result.truncated,
                        "tables": statement.result.tables,
                        "notes": statement.result.notes,
                        "warnings": statement.result.warnings,
                    }
                ),
            )
            for statement in batch
        ],
        profiled_on_demand=shared.profiled,
        warnings=shared.warnings,
    )
    if shared.adapter is not None:
        command_args.stamp_spend(result, shared.adapter)
        if shared.profiled and command_args.cost_gate(shared.adapter) is not None:
            result.notes.append(
                f"the spend reported here covers profiling {len(shared.profiled)} "
                f"object(s) and running {len(batch)} statement(s)"
            )
    return result


def _run_statements(
    engine: DexEngine,
    statements: list[tuple[str, int | None]],
    *,
    auto_profile: bool | None = None,
) -> tuple[list[_Statement], _Batch]:
    """Drive every statement of one call through the firewall and the warehouse.

    The firewall adjudicates against the exploration cache, and that is not a
    formality: the PII policy it applies is computed from what profiling flagged,
    so a query cannot be judged against a warehouse nobody has profiled. What used
    to follow from that, sending the caller away to run a command whose exact
    argument this function was already holding, does not. An object the connection
    has but the cache cannot speak for is profiled here and now, and the statement
    proceeds under the flags that scan produced.

    Profiling costs money on a metered connector, so it is priced, not implied:
    the estimate covers the profiles and the statements together and one handshake
    admits them all. ``auto_profile=False`` (``--no-auto-profile``, or
    ``auto_profile: false`` in config) restores the strict prerequisite, and on
    that path nothing here touches the connection before the firewall has spoken.

    Every decision, allowed or refused, lands in the query ledger, one entry per
    statement whatever the call carried.
    """

    store = engine.store
    config = engine.config
    limits = config.query
    now = datetime.now(UTC)
    at = now.isoformat()
    cache = readable_cache(store)
    # The firewall parses in the active connector's dialect, so BigQuery SQL
    # (backticks, COUNTIF) is inspected as BigQuery, not as DuckDB.
    dialect = get_dialect(engine.connector or config.connector)
    auto = config.auto_profile if auto_profile is None else auto_profile

    batch = [_Statement(i, sql, line) for i, (sql, line) in enumerate(statements)]
    size = len(batch)
    shared = _Batch()

    def ledger(statement: _Statement, entry: dict) -> None:
        store.append_query_log({"at": at, **entry, **_batch_marks(statement, size)})

    if auto:
        # Read-only first, before anything reaches the warehouse. Resolving a
        # relation introspects the live connection, and no agent-authored
        # statement earns that until it is proven to be a single SELECT.
        reads: dict[int, list[str]] = {}
        for statement in batch:
            try:
                assert_query_shape(statement.sql, dialect=dialect)
            except QueryRefusedError as exc:
                statement.stop(exc, "refused")
                ledger(
                    statement,
                    {
                        "sql": statement.sql,
                        "decision": "refused",
                        "reason": str(exc),
                    },
                )
                continue
            reads[statement.index] = referenced_relations(
                statement.sql, dialect=dialect
            )

        named = list(dict.fromkeys(name for names in reads.values() for name in names))
        if named:
            try:
                shared.adapter = engine._adapter("explore query")
            except DexError:
                # No connection settles nothing, exactly as an unreadable column
                # signature settles nothing inside `_object_gap`. That tolerance
                # already exists one level down; it was missing here, at the
                # acquisition, and the difference is not cosmetic: the guard below
                # decides from cached PII flags and needs no warehouse at all, so
                # letting an absent connector stop it turns a policy decision into
                # a connectivity one and closes the firewall in exactly the
                # offline environments that cannot bill for a mistake.
                #
                # Nothing is swallowed. A statement that PASSES the guard reaches
                # the same opener below and raises there, which is where a caller
                # who is about to run SQL wants to hear that the warehouse is
                # unreachable. Only a refusal now returns without it.
                shared.adapter = None
        if shared.adapter is not None:
            gap = _object_gap(shared.adapter, cache, named)
            if gap.absent:
                # Only the statements that named a missing object are refused; a
                # neighbor that reads a table this connection has is unaffected by
                # one that does not.
                missing = {name for name, _ in gap.absent}
                for statement in batch:
                    if not statement.live or missing.isdisjoint(reads[statement.index]):
                        continue
                    exc = QueryRefusedError(gap.refusal())
                    statement.stop(exc, "refused")
                    ledger(
                        statement,
                        {
                            "sql": statement.sql,
                            "decision": "refused",
                            "reason": str(exc),
                        },
                    )
            live = [statement for statement in batch if statement.live]
            # Never scan for a statement that is already refused: the objects worth
            # profiling are the ones a statement that can still run will read.
            wanted = {name for s in live for name in reads[s.index]}
            to_profile = [
                identifier
                for identifier in gap.to_profile
                if not wanted.isdisjoint(_names_for(identifier, gap.resolved))
            ]
            if to_profile and live:
                cache, shared.profiled, shared.warnings = _profile_for_statements(
                    engine, shared.adapter, live, to_profile, cache, now, at, size
                )

    if cache is None and any(statement.live for statement in batch):
        raise CacheRequiredError(
            "no exploration cache yet; run `explore map` first so the query "
            "firewall knows the schema and the PII flags"
        )

    if cache is not None:
        # After the cache write, never before: _mask_overridden edits the datasets
        # in place, and masking first would persist an in-memory override onto
        # objects this run never re-profiled.
        cache = _mask_overridden(cache, pii_override_paths(config.pii_overrides))
    for statement in batch:
        if not statement.live:
            continue
        try:
            statement.inspected = inspect_query(
                statement.sql, cache, limits, dialect=dialect
            )
        except QueryRefusedError as exc:
            entry = {
                "sql": statement.sql,
                "decision": "refused",
                "reason": str(exc),
            }
            if shared.profiled:
                entry["profiled_on_demand"] = shared.profiled
            ledger(statement, entry)
            # A caller who paid for a scan is told what they got, even when the
            # guard then refuses: the profile is cached and the next attempt
            # reuses it.
            if shared.profiled:
                wrapped = QueryRefusedError(
                    f"{exc}. {_profiled_names_phrase(shared.profiled)} profiled "
                    "before this refusal; the profile is cached, so a corrected "
                    "query does not pay for it again"
                )
                # `raise ... from exc` in one line, split because the exception is
                # stored and re-raised by the caller rather than raised here.
                wrapped.__cause__ = exc
                exc = wrapped
            statement.stop(exc, "refused")

    live = [statement for statement in batch if statement.live]
    if not live:
        return batch, shared

    shared.adapter = shared.adapter or engine._adapter("explore query")
    # Priced once, above, when a profile was needed: `preflight_command` sets the
    # reservation rather than adding to it, so a second handshake here would
    # release the profile's booking. Every statement still passes the
    # per-statement gate and the server-side cap on its way out.
    if not shared.profiled:
        _price_statements(shared.adapter, live, size, ledger)

    _execute(engine, shared, batch, limits, ledger)
    return batch, shared


def _price_statements(
    adapter: Adapter,
    live: list[_Statement],
    size: int,
    ledger: Callable[[_Statement, dict], None],
) -> None:
    """One handshake for the whole call, itemized per statement.

    Summed rather than charged one at a time so the ceiling binds on what the call
    will actually spend: a caller confirming a batch is confirming all of it, and
    a sequence of per-statement asks would let the third statement discover a
    budget the first two had already eaten.
    """

    query_estimate = getattr(adapter, "query_estimate", None)
    per_statement = {
        _statement_label(statement, size): (
            query_estimate(statement.inspected.sql) if query_estimate else 0.0
        )
        for statement in live
    }
    estimate = sum(per_statement.values())
    try:
        command_args.billed_handshake(
            "explore query",
            adapter,
            estimate,
            per_table=per_statement if size > 1 else None,
        )
    except ConfirmationRequiredError:
        for statement in live:
            ledger(
                statement,
                {
                    "sql": statement.inspected.sql,
                    "decision": "needs_confirmation",
                    "estimated_bytes": per_statement[_statement_label(statement, size)]
                    if size > 1
                    else estimate,
                },
            )
        raise


def _execute(
    engine: DexEngine,
    shared: _Batch,
    batch: list[_Statement],
    limits: QueryLimits,
    ledger: Callable[[_Statement, dict], None],
) -> None:
    """Run each approved statement in order, against one shared payload budget.

    The byte cap exists to keep a result from flooding agent context, so it is the
    call's budget rather than each statement's: ten statements under a per-statement
    cap would emit ten times what one is allowed. It is spent in statement order and
    what one statement leaves unspent the next may use, which is why a lone
    statement still sees the whole of it and behaves exactly as it always did.

    A cost-guard refusal partway through stops the call rather than being retried
    per statement: the budget is gone, so every statement after it would meet the
    same wall. What already ran is kept and reported, because it has been paid for.
    """

    budget = limits.max_payload_bytes
    stopped: Exception | None = None
    for statement in batch:
        if not statement.live:
            continue
        if stopped is not None:
            statement.stop(stopped, "skipped")
            continue
        try:
            rows = shared.adapter.run_query(
                statement.inspected.sql,
                max_rows=statement.inspected.row_cap,
                timeout_seconds=limits.timeout_seconds,
            )
        except ConfirmationRequiredError:
            raise
        except Exception as exc:
            if isinstance(exc, OverCeilingError):
                exc = _with_batch_shortfall(exc, shared.adapter, batch, statement)
            entry = {
                "sql": statement.inspected.sql,
                "decision": "failed",
                "reason": env.redact(str(exc)),
            }
            if shared.profiled:
                entry["profiled_on_demand"] = shared.profiled
            ledger(statement, entry)
            statement.stop(exc, "failed")
            if isinstance(exc, (CostGuardError, BudgetExhaustedError)):
                stopped = exc
            continue

        payload = _shape_query_payload(
            rows,
            statement.inspected,
            limits,
            budget_bytes=None if len(batch) == 1 else budget,
        )
        budget -= payload.pop("payload_bytes")
        notes = payload.pop("notes")
        statement.result = QueryResult(
            **payload,
            profiled_on_demand=shared.profiled,
            notes=notes,
            # A lone statement carries the call's own warnings, because there is no
            # batch record above it to hold them.
            warnings=[
                *(shared.warnings if len(batch) == 1 else []),
                *statement.inspected.warnings,
            ],
        )
        entry = {
            "sql": statement.inspected.sql,
            "decision": "allowed",
            "tables": statement.result.tables,
            "row_count": statement.result.row_count,
            "truncated": statement.result.truncated,
        }
        if shared.profiled:
            entry["profiled_on_demand"] = shared.profiled
        # The audit trail records which allowed queries projected sub-threshold
        # PII-flagged columns, so a later review can find every such projection.
        if statement.inspected.warnings:
            entry["pii_warnings"] = statement.inspected.warnings
        ledger(statement, entry)


def _with_batch_shortfall(
    exc: OverCeilingError,
    adapter: Adapter,
    batch: list[_Statement],
    statement: _Statement,
) -> OverCeilingError:
    """Name exactly how much more this batch needs to finish, not just why
    this one statement hit the wall (issue #321).

    BigQuery bills at least its per-query floor no matter how little a
    statement reads, so an N-statement call needs at least ``N x`` that floor
    of headroom; that arithmetic is fully knowable the moment inference finds
    the remaining statements, which is exactly what this recomputes. A caller
    who otherwise has to raise ``--budget`` and guess again now sees the
    number the first guess would have needed. Already-completed statements
    are unaffected: their results are kept exactly as :func:`_execute`
    already keeps them, so a wider budget on re-run does not re-pay for them.

    A no-op (returns ``exc`` unchanged) for a lone, non-batch statement,
    where "the remaining budget is below the floor" already says everything
    there is to say, and when the adapter cannot price a dry run at all.
    """

    if len(batch) == 1:
        return exc
    remaining = [s for s in batch if s.index >= statement.index and s.live]
    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not remaining:
        return exc
    extra = sum(query_estimate(s.inspected.sql) for s in remaining)
    gate = command_args.cost_gate(adapter)
    unit = gate.paradigm.value if gate is not None else "bytes"
    wrapped = OverCeilingError(
        f"{exc}. Completing the remaining {len(remaining)} statement(s) in "
        f"this batch needs at least {extra:,.0f} more {unit}; the "
        "statement(s) already run above are saved, so a wider --budget on "
        "re-run does not re-pay for them",
        cost=exc.cost,
    )
    wrapped.__cause__ = exc
    return wrapped


def _batch_marks(statement: _Statement, size: int) -> dict:
    """Where a ledger line sat in its call, when the call carried more than one.

    Absent for a lone statement, so a single-statement ledger line is byte for
    byte what it always was; present otherwise, so an auditor can see that six
    statements were one authorization event rather than six.
    """

    if size == 1:
        return {}
    return {"batch_index": statement.index, "batch_size": size}


def _statement_label(statement: _Statement, size: int) -> str:
    if size == 1:
        return "(the statement itself)"
    return f"(statement {statement.index + 1})"


def _names_for(identifier: str, resolved: dict[str, str]) -> set[str]:
    """Every name a caller could have written for one resolved identifier."""

    return {
        identifier,
        *(name for name, target in resolved.items() if target == identifier),
    }


def _profiled_names_phrase(names: list[str]) -> str:
    if len(names) == 1:
        return f"'{names[0]}' was"
    return f"{len(names)} object(s) ({', '.join(names)}) were"


def _profile_for_statements(
    engine: DexEngine,
    adapter: Adapter,
    live: list[_Statement],
    to_profile: list[str],
    prior: DexCache | None,
    now: datetime,
    at: str,
    size: int,
) -> tuple[DexCache, list[str], list[str]]:
    """Price and run the profiles a call needs, then hand back the new cache.

    One handshake covers the profile scans and the statements themselves, because
    the caller made one request and should be quoted one number for it. Statements
    are priced as written rather than as the firewall will rewrite them: the
    rewrite needs a cache that can resolve these very objects, which is what this
    call is about to create. Nothing is lost by it, since a row cap does not
    reduce the bytes a scan reads or the seconds a warehouse runs.

    Returns the merged cache, the objects profiled, and the disclosure the result
    carries. Every refusal on the way out is ledgered first, so an unconfirmed ask
    is as findable in the audit trail as a spend.
    """

    store = engine.store
    config = engine.config
    profile_estimate, per_table = _profile_estimate(
        adapter, to_profile, include_blobs=blob_override_paths(config.blob_overrides)
    )
    query_estimate = getattr(adapter, "query_estimate", None)
    per_statement = {
        _statement_label(statement, size): (
            query_estimate(statement.sql) if query_estimate else 0.0
        )
        for statement in live
    }
    statement_estimate = sum(per_statement.values())
    subject = "this statement reads" if size == 1 else "these statements read"
    try:
        command_args.billed_handshake(
            "explore query",
            adapter,
            profile_estimate + statement_estimate,
            per_table={**per_table, **per_statement},
            notes=[
                f"{len(to_profile)} object(s) {subject} have no usable "
                "profile, and the firewall reads PII flags from the cache; this "
                "estimate covers profiling them and then running the statement. "
                "Pass --no-auto-profile to be refused instead of profiling"
            ],
        )
    except ConfirmationRequiredError:
        for statement in live:
            store.append_query_log(
                {
                    "at": at,
                    "sql": statement.sql,
                    "decision": "needs_confirmation",
                    "estimated_bytes": profile_estimate + statement_estimate,
                    "profile_planned": to_profile,
                    **_batch_marks(statement, size),
                }
            )
        raise

    _profiled, cache, _locator, _note = _profile_into_cache(
        store,
        adapter,
        config,
        _project_definitions(engine, False),
        to_profile,
        prior,
        now,
    )
    return cache, to_profile, _auto_profile_warning(to_profile, adapter)


def _auto_profile_warning(names: list[str], adapter: Adapter) -> list[str]:
    """The disclosure that a scan happened which the caller did not ask for.

    A warning rather than a note: this is a spend and an authorization event, not
    a remark about the shape of the answer. It says what the profile is worth,
    because the whole reason the guard can proceed is that this profile is the one
    a deliberate ``explore profile`` would have written, not a weaker stand-in.
    """

    if not names:
        return []
    billed = (
        " The scan is included in this command's spend."
        if command_args.cost_gate(adapter) is not None
        else ""
    )
    return [
        f"profiled {len(names)} object(s) on demand: {', '.join(names)}. They are "
        "in the warehouse but the .dex/ cache could not speak for them, and the "
        "firewall reads PII flags from that cache. These are full profiles (same "
        "detection, same overrides, same cache write) and they are saved, so the "
        f"next statement over them profiles nothing.{billed} Pass "
        "--no-auto-profile to be refused instead of profiled"
    ]


def cmd_query(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    statements = _statements_from(args, engine)
    if len(statements) == 1:
        try:
            return to_envelope(
                query(engine, statements[0][0], auto_profile=_auto_profile(args))
            )
        except QueryRefusedError as exc:
            return env.error_for(exc, f"query refused: {exc}")
    return _batch_envelope(
        query_batch(engine, statements, auto_profile=_auto_profile(args))
    )


def _statements_from(
    args: argparse.Namespace, engine: DexEngine
) -> list[tuple[str, int | None]]:
    """Where this call's statements came from: argv, or a file, never both.

    Refusing the overlap rather than concatenating keeps the numbering a refusal
    reports against unambiguous, and there is no question a caller can only ask by
    mixing the two.
    """

    # A bare string is one statement, not a list of characters. argparse always
    # hands over a list, but a namespace built by an embedding host need not, and
    # the failure mode of getting this wrong is silent and absurd.
    named = getattr(args, "sql", None)
    positional = [named] if isinstance(named, str) else list(named or [])
    path = getattr(args, "sql_file", None)
    if positional and path:
        raise RequestError(
            "pass statements as arguments or as --sql-file, not both; the file "
            "and the arguments would have no defined order"
        )
    if not positional and not path:
        raise RequestError(
            'name at least one statement: dex explore query "<SELECT ...>" '
            "[more...], or --sql-file <path>"
        )

    if path:
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise RequestError(f"could not read --sql-file {path}: {exc}") from exc
        dialect = get_dialect(engine.connector or engine.config.connector)
        statements = split_statements(text, dialect=dialect)
    else:
        # Never comma-split, the way `explore profile` splits object names: SQL
        # carries commas of its own and one argument is one statement.
        statements = [(sql, None) for sql in positional]

    limit = engine.config.query.max_statements
    if len(statements) > limit:
        raise RequestError(
            f"{len(statements)} statements in one call exceeds the "
            f"query.max_statements limit of {limit}; split the call, or raise the "
            "limit in .dex/config.yml"
        )
    return statements


def _batch_envelope(result: QueryBatchResult) -> env.Envelope:
    """One envelope for a call that answered several statements.

    A statement that failed makes the envelope's own status ``error`` even when
    its neighbors answered, and every answer stays in ``data``. Reporting ``ok``
    over a guard refusal would be the weaker claim, and dropping the answers to
    report the refusal would discard work already paid for; `transform build`
    resolves the same tension the same way.
    """

    envelope = to_envelope(result)
    failures = [statement for statement in result.results if statement.status != "ok"]
    if not failures:
        return envelope
    return env.Envelope(
        status=env.Status.ERROR,
        data=envelope.data,
        cost=envelope.cost,
        warnings=envelope.warnings,
        errors=[
            f"statement {statement.index + 1} {statement.status}: {statement.error}"
            for statement in failures
        ],
        reason=env.Reason(failures[0].reason) if failures[0].reason else None,
    )


def _auto_profile(args: argparse.Namespace) -> bool | None:
    """``False`` when --no-auto-profile was passed, else None to defer to config.

    None rather than True so an absent flag cannot override a repo that turned the
    behavior off; the flag is an off switch, never an on switch.
    """

    return False if getattr(args, "no_auto_profile", False) else None


def map(
    engine: DexEngine,
    *,
    full: bool = False,
    detail: bool = False,
    verify: bool = False,
    infer_by_overlap: bool = False,
    refresh: bool = False,
    use_project: bool = False,
) -> MapResult:
    """The whole landscape in one pass: inventory, ranked profiling, and joins.

    Note this shadows the builtin ``map`` for the rest of this module, which is
    the price of every run function being named for its subcommand. Use a
    comprehension rather than the builtin below this line.

    The default is selective: on a warehouse above the auto-profile threshold
    only the top-ranked objects are deep-profiled and the rest stay
    inventory-only, because profiling everything on a metered connector is how a
    first look becomes an expensive one. ``full`` overrides that.

    With ``infer_by_overlap``, key-shaped columns no name-based rule matched
    are swept for measured value containment (issue #220), the same opt-in,
    priced-after-the-fact phase :func:`relationships` runs, in the same slot
    after ``verify``.
    """

    store = engine.store
    config = engine.config
    defs = _project_definitions(engine, use_project)
    catalog = _semantic_catalog(engine, use_project)
    hints = _merged_hints(config.ranking_hints, defs.metric_models)

    adapter = engine._adapter("explore map")
    # Capture pre-run cache state before any checkpoint write, so the success-path
    # compose reads the pre-run cache rather than a checkpoint this run wrote.
    now = datetime.now(UTC)
    prior = readable_cache(store)
    accumulated: list[Dataset] = []
    over_ceiling = False
    verify_pending: ConfirmationRequest | None = None
    verify_warning: str | None = None
    overlap_pending: ConfirmationRequest | None = None
    overlap_warning: str | None = None
    warnings: list[str] = []

    metas = inventory_mod.inventory(adapter)
    orphaned = _orphan_candidates(metas, defs)
    # The schema/dataset namespaces this run's inventory actually saw, so
    # carry-forward can tell "out of this run's scope" from "gone from the
    # warehouse" (issue #149) -- an identifier whose own namespace is here but
    # who is itself absent from metas was looked at and is not there.
    observed_namespaces = {
        m.identifier.rpartition(".")[0].lower() for m in metas if "." in m.identifier
    }
    # First-pass rank on cheap signals (no connectivity yet) to choose what to
    # profile; re-ranked with connectivity once relationships are known.
    first_pass = rank_mod.rank(metas, None, hints, orphaned)
    selected = _select_for_profiling(metas, first_pass, config, full)
    # Skip re-scanning a selected object whose cached profile is still fresh
    # (same connector, schema unchanged, within the freshness window); only
    # the stale remainder is estimated, confirmed, and profiled below.
    stale, fresh_reused = _split_fresh_stale(
        [m.identifier for m in selected],
        prior,
        adapter.name,
        adapter,
        timedelta(hours=config.profile_freshness_hours),
        now,
        refresh=refresh,
    )
    # Inventory and ranking are free, so an unconfirmed billed run repeats
    # them on re-issue; only the profiling scans below need the handshake.
    blob_paths = blob_override_paths(config.blob_overrides)
    estimate, per_table = _profile_estimate(adapter, stale, include_blobs=blob_paths)
    handshake_notes: list[str] = []
    if verify:
        handshake_notes.append(
            "--verify overlap probes depend on what inference finds; they "
            "are priced after profiling, and if their estimate exceeds "
            "what remains of this budget a second confirmation checkpoint "
            "appears before any probe runs"
        )
    if fresh_reused:
        handshake_notes.append(
            f"{len(fresh_reused)} object(s) excluded from this estimate as "
            "fresh-cached (schema unchanged, profiled within the freshness "
            "window); pass --refresh to re-profile them"
        )
    profiled: list[Dataset] = []
    # Nothing stale means nothing to price or confirm: skip the handshake and
    # the scan entirely, and reuse the cached profiles wholesale.
    if stale:
        command_args.billed_handshake(
            "explore map",
            adapter,
            estimate,
            per_table=per_table,
            notes=handshake_notes or None,
        )
        # Billed-connector-gated per-object checkpointing (see profile).
        checkpoint = None
        if command_args.cost_gate(adapter) is not None:
            checkpoint, accumulated = _profile_checkpointer(
                store, prior, adapter.name, now
            )

        profile_reporter = _reporter(len(stale), "profiled", "objects")
        try:
            profiled = profile_mod.profile(
                adapter,
                stale,
                progress=profile_reporter,
                on_complete=checkpoint,
                pii_overrides=pii_override_paths(config.pii_overrides),
                include_blobs=blob_paths,
            )
            profile_reporter.done()
        except OverCeilingError:
            over_ceiling = True

    if over_ceiling:
        raise _budget_exhausted(store, adapter, accumulated, len(stale))

    # Freshly profiled plus fresh-cached: the full selected set, whether
    # scanned this run or reused. Only the freshly profiled need annotation;
    # the reused already carry theirs from the cache write that stored them.
    all_selected = profiled + list(fresh_reused.values())
    _annotate_grain(profiled, defs, orphaned=orphaned)
    suppressed: list[rel_mod.SuppressedMatch] = []
    affix_matches: list[rel_mod.AffixMatch] = []
    inferred = rel_mod.infer_relationships(
        all_selected,
        suppressed=suppressed,
        affixes=config.entity_affixes,
        affix_matches=affix_matches,
    )

    # Fold same-lineage duplicates before they reach the cache: a dev/replica
    # dataset mapped alongside its source otherwise inflates one real foreign key
    # into source, replica, and cross-dataset lookalike edges.
    inferred, folded_edges, mirrored_objects = rel_mod.fold_replica_relationships(
        all_selected, inferred, _dev_schemas(config)
    )

    # Resolved against the full live inventory rather than the profiled subset:
    # a declared join between two objects the rank cutoff skipped is still a fact
    # about this warehouse, and both channels are read on the same terms.
    identifiers = [m.identifier for m in metas]
    declared, declared_notes = rel_mod.declared_relationships(defs, identifiers)
    semantic_edges, semantic_edge_notes = _semantic_edges(catalog, identifiers)
    declared, semantic_already_declared = _fold_semantic_edges(declared, semantic_edges)
    relationship_set, confirmed = _merge_relationships(declared, inferred)

    # A prior overlap-derived edge (issue #220) is never rediscovered by
    # plain inference, so it is carried forward unconditionally rather than
    # only when out of scope; see `_carry_forward_overlap_edges` and the
    # identical call in `relationships`. Checked against the full live
    # inventory (`metas`), not just `all_selected`, so an object merely
    # skipped by this run's rank cutoff still counts as known.
    relationship_set, overlap_carried = _carry_forward_overlap_edges(
        prior, adapter.name, {m.identifier for m in metas}, relationship_set
    )

    # Verify the merged set: declared joins are probe candidates (issue #163)
    # and only exist from the merge above. Same ordering as `relationships`.
    if verify:
        probe_cost, candidates, objects = _verify_estimate(adapter, relationship_set)
        verify_pending = command_args.verify_handshake(
            "explore map",
            adapter,
            probe_cost,
            candidate_count=candidates,
            object_count=objects,
        )
        if verify_pending is None:
            probed = rel_mod.probe_candidates(relationship_set)
            verify_reporter = _reporter(len(probed), "verified", "joins")
            try:
                rel_mod.verify_relationships(
                    adapter,
                    relationship_set,
                    timeout_seconds=config.query.timeout_seconds,
                    progress=verify_reporter,
                )
                verify_reporter.done()
            except OverCeilingError:
                # The upfront probe pricing fit, but per-statement estimates
                # drifted past the ceiling mid-loop. The map itself is
                # complete, so finish with a warning instead of the profiling
                # phase's partial-completion error.
                done = sum(1 for r in probed if r.verified)
                verify_warning = (
                    f"budget exhausted after verifying {done} of "
                    f"{len(probed)} candidate join(s); verified "
                    "results are saved; raise --budget and re-run to "
                    "finish verification"
                )

    # The overlap sweep (issue #220), same slot and same deferral rule as in
    # `relationships`: it runs after --verify, on the merged set, and defers
    # entirely behind a still-pending verify checkpoint so at most one of the
    # two phases is ever pending confirmation at once.
    overlap_proposed = 0
    overlap_rejected = 0
    overlap_elided = 0
    overlap_deferred = infer_by_overlap and verify_pending is not None
    if infer_by_overlap and verify_pending is None:
        matched = {
            (rel.from_dataset.lower(), col.lower())
            for rel in relationship_set
            for col in rel.from_columns
        } | {
            (rel.to_dataset.lower(), col.lower())
            for rel in relationship_set
            for col in rel.to_columns
        }
        overlap_candidates, overlap_elided, overlap_cap = (
            rel_mod.overlap_sweep_candidates(all_selected, matched)
        )
        if overlap_candidates:
            overlap_cost, candidate_count, object_count = _overlap_estimate(
                adapter, overlap_candidates
            )
            overlap_pending = command_args.overlap_handshake(
                "explore map",
                adapter,
                overlap_cost,
                candidate_count=candidate_count,
                object_count=object_count,
                cap=overlap_cap,
                elided=overlap_elided,
            )
            if overlap_pending is None:
                overlap_reporter = _reporter(
                    len(overlap_candidates), "swept", "candidates"
                )
                try:
                    overlap_rejected = rel_mod.probe_overlap_candidates(
                        adapter,
                        overlap_candidates,
                        timeout_seconds=config.query.timeout_seconds,
                        progress=overlap_reporter,
                    )
                    overlap_reporter.done()
                except OverCeilingError:
                    overlap_warning = (
                        "budget exhausted mid-sweep; a candidate not yet "
                        "probed when the ceiling hit is neither proposed nor "
                        "reported as rejected, since it was never measured. "
                        "Relationships proposed before the ceiling are "
                        "saved; raise --budget and re-run to finish the sweep"
                    )
                proposed = [rel for rel in overlap_candidates if rel.verified]
                relationship_set = relationship_set + proposed
                overlap_proposed = len(proposed)

    # Prior profiles are only reusable when they came from the same connector.
    reusable = prior if prior and prior.provenance.connector == adapter.name else None
    examined = {d.identifier for d in all_selected}
    relationship_set, carried_relationships = _carry_forward_relationships(
        reusable, examined, relationship_set, observed_namespaces=observed_namespaces
    )
    final_scores = rank_mod.rank(metas, relationship_set, hints, orphaned)
    datasets, carried, out_of_scope, dropped = _compose_datasets(
        metas,
        all_selected,
        final_scores,
        reusable,
        observed_namespaces=observed_namespaces,
    )

    # A verified join at a catastrophic orphan rate is a finding, not just a
    # demoted confidence (issue #207): mirrored onto the child dataset's
    # data_quality (persisted below) and surfaced in notes further down.
    orphan_findings = rel_mod.orphan_findings(relationship_set)
    datasets_by_id = {d.identifier: d for d in datasets}
    for rel, text in orphan_findings:
        child = datasets_by_id.get(rel.from_dataset)
        if child is not None:
            child.data_quality.append(text)

    # After `_compose_datasets` rather than beside `_annotate_grain`, and over the
    # composed set rather than this run's profiles: the link is derived from the
    # project, not from a scan, so an object this run declined to re-profile is
    # marked as accurately as one it just read.
    semantic_exposed = (
        _annotate_semantic_exposure(datasets, catalog) if catalog is not None else 0
    )

    cache = DexCache(datasets=datasets, relationships=relationship_set)
    cache.provenance.connector = adapter.name
    cache.provenance.created_at = (
        prior.provenance.created_at
        if prior and prior.provenance.created_at
        else now.isoformat()
    )
    locator = store.save_cache(cache, now=now)

    notes = _relationship_notes(all_selected, declared, inferred, defs)
    notes.extend(declared_notes)
    notes.extend(semantic_edge_notes)
    notes.extend(
        _semantic_join_notes(semantic_edges, semantic_already_declared, inferred)
    )
    if semantic_exposed:
        notes.append(
            f"{semantic_exposed} object(s) are exposed through the project's "
            "semantic layer (see semantic_models on each); the rest back no "
            "metric, which is what separates a load-bearing table from a large one"
        )
    notes.extend(defs.notes)
    notes.extend(text for _rel, text in orphan_findings)
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
    # Rank-cutoff skip is an axis of its own: objects not selected for profiling.
    # It is decoupled from len(profiled), which no longer equals len(selected)
    # once some selected objects are served from cache.
    skipped = len(metas) - len(selected)
    if skipped > 0:
        notes.append(
            f"profiled top {len(selected)} of {len(metas)} objects by rank "
            f"(profile_top_n={config.profile_top_n}; all objects are profiled "
            f"automatically at {_AUTO_PROFILE_ALL} or fewer); pass --full to "
            "profile everything"
        )
    if fresh_reused:
        window = config.profile_freshness_hours
        notes.append(
            f"reused {len(fresh_reused)} fresh cached profile(s) for selected "
            f"object(s) (schema unchanged, profiled within {window:g}h); pass "
            "--refresh to force re-profiling"
        )
    if carried > 0:
        notes.append(
            f"carried forward {carried} prior profile(s) for objects not "
            "re-profiled this run; per-dataset profiled_at marks their age"
        )
    if out_of_scope > 0:
        notes.append(
            f"carried forward {out_of_scope} prior profile(s) for object(s) "
            "outside this run's --scope/--dataset; the cache stays complete "
            "across every scope it has ever been mapped with, not just this one"
        )
    if carried_relationships > 0:
        notes.append(
            f"carried forward {carried_relationships} prior relationship(s) "
            "with an endpoint this run did not profile or reuse fresh"
        )
    if dropped:
        warnings.append(
            f"{len(dropped)} object(s) no longer in the warehouse were dropped "
            "from the cache rather than resurrected as carried-forward "
            f"profiles: {name_list(dropped)}"
        )
    if folded_edges > 0:
        notes.append(
            f"folded {folded_edges} same-lineage duplicate relationship(s); "
            f"{mirrored_objects} object(s) mirror source lineage (a dev/replica "
            "dataset mapped alongside its source)"
        )
    notes.extend(_generic_name_notes(suppressed))
    notes.extend(_affix_match_notes(affix_matches))
    if verify_pending is not None:
        notes.append(
            "relationships saved unverified; verification awaits confirmation "
            "(see hint)"
        )
    if verify_warning:
        warnings.append(verify_warning)
    notes.extend(
        _overlap_sweep_notes(
            infer_by_overlap=infer_by_overlap,
            deferred=overlap_deferred,
            pending=overlap_pending,
            proposed=overlap_proposed,
            rejected=overlap_rejected,
            elided=overlap_elided,
            carried=overlap_carried,
        )
    )
    if overlap_warning:
        warnings.append(overlap_warning)

    # Same ordering the payload's own selection uses: rank first, identifier
    # second. Two orderings derived from one cache inside one envelope would be a
    # bug, and rank alone does not break ties.
    top = sorted(datasets, key=lambda d: (-(d.rank_score or 0.0), d.identifier))[:5]
    view = summarize_map(cache, detail=detail)
    notes.extend(view.notes)
    result = MapResult(
        cache=cache,
        cache_path=locator,
        object_count=len(metas),
        profiled_count=len(profiled),
        cache_hit_count=len(fresh_reused),
        skipped_count=skipped,
        carried_forward_count=carried,
        out_of_scope_carried_count=out_of_scope,
        dropped_count=len(dropped),
        carried_relationship_count=carried_relationships,
        relationship_count=len(relationship_set),
        pii_column_count=sum(
            1 for d in all_selected for c in d.columns if c.pii is not None
        ),
        data_quality_note_count=sum(len(d.data_quality) for d in all_selected),
        top_objects=[
            RankedObject(identifier=d.identifier, rank_score=d.rank_score) for d in top
        ],
        objects=view.objects,
        edges=view.edges,
        elided_object_count=view.elided_object_count,
        elided_column_count=view.elided_column_count,
        elided_edge_count=view.elided_edge_count,
        updated_at=now.isoformat(),
        notes=notes,
        warnings=warnings,
        pending_confirmation=verify_pending
        if verify_pending is not None
        else overlap_pending,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_map(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(
        map(
            engine,
            full=getattr(args, "full", False),
            detail=getattr(args, "detail", False),
            verify=getattr(args, "verify", False),
            infer_by_overlap=getattr(args, "infer_by_overlap", False),
            refresh=getattr(args, "refresh", False),
            use_project=getattr(args, "use_project", False),
        )
    )


def cluster(
    engine: DexEngine,
    obj: str,
    *,
    features: list[str] | None = None,
    k: int | None = None,
    auto_profile: bool | None = None,
) -> ClusterResult:
    """k-means over a bounded sample of one object's numeric columns.

    Cache-gated like :func:`query`, and for the same reason: profiling is what
    says which columns are numeric and which are PII. Like ``query``, an object
    the connection has but the cache cannot speak for is profiled here rather than
    refused, and ``auto_profile=False`` restores the strict prerequisite.

    The pricing shape differs from ``query``'s and the difference is forced rather
    than chosen. A caller-authored statement can be priced before anything is
    profiled, so ``query`` quotes one number for the profile and the statement
    together. Clustering's sample statement does not exist until the profile does,
    because the feature columns are chosen from the column types, the PII flags,
    and the inferred keys. So the profile is priced first and the sample passes the
    mid-command gate afterward, which asks for a bigger budget rather than
    refusing; the re-run is cheap, since the profile it already paid for is cached.

    Only the feature columns are scanned, only a bounded sample is fetched into
    the process for scikit-learn, and only aggregates (cluster sizes and
    centroids) come back.
    """

    store = engine.store
    config = engine.config
    cache = readable_cache(store)
    now = datetime.now(UTC)
    auto = config.auto_profile if auto_profile is None else auto_profile

    # Fail fast if the [cluster] extra is missing: no connection, no spend.
    cluster_mod.ensure_available()

    adapter = None
    warnings: list[str] = []
    profiled_names: list[str] = []
    if auto:
        try:
            adapter = engine._adapter("explore cluster")
        except DexError:
            # The same tolerance the query path applies, at the second site the
            # object-gap probe was added to. `cluster` decides two things from
            # the cache alone -- "there is no cache" and "that object is not in
            # it" -- and both refusals sit below this acquisition, so gating it
            # here made them unreachable wherever no connector is installed.
            #
            # `auto_profile` defaults to True, so this is the ordinary path and
            # not an opt-in one; `--no-auto-profile` was the only way left to
            # reach either refusal offline. Nothing is swallowed: an object that
            # IS profiled still needs a connection to build its sample, reaches
            # the opener at the bottom of this function, and raises there.
            adapter = None
    if adapter is not None:
        gap = _object_gap(adapter, cache, [obj])
        if gap.absent:
            raise RequestError(gap.refusal())
        if gap.to_profile:
            profile_estimate, per_table = _profile_estimate(
                adapter,
                gap.to_profile,
                include_blobs=blob_override_paths(config.blob_overrides),
            )
            command_args.billed_handshake(
                "explore cluster",
                adapter,
                profile_estimate,
                per_table=per_table,
                notes=[
                    f"'{obj}' has no usable profile, and clustering picks its "
                    "features from the column types, the PII flags, and the keys "
                    "profiling finds; this estimate covers profiling it. The "
                    "sample scan is priced once the features are known, and asks "
                    "again only if it does not fit the confirmed budget. Pass "
                    "--no-auto-profile to be refused instead of profiling"
                ],
            )
            _profiled, cache, _locator, _note = _profile_into_cache(
                store,
                adapter,
                config,
                _project_definitions(engine, False),
                gap.to_profile,
                cache,
                now,
            )
            profiled_names = gap.to_profile
            warnings = _auto_profile_warning(profiled_names, adapter)

    if cache is None:
        raise CacheRequiredError(
            "no exploration cache yet; run `explore map` (or `explore profile "
            "<object>`) first so clustering knows which columns are numeric and "
            "which are PII"
        )

    limits = config.cluster
    known = [d.identifier for d in cache.datasets if d.columns]
    matches = match_identifier(obj, known)
    if not matches:
        raise CacheRequiredError(
            f"'{obj}' is not a profiled object in the exploration cache; run "
            f"`explore profile {obj}` (or `explore map`) first"
        )
    if len(matches) > 1:
        raise RequestError(f"'{obj}' is ambiguous: {', '.join(matches)}; qualify it")
    dataset = next(d for d in cache.datasets if d.identifier == matches[0])

    feature_names, selection_notes = _select_cluster_features(
        dataset, features, limits.max_features, cache.relationships
    )

    adapter = adapter or engine._adapter("explore cluster")
    sample_sql, sample_method = cluster_mod.build_sample_sql(
        dataset.identifier,
        feature_names,
        dialect=adapter.dialect,
        sample_rows=limits.sample_rows,
        row_count=dataset.row_count,
        seed=limits.sample_seed,
    )
    null_count_sql = cluster_mod.build_null_count_sql(
        dataset.identifier,
        feature_names,
        dialect=adapter.dialect,
        sample_rows=limits.sample_rows,
        row_count=dataset.row_count,
        seed=limits.sample_seed,
    )
    repeatable = cluster_mod.sample_is_repeatable(adapter.dialect, limits.sample_seed)
    adapter_name = adapter.name
    query_estimate = getattr(adapter, "query_estimate", None)
    sample_estimate = query_estimate(sample_sql) if query_estimate else 0.0
    null_count_estimate = query_estimate(null_count_sql) if query_estimate else 0.0
    sample_notes = [
        f"clusters a sample of up to {limits.sample_rows} rows over "
        f"{len(feature_names)} feature column(s); sampling: {sample_method}; "
        "a companion count query measures how many rows the null filter "
        "excludes, over the same table and sample scope"
    ]
    if profiled_names:
        # The profile already went through this command's one handshake, so the
        # sample is a phase whose price only became knowable after that spend.
        # A phase gate asks for more budget instead of refusing, and the profile
        # it already paid for is cached, so the re-run only runs the sample.
        pending = command_args.sample_handshake(
            "explore cluster",
            adapter,
            sample_estimate + null_count_estimate,
            notes=sample_notes,
        )
        if pending is not None:
            return ClusterResult(
                object=dataset.identifier,
                total_rows=dataset.row_count,
                notes=selection_notes,
                warnings=warnings,
                pending_confirmation=pending,
            )
    else:
        command_args.billed_handshake(
            "explore cluster",
            adapter,
            sample_estimate + null_count_estimate,
            notes=sample_notes,
        )
    sample = adapter.run_query(
        sample_sql,
        max_rows=limits.sample_rows,
        timeout_seconds=limits.timeout_seconds,
    )
    null_counts = adapter.run_query(
        null_count_sql, max_rows=1, timeout_seconds=limits.timeout_seconds
    )

    clustering = cluster_mod.cluster_features(
        feature_names,
        sample.cells,
        k=k,
        k_min=limits.k_min,
        k_max=limits.k_max,
        silhouette_sample=limits.silhouette_sample,
        random_state=limits.random_state,
    ).to_data()
    notes = [*selection_notes, *clustering.pop("notes", [])]
    if sample.truncated:
        notes.append(
            f"the sample hit the {limits.sample_rows}-row cap (the table has more "
            "rows); raise cluster.sample_rows in .dex/config.yml to widen it"
        )
    if not repeatable and dataset.row_count and dataset.row_count > limits.sample_rows:
        notes.append(
            f"this sample is not reproducible on {adapter_name}: re-running can "
            "draw different rows and reach a different k. Compare runs only with "
            "an identical sample"
        )
    dropped_null_rows, drop_note = _null_drop_note(feature_names, null_counts.cells)
    if drop_note:
        notes.append(drop_note)
    result = ClusterResult(
        object=dataset.identifier,
        total_rows=dataset.row_count,
        dropped_null_rows=dropped_null_rows,
        sample_method=sample_method,
        sample_repeatable=repeatable,
        clustering=clustering,
        profiled_on_demand=profiled_names,
        notes=notes,
        warnings=warnings,
    )
    return command_args.stamp_spend(result, adapter)


def _null_drop_note(
    feature_names: list[str], cells: list[list]
) -> tuple[int | None, str | None]:
    """Turn the null-count query's one row into a count and, when anything was
    actually dropped, an attributed note (which feature(s) caused it) plus the
    reminder that ``total_rows`` is cache-derived, not this run's live count --
    the only two numbers a reader could otherwise subtract come from different
    moments."""

    if not cells:
        return None, None
    sampled, dropped, *per_feature = cells[0]
    dropped = int(dropped or 0)
    if dropped == 0:
        return 0, None
    sampled = int(sampled or 0)
    contributors = sorted(
        (
            (name, int(count or 0))
            for name, count in zip(feature_names, per_feature, strict=True)
            if count
        ),
        key=lambda kv: -kv[1],
    )
    breakdown = ", ".join(f"{name}: {count}" for name, count in contributors)
    fraction = f" ({dropped / sampled:.1%})" if sampled else ""
    return dropped, (
        f"the null filter excluded {dropped} row(s){fraction} of this scan's "
        f"scope, missing at least one feature; by column: {breakdown}. "
        "total_rows above is cache-derived (from the last explore map/profile), "
        "a different moment than this live count"
    )


def diagram(engine: DexEngine, *, full: bool = False) -> DiagramResult:
    """Serialize the cached map as a Mermaid ER diagram.

    Free everywhere and on every connector: it reads the exploration cache and
    nothing else, so it opens no connection, needs no credential, and cannot
    spend. That is what makes it safe to re-run while iterating on a diagram,
    and it is why there is no confirm handshake here.

    Cache-gated like :func:`query` and :func:`cluster`, for a different reason:
    those need the cache to know what is safe to touch, this one needs it
    because the cache *is* the subject. An empty cache is a prerequisite
    failure naming `explore map`, never an empty diagram, because a diagram of
    nothing and a diagram of an unexplored warehouse look identical.
    """

    cache = readable_cache(engine.store)
    if cache is None:
        raise CacheRequiredError(
            "no exploration cache yet; run `explore map` first so there is a "
            "map to draw"
        )

    rendered = diagram_mod.render_er_mermaid(cache, full=full)
    if not rendered.entity_count:
        raise CacheRequiredError(
            "the exploration cache holds no object that can be drawn: run "
            "`explore map` (or `explore profile <object>`) so objects carry "
            "profiles and inferred joins"
        )
    return DiagramResult(
        mermaid=rendered.mermaid,
        entities=rendered.entities,
        entity_count=rendered.entity_count,
        edge_count=rendered.edge_count,
        elided_entity_count=rendered.elided_entity_count,
        elided_column_count=rendered.elided_column_count,
        elided_edge_count=rendered.elided_edge_count,
        full=full,
        updated_at=cache.provenance.updated_at or "",
        notes=rendered.notes,
    )


def cmd_diagram(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(diagram(engine, full=getattr(args, "full", False)))
    except (CacheRequiredError, CacheUnreadableError, ValueError) as exc:
        return env.error_for(exc)


def cmd_cluster(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        return to_envelope(
            cluster(
                engine,
                args.object,
                features=_split_features(getattr(args, "features", None)),
                k=getattr(args, "k", None),
                auto_profile=_auto_profile(args),
            )
        )
    except (
        CacheRequiredError,
        CacheUnreadableError,
        ValueError,
        cluster_mod.ClusterError,
        cluster_mod.ClusterDependencyError,
    ) as exc:
        return env.error_for(exc)


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


_KEY_WORDS = ("id", "uuid", "guid", "key")
_ID_NAME_RE = re.compile(
    "|".join(
        (
            rf"(?:^|_)(?:{'|'.join(_KEY_WORDS)})$",
            rf"[a-z0-9](?:{'|'.join(w.capitalize() for w in _KEY_WORDS)})$",
            rf"(?:^|_)(?:{'|'.join(w.upper() for w in _KEY_WORDS)})$",
        )
    ),
    re.ASCII,
)


def _is_id_shaped(name: str) -> bool:
    """Whether a column name looks like a key, on word boundaries only."""

    return bool(_ID_NAME_RE.search(name))


def _foreign_key_columns(
    dataset: Dataset, relationships: list[Relationship]
) -> set[str]:
    """Lower-cased columns of ``dataset`` that join out to another object.

    Only the ``from`` side is a foreign key; the ``to`` side is the referenced
    key, which auto-selection already drops via its own uniqueness. Joins are
    what `explore map` inferred, so this costs no extra scan.
    """

    identifier = dataset.identifier.lower()
    return {
        col.lower()
        for rel in relationships
        if rel.from_dataset.lower() == identifier
        for col in rel.from_columns
    }


def _select_cluster_features(
    dataset: Dataset,
    requested: list[str] | None,
    max_features: int,
    relationships: list[Relationship] | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve the feature columns for clustering plus notes explaining the set.

    Explicit ``--features`` are honored as given (validated numeric); a PII
    column may be named deliberately, and only its per-cluster mean, an
    aggregate, is ever reported. Auto-selection is conservative: numeric columns
    that are not PII-flagged, not a key, and not constant. A key is any of a
    proven unique column (the primary key), a column joining out to another
    object (a foreign key, per the joins `explore map` inferred), or a column
    named like one. A fact table is mostly foreign keys and a handful of
    measures, and clustering on the keys just partitions surrogate ranges.
    Raises ``ValueError`` with an actionable message when the request cannot be
    satisfied.
    """

    by_lower = {col.name.lower(): col for col in dataset.columns}
    notes: list[str] = []

    if requested is not None:
        chosen = []
        for name in requested:
            col = by_lower.get(name.lower())
            if col is None:
                raise RequestError(
                    f"column '{name}' is not among the profiled columns of "
                    f"{dataset.identifier}"
                )
            if not profile_mod.is_numeric_type(col.data_type):
                raise RequestError(
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
        rels = relationships or []
        fk_columns = _foreign_key_columns(dataset, rels)
        numeric = [
            c for c in dataset.columns if profile_mod.is_numeric_type(c.data_type)
        ]
        excluded_pii = [c.name for c in numeric if c.pii is not None]
        remaining = [c for c in numeric if c.pii is None]
        excluded_id = [c.name for c in remaining if c.is_unique is True]
        remaining = [c for c in remaining if c.is_unique is not True]
        excluded_fk = [c.name for c in remaining if c.name.lower() in fk_columns]
        remaining = [c for c in remaining if c.name.lower() not in fk_columns]
        excluded_named = [c.name for c in remaining if _is_id_shaped(c.name)]
        remaining = [c for c in remaining if not _is_id_shaped(c.name)]
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
        if excluded_fk:
            notes.append(
                f"excluded {len(excluded_fk)} foreign-key column(s) "
                f"({', '.join(excluded_fk)}) that join out to another object; a "
                "key is not a feature. Name one in --features to include it"
            )
        if excluded_named:
            notes.append(
                f"excluded {len(excluded_named)} column(s) named like a key "
                f"({', '.join(excluded_named)}); name one in --features to "
                "include it"
            )
        if not rels and (excluded_named or features):
            notes.append(
                "no relationships in the exploration cache, so key detection fell back "
                "to column names; run `explore relationships` (or `explore map`) "
                "for join-based detection"
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
        why = f" ({'; '.join(notes)})" if notes else ""
        raise RequestError(
            f"found {len(features)} usable numeric feature column(s) for "
            f"{dataset.identifier}; k-means needs at least 2. Pass --features to "
            f"choose columns, or profile a table with more numeric columns{why}"
        )
    return features, notes


def _shape_query_payload(
    result: QueryResult,
    inspected: InspectedQuery,
    limits: QueryLimits,
    *,
    budget_bytes: int | None = None,
) -> dict:
    """Cap the result for agent context: row-major cells, cell-width truncation,
    and a payload byte cap, each announced in `notes` so a cut result is never
    mistaken for a complete one.

    ``budget_bytes`` is what remains of a multi-statement call's shared budget,
    and None means this statement is the whole call and owns the configured cap.
    The distinction is in the note as well as the arithmetic, because "this
    result was too big" and "the statements before it had already spent the
    budget" are different problems with different fixes."""

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

    budget = limits.max_payload_bytes if budget_bytes is None else max(budget_bytes, 0)
    dropped = 0
    while cells and len(json.dumps(cells)) > budget:
        cells.pop()
        dropped += 1
    if dropped and budget_bytes is None:
        notes.append(
            f"dropped {dropped} row(s) to fit the {limits.max_payload_bytes}-byte "
            "payload cap; aggregate further or select fewer columns"
        )
    elif dropped:
        notes.append(
            f"dropped {dropped} row(s) to fit the {budget} bytes left of this "
            f"call's {limits.max_payload_bytes}-byte payload budget, which the "
            "statements before this one had already drawn on; aggregate further, "
            "select fewer columns, or ask fewer statements at once"
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
        "payload_bytes": len(json.dumps(cells)),
    }


def _project_definitions(
    engine: DexEngine, use_project: bool
) -> dbt_project.ProjectDefinitions:
    """The project's declared definitions, read through the tier-1 seam.

    Exploration starts bare: warehouse observations stay independent of
    whatever repo dex happens to run from, so the declared definitions fold in
    only when ``use_project`` asks for them. Without it, a present project earns
    a discovery note instead of influence. With it, a repo without a project (or
    with an ambiguous choice) degrades to the empty view, so explore keeps
    working on raw warehouses, and an engine with no repo root at all does the
    same rather than refusing.

    The discovery note is dbt-shaped and stays gated on the dbt format being the
    one selected. It looks for a ``dbt_project.yml`` on disk, which tells a host
    whose format is a graph in memory nothing true; a format asked whether it is
    "present but unused" would have to be read to answer, and reading it is
    precisely what this branch exists not to do.
    """

    repo_root = engine.repo_root
    if repo_root is None or not use_project:
        defs = dbt_project.ProjectDefinitions()
        discovered = (
            dbt_project.discover_projects(repo_root)
            if repo_root is not None and engine.config.project.format == "dbt"
            else []
        )
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
    # Through the seam rather than the module: this is the one project read on the
    # explore path, and `definitions()` is the channel declared keys and joins
    # reach dex through at all. Whichever format configuration named answers here.
    return engine.project_format().definitions()


def _semantic_catalog(engine: DexEngine, use_project: bool):
    """The project's semantic layer as a read catalog, or None.

    The optional channel beside tier 2, read the way :func:`_project_definitions`
    reads tier 1, and gated the same way: exploration starts bare, so a project's
    declared join graph and its physical exposure fold in only when
    ``--use-project`` asks for them. Without the flag the default map's payload is
    byte-identical to what it was.

    **None on every declinable condition, and it never raises**, which is the
    difference between this and calling ``semantic_catalog()`` directly. There is
    no repo, or the configured format does not read a semantic layer, or the
    project has no compiled semantic manifest yet: every one of those is an
    ordinary state on the explore path, where the warehouse is the subject and a
    project is a bonus. ``explore semantic list`` is the command whose whole
    subject *is* the layer, and it is the one that refuses by name instead.
    """

    from ..adapters.project import SemanticCatalogProject

    if engine.repo_root is None or not use_project:
        return None
    try:
        project = engine.project_format()
        if not isinstance(project, SemanticCatalogProject):
            return None
        return project.semantic_catalog()
    except (DexError, ValueError, OSError):
        return None


def _annotate_semantic_exposure(datasets: list[Dataset], catalog) -> int:
    """Mark each dataset with the semantic models that sit on it. Returns how many
    were exposed.

    Runs over the **composed** set rather than this run's profiles, unlike
    :func:`_annotate_grain`: the link is derived from the project rather than from
    a scan, so it costs nothing to apply to a carried-forward object, and leaving
    those unmarked would make the annotation read as "not exposed" for exactly the
    objects a narrower run declined to re-profile.

    Every dataset in view is written, empty list included, so a model dropped from
    the layer clears on the next run instead of persisting as a stale claim. That
    is only correct because the caller reaches this at all only when a catalog was
    actually read; with no catalog nothing here runs and the prior values stand.
    """

    known = [d.identifier for d in datasets]
    by_identifier: dict[str, set[str]] = {}
    for model in catalog.semantic_models:
        if not model.relation:
            continue
        # The same resolution a declared join's endpoints go through, and for the
        # same reason: a compiled manifest spells the database component the way
        # dbt was configured while the adapter normalizes it per connector, so an
        # exact compare would expose nothing on a project that works. A relation
        # matching several objects here resolves to none of them rather than to a
        # guess.
        identifier, _ambiguous = rel_mod.resolve_declared(
            model.relation, model.name, known
        )
        if identifier is not None:
            by_identifier.setdefault(identifier, set()).add(model.name)
    exposed = 0
    for dataset in datasets:
        models = sorted(by_identifier.get(dataset.identifier, ()))
        dataset.semantic_models = models
        exposed += bool(models)
    return exposed


def _semantic_edges(
    catalog, identifiers: list[str]
) -> tuple[list[Relationship], list[str]]:
    """The layer's declared entity graph as join edges, or nothing without one."""

    if catalog is None:
        return [], []
    return rel_mod.semantic_relationships(entity_joins(catalog), identifiers)


def _fold_semantic_edges(
    declared: list[Relationship], semantic: list[Relationship]
) -> tuple[list[Relationship], int]:
    """Semantic edges added to the declared set, deduped against it.

    Returns the widened declared set and how many semantic edges the project's
    ``relationships`` tests already stated. Both channels are declarations of the
    same tier, so an edge in both is one edge; which of the two named it first is
    not a fact about the warehouse, and doubling it would inflate the connectivity
    ranking the same way a doubled declared/inferred pair would.
    """

    known = {_relationship_edge_key(rel) for rel in declared}
    merged = list(declared)
    already = 0
    for rel in semantic:
        if _relationship_edge_key(rel) in known:
            already += 1
            continue
        known.add(_relationship_edge_key(rel))
        merged.append(rel)
    return merged, already


def _semantic_join_notes(
    semantic: list[Relationship], already_declared: int, inferred: list[Relationship]
) -> list[str]:
    """What the declared entity graph added, and specifically what it rescued.

    The count alone is not the interesting number. An edge the semantic layer
    declares and name-based inference did not find is a join that would otherwise
    be missing from the map entirely, with a key no naming rule could have matched,
    and that is the case worth naming out loud: it is the whole argument for
    reading the graph rather than scanning for it.
    """

    if not semantic:
        return []
    inferred_keys = {_relationship_edge_key(rel) for rel in inferred}
    missed = [
        rel for rel in semantic if _relationship_edge_key(rel) not in inferred_keys
    ]
    notes = [
        f"{len(semantic)} join(s) come from the semantic layer's declared entity "
        "graph, at the declared tier: the layer states the join and names its key "
        "per model, so these are read rather than inferred"
    ]
    if missed:
        named = ", ".join(
            sorted({rel.declared_by for rel in missed if rel.declared_by})[:5]
        )
        notes.append(
            f"{len(missed)} of them were not found by name-based inference and "
            f"would otherwise be missing from this map ({named})"
        )
    if already_declared:
        notes.append(
            f"{already_declared} of them are also declared by a relationships "
            "test; counted once"
        )
    return notes


def _relationship_edge_key(rel: Relationship) -> tuple:
    return (
        rel.from_dataset.lower(),
        tuple(c.lower() for c in rel.from_columns),
        rel.to_dataset.lower(),
        tuple(c.lower() for c in rel.to_columns),
    )


def _merge_relationships(
    declared: list[Relationship], inferred: list[Relationship]
) -> tuple[list[Relationship], int]:
    """Declared joins win over the same inferred edge.

    Returns the merged list plus how many inferred edges the declared set
    absorbed: inference independently agreeing with a declared test is worth a
    note, and double-reporting the edge would inflate connectivity ranking.
    """

    declared_keys = {_relationship_edge_key(rel) for rel in declared}
    merged = list(declared)
    confirmed = 0
    for rel in inferred:
        if _relationship_edge_key(rel) in declared_keys:
            confirmed += 1
            continue
        merged.append(rel)
    return merged, confirmed


def _carry_forward_relationships(
    prior: DexCache | None,
    examined: set[str],
    relationships: list[Relationship],
    *,
    observed_namespaces: set[str] | None = None,
) -> tuple[list[Relationship], int]:
    """Union back in any prior relationship this run could not possibly have
    regenerated or superseded: one with an endpoint outside ``examined`` (the
    identifiers this run actually profiled or reused fresh, not merely
    inventoried -- a `--scope`/`--dataset` narrower than what built the prior
    cache, or a rank-cutoff skip on a large `explore map`).

    Without this, a narrower run silently drops every cross-scope edge the
    prior cache held, the same loss `_compose_datasets` guards against for
    datasets (issue #111). Deduped against the newly built set by the same
    edge key `_merge_relationships` uses, so an edge both runs agree on is
    never doubled.

    ``observed_namespaces`` (see `_compose_datasets`) excludes an edge whose
    endpoint is gone from the warehouse rather than merely out of scope, so a
    deleted relation's join does not resurrect either (issue #149).
    """

    if prior is None or not prior.relationships:
        return relationships, 0
    existing = {_relationship_edge_key(rel) for rel in relationships}

    def dropped(identifier: str) -> bool:
        return (
            observed_namespaces is not None
            and identifier not in examined
            and identifier.rpartition(".")[0].lower() in observed_namespaces
        )

    carried = [
        rel
        for rel in prior.relationships
        if (rel.from_dataset not in examined or rel.to_dataset not in examined)
        and _relationship_edge_key(rel) not in existing
        and not dropped(rel.from_dataset)
        and not dropped(rel.to_dataset)
    ]
    if not carried:
        return relationships, 0
    return relationships + carried, len(carried)


def _carry_forward_overlap_edges(
    prior: DexCache | None,
    connector: str,
    known_identifiers: set[str],
    relationships: list[Relationship],
) -> tuple[list[Relationship], int]:
    """Union back in every prior ``OVERLAP_INFERRED`` edge whose endpoints
    are both still known objects on this connector (issue #220).

    The general :func:`_carry_forward_relationships` above only reaches back
    for an edge outside this run's examined scope, on the premise that
    anything in scope would be re-found this run if it still holds -- true
    for a declared join (re-read from the dbt project every run) and a
    name-inferred one (re-derived from cheap metadata every run), but false
    for an overlap-derived one: nothing rediscovers it except the opt-in,
    priced ``--infer-by-overlap`` sweep. Without this, an edge only that
    sweep can find would vanish the moment a caller runs a plain
    ``explore relationships``/``explore map``, which is a worse loss than
    the general function guards against, since here there is no cheaper path
    back to the same fact.

    Called before the sweep builds its own "already matched" exclusion set,
    so a pair this already covers is never re-probed (and re-paid for) on a
    later ``--infer-by-overlap`` run that finds the same edge again.
    Deduped the same way the general carry-forward is, so a --infer-by-overlap
    run that reconfirms an edge this run doesn't double it.
    """

    if prior is None or prior.provenance.connector != connector:
        return relationships, 0
    existing = {_relationship_edge_key(rel) for rel in relationships}
    carried = [
        rel
        for rel in prior.relationships
        if rel.kind is RelationshipKind.OVERLAP_INFERRED
        and rel.from_dataset in known_identifiers
        and rel.to_dataset in known_identifiers
        and _relationship_edge_key(rel) not in existing
    ]
    if not carried:
        return relationships, 0
    return relationships + carried, len(carried)


def _orphan_candidates(
    metas: list[ObjectMeta], defs: dbt_project.ProjectDefinitions
) -> set[str]:
    """Identifiers no current model/source builds or sources, restricted to
    schemas the project *does* build/source at least one other object into --
    that schema-membership check stands in for maintain's baseline-vs-current
    comparison, which explore has no snapshot to perform, so a raw, never
    dbt-modeled schema is never miscast as full of orphans."""

    if not defs.present or not defs.built_relation_names:
        return set()
    backed = {name.lower() for name in defs.built_relation_names}
    owned_schemas = {
        meta.identifier.rpartition(".")[0].lower()
        for meta in metas
        if "." in meta.identifier
        and meta.identifier.rpartition(".")[2].lower() in backed
    }
    if not owned_schemas:
        return set()
    return {
        meta.identifier
        for meta in metas
        if "." in meta.identifier
        and meta.identifier.rpartition(".")[0].lower() in owned_schemas
        and meta.identifier.rpartition(".")[2].lower() not in backed
    }


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
    datasets: list[Dataset],
    defs: dbt_project.ProjectDefinitions | None = None,
    *,
    orphaned: set[str] | None = None,
) -> None:
    """Attach the interpretation layer to raw profiles: candidate keys, the likely
    grain, and the data-quality warnings an analyst would write (non-unique own
    key, unknown grain). Shared by profile and map so a single-table profile
    surfaces a broken grain without requiring a full map.

    With project definitions, the declared truth refines the heuristics: a
    semantic model's primary entity overrides the detected grain (noting any
    disagreement), and a profiled column contradicting its declared ``unique``
    test gets a data-quality note. A declared composite key (a model-level
    ``unique_combination_of_columns`` test -- the one grain shape measurement
    alone can miss or a column-level test cannot express at all) overrides the
    detected grain too, unless the detected grain is already a measurement-
    proven single column, in which case the proven single wins and the
    composite is only noted. ``candidate_keys`` stays measurement-only: an
    unmeasured declared key is a claim, and the cache is a drift baseline.
    ``orphaned`` (map only) badges a relation no current model/source builds.
    """

    declared_grain: dict[str, str] = {}
    declared_unique: dict[str, set[str]] = {}
    declared_composite: dict[str, list[list[str]]] = {}
    if defs is not None and (
        defs.primary_entities or defs.declared_keys or defs.declared_composite_keys
    ):
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
        for composite in defs.declared_composite_keys:
            ident, _ambiguous = rel_mod.resolve_declared(
                composite.relation, composite.model, identifiers
            )
            if ident is None:
                continue
            bucket = declared_composite.setdefault(ident.lower(), [])
            normalized = {c.lower() for c in composite.columns}
            if not any({c.lower() for c in cols} == normalized for cols in bucket):
                bucket.append(list(composite.columns))

    for ds in datasets:
        ds.candidate_keys = rel_mod.candidate_keys(ds)
        ds.grain = rel_mod.detect_grain(ds)
        ds.data_quality.extend(rel_mod.data_quality_notes(ds))
        if orphaned and ds.identifier in orphaned:
            ds.data_quality.append(
                "no current dbt model or source builds this relation, though "
                "the project builds/sources others in this schema; likely "
                "orphaned residue of a rename or removal"
            )

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

        composite_candidates = declared_composite.get(ds.identifier.lower())
        if composite_candidates:
            live = {c.name.lower(): c.name for c in ds.columns}
            valid: list[list[str]] = []
            for cols in composite_candidates:
                resolved = [live.get(c.lower()) for c in cols]
                if all(resolved):
                    valid.append(resolved)
                else:
                    missing = [
                        c for c, r in zip(cols, resolved, strict=True) if r is None
                    ]
                    ds.data_quality.append(
                        f"declared composite key ({', '.join(cols)}) names "
                        f"column(s) not in this profile ({', '.join(missing)}); "
                        "not applied"
                    )
            if valid:
                # detect_grain() only ever returns a single column drawn from
                # candidate_keys()'s proven singles, EXCEPT the primary_entities
                # override just above can also force ds.grain to an unproven
                # single column -- this membership check catches both cases
                # correctly: a freshly measured proven single passes it, an
                # unproven declared one does not, and only the former should
                # block a composite override.
                proven_single = bool(
                    ds.grain and len(ds.grain) == 1 and ds.grain in ds.candidate_keys
                )
                if proven_single:
                    names = "; ".join(", ".join(c) for c in valid)
                    ds.data_quality.append(
                        f"a composite key ({names}) is also declared for this "
                        f"table; the measured single-column grain {ds.grain[0]} "
                        "took precedence"
                    )
                else:
                    chosen = valid[0]
                    if len(valid) > 1:
                        others = "; ".join(", ".join(c) for c in valid[1:])
                        ds.data_quality.append(
                            f"{len(valid)} composite keys are declared for this "
                            f"table; using {', '.join(chosen)} (also declared: "
                            f"{others})"
                        )
                    if ds.grain and ds.grain != chosen:
                        ds.data_quality.append(
                            f"grain {', '.join(chosen)} comes from the project's "
                            "declared composite key (heuristic suggested "
                            f"{', '.join(ds.grain)})"
                        )
                    ds.grain = chosen


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


def _generic_name_notes(suppressed: list[rel_mod.SuppressedMatch]) -> list[str]:
    """Explain what a generic shared id-column name cost inference, so the
    withheld count doesn't read as "nothing more to find" when it's really
    "found, and declined to trust".

    Empty when nothing was withheld: the common case on warehouses without a
    CDC-style universal id column.
    """

    if not suppressed:
        return []
    names = sorted({s.shared_name for s in suppressed})
    max_hosts = max(s.host_count for s in suppressed)
    shown = ", ".join(names[:5])
    more = f", +{len(names) - 5} more" if len(names) > 5 else ""
    return [
        f"declined to infer {len(suppressed)} candidate join(s) that matched only "
        f"on a column name shared as a key by {max_hosts} unrelated object(s) "
        f"({shown}{more}); a name shared this widely (the norm for Firestore/"
        "Mongo/DynamoDB-style CDC exports) is a naming convention, not evidence "
        "of a relationship, so these never reached --verify"
    ]


def _affix_match_notes(affix_matches: list[rel_mod.AffixMatch]) -> list[str]:
    """Explain when a join matched only after stripping a configured entity
    affix (see `EntityAffixes`), so the lower confidence these joins carry
    doesn't read as an unexplained demotion (issue #208)."""

    if not affix_matches:
        return []
    examples = ", ".join(
        f"{m.child_column} -> {m.parent} (stripped to {m.stripped_to})"
        for m in affix_matches[:5]
    )
    more = f", +{len(affix_matches) - 5} more" if len(affix_matches) > 5 else ""
    return [
        f"matched {len(affix_matches)} join(s) to a parent name only after "
        f"stripping a configured prefix/suffix ({examples}{more}); scored below "
        "an exact entity-name match to the same key"
    ]


def _overlap_sweep_notes(
    *,
    infer_by_overlap: bool,
    deferred: bool,
    pending: ConfirmationRequest | None,
    proposed: int,
    rejected: int,
    elided: int,
    carried: int,
) -> list[str]:
    """Notes for the ``--infer-by-overlap`` sweep phase (issue #220).

    Named even when the sweep did not run, off by default, deferred behind a
    still-pending ``--verify`` checkpoint, or pending its own, per the
    issue's acceptance criterion that the flag is discoverable from notes
    alone rather than only from ``--help``. ``carried`` is reported
    unconditionally, since a prior overlap-derived edge survives into this
    run's set regardless of whether the sweep itself ran again (see
    :func:`_carry_forward_overlap_edges`).
    """

    notes: list[str] = []
    if carried:
        notes.append(
            f"carried forward {carried} previously discovered value-overlap "
            "join(s); an overlap-derived edge is never rediscovered by "
            "plain inference, so it persists once found rather than "
            "needing --infer-by-overlap again on every call"
        )
    if not infer_by_overlap:
        notes.append(
            "value-overlap join inference not run; pass --infer-by-overlap "
            "to probe key-shaped columns whose names matched nothing for "
            "measured value containment"
        )
        return notes
    if deferred:
        notes.append(
            "value-overlap sweep deferred: --verify's own checkpoint is "
            "still pending confirmation; confirm and re-run to also run "
            "the sweep in the same pass"
        )
        return notes
    if pending is not None:
        notes.append(
            "value-overlap sweep saved unswept; confirmation awaits (see hint)"
        )
        return notes
    if proposed or rejected:
        notes.append(
            f"overlap sweep proposed {proposed} join(s) from measured value "
            f"containment alone (no column name matched); {rejected} probed "
            "candidate(s) did not show enough containment to propose"
        )
    else:
        notes.append("overlap sweep found no unmatched key-shaped column pair to probe")
    if elided:
        notes.append(
            f"{elided} additional unmatched key-shaped column pair(s) "
            "exceeded the overlap-sweep cap and were not probed"
        )
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


def _column_signature(columns) -> list[tuple[str, str, bool]]:
    """The (name, data_type, nullable) shape of a column set, sorted so two
    profiles compare independent of column order. Shared shape as
    ``maintain/drift.py``'s schema diff, applied here pre-profile against cheap
    metadata instead of post-profile against two cached snapshots."""

    return sorted((c.name, c.data_type, c.nullable) for c in columns)


def _split_fresh_stale(
    identifiers: list[str],
    prior: DexCache | None,
    connector: str,
    adapter: Adapter,
    max_age: timedelta,
    now: datetime,
    *,
    refresh: bool,
) -> tuple[list[str], dict[str, Dataset]]:
    """Split requested identifiers into (still need profiling, reusable-fresh
    datasets).

    An object is *fresh* (skip the re-scan) only when its cached profile came
    from this same connector, was actually profiled before (has columns and a
    ``profiled_at``), was profiled within ``max_age``, and its column signature
    still matches the warehouse's cheap metadata. Anything else is *stale* and
    goes back through the billed profiling scan. ``refresh`` forces every object
    stale (the pre-fix, unconditional-reprofile behavior), and so does a
    mismatched-connector or absent prior — mirroring ``cmd_map``'s reuse gate.

    Freshness is fail-closed: a missing or unparseable ``profiled_at``, or any
    doubt, re-profiles rather than trusting a stale scan.

    This is the gate for a *deliberate* profile, which is why the age window
    belongs in it. The on-demand path asks a narrower question (see
    :func:`_object_gap`) and deliberately does not reuse it: re-scanning a probe's
    table because a day passed would bill a caller for statistics nothing is about
    to read.

    The profiling commands share this gate but arrive holding identifiers at
    different points — ``map``/``relationships`` from inventory metas, ``profile``
    from resolved arguments — so it works on the identifier strings they have in
    common, not on ``ObjectMeta``.
    """

    if refresh or prior is None or prior.provenance.connector != connector:
        return list(identifiers), {}

    prior_by_id = {d.identifier: d for d in prior.datasets if d.columns}
    stale: list[str] = []
    fresh: dict[str, Dataset] = {}
    for identifier in identifiers:
        prior_ds = prior_by_id.get(identifier)
        if prior_ds is None or prior_ds.profiled_at is None:
            stale.append(identifier)
            continue
        try:
            profiled_at = datetime.fromisoformat(prior_ds.profiled_at)
        except ValueError:
            stale.append(identifier)
            continue
        if now - profiled_at > max_age:
            stale.append(identifier)
            continue
        # The same free, no-scan metadata call profile() makes first: confirms
        # the schema has not drifted since the cached profile was written.
        _meta, columns = adapter.table_metadata(identifier)
        if _column_signature(columns) != _column_signature(prior_ds.columns):
            stale.append(identifier)
            continue
        fresh[identifier] = prior_ds.model_copy(deep=True)
    return stale, fresh


def _compose_datasets(
    metas: list[ObjectMeta],
    profiled: list[Dataset],
    scores: dict[str, float],
    prior: DexCache | None,
    *,
    observed_namespaces: set[str] | None = None,
) -> tuple[list[Dataset], int, int, list[str]]:
    """Merge this run's profiles over the full inventory. Returns the composed
    datasets, how many prior profiles were carried forward within this run's
    inventory, how many were carried forward from entirely outside it, and
    which prior identifiers were dropped as gone rather than carried.

    An object not profiled this run reuses its prior profile wholesale (columns,
    keys, grain, notes, and its original ``profiled_at``, which marks the age)
    rather than silently degrading to an inventory-only entry; only the rank
    score is refreshed. ``row_count`` stays the prior one so the carried record
    is internally consistent with its own notes and counts. Carried profiles do
    not feed relationship inference, which runs on this run's profiles only.

    A prior dataset absent from ``metas`` entirely is either out of this run's
    scope or gone from the warehouse, and ``observed_namespaces`` (the
    schema/dataset prefixes this run's inventory actually saw, lowered) is
    what tells the two apart: if the identifier's own namespace was never
    observed, this run's inventory never looked there at all (a
    ``--scope``/``--dataset`` narrower than what built the prior cache), so it
    is carried forward untouched, rank score included, same as before (issue
    #111) -- not stale, not superseded, not this run's to drop. If its
    namespace WAS observed but the object itself is not in ``metas``, this
    run did look and it is not there: dropped, not carried, so a deleted
    relation cannot resurrect itself into the next baseline as a false
    ``table_dropped`` (issue #149). ``observed_namespaces=None`` (no scope
    information available) preserves the pre-#149 behavior of carrying
    everything unexamined forward.
    """

    by_id = {d.identifier: d for d in profiled}
    prior_by_id = (
        {d.identifier: d for d in prior.datasets if d.columns} if prior else {}
    )
    seen = {meta.identifier for meta in metas}
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

    out_of_scope = 0
    dropped: list[str] = []
    for identifier, previous in prior_by_id.items():
        if identifier in seen:
            continue
        namespace = identifier.rpartition(".")[0].lower()
        if observed_namespaces is not None and namespace in observed_namespaces:
            dropped.append(identifier)
            continue
        datasets.append(previous.model_copy(deep=True))
        out_of_scope += 1
    return datasets, carried, out_of_scope, dropped


# Sentinel: preserve the prior cache's relationships (profile has no inference
# pass, so it has no business touching them).
def _profile_checkpointer(
    store: ExploreStore,
    prior: DexCache | None,
    connector: str,
    now: datetime,
) -> tuple[Callable[[Dataset], None], list[Dataset]]:
    """Persist each profiled dataset as it completes, so a budget-exhaustion
    failure mid-run still leaves the objects already paid for in the cache.

    Reuses ``_merge_profiles`` (KEEP_RELATIONSHIPS), so a partial write is exactly
    a ``cmd_profile``-shaped result: fresh profiles folded over the same-connector
    prior, prior relationships preserved, no fabricated stubs. ``accumulated`` is
    returned so the failure handler can report "N of M".
    """

    accumulated: list[Dataset] = []

    def checkpoint(ds: Dataset) -> None:
        accumulated.append(ds)
        cache, _ = _merge_profiles(prior, accumulated, connector, now)
        store.save_cache(cache, now=now)

    return checkpoint, accumulated


def _budget_exhausted(
    store: ExploreStore, adapter: Adapter, accumulated: list[Dataset], selected: int
) -> BudgetExhaustedError:
    """The partial-completion refusal for a mid-run budget exhaustion.

    Because ``charge()`` fires before an object's ``Dataset`` is appended,
    ``accumulated`` holds only fully-profiled objects, so the "N of M" is
    truthful. The spend rides along because a caller deciding how much to raise
    the budget by needs to know what the attempt already cost.
    """

    gate = command_args.cost_gate(adapter)
    spend = gate.spend_summary() if gate is not None else None
    cost = gate.cost() if gate is not None else None
    n = len(accumulated)
    if n == 0:
        return BudgetExhaustedError(
            f"budget exhausted before the first of {selected} object(s) finished "
            "profiling; no partial profiles were saved. Raise --budget or narrow "
            "scope, then re-run",
            spend=spend,
            cost=cost,
        )
    cache_locator = store.locator(Document.CACHE)
    return BudgetExhaustedError(
        f"budget exhausted after profiling {n} of {selected} object(s); partial "
        f"profiles saved to {cache_locator}. Raise --budget or narrow scope, then "
        "re-run",
        spend=spend,
        cost=cost,
    )


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
        f"created the exploration cache with {count} profiled object(s); run "
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
            raise RequestError(f"no object named '{name}' in this connection")
        if len(unique) > 1:
            raise RequestError(
                f"'{name}' is ambiguous: {', '.join(unique)}; qualify it"
            )
        resolved.append(unique[0])
    return resolved
