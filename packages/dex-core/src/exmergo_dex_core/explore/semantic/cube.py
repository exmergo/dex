"""The local Cube layer: the Cube model files in this repository, queried.

The reading is the semantic source's job and reaches this layer through the
engine's semantic-source seam. What this layer adds is provenance, the declared
gaps, and the governed metric query.

**The catalog needs no Cube server.** ``explore semantic list`` reads the
repository's plain-YAML cubes and views, statically, and the declaration channel
carries their primary keys and joins to ``explore relationships|map
--use-project``.

**Cube renders, dex executes.** Cube has no Python SQL renderer: its planner
ships only inside a Cube server, which renders a query over ``/v1/sql`` without
executing it. ``query`` asks a Cube server for that SQL and runs it through the
governed spine, in this order, because the safety contract is the order:

1. the connector check: DuckDB, Postgres, BigQuery, Snowflake and Redshift,
   each held to a golden of what Cube renders for it;
2. the PII request-gate on the grouped members, on the time dimension a grain
   buckets, and on every member a ``--where`` filter names, read from Cube's
   JSON filter (:mod:`...cube_dialect`) and looked up in the ``.dex/`` cache
   through the catalog's ``physical_columns``, with the name heuristic as the
   floor;
3. the grain check, against each time dimension's built-in and custom grains;
4. translation into Cube's query JSON (:mod:`...cube.query`), and the
   data-source check: every cube the query reaches is on
   ``semantic.cube.data_source``;
5. the stale-renderer check: every member the query names is compared with the
   server's ``/v1/meta``, since a renderer is not a reviewed artifact and a Cube
   server outside dev mode keeps serving the model it started with;
6. the render, over ``/v1/sql`` (:mod:`...cube.transport`), and the plan checks:
   one statement, no pre-aggregation, no external store, no lambda query, the
   configured data source, and only the relations the repository's cubes name;
7. the parameters written in as literals of dex's dialect, refusing a
   placeholder style that says the server renders for another warehouse;
8. the governed spine (:mod:`.governed`): SELECT-only, the relation pre-check,
   the cost handshake, dex's connector, the columnar cap.

The Cube server never executes under dex and never holds a warehouse
credential: dex calls ``/v1/meta`` and ``/v1/sql`` and nothing else. So
``execution`` is ``dex`` and no cost-guard-unavailable warning applies. Result
columns carry the tokens the caller wrote (``orders.status``,
``orders.ordered_at__month``), on every connector.

``values`` runs the same spine behind a different front: the token is resolved
against the catalog (an unknown one is refused by name), the values gate refuses
a PII dimension outright, and the render is a dimension-only query rooted at the
dimension's own cube or, with ``--metric``, carrying that measure, which
``scoped_to`` reports.
"""

from __future__ import annotations

from dataclasses import replace
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
    requested_dimension_refs,
    resolve_values_request,
    screen_dimension_refs,
    screen_values_request,
    unadjudicated_refs,
    validate_grain,
)

# Fields of the neutral catalog Cube has nowhere to put, per element kind.
#
# Cube declares no entities: its primary keys and joins reach dex through the
# declaration channel instead. A cube sits on a relation, not on a dbt model,
# and names no primary entity because it has none.
CUBE_CATALOG_GAPS: dict[str, list[str]] = {
    "semantic_models": ["model_ref", "primary_entity"],
    "entities": ["name", "type", "label", "description", "roles"],
}

# A relation that resolves to the wrong namespace is a cube whose `sql_table`
# names another target, since Cube renders it into the SQL verbatim.
_RECOMPILE_HINT = "correct the Cube model's `sql_table`"


class LocalCubeLayer:
    """The Cube model files committed to this repository."""

    name = "local"
    vendor = "cube"
    deployment = "local"
    execution = EXECUTION_DEX
    catalog_gaps = CUBE_CATALOG_GAPS
    descriptor = BackendDescriptor(
        name=name,
        vendor=vendor,
        deployment=deployment,
        execution=execution,
        catalog_gaps=CUBE_CATALOG_GAPS,
    )

    def __init__(self, source: Any, engine: Any = None) -> None:
        self._source = source
        # The dex engine, which the governed spine opens the connection through.
        # Optional: reading the catalog needs no engine.
        self._dex = engine
        self._view: Any = None
        self._governed: GovernedSql | None = None

    @classmethod
    def from_engine(cls, engine: Any) -> LocalCubeLayer:
        """Build through the engine's semantic-source seam, never a project."""

        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Cube models needs a repository to read them from: they "
                "are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        return cls(engine.semantic_catalog_source(), engine)

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """Cube's joins, with every ordered column pair."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        """Cube's primary keys. Cube is never the transformation project, so
        this is the only route its keys have to grain detection."""

        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def _catalog_view(self):
        if self._view is None:
            try:
                self._view = self._source.semantic_catalog()
            except ProjectError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._view

    # ---- query -------------------------------------------------------------

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        """The ``cube.member`` tokens these Cube JSON filters name, or None.

        None for a clause that is not a Cube filter, or one whose structure
        cannot be read, which the gate refuses rather than screening by half.
        """

        from ...cube_dialect import CubeFilterError, filter_refs, parse_where

        try:
            return filter_refs(parse_where(clauses))
        except CubeFilterError:
            return None

    def time_axis_refs(self, metrics: list[str]) -> list[str] | None:
        """The ``cube.dimension`` tokens a time grouping on these metrics buckets.

        A metric names a time axis only where its cube has exactly one time
        dimension; None otherwise, which leaves the gate on the name heuristic.
        Cube has no ``metric_time``, so a query always names the time dimension
        it buckets and the gate screens that token directly.
        """

        from ...semantic_catalog import time_axis_tokens

        try:
            return time_axis_tokens(self._catalog_view(), metrics)
        except SemanticBackendError:
            return None

    def query(self, q: SemanticQuery) -> SemanticQueryResult:
        from ...cube.query import CubeQueryError, split_time, to_cube_query
        from ...cube_dialect import CubeFilterError, parse_where

        if not q.metrics:
            raise SemanticBackendError("a metric query needs at least one --metric")
        connector = self._supported_connector()
        governed = self._governed_sql()
        members = self._members()
        cache = governed.load_cache()

        # A `--where` that is not a Cube filter is refused for what it is, before
        # the gate refuses it as a filter it could not read.
        if q.where:
            try:
                parse_where(list(q.where))
            except CubeFilterError as exc:
                raise SemanticBackendError(str(exc)) from exc
            if self.filter_refs(list(q.where)) is None:
                raise SemanticQueryRefusedError(
                    "refused: dex could not read which members this --where names, "
                    "so it cannot be screened for PII. A Cube filter is "
                    '{"member": "cube.member", "operator": <one of Cube\'s '
                    'operators>, "values": [strings or numbers]}, or an "and"/"or" '
                    "group of them, with no other keys; correct the clause, or move "
                    "the condition into --group-by"
                )

        # PII request-gate, before anything is sent. A grouped time dimension
        # carries its grain in the token, so the gate screens the dimension it
        # buckets in its place.
        refs = requested_dimension_refs(q, filter_refs=self.filter_refs)
        aliases = {
            token: [name]
            for token in refs
            if (name := split_time(token, members)[0]) != token
        }
        lookup = governed.pii_lookup(cache)
        blocked = screen_dimension_refs(refs, meta_lookup=lookup, aliases=aliases)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII. "
                "PII is flagged, never surfaced; query a non-PII dimension instead."
            )

        # The grain's shape here; whether the time dimension has it is the
        # translation's check, since each Cube time dimension declares its own.
        q = replace(q, grain=validate_grain(q.grain, available=None))
        try:
            request = to_cube_query(q, members, max_rows=self._limits().max_rows)
        except CubeQueryError as exc:
            raise SemanticBackendError(str(exc)) from exc
        sql, names = self._render(request, connector, "the metric query")

        return governed.run(
            sql,
            cache,
            backend=self,
            command="explore semantic query",
            result_type=SemanticQueryResult,
            extra_notes=name_only_note(
                unadjudicated_refs(refs, meta_lookup=lookup, aliases=aliases)
            ),
            column_names=names,
            utc_session=True,
        )

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        """One dimension's value domain, rendered by Cube and executed here."""

        from ...cube.query import CubeQueryError, to_cube_values_query
        from ...cube.validator import PREDEFINED_GRANULARITIES

        connector = self._supported_connector()
        governed = self._governed_sql()
        members = self._members()
        grains = tuple(
            dict.fromkeys(
                [
                    *PREDEFINED_GRANULARITIES,
                    *(g for t in members.values() for g in t.member.granularities),
                ]
            )
        )
        request = resolve_values_request(
            self._catalog_view(),
            dimension,
            metrics,
            grains=grains,
            spelling="named cube.member (orders.status)",
        )
        cache = governed.load_cache()
        notes = screen_values_request(
            request.name, meta_lookup=governed.pii_lookup(cache)
        )
        try:
            cube_request = to_cube_values_query(
                request.name,
                request.grain,
                request.metrics,
                members,
                max_rows=self._limits().max_rows,
            )
        except CubeQueryError as exc:
            raise SemanticBackendError(str(exc)) from exc
        sql, names = self._render(
            cube_request, connector, f"the values of {request.token}"
        )

        return governed.run(
            sql,
            cache,
            backend=self,
            command="explore semantic values",
            result_type=SemanticValuesResult,
            extra_notes=notes,
            column_names=names,
            utc_session=True,
            dimension=request.token,
            scoped_to=request.metrics,
        )

    # ---- rendering ---------------------------------------------------------

    def _render(
        self, request: Any, connector: str, what: str
    ) -> tuple[str, dict[str, str]]:
        """The bound statement Cube plans for ``request``, and its result names.

        Everything between the translation and the spine: the data-source
        check, the stale-renderer check, the render, the plan checks, the
        relations the plan reads, and the parameter binding.
        """

        from ...adapters import get_dialect
        from ...cube.query import (
            CubeQueryError,
            CubeRefusalError,
            bind_params,
            check_data_sources,
            check_plan,
            check_renderer,
            result_names,
        )
        from ...cube.transport import CubeTransportError

        data_source = getattr(self._source, "data_source", None) or "default"
        dialect = get_dialect(connector)
        loaded = self._source.loaded()
        try:
            check_data_sources(
                request.cubes,
                {cube.name: cube.data_source for cube in loaded.cubes},
                data_source,
            )
            transport = self._transport()
            try:
                meta = transport.meta()
                check_renderer(
                    meta,
                    request.members,
                    self._members(),
                    where=transport.base_url,
                    cubes={cube.name: cube for cube in loaded.cubes},
                )
                body = transport.sql(request.query)
            except CubeTransportError as exc:
                if exc.resolution:
                    raise SemanticBackendError(
                        f"could not resolve {what}: {exc}"
                    ) from exc
                raise SemanticBackendError(str(exc)) from exc
            plan = check_plan(body, data_source=data_source)
            self._check_relations(plan.sql, dialect)
            sql = bind_params(plan.sql, plan.params, connector, dialect)
        except CubeRefusalError as exc:
            raise SemanticQueryRefusedError(str(exc)) from exc
        except CubeQueryError as exc:
            raise SemanticBackendError(str(exc)) from exc
        return sql, result_names(plan.aliases, request.names)

    def _check_relations(self, sql: str, dialect: str) -> None:
        """Refuse a plan reading a relation no cube in the repository sits on.

        Cube renders a cube's ``sql_table`` verbatim, so a relation outside that
        set is a pre-aggregation table, a cube the server has and the repository
        does not, or a ``sql_table`` changed since the server started.
        """

        from ...cache import match_identifier
        from ...cube.query import CubeRefusalError
        from ...guards.sql_guard import referenced_relations

        allowed = self._repository_relations(dialect)
        if allowed is None:
            return
        foreign = [
            name
            for name in referenced_relations(sql, dialect=dialect)
            if not match_identifier(name, allowed)
            and not any(match_identifier(table, [name]) for table in allowed)
        ]
        if foreign:
            raise CubeRefusalError(
                f"refused: Cube planned this query over {', '.join(sorted(foreign))}, "
                "which no cube in this repository's models sits on: a "
                "pre-aggregation table, or a model the Cube server holds and the "
                "repository does not. Restart the Cube server on the repository's "
                "current files"
            )

    def _repository_relations(self, dialect: str) -> list[str] | None:
        """Every relation the repository's cubes read, as their files spell it,
        or None where one cannot be read (a templated ``sql_table``, a ``sql``
        cube that does not parse), which leaves the check to the spine's
        relation pre-check alone."""

        from ...guards.sql_guard import referenced_relations

        relations: list[str] = []
        for cube in self._source.loaded().cubes:
            if cube.sql_table is not None:
                table = cube.sql_table.strip()
                if "{" in table or "\\" in table:
                    return None
                relations.append(table.replace('"', "").replace("`", ""))
                continue
            try:
                relations += referenced_relations(cube.sql or "", dialect=dialect)
            except Exception:
                return None
        return relations

    def _transport(self):
        from ...cube.transport import CubeTransport, CubeTransportError

        try:
            return CubeTransport(getattr(self._source, "url", None))
        except CubeTransportError as exc:
            raise SemanticQueryRefusedError(str(exc)) from exc

    def _members(self):
        from ...cube.query import index

        loaded = self._source.loaded()
        return index(loaded.cubes, loaded.views)

    def _supported_connector(self) -> str:
        from ...cube.query import CUBE_CONNECTORS, UNSUPPORTED_CONNECTORS

        connector = self._connector() or ""
        if connector in CUBE_CONNECTORS:
            return connector
        why = UNSUPPORTED_CONNECTORS.get(connector, "it is not one Cube renders for")
        raise SemanticBackendError(
            f"a governed Cube query runs on {', '.join(CUBE_CONNECTORS)} in this "
            f"release, and not on {connector or 'no connector'}: {why}. Read the "
            "layer with `explore semantic list`, then query the relations it names "
            "with `explore query`, which runs under the firewall and the cost guard"
        )

    def _connector(self) -> str | None:
        if self._dex is not None:
            return self._dex.connector or getattr(self._dex.config, "connector", None)
        return getattr(self._source, "connector", None)

    def _governed_sql(self) -> GovernedSql:
        if self._governed is None:
            if self._dex is None:
                raise SemanticBackendError(
                    "a Cube metric query runs through dex's own connection, so "
                    "build the layer from an engine (LocalCubeLayer.from_engine)"
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
