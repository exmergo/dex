"""An intended row change, stated and then held to the rows (#1157).

``transform compare`` answers "are the results kept". Most repairs are meant to
change them: backfill missing days, drop archived entities, pick up late
arrivals. Comparing such a plan without saying what was meant gives
``different``, which says nothing about whether only the intended rows moved,
so agents skipped the comparison and an over-broad repair went in with nothing
checking it.

An :class:`Expectation` states the change:

- ``added``: which rows may appear (``"none"``, ``"any"``, or a row predicate
  over the proposed output);
- ``removed``: which rows may disappear (the same, over the baseline output);
- ``changed_columns``: which non-key columns may change on a key present on
  both sides;
- ``key``: ``"unchanged"``, the key stays unique on the proposed side.

The comparison then splits every added, removed and changed key (or row, with
no key) into matching and not matching, names the clauses that failed, and
names up to :data:`SAMPLE_PER_CLASS` keys per failed class. Every statement
here returns counts, except the key sample, which projects key columns only and
passes through the query firewall like any other read, so a PII-flagged key is
withheld rather than shown.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from ..sql_shape import simple_row_predicate

#: Keys named per unexpected class (added, removed, changed).
SAMPLE_PER_CLASS = 5

Clause = Literal["none", "any"] | str

#: The working columns the expectation statements add, beside the comparison's.
RESERVED_COLUMNS = frozenset(
    {"dex_xa", "dex_xr", "dex_seq", "dex_cls", "dex_v", "dex_base"}
)


class ExpectationError(ValueError):
    """An expectation that cannot be judged as stated: ``invalid_expectation``."""


@dataclass(frozen=True)
class Expectation:
    """The change a plan is meant to make, as ``--expect`` states it."""

    added: Clause = "none"
    removed: Clause = "none"
    changed_columns: tuple[str, ...] = ()
    key: Literal["unchanged", "any"] = "unchanged"

    def data(self) -> dict[str, Any]:
        return {
            "added": _clause_data(self.added),
            "removed": _clause_data(self.removed),
            "changed_columns": list(self.changed_columns),
            "key": self.key,
        }

    def digest(self) -> str:
        text = json.dumps(self.data(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clause_data(clause: Clause) -> str | dict[str, str]:
    return clause if clause in ("none", "any") else {"where": clause}


def parse_expectation(raw: Any) -> Expectation:
    """Read ``--expect``'s JSON object, refusing anything it does not define.

    Accepts the object itself or ``{"expected_change": {...}}``, the spelling the
    issue proposed. Every refusal names the field, so the caller can fix it.
    """

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ExpectationError(f"--expect is not JSON: {exc}") from exc
    if isinstance(raw, dict) and set(raw) == {"expected_change"}:
        raw = raw["expected_change"]
    if not isinstance(raw, dict):
        raise ExpectationError("an expectation is a JSON object")
    unknown = sorted(set(raw) - {"added", "removed", "changed_columns", "key"})
    if unknown:
        raise ExpectationError(
            "an expectation takes added, removed, changed_columns and key; "
            f"not {', '.join(unknown)}"
        )
    columns = raw.get("changed_columns", [])
    if not isinstance(columns, list) or not all(
        isinstance(c, str) and c for c in columns
    ):
        raise ExpectationError("changed_columns is a list of output column names")
    key = raw.get("key", "unchanged")
    if key not in ("unchanged", "any"):
        raise ExpectationError('key is "unchanged" or "any"')
    return Expectation(
        added=_clause(raw.get("added", "none"), "added"),
        removed=_clause(raw.get("removed", "none"), "removed"),
        changed_columns=tuple(dict.fromkeys(columns)),
        key=key,
    )


def _clause(value: Any, name: str) -> Clause:
    if value in ("none", "any"):
        return value
    if isinstance(value, dict) and set(value) == {"where"}:
        value = value["where"]
    if isinstance(value, str) and value.strip() and value not in ("none", "any"):
        return value.strip()
    raise ExpectationError(
        f'{name} is "none", "any", or {{"where": "<row predicate>"}}'
    )


_REF = re.compile(r"""\bref\(\s*(['"])([^'"]+)\1\s*\)""")


def _wheres(expectation: Expectation) -> list[tuple[str, str]]:
    return [
        (name, clause)
        for name, clause in (
            ("added", expectation.added),
            ("removed", expectation.removed),
        )
        if clause not in ("none", "any")
    ]


def resolve_refs(
    expectation: Expectation, relations: Sequence[str], dialect: str
) -> Expectation:
    """``ref('model')`` in a lookup, as the relation the comparison reads.

    The compiled SQL names relations, not models, so a lookup naming a model
    by ``ref`` is resolved against the relations both sides read: the one whose
    last name part is the model's. One that names no such relation is refused,
    naming the fix (write the relation, as the dev build named it).
    """

    from dataclasses import replace

    def table_name(relation: str) -> str:
        try:
            table = exp.to_table(relation, dialect=dialect)
        except Exception:
            return relation.rsplit(".", 1)[-1].strip('"`')
        return table.name

    def swap(match: re.Match[str], where: str) -> str:
        model = match.group(2)
        found = [r for r in relations if table_name(r).lower() == model.lower()]
        if len(found) != 1:
            raise ExpectationError(
                f"{where} names ref('{model}'), which "
                + ("is ambiguous among" if found else "is not one of")
                + " the relations this comparison reads; write the relation itself "
                "as the dev build named it"
            )
        return found[0]

    updates = {}
    for name, clause in _wheres(expectation):
        updates[name] = _REF.sub(lambda m, n=name: swap(m, f"{n}.where"), clause)
    return replace(expectation, **updates) if updates else expectation


def lookup_relations(expectation: Expectation, dialect: str) -> list[str]:
    """The relations an expectation's ``IN (SELECT ...)`` lookups read."""

    found: list[str] = []
    for _name, clause in _wheres(expectation):
        try:
            tree = sqlglot.parse_one(clause, dialect=dialect)
        except sqlglot.errors.ParseError:
            # resolve() refuses it by name; there is nothing to profile.
            tree = None
        for node in tree.find_all(exp.In) if tree is not None else ():
            query = node.args.get("query")
            if query is not None:
                found.extend(t.sql(dialect=dialect) for t in query.find_all(exp.Table))
    return list(dict.fromkeys(found))


@dataclass(frozen=True)
class Resolved:
    """An expectation bound to one comparison's output names.

    Predicates are rendered SQL over the projected (baseline-named) columns;
    ``changed`` is the allowed columns in that same spelling.
    """

    expectation: Expectation
    added_sql: str
    removed_sql: str
    changed: tuple[str, ...]
    columns: tuple[str, ...]
    keys: tuple[str, ...] | None
    #: ``<column> IN (SELECT ...)`` terms, each read once as a distinct list and
    #: left-joined onto the compared rows as a ``dex_in<i>`` flag.
    lookups: tuple[Lookup, ...] = ()


@dataclass(frozen=True)
class Lookup:
    """One ``IN (SELECT <col> FROM <relation> [WHERE ...])`` term."""

    index: int
    column: str  # the compared output column it tests, baseline spelling
    sql: str  # select distinct <col> as dex_v from <relation> where ...


def resolve(
    expectation: Expectation,
    *,
    output_names: Sequence[str],
    proposed_names: Sequence[str],
    keys: Sequence[str] | None,
    dialect: str,
) -> Resolved:
    """Bind the expectation to the compared columns, or refuse it by name.

    A predicate may name a column by its baseline or its proposed spelling (a
    ``--map`` rename), in any case the warehouse folds; it must be a
    deterministic, row-by-row condition (the whitelist a probe uses to carry a
    filter), since it is evaluated on each compared row.
    """

    by_name: dict[str, str] = {}
    for baseline, proposed in zip(output_names, proposed_names, strict=True):
        by_name.setdefault(baseline.lower(), baseline)
        by_name.setdefault(proposed.lower(), baseline)
    key_set = {k.lower() for k in keys or ()}

    def column(name: str, where: str) -> str:
        found = by_name.get(name.lower())
        if found is None:
            raise ExpectationError(
                f"{where} names {name!r}, which is not an output column"
            )
        return found

    changed = tuple(
        column(name, "changed_columns") for name in expectation.changed_columns
    )
    keyed_changes = [name for name in changed if name.lower() in key_set]
    if keyed_changes:
        raise ExpectationError(
            "changed_columns names a key column, and a key that changes is a "
            f"removed and an added row: {', '.join(keyed_changes)}"
        )
    if changed and not keys:
        raise ExpectationError(
            "changed_columns needs a key (--key, or a declared grain) to tell a "
            "changed row from a removed and an added one"
        )
    lookups: list[Lookup] = []
    added_sql = _predicate(expectation.added, "added", column, dialect, lookups)
    removed_sql = _predicate(expectation.removed, "removed", column, dialect, lookups)
    return Resolved(
        expectation=expectation,
        added_sql=added_sql,
        removed_sql=removed_sql,
        changed=changed,
        columns=tuple(output_names),
        keys=tuple(keys) if keys else None,
        lookups=tuple(lookups),
    )


def _lookup(
    node: exp.In, name: str, column, dialect: str, lookups: list[Lookup]
) -> exp.Expression:
    """``<col> IN (SELECT <c> FROM <relation> [WHERE <row predicate>])`` as a flag.

    The one subquery an expectation may carry, since an exclusion is usually
    stated by a column the model does not output (``workspace_id IN (SELECT id
    FROM workspaces WHERE archived)``). One relation, one column, and a
    row-by-row filter: anything else is refused by name.
    """

    query = node.args.get("query")
    select = query.this if isinstance(query, exp.Subquery) else query
    where_text = f"{name}.where"
    if not isinstance(node.this, exp.Column) or node.this.table:
        raise ExpectationError(
            f"{where_text}: IN (SELECT ...) tests one output column, named alone"
        )
    if not isinstance(select, exp.Select):
        raise ExpectationError(f"{where_text}: IN takes one plain SELECT")
    tables = list(select.find_all(exp.Table))
    unsupported = [
        arg
        for arg in ("joins", "group", "having", "qualify", "limit", "order", "with")
        if select.args.get(arg)
    ]
    projections = select.expressions
    if (
        unsupported
        or len(tables) != 1
        or len(projections) != 1
        or not isinstance(projections[0].unalias(), exp.Column)
        or select.args.get("distinct")
    ):
        raise ExpectationError(
            f"{where_text}: IN takes one column of one relation "  # noqa: S608
            "(IN (SELECT <column> FROM <relation> [WHERE ...])), with no join, "
            "grouping or limit"
        )
    where = select.args.get("where")
    if where is not None:
        problem = simple_row_predicate(where.this, dialect)
        if problem is not None:
            raise ExpectationError(f"{where_text}: the IN subquery's filter {problem}")
    index = len(lookups)
    inner = projections[0].unalias().copy()
    source = tables[0].copy()
    # The parts are re-rendered from a parse the whitelist accepted.
    body = (
        f"select distinct {inner.sql(dialect=dialect)} as dex_v "  # noqa: S608
        f"from {source.sql(dialect=dialect)}"
    )
    if where is not None:
        body += f" where {where.this.sql(dialect=dialect)}"
    lookups.append(
        Lookup(index=index, column=column(node.this.name, where_text), sql=body)
    )
    return exp.EQ(this=exp.column(f"dex_in{index}"), expression=exp.Literal.number(1))


def _predicate(
    clause: Clause, name: str, column, dialect: str, lookups: list[Lookup]
) -> str:
    if clause == "none":
        return "1 = 0"
    if clause == "any":
        return "1 = 1"
    try:
        tree = sqlglot.parse_one(clause, dialect=dialect)
    except sqlglot.errors.ParseError as exc:
        raise ExpectationError(f"{name}.where does not parse: {exc}") from exc
    if tree is None:
        raise ExpectationError(f"{name}.where is empty")
    tree = tree.transform(
        lambda node: (
            _lookup(node, name, column, dialect, lookups)
            if isinstance(node, exp.In) and node.args.get("query") is not None
            else node
        ),
        copy=True,
    )
    problem = simple_row_predicate(tree, dialect)
    if problem is not None:
        raise ExpectationError(f"{name}.where: {problem}")

    def bind(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and re.fullmatch(r"dex_in\d+", node.name):
            return node
        if isinstance(node, exp.Column):
            if node.table:
                raise ExpectationError(
                    f"{name}.where qualifies {node.sql()}; name the output column alone"
                )
            return exp.column(
                exp.to_identifier(column(node.name, f"{name}.where"), quoted=True)
            )
        return node

    return tree.transform(bind).sql(dialect=dialect)


# --- statements ---------------------------------------------------------------


def with_lookups(rows_cte: str, resolved: Resolved, dialect: str) -> str:
    """``rows_cte`` with each lookup flag joined on, still named ``dex_rows``.

    The comparison's grouped rows become ``dex_base``; each lookup is read once
    as a distinct list and left-joined, so a row whose column is NULL or absent
    from the list reads 0.
    """

    if not resolved.lookups:
        return rows_cte
    if not rows_cte.rstrip().endswith(")") or "dex_rows as (" not in rows_cte:
        raise ExpectationError("the comparison rows could not carry a lookup")
    base = rows_cte.replace("dex_rows as (", "dex_base as (", 1)
    parts = [base]
    joins = []
    flags = []
    for lookup in resolved.lookups:
        alias = f"dex_l{lookup.index}"
        parts.append(f"{alias} as ({lookup.sql})")
        joins.append(
            f"left join {alias} on dex_base.{_quote(lookup.column, dialect)} = "
            f"{alias}.dex_v"
        )
        flags.append(
            f"case when {alias}.dex_v is not null then 1 else 0 end "
            f"as dex_in{lookup.index}"
        )
    parts.append(
        "dex_rows as (\n"  # noqa: S608
        f"    select dex_base.*, {', '.join(flags)}\n"
        "    from dex_base\n    " + "\n    ".join(joins) + "\n)"
    )
    return ",\n".join(parts)


def _quote(name: str, dialect: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def _count(expression: str, alias: str) -> str:
    return f"cast(coalesce({expression}, 0) as bigint) as {alias}"


def rows_sql(rows_cte: str, resolved: Resolved, dialect: str) -> str:
    """Added and removed rows (excess multiplicity), each split by its clause.

    Four counts: ``added``, ``added_matching``, ``removed``, ``removed_matching``.
    A predicate that is NULL on a row does not match it.
    """

    add = f"case when ({resolved.added_sql}) then 1 else 0 end"
    rem = f"case when ({resolved.removed_sql}) then 1 else 0 end"
    excess_r = "case when dex_rn > dex_ln then dex_rn - dex_ln else 0 end"
    excess_l = "case when dex_ln > dex_rn then dex_ln - dex_rn else 0 end"
    selected = ",\n    ".join(
        (
            _count(f"sum({excess_r})", "added"),
            _count(f"sum({excess_r} * {add})", "added_matching"),
            _count(f"sum({excess_l})", "removed"),
            _count(f"sum({excess_l} * {rem})", "removed_matching"),
        )
    )
    rows_cte = with_lookups(rows_cte, resolved, dialect)
    return f"with {rows_cte}\nselect\n    {selected}\nfrom dex_rows\n"


def _keys_cte(rows_cte: str, resolved: Resolved, dialect: str) -> tuple[str, list[str]]:
    keys = resolved.keys or ()
    key_list = ", ".join(_quote(k, dialect) for k in keys)
    others = [c for c in resolved.columns if c not in keys]
    flags = []
    for index, name in enumerate(others):
        quoted = _quote(name, dialect)
        # A key unique on both sides holds one row per side, so its group in
        # dex_rows is one row (unchanged) or two (changed). A column differs
        # when the two rows hold two values, or one NULL and one value.
        flags.append(
            f"case when count(distinct {quoted}) > 1 or "
            f"(count({quoted}) = 1 and count(*) = 2) then 1 else 0 end as dex_c{index}"
        )
    selected = ",\n           ".join(
        [
            "sum(dex_ln) as dex_kl",
            "sum(dex_rn) as dex_kr",
            "max(case when dex_ln <> dex_rn then 1 else 0 end) as dex_diff",
            f"max(case when dex_rn > 0 and ({resolved.added_sql}) then 1 else 0 end) "
            "as dex_xa",
            f"max(case when dex_ln > 0 and ({resolved.removed_sql}) then 1 else 0 end) "
            "as dex_xr",
            *flags,
        ]
    )
    cte = (
        f"{rows_cte},\n"  # noqa: S608
        "dex_keys as (\n"
        f"    select {key_list},\n           {selected}\n"
        f"    from dex_rows group by {key_list}\n"
        ")"
    )
    return cte, others


def _disallowed(resolved: Resolved, others: Sequence[str]) -> str:
    allowed = set(resolved.changed)
    terms = [f"dex_c{i} = 1" for i, name in enumerate(others) if name not in allowed]
    return " or ".join(terms) if terms else "1 = 0"


def keyed_sql(rows_cte: str, resolved: Resolved, dialect: str) -> tuple[str, int]:
    """Key-level counts, split by clause, with one changed count per column.

    Returns the statement and its width: ``non_unique_left``,
    ``non_unique_right``, ``added``, ``added_matching``, ``removed``,
    ``removed_matching``, ``changed``, ``changed_outside`` (a changed key that
    moved a column ``changed_columns`` does not name), then one count per
    non-key column in output order.
    """

    cte, others = _keys_cte(
        with_lookups(rows_cte, resolved, dialect), resolved, dialect
    )
    changed = "dex_kl > 0 and dex_kr > 0 and dex_diff = 1"
    added = "dex_kr > 0 and dex_kl = 0"
    removed = "dex_kl > 0 and dex_kr = 0"
    parts = [
        _count("sum(case when dex_kl > 1 then 1 else 0 end)", "non_unique_left"),
        _count("sum(case when dex_kr > 1 then 1 else 0 end)", "non_unique_right"),
        _count(f"sum(case when {added} then 1 else 0 end)", "added"),
        _count(
            f"sum(case when {added} and dex_xa = 1 then 1 else 0 end)", "added_matching"
        ),
        _count(f"sum(case when {removed} then 1 else 0 end)", "removed"),
        _count(
            f"sum(case when {removed} and dex_xr = 1 then 1 else 0 end)",
            "removed_matching",
        ),
        _count(f"sum(case when {changed} then 1 else 0 end)", "changed"),
        _count(
            f"sum(case when {changed} and ({_disallowed(resolved, others)}) "
            "then 1 else 0 end)",
            "changed_outside",
        ),
        *(
            _count(
                f"sum(case when {changed} and dex_c{i} = 1 then 1 else 0 end)",
                f"dex_n{i}",
            )
            for i in range(len(others))
        ),
    ]
    selected = ",\n    ".join(parts)
    return (
        f"with {cte}\nselect\n    {selected}\nfrom dex_keys\n",
        8 + len(others),
    )


def sample_sql(rows_cte: str, resolved: Resolved, dialect: str) -> str:
    """Up to :data:`SAMPLE_PER_CLASS` keys per unexpected class, keys only.

    ``dex_cls`` is ``added``, ``removed`` or ``changed``. No other column is
    projected, and the firewall judges the key columns like any projection.
    """

    cte, others = _keys_cte(
        with_lookups(rows_cte, resolved, dialect), resolved, dialect
    )
    keys = resolved.keys or ()
    key_list = ", ".join(_quote(k, dialect) for k in keys)
    cls = (
        "case "
        "when dex_kr > 0 and dex_kl = 0 and dex_xa = 0 then 'added' "
        "when dex_kl > 0 and dex_kr = 0 and dex_xr = 0 then 'removed' "
        f"when dex_kl > 0 and dex_kr > 0 and dex_diff = 1 and "
        f"({_disallowed(resolved, others)}) then 'changed' "
        "end"
    )
    return (
        f"with {cte},\n"  # noqa: S608
        "dex_cls_keys as (\n"
        f"    select {key_list}, {cls} as dex_cls from dex_keys\n"
        "),\n"
        "dex_ranked as (\n"
        f"    select {key_list}, dex_cls,\n"
        f"           row_number() over (partition by dex_cls order by {key_list}) "
        "as dex_seq\n"
        "    from dex_cls_keys where dex_cls is not null\n"
        ")\n"
        f"select dex_cls, {key_list} from dex_ranked\n"
        f"where dex_seq <= {SAMPLE_PER_CLASS}\n"
        f"order by dex_cls, {key_list}\n"
    )


# --- the report -----------------------------------------------------------------


@dataclass(frozen=True)
class ClauseCounts:
    """One clause's rows or keys: how many there were, and how many it allows."""

    total: int
    matching: int

    @property
    def unexpected(self) -> int:
        return self.total - self.matching

    def data(self) -> dict[str, int]:
        return {
            "total": self.total,
            "matching": self.matching,
            "unexpected": self.unexpected,
        }


@dataclass(frozen=True)
class ColumnChange:
    column: str
    keys_changed: int
    allowed: bool


@dataclass(frozen=True)
class ExpectationReport:
    """How the actual difference matches the stated one.

    ``unit`` is ``keys`` when a key unique on the baseline counted it, else
    ``rows`` (excess multiplicity). ``verdict`` is ``as_expected`` only when no
    clause failed.
    """

    expectation: Expectation
    unit: Literal["keys", "rows"]
    added: ClauseCounts
    removed: ClauseCounts
    changed: ClauseCounts | None = None
    columns: tuple[ColumnChange, ...] = ()
    grain: dict[str, Any] = field(default_factory=dict)
    findings: tuple[str, ...] = ()
    unexpected_keys: dict[str, list[dict[str, Any]]] | None = None
    samples_withheld: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def verdict(self) -> Literal["as_expected", "unexpected"]:
        return "unexpected" if self.findings else "as_expected"

    def data(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "stated": self.expectation.data(),
            "verdict": self.verdict,
            "findings": list(self.findings),
            "unit": self.unit,
            "added": self.added.data(),
            "removed": self.removed.data(),
        }
        if self.changed is not None:
            out["changed"] = self.changed.data()
            out["columns"] = [
                {
                    "column": c.column,
                    "keys_changed": c.keys_changed,
                    "allowed": c.allowed,
                }
                for c in self.columns
                if c.keys_changed
            ]
        out["grain"] = self.grain
        if self.unexpected_keys is not None:
            out["unexpected_keys"] = self.unexpected_keys
        if self.samples_withheld is not None:
            out["samples_withheld"] = self.samples_withheld
        if self.notes:
            out["notes"] = list(self.notes)
        return out


def judge_rows(
    resolved: Resolved,
    counts: Sequence[int],
    *,
    grain: Mapping[str, Any],
    notes: Sequence[str] = (),
) -> ExpectationReport:
    """The report from row-level counts, where no key settles a changed row."""

    added = ClauseCounts(counts[0], counts[1])
    removed = ClauseCounts(counts[2], counts[3])
    findings = []
    if added.unexpected:
        findings.append("unexpected_added")
    if removed.unexpected:
        findings.append("unexpected_removed")
    return ExpectationReport(
        expectation=resolved.expectation,
        unit="rows",
        added=added,
        removed=removed,
        grain=dict(grain),
        findings=tuple(findings),
        samples_withheld="no_key",
        notes=tuple(notes),
    )


def judge_keys(resolved: Resolved, counts: Sequence[int]) -> ExpectationReport | None:
    """The report from key-level counts, or None where the baseline key is not
    unique (the caller falls back to rows)."""

    non_unique_left, non_unique_right = counts[0], counts[1]
    if non_unique_left:
        return None
    keys = list(resolved.keys or ())
    others = [c for c in resolved.columns if c not in keys]
    added = ClauseCounts(counts[2], counts[3])
    removed = ClauseCounts(counts[4], counts[5])
    changed = ClauseCounts(counts[6], counts[6] - counts[7])
    allowed = set(resolved.changed)
    columns = tuple(
        ColumnChange(name, counts[8 + i], name in allowed)
        for i, name in enumerate(others)
    )
    holds = non_unique_right == 0
    findings = []
    if added.unexpected:
        findings.append("unexpected_added")
    if removed.unexpected:
        findings.append("unexpected_removed")
    if changed.unexpected:
        findings.append("unexpected_changed_column")
    if resolved.expectation.key == "unchanged" and not holds:
        findings.append("grain_not_held")
    return ExpectationReport(
        expectation=resolved.expectation,
        unit="keys",
        added=added,
        removed=removed,
        changed=changed,
        columns=columns,
        grain={
            "key_columns": keys,
            "holds": holds,
            "non_unique_keys": non_unique_right,
        },
        findings=tuple(findings),
    )


def read_samples(
    cells: Sequence[Sequence[Any]], keys: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in cells:
        cls, *values = row
        bucket = out.setdefault(str(cls), [])
        if len(bucket) < SAMPLE_PER_CLASS:
            bucket.append(dict(zip(keys, values, strict=False)))
    return out


def with_samples(
    report: ExpectationReport,
    *,
    samples: dict[str, list[dict[str, Any]]] | None = None,
    withheld: str | None = None,
) -> ExpectationReport:
    from dataclasses import replace

    return replace(report, unexpected_keys=samples, samples_withheld=withheld)


def schema_report(expectation: Expectation) -> ExpectationReport:
    """The report for a schema that changed: no row was compared."""

    return ExpectationReport(
        expectation=expectation,
        unit="rows",
        added=ClauseCounts(0, 0),
        removed=ClauseCounts(0, 0),
        findings=("schema_changed",),
        notes=(
            "the output columns or their types changed, which no clause of an "
            "expectation allows; state a rename with --map or a type change with "
            "--accept-type",
        ),
    )
