"""What each generated test a plan renders costs per production run (#1462).

A generated test (#1449) runs in the project's production runs, which dex does
not run and whose cost the project pays, on every run (``dex_every_run``) or on
the project's own schedule (``dex_scheduled``). A test whose cost surprises the
person paying for it gets turned off, so the plan that renders it says what it
will cost per run before anyone applies it.

Dex's cost guard prices dex's own statements on the dev target, which does not
give production's cost: dev tables are often smaller, and the tests run where
dex never connects. So the estimate is built from production's own sizes:

- **Which tables.** Each shipped macro reads exactly the relations its
  arguments name (the model it sits on, a parent, a snapshot, a join's two
  inputs), so a test reads what :func:`~.declarations._generated` lists for it.
  Each is resolved to its production relation: the model's alias in the
  connector's ``prod`` namespace (``<connector>.prod``), its custom schema named
  as dbt names it there (``<prod>_<custom>`` by dbt's default, ``<custom>_<prod
  target>`` under the layered override ``transform init`` scaffolds), a source
  as it is declared. A naming dex cannot reproduce for production (any other
  ``generate_schema_name``) is unknown, never guessed.
- **Which sizes.** The catalog's row count and bytes for each relation, read
  from metadata alone: never a row, never a statement over the data. ``prod`` is
  committed in ``.dex/config.yml``, which is what authorizes reading its
  catalog where the source allowlist does not reach it; a source is read only
  where the allowlist does. A connector whose catalog read bills (Redshift
  Serverless wakes its workgroup) is not read unasked, and a catalog that keeps
  no size (a view anywhere, Databricks before a billed ``DESCRIBE DETAIL``)
  gives an unknown size.
- **Which estimate.** The connector's own size-based price for one statement
  over those tables (``size_estimate``), in its unit, with nothing dex's own
  command would add. An estimate with its basis: which relations, which sizes,
  read when. A test with an unknown input is unknown, with the reason, and the
  per-cadence total says it is partial.

Commentary on a stored plan, like row attribution: nothing here refuses a plan,
and a defect is a warning naming the exception type.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import generated_tests as gt

#: The statuses of a row whose test is in the YAML once the plan lands, so runs
#: in production: rendered now, already there, or there while the switch is off.
RUNNING = ("added", "already_listed", "listed_while_off")

# Why a test's estimate is unknown. Stable codes a caller can branch on.
PROD_UNSET = "prod_unset"
NO_CONNECTOR = "no_connector"
CONNECTION_UNAVAILABLE = "connection_unavailable"
METADATA_READ_BILLED = "metadata_read_billed"
NO_SIZE_ESTIMATOR = "no_size_estimator"
RELATION_UNNAMED = "relation_unnamed"
RELATION_UNRESOLVED = "relation_unresolved"
CUSTOM_SCHEMA_NAMING = "custom_schema_naming"
PROD_TARGET_UNSET = "prod_target_unset"
OUTSIDE_ALLOWLIST = "outside_allowlist"
RELATION_NOT_FOUND = "relation_not_found"
NO_SIZE_IN_METADATA = "no_size_in_metadata"
METADATA_UNREADABLE = "metadata_unreadable"
LINEAGE_TOO_DEEP = "lineage_too_deep"
STALE_TEST = "stale_test"
NOT_IN_MANIFEST = "not_in_manifest"
UNTRACKED_INPUT = "untracked_input"
LINEAGE_UNVERIFIED = "lineage_unverified"
ESTIMATE_FAILED = "estimate_failed"
PLACEMENT_CHANGED = "placement_changed"

#: How many views (or ephemeral models) deep a test's inputs are followed to
#: the tables they read, so a long chain of views is priced on its tables and a
#: cycle a broken manifest could hold still ends.
_MAX_DEPTH = 12

_UNITS = {
    "bytes_scanned": "bytes",
    "compute_time": "seconds",
    "db_load": "seconds",
    "free_local": None,
}

_BASIS_NOTES = {
    "bytes_scanned": (
        "an upper bound: the summed bytes of the tables each test reads, each "
        "raised to BigQuery's 10 MB per-table minimum, while BigQuery bills only "
        "the columns a statement reads"
    ),
    "compute_time": (
        "a heuristic: the summed bytes of the tables each test reads over the "
        "scan rate of the warehouse dex is pinned to, which production's may not "
        "be"
    ),
    "db_load": (
        "a heuristic: the summed bytes of the tables each test reads over a "
        "reference scan rate"
    ),
    "free_local": "nothing bills on this connector; the sizes are what each test reads",
}

_REF = re.compile(
    r"""^\s*ref\(\s*(['"])(?P<a>[^'"]+)\1\s*(?:,\s*(['"])(?P<b>[^'"]+)\3\s*)?"""
    r"""(?:,\s*v(?:ersion)?\s*=\s*['"]?(?P<v>[^'"\s)]+)['"]?\s*)?\)\s*$"""
)
_SOURCE = re.compile(
    r"""^\s*source\(\s*(['"])(?P<s>[^'"]+)\1\s*,\s*(['"])(?P<t>[^'"]+)\3\s*\)\s*$"""
)


@dataclass(frozen=True)
class Relation:
    """One relation a test reads, resolved for production: its identifier as the
    adapter spells it, or why it has none. ``kind`` is ``model`` (a project
    node built in ``prod``) or ``source`` (read where it is declared)."""

    label: str
    identifier: str | None = None
    kind: str = "model"
    reason: str | None = None
    detail: str | None = None
    #: The manifest node it was resolved from, so a view or an ephemeral model
    #: can be followed to the relations it reads.
    node_id: str | None = None
    #: An ephemeral model builds no relation: it is read as what it reads.
    ephemeral: bool = False

    @property
    def key(self) -> str:
        return self.identifier or (
            f"ephemeral:{self.node_id}" if self.node_id else self.label
        )


def price(
    engine: Any,
    report: dict[str, Any] | None,
    *,
    view: Any = None,
    adapter_factory: Any = None,
    edits: Sequence[Any] = (),
) -> list[str]:
    """Add ``cost`` to ``report`` (``data.generated_tests``) and to each row
    whose test runs in production, and return the warnings to relay.

    ``view`` is the project as dbt last parsed it (its manifest names each
    node's alias and custom schema); ``adapter_factory`` opens the connection,
    by default the engine's own. Both are seams for tests. ``edits`` are the
    plan's, so a model whose alias, schema or database the plan changes is not
    priced from the placement the manifest still names."""

    if not report:
        return []
    tests = report.get("tests") or []
    rows = [row for row in tests if row.get("status") in RUNNING]
    # A stale test stays in the YAML and runs as written, so it counts in its
    # cadence's total; dex does not price a test the declaration no longer gives.
    stale = [row for row in tests if row.get("status") == "stale"]
    for row in stale:
        row["cost"] = {
            "status": "unknown",
            "reason": STALE_TEST,
            "detail": (
                "the declaration no longer gives this listed test, which still runs "
                "as written; dex does not price it"
            ),
        }
    if not rows and not stale:
        return []

    config = engine.config
    connector = engine.connector or config.connector
    target = getattr(config, connector, None) if connector else None
    from ..connect import prod_target_scope

    prod = prod_target_scope(connector, target) if connector else None
    cost: dict[str, Any] = {"prod": prod, "read_at": None}
    report["cost"] = cost
    if not rows:
        cost["per_run"] = _per_run(stale)
        cost["relations"] = []
        return []
    counted = rows + stale

    if not connector:
        return _all_unknown(
            rows, cost, NO_CONNECTOR, "no connector is configured", stale
        )
    if prod is None:
        _all_unknown(
            rows,
            cost,
            PROD_UNSET,
            f"{connector}.prod is not set, so dex does not know where the "
            "project's models build in production",
            stale,
        )
        return [
            f"{len(rows)} generated test(s) run in production and are not priced: "
            f"set {connector}.prod in .dex/config.yml to where the project's models "
            "build in production (dex reads only that namespace's catalog "
            "metadata, never its rows)"
        ]

    if connector == "redshift" and _redshift_serverless(engine, target):
        # Decided before any connection: opening one can resume a sleeping
        # Serverless workgroup, which bills a wake.
        return _billed(rows, cost, stale)
    try:
        adapter = (adapter_factory or (lambda: engine._adapter("transform plan")))()
    except Exception as exc:
        return _all_unknown(
            rows,
            cost,
            CONNECTION_UNAVAILABLE,
            f"no connection could be opened ({type(exc).__name__})",
            stale,
        )
    resolver = _Resolver(
        engine, connector, target, prod, view, container=_container(adapter)
    )
    resolver.changed, resolver.all_changed = placement_changes(
        getattr(resolver.view, "root", None), edits
    )
    reads = {id(row): resolver.reads(row) for row in rows}
    paradigm = getattr(getattr(adapter, "paradigm", None), "value", None)
    cost["paradigm"] = paradigm
    cost["unit"] = _UNITS.get(paradigm or "")
    cost["note"] = _BASIS_NOTES.get(paradigm or "")

    # Views are read through the lineage of the manifest dbt last wrote, which
    # may be another target's: a view branching on target.name can read other
    # relations in production (named in the docs, said here).
    cost["lineage_parsed_for"] = resolver.manifest_target
    billed = getattr(adapter, "catalog_read_billed", None)
    if callable(billed) and billed():
        return _billed(rows, cost, stale)

    sizes, expansions = _read_lineage(
        adapter, connector, prod, resolver, [r for rs in reads.values() for r in rs]
    )
    cost["read_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()

    estimator = getattr(adapter, "size_estimate", None)
    relations: dict[str, dict[str, Any]] = {}
    for row in rows:
        row["cost"] = _row_cost(reads[id(row)], sizes, expansions, estimator, relations)
    cost["relations"] = sorted(relations.values(), key=lambda r: r["relation"])
    cost["per_run"] = _per_run(counted)
    unknown = sum(1 for row in counted if row["cost"]["status"] == "unknown")
    if unknown:
        return [
            f"{unknown} of {len(counted)} generated test(s) have no production cost "
            "estimate (data.generated_tests.tests[].cost.reason says why); the "
            "per-run totals under data.generated_tests.cost.per_run are partial"
        ]
    return []


def _all_unknown(
    rows: list[dict[str, Any]],
    cost: dict[str, Any],
    reason: str,
    detail: str,
    stale: list[dict[str, Any]] | None = None,
) -> list[str]:
    for row in rows:
        row["cost"] = {"status": "unknown", "reason": reason, "detail": detail}
    cost["per_run"] = _per_run(rows + list(stale or []))
    cost["relations"] = []
    return []


def _redshift_serverless(engine: Any, target: Any) -> bool:
    """Whether the Redshift connection the engine would open is Serverless,
    told without opening it: a pinned ``workgroup``, or an endpoint host from
    the environment, the committed target or a dbt profile that is a
    Serverless one (the same discovery the connection itself runs, which reads
    no warehouse for those). A connection a host supplies carries no compute
    facts, so it may be Serverless and is not opened unasked either."""

    if getattr(engine, "connection", None) is not None:
        return True
    if getattr(target, "workgroup", None):
        return True
    import os

    from ..config import RedshiftTarget
    from ..connect import resolve_redshift_connection

    try:
        _params, _method, compute = resolve_redshift_connection(
            target or RedshiftTarget(),
            os.environ,
            getattr(engine, "repo_root", None) or ".",
        )
    except Exception:
        return False  # nothing resolves, so nothing opens either
    return bool(compute) and compute.get("kind") == "serverless"


def _billed(
    rows: list[dict[str, Any]], cost: dict[str, Any], stale: list[dict[str, Any]]
) -> list[str]:
    return _all_unknown(
        rows,
        cost,
        METADATA_READ_BILLED,
        "reading this connector's catalog can bill (a Redshift Serverless "
        "workgroup wakes for it, and a connection a host supplies may be one), "
        "and a plan does not spend unasked",
        stale,
    )


def _read_lineage(
    adapter: Any,
    connector: str,
    prod: str,
    resolver: _Resolver,
    relations: list[Relation],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[Relation]]]:
    """Every relation's catalog size, and each view's or ephemeral model's
    parents: a test over a view scans the tables the view reads, so a view the
    manifest's lineage can follow is read as those, down to tables, and only a
    view it cannot follow is a size the catalog does not keep."""

    sizes: dict[str, dict[str, Any]] = {}
    expansions: dict[str, list[Relation]] = {}
    frontier = list({r.key: r for r in relations}.values())
    for _ in range(_MAX_DEPTH):
        if not frontier:
            break
        readable = [
            r
            for r in frontier
            if r.identifier and not r.ephemeral and r.identifier not in sizes
        ]
        sizes.update(_read_sizes(adapter, connector, prod, readable))
        following: dict[str, Relation] = {}
        for relation in frontier:
            if relation.key in expansions or relation.node_id is None:
                continue
            is_view = (
                relation.identifier is not None
                and sizes.get(relation.identifier, {}).get("object_type") == "view"
            )
            if not (relation.ephemeral or is_view):
                continue
            parents = resolver.parents(relation.node_id)
            if not parents:
                continue
            tracked = resolver.tracked(relation.node_id)
            if tracked is not True:
                # The manifest's lineage is only what ref() and source() name: a
                # view reading a relation by its literal name, or one dex cannot
                # check, is not priced as though its inputs were all known.
                sizes[relation.key] = {
                    "relation": relation.identifier or relation.label,
                    "kind": relation.kind,
                    "object_type": "view" if is_view else "ephemeral",
                    "reason": UNTRACKED_INPUT
                    if tracked is False
                    else LINEAGE_UNVERIFIED,
                    "detail": (
                        "its compiled SQL reads a relation its ref() and source() "
                        "lineage does not name, so its inputs are not all known"
                        if tracked is False
                        else "dbt has not compiled it, so dex cannot check that its "
                        "ref() and source() lineage is every relation it reads; a "
                        "dev build compiles it"
                    ),
                }
                continue
            expansions[relation.key] = parents
            following.update({p.key: p for p in parents})
        frontier = [r for r in following.values() if r.key not in expansions]
    return sizes, expansions


def _row_cost(
    reads: list[Relation],
    sizes: Mapping[str, dict[str, Any]],
    expansions: Mapping[str, list[Relation]],
    estimator: Any,
    relations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """One test's estimate: the connector's price for one statement over the
    production sizes of every table it reads (a view read as the tables it
    reads), or the first reason one of them has no size."""

    tables: dict[str, None] = {}
    through: dict[str, None] = {}
    seen: set[str] = set()

    def walk(relation: Relation, depth: int) -> dict[str, Any] | None:
        if relation.key in seen:
            return None
        seen.add(relation.key)
        if relation.key in expansions:
            if relation.identifier:
                # Read through, not priced: its own size is not the question.
                relations[relation.identifier] = {
                    **{
                        k: v
                        for k, v in sizes[relation.identifier].items()
                        if k not in ("reason", "detail")
                    },
                    "reads": [
                        p.identifier or p.label for p in expansions[relation.key]
                    ],
                }
            through[relation.identifier or relation.label] = None
            for parent in expansions[relation.key]:
                if depth >= _MAX_DEPTH:
                    return _unknown(
                        LINEAGE_TOO_DEEP,
                        f"more than {_MAX_DEPTH} views deep before a table",
                        parent.identifier or parent.label,
                    )
                if unknown := walk(parent, depth + 1):
                    return unknown
            return None
        if relation.key in sizes and relation.key != relation.identifier:
            # An ephemeral model whose lineage could not be trusted.
            size = sizes[relation.key]
            return _unknown(size["reason"], size.get("detail"), relation.label)
        if relation.identifier is None:
            return _unknown(relation.reason, relation.detail, relation.label)
        size = sizes.get(relation.identifier)
        if size is None:
            return _unknown(
                LINEAGE_TOO_DEEP,
                f"more than {_MAX_DEPTH} views deep before a table",
                relation.identifier,
            )
        relations[relation.identifier] = size
        if size.get("reason"):
            return _unknown(size["reason"], size.get("detail"), relation.identifier)
        tables[relation.identifier] = None
        return None

    for relation in reads:
        if unknown := walk(relation, 0):
            return unknown
    names = list(tables)
    out: dict[str, Any] = {"relations": names}
    if through:
        out["through"] = list(through)
    if not callable(estimator):
        return {
            "status": "unknown",
            "reason": NO_SIZE_ESTIMATOR,
            "detail": "this connector gives no estimate from table sizes",
            **out,
        }
    byte_sizes = [sizes[name].get("bytes") or 0 for name in names]
    try:
        estimate = estimator(byte_sizes)
    except Exception as exc:
        # The type and never the words, as for every other failure here.
        return {
            "status": "unknown",
            "reason": ESTIMATE_FAILED,
            "detail": f"the connector could not price it ({type(exc).__name__})",
            **out,
        }
    return {"status": "estimated", "estimate": estimate, **out}


def _unknown(reason: str | None, detail: str | None, relation: str) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "unknown", "reason": reason, "relation": relation}
    if detail:
        out["detail"] = detail
    return out


def _per_run(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The per-run total of each cadence's tests, apart, since a project runs
    them at different rates: ``complete`` is false where a test of that cadence
    has no estimate, so the total is a floor."""

    out: dict[str, Any] = {}
    for cadence in gt.CADENCES:
        of = [row for row in rows if row.get("cadence") == cadence]
        if not of:
            continue
        known = [row["cost"] for row in of if row["cost"]["status"] == "estimated"]
        out[cadence] = {
            "tests": len(of),
            "estimated": len(known),
            "unknown": len(of) - len(known),
            "estimate": sum(c["estimate"] for c in known) if known else None,
            "complete": len(known) == len(of),
        }
    return out


# --- reading the sizes ----------------------------------------------------------


def _read_sizes(
    adapter: Any, connector: str, prod: str, relations: Any
) -> dict[str, dict[str, Any]]:
    """Each relation's catalog size, or why it has none. ``prod``'s namespaces
    are read for this one call only, folded in the way a verified build reads
    its own dev namespace, and nothing but catalog metadata is asked for."""

    relations = list(relations)
    out: dict[str, dict[str, Any]] = {}
    namespaces = sorted(
        {
            _namespace(connector, r.identifier)
            for r in relations
            if r.kind == "model" and r.identifier
        }
        - {""}
    )
    read_dev_target = getattr(adapter, "read_dev_target", None)
    previous = getattr(adapter, "_dev_target", None)
    if read_dev_target is not None and namespaces:
        read_dev_target([*(previous or ()), *namespaces])
    try:
        for relation in relations:
            out[relation.identifier] = _size(adapter, relation)
    finally:
        if read_dev_target is not None and namespaces:
            read_dev_target(list(previous) if previous else None)
    return out


def _size(adapter: Any, relation: Relation) -> dict[str, Any]:
    identifier = relation.identifier
    record: dict[str, Any] = {"relation": identifier, "kind": relation.kind}
    try:
        reader = getattr(adapter, "catalog_meta", None)
        meta = (
            reader(identifier)
            if callable(reader)
            else adapter.table_metadata(identifier)[0]
        )
    except Exception as exc:
        name = type(exc).__name__
        # The message decides nothing but the code, and is never relayed: a
        # warehouse's words can name more than the relation asked for.
        missing = (
            isinstance(exc, LookupError)
            or "notfound" in name.lower()
            or "not found" in str(exc).lower()
            or "does not exist" in str(exc).lower()
        )
        record["reason"] = RELATION_NOT_FOUND if missing else METADATA_UNREADABLE
        record["detail"] = (
            "no such relation in production's catalog"
            if missing
            else f"its catalog metadata could not be read ({name})"
        )
        return record
    record["object_type"] = meta.object_type
    record["rows"] = meta.row_count
    record["bytes"] = meta.byte_size
    if meta.byte_size is None and getattr(adapter, "name", None) != "duckdb":
        record["reason"] = NO_SIZE_IN_METADATA
        record["detail"] = "the catalog keeps no byte size for it" + (
            " (a view)" if meta.object_type == "view" else ""
        )
    return record


def _namespace(connector: str, identifier: str | None) -> str:
    """The scope entry ``identifier`` lies in, spelled as the adapter's
    ``read_dev_target`` reads one: a schema on Postgres and Redshift, a
    database on ClickHouse, ``container.schema`` elsewhere."""

    if not identifier:
        return ""
    parts = identifier.split(".")[:-1]
    if connector in {"postgres", "redshift"}:
        return parts[-1] if parts else ""
    return ".".join(parts)


# --- resolving each relation for production ------------------------------------


class _Resolver:
    """Names the production relation of each input a test reads, from the
    project dbt last parsed (its manifest) and the committed ``prod``."""

    def __init__(
        self,
        engine: Any,
        connector: str,
        target: Any,
        prod: str,
        view: Any,
        *,
        container: str | None = None,
    ) -> None:
        self.connector = connector
        # What a one-part ``prod`` is qualified with where the manifest names no
        # database for the node (a model this plan creates): the connection's
        # own project or database, as the adapter keys its relations.
        self.container = container
        self.target = target
        self.prod = prod
        self.prod_target = getattr(engine.config, "dbt_prod_target", None)
        if view is None:
            view = _load_view(engine)
        self.view = view
        manifest = getattr(view, "manifest", None) or {}
        self.nodes = _nodes_by_name(manifest)
        self.packaged = {
            (
                str(node.get("package_name", "")).lower(),
                str(node.get("name", "")).lower(),
                str(node["version"]) if node.get("version") is not None else None,
            ): node
            for node in (manifest.get("nodes") or {}).values()
            if node.get("resource_type") in ("model", "seed", "snapshot")
        }
        self.manifest_target = _parsed_for(manifest, prod)
        self.by_id: dict[str, Any] = {
            **(manifest.get("nodes") or {}),
            **(manifest.get("sources") or {}),
        }
        self.sources = {
            (
                str(node.get("source_name", "")).lower(),
                str(node.get("name", "")).lower(),
            ): node
            for node in (manifest.get("sources") or {}).values()
        }
        self.naming = _schema_naming(view)
        #: Models whose placement the plan changes, and whether it may change
        #: every model's (a ``dbt_project.yml`` edit): set by :func:`price`.
        self.changed: frozenset[str] = frozenset()
        self.all_changed = False

    def reads(self, row: Mapping[str, Any]) -> list[Relation]:
        listed = getattr(row, "reads", None)
        if listed is None:
            listed = [{"model": row.get("model")}]
        out: list[Relation] = []
        for read in listed:
            if "model" in read:
                out.append(self.model(str(read["model"])))
            else:
                out.append(self.relation(read.get("relation")))
        return list({(r.identifier or r.label): r for r in out}.values())

    def relation(self, text: Any) -> Relation:
        if not isinstance(text, str) or not text.strip():
            return Relation(
                "?",
                reason=RELATION_UNNAMED,
                detail="the plan could not name this input as a ref() or source()",
            )
        if match := _SOURCE.match(text):
            return self.source(match["s"], match["t"], text)
        if match := _REF.match(text):
            name = match["b"] or match["a"]
            version = match["v"]
            label = f"{name}.v{version}" if version else name
            if match["b"]:
                # ref('pkg', 'name') resolves in that package only, as dbt
                # resolves it, never to a same-named node elsewhere.
                node = self.packaged.get((match["a"].lower(), name.lower(), version))
                return self._from_node(label, node, self._id_of(node))
            return self.model(label)
        return Relation(
            text,
            reason=RELATION_UNNAMED,
            detail="the input is not a plain ref() or source() dex can resolve",
        )

    def _id_of(self, node: Any) -> str | None:
        if node is None:
            return None
        return next((k for k, v in self.by_id.items() if v is node), None)

    def tracked(self, node_id: str) -> bool | None:
        """Whether the relations a view's (or an ephemeral model's) compiled
        SQL reads are all ones its ``depends_on`` names: ``None`` where dbt has
        not compiled it, so nothing can be checked."""

        node = self.by_id.get(node_id) or {}
        compiled = node.get("compiled_code")
        if not compiled:
            return None
        named: set[str] = set()
        for parent_id in (node.get("depends_on") or {}).get("nodes") or []:
            parent = self.by_id.get(parent_id) or {}
            relation = parent.get("relation_name")
            if relation:
                named.add(_bare(relation))
        try:
            import sqlglot
            from sqlglot import expressions as exp

            from ..adapters import get_dialect

            parsed = sqlglot.parse_one(compiled, read=get_dialect(self.connector))
        except Exception:
            return None
        if parsed is None:
            return None
        ctes = {cte.alias_or_name.lower() for cte in parsed.find_all(exp.CTE)}
        for table in parsed.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            if not table.db and name.lower() in ctes:
                continue
            bare = _bare(".".join(p for p in (table.catalog, table.db, name) if p))
            if bare in named or any(n.endswith("." + bare) for n in named):
                continue
            return False
        return True

    def parents(self, node_id: str) -> list[Relation]:
        """The relations a view or an ephemeral model reads, from its
        manifest node's ``depends_on``: models, seeds and snapshots resolved
        for production, sources as declared."""

        node = self.by_id.get(node_id) or {}
        out: list[Relation] = []
        for parent_id in (node.get("depends_on") or {}).get("nodes") or []:
            parent = self.by_id.get(parent_id)
            if parent is None:
                continue
            if parent.get("resource_type") == "source":
                out.append(self._from_source(parent_id, parent, parent_id))
            elif parent.get("resource_type") in ("model", "seed", "snapshot"):
                out.append(self._from_node(str(parent.get("name")), parent, parent_id))
        return out

    def source(self, source: str, table: str, label: str) -> Relation:
        node = self.sources.get((source.lower(), table.lower()))
        if node is None:
            return Relation(
                label,
                kind="source",
                reason=RELATION_UNRESOLVED,
                detail="the compiled manifest does not hold this source",
            )
        node_id = next((k for k, v in self.by_id.items() if v is node), None)
        return self._from_source(label, node, node_id)

    def _from_source(self, label: str, node: Any, node_id: str | None) -> Relation:
        identifier = _identifier(
            self.connector,
            node.get("database"),
            node.get("schema"),
            node.get("identifier") or node.get("name"),
        )
        if not self._in_allowlist(identifier):
            return Relation(
                label,
                kind="source",
                reason=OUTSIDE_ALLOWLIST,
                detail=(
                    "the source lies outside the source allowlist, and prod "
                    "authorizes reading only where the project's models build"
                ),
            )
        return Relation(label, identifier=identifier, kind="source", node_id=node_id)

    def model(self, label: str) -> Relation:
        name, _, version = label.partition(".v")
        node = self.nodes.get((name.lower(), version or None)) or (
            self.nodes.get((name.lower(), None)) if not version else None
        )
        node_id = (
            next((k for k, v in self.by_id.items() if v is node), None)
            if node is not None
            else None
        )
        return self._from_node(label, node, node_id)

    def _from_node(self, label: str, node: Any, node_id: str | None) -> Relation:
        if node is None:
            # Never named from the bare file name: a model's alias, schema and
            # database are its config's, which only the manifest holds.
            return Relation(
                label,
                reason=NOT_IN_MANIFEST,
                detail=(
                    "the compiled manifest does not hold this model (one this "
                    "plan creates, or a project dbt has not parsed since), so its "
                    "production relation is not known; a model not yet built is "
                    "not in production either"
                ),
            )
        if self.all_changed or str(node.get("name", "")).lower() in self.changed:
            # The manifest names the placement before the plan: what the plan
            # builds is only known once dbt parses it.
            return Relation(
                label,
                reason=PLACEMENT_CHANGED,
                detail=(
                    "this plan changes where the model builds (its alias, schema "
                    "or database, or dbt_project.yml), and the compiled manifest "
                    "names where it built before"
                ),
            )
        relation = self._resolve(label, node)
        ephemeral = ((node.get("config") or {}).get("materialized")) == "ephemeral"
        return replace(
            relation,
            node_id=node_id,
            ephemeral=ephemeral,
            identifier=None if ephemeral else relation.identifier,
        )

    def _resolve(self, label: str, node: Any) -> Relation:
        name, _, version = label.partition(".v")
        config = (node or {}).get("config") or {}
        alias = (node or {}).get("alias") or (f"{name}_v{version}" if version else name)
        custom = config.get("schema")
        resource = str((node or {}).get("resource_type") or "model")
        if resource == "snapshot" and config.get("target_schema"):
            # A literal schema every target writes, production included. It is
            # not prod's, so committing prod does not authorize reading it: only
            # the source allowlist does.
            schema = str(config["target_schema"])
            database = config.get("target_database") or (node or {}).get("database")
            return self._literal(
                label, _identifier(self.connector, database, schema, alias)
            )
        prod_parts = self.prod.split(".")
        prod_schema = prod_parts[-1]
        # Production's container is prod's own, else the connection's project
        # or database: never the node's, which a model's own `database` config
        # sets, so an override cannot pass for prod (#1462).
        container = (
            prod_parts[0]
            if len(prod_parts) == 2
            else self.container
            or (None if config.get("database") else (node or {}).get("database"))
        )
        literal_database = (
            bool(config.get("database"))
            and str(config["database"]).lower() != str(container or "").lower()
        )
        if config.get("database"):
            container = config["database"]
        if custom:
            custom = str(custom).strip()
            if self.naming == "default":
                schema = f"{prod_schema}_{custom}"
            elif self.naming == "layered":
                if not self.prod_target:
                    return Relation(
                        label,
                        reason=PROD_TARGET_UNSET,
                        detail=(
                            "the project's generate_schema_name names a custom "
                            "schema <schema>_<target name>; set dbt_prod_target "
                            "in .dex/config.yml to the production target's name"
                        ),
                    )
                schema = f"{custom}_{self.prod_target}"
            else:
                return Relation(
                    label,
                    reason=CUSTOM_SCHEMA_NAMING,
                    detail=(
                        "the project's own generate_schema_name decides the "
                        "custom schema in production, and dex does not run it"
                    ),
                )
        elif self.naming == "custom":
            return Relation(
                label,
                reason=CUSTOM_SCHEMA_NAMING,
                detail=(
                    "the project's own generate_schema_name decides each schema "
                    "in production, and dex does not run it"
                ),
            )
        else:
            schema = prod_schema
        identifier = _identifier(self.connector, container, schema, alias)
        if literal_database:
            # A database the model's config names, not prod's.
            return self._literal(label, identifier)
        return Relation(label, identifier=identifier)

    def _literal(self, label: str, identifier: str) -> Relation:
        """A relation outside prod's namespaces, read only where the source
        allowlist reaches it, as a source is."""

        if not self._in_allowlist(identifier):
            return Relation(
                label,
                kind="literal",
                reason=OUTSIDE_ALLOWLIST,
                detail=(
                    "it builds outside prod's namespaces and the source allowlist, "
                    "and prod authorizes reading only where the project's models "
                    "build there"
                ),
            )
        return Relation(label, identifier=identifier, kind="literal")

    def _in_allowlist(self, identifier: str) -> bool:
        from ..connect import source_scope

        committed = source_scope(self.connector, self.target)
        if not committed:
            return True
        from ..adapters.base import scope_within

        namespace = _namespace(self.connector, identifier)
        if self.connector == "bigquery":
            project = getattr(self.target, "project", None)
            committed = [
                entry if "." in entry or not project else f"{project}.{entry}"
                for entry in committed
            ]
        return scope_within(namespace, committed)


def _bare(relation: str) -> str:
    from ..dbt_project import strip_relation_quoting

    return strip_relation_quoting(relation).lower()


def _parsed_for(manifest: Mapping[str, Any], prod: str) -> str | None:
    """Which target the manifest's lineage was read for, as far as its
    relations say (dbt records no target name): ``production`` where a model
    sits in ``prod``, ``another target`` where models sit elsewhere, ``None``
    with no model to tell by. A view whose SQL branches on ``target.name`` can
    read other relations in production than a manifest for another target names."""

    wanted = prod.lower().split(".")[-1]
    seen = False
    for node in (manifest.get("nodes") or {}).values():
        if node.get("resource_type") != "model" or not node.get("relation_name"):
            continue
        seen = True
        parts = _bare(node["relation_name"]).split(".")
        if len(parts) >= 2 and parts[-2] == wanted:
            return "production"
    return "another target" if seen else None


_PLACEMENT = re.compile(r"""\b(alias|schema|database)\s*=\s*(['"])([^'"]*)\2""")
_PLACEMENT_KEYS = ("alias", "schema", "database")


def placement_changes(
    root: str | Path | None, edits: Sequence[Any]
) -> tuple[frozenset[str], bool]:
    """The models whose alias, schema or database ``edits`` change, read from
    each model's SQL ``config()`` and YAML ``config`` before and after, and
    whether a ``dbt_project.yml`` edit may change any model's (#1462)."""

    import yaml

    changed: set[str] = set()
    every = False
    for edit in edits:
        kind = str(getattr(getattr(edit, "kind", None), "value", ""))
        path = str(getattr(edit, "path", ""))
        before = None
        if root is not None:
            try:
                before = (Path(root) / path).read_text(encoding="utf-8")
            except OSError:
                before = None
        after = getattr(edit, "new_content", None)
        if kind == "project_yml":
            if _models_config(before) != _models_config(after):
                every = True
        elif kind == "model_sql" and path.endswith(".sql"):
            if before is not None and _sql_placement(before) != _sql_placement(after):
                changed.add(Path(path).stem.lower())
        elif kind == "schema_yml":
            old, new = _yaml_placements(before, yaml), _yaml_placements(after, yaml)
            changed.update(
                name for name in set(old) | set(new) if old.get(name) != new.get(name)
            )
    return frozenset(changed), every


def _sql_placement(text: str | None) -> dict[str, str]:
    return {m[1]: m[3] for m in _PLACEMENT.finditer(text or "")}


def _models_config(text: str | None) -> Any:
    import yaml

    try:
        loaded = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return object()  # unreadable: never equal, so read as changed
    return loaded.get("models") if isinstance(loaded, Mapping) else None


def _yaml_placements(text: str | None, yaml: Any) -> dict[str, dict[str, Any]]:
    try:
        loaded = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in (loaded.get("models") if isinstance(loaded, Mapping) else None) or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            continue
        config = entry.get("config") if isinstance(entry.get("config"), Mapping) else {}
        placed = {key: config[key] for key in _PLACEMENT_KEYS if key in config}
        if placed:
            out[entry["name"].lower()] = placed
    return out


def _container(adapter: Any) -> str | None:
    """The project (BigQuery) or database (Postgres, Redshift) the connection
    reads, where the adapter keys a relation with one; ``None`` elsewhere."""

    for attribute in ("project", "_database"):
        value = getattr(adapter, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def _identifier(connector: str, container: Any, schema: Any, name: Any) -> str:
    """A relation spelled as the connector's adapter keys it: ``database.table``
    on ClickHouse, three parts elsewhere (two where no container is known)."""

    parts = [str(p) for p in (container, schema, name) if p not in (None, "")]
    if connector == "clickhouse":
        parts = parts[-2:]
    text = ".".join(parts)
    if connector == "snowflake":
        return text.upper()
    if connector == "databricks":
        return text.lower()
    return text


def _nodes_by_name(manifest: Mapping[str, Any]) -> dict[tuple[str, str | None], Any]:
    """The manifest's models, seeds and snapshots by ``(name, version)``, the
    project's own first, so a package node of the same name never shadows one."""

    out: dict[tuple[str, str | None], Any] = {}
    project = (manifest.get("metadata") or {}).get("project_name")
    nodes = list((manifest.get("nodes") or {}).values())
    nodes.sort(key=lambda node: node.get("package_name") == project)
    for node in nodes:
        if node.get("resource_type") not in ("model", "seed", "snapshot"):
            continue
        version = node.get("version")
        key = (str(node.get("name", "")).lower(), str(version) if version else None)
        out[key] = node
        if version is not None and node.get("latest_version") == version:
            out[(key[0], None)] = node
    return out


def _schema_naming(view: Any) -> str:
    """How the project names a custom schema: ``default`` (dbt's own),
    ``layered`` (the override ``transform init --layered-schemas`` scaffolds,
    byte for byte), or ``custom`` (any other override, which dex does not run)."""

    if view is None:
        return "default"
    from importlib import resources

    asset = (
        resources.files("exmergo_dex_core.transform")
        / "assets"
        / "macros"
        / "generate_schema_name.sql"
    )
    shipped = _normalized(asset.read_text(encoding="utf-8"))
    root = Path(getattr(view, "root", "."))
    found = "default"
    for folder in getattr(view, "macro_paths", None) or ["macros"]:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.sql")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not re.search(r"macro\s+generate_schema_name\s*\(", text):
                continue
            if _normalized(text) == shipped:
                found = "layered"
            else:
                return "custom"
    return found


def _normalized(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _load_view(engine: Any) -> Any:
    try:
        from ..dbt_project import load as load_project

        return load_project(engine.project_dir())
    except Exception:
        return None
