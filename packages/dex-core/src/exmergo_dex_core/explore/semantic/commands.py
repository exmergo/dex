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
from ..results import SemanticListResult, SemanticQueryResult

if TYPE_CHECKING:
    from ...engine import DexEngine


def semantic_list(engine: DexEngine, *, api: bool = False, local: bool = False):
    """The metrics, dimensions, and entities the semantic layer exposes."""

    from . import resolve_backend

    backend = resolve_backend(engine, api=api, local=local)
    catalog = backend.list_definitions()
    return SemanticListResult(catalog=catalog, notes=list(catalog.notes))


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
    """`explore semantic list|query`: discover and query the dbt semantic layer.

    Backend resolution and the two guard postures live in the
    ``explore.semantic`` package; this shim resolves the mode and turns any
    backend refusal into a clean envelope rather than a stack trace.
    """

    from . import SemanticBackendError

    api = bool(getattr(args, "api", False))
    local = bool(getattr(args, "local", False))
    try:
        if getattr(args, "mode", None) == "list":
            return to_envelope(semantic_list(engine, api=api, local=local))
        return to_envelope(
            semantic_query(
                engine,
                getattr(args, "metric", None) or [],
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
