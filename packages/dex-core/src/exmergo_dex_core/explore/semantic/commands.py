"""The `explore semantic` command surface, kept apart from the rest of explore.

Every other explore command parses SQL, so `explore/commands.py` imports the query
firewall and the clustering module and cannot be imported without the dialect
engine. The semantic subcommand needs neither on the hosted backend: dbt Cloud
renders and executes the query, and dex governs the request by dimension name and
the response by capping it.

So these three handlers live here rather than there, and the CLI routes to them
without touching the heavier module. That is what lets `[semantic-api]` be the
whole install for a deployment with no local project and no warehouse client, and
what keeps `explore semantic list --local` (a pure manifest read-view) working on
an install that picked no connector extra. The chain from here stays free of the
dialect engine, so adding an import that needs it here silently undoes that: the
packaging suite installs `[semantic-api]` alone and asserts sqlglot is absent.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from ... import envelope as env
from ...results import to_envelope
from ..results import SemanticListResult, SemanticQueryResult, SemanticValuesResult

if TYPE_CHECKING:
    from ...engine import DexEngine


def semantic_list(
    engine: DexEngine,
    *,
    metrics: list[str] | None = None,
    for_dimensions: list[str] | None = None,
    search: list[str] | None = None,
    full: bool = False,
    api: bool = False,
    local: bool = False,
):
    """The semantic layer's objects: semantic models, metrics, dimensions,
    entities, measures.

    Three ways to narrow it, and they compose in the order a caller reads them.
    ``for_dimensions`` asks the reverse question, "I want to slice by pricing
    tier, what can I slice", and resolves to the metrics groupable by every named
    token. ``metrics`` keeps those metrics and what is reachable from them, which
    is what a caller that already knows the metric wants. ``search`` is for the
    caller who knows a word rather than a name, and matches it against every
    element's name and the project's own words about it.

    None of them costs an extra round trip or a warehouse query: the reverse
    lookup is an inversion of the ``dimensions`` list each metric already carries,
    and the search is over the catalog already in hand rather than a second call
    to the layer. The scope is named in the payload every way, so a subset is
    never mistaken for the layer.

    Whatever survives is then capped, and every cut is counted in ``elided`` and
    named in a note. ``full`` lifts the caps. The narrowing flags are the better
    answer on a large layer, because they decide *which* part comes back rather
    than letting a cap decide.
    """

    from . import SemanticBackendError, SemanticQuery, resolve_backend

    backend = resolve_backend(engine, api=api, local=local)
    catalog = backend.list_definitions()
    # Normalized through the query object so `--metric a,b` and the repeated flag
    # mean the same thing here as they do on a metric query. The same normalization
    # for `--for-dimension`, whose values are identifiers too.
    wanted = SemanticQuery(metrics=list(metrics or [])).metrics
    slicing = SemanticQuery(metrics=list(for_dimensions or [])).metrics
    # Counted before scoping: asking the backend again for the denominator would be
    # a second round trip for a sentence.
    total = len(catalog.metrics)
    notes: list[str] = []

    if slicing:
        groupable, unknown = catalog.metrics_for_dimensions(slicing)
        if unknown:
            raise SemanticBackendError(
                f"no such dimension in this semantic layer: {', '.join(unknown)}. "
                "List without --for-dimension to see what it exposes, and note "
                "that a dimension reached through a join is only in the list when "
                "the read resolved the join graph (see dimension_scope)"
            )
        if wanted:
            # An explicit metric that cannot be grouped by the named dimensions is
            # dropped rather than refused: the caller asked which of these can be
            # sliced this way, and "none of them" is an answer. Dropped loudly,
            # because a silently shorter list reads as the layer's own answer.
            dropped = [name for name in wanted if name not in groupable]
            wanted = [name for name in wanted if name in groupable]
            if dropped:
                notes.append(
                    f"{', '.join(dropped)} cannot be grouped by all of "
                    f"{', '.join(slicing)}, so they are not in this catalog"
                )
        else:
            wanted = groupable
        named = ", ".join(slicing)
        if groupable:
            notes.append(
                f"{len(groupable)} of {total} metrics can be grouped by "
                f"{'all of ' if len(slicing) > 1 else ''}{named}"
            )
        elif len(slicing) > 1:
            # The likelier reading of an empty answer with several tokens: each is
            # groupable somewhere and no metric carries them together.
            notes.append(
                f"no metric in this layer can be grouped by all of {named} at "
                "once; ask for one at a time to see which metrics each reaches"
            )
        else:
            notes.append(
                f"no metric in this layer can be grouped by {named}; the layer "
                "declares the dimension and no metric reaches it"
            )

    if wanted or slicing:
        catalog, unknown = catalog.narrowed_to(wanted)
        if unknown:
            raise SemanticBackendError(
                f"no such metric in this semantic layer: {', '.join(unknown)}. "
                "List without --metric to see what it exposes"
            )
        catalog.scoped_to = wanted
        catalog.for_dimensions = slicing
        if wanted and not slicing:
            notes.append(
                f"scoped to {len(wanted)} of {total} metrics and what they reach; "
                "list without --metric for the whole layer"
            )

    terms = SemanticQuery(metrics=list(search or [])).metrics
    if terms:
        # Applied after the two name-based scopes, so `--metric x --search y` reads
        # as "within x, the parts about y" rather than the other way round.
        matched, unmatched = catalog.matching(terms)
        catalog = matched
        catalog.searched_for = terms
        if unmatched:
            # A note rather than a refusal, and the difference from an unknown
            # metric name is real: a substring that matches nothing is an honest
            # answer about the layer's words, where a misspelled identifier is a
            # question the layer was never asked. Named individually so a search
            # of three words with one typo is not read as three empty answers.
            answered = [term for term in terms if term not in unmatched]
            notes.append(
                f"nothing in this layer is named or described with "
                f"{', '.join(unmatched)}"
                + (f"; {', '.join(answered)} still answered" if answered else "")
            )
        if not catalog.metrics:
            notes.append(
                f"no metric matched {', '.join(terms)}, so this catalog is empty. "
                "A search resolves to the metrics it touches, so a term matching "
                "only an element no metric reads answers nothing queryable; list "
                "without --search for the whole layer"
            )
        elif len(catalog.metrics) < total:
            notes.append(
                f"matched {len(catalog.metrics)} of {total} metrics and what they "
                "reach; list without --search for the whole layer"
            )

    catalog = catalog.capped(full=full)
    catalog.notes = [*catalog.notes, *notes]
    return SemanticListResult(catalog=catalog, notes=list(catalog.notes))


def semantic_values(
    engine: DexEngine,
    dimension: str,
    *,
    metrics: list[str] | None = None,
    api: bool = False,
    local: bool = False,
) -> SemanticValuesResult:
    """One dimension's value domain: what a filter on it may be filtered to.

    The one precondition for writing a filter that no other dex surface can reach.
    ``explore profile`` cannot see a semantic dimension, and on a hosted layer there
    is no SQL path at all, because dbt Cloud is not a connector.

    ``metrics`` scopes the values to those a metric actually reaches, which is
    required for a dimension reached through a join and narrowing everywhere else;
    the backend says in the result which of the two it did.
    """

    from . import SemanticBackendError, SemanticQuery, resolve_backend, values_gap

    backend = resolve_backend(engine, api=api, local=local)
    read = getattr(backend, "values", None)
    if read is None:
        raise SemanticBackendError(values_gap(backend))
    return read(dimension, SemanticQuery(metrics=list(metrics or [])).metrics)


def semantic_query(
    engine: DexEngine,
    metrics: list[str],
    *,
    group_by: list[str] | None = None,
    where: list[str] | None = None,
    order_by: list[str] | None = None,
    grain: str | None = None,
    limit: int | None = None,
    api: bool = False,
    local: bool = False,
) -> SemanticQueryResult:
    """Run one governed metric query against the dbt semantic layer.

    Which backend answers decides who governs spend: local renders the metric
    SQL and executes it under dex's cost guard, while dbt Cloud executes
    server-side where that guard is structurally unavailable.
    """

    from . import SemanticBackendError, SemanticQuery, resolve_backend

    # Built before the check, because the query object is what normalizes the name
    # lists: `--metric ,` is as empty as no flag at all, and both backends should
    # hear the same thing about it rather than one refusing and the other asking
    # dbt Cloud for no metrics.
    query = SemanticQuery(
        metrics=metrics,
        group_by=group_by or [],
        where=where or [],
        order_by=order_by or [],
        grain=grain,
        limit=limit,
    )
    if not query.metrics:
        raise SemanticBackendError(
            "a metric query needs at least one metric (discover them with "
            "`explore semantic list`)"
        )
    return resolve_backend(engine, api=api, local=local).query(query)


def cmd_semantic(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    """`explore semantic list|values|query`: discover and query the semantic layer.

    Backend resolution and the two guard postures live in the
    ``explore.semantic`` package; this shim resolves the mode and turns any
    backend refusal into a clean envelope rather than a stack trace.

    The three modes share one parser, so a flag that means nothing in the mode it
    was passed to is refused here rather than accepted and dropped. A dropped flag
    is indistinguishable from an honored one right up until the answer is wrong,
    and this surface has three of them that read as plausible in the wrong mode.
    """

    from . import SemanticBackendError, SemanticQuery

    api = bool(getattr(args, "api", False))
    local = bool(getattr(args, "local", False))
    mode = getattr(args, "mode", None) or "list"
    try:
        positional = list(getattr(args, "metrics", None) or [])
        flagged = list(getattr(args, "metric", None) or [])
        for_dimensions = list(getattr(args, "for_dimension", None) or [])
        search = list(getattr(args, "search", None) or [])
        full = bool(getattr(args, "full", False))
        catalog_only = {
            "--for-dimension": bool(for_dimensions),
            "--search": bool(search),
            "--full": full,
        }
        misplaced = [flag for flag, given in catalog_only.items() if given]
        if misplaced and mode != "list":
            one = len(misplaced) == 1
            raise SemanticBackendError(
                f"{', '.join(misplaced)} "
                f"{'shapes' if one else 'shape'} the catalog and "
                f"{'has' if one else 'have'} no meaning on "
                f"`explore semantic {mode}`; use "
                f"{'it' if one else 'them'} with `list`"
            )
        if mode == "list":
            return to_envelope(
                semantic_list(
                    engine,
                    metrics=[*positional, *flagged],
                    for_dimensions=for_dimensions,
                    search=search,
                    full=full,
                    api=api,
                    local=local,
                )
            )
        if mode == "values":
            # Normalized the same way every other name list on this surface is, so
            # `values a,b` is refused as two dimensions rather than looked up as
            # one token that cannot exist.
            named = SemanticQuery(metrics=positional).metrics
            if len(named) != 1:
                raise SemanticBackendError(
                    "`explore semantic values` takes exactly one dimension "
                    "(`explore semantic values user__pricing_tier`). Two "
                    "dimensions would be a cross product of their values, which "
                    "is a metric query with two --group-by tokens"
                )
            return to_envelope(
                semantic_values(engine, named[0], metrics=flagged, api=api, local=local)
            )
        return to_envelope(
            semantic_query(
                engine,
                [*positional, *flagged],
                group_by=getattr(args, "group_by", None),
                where=getattr(args, "where", None),
                order_by=getattr(args, "order_by", None),
                grain=getattr(args, "grain", None),
                limit=getattr(args, "limit", None),
                api=api,
                local=local,
            )
        )
    except SemanticBackendError as exc:
        return env.error_for(exc)
