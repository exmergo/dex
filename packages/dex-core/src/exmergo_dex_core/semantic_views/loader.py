"""Reading the configured Semantic View files into a validated set.

Never raises for a bad file. Every problem is a :class:`Diagnostic` naming the
file and the element, and a refused element is left out of the set rather than
read best-effort:

1. **Confinement.** Each configured path resolves inside the repository,
   symlinks included, and is a file.
2. **YAML as Snowflake reads it**, through the shared strict loader
   (:mod:`..native_yaml`): YAML 1.2 booleans, and a key written twice or a key
   that is not text refused. Snowflake itself keeps one of two duplicate keys and
   accepts the file; which one a deployment reads is not something dex guesses.
3. **One view per file**, by :mod:`.validator`, named by its own ``name``. A
   file whose stem disagrees with that name warns, and two files declaring one
   view are refused.
4. **The expressions**, through :mod:`.lineage`, held to what Snowflake resolves
   (each rule checked against ``verify_only`` on a live account):

   - a name reads a member of its own table, a column of its base table, or a
     member of another table the view's relationships reach, never another
     table's base column;
   - dimensions and facts are row-level: they aggregate nothing of their own
     table and read no metric of it, and a read of a finer table is aggregated;
   - a table metric aggregates, and a read of a finer table's row-level member
     is nested in two aggregates;
   - a derived metric reads only metrics and is no window function, and nothing
     reads a window metric;
   - two tables joined more than one way (or many-to-many) are not read across
     without ``using_relationships``;
   - no expression reads itself, directly or through others.

   A refused member retracts the members that read it, to a fixed point.

Granularity follows the relationships: a relationship's left table is finer than
its right table (many rows to one), unless its left columns are themselves a key
(one-to-one), and it is transitive. Where two tables are related only through a
third in mixed directions, dex does not judge their granularity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..native_yaml import (
    MAX_EXPANDED_VALUES,
    StrictLoader,
    expansion,
    read_strict_yaml,
)
from . import validator
from .model import DERIVED, METRIC, LogicalTable, Member, SemanticView

__all__ = [
    "ERROR",
    "WARNING",
    "Diagnostic",
    "LoadResult",
    "load_files",
    "read_yaml",
    "validate_view_contents",
]

ERROR = validator.ERROR
WARNING = validator.WARNING


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with a Semantic View file.

    ``element`` names where (``tables['orders'].metrics['revenue']``), ``(root)``
    for the file as a whole, and ``(config)`` for the coordinate that named it.
    ``rule`` says which check spoke, so a caller can act on a class of finding
    without matching on prose.
    """

    file: str
    element: str
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        """The diagnostic as one line: file, element, rule and message."""
        return f"{self.file}: {self.element}: [{self.rule}] {self.message}"


@dataclass
class LoadResult:
    """Every view dex could read, and every diagnostic about the rest."""

    views: list[SemanticView] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        """The diagnostics that refused something."""
        return [d for d in self.diagnostics if d.severity == ERROR]

    @property
    def empty(self) -> bool:
        """Whether no view could be read."""
        return not self.views

    def notes(self) -> list[str]:
        """Every diagnostic, as a line for a catalog's or envelope's notes."""
        return [d.render() for d in self.diagnostics]

    def view(self, name: str) -> SemanticView | None:
        """The view named ``name`` (ignoring case), or ``None``."""
        lowered = name.lower()
        return next((v for v in self.views if v.name.lower() == lowered), None)


class _Loader(StrictLoader):
    """The shared strict loader, refusals naming a Semantic View."""

    document = "Semantic View"


def read_yaml(text: str) -> Any:
    """``text`` parsed the way dex reads a Semantic View: YAML 1.2 booleans, and
    a duplicate or non-text key refused (``yaml.YAMLError``)."""

    return read_strict_yaml(text, _Loader)


# --- the files ---------------------------------------------------------------------


def load_files(repo_root: Path | str, files: Sequence[str]) -> LoadResult:
    """Read, validate and return every configured file, named as configured."""

    root = Path(repo_root).resolve()
    contents: dict[str, str] = {}
    result = LoadResult()
    for name in files:
        path, refusal = _resolve(root, name)
        if path is None:
            result.diagnostics.append(refusal)  # type: ignore[arg-type]
            continue
        try:
            contents[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.diagnostics.append(
                _diagnostic(name, "(root)", "unreadable", f"could not read it: {exc}")
            )
    return _load(contents, result)


def validate_view_contents(contents: Mapping[str, str]) -> LoadResult:
    """Judge files given as text, keyed as ``semantic.snowflake.files`` names
    them: every check :func:`load_files` runs, over content not on disk."""

    return _load(dict(contents), LoadResult())


def _diagnostic(
    file: str, element: str, rule: str, message: str, severity: str = ERROR
) -> Diagnostic:
    """One :class:`Diagnostic`, an error unless ``severity`` says otherwise."""
    return Diagnostic(
        file=file, element=element, rule=rule, severity=severity, message=message
    )


def _resolve(root: Path, name: str) -> tuple[Path | None, Diagnostic | None]:
    """The configured name as a file inside the repository, or a refusal.
    Checked on the resolved path, so a symlink out of the repository is caught."""

    if PurePosixPath(name).is_absolute():
        return None, _diagnostic(
            name,
            "(config)",
            "confinement",
            "a Semantic View file is named relative to the repository root, and "
            "this is an absolute path",
        )
    resolved = (root / name).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, _diagnostic(
            name,
            "(config)",
            "confinement",
            f"resolves outside the repository ({resolved}); dex reads Semantic View "
            "files only from inside it",
        )
    if not resolved.is_file():
        return None, _diagnostic(name, "(config)", "missing", "no such file")
    return resolved, None


def _load(contents: dict[str, str], result: LoadResult) -> LoadResult:
    """Read and validate every file given as text, into ``result``."""
    names: dict[str, str] = {}
    for file, text in contents.items():
        view = _read_file(file, text, result)
        if view is None:
            continue
        other = names.get(view.name.lower())
        if other is not None:
            result.diagnostics.append(
                _diagnostic(
                    file,
                    "(root)",
                    "duplicate_view",
                    f"declares the view `{view.name}`, which {other} declares too; "
                    "a view is named by its `name`, so two files cannot share one",
                )
            )
            continue
        names[view.name.lower()] = file
        result.views.append(_expressions(view, result))
    return result


def _read_file(name: str, text: str, result: LoadResult) -> SemanticView | None:
    """One file's view, or ``None`` with the diagnostics that say why."""
    try:
        raw = read_yaml(text)
    except yaml.YAMLError as exc:
        result.diagnostics.append(
            _diagnostic(name, "(root)", "malformed", f"not valid YAML: {exc}")
        )
        return None
    expanded = expansion(raw)
    if expanded is None or expanded > MAX_EXPANDED_VALUES:
        reason = (
            "a YAML alias refers to the value that contains it (`&a [*a]`), so "
            "the definition has no end; write the value out"
            if expanded is None
            else f"its YAML aliases expand to more than {MAX_EXPANDED_VALUES} "
            "values, far past what a Semantic View needs; write the values out"
        )
        result.diagnostics.append(_diagnostic(name, "(root)", "malformed", reason))
        return None
    if raw is None:
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "empty_file",
                "the file is empty, so it declares no view",
                WARNING,
            )
        )
        return None
    try:
        view, findings = validator.check_view(raw, name)
    except (TypeError, ValueError, AttributeError) as exc:
        # A shape no rule anticipated is this file's problem, never the layer's:
        # the other files still read.
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "malformed",
                f"could not be read as a Semantic View ({type(exc).__name__}: {exc})",
            )
        )
        return None
    for finding in findings:
        result.diagnostics.append(
            _diagnostic(
                name, finding.element, finding.rule, finding.message, finding.severity
            )
        )
    if view is not None and PurePosixPath(name).stem.lower() != view.name.lower():
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "file_name",
                f"the file is named `{PurePosixPath(name).stem}` and declares the "
                f"view `{view.name}`; the view is named by its `name`, which is "
                "what Snowflake creates and dex lists",
                WARNING,
            )
        )
    return view


# --- the expressions ---------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What one read resolves to: a member (``member`` set), a base column of
    ``table`` (``column`` set), or nothing (``problem`` says why)."""

    table: str | None
    member: Member | None = None
    column: str | None = None
    problem: str | None = None


@dataclass
class Graph:
    """The view's relationships as granularity and reachability.

    ``up`` maps each table to the tables a many-to-one relationship takes it to
    (coarser ones); ``same`` holds one-to-one pairs. ``links`` is every
    relationship as an undirected edge, many-to-many included, for path counting.
    """

    up: dict[str, set[str]] = field(default_factory=dict)
    same: set[frozenset[str]] = field(default_factory=set)
    #: ``(left, right, many_to_many, relationship name)``, all lowered.
    links: list[tuple[str, str, bool, str]] = field(default_factory=list)

    def coarser(self, start: str) -> set[str]:
        """Every table ``start`` reaches through many-to-one (and one-to-one)
        relationships, ``start`` excluded."""
        found: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            nexts = set(self.up.get(current, ()))
            nexts |= {
                other
                for pair in self.same
                if current in pair
                for other in pair
                if other != current
            }
            for table in nexts - found - {start}:
                found.add(table)
                pending.append(table)
        return found

    def paths(self, a: str, b: str) -> tuple[int, bool, set[str]]:
        """How many simple paths join ``a`` and ``b`` (capped at 2), whether one
        runs through a many-to-many relationship, and the relationships on the
        paths found.

        Reachability is checked first, so a table no relationship reaches costs
        one walk of the graph, and the path search is bounded: past
        :data:`PATH_STEPS` steps the pair is treated as joined more than one
        way, which asks the author to name the path rather than letting a dense
        relationship graph stall the read.
        """

        if not self._reachable(a, b):
            return 0, False, set()
        count, crosses, names, steps = 0, False, set(), 0

        def walk(node: str, seen: set[str], m2m: bool, used: tuple[str, ...]) -> None:
            nonlocal count, crosses, steps
            steps += 1
            if count >= 2 or steps > PATH_STEPS:
                return
            if node == b:
                count += 1
                crosses = crosses or m2m
                names.update(used)
                return
            for index, (x, y, many, name) in enumerate(self.links):
                if node not in (x, y) or index in seen_edges:
                    continue
                other = y if node == x else x
                if other in seen:
                    continue
                seen_edges.add(index)
                walk(other, seen | {other}, m2m or many, (*used, name))
                seen_edges.discard(index)

        seen_edges: set[int] = set()
        walk(a, {a}, False, ())
        if steps > PATH_STEPS:
            count = max(count, 2)
        return count, crosses, names

    def _reachable(self, a: str, b: str) -> bool:
        """Whether any relationship path joins ``a`` and ``b``."""
        pending, seen = [a], {a}
        while pending:
            node = pending.pop()
            if node == b:
                return True
            for x, y, _, _ in self.links:
                if node in (x, y):
                    other = y if node == x else x
                    if other not in seen:
                        seen.add(other)
                        pending.append(other)
        return False

    def joined_by(self, a: str, b: str, chosen: set[str]) -> bool:
        """Whether the relationships named in ``chosen`` (lowered) alone join
        ``a`` and ``b``: what `using_relationships` selects, judged on its own
        rather than against the paths the bounded search happened to find."""
        if not chosen:
            return False
        pending, seen = [a], {a}
        while pending:
            node = pending.pop()
            if node == b:
                return True
            for x, y, _, name in self.links:
                if name in chosen and node in (x, y):
                    other = y if node == x else x
                    if other not in seen:
                        seen.add(other)
                        pending.append(other)
        return False

    def same_grain(self, a: str, b: str) -> bool:
        """Whether ``a`` and ``b`` are one table or joined one-to-one throughout."""
        if a == b:
            return True
        pending, seen = [a], {a}
        while pending:
            node = pending.pop()
            for pair in self.same:
                if node in pair:
                    for other in pair - {node}:
                        if other == b:
                            return True
                        if other not in seen:
                            seen.add(other)
                            pending.append(other)
        return False

    def finer(self, table: str, other: str) -> bool:
        """Whether ``other`` is strictly finer than ``table`` (``table`` is
        coarser than it and not one-to-one with it)."""
        return table in self.coarser(other) and other not in self.coarser(table)


#: How many steps the path search takes before it treats a pair of tables as
#: joined more than one way. A Semantic View has a handful of tables; this is
#: far past any real one.
PATH_STEPS = 20_000


def relationship_graph(view: SemanticView) -> Graph:
    """The relationships of ``view`` as a :class:`Graph`."""
    graph = Graph()
    for rel in view.relationships:
        left, right = view.table(rel.left_table), view.table(rel.right_table)
        a, b = rel.left_table.lower(), rel.right_table.lower()
        many_to_many = not (
            right is not None and (right.is_key(rel.right_columns) or not rel.equi)
        )
        graph.links.append((a, b, many_to_many, rel.name.lower()))
        if many_to_many:
            continue
        if left is not None and rel.equi and left.is_key(rel.left_columns):
            graph.same.add(frozenset((a, b)))
        else:
            graph.up.setdefault(a, set()).add(b)
    return graph


def resolve_read(
    view: SemanticView, owner: Member, qualifier: str | None, name: str
) -> Target:
    """Where one name in ``owner``'s expression points."""

    if owner.table is None:
        if qualifier is None:
            found = view.metric(name)
            if found is not None and found.name.lower() != owner.name.lower():
                return Target(table=None, member=found)
            if name.lower() in {m.lower() for m in view.refused_metrics}:
                return Target(None, problem=f"reads `{name}`, a metric that is refused")
            return Target(None, problem=f"`{name}` is no metric of the view")
        table = view.table(qualifier)
        member = table.member(name) if table else None
        if member is None:
            return Target(
                None, problem=f"`{qualifier}.{name}` is no member of the view"
            )
        return Target(table=table.name, member=member)  # type: ignore[union-attr]

    own = view.table(owner.table)
    if qualifier is None or qualifier.lower() == owner.table.lower():
        member = own.member(name) if own else None
        if member is not None and member.name.lower() != owner.name.lower():
            return Target(table=owner.table, member=member)
        if own is not None and own.is_refused(name):
            return Target(
                None,
                problem=f"reads `{name}`, a member of `{own.name}` that is refused",
            )
        return Target(table=owner.table, column=name)
    table = view.table(qualifier)
    if table is None:
        return Target(
            None,
            problem=f"reads `{qualifier}.{name}`, and `{qualifier}` is no logical "
            "table of this view",
        )
    member = table.member(name)
    if member is None and table.is_refused(name):
        return Target(
            None, problem=f"reads `{qualifier}.{name}`, a member that is refused"
        )
    if member is None:
        return Target(
            None,
            problem=f"reads `{qualifier}.{name}`, which is no member of "
            f"`{table.name}`; another table is read through its members, never its "
            "base columns",
        )
    return Target(table=table.name, member=member)


def _expressions(view: SemanticView, result: LoadResult) -> SemanticView:
    """The rules that need a view's expressions parsed. Imports lineage, the one
    sqlglot importer, here rather than at module level, so loading this module
    costs nothing on an install without the extra."""

    from . import lineage

    view = _with_windows(view, lineage)
    graph = relationship_graph(view)

    def element(member: Member) -> str:
        """Where a member sits, as a diagnostic names it."""
        if member.table is None:
            return f"metrics['{member.name}']"
        listing = {
            "dimension": "dimensions",
            "time_dimension": "time_dimensions",
            "fact": "facts",
            "metric": "metrics",
        }[member.kind]
        return f"tables['{member.table}'].{listing}['{member.name}']"

    def key(member: Member) -> tuple[str | None, str]:
        """A member's identity in the view."""
        return ((member.table or "").lower() or None, member.name.lower())

    members: list[Member] = [m for table in view.tables for m in table.members()]
    members += list(view.metrics)
    by_key = {key(m): m for m in members}
    refused: dict[tuple[str | None, str], None] = {}
    reads_of: dict[tuple[str | None, str], list[tuple[str | None, str]]] = {}

    def refuse(member: Member, rule: str, message: str) -> None:
        """Record one finding against ``member``; an error refuses it."""
        result.diagnostics.append(
            _diagnostic(
                view.file, element(member), rule, message, validator.RULES[rule]
            )
        )
        if validator.RULES[rule] == ERROR:
            refused[key(member)] = None

    for member in members:
        found = lineage.reads(member.expr)
        if found is None:
            refuse(
                member,
                "unparsed_expression",
                "its `expr` could not be parsed as Snowflake SQL, so what it reads "
                "is not resolved and its rules are not checked",
            )
            reads_of[key(member)] = []
            continue
        targets = [
            (read, resolve_read(view, member, read.qualifier, read.name))
            for read in found
        ]
        reads_of[key(member)] = [
            key(t.member) for _, t in targets if t.member is not None
        ] + [
            (str(entry["table"]).lower(), str(entry["dimension"]).lower())
            for entry in member.non_additive
        ]
        _check(view, graph, member, targets, refuse, lineage)

    _cycles(members, reads_of, by_key, refuse)
    changed = True
    while changed:
        changed = False
        for member in members:
            if key(member) in refused:
                continue
            gone = [k for k in reads_of.get(key(member), []) if k in refused]
            if gone:
                target = by_key[gone[0]]
                refuse(
                    member,
                    "reference",
                    f"reads `{_token(target)}`, which is refused",
                )
                changed = True

    def kept(table: LogicalTable, members_: tuple[Member, ...]) -> tuple[Member, ...]:
        """The members of one list that survived, with their window flag."""
        return tuple(by_key[key(m)] for m in members_ if key(m) not in refused)

    tables = tuple(
        replace(
            table,
            dimensions=kept(table, table.dimensions),
            time_dimensions=kept(table, table.time_dimensions),
            facts=kept(table, table.facts),
            metrics=kept(table, table.metrics),
        )
        for table in view.tables
    )
    derived = tuple(by_key[key(m)] for m in view.metrics if key(m) not in refused)
    return replace(view, tables=tables, metrics=derived)


def _with_windows(view: SemanticView, lineage: Any) -> SemanticView:
    """``view`` with each metric's ``window`` flag set from its expression, so
    every read of a window metric sees the flag."""

    def flagged(members: tuple[Member, ...]) -> tuple[Member, ...]:
        return tuple(replace(m, window=lineage.window(m.expr)) for m in members)

    tables = tuple(replace(t, metrics=flagged(t.metrics)) for t in view.tables)
    return replace(view, tables=tables, metrics=flagged(view.metrics))


def _token(member: Member) -> str:
    """A member as an expression names it: ``table.member``, or a derived
    metric's bare name."""
    return f"{member.table}.{member.name}" if member.table else member.name


def _check(
    view: SemanticView,
    graph: Graph,
    member: Member,
    targets: list[tuple[Any, Target]],
    refuse: Any,
    lineage: Any,
) -> None:
    """Every expression rule for one member, over its resolved reads."""

    for _, target in targets:
        if target.problem is not None:
            rule = "derived_metric" if member.table is None else "reference"
            refuse(member, rule, target.problem)
            return
        if target.member is not None and target.member.window:
            refuse(
                member,
                "window_reference",
                f"reads `{_token(target.member)}`, a window function metric, which "
                "no other expression may read",
            )
            return

    if member.table is None:
        if member.window:
            refuse(
                member,
                "derived_metric",
                "a derived metric is no window function: a window metric belongs "
                "to a logical table",
            )
            return
        for _, target in targets:
            if target.member is None or target.member.kind not in (METRIC, DERIVED):
                refuse(
                    member,
                    "derived_metric",
                    f"reads `{_token(target.member) if target.member else '?'}`, "
                    "which is no metric; a derived metric reads only metrics",
                )
                return
        return

    own = member.table.lower()
    for read, target in targets:
        if target.table is None or target.table.lower() == own:
            continue
        count, crosses, _ = graph.paths(own, target.table.lower())
        if count == 0:
            refuse(
                member,
                "reference",
                f"reads `{read.qualifier}.{read.name}`, and no relationship joins "
                f"`{member.table}` to `{target.table}`",
            )
            return
        chosen = {r.lower() for r in member.using_relationships}
        if (count > 1 or crosses) and not graph.joined_by(
            own, target.table.lower(), chosen
        ):
            refuse(
                member,
                "multiple_paths",
                f"reads `{target.table}`, which is joined to `{member.table}` more "
                "than one way or many-to-many; name the path with "
                "`using_relationships`"
                + (
                    ", which here names no relationships that join them"
                    if chosen
                    else ""
                ),
            )
            return

    if member.window:
        # A window metric is neither row-level nor a plain aggregate; dex does not
        # judge its aggregation.
        return
    if member.row_level:
        justified = False
        for read, target in targets:
            same = target.table is None or graph.same_grain(own, target.table.lower())
            if same and target.member is not None and target.member.kind == METRIC:
                refuse(
                    member,
                    "row_level",
                    f"reads `{_token(target.member)}`, a metric at its own grain; a "
                    "dimension or fact holds one value per row, and reads no "
                    "aggregate defined at its own or a finer level",
                )
                return
            if same and read.depth > 0:
                refuse(
                    member,
                    "row_level",
                    "aggregates its own grain; a dimension or fact holds one value "
                    "per row, and aggregates only what it reads from a finer table",
                )
                return
            finer = not same and graph.finer(own, target.table.lower())
            if finer and read.depth > 0:
                justified = True
            if (
                finer
                and target.member is not None
                and target.member.row_level
                and read.depth < 1
            ):
                refuse(
                    member,
                    "granularity",
                    f"reads `{_token(target.member)}`, a row of a finer table, "
                    "without an aggregate; a row-level read of a finer table needs "
                    "one",
                )
                return
        if lineage.aggregates(member.expr) and not justified:
            refuse(
                member,
                "row_level",
                "aggregates, and reads nothing of a finer table to aggregate; a "
                "dimension or fact holds one value per row",
            )
        return

    if lineage.aggregates(member.expr) is False and not any(
        t.member is not None and t.member.kind == METRIC for _, t in targets
    ):
        refuse(
            member,
            "metric_aggregate",
            "a table metric aggregates: its expression has no aggregate function "
            "and reads no other metric",
        )
        return
    for read, target in targets:
        row_level = target.column is not None or (
            target.member is not None and target.member.row_level
        )
        coarse = target.table is None or not graph.finer(own, target.table.lower())
        if row_level and coarse and read.depth == 0:
            refuse(
                member,
                "metric_aggregate",
                f"reads `{read.qualifier + '.' if read.qualifier else ''}{read.name}`, "
                "a row-level value, outside any aggregate; a table metric "
                "aggregates every row-level value it reads",
            )
            return
    for read, target in targets:
        if target.table is None or target.member is None:
            continue
        if not graph.finer(own, target.table.lower()):
            continue
        needed = 2 if target.member.row_level else 1
        if read.depth < needed:
            refuse(
                member,
                "granularity",
                f"reads `{_token(target.member)}` of a finer table with "
                f"{read.depth} aggregate(s); a metric needs "
                f"{'a nested aggregate (two)' if needed == 2 else 'one'} over it",
            )
            return


def _cycles(
    members: list[Member],
    reads_of: Mapping[tuple[str | None, str], list[tuple[str | None, str]]],
    by_key: Mapping[tuple[str | None, str], Member],
    refuse: Any,
) -> None:
    """Refuse every member that reads itself, directly or through others."""

    def key(member: Member) -> tuple[str | None, str]:
        return ((member.table or "").lower() or None, member.name.lower())

    for member in members:
        start = key(member)
        pending = list(reads_of.get(start, []))
        seen: set[tuple[str | None, str]] = set()
        while pending:
            current = pending.pop()
            if current == start:
                refuse(
                    member,
                    "expression_cycle",
                    "reads itself, directly or through other members",
                )
                break
            if current in seen or current not in by_key:
                continue
            seen.add(current)
            pending.extend(reads_of.get(current, []))
