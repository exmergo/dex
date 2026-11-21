"""A model's joins, read from its SQL at plan time (#1456).

``maintain verify`` reads a model's equality joins from its *compiled* SQL and
holds each one its declaration requires to full coverage (``join_policy``,
``maintain/verify.py``). The generated test ``dex_join_coverage`` says the same
thing in the build, so the plan has to read the same joins, before anything is
compiled: from the model's SQL as the plan leaves it.

The reading substitutes each ``ref()`` and ``source()`` call with a placeholder
relation, parses the SQL in the connector's dialect, and hands the tree to the
very readers ``maintain verify`` uses (``_model_joins`` and ``_output_lineage``),
then asks ``join_policy`` itself whether each join is required. One reading of
"required", shared with ``maintain verify``, so the test and the check cannot
drift apart. Each placeholder maps back to the call it stands for, which is
what the rendered test names, so the YAML holds ``ref('stg_orders')`` and never
a target's physical relation.

SQL with any other Jinja (a macro, a loop, a conditional, a versioned ``ref``)
is not read: what it compiles to is not in the file. The plan lists the test
``not_rendered`` (``join_unread``) and ``maintain verify``, which reads the
compiled SQL, still judges the join.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import model_versions as mv

#: The prefix of a placeholder relation, unlikely in any real project.
_PLACEHOLDER = "dex_joinref_{}_"
_SIMPLE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_VERSION_KEYWORD = re.compile(r"\b(?:v|version)\s*=")

#: Why a model's joins were not read.
UNREAD_NO_SQL = "the model has no SQL file the plan can read"
UNREAD_JINJA = (
    "its SQL holds Jinja other than ref(), source() and config(), so what it "
    "compiles to is not in the file"
)
UNREAD_PARSE = "its SQL does not parse"
UNREAD_VERSIONED = (
    "it is a versioned model's shared entry, which no one version's SQL speaks for"
)


@dataclass(frozen=True)
class RequiredJoin:
    """One join of the model, as ``dex_join_coverage`` would render it.

    ``child`` and ``parent`` are the ``ref()``/``source()`` calls the join's two
    inputs are read through, or ``None`` where a side reads something that is
    neither (a hard-coded relation). ``child_where`` and ``parent_where`` are the
    row filters the model applies to each side before the join, in the
    connector's dialect. ``status`` is ``render`` for a join the test renders,
    or the reason it does not: ``covered_by_builtin`` (a LEFT join keeps the
    unmatched rows, so ``relationships`` or ``not_null`` on the output already
    count them) or ``join_shape_unsupported`` (a key conversion, a grouped side,
    a RIGHT or FULL join, a side that is not a ``ref()`` or ``source()``, or a
    filter that does not carry into the dialect).
    """

    child: str | None
    child_columns: tuple[str, ...]
    parent: str | None
    parent_columns: tuple[str, ...]
    driving: tuple[str, ...]
    required_by: str
    null_keys: str
    side: str
    label: str
    child_where: str | None = None
    parent_where: str | None = None
    status: str = "render"
    detail: str | None = None


@dataclass
class ModelJoins:
    """What the plan read of one model's joins, or why it read nothing.

    ``read`` is ``False`` where the SQL could not be read (``reason`` says why):
    the plan then cannot tell which joins the declaration requires, and says so
    rather than rendering nothing silently.
    """

    model: str
    read: bool
    reason: str | None = None
    dialect: str | None = None
    candidates: list[Any] = field(default_factory=list)
    lineage: dict[str, tuple[str, str]] = field(default_factory=dict)
    projected: dict[tuple[int, str, str], tuple[str, ...]] = field(default_factory=dict)
    #: Placeholder relation (lowered) -> the call it stands for.
    calls: dict[str, str] = field(default_factory=dict)
    #: Node name a ``references`` can name (lowered) -> its placeholder.
    parents: dict[str, str] = field(default_factory=dict)
    #: The identifiers the SQL writes quoted (``o."CustId"``), which keep
    #: their exact spelling in the test rather than folding.
    quoted: frozenset[str] = frozenset()

    def required(self, declaration: Any) -> list[RequiredJoin]:
        """Every join of the model ``declaration`` requires, as
        ``maintain verify``'s ``join_policy`` reads the requirement."""

        if not self.read:
            return []
        from ..maintain.verify import ModelJoinContext, join_policy

        ctx = ModelJoinContext(
            lineage=self.lineage,
            columns=_declared_columns(declaration),
            parents=self.parents,
            assumptions=[],
            population_filters=(),
            projected=self.projected,
        )
        out: list[RequiredJoin] = []
        for candidate in self.candidates:
            policy = join_policy(candidate, ctx)
            if policy.required_by is None:
                continue
            out.append(self._render(candidate, policy))
        return out

    def _render(self, candidate: Any, policy: Any) -> RequiredJoin:
        child = self.calls.get(candidate.from_relation.lower())
        parent = self.calls.get(candidate.to_relation.lower())
        side = (candidate.side or "inner").split(" ")[0]
        base = {
            "child": child,
            "child_columns": tuple(
                _column(c, self.dialect, self.quoted) for c in candidate.from_columns
            ),
            "parent": parent,
            "parent_columns": tuple(
                _column(c, self.dialect, self.quoted) for c in candidate.to_columns
            ),
            "driving": tuple(policy.driving),
            "required_by": policy.required_by,
            "null_keys": (
                "unmatched" if policy.null_keys_unmatched_by is not None else "excluded"
            ),
            "side": candidate.side,
            "label": candidate.label,
        }
        if side == "left" and not candidate.to_filters:
            # The relationships test reads the whole parent, so it counts the
            # same rows only where the model joins the whole parent; a LEFT
            # join to a filtered parent renders the test (below).
            return RequiredJoin(
                **base,
                status="covered_by_builtin",
                detail=(
                    "a LEFT join keeps the unmatched rows, so the relationships "
                    "or not_null test the declaration renders counts them"
                ),
            )
        unsupported = None
        if side not in ("inner", "left"):
            unsupported = f"a {candidate.side} join"
        elif child is None or parent is None:
            unsupported = "a side that reads neither a ref() nor a source()"
        elif candidate.key_exprs() is not None:
            unsupported = "a key compared through a conversion"
        elif candidate.grouped_sides() is not None:
            unsupported = "a side read through a GROUP BY"
        child_where = parent_where = None
        if unsupported is None:
            child_where = _filter(candidate.from_filters, self.dialect)
            parent_where = _filter(candidate.to_filters, self.dialect)
            if child_where is False or parent_where is False:
                unsupported = "a filter that does not carry into the dialect"
        if unsupported is not None:
            return RequiredJoin(
                **base,
                status="join_shape_unsupported",
                detail=unsupported,
            )
        return RequiredJoin(
            **base,
            child_where=child_where or None,
            parent_where=parent_where or None,
        )


_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _column(
    name: str, dialect: str | None, quoted: frozenset[str] = frozenset()
) -> str:
    """A key column as the test writes it into SQL: as written where it is a
    plain identifier, which the warehouse folds as it folds the model's own
    columns, and quoted in ``dialect`` where it cannot stand unquoted
    (``customer id``) or the SQL wrote it quoted (``"CustId"``, which folding
    would turn into another column on Snowflake)."""

    if _PLAIN_IDENTIFIER.match(name) and name not in quoted:
        return name
    from sqlglot import exp

    return exp.to_identifier(name, quoted=True).sql(dialect=dialect or "duckdb")


def _declared_columns(declaration: Any) -> dict[str, dict]:
    """Each declared column's facts ``join_policy`` reads, as the manifest's
    ``config.meta.dex`` would carry them (lowered names)."""

    out: dict[str, dict] = {}
    for name, column in (getattr(declaration, "columns", None) or {}).items():
        role = getattr(column, "role", None)
        out[str(name).lower()] = {
            "references": getattr(column, "references", None),
            "role": getattr(role, "value", role),
            "null_rule": getattr(column, "null_rule", None),
        }
    return out


def _filter(filters: Iterable[str], dialect: str | None) -> str | bool | None:
    """A side's filters, each carried by ``maintain verify`` as DuckDB SQL over
    quoted physical columns, joined and written in ``dialect`` with plain names
    unquoted, so the warehouse resolves them as it resolves the model's own
    columns. ``None`` where the side is unfiltered; ``False`` where a filter
    does not transpile."""

    filters = tuple(filters)
    if not filters:
        return None
    import sqlglot
    from sqlglot import exp

    parts: list[str] = []
    try:
        for text in filters:
            tree = sqlglot.parse_one(text, read="duckdb")
            for identifier in tree.find_all(exp.Identifier):
                if identifier.args.get("quoted") and _SIMPLE_IDENTIFIER.match(
                    identifier.name
                ):
                    identifier.set("quoted", False)
            parts.append(tree.sql(dialect=dialect or "duckdb"))
    except Exception:
        return False
    if len(parts) == 1:
        return parts[0]
    return " and ".join(f"({p})" for p in parts)


def _substitute(content: str) -> tuple[str, dict[str, str], dict[str, str]] | str:
    """``content`` with each ``ref()``/``source()`` region replaced by a
    placeholder relation, the placeholders mapped to their calls, and the node
    names mapped to their placeholders; or the reason the SQL cannot be read."""

    from ..dbt_project import jinja_regions

    regions, masked = jinja_regions(content)
    out: list[str] = []
    cursor = 0
    calls: dict[str, str] = {}
    parents: dict[str, str] = {}
    by_call: dict[str, str] = {}
    for region in regions:
        out.append(masked[cursor : region.start])
        cursor = region.end
        spanning = next((c for c in region.calls if c.spans_region), None)
        if region.kind == "expression" and spanning is not None:
            if spanning.callee == "config":
                out.append(" " * (region.end - region.start))
                continue
            if spanning.callee in ("ref", "source") and len(region.calls) == 1:
                args = spanning.args
                if (
                    _VERSION_KEYWORD.search(region.body)
                    or not args
                    or any(a is None for a in args)
                ):
                    return UNREAD_JINJA
                if spanning.callee == "ref" and len(args) in (1, 2):
                    call = "ref(" + ", ".join(f"'{a}'" for a in args) + ")"
                    node = str(args[-1])
                elif spanning.callee == "source" and len(args) == 2:
                    call = f"source('{args[0]}', '{args[1]}')"
                    node = None
                else:
                    return UNREAD_JINJA
                placeholder = by_call.get(call)
                if placeholder is None:
                    placeholder = _PLACEHOLDER.format(len(by_call))
                    by_call[call] = placeholder
                    calls[placeholder] = call
                if node is not None:
                    parents.setdefault(node.lower(), placeholder)
                out.append(placeholder)
                continue
        return UNREAD_JINJA
    out.append(masked[cursor:])
    return "".join(out), calls, parents


def read_model_joins(
    files: Mapping[str, str],
    model_paths: Iterable[str],
    model: str,
    dialect: str | None,
) -> ModelJoins:
    """The joins of ``model``'s SQL as the plan leaves it (``files``), read as
    ``maintain verify`` reads compiled SQL. Never raises: a model whose SQL
    cannot be read comes back with ``read=False`` and the reason."""

    from .declarations import _version_sql_path, model_sql_path

    model_paths = list(model_paths)
    _base, version = mv.split_label(model)
    if version is None and _version_sql_path(files, model_paths, model) is not None:
        return ModelJoins(model, False, UNREAD_VERSIONED, dialect)
    path = model_sql_path(files, model_paths, model)
    content = files.get(path) if path else None
    if not content:
        return ModelJoins(model, False, UNREAD_NO_SQL, dialect)
    substituted = _substitute(content)
    if isinstance(substituted, str):
        return ModelJoins(model, False, substituted, dialect)
    sql, calls, parents = substituted
    try:
        import sqlglot

        from .. import sql_shape
        from ..maintain.verify import _final_output_map, _model_joins, _output_lineage

        tree = sqlglot.parse_one(sql, read=dialect or "duckdb")
        candidates, _skipped = _model_joins(tree, sql_shape, dialect)
        lineage = _output_lineage(tree, sql_shape)
        projected = _final_output_map(tree)
        quoted = frozenset(
            identifier.name
            for identifier in tree.find_all(sqlglot.exp.Identifier)
            if identifier.args.get("quoted")
        )
    except Exception:
        return ModelJoins(model, False, UNREAD_PARSE, dialect)
    return ModelJoins(
        model,
        True,
        None,
        dialect,
        candidates=list(candidates),
        lineage=lineage,
        projected=projected,
        calls={k.lower(): v for k, v in calls.items()},
        parents=parents,
        quoted=quoted,
    )
