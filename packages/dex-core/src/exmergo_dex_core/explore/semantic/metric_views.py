"""The local Databricks Metric View layer: the Metric View files in this
repository, read, and queried through the views deployed from them.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, and the governed query.

**The catalog needs no workspace.** ``explore semantic list`` reads the
repository's Metric View YAML statically, and the declaration channel carries
each join and each ``rely.at_most_one_match`` key to ``explore
relationships|map|profile --use-project``. ``list --deployed`` is the explicit
opt-in to reading the views deployed from those files instead
(:mod:`...metric_views.deployed`), with each view's coverage named.

**Dex renders, Databricks executes, through dex's connector (#1362).** A governed
query is a ``SELECT ... MEASURE(...) FROM <deployed view> GROUP BY ALL``
statement rendered from the catalog (:mod:`...metric_views.query`), in this order,
because the safety contract is the order:

1. the connector check: Databricks only;
2. translation: every refusal the request can earn on its own (a window or
   ``partition`` measure, ``parameters``, a ``materialization``, two views, a
   grain on a field that is not a date, a ``--where`` naming a measure, a query
   with no measure), before anything is sent;
3. the PII request-gate, through the catalog's lineage, on every grouped field,
   every field ``--where`` names, the field a grain buckets, and every column a
   measure returns the values of (``MIN``, ``MAX``, ``ANY_VALUE``,
   ``COLLECT_LIST``, ...);
4. the deployed view, found by its file's stem among the Metric Views the source
   allowlist holds and read back with ``tables.get`` (both Unity Catalog REST
   reads, no warehouse); everything the query relies on is held to the
   repository's file, and a difference refuses (#1363);
5. the base tables the request reaches through the view, which must lie inside
   the scoped inventory, since they are what the statement is priced by;
6. the render, against the deployed view's own name;
7. the governed spine (:mod:`.governed`): SELECT-only, the relation pre-check
   (the deployed view admitted by name, since the inventory leaves Metric Views
   out), the cost handshake priced from those base tables (Databricks keeps no
   size for a Metric View), dex's connector under ``STATEMENT_TIMEOUT``, the
   columnar cap.

``values`` screens the field first, as every vendor does, so a PII field is
refused as PII wherever this runs; then it runs the same spine behind a
fields-only ``GROUP BY ALL``, or one carrying the ``--metric`` that scopes the
values, which ``scoped_to`` names.
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

__all__ = ["METRIC_VIEW_CATALOG_GAPS", "LocalMetricViewLayer"]

# Fields of the neutral catalog a Metric View has nowhere to put, per element
# kind.
#
# A view carries no display name of its own and sits on a relation rather than
# a dbt model; it declares no entities, its joins and keys reaching dex through
# the declaration channel instead. Metric Views have no time grains: a time
# bucket is a field's own SQL (`DATE_TRUNC('MONTH', o_orderdate)`), so no field
# or metric names a queryable grain or a time axis, and no measure names the
# time dimension it aggregates over.
METRIC_VIEW_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["label", "model_ref", "agg_time_dimension", "primary_entity"],
    "metrics": ["time_axis", "queryable_granularities"],
    "dimensions": ["queryable_granularities"],
    "measures": ["agg_time_dimension"],
    "entities": ["name", "type", "label", "description", "roles"],
}

# A relation that resolves to the wrong namespace is a view whose `source` names
# another catalog or schema than the one dex reads.
_RECOMPILE_HINT = "correct the Metric View's `source`"

_CONNECTOR = "databricks"


class LocalMetricViewLayer:
    """The Databricks Metric View files committed to this repository."""

    name = "local"
    vendor = "databricks"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = METRIC_VIEW_CATALOG_GAPS
    #: ``explore semantic list --deployed`` reads the views deployed from these
    #: files, on request (#1363).
    reads_deployed = True
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=METRIC_VIEW_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        """Wrap a Metric View source; an engine, where given, backs the PII gate
        and the governed query."""
        self._source = source
        # Optional: reading the catalog needs no engine. With one, the values
        # gate reads the profiled cache rather than the name alone.
        self._dex = engine
        self._view: Any = None
        self._governed: GovernedSql | None = None
        #: Columns a value-returning measure reads directly, registered by the
        #: PII gate's screening for this command (see :meth:`_screened`).
        self._base_columns: dict[str, tuple[str, str]] = {}

    @classmethod
    def from_engine(cls, engine: Any) -> LocalMetricViewLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Metric View files needs a repository to read them from: "
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
        """Each join with key pairs, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Each ``rely.at_most_one_match`` key, on the joined table. A Metric
        View is never the transformation project, so this is the only route its
        keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def declared_models(self) -> tuple[dict[str, Any], list[str]]:
        """Each definition's declaration, with what could not be read. A Metric
        View has no metadata block a declaration could live in, so this is empty
        by construction."""

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

        Each configured view is located among the Metric Views the source
        allowlist holds and read back from Unity Catalog; one that is not
        deployed, or that this identity cannot read, is named in ``coverage``
        with the reason, never silently left out. A read-back is named after
        the file it was deployed from, so its tokens line up with the
        repository's.
        """

        self._supported_connector(
            "`explore semantic list --deployed`",
            advice="List the repository's files without --deployed, which needs "
            "no connection",
        )
        governed = self._governed_sql()
        try:
            return self._read_deployed_catalog(governed)
        finally:
            governed.release()

    def _read_deployed_catalog(self, governed: GovernedSql) -> SemanticCatalog:
        from ...metric_views import catalog as catalog_mod
        from ...metric_views.deployed import (
            FULL,
            REFUSED,
            Coverage,
            LocateRefused,
            locate,
            read_deployed,
        )
        from ...metric_views.loader import LoadResult

        adapter = governed.adapter("explore semantic list")
        listed = adapter.list_metric_views()
        views: list[Any] = []
        coverage: list[Coverage] = []
        for view in self._loaded().views:
            try:
                where = locate(listed, view.name)
            except LocateRefused as exc:
                coverage.append(Coverage(view.name, None, REFUSED, [str(exc)]))
                continue
            read = read_deployed(view.name, where, adapter.read_metric_view_yaml)
            coverage.append(read.coverage)
            if read.view is not None:
                views.append(read.view)
        deployed = LoadResult(views=views)
        resolved = catalog_mod.resolution(deployed, self._connector())
        notes = [
            "read from the Metric Views deployed in Unity Catalog (each view's "
            "`view_definition`, read with tables.get), not the repository's "
            "files; each is named after the file it was deployed from"
        ]
        if any(c.status != FULL for c in coverage):
            notes.append(
                "coverage is not full for every view: see `coverage`. A limited "
                "view is missing what dex's reader refused in its read-back, and "
                "a refused one is not in this catalog at all"
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
        """The ``view.field`` tokens these ``--where`` predicates name, or None
        for a clause dex cannot read (refused rather than screened by half) or
        one naming something a filter may not."""

        from ...metric_views.query import QueryRefused, filter_fields

        try:
            read = filter_fields(self._loaded(), clauses)
        except (QueryRefused, SemanticBackendError):
            return None
        if read is None:
            return None
        return [resolved.canonical for resolved in read[0]]

    def time_axis_refs(self, _metrics: list[str]) -> list[str] | None:
        """None: a Metric View names no time axis for a measure to bucket; a
        grain is always written on the field it buckets, which the gate screens
        directly."""

        return None

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        """One governed query, rendered with ``MEASURE()`` and run here."""

        from ...metric_views.query import translate_query

        self._supported_connector("a governed Metric View query")
        governed = self._governed_sql()
        plan = self._translate(
            lambda loaded: translate_query(
                loaded,
                metrics=q.metrics,
                group_by=q.group_by,
                where=q.where,
                grain=q.grain,
            )
        )
        try:
            return self._run_query(q, plan, governed)
        finally:
            governed.release()

    def _run_query(
        self, q: SemanticQuery, plan: Any, governed: GovernedSql
    ) -> SemanticQueryResult:
        cache = governed.load_cache()
        refs, aliases = self._screened(plan)
        lookup = governed.pii_lookup(cache)
        blocked = screen_dimension_refs(refs, meta_lookup=lookup, aliases=aliases)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping, filtering or returning the values of {named} "
                "would surface PII. PII is flagged, never surfaced; query a non-PII "
                "field instead."
            )
        command = "explore semantic query"
        where, notes = self._deployed(plan, command)
        priced = self._in_scope(plan, command)
        rendered = self._render(plan, where, order_by=q.order_by, limit=q.limit)
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
            priced_relations=priced,
            live_relations=[where],
        )

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        """One field's values, rendered as a fields-only ``GROUP BY ALL`` and run
        here, after the values gate."""

        from ...metric_views.model import FIELD
        from ...metric_views.query import QueryRefused, resolve_token

        # The token is resolved and screened before the connector is asked for:
        # a personal field is refused as PII wherever this runs.
        try:
            token = resolve_token(self._loaded(), dimension)
        except QueryRefused as exc:
            raise SemanticBackendError(str(exc)) from exc
        if token.kind != FIELD:
            raise SemanticBackendError(f"{token.token} is a measure, not a field")
        # Screened as the catalog spells it: a token written in another case
        # resolves to the same field and must meet the same evidence. Resolved
        # before the lookup is built, which reads the columns it registers.
        aliases = {token.token: self._field_aliases(token)}
        lookup = None
        if self._dex is not None:
            governed = self._governed_sql()
            lookup = governed.pii_lookup(governed.load_cache())
        notes = screen_values_request(token.token, meta_lookup=lookup, aliases=aliases)
        self._supported_connector("reading a Metric View field's values")
        governed = self._governed_sql()
        try:
            return self._run_values(dimension, metrics, token, notes, governed)
        finally:
            governed.release()

    def _run_values(
        self,
        dimension: str,
        metrics: list[str],
        token: Any,
        notes: list[str],
        governed: GovernedSql,
    ) -> SemanticValuesResult:
        from ...metric_views.query import translate_values

        plan = self._translate(
            lambda loaded: translate_values(loaded, dimension, metrics)
        )
        cache = governed.load_cache()
        command = "explore semantic values"
        where, deployed_notes = self._deployed(plan, command)
        priced = self._in_scope(plan, command)
        rendered = self._render(plan, where)
        return governed.run(
            rendered.sql,
            cache,
            backend=self,
            command=command,
            result_type=SemanticValuesResult,
            extra_notes=[*deployed_notes, *notes],
            column_names=rendered.names,
            priced_relations=priced,
            live_relations=[where],
            dimension=token.token,
            scoped_to=[m.token for m in plan.measures],
        )

    # ---- the steps -----------------------------------------------------------

    def _translate(self, build) -> Any:
        """Run a translation, turning its refusals into backend errors."""

        from ...metric_views.query import QueryRefused

        try:
            return build(self._loaded())
        except QueryRefused as exc:
            if exc.guard:
                raise SemanticQueryRefusedError(str(exc)) from exc
            raise SemanticBackendError(str(exc)) from exc

    def _screened(self, plan: Any) -> tuple[list[str], dict[str, list[str]]]:
        """Every token the PII gate screens, and the tokens each stands for.

        Each field is screened as the catalog spells it, since a token written
        in another case, or with a grain suffix, reads the same field and must
        meet the same evidence; the refusal still names the caller's spelling.
        A column a value-returning measure reads directly (``MAX(customer.email)``)
        has no catalog token, so it is screened through its physical column,
        registered for this command in :meth:`_physical_columns`. A field a
        measure reads (``MAX(location)``) is screened as a grouped field is, so
        one computed over several tables is screened through every column it
        reads.
        """

        from ...metric_views.query import Resolved, value_columns

        refs: list[str] = []
        aliases: dict[str, list[str]] = {}
        for item in [*plan.fields, *plan.filters]:
            refs.append(item.token)
            aliases[item.token] = self._field_aliases(item)
        for measure in plan.measures:
            columns = value_columns(
                plan.view, measure.member, self._source.resolution(), self._fold
            )
            if columns is None:
                raise SemanticQueryRefusedError(
                    f"refused: dex could not read which columns measure "
                    f"{measure.token} returns the values of, so the PII gate cannot "
                    "screen them"
                )
            for column in columns:
                member = plan.view.member(column.field) if column.field else None
                if member is not None and column.token not in aliases:
                    aliases[column.token] = self._field_aliases(
                        Resolved(token=column.token, view=plan.view, member=member)
                    )
            refs.extend(self._register(columns))
        return list(dict.fromkeys(refs)), aliases

    def _field_aliases(self, item: Any) -> list[str]:
        """The tokens one field is screened as: its catalog token, and, where the
        catalog's lineage has no column or expression for it (a field computed
        over more than one table), every column its expression reads, so a
        neutral name cannot carry a flagged column through."""

        from ...metric_views.query import field_columns

        catalog = self._catalog_view()
        if item.canonical in {*catalog.physical_columns, *catalog.computed_columns}:
            return [item.canonical]
        columns = field_columns(
            item.view, item.member, self._source.resolution(), self._fold
        )
        if columns is None:
            raise SemanticQueryRefusedError(
                f"refused: dex could not read which columns field {item.base} "
                "reads, so the PII gate cannot screen them"
            )
        return [item.canonical, *self._register(columns)]

    def _register(self, columns: list[Any]) -> list[str]:
        """Each column's token, its physical column registered for the gate."""
        for column in columns:
            if column.gate is not None:
                self._base_columns[column.token] = column.gate
        return [column.token for column in columns]

    def _fold(self, column: str) -> str:
        """A column name spelled the way the connector folds an unquoted one."""
        from ...adapters import fold_identifier

        return fold_identifier(self._connector(), column)

    def _deployed(self, plan: Any, command: str) -> tuple[str, list[str]]:
        """Locate the deployed view and hold it to the repository, before any
        spend: its name, and notes for the result."""

        from ...metric_views.deployed import (
            LIMITED,
            REFUSED,
            LocateRefused,
            compare,
            locate,
            read_deployed,
            relied_on,
        )

        adapter = self._governed_sql().adapter(command)
        view = plan.view
        try:
            where = locate(adapter.list_metric_views(), view.name)
        except LocateRefused as exc:
            raise SemanticQueryRefusedError(str(exc)) from exc
        read = read_deployed(view.name, where, adapter.read_metric_view_yaml)
        if read.coverage.status == REFUSED or read.view is None:
            raise SemanticQueryRefusedError(
                f"refused: the metric view deployed as {where} could not be read "
                "back, so dex cannot hold it to the repository's definition: "
                + "; ".join(read.coverage.reasons)
            )
        reliance = relied_on(
            view,
            [item.member for item in plan.members],
            [item.member for item in plan.fields if item.grain is not None],
        )
        if reliance is None:
            raise SemanticQueryRefusedError(
                f"refused: dex could not read what this query relies on in view "
                f"'{view.name}' (an expression it reaches does not parse), so it "
                "cannot hold the deployment to the repository's definition"
            )
        differences = compare(view, read.view, reliance, unresolved=read.unresolved)
        if differences:
            listed = "; ".join(d.render() for d in differences[:5])
            more = f" (and {len(differences) - 5} more)" if len(differences) > 5 else ""
            raise SemanticQueryRefusedError(
                f"refused: the metric view deployed as {where} differs from the "
                f"repository's {view.file} on what this query reads: {listed}{more}. "
                "A governed query answers with the reviewed definition or not at "
                "all: redeploy the reviewed file, or update the repository to "
                "match what is deployed and review it"
            )
        notes = [
            f"executed against the metric view deployed as {where}, found by the "
            f"stem of {view.file} among the Metric Views the source allowlist "
            "holds; its deployed definition was read back from Unity Catalog and "
            "matches the file on everything this query reads"
        ]
        if read.coverage.status == LIMITED:
            notes.extend(read.coverage.reasons)
        if view.filter:
            notes.append(
                f"view '{view.name}' declares a `filter`, which Databricks applies "
                "to every query of it; dex does not repeat it"
            )
        return where, notes

    def _render(
        self,
        plan: Any,
        where: str,
        *,
        order_by: list[str] | None = None,
        limit: int | None = None,
    ) -> Any:
        from ...metric_views.query import QueryRefused

        try:
            return plan.render(
                where,
                order_by=order_by or [],
                limit=limit,
                max_rows=self._limits().max_rows,
            )
        except QueryRefused as exc:
            raise SemanticBackendError(str(exc)) from exc

    def _in_scope(self, plan: Any, command: str) -> list[str]:
        """The base tables the statement reaches, refused unless every one is in
        the scoped inventory.

        The statement is priced from those tables' sizes, and the inventory is
        the source allowlist, narrowed by ``--scope``: a table outside it has no
        size to read and would be quoted as nothing, while Databricks still scans
        it. The allowlist is a cost boundary a command may narrow and never
        widen, so such a query is refused by name rather than under-priced.
        """

        from ...adapters import normalize_relation
        from ...cache import match_identifier
        from ...metric_views.query import base_tables

        tables = base_tables(plan.view, plan)
        if tables is None:
            raise SemanticQueryRefusedError(
                f"refused: dex could not read which tables view '{plan.view.name}' "
                "reads for this query (its `source`, or the expression of a join "
                "or member it reaches), so it cannot price what the query would "
                "scan"
            )
        relations = [
            normalize_relation(_CONNECTOR, name) or name.lower() for name in tables
        ]
        adapter = self._governed_sql().adapter(command)
        listed = [meta.identifier for meta in adapter.list_objects()]
        outside = [
            name
            for name in relations
            if not match_identifier(name, listed)
            and not any(match_identifier(item, [name]) for item in listed)
        ]
        if outside:
            raise SemanticQueryRefusedError(
                f"refused: this query reads {', '.join(outside)} through the "
                "metric view, outside the source allowlist (or the --scope it was "
                "narrowed to), so dex cannot price what it would scan. Add the "
                "schema to databricks.catalogs in .dex/config.yml, or widen "
                "--scope to it"
            )
        # Priced by the inventory's own identifiers: a name written partially
        # (`core.orders`) has no size of its own to read, and would be quoted as
        # nothing while Databricks still scans it.
        resolved: list[str] = []
        for name in relations:
            matches = match_identifier(name, listed) or [
                item for item in listed if match_identifier(item, [name])
            ]
            for match in matches:
                if match.lower() not in resolved:
                    resolved.append(match.lower())
        return resolved

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
        raise SemanticQueryRefusedError(
            f"refused: {what} runs on the Databricks connector, and this "
            f"repository's connector is {connector or 'not set'}: a Metric View "
            "is a Unity Catalog object, queried with MEASURE() on a Databricks SQL "
            "warehouse. "
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
                    "a Metric View query runs through dex's own connection, so "
                    "build the layer from an engine "
                    "(LocalMetricViewLayer.from_engine)"
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
            columns = dict(self._catalog_view().physical_columns)
        except SemanticBackendError:
            columns = {}
        return {**columns, **self._base_columns}

    def _computed_columns(self) -> dict[str, tuple[str, str]]:
        try:
            return dict(self._catalog_view().computed_columns)
        except SemanticBackendError:
            return {}

    def _limits(self) -> QueryLimits:
        config = getattr(self._dex, "config", None)
        return getattr(config, "query", None) or QueryLimits()
