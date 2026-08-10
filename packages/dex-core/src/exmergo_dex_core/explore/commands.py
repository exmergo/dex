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
from datetime import UTC, datetime, timedelta
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
from ..errors import PrerequisiteError, RequestError
from ..guards.cost_guard import ConfirmationRequiredError, OverCeilingError
from ..guards.query_firewall import (
    InspectedQuery,
    QueryRefusedError,
    assert_query_shape,
    inspect_query,
)
from ..guards.sql_guard import referenced_relations
from ..progress import ProgressReporter
from ..results import BudgetExhaustedError, ConfirmationRequest, to_envelope
from ..storage import Document, ExploreStore
from . import cluster as cluster_mod
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
    QueryResult,
    RankedObject,
    RelationshipsResult,
)

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
    adapters or when inference found nothing to probe."""

    candidates = [r for r in relationships if r.kind is RelationshipKind.INFERRED]
    objects = {r.from_dataset for r in candidates} | {r.to_dataset for r in candidates}
    query_estimate = getattr(adapter, "query_estimate", None)
    if query_estimate is None or not candidates:
        return 0.0, len(candidates), len(objects)
    total = sum(
        query_estimate(sql)
        for sql in rel_mod.probe_statements(candidates, adapter.dialect)
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
) -> ProfileResult:
    """Profile the named objects, reusing fresh cached profiles where they exist.

    Raises :class:`~..results.ConfirmationRequired` on a billed connector before
    anything is scanned. ``refresh`` forces a re-scan of objects the cache would
    otherwise serve; ``use_project`` folds the dbt project's declared joins,
    grain, and metric lineage into the result.
    """

    store = engine.store
    config = engine.config
    defs = _project_definitions(engine, use_project)
    blob_paths = blob_override_paths(config.blob_overrides)
    adapter = engine._adapter("explore profile")
    # Capture pre-run cache state before any checkpoint write, so the success-path
    # compose reads the pre-run cache rather than a checkpoint this run wrote.
    now = datetime.now(UTC)
    prior = store.load_cache()

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
    profiled, _cache, locator, persist_note = _profile_into_cache(
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
    result = ProfileResult(
        datasets=datasets,
        profiled_count=len(profiled),
        cache_hit_count=len(fresh_reused),
        cache_path=locator,
        updated_at=now.isoformat(),
        notes=notes,
        warnings=_override_mismatches(datasets, config.pii_overrides),
    )
    return command_args.stamp_spend(result, adapter)


def cmd_profile(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(
        profile(
            engine,
            args.objects,
            refresh=getattr(args, "refresh", False),
            use_project=getattr(args, "use_project", False),
        )
    )


def relationships(
    engine: DexEngine,
    *,
    verify: bool = False,
    refresh: bool = False,
    use_project: bool = False,
) -> RelationshipsResult:
    """Infer joins across every object in scope, optionally probing them.

    Inference needs uniqueness signals, so this profiles the full inventory
    first; on a metered connector ``map`` (top-ranked objects only) is usually
    the cheaper way in. With ``verify``, candidate joins are probed for real
    overlap, priced only after inference knows what the candidates are, which is
    why that phase can come back as ``pending_confirmation`` rather than raising.
    """

    store = engine.store
    config = engine.config
    defs = _project_definitions(engine, use_project)

    adapter = engine._adapter("explore relationships")
    # Capture pre-run cache state before any checkpoint write, so the success-path
    # compose reads the pre-run cache rather than a checkpoint this run wrote.
    now = datetime.now(UTC)
    prior = store.load_cache()
    accumulated: list[Dataset] = []
    over_ceiling = False
    verify_pending: ConfirmationRequest | None = None
    verify_warning: str | None = None
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
    inferred = rel_mod.infer_relationships(datasets, suppressed=suppressed)
    if verify:
        probe_cost, candidates, objects = _verify_estimate(adapter, inferred)
        verify_pending = command_args.verify_handshake(
            "explore relationships",
            adapter,
            probe_cost,
            candidate_count=candidates,
            object_count=objects,
        )
        if verify_pending is None:
            verify_reporter = _reporter(len(inferred), "verified", "joins")
            try:
                rel_mod.verify_relationships(
                    adapter,
                    inferred,
                    timeout_seconds=config.query.timeout_seconds,
                    progress=verify_reporter,
                )
                verify_reporter.done()
            except OverCeilingError:
                # Estimate drift mid-loop; the relationship set itself is
                # complete, so finish with a warning (see map).
                done = sum(1 for r in inferred if r.verified)
                verify_warning = (
                    f"budget exhausted after verifying {done} of "
                    f"{len(inferred)} candidate join(s); verified "
                    "results are saved; raise --budget and re-run to "
                    "finish verification"
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

    declared, declared_notes = rel_mod.declared_relationships(
        defs, [d.identifier for d in datasets]
    )
    rels, confirmed = _merge_relationships(declared, inferred)
    # Prior relationships are only reusable when they came from the same
    # connector, same as the profiles below.
    reusable = prior if prior and prior.provenance.connector == connector else None
    examined = {d.identifier for d in datasets}
    rels, carried_relationships = _carry_forward_relationships(
        reusable, examined, rels, observed_namespaces=observed_namespaces
    )
    notes = _relationship_notes(datasets, declared, inferred, defs)
    notes.extend(declared_notes)
    notes.extend(defs.notes)
    if confirmed:
        notes.append(
            f"{confirmed} inferred join(s) match declared tests; kept as declared"
        )
    if verify and inferred and verify_pending is None and verify_warning is None:
        notes.append(
            f"verified {len(inferred)} inferred join(s) with aggregate overlap probes"
        )
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
    if carried_relationships > 0:
        notes.append(
            f"carried forward {carried_relationships} prior relationship(s) "
            "with an endpoint this run did not profile or reuse fresh (a "
            "--scope/--dataset narrower than a prior run)"
        )

    # Persist the profiles this run already paid for. Relationships inventories
    # and profiles the full set and infers across all of it, so its relationship
    # set is authoritative for every identifier it examined; anything with an
    # endpoint outside that (a narrower --scope/--dataset than a prior run) is
    # carried forward above rather than dropped, same as the profiles are.
    cache, stats = _merge_profiles(prior, datasets, connector, now, relationships=rels)
    locator = store.save_cache(cache, now=now)
    notes.append(_persist_note(stats, len(datasets), keeps_relationships=False))

    result = RelationshipsResult(
        relationships=rels,
        declared_count=len(declared),
        profiled_count=len(profiled),
        cache_hit_count=len(fresh_reused),
        carried_relationship_count=carried_relationships,
        cache_path=locator,
        updated_at=now.isoformat(),
        notes=notes,
        warnings=warnings,
        pending_confirmation=verify_pending,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_relationships(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(
        relationships(
            engine,
            verify=getattr(args, "verify", False),
            refresh=getattr(args, "refresh", False),
            use_project=getattr(args, "use_project", False),
        )
    )


def query(
    engine: DexEngine, sql: str, *, auto_profile: bool | None = None
) -> QueryResult:
    """Run one caller-authored SELECT through the query firewall.

    The firewall adjudicates against the exploration cache, and that is not a
    formality: the PII policy it applies is computed from what profiling flagged,
    so a query cannot be judged against a warehouse nobody has profiled. What used
    to follow from that, sending the caller away to run a command whose exact
    argument this function was already holding, does not. An object the connection
    has but the cache cannot speak for is profiled here and now, and the query
    proceeds under the flags that scan produced.

    Profiling costs money on a metered connector, so it is priced, not implied:
    the estimate covers the profile and the query together and one handshake
    admits both. ``auto_profile=False`` (``--no-auto-profile``, or
    ``auto_profile: false`` in config) restores the strict prerequisite, and on
    that path nothing here touches the connection before the firewall has spoken.

    Every decision, allowed or refused, lands in the query ledger.
    """

    store = engine.store
    config = engine.config
    limits = config.query
    now = datetime.now(UTC)
    at = now.isoformat()
    cache = store.load_cache()
    # The firewall parses in the active connector's dialect, so BigQuery SQL
    # (backticks, COUNTIF) is inspected as BigQuery, not as DuckDB.
    dialect = get_dialect(engine.connector or config.connector)
    auto = config.auto_profile if auto_profile is None else auto_profile

    profiled_names: list[str] = []
    adapter = None
    warnings: list[str] = []
    if auto:
        # Read-only first, before anything reaches the warehouse. Resolving a
        # relation introspects the live connection, and no agent-authored
        # statement earns that until it is proven to be a single SELECT.
        try:
            assert_query_shape(sql, dialect=dialect)
        except QueryRefusedError as exc:
            store.append_query_log(
                {"at": at, "sql": sql, "decision": "refused", "reason": str(exc)}
            )
            raise
        named = referenced_relations(sql, dialect=dialect)
        if named:
            adapter = engine._adapter("explore query")
            gap = _object_gap(adapter, cache, named)
            if gap.absent:
                exc = QueryRefusedError(gap.refusal())
                store.append_query_log(
                    {"at": at, "sql": sql, "decision": "refused", "reason": str(exc)}
                )
                raise exc
            if gap.to_profile:
                cache, profiled_names, warnings = _profile_for_statement(
                    engine, adapter, sql, gap.to_profile, cache, now, at
                )

    if cache is None:
        raise CacheRequiredError(
            "no exploration cache yet; run `explore map` first so the query "
            "firewall knows the schema and the PII flags"
        )

    # After the cache write, never before: _mask_overridden edits the datasets in
    # place, and masking first would persist an in-memory override onto objects
    # this run never re-profiled.
    cache = _mask_overridden(cache, pii_override_paths(config.pii_overrides))
    try:
        inspected = inspect_query(sql, cache, limits, dialect=dialect)
    except QueryRefusedError as exc:
        entry = {"at": at, "sql": sql, "decision": "refused", "reason": str(exc)}
        if profiled_names:
            entry["profiled_on_demand"] = profiled_names
        store.append_query_log(entry)
        # A caller who paid for a scan is told what they got, even when the guard
        # then refuses: the profile is cached and the next attempt reuses it.
        if profiled_names:
            raise QueryRefusedError(
                f"{exc}. {_profiled_names_phrase(profiled_names)} profiled before "
                "this refusal; the profile is cached, so a corrected query does "
                "not pay for it again"
            ) from exc
        raise

    adapter = adapter or engine._adapter("explore query")
    try:
        # Priced once, above, when a profile was needed: `preflight_command` sets
        # the reservation rather than adding to it, so a second handshake here
        # would release the profile's booking. The query still passes the
        # per-statement gate and the server-side cap on its way out.
        if not profiled_names:
            query_estimate = getattr(adapter, "query_estimate", None)
            estimate = query_estimate(inspected.sql) if query_estimate else 0.0
            try:
                command_args.billed_handshake("explore query", adapter, estimate)
            except ConfirmationRequiredError:
                store.append_query_log(
                    {
                        "at": at,
                        "sql": inspected.sql,
                        "decision": "needs_confirmation",
                        "estimated_bytes": estimate,
                    }
                )
                raise
        result = adapter.run_query(
            inspected.sql,
            max_rows=inspected.row_cap,
            timeout_seconds=limits.timeout_seconds,
        )
    except ConfirmationRequiredError:
        raise
    except Exception as exc:
        entry = {
            "at": at,
            "sql": inspected.sql,
            "decision": "failed",
            "reason": env.redact(str(exc)),
        }
        if profiled_names:
            entry["profiled_on_demand"] = profiled_names
        store.append_query_log(entry)
        raise

    payload = _shape_query_payload(result, inspected, limits)
    notes = payload.pop("notes")
    record = QueryResult(
        **payload,
        profiled_on_demand=profiled_names,
        notes=notes,
        warnings=[*warnings, *inspected.warnings],
    )
    command_args.stamp_spend(record, adapter)
    if profiled_names and command_args.cost_gate(adapter) is not None:
        record.notes.append(
            f"the spend reported here covers profiling {len(profiled_names)} "
            "object(s) and running the query"
        )

    log_entry = {
        "at": at,
        "sql": inspected.sql,
        "decision": "allowed",
        "tables": record.tables,
        "row_count": record.row_count,
        "truncated": record.truncated,
    }
    if profiled_names:
        log_entry["profiled_on_demand"] = profiled_names
    # The audit trail records which allowed queries projected sub-threshold
    # PII-flagged columns, so a later review can find every such projection.
    if inspected.warnings:
        log_entry["pii_warnings"] = inspected.warnings
    store.append_query_log(log_entry)
    return record


def _profiled_names_phrase(names: list[str]) -> str:
    if len(names) == 1:
        return f"'{names[0]}' was"
    return f"{len(names)} object(s) ({', '.join(names)}) were"


def _profile_for_statement(
    engine: DexEngine,
    adapter: Adapter,
    sql: str,
    to_profile: list[str],
    prior: DexCache | None,
    now: datetime,
    at: str,
) -> tuple[DexCache, list[str], list[str]]:
    """Price and run the profiles a statement needs, then hand back the new cache.

    One handshake covers the profile scans and the statement itself, because the
    caller asked one question and should be quoted one number for it. The
    statement is priced as written rather than as the firewall will rewrite it:
    the rewrite needs a cache that can resolve these very objects, which is what
    this call is about to create. Nothing is lost by it, since a row cap does not
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
    statement_estimate = query_estimate(sql) if query_estimate else 0.0
    try:
        command_args.billed_handshake(
            "explore query",
            adapter,
            profile_estimate + statement_estimate,
            per_table={**per_table, "(the statement itself)": statement_estimate},
            notes=[
                f"{len(to_profile)} object(s) this statement reads have no usable "
                "profile, and the firewall reads PII flags from the cache; this "
                "estimate covers profiling them and then running the statement. "
                "Pass --no-auto-profile to be refused instead of profiling"
            ],
        )
    except ConfirmationRequiredError:
        store.append_query_log(
            {
                "at": at,
                "sql": sql,
                "decision": "needs_confirmation",
                "estimated_bytes": profile_estimate + statement_estimate,
                "profile_planned": to_profile,
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
    try:
        return to_envelope(query(engine, args.sql, auto_profile=_auto_profile(args)))
    except QueryRefusedError as exc:
        return env.error_for(exc, f"query refused: {exc}")


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
    verify: bool = False,
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
    """

    store = engine.store
    config = engine.config
    defs = _project_definitions(engine, use_project)
    hints = _merged_hints(config.ranking_hints, defs.metric_models)

    adapter = engine._adapter("explore map")
    # Capture pre-run cache state before any checkpoint write, so the success-path
    # compose reads the pre-run cache rather than a checkpoint this run wrote.
    now = datetime.now(UTC)
    prior = store.load_cache()
    accumulated: list[Dataset] = []
    over_ceiling = False
    verify_pending: ConfirmationRequest | None = None
    verify_warning: str | None = None
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
    inferred = rel_mod.infer_relationships(all_selected, suppressed=suppressed)
    if verify:
        probe_cost, candidates, objects = _verify_estimate(adapter, inferred)
        verify_pending = command_args.verify_handshake(
            "explore map",
            adapter,
            probe_cost,
            candidate_count=candidates,
            object_count=objects,
        )
        if verify_pending is None:
            verify_reporter = _reporter(len(inferred), "verified", "joins")
            try:
                rel_mod.verify_relationships(
                    adapter,
                    inferred,
                    timeout_seconds=config.query.timeout_seconds,
                    progress=verify_reporter,
                )
                verify_reporter.done()
            except OverCeilingError:
                # The upfront probe pricing fit, but per-statement estimates
                # drifted past the ceiling mid-loop. The map itself is
                # complete, so finish with a warning instead of the profiling
                # phase's partial-completion error.
                done = sum(1 for r in inferred if r.verified)
                verify_warning = (
                    f"budget exhausted after verifying {done} of "
                    f"{len(inferred)} candidate join(s); verified "
                    "results are saved; raise --budget and re-run to "
                    "finish verification"
                )

    # Fold same-lineage duplicates before they reach the cache: a dev/replica
    # dataset mapped alongside its source otherwise inflates one real foreign key
    # into source, replica, and cross-dataset lookalike edges.
    inferred, folded_edges, mirrored_objects = rel_mod.fold_replica_relationships(
        all_selected, inferred, _dev_schemas(config)
    )

    declared, declared_notes = rel_mod.declared_relationships(
        defs, [m.identifier for m in metas]
    )
    relationship_set, confirmed = _merge_relationships(declared, inferred)
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
    if verify_pending is not None:
        notes.append(
            "relationships saved unverified; verification awaits confirmation "
            "(see hint)"
        )
    if verify_warning:
        warnings.append(verify_warning)

    top = sorted(datasets, key=lambda d: d.rank_score or 0.0, reverse=True)[:5]
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
        updated_at=now.isoformat(),
        notes=notes,
        warnings=warnings,
        pending_confirmation=verify_pending,
    )
    return command_args.stamp_spend(result, adapter)


def cmd_map(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    return to_envelope(
        map(
            engine,
            full=getattr(args, "full", False),
            verify=getattr(args, "verify", False),
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
    cache = store.load_cache()
    now = datetime.now(UTC)
    auto = config.auto_profile if auto_profile is None else auto_profile

    # Fail fast if the [cluster] extra is missing: no connection, no spend.
    cluster_mod.ensure_available()

    adapter = None
    warnings: list[str] = []
    profiled_names: list[str] = []
    if auto:
        adapter = engine._adapter("explore cluster")
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

    cache = engine.store.load_cache()
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
    except (CacheRequiredError, ValueError) as exc:
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
) -> dict:
    """Cap the result for agent context: row-major cells, cell-width truncation,
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
