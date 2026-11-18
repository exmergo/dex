"""Cube executing the query: ``--api`` on either Cube deployment (#914).

The Cube server runs the query over ``/v1/load`` under its own warehouse
connection, on a Cube Core (``deployment: local``) or a Cube Cloud deployment
(``deployment: cube_cloud``). dex owns the request and the rows that come back,
and nothing in between, so ``execution`` is ``vendor``: no estimate, no ceiling,
and every result carries the cost-guard-unavailable warning.

**Where the catalog comes from follows the deployment.** Under ``local`` it is
the repository's reviewed model files, as on the render path, and before a query
is sent the Core's ``/v1/meta`` is held to them by the stale-renderer check: a
Core outside dev mode keeps serving the model it started with, and a result from
another model than the reviewed one would be a wrong number with nothing to say
so. Under ``cube_cloud`` the catalog is the deployment's compiled ``/v1/meta``,
which answers JavaScript, Jinja and ``extends`` definitions the static reader can
only name, and every result says it was not checked against the files. ``--local``
on ``cube_cloud`` is the render path against the files (:mod:`.cube`).

The order is the safety contract, as on the render path:

1. the ``--where`` read: a clause that is not a Cube filter, or whose members
   cannot be read, is refused before anything is sent;
2. the PII request-gate on every grouped member, the time dimension a grain
   buckets, and every member a filter names: a member the Cube model marks
   ``meta: {pii: true}`` is refused on that word, then the ``.dex/`` cache
   through the files' physical columns, then the name heuristic, disclosed
   wherever the heuristic alone spoke;
3. the grain check and the translation into Cube's query JSON;
4. under ``local``, the data-source check and the stale-renderer check;
5. ``/v1/load``, polled through "Continue wait";
6. the rows decoded (numbers Cube sends as text read by the annotation), named
   by the caller's tokens, and capped like every other semantic result.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...config import QueryLimits
from ...errors import ProjectError
from ...guards.pii_meta import says_pii
from ...semantic_hosts import CUBE_CLOUD_DEPLOYMENT
from ..results import SemanticQueryResult, SemanticValuesResult
from .backend import (
    EXECUTION_VENDOR,
    BackendDescriptor,
    SemanticBackendError,
    SemanticQueryRefusedError,
    cost_posture,
)
from .catalog import SemanticCatalog
from .cube import CUBE_CATALOG_GAPS
from .governed import GovernedSql
from .model import SemanticQuery
from .policy import (
    cap_columnar,
    name_only_note,
    requested_dimension_refs,
    resolve_values_request,
    screen_dimension_refs,
    screen_values_request,
    sub_threshold_note,
    unadjudicated_refs,
    validate_grain,
)

_RECOMPILE_HINT = "correct the Cube model's `sql_table`"


class CubeVendorLayer:
    """A Cube server executing the query, on either deployment."""

    vendor = "cube"
    execution = EXECUTION_VENDOR

    def __init__(
        self,
        source: Any,
        engine: Any = None,
        *,
        deployment: str = "local",
        token: Any = None,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        from ...cube.hosted import (
            CUBE_HOSTED_CATALOG_GAPS,
            CUBE_VENDOR_COST_WARNING,
        )

        self._source = source
        self._dex = engine
        self._cloud = deployment == CUBE_CLOUD_DEPLOYMENT
        self._token = token
        self._allowed_hosts = tuple(allowed_hosts)
        self.name = deployment
        self.deployment = deployment
        self.catalog_gaps = (
            CUBE_HOSTED_CATALOG_GAPS if self._cloud else CUBE_CATALOG_GAPS
        )
        self.cost_guard_warning = CUBE_VENDOR_COST_WARNING
        #: Where every result's definitions came from, said structurally: the
        #: Cloud deployment's compiled model, or (None) the repository's files.
        self.definition_source = CUBE_CLOUD_DEPLOYMENT if self._cloud else None
        self.descriptor = BackendDescriptor(
            name=deployment,
            vendor=self.vendor,
            deployment=deployment,
            execution=self.execution,
            catalog_gaps=self.catalog_gaps,
            cost_guard_warning=self.cost_guard_warning,
        )
        self._meta_body: dict[str, Any] | None = None
        self._files_view: Any = None
        self._hosted_view: Any = None
        self._transport_obj: Any = None
        self._governed: GovernedSql | None = None
        #: What this layer could not check, said on the result.
        self._notes: list[str] = []

    @classmethod
    def from_engine(cls, engine: Any, deployment: str = "local") -> CubeVendorLayer:
        if engine.repo_root is None:
            raise SemanticBackendError(
                "reading Cube models needs a repository to read them from: they "
                "are git-reviewable files, so build the engine with "
                "DexEngine.from_repo(repo_root)"
            )
        source = getattr(engine, "semantic_source", None)
        return cls(
            engine.semantic_catalog_source(),
            engine,
            deployment=deployment,
            token=getattr(source, "token", None),
            allowed_hosts=tuple(getattr(source, "allowed_hosts", ()) or ()),
        )

    def disclosure(self) -> list[str]:
        """The warnings every result of this layer carries, beyond the cost one."""

        from ...cube.hosted import CUBE_CLOUD_DISCLOSURE

        return [CUBE_CLOUD_DISCLOSURE] if self._cloud else []

    # ---- discovery ---------------------------------------------------------

    def list_definitions(self) -> SemanticCatalog:
        return SemanticCatalog.from_view(self._catalog_view(), self)

    def declared_relationships(self) -> list[Any]:
        """The repository's joins: the files are required on every deployment."""

        return self._source.declared_definitions().declared_relationships

    def declared_keys(self) -> tuple[list[Any], list[Any]]:
        defs = self._source.declared_definitions()
        return defs.declared_keys, defs.declared_composite_keys

    def declared_models(self) -> tuple[dict[str, Any], list[str]]:
        defs = self._source.declared_definitions()
        return defs.model_declarations, defs.declaration_notes

    def _catalog_view(self):
        if self._cloud:
            if self._hosted_view is None:
                from ...cube.hosted import meta_catalog

                self._hosted_view = meta_catalog(self._meta())
            return self._hosted_view
        return self._files()

    def _files(self):
        if self._files_view is None:
            try:
                self._files_view = self._source.semantic_catalog()
            except ProjectError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._files_view

    # ---- the server --------------------------------------------------------

    def _transport(self):
        from ...cube.transport import CubeTransport, CubeTransportError

        if self._transport_obj is None:
            try:
                self._transport_obj = CubeTransport(
                    getattr(self._source, "url", None),
                    deployment=self.deployment,
                    execution=EXECUTION_VENDOR,
                    token=self._token,
                    allowed_hosts=self._allowed_hosts,
                )
            except CubeTransportError as exc:
                raise SemanticQueryRefusedError(str(exc)) from exc
        return self._transport_obj

    def _meta(self) -> dict[str, Any]:
        from ...cube.transport import CubeTransportError

        if self._meta_body is None:
            try:
                self._meta_body = self._transport().meta()
            except CubeTransportError as exc:
                raise SemanticBackendError(str(exc)) from exc
        return self._meta_body

    def _members(self):
        if self._cloud:
            from ...cube.hosted import meta_members

            return meta_members(self._meta())
        from ...cube.query import index

        loaded = self._source.loaded()
        return index(loaded.cubes, loaded.views)

    def _load(self, request: Any, what: str) -> dict[str, Any]:
        """Hold a Core to the files, then have the server run ``request``."""

        from ...cube.query import (
            CubeQueryError,
            CubeRefusalError,
            check_data_sources,
            check_renderer,
        )
        from ...cube.transport import CubeTransportError

        transport = self._transport()
        try:
            if not self._cloud:
                loaded = self._source.loaded()
                check_data_sources(
                    request.cubes,
                    {cube.name: cube.data_source for cube in loaded.cubes},
                    getattr(self._source, "data_source", None) or "default",
                )
                check_renderer(
                    self._meta(),
                    request.members,
                    self._members(),
                    where=transport.base_url,
                    cubes={cube.name: cube for cube in loaded.cubes},
                )
                self._check_core_relations(request)
            return transport.load(request.query)
        except CubeRefusalError as exc:
            raise SemanticQueryRefusedError(str(exc)) from exc
        except CubeQueryError as exc:
            raise SemanticBackendError(str(exc)) from exc
        except CubeTransportError as exc:
            if exc.resolution:
                raise SemanticBackendError(f"could not resolve {what}: {exc}") from exc
            raise SemanticBackendError(str(exc)) from exc

    def _check_core_relations(self, request: Any) -> None:
        """Hold the relations a Core would read to the files' ``sql_table``.

        ``/v1/meta`` carries no ``sql_table``, so a Core still serving a model
        whose cube read another table passes the member check. The Core is asked
        to plan the query over ``/v1/sql`` (which runs nothing) through a render
        transport of its own, and the plan is held to the files exactly as on
        the render path. Where that cannot be judged (dex has no connector whose
        dialect reads the plan, or Cube plans over a pre-aggregation, which a
        load may use), the result says the relations were not checked.
        """

        from ...adapters import get_dialect
        from ...cube.query import CUBE_CONNECTORS
        from ...cube.transport import CubeTransport
        from .cube import LocalCubeLayer

        connector = (
            getattr(self._dex, "connector", None)
            or getattr(getattr(self._dex, "config", None), "connector", None)
            or getattr(self._source, "connector", None)
        )
        if connector not in CUBE_CONNECTORS:
            self._notes.append(
                "the tables this Cube Core reads were not checked against the "
                "repository's sql_table: dex has no connector whose dialect reads "
                "the Core's plan"
            )
            return
        render = CubeTransport(getattr(self._source, "url", None))
        body = render.sql(request.query)
        plan = body.get("sql") if isinstance(body, dict) else None
        if not isinstance(plan, dict) or not isinstance(plan.get("sql"), list):
            self._notes.append(
                "the tables this Cube Core reads were not checked against the "
                "repository's sql_table: its plan carried no SQL"
            )
            return
        if plan.get("preAggregations"):
            self._notes.append(
                "the tables this Cube Core reads were not checked against the "
                "repository's sql_table: it answers this query from a "
                "pre-aggregation"
            )
            return
        LocalCubeLayer(self._source, self._dex)._check_relations(
            plan["sql"][0], get_dialect(connector)
        )

    # ---- the PII gate ------------------------------------------------------

    def _lookup(self):
        """The gate's lookup, in order:

        1. the model's own ``meta: {pii: true}`` on the member, or on the cube
           member a view member exposes (``aliasMember``);
        2. the cache through the files' physical columns. On Cube Cloud a
           column the files name is not proven to be the one the deployment
           reads, so there the cache may refuse a member and never clears one;
        3. otherwise None, which leaves the name heuristic in charge, and the
           result discloses that it alone spoke.

        A ``/v1/meta`` that cannot be read refuses the query: screening without
        the model's own marks and then sending it would be the gate passing
        on half its evidence.
        """

        from ...cube.hosted import meta_pii

        marked = meta_pii(self._meta())
        members = self._members()
        governed = self._governed_sql()
        cached = governed.pii_lookup(governed.load_cache()) if governed else None
        cloud = self._cloud

        def lookup(ref: str) -> Any:
            own = marked.get(ref)
            if says_pii(own):
                return own
            target = members.get(ref)
            behind = marked.get(target.alias_of) if target and target.alias_of else None
            if says_pii(behind):
                return behind
            verdict = cached(ref) if cached is not None else None
            if cloud and verdict is not None and not says_pii(verdict):
                verdict = None
            if verdict is not None:
                return verdict
            return own if own is not None else behind

        return lookup

    def _governed_sql(self) -> GovernedSql | None:
        if self._dex is None:
            return None
        if self._governed is None:
            self._governed = GovernedSql(
                self._dex,
                self._dex.connector or getattr(self._dex.config, "connector", "") or "",
                self._limits(),
                self._physical_columns,
                recompile_hint=_RECOMPILE_HINT,
                computed_columns=self._computed_columns,
            )
        return self._governed

    def _physical_columns(self) -> dict[str, tuple[str, str]]:
        try:
            return dict(self._files().physical_columns)
        except SemanticBackendError:
            return {}

    def _computed_columns(self) -> dict[str, tuple[str, str]]:
        try:
            return dict(self._files().computed_columns)
        except SemanticBackendError:
            return {}

    # ---- query -------------------------------------------------------------

    def filter_refs(self, clauses: list[str]) -> list[str] | None:
        from ...cube_dialect import CubeFilterError, filter_refs, parse_where

        try:
            return filter_refs(parse_where(clauses))
        except CubeFilterError:
            return None

    def time_axis_refs(self, metrics: list[str]) -> list[str] | None:
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
        members = self._members()
        refs = requested_dimension_refs(q, filter_refs=self.filter_refs)
        aliases = {
            token: [name]
            for token in refs
            if (name := split_time(token, members)[0]) != token
        }
        lookup = self._lookup()
        blocked = screen_dimension_refs(refs, meta_lookup=lookup, aliases=aliases)
        if blocked:
            named = ", ".join(f"{ref} ({reason})" for ref, reason in blocked)
            raise SemanticQueryRefusedError(
                f"refused: grouping or filtering by {named} would surface PII. "
                "PII is flagged, never surfaced; query a non-PII dimension instead."
            )
        notes = [
            *name_only_note(
                unadjudicated_refs(refs, meta_lookup=lookup, aliases=aliases)
            ),
            *sub_threshold_note(refs, meta_lookup=lookup, aliases=aliases),
        ]

        q = replace(q, grain=validate_grain(q.grain, available=None))
        try:
            request = to_cube_query(q, members, max_rows=self._limits().max_rows)
        except CubeQueryError as exc:
            raise SemanticBackendError(str(exc)) from exc
        body = self._load(request, "the metric query")
        notes += self._notes
        cost, warnings = cost_posture(self)
        return SemanticQueryResult.from_capped(
            self._shape(body, request, notes),
            backend=self,
            cost=cost,
            warnings=warnings,
        )

    def values(self, dimension: str, metrics: list[str]) -> SemanticValuesResult:
        from ...cube.query import CubeQueryError, to_cube_values_query
        from ...cube.validator import PREDEFINED_GRANULARITIES

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
        notes = screen_values_request(request.name, meta_lookup=self._lookup())
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
        body = self._load(cube_request, f"the values of {request.token}")
        notes = [*notes, *self._notes]
        cost, warnings = cost_posture(self)
        return SemanticValuesResult.from_capped(
            self._shape(body, cube_request, notes),
            backend=self,
            dimension=request.token,
            scoped_to=request.metrics,
            cost=cost,
            warnings=warnings,
        )

    def _shape(
        self, body: dict[str, Any], request: Any, notes: list[str]
    ) -> dict[str, Any]:
        """The rows as columns in the order Cube's own SQL selects them
        (dimensions, then time buckets, then measures), so ``--api`` and the
        render path answer one query with one column order."""

        from ...cube.hosted import decode_load

        query = request.query
        order = [
            *query.get("dimensions", []),
            *(
                f"{t['dimension']}.{t['granularity']}"
                for t in query.get("timeDimensions", [])
                if t.get("granularity")
            ),
            *query.get("measures", []),
        ]
        columns, types, cells = decode_load(body, request.names, order)
        limits = self._limits()
        return cap_columnar(
            columns,
            types,
            cells,
            max_rows=limits.max_rows,
            max_cell_chars=limits.max_cell_chars,
            max_payload_bytes=limits.max_payload_bytes,
            extra_notes=notes,
        )

    def _limits(self) -> QueryLimits:
        config = getattr(self._dex, "config", None)
        return getattr(config, "query", None) or QueryLimits()
