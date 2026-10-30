"""The governed spine: everything that happens to rendered SQL before a result.

A semantic layer that renders its own SQL and leaves execution to dex (``execution:
dex``) hands the statement here, and from here on nothing depends on which layer
rendered it. The order is the safety contract rather than a sequence that happens
to be written this way, and this is the one place it is stated:

1. **SELECT-only.** The statement is proven read-only before anything touches the
   connection, because the pre-check below may introspect it.
2. **Relation pre-check.** Every relation the statement reads is resolved against
   the ``.dex/`` cache and then the connection's own inventory, so a namespace
   mismatch is refused by name rather than billed as a failed job.
3. **Cost handshake.** The adapter prices the statement and a billed connector
   stops for ``--confirm --budget`` before any spend.
4. **Run**, through dex's own connector, under the query limits.
5. **Cap** the columnar result, disclosing every cut and any relation the PII
   gate could only screen by name.
6. **Stamp spend** onto the record that comes back.

What comes before rendering belongs to the layer, because only the layer can read
its own request: the PII request-gate on the grouped and filtered dimensions, and
the grain check. The gate's evidence is shared, though: :meth:`GovernedSql.pii_lookup`
resolves a dimension token to its physical column through the ``physical_columns``
mapping the layer supplies, and answers from that column's cached flag, or, for an
element defined as an expression, to every column the expression reads.

Every layer that renders metric SQL runs through this for one reason: a second
copy of the order is a second place for it to drift, and the drift would be
silent. So this module imports no layer: neither MetricFlow nor SLayer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ... import command_args
from ...adapters import get_dialect
from ...cache import mask_overridden, match_identifier
from ...config import QueryLimits, pii_override_paths
from ...guards import PII_BLOCK_CONFIDENCE
from .backend import SemanticBackendError, SemanticQueryRefusedError
from .policy import READS, UNREADABLE, cap_columnar

#: ``dimension token -> (relation, physical column)``, as the layer resolves it.
PhysicalColumns = Mapping[str, tuple[str, str]]
#: ``dimension token -> (relation, SQL expression)``, for a computed element.
ComputedColumns = Mapping[str, tuple[str, str]]


class GovernedSql:
    """Run SQL a semantic layer rendered through dex's guards, in dex's order.

    ``physical_columns`` is asked for the mapping only when the PII lookup needs
    it, so a layer whose catalog read is the expensive part never pays for it on
    a command with no cache to look the columns up in. ``computed_columns`` is
    the same for elements defined as an expression, and is asked even without a
    cache, because reading the expression is what lets the gate screen the
    columns behind the element rather than its name. A layer that cannot
    answer returns an empty mapping, which leaves the gate on the name heuristic:
    the fail-closed floor, and the posture an unprofiled relation already gets.

    ``recompile_hint`` is how the layer tells a caller to re-read its definitions
    against another target (``re-run `dbt parse```), because a relation that
    resolves to the wrong namespace is almost always a layer compiled elsewhere.
    """

    def __init__(
        self,
        engine,
        connector: str,
        limits: QueryLimits,
        physical_columns: Callable[[], PhysicalColumns],
        *,
        recompile_hint: str,
        computed_columns: Callable[[], ComputedColumns] | None = None,
    ) -> None:
        # The dex engine, not an adapter: the connection is opened through it on
        # the one billed path below, so no layer becomes a second place that
        # discovers credentials or builds a cost gate.
        self._dex = engine
        self._connector = connector
        self._limits = limits
        self._physical_columns = physical_columns
        self._computed_columns = computed_columns or dict
        self._recompile_hint = recompile_hint

    def run(
        self,
        sql: str,
        cache,
        *,
        backend: Any,
        command: str,
        result_type,
        extra_notes: list[str] | None = None,
        column_names: Mapping[str, str] | None = None,
        utc_session: bool = False,
        **fields,
    ):
        """Take rendered SQL to a stamped result, in the order the module states.

        ``backend`` is the answering backend itself, whose provenance the result
        carries; ``fields`` are the result type's own fields beyond the columnar
        payload. ``column_names`` renames a column the warehouse returned under
        the name the statement had to emit to the name the layer reports it by,
        for a layer that encodes its result names to suit a dialect.
        ``utc_session`` runs the statement in a UTC session, for a layer whose
        rendered SQL assumes its own driver's (Cube's): an adapter whose
        sessions carry no time zone (BigQuery, whose ``TIMESTAMP`` is UTC) has
        nothing to pin.
        """

        from ...guards.sql_guard import NotSelectOnlyError, assert_select_only

        dialect = get_dialect(self._connector)
        try:
            assert_select_only(sql, dialect=dialect)
        except NotSelectOnlyError as exc:
            raise SemanticQueryRefusedError(
                f"rendered metric SQL was not read-only: {exc}"
            ) from exc

        # One adapter for the whole command: the pre-check may introspect the
        # connection, and `_adapter` rebuilds the cost gate on every call, so
        # asking twice would settle and rebuild a gate for nothing.
        adapter = self._dex._adapter(command)
        if utc_session and (pin := getattr(adapter, "pin_utc_session", None)):
            pin()
        refusal, unprofiled = self.relation_precheck(
            sql,
            cache,
            dialect,
            lambda: [meta.identifier for meta in adapter.list_objects()],
        )
        if refusal is not None:
            raise SemanticBackendError(refusal)

        estimate_fn = getattr(adapter, "query_estimate", None)
        estimate = estimate_fn(sql) if estimate_fn else 0.0
        command_args.billed_handshake(command, adapter, estimate)
        result = adapter.run_query(
            sql,
            max_rows=self._limits.max_rows,
            timeout_seconds=self._limits.timeout_seconds,
        )
        columns = list(result.columns)
        if column_names:
            columns = [column_names.get(name, name) for name in columns]
        capped = cap_columnar(
            columns,
            result.types,
            result.cells,
            max_rows=self._limits.max_rows,
            max_cell_chars=self._limits.max_cell_chars,
            max_payload_bytes=self._limits.max_payload_bytes,
            truncated_by_source=result.truncated,
            extra_notes=[*(extra_notes or []), *unprofiled_note(unprofiled)],
        )
        record = result_type.from_capped(capped, backend=backend, **fields)
        return command_args.stamp_spend(record, adapter)

    def load_cache(self):
        """The exploration cache with config PII overrides applied in memory, or None.

        Absence is not fatal here (unlike ``explore query``, whose whole policy is
        the cache: it tracks PII taint through the projection of agent-authored SQL
        and cannot do that for a relation it has never seen). A metric query is
        governed at the request, by dimension name, before any SQL exists, so the
        name heuristic and the semantic layer's own metadata still bind, and the
        relation pre-check falls through to the live inventory. A repo that never
        ran ``explore map`` can still query metrics.
        """

        try:
            cache = self._dex.store.load_cache()
        except Exception:
            return None
        if cache is None:
            return None
        config = self._dex.config
        overrides = pii_override_paths(getattr(config, "pii_overrides", []) or [])
        if not overrides:
            return cache
        return mask_overridden(cache, overrides)

    def pii_lookup(self, cache):
        """A ``dimension token -> verdict`` lookup backed by the cache.

        A semantic dimension (``session__is_deleted``) maps to a physical column on
        its owning model, so the token is resolved through ``physical_columns`` to
        (relation, column) and that column's cached PII flag decides. This is the
        value-evidence-backed adjudication the profiler produced; the name
        heuristic in ``screen_dimension_refs`` remains the floor underneath it, so
        an unprofiled column is still caught by its name.

        A token defined as an expression resolves through ``computed_columns``
        instead, and is read for every column the expression reads: any of them
        flagged refuses it, and it is cleared only where every one of them was
        profiled and cleared. The columns travel back under ``reads`` so the name
        heuristic screens them too, and an expression that cannot be read comes
        back ``unreadable``, which the gate refuses rather than passing on the
        element's name.

        Returns None for a token neither map speaks to, which leaves the
        heuristic in charge.
        """

        # Without a cache no column can be adjudicated, so the mapping is not
        # asked for. An expression is still read: the columns it reads are what
        # the name heuristic screens, and one it cannot read is refused.
        columns = self._physical_columns() if cache is not None else {}
        computed = self._computed_columns()
        if not columns and not computed:
            return None
        datasets = list(cache.datasets) if cache is not None else []
        known = [dataset.identifier for dataset in datasets]

        def verdict(relation: str, column_name: str):
            # A partially qualified relation can match more than one dataset.
            # Any of them flagging the column refuses it, and it is cleared only
            # where every match profiled the column and cleared it: one that
            # cannot speak leaves the name heuristic in charge.
            matches = set(match_identifier(relation, known))
            cleared: set[str] = set()
            for dataset in datasets:
                if dataset.identifier not in matches:
                    continue
                for column in dataset.columns:
                    if column.name.lower() != column_name.lower():
                        continue
                    flag = column.pii
                    if flag is not None and flag.confidence >= PII_BLOCK_CONFIDENCE:
                        return {"pii": True, "category": flag.category.value}
                    cleared.add(dataset.identifier)
            if matches and cleared == matches:
                # Profiled and cleared (or a human override): authoritative,
                # so say so rather than leaving it to the name heuristic.
                return {"pii": False}
            return None

        try:
            dialect: str | None = get_dialect(self._connector)
        except ValueError:
            # No dialect to read an expression in, so none can be read.
            dialect = None

        def lookup(ref: str):
            target = columns.get(ref)
            if target is not None:
                return verdict(*target)
            expression = computed.get(ref)
            if expression is None:
                return None
            relation, sql = expression
            reads = expression_columns(sql, dialect) if dialect else None
            if reads is None:
                return {UNREADABLE: True}
            verdicts = [verdict(relation, column) for column in reads]
            for column, found in zip(reads, verdicts, strict=True):
                if found is not None and found.get("pii"):
                    return {**found, "column": column, READS: reads}
            if all(found is not None for found in verdicts):
                # Every column it reads was profiled and cleared, which includes
                # an expression that reads none: a constant carries no data.
                return {"pii": False, READS: reads}
            return {READS: reads}

        return lookup

    def relation_precheck(
        self,
        sql: str,
        cache,
        dialect: str,
        live_identifiers: Callable[[], list[str]],
    ) -> tuple[str | None, list[str]]:
        """``(refusal message or None, relations with no profile)`` for the
        rendered SQL.

        A semantic layer bakes the relations it was compiled against straight into
        the SQL, so a layer compiled against another database (or a different dev
        target) renders relations that do not exist here. Catching that is worth a
        precise message before the cost handshake rather than a table-not-found
        from the warehouse.

        The authority is the *connection*, not the ``.dex/`` cache. That cache
        records what has been profiled, which is a different question: a model
        ``transform build`` created minutes ago is in the warehouse and not in the
        cache, and refusing it made "build a model, then validate its metric"
        impossible without a profiling pass in between. So the cache is only a free
        fast path, and anything it cannot resolve is asked of the live inventory,
        the same authority ``explore profile`` resolves its arguments against.

        What the listing can and cannot settle is
        :func:`~...cache.relation_verdict`. An inventory that cannot be read at all
        settles nothing, and a relation that is genuinely absent still fails at
        planning without billing.

        Resolution is by suffix in both directions, because the cache and the
        inventory are namespace-normalized per connector and an exact string
        compare would reject legitimate spellings.
        """

        # Imported here, not at module scope: `explore semantic` is routed from
        # its own module so a remote-only install with no dialect engine can reach
        # the hosted backend, and a top-level guards import would undo that.
        from ...cache import relation_verdict
        from ...guards.sql_guard import referenced_relations

        relations = referenced_relations(sql, dialect=dialect)
        if not relations:
            return None, []

        datasets = list(cache.datasets) if cache is not None else []
        cached = [dataset.identifier for dataset in datasets]
        # Presence is not a profile: `explore map` writes inventory-only entries
        # with no columns, and those tell the PII gate nothing.
        profiled = {dataset.identifier for dataset in datasets if dataset.columns}

        unresolved: list[str] = []
        unprofiled: list[str] = []
        for name in relations:
            matches = match_identifier(name, cached)
            if not matches:
                unresolved.append(name)
                unprofiled.append(name)
            elif not any(match in profiled for match in matches):
                unprofiled.append(name)
        if not unresolved:
            return None, unprofiled

        try:
            live = live_identifiers()
        except Exception:
            return None, unprofiled

        verdicts: dict[str, list[str]] = {"foreign": [], "missing": []}
        for name in unresolved:
            if match_identifier(name, live):
                continue
            verdict = relation_verdict(name, live)
            if verdict is not None:
                verdicts[verdict].append(name)

        if verdicts["foreign"]:
            named = ", ".join(sorted(set(verdicts["foreign"])))
            return (
                f"refused: the metric query reads {named}, in a namespace this "
                "connection does not reach. The project was compiled against a "
                "different namespace than the one dex is connected to; "
                f"{self._recompile_hint} against the target you are querying, or "
                "point dex at the connection the project was built for.",
                unprofiled,
            )
        if verdicts["missing"]:
            named = ", ".join(sorted(set(verdicts["missing"])))
            return (
                f"refused: the metric query reads {named}, which this connection "
                "does not have: its namespace was listed and the relation was not "
                "in it. Build the model into the target you are querying, or "
                f"{self._recompile_hint} if the project was compiled against a "
                "different target.",
                unprofiled,
            )
        return None, unprofiled


def expression_columns(expression: str, dialect: str) -> list[str] | None:
    """The columns a computed element's SQL expression reads, or None.

    Read with the connector's own dialect, because that is the dialect the layer
    renders the expression into. None where the expression does not parse, or
    reads a subquery, whose columns belong to a relation the gate was not told
    about: either way the gate cannot say what the element projects, and refuses
    it rather than screening its name.

    A brace is refused before parsing. It is templating (Jinja, a ``{column}``
    placeholder) filled after the gate has run, and DuckDB reads ``{{ x }}`` as a
    struct literal that names no column, which would clear it as a constant.
    """

    if "{" in expression or "}" in expression:
        return None
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(expression, read=dialect)
    except Exception:
        return None
    if tree is None or tree.find(exp.Select, exp.Subquery) is not None:
        return None
    return list(dict.fromkeys(column.name for column in tree.find_all(exp.Column)))


def unprofiled_note(relations: list[str]) -> list[str]:
    """The disclosure that a queried relation carries no profile.

    The PII request-gate adjudicates a dimension from its physical column's cached
    flag and falls back to the name heuristic when the cache cannot speak. That
    fallback is the fail-closed floor, not an equivalent, so a result whose
    relations were never profiled says which ones and how to fix it rather than
    letting the weaker screening pass unremarked.
    """

    if not relations:
        return []
    ordered = sorted(set(relations))
    return [
        "PII screening fell back to the name heuristic: the .dex/ cache holds no "
        f"profile for {', '.join(ordered)}. Run `explore profile "
        f"{' '.join(ordered)}` for value-evidence screening."
    ]
