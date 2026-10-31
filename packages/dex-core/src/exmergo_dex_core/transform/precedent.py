"""Precedent: how the project's other models already treat a column (#494).

When a request does not say which rows belong, the best evidence is usually in
the project already: every other model reading ``stg_orders.status`` made that
choice, and four of them excluding cancelled orders while one keeps them is
worth knowing before writing a fifth. Agents rarely look, because finding it
means reading every sibling's SQL and following each alias and CTE back to the
column.

This module does that reading. For one upstream column it lists the models
that read its relation, groups them by what they do with the column (a filter
conjunct, a ``CASE`` condition, an aggregation, or nothing at all), and counts
each group. It shares the lineage reader of :mod:`.drafting`, so a column is the
same column only when the SQL resolves to it: ``status`` on an unrelated
relation is not precedent, and ``o.status`` through an aliased CTE is.

Three rules shape the answer.

**Evidence, not a rule.** Every group is listed and none is called the
majority. Conflicting precedent is the useful case; the agent decides and
records the decision with ``evidence: precedent``.

**Honest about its limits.** A reader dex could not read statically, a
``ref()`` it could not resolve, or a use of the column it could not trace makes
the answer ``incomplete`` with each reason in ``limits``, the vocabulary
``transform references`` already uses. A reader dex could not trace is never
counted as one that ignores the column.

**Repository and cache only.** Nothing here opens a connection. The cache is
read only so a ``select *`` can be expanded, and the expressions reported are
the project's own SQL, never a value read from the warehouse.

Grouping is by sqlglot's rendering of each fragment with columns reduced to
their upstream names, not by logical equivalence: ``status not in ('a', 'b')``
and ``status <> 'a' and status <> 'b'`` are two groups.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from .drafting import _untoken, read

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..references import ReferenceIndex

#: How much one column's precedent lists before it counts instead. Enough to
#: read the decision in one pass; ``--full`` on ``transform references`` lifts
#: both, and every elision is counted.
MAX_GROUPS = 10
MAX_MODELS = 5

#: The group for a model that reads the relation and does nothing with the
#: column, which for a population question means it keeps every value.
NO_USE = "none"

_FIRST_CLAUSE = {
    "where": 0,
    "having": 1,
    "qualify": 2,
    "join": 3,
    "case": 4,
    "aggregation": 5,
}


def usage_of(
    targets: Iterable[tuple[str, str]],
    files: Mapping[str, str],
    model_paths: Iterable[str],
    cache: DexCache | None,
    *,
    dialect: str | None,
    vars: Mapping[str, Any] | None,
    index: ReferenceIndex,
    exclude: str | None = None,
    full: bool = False,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Each ``(relation, column)``'s precedent among the project's models.

    ``files`` is the project as it reads once the plan lands, and ``index`` was
    built over the same state. ``exclude`` is the model being planned, whose own
    use is reported by the caller rather than counted as precedent for itself.
    """

    roots = [PurePosixPath(p) for p in model_paths]
    readings: dict[str, Any] = {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for relation, column in targets:
        key = (relation, column.lower())
        if key in out:
            continue
        out[key] = _one(
            relation,
            column,
            files,
            roots,
            cache,
            dialect=dialect,
            vars=vars,
            index=index,
            exclude=exclude,
            full=full,
            readings=readings,
        )
    return out


def _one(
    relation: str,
    column: str,
    files: Mapping[str, str],
    roots: list[PurePosixPath],
    cache: DexCache | None,
    *,
    dialect: str | None,
    vars: Mapping[str, Any] | None,
    index: ReferenceIndex,
    exclude: str | None,
    full: bool,
    readings: dict[str, Any],
) -> dict[str, Any]:
    wanted = column.lower()
    groups: dict[tuple[str, str | None], set[str]] = {}
    limits: list[str] = []
    readers = 0
    for path in index.readers_of(relation):
        model = PurePosixPath(path).stem
        if model == exclude or path not in files or not _is_model_sql(path, roots):
            continue
        if path not in readings:
            readings[path] = read(files[path], cache, dialect=dialect, vars=vars)
        reading = readings[path]
        unread = [
            reason
            for reason in reading.incomplete
            if not reason.startswith("the final SELECT list")
        ]
        uses = [
            usage
            for usage in reading.usages
            if usage.relation.lower() == relation.lower()
            and usage.column.lower() == wanted
        ]
        if not uses and (unread or wanted in reading.untraced):
            limits.append(
                f"'{model}' reads {relation}, but dex could not tell what it does "
                f"with {column}: "
                + ("; ".join(unread) if unread else "a use of it was not traced")
            )
            continue
        readers += 1
        if not uses:
            groups.setdefault((NO_USE, None), set()).add(model)
        for usage in uses:
            expression = _untoken(usage.expression, reading.tokens)
            groups.setdefault((usage.clause, expression), set()).add(model)

    unresolved = [
        reference
        for kind in ("model", "seed", "snapshot", "source")
        for reference in index.indeterminate_for(kind)
        if reference.package is None
        and reference.form in ("ref_call", "source_call")
        and reference.path in files
    ]
    if unresolved:
        where = sorted({reference.path for reference in unresolved})
        limits.append(
            f"{len(unresolved)} reference(s) could not be resolved statically "
            f"({', '.join(where)}); each may or may not read {relation}"
        )

    ordered = sorted(
        groups.items(),
        key=lambda item: (
            -len(item[1]),
            _FIRST_CLAUSE.get(item[0][0], len(_FIRST_CLAUSE)),
            item[0][1] or "",
        ),
    )
    listed = ordered if full else ordered[:MAX_GROUPS]
    limits = list(dict.fromkeys(limits))
    return {
        "column": f"{relation}.{column}",
        "readers": readers,
        "groups": [
            _group(clause, expression, models, full)
            for (clause, expression), models in listed
        ],
        "elided_groups": len(ordered) - len(listed),
        "completeness": "complete" if not limits else "incomplete",
        "limits": limits,
    }


def _group(
    clause: str, expression: str | None, models: set[str], full: bool
) -> dict[str, Any]:
    names = sorted(models)
    shown = names if full else names[:MAX_MODELS]
    group: dict[str, Any] = {"usage": clause}
    if expression is not None:
        group["expression"] = expression
    group["count"] = len(names)
    group["models"] = shown
    group["elided_models"] = len(names) - len(shown)
    return group


def _is_model_sql(path: str, roots: list[PurePosixPath]) -> bool:
    candidate = PurePosixPath(path)
    return candidate.suffix == ".sql" and any(
        root == candidate or root in candidate.parents for root in roots
    )
