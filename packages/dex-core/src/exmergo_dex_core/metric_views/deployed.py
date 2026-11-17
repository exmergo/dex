"""The Metric Views deployed in Unity Catalog, read back and compared (#1363).

A governed query reads the deployed view and answers with the reviewed
repository definition or not at all, so before anything is priced dex:

1. **locates** the view: the Metric Views the source allowlist's scopes hold,
   listed with the Unity Catalog REST API (``tables.list``, ``table_type =
   METRIC_VIEW``), matched by the file's stem ignoring case. A view no scope
   holds is refused as defined only in the repository (dex never deploys one);
   one name in two allowlisted schemas is refused as ambiguous, and ``--scope``
   narrows it (:mod:`...semantic_deployment`, shared with Snowflake);
2. **reads it back** with ``tables.get``, whose ``view_definition`` holds the
   YAML body. Both calls are REST metadata: no SQL warehouse is woken, so the
   refusal below comes before anything is priced or run. The text is validated
   by the reader a file goes through (:func:`.loader.validate_view_contents`),
   so a view re-saved in a spec version other than ``1.1`` is refused, named;
3. **compares** what the query relies on (:func:`relied_on`): the view's
   ``source`` and ``filter``, each named field and measure and every field or
   measure it reads, followed transitively, a field's ``format`` where a grain
   reads it, and every join those expressions reach, with its ancestors, its
   ``source``, ``on`` / ``using``, cardinality and ``rely.at_most_one_match``.
   The first difference refuses, naming the member and the field.

What Unity Catalog does to a definition on the way back is not a difference.
The YAML is compared as parsed values, never as text, so a reserialized body
reads the same; names are matched ignoring case, as Databricks matches them;
and expressions are compared as parsed Databricks SQL with identifiers folded
(:func:`.lineage.canonical`), so whitespace and case are not differences either.
A body handed back wrapped in its ``CREATE VIEW ... AS $$ ... $$`` statement is
read between the delimiters.

An identity that can browse a view and is not shown its ``view_definition``
reads nothing back: that view's coverage is ``refused``, naming why, never a
catalog presented as complete.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..semantic_deployment import (
    FULL,
    LIMITED,
    REFUSED,
    Coverage,
    DeployedRead,
    LocateRefused,
    ObjectKind,
    judge,
    read_text,
)
from ..semantic_deployment import locate as _locate
from .model import FIELD, Join, Member, MetricView

__all__ = [
    "FULL",
    "LIMITED",
    "REFUSED",
    "Coverage",
    "DeployedRead",
    "Difference",
    "LocateRefused",
    "Reliance",
    "compare",
    "locate",
    "read_deployed",
    "relied_on",
    "unwrap",
]

#: A query is priced from its base tables, which must stay inside the scope, so
#: a `--scope` picking one copy of a view repeats for the schema holding them.
SCOPE_BASE_TABLES = (
    ", repeated for the schema holding the view's source and join tables where "
    "it is another (the scope is also the cost boundary a query is priced within)"
)


def kind(name: str) -> ObjectKind:
    """How a Metric View is named in a locate refusal."""

    return ObjectKind(
        noun="metric view",
        scope="<catalog>.<schema>",
        deploy=(
            f"deploy the reviewed file with `CREATE VIEW <catalog>.<schema>.{name} "
            "WITH METRICS LANGUAGE YAML AS $$ ... $$` into a schema the allowlist "
            "names, then query again"
        ),
        narrow=SCOPE_BASE_TABLES,
    )


def locate(listed: Iterable[str], name: str) -> str:
    """The one allowlisted ``catalog.schema.name`` deployed as ``name``.

    ``listed`` is every Metric View the allowlist's scopes hold. Matched ignoring
    case, since Unity Catalog stores a name lower-cased and a file's stem may be
    spelled either way.
    """

    return _locate(listed, name, kind(name))


# --- reading back ------------------------------------------------------------------


@dataclass(frozen=True)
class Difference:
    """One field on which the deployed view differs from the repository."""

    element: str
    field: str
    repository: Any
    deployed: Any

    def render(self) -> str:
        """One clause for a refusal."""
        if self.field == "missing":
            return f"{self.element} is not in the deployed view"
        if self.field == "unreadable":
            return (
                f"{self.element} is in a part of the deployed view dex's reader "
                "refused, so it could not be compared"
            )
        return (
            f"{self.element} has a different {self.field} there "
            f"(repository {self.repository!r}, deployed {self.deployed!r})"
        )


def unwrap(text: str) -> str:
    """The YAML body of a definition: the text between its ``$$`` delimiters
    where Unity Catalog hands back the whole statement, else the text."""

    first = text.find("$$")
    last = text.rfind("$$")
    if first == -1 or last == first:
        return text
    return text[first + 2 : last]


def read_deployed(
    name: str, deployed_as: str, reader: Callable[[str], str]
) -> DeployedRead:
    """Read ``deployed_as`` back and validate it as the reader reads a file.

    ``name`` is the repository's name for the view (its file's stem), which the
    read-back is given, so its tokens line up with the repository's. ``reader``
    returns ``view_definition``, or raises; a refused read is coverage
    ``refused`` with the reason, never a crash.
    """

    from .loader import validate_view_contents

    text, refused = read_text(name, deployed_as, reader)
    if refused is not None:
        return DeployedRead(name, deployed_as, None, None, refused)
    loaded = validate_view_contents({f"{name}.yaml": unwrap(text or "")})
    view = loaded.views[0] if loaded.views else None
    if view is not None:
        view = replace(view, file=deployed_as)
    diagnostics = [
        f"{deployed_as}: {d.element}: [{d.rule}] {d.message}" for d in loaded.errors
    ]
    coverage = judge(name, deployed_as, view, diagnostics)
    # The elements the reader refused, by name: a member missing from a limited
    # read-back is unreadable there, not absent.
    unresolved = sorted({d.element for d in loaded.errors})
    return DeployedRead(
        name, deployed_as, text, view, coverage, diagnostics, unresolved
    )


# --- what a query relies on -----------------------------------------------------------


@dataclass
class Reliance:
    """What a query reads of a view: every member, the names it reads them by
    first, the join paths those reach (ancestors included), and the fields a
    grain buckets, whose ``format`` is what makes the grain legal."""

    members: list[Member]
    joins: list[tuple[str, ...]]
    grained: list[Member]


def relied_on(
    view: MetricView, named: Sequence[Member], grained: Sequence[Member] = ()
) -> Reliance | None:
    """Everything a query naming ``named`` relies on in ``view``, followed
    transitively, or None where an expression cannot be read (which the caller
    refuses rather than compare by half).

    A bare name that is a field of the view is that field, a ``MEASURE()`` call
    the measure it names, and a qualified column the join its qualifier names.
    The view's ``filter`` applies to every query, so what it reads is relied on
    too.
    """

    from . import lineage

    members: dict[str, Member] = {}
    paths: set[tuple[str, ...]] = set()
    pending: list[str] = []

    def add(member: Member) -> None:
        if member.name.lower() not in members:
            members[member.name.lower()] = member
            pending.append(member.expr)

    for member in named:
        add(member)
    if view.filter:
        pending.append(view.filter)
    while pending:
        expr = pending.pop(0)
        refs = lineage.column_refs(expr)
        measures = lineage.measure_refs(expr)
        if refs is None or measures is None:
            return None
        for name in measures:
            target = view.measure(name)
            if target is None:
                return None
            add(target)
        for ref in refs:
            if not ref.qualifier:
                field_ = view.member(ref.column)
                if field_ is not None and field_.kind == FIELD:
                    add(field_)
                continue
            if tuple(q.lower() for q in ref.qualifier) == ("source",):
                continue
            join = view.join(ref.qualifier)
            if join is None:
                return None
            paths.update(join.path[:depth] for depth in range(1, len(join.path) + 1))
    return Reliance(
        members=list(members.values()),
        joins=sorted(paths),
        grained=list({m.name.lower(): m for m in grained}.values()),
    )


# --- comparing -----------------------------------------------------------------------


def canonical(sql: str | None) -> str | None:
    """An expression in the form two spellings of it share; see
    :func:`.lineage.canonical`."""

    from .lineage import canonical as _canonical

    return _canonical(sql)


def _value(value: Any) -> Any:
    """A YAML value in a form two serializations of it share: mappings sorted
    by key, keys and text lower-cased (a window's ``order`` names a field, which
    Databricks matches ignoring case)."""

    if isinstance(value, dict):
        return tuple(sorted((str(k).lower(), _value(v)) for k, v in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_value(v) for v in value)
    if isinstance(value, str):
        return " ".join(value.split()).lower()
    return value


def _member_fields(member: Member) -> dict[str, Any]:
    return {
        "kind": member.kind,
        "expr": canonical(member.expr),
        "window": _value(list(member.window)) or None,
        "partition": _value(member.partition) if member.partition else None,
    }


def _join_fields(join: Join) -> dict[str, Any]:
    return {
        "source": canonical(join.source),
        "on": canonical(join.on),
        "using": tuple(sorted(c.lower() for c in join.using))
        if join.using is not None
        else None,
        "cardinality": join.cardinality,
        "rely.at_most_one_match": join.at_most_one_match,
    }


def _view_fields(view: MetricView) -> dict[str, Any]:
    return {
        "source": canonical(view.source),
        "filter": canonical(view.filter),
        # A view declaring either is one a governed query refuses, so a
        # deployment that gained one answers differently from the file.
        "materialization": "declared" if view.materialization else None,
        "parameters": tuple(
            sorted(str(p.get("name", "")).lower() for p in view.parameters)
        )
        or None,
    }


def _format_type(member: Member) -> str | None:
    found = (member.format or {}).get("type")
    return str(found).lower() if found is not None else None


def _refused_there(name: str, unresolved: Iterable[str]) -> bool:
    """Whether the reader refused an element named ``name`` in the read-back."""
    suffix = f"['{name.lower()}']"
    return any(element.lower().endswith(suffix) for element in unresolved)


def _differ(
    label: str, mine: dict[str, Any], theirs: dict[str, Any]
) -> list[Difference]:
    return [
        Difference(label, key, mine[key], theirs[key])
        for key in mine
        if mine[key] != theirs[key]
    ]


def compare(
    repository: MetricView,
    deployed: MetricView,
    reliance: Reliance,
    *,
    unresolved: Iterable[str] = (),
) -> list[Difference]:
    """Every difference between the two views on what a query relies on.

    ``unresolved`` is what the reader refused in the read-back: a member it
    names that the read-back does not carry is ``unreadable`` there rather than
    ``missing``.
    """

    unresolved = list(unresolved)
    found = _differ(
        f"view {repository.name}", _view_fields(repository), _view_fields(deployed)
    )
    for member in reliance.members:
        label = f"{repository.name}.{member.name}"
        other = deployed.member(member.name)
        if other is None:
            field_ = (
                "unreadable" if _refused_there(member.name, unresolved) else "missing"
            )
            found.append(Difference(label, field_, None, None))
            continue
        found.extend(_differ(label, _member_fields(member), _member_fields(other)))
    for member in reliance.grained:
        other = deployed.member(member.name)
        if other is not None and _format_type(member) != _format_type(other):
            found.append(
                Difference(
                    f"{repository.name}.{member.name}",
                    "format.type",
                    _format_type(member),
                    _format_type(other),
                )
            )
    for path in reliance.joins:
        join = repository.join(path)
        if join is None:
            continue
        label = f"join {join.dotted}"
        other_join = deployed.join(path)
        if other_join is None:
            found.append(Difference(label, "missing", None, None))
            continue
        found.extend(_differ(label, _join_fields(join), _join_fields(other_join)))
    return found
