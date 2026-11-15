"""The local Snowflake Semantic View layer: the Semantic View files in this
repository, read, and queried through the views deployed from them.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, the deployed read, and the governed metric query.

**The catalog needs no Snowflake connection.** ``explore semantic list`` reads
the repository's Semantic View YAML statically, and the declaration channel
carries each primary and unique key and each relationship to ``explore
relationships|map|profile --use-project``. ``list --deployed`` is the explicit
opt-in to reading the views deployed on the account instead
(:mod:`...semantic_views.deployed`), with each view's coverage named.

**Dex renders, Snowflake executes, through dex's connector.** A governed query
is a ``SELECT ... FROM SEMANTIC_VIEW(<deployed view> DIMENSIONS ... METRICS ...
[WHERE ...])`` statement rendered from the catalog
(:mod:`...semantic_views.query`), in this order, because the safety contract is
the order:

1. the connector check: Snowflake only;
2. translation: every refusal the request can earn on its own (facts beside
   metrics or dimensions, a private, window, LOD or ``using_relationships``
   member, ``variables``, an unreachable dimension, a grain on a categorical
   dimension, a ``--where`` naming a metric), before anything is sent;
3. the PII request-gate, through the catalog's lineage, on every grouped
   member, every member ``--where`` names, the time dimension a grain buckets,
   and every input of a metric that returns one of its values (``MAX``,
   ``MIN``, ``ANY_VALUE``, ``LISTAGG``, ...);
4. the deployed view, located with a free ``SHOW SEMANTIC VIEWS`` over the
   source allowlist and read back with ``SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW``,
   which Snowflake answers without a warehouse; every member the query relies
   on is held to the repository's definition, and a difference refuses;
5. the render, against the deployed view's own name;
6. the governed spine (:mod:`.governed`): SELECT-only, the relation pre-check
   (the deployed view admitted by name, since the inventory leaves semantic
   views out), the cost handshake priced from the base tables the members
   reach (Snowflake keeps no bytes for a semantic view), dex's connector, the
   columnar cap.

``values`` runs the same spine behind a ``DIMENSIONS``-only clause, or one
carrying the ``--metric`` that scopes the values, which ``scoped_to`` names.
"""

from __future__ import annotations

from typing import Any

from ...config import QueryLimits
from ...errors import ProjectError
from ..results import SemanticQueryResult, SemanticValuesResult
from .backend import (
    EXECUTION_DEX,
    BackendDescriptor,
    SemanticBackendError,
    SemanticQueryRefusedError,
)
from .catalog import SemanticCatalog
from .governed import GovernedSql
from .model import SemanticQuery
from .policy import (
    name_only_note,
    screen_dimension_refs,
    screen_values_request,
    sub_threshold_note,
    unadjudicated_refs,
)

__all__ = ["SEMANTIC_VIEW_CATALOG_GAPS", "LocalSemanticViewLayer"]

# Fields of the neutral catalog a Semantic View has nowhere to put, per element
# kind.
#
# A Semantic View element carries a description and synonyms, and no display
# label. A logical table sits on a relation rather than a dbt model, and names no
# default time dimension. Semantic Views have no time grains: a time bucket is a
# dimension's own SQL, so no dimension or metric names a queryable grain or a
# time axis, and no measure names the time dimension it aggregates over. An
# entity is a table's primary key, which carries no label or description of its
# own. A metric has no filter apart from its own expression (a table's `filters`
# are Cortex Analyst's, not a metric's).
SEMANTIC_VIEW_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["label", "model_ref", "agg_time_dimension"],
    "metrics": ["label", "filter", "time_axis", "queryable_granularities"],
    "dimensions": ["label", "queryable_granularities"],
    "measures": ["label", "agg_time_dimension"],
    "entities": ["label", "description"],
}

# A relation that resolves to the wrong namespace is a logical table whose
# `base_table` names another database or schema than the one dex reads.
_RECOMPILE_HINT = "correct the logical table's `base_table`"

_CONNECTOR = "snowflake"


class LocalSemanticViewLayer:
    """The Snowflake Semantic View files committed to this repository."""

    name = "local"
    vendor = "snowflake"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = SEMANTIC_VIEW_CATALOG_GAPS
    #: ``explore semantic list --deployed`` reads the views deployed from these
    #: files, on request.
    reads_deployed = True
    #: A query of facts alone names no metric.
    metricless_queries = True
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=SEMANTIC_VIEW_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        """Wrap a Semantic View source; an engine, where given, backs the PII gate
        and the governed query."""
        self._source = source
        # Optional: reading the catalog needs no engine. With one, the values
        # gate reads the profiled cache rather than the name alone.
        self._dex = engine
        self._view: Any = None
        self._governed: GovernedSql | None = None

    @classmethod
    def from_engine(cls, engine: Any) -> LocalSemanticViewLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Semantic View files needs a repository to read them from: "
                "they are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source(), engine)

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self, *, deployed: bool = False) -> SemanticCatalog:
        """The catalog `explore semantic list` renders: the repository's files,
        or, with ``deployed``, the views deployed from them."""
        if deployed:
            return self._deployed_catalog()
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """Each key-equality relationship, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Each primary and unique key, single-column and composite. A Semantic
        View is never the transformation project, so this is the only route its
        keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def declared_models(self) -> tuple[dict[str, Any], list[str]]:
        """Each definition's declaration, with what could not be read. A
        Semantic View has no metadata block a dex declaration could live in, so
        this is empty by construction."""

        defs = self._source.declared_definitions()
        return defs.model_declarations, defs.declaration_notes

    def _catalog_view(self):
        """The source's catalog, read once; an unreadable layer is a backend error."""
        if self._view is None:
            try:
                self._view = self._source.semantic_catalog()
            except ProjectError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._view

    def _deployed_catalog(self) -> SemanticCatalog:
        """The views deployed from the configured files, read back.

        Each configured view is located on the account and read back; one that
        is not deployed, or that this role cannot read, is named in
        ``coverage`` with the reason, never silently left out. A name the
        repository also has keeps the repository's spelling, so a token here
        lines up with the repository's.
        """

        from ...semantic_views import catalog as catalog_mod
        from ...semantic_views.deployed import (
            REFUSED,
            Coverage,
            LocateRefused,
            locate,
            read_deployed,
            respell,
        )
        from ...semantic_views.loader import LoadResult

        self._supported_connector(
            "`explore semantic list --deployed`",
            advice="List the repository's files without --deployed, which needs no "
            "connection",
        )
        governed = self._governed_sql()
        adapter = governed.adapter("explore semantic list")
        listed = adapter.list_semantic_views()
        loaded = self._loaded()
        views: list[Any] = []
        coverage: list[Coverage] = []
        for view in loaded.views:
            try:
                where = locate(listed, view.name)
            except LocateRefused as exc:
                coverage.append(Coverage(view.name, None, REFUSED, [str(exc)]))
                continue
            read = read_deployed(
                view.name, where, adapter.read_semantic_view_yaml, view
            )
            coverage.append(read.coverage)
            if read.view is not None:
                views.append(respell(read.view, view))
        deployed = LoadResult(views=views)
        resolved = catalog_mod.resolution(deployed, self._connector())
        notes = [
            "read from the views deployed on the account "
            "(SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW), not the repository's files; a "
            "name the repository also has keeps the repository's spelling, and "
            "one only the deployment has is spelled as Snowflake returns it"
        ]
        if any(c.status != "full" for c in coverage):
            notes.append(
                "coverage is not full for every view: see `coverage`. A limited "
                "view is missing what this role cannot read back, and a refused "
                "one is not in this catalog at all"
            )
        view = catalog_mod.semantic_catalog(deployed, resolved, notes=notes)
        return SemanticCatalog.from_view(
            view,
            self,
            source="deployed",
            coverage=[c.as_dict() for c in coverage],
        )

    # ---- query -------------------------------------------------------------

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        """The ``view.table.member`` tokens these ``--where`` predicates name, or
        None for a clause dex cannot read (refused rather than screened by half)
        or one naming something a filter may not."""

        from ...semantic_views.query import QueryRefused, where_tokens

        try:
            read = where_tokens(self._loaded(), clauses)
        except (QueryRefused, SemanticBackendError):
            return None
        if read is None:
            return None
        return [resolved.token for resolved in read[0]]

    def time_axis_refs(self, _metrics: list[str]) -> list[str] | None:
        """None: a Semantic View names no time axis for a metric to bucket; a
        grain is always written on the dimension it buckets, which the gate
        screens directly."""

        return None

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        """One governed query, rendered as ``SEMANTIC_VIEW(...)`` and run here."""

        from ...semantic_views.query import translate_query

        self._supported_connector("a governed Semantic View query")
        governed = self._governed_sql()
        plan = self._translate(
            lambda loaded, resolved, catalog: translate_query(
                loaded,
                resolved,
                catalog,
                metrics=q.metrics,
                group_by=q.group_by,
                where=q.where,
                grain=q.grain,
            )
        )
        cache = governed.load_cache()
        refs, aliases = self._screened(plan)
        lookup = governed.pii_lookup(cache)
        blocked = screen_dimension_refs(refs, meta_lookup=lookup, aliases=aliases)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping, filtering or returning the values of {named} "
                "would surface PII. PII is flagged, never surfaced; query a non-PII "
                "member instead."
            )
        command = "explore semantic query"
        where, notes = self._deployed(plan, command)
        rendered = self._render(
            plan, where, order_by=q.order_by, limit=q.limit, governed=governed
        )
        return governed.run(
            rendered.sql,
            cache,
            backend=self,
            command=command,
            result_type=SemanticQueryResult,
            extra_notes=[
                *notes,
                *name_only_note(
                    unadjudicated_refs(refs, meta_lookup=lookup, aliases=aliases)
                ),
                *sub_threshold_note(refs, meta_lookup=lookup, aliases=aliases),
            ],
            column_names=rendered.names,
            priced_relations=self._base_relations(plan),
            live_relations=[where],
        )

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        """One dimension's values, rendered as ``SEMANTIC_VIEW(... DIMENSIONS
        <one>)`` and run here, after the values gate."""

        from ...semantic_views.query import (
            QueryRefused,
            resolve_token,
            translate_values,
        )

        # The token is resolved and screened before the connector is asked for:
        # a personal dimension is refused as PII wherever this runs.
        try:
            token = resolve_token(self._loaded(), dimension)
        except QueryRefused as exc:
            raise SemanticBackendError(str(exc)) from exc
        governed = self._governed_sql() if self._dex is not None else None
        cache = governed.load_cache() if governed is not None else None
        lookup = governed.pii_lookup(cache) if governed is not None else None
        notes = screen_values_request(
            token.token,
            meta_lookup=lookup,
            aliases={token.token: [token.base]} if token.grain else None,
        )
        self._supported_connector("reading a Semantic View dimension's values")
        governed = self._governed_sql()
        plan = self._translate(
            lambda loaded, resolved, catalog: translate_values(
                loaded, resolved, catalog, dimension, metrics
            )
        )
        command = "explore semantic values"
        where, deployed_notes = self._deployed(plan, command)
        rendered = self._render(plan, where, governed=governed)
        return governed.run(
            rendered.sql,
            cache,
            backend=self,
            command=command,
            result_type=SemanticValuesResult,
            extra_notes=[*deployed_notes, *notes],
            column_names=rendered.names,
            priced_relations=self._base_relations(plan),
            live_relations=[where],
            dimension=token.token,
            scoped_to=[m.token for m in plan.metrics],
        )

    # ---- the steps -----------------------------------------------------------

    def _translate(self, build) -> Any:
        """Run a translation, turning its refusals into backend errors."""

        from ...semantic_views.query import QueryRefused

        try:
            return build(
                self._loaded(), self._source.resolution(), self._catalog_view()
            )
        except QueryRefused as exc:
            if exc.guard:
                raise SemanticQueryRefusedError(str(exc)) from exc
            raise SemanticBackendError(str(exc)) from exc

    def _screened(self, plan: Any) -> tuple[list[str], dict[str, list[str]]]:
        """Every token the PII gate screens, and the tokens each stands for."""

        from ...semantic_views.query import token_of, value_inputs

        refs: list[str] = []
        aliases: dict[str, list[str]] = {}
        for item in [*plan.dimensions, *plan.facts, *plan.filters]:
            refs.append(item.token)
            if item.grain is not None:
                aliases[item.token] = [item.base]
        for metric in plan.metrics:
            inputs = value_inputs(plan.view, metric.member)
            if inputs is None:
                raise SemanticQueryRefusedError(
                    f"refused: dex could not read which columns metric "
                    f"{metric.token} returns the values of, so the PII gate cannot "
                    "screen them"
                )
            refs.extend(token_of(plan.view, member) for member in inputs)
        return list(dict.fromkeys(refs)), aliases

    def _deployed(self, plan: Any, command: str) -> tuple[str, list[str]]:
        """Locate the deployed view and hold it to the repository, before any
        spend; the deployed name, and notes for the result."""

        from ...semantic_views.deployed import (
            LIMITED,
            REFUSED,
            LocateRefused,
            compare,
            locate,
            read_deployed,
        )
        from ...semantic_views.query import metric_inputs

        adapter = self._governed_sql().adapter(command)
        view = plan.view
        try:
            where = locate(adapter.list_semantic_views(), view.name)
        except LocateRefused as exc:
            raise SemanticQueryRefusedError(str(exc)) from exc
        read = read_deployed(view.name, where, adapter.read_semantic_view_yaml, view)
        if read.coverage.status == REFUSED or read.view is None:
            raise SemanticQueryRefusedError(
                f"refused: the deployed view {where} could not be read back, so "
                "dex cannot hold it to the repository's definition: "
                + "; ".join(read.coverage.reasons)
            )
        members = [item.member for item in plan.members]
        for item in plan.metrics:
            members.extend(metric_inputs(view, item.member))
        unique = list({(m.table or "", m.name.lower()): m for m in members}.values())
        differences = compare(
            view, read.view, unique, plan.tables(self._source.resolution())
        )
        if differences:
            listed = "; ".join(d.render() for d in differences[:5])
            more = f" (and {len(differences) - 5} more)" if len(differences) > 5 else ""
            raise SemanticQueryRefusedError(
                f"refused: the view deployed as {where} differs from the "
                f"repository's {view.file} on what this query reads: {listed}{more}. "
                "A governed query answers with the reviewed definition or not at "
                "all: redeploy the reviewed file, or update the repository to "
                "match what is deployed and review it"
            )
        notes = [f"executed against the semantic view deployed as {where}"]
        if read.coverage.status == LIMITED:
            notes.extend(read.coverage.reasons)
        return where, notes

    def _render(
        self,
        plan: Any,
        where: str,
        *,
        governed: GovernedSql,
        order_by: list[str] | None = None,
        limit: int | None = None,
    ) -> Any:
        from ...semantic_views.query import QueryRefused

        try:
            return plan.render(
                where,
                order_by=order_by or [],
                limit=limit,
                max_rows=self._limits().max_rows,
            )
        except QueryRefused as exc:
            raise SemanticBackendError(str(exc)) from exc

    def _base_relations(self, plan: Any) -> list[str]:
        """The base tables of every logical table the statement reaches."""

        resolved = self._source.resolution()
        found: list[str] = []
        for table in sorted(plan.tables(resolved)):
            relation = resolved.relations.get((plan.view.name.lower(), table))
            if relation:
                found.append(relation)
        return found

    # ---- plumbing ------------------------------------------------------------

    def _loaded(self):
        try:
            loaded = self._source.loaded()
        except ProjectError as exc:
            raise SemanticBackendError(str(exc)) from exc
        if loaded.errors and loaded.empty:
            self._catalog_view()  # raises, naming every file the reader refused
        return loaded

    def _supported_connector(self, what: str, *, advice: str | None = None) -> str:
        connector = self._connector() or ""
        if connector == _CONNECTOR:
            return connector
        raise SemanticBackendError(
            f"{what} runs on the Snowflake connector, and this repository's "
            f"connector is {connector or 'not set'}: a Semantic View is a "
            "Snowflake object, queried through Snowflake's own SEMANTIC_VIEW "
            "clause. "
            + (
                advice
                or "Read the layer with `explore semantic list`, then query the "
                "relations it names with `explore query`, which runs under the "
                "firewall and the cost guard"
            )
        )

    def _connector(self) -> str | None:
        if self._dex is not None:
            return self._dex.connector or getattr(self._dex.config, "connector", None)
        return getattr(self._source, "connector", None)

    def _governed_sql(self) -> GovernedSql:
        if self._governed is None:
            if self._dex is None:
                raise SemanticBackendError(
                    "a Semantic View query runs through dex's own connection, so "
                    "build the layer from an engine "
                    "(LocalSemanticViewLayer.from_engine)"
                )
            self._governed = GovernedSql(
                self._dex,
                self._connector() or "",
                self._limits(),
                self._physical_columns,
                recompile_hint=_RECOMPILE_HINT,
                computed_columns=self._computed_columns,
            )
        return self._governed

    def _physical_columns(self) -> dict[str, tuple[str, str]]:
        try:
            return dict(self._catalog_view().physical_columns)
        except SemanticBackendError:
            return {}

    def _computed_columns(self) -> dict[str, tuple[str, str]]:
        try:
            return dict(self._catalog_view().computed_columns)
        except SemanticBackendError:
            return {}

    def _limits(self) -> QueryLimits:
        config = getattr(self._dex, "config", None)
        return getattr(config, "query", None) or QueryLimits()
