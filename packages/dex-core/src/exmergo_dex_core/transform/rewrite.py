"""Surgical edits: change the bytes that name a thing, and nothing else.

Every rewrite here is a splice into the original text at offsets a reader
computed, never a round trip through a printer. That is the whole design, and it
is worth being explicit about why, because regenerating is the obvious
alternative and it is wrong for this job.

A propagation plan is reviewed as a diff. Renaming a column across five models by
parsing each one and printing it back out produces five whole-file diffs in which
the actual change is invisible: comments are gone, the author's casing and line
breaks are gone, and every dialect spelling has been normalised to sqlglot's. The
reviewer cannot see what dex did, which defeats the point of proposing rather than
imposing. Splicing produces a diff with exactly the renamed identifiers in it.

Three readers, one per surface, each giving offsets into the *original* text:

- SQL, through ``sqlglot``. Its parse tree carries a byte range on every
  identifier and says what each one is, so a column name is distinguishable from
  the table that qualifies it and from a table that merely shares its spelling.
- YAML, through ``yaml.compose``, whose nodes carry marks. A structural walk
  reaches the scalars that *name* something and leaves descriptions, which may
  well contain the same word, alone.
- Jinja, through ``dbt_project.jinja_regions``, whose calls carry a span for the
  callee and for each resolved argument.

**Every SQL rewrite is checked after the fact** by re-reading the result and
comparing its output columns against what was intended. A splice at a wrong
offset produces SQL that still parses, so the offsets being right is not
something to take on trust.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlglot
import yaml
from sqlglot import expressions as exp

from .. import model_versions as mv
from ..dbt_project import jinja_regions, metric_inputs, physical_column
from ..errors import DexError
from ..references import blank_jinja
from ..yaml_items import dump_items
from ..yaml_spans import (
    before_trailing_blanks,
    block_end,
    indent_of,
    sequence_item_span,
)


class RewriteError(DexError):
    """A file dex cannot rewrite without guessing. Always names the file."""


@dataclass
class SqlRewrite:
    """The result of one SQL rewrite.

    ``star`` is not an error and not a silent skip. A model whose outermost
    SELECT projects ``*`` carries every column of its inputs through by
    construction, so a renamed column arrives downstream under its new name with
    no edit at all, and a threaded column arrives the same way. The caller has to
    say that in the plan, which is why it comes back as a fact rather than as a
    zero count.
    """

    content: str
    changed: int = 0
    star: bool = False


def splice(content: str, edits: list[tuple[int, int, str]]) -> str:
    """Replace each half-open ``[start, end)`` range with its text.

    Applied right to left so an earlier edit's offsets are never shifted by a
    later one. Overlapping ranges are a caller bug rather than an input
    condition, so they are asserted rather than reconciled.
    """

    ordered = sorted(edits, key=lambda edit: edit[0], reverse=True)
    last_start = len(content) + 1
    out = content
    for start, end, text in ordered:
        if end > last_start:
            raise RewriteError(
                f"overlapping rewrites at {start}-{end}; dex would have produced "
                "a file it could not predict, so nothing was changed"
            )
        last_start = start
        out = out[:start] + text + out[end:]
    return out


# --- SQL ----------------------------------------------------------------------


def _outermost_select(parsed: exp.Expression) -> exp.Select | None:
    """The SELECT whose projections are the model's output columns.

    ``None`` for a set operation, where each branch projects independently and
    there is no single list to read or to add to.
    """

    node: Any = parsed
    while isinstance(node, (exp.With, exp.Subquery)):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def _parse_model(content: str, path: str) -> tuple[str, exp.Expression, exp.Select]:
    """The jinja-blanked SQL, its parse tree, and its outermost SELECT.

    Blanking preserves every offset and newline, so an offset the parse tree
    reports is an offset into the file a human opens. Anything that does not
    reduce to a single SELECT is refused here rather than guessed at, which is
    what makes every caller below able to assume it has a projection list.
    """

    blanked = blank_jinja(content)
    try:
        parsed = sqlglot.parse_one(blanked, read="duckdb")
    except Exception as exc:
        raise RewriteError(
            f"{path}: dex could not parse this model's SQL ({exc}), so it cannot "
            "rewrite it without guessing. Fix the SQL, or make this edit by hand"
        ) from exc
    if parsed is None:
        raise RewriteError(f"{path}: this model's SQL is empty")
    select = _outermost_select(parsed)
    if select is None:
        raise RewriteError(
            f"{path}: this model's outermost statement is a set operation, whose "
            "branches each project their own columns; dex will not guess which "
            "branch a column change belongs in. Make this edit by hand"
        )
    return blanked, parsed, select


def _projects_star(select: exp.Select) -> bool:
    return any(
        isinstance(projection, exp.Star)
        or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        )
        for projection in select.expressions
    )


def _passes_through(select: exp.Select) -> bool:
    """Whether ``select`` is a bare ``select * from <one relation>``.

    A filter, an ordering, a limit or a ``distinct`` changes which rows reach the
    output and not which columns, so the list beneath is still the model's list.
    A join or a lateral adds the other side's columns to the star, and a grouping
    makes the star something else, so following any of those would put an edit
    in a list the output does not simply mirror.
    """

    if len(select.expressions) != 1 or not isinstance(select.expressions[0], exp.Star):
        return False
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None or not isinstance(from_.this, exp.Table):
        return False
    return not any(
        select.args.get(key) for key in ("joins", "laterals", "group", "having")
    )


def _projecting_select(
    parsed: exp.Expression, select: exp.Select, through_ctes: bool
) -> tuple[exp.Select, str | None]:
    """The SELECT that names the model's columns, and the CTE it is the body of.

    Without ``through_ctes`` that is the outermost SELECT and no CTE, which is
    what every caller saw before the option existed. With it, a final
    ``select * from renamed`` is followed into ``renamed``, and on through any
    chain of bare stars, to the first list that names columns: the shape the dbt
    scaffold writes, and the one where a column is added or removed by a person.
    A star over something that is not one of this query's CTEs (a ``source()``,
    a table) stops the walk where it is, so the caller still sees a star.
    """

    if not through_ctes:
        return select, None
    with_ = parsed.args.get("with_") or parsed.args.get("with")
    ctes = {
        cte.alias.lower(): cte.this
        for cte in (with_.expressions if with_ is not None else [])
        if isinstance(cte, exp.CTE) and isinstance(cte.this, exp.Select)
    }
    current, name, seen = select, None, set()
    while _passes_through(current):
        from_ = current.args.get("from_") or current.args.get("from")
        table = from_.this
        target = table.name.lower()
        if table.args.get("db") or target not in ctes or target in seen:
            break
        seen.add(target)
        current, name = ctes[target], target
    return current, name


def output_columns(
    content: str, path: str, *, through_ctes: bool = False
) -> set[str] | None:
    """The lowercased output column names, or ``None`` where a star hides them.

    ``None`` is *unknown*, never *empty*. A caller proving an ancestor has the
    inputs a derivation needs cannot treat a star as proof of anything.
    ``through_ctes`` reads the list a trailing ``select * from <cte>`` mirrors
    (see :func:`_projecting_select`) rather than the star itself.
    """

    _blanked, parsed, outer = _parse_model(content, path)
    select, _cte = _projecting_select(parsed, outer, through_ctes)
    if _projects_star(select):
        return None
    names = set()
    for projection in select.expressions:
        name = projection.alias_or_name
        if not name:
            return None
        names.add(name.lower())
    return names


def rename_column_in_sql(content: str, path: str, old: str, new: str) -> SqlRewrite:
    """Rename every use of column ``old`` in one model's SQL.

    Identifiers are taken from the parse tree rather than from the token stream,
    which is what keeps a table called ``order_id`` and the ``o`` in
    ``o.order_id`` out of the rewrite: the tree says which slot each identifier
    sits in, and only a column's own name and an output alias are renamed.

    A quoted identifier keeps its quotes, because the span covers what is inside
    them.
    """

    _blanked, parsed, select = _parse_model(content, path)
    star = _projects_star(select)

    edits: list[tuple[int, int, str]] = []
    for identifier in parsed.find_all(exp.Identifier):
        parent = identifier.parent
        if parent is None or identifier.name != old:
            continue
        slot = next(
            (key for key, value in parent.args.items() if value is identifier), None
        )
        is_column_name = isinstance(parent, exp.Column) and slot == "this"
        is_output_alias = isinstance(parent, exp.Alias) and slot == "alias"
        if not (is_column_name or is_output_alias):
            continue
        start, end = identifier.meta.get("start"), identifier.meta.get("end")
        if start is None or end is None:
            raise RewriteError(
                f"{path}: dex read a use of '{old}' it could not locate in the "
                "file, so it will not rewrite this model"
            )
        edits.append((start, end + 1, new))

    if not edits:
        return SqlRewrite(content=content, changed=0, star=star)

    rewritten = splice(content, edits)
    _verify_rename(content, rewritten, path, old, new)
    return SqlRewrite(content=rewritten, changed=len(edits), star=star)


def _verify_rename(before: str, after: str, path: str, old: str, new: str) -> None:
    """Re-read the rewritten model and check it says what dex meant it to say.

    A splice at a wrong offset yields SQL that still parses, so this is the only
    thing standing between an offset bug and a plan that looks reviewable and is
    not. Compares the whole output column set rather than only the renamed name,
    so a rewrite that also disturbed a neighbouring column is caught.
    """

    was = output_columns(before, path)
    now = output_columns(after, path)
    if was is None or now is None:
        return
    expected = {new.lower() if name == old.lower() else name for name in was}
    if now != expected:
        raise RewriteError(
            f"{path}: rewriting '{old}' to '{new}' would have produced columns "
            f"{sorted(now)} where {sorted(expected)} was intended, so nothing "
            "was changed. This is a defect in dex; make this edit by hand and "
            "please report it"
        )


def project_column_in_sql(
    content: str,
    path: str,
    expression: str,
    alias: str,
    *,
    through_ctes: bool = False,
) -> SqlRewrite:
    """Add one projection to a model's outermost SELECT.

    Inserted before the top-level ``FROM``, at the indentation the last existing
    projection uses, so a threaded column lands where a human would have put it.

    A model projecting a star is returned untouched: it already carries the
    column, and adding it explicitly would shadow the star's copy with a second
    one of the same name.

    ``through_ctes`` adds to the list a trailing ``select * from <cte>`` mirrors
    instead (see :func:`_projecting_select`). It also counts a bare column as
    already there when any projection reads it, however it is spelled or
    aliased: ``cast(x as ...) as x`` and ``x as x_label`` both mean a person
    already brought the column in, and a second copy beside theirs would be a
    duplicate, not a fix.
    """

    blanked, parsed, outer = _parse_model(content, path)
    select, cte = _projecting_select(parsed, outer, through_ctes)
    if _projects_star(select):
        return SqlRewrite(content=content, changed=0, star=True)
    projected = {(p.alias_or_name or "").lower() for p in select.expressions}
    if alias.lower() in projected:
        return SqlRewrite(content=content, changed=0)
    if through_ctes and expression == alias and _reads(select.expressions, alias):
        return SqlRewrite(content=content, changed=0)
    _refuse_ungrouped(select, path, expression, alias)

    insert_at = _projection_end(blanked, path, cte)
    projection = expression if expression == alias else f"{expression} as {alias}"
    indent = _indent_before(content, insert_at)
    rewritten = splice(content, [(insert_at, insert_at, f",\n{indent}{projection}")])

    now = output_columns(rewritten, path, through_ctes=through_ctes)
    if now is None or alias.lower() not in now:
        raise RewriteError(
            f"{path}: adding '{alias}' did not produce a column called that, so "
            "nothing was changed. This is a defect in dex; make this edit by "
            "hand and please report it"
        )
    return SqlRewrite(content=rewritten, changed=1)


def _refuse_ungrouped(
    select: exp.Select, path: str, expression: str, alias: str
) -> None:
    """Refuse to add a non-aggregate column to an aggregating SELECT.

    A model with a ``GROUP BY`` projects one row per group, so a bare column added
    to it is neither grouped nor aggregated and the query is invalid. Which of the
    two it should become is a question about what the model *means*: grouping by
    it changes the grain, aggregating it picks one value out of many, and dex
    cannot know which was wanted. Refusing and naming both options is the honest
    move, and it fails here rather than at ``dbt run``.

    An aggregate expression is admitted, because that one needs no decision.
    """

    if not select.args.get("group"):
        return
    try:
        added = sqlglot.parse_one(expression, read="duckdb")
    except Exception:
        added = None
    if added is not None and list(added.find_all(exp.AggFunc)):
        return
    raise RewriteError(
        f"{path}: this model groups its rows, so '{alias}' cannot simply be "
        "added to it. Decide what it means here: add it to the GROUP BY to make "
        "it part of the grain, or wrap it in an aggregate (min, max, any_value) "
        "to pick one value per group. dex will not choose that for you"
    )


def _projection_spans(
    blanked: str, path: str, cte: str | None = None
) -> list[tuple[int, int]]:
    """The span of each projection in the outermost SELECT, in source order.

    Read off the token stream rather than the parse tree because a projection is
    not always an identifier: ``1 as n`` and ``count(*)`` carry no span the tree
    would report, and removing one means removing all of its text, not the part
    that happens to be a name.

    Depth tracking is what keeps a CTE's own ``SELECT``/``FROM`` and every
    subquery's out of this: only the top-level pair bounds the list, and only a
    top-level comma separates it. Positionally aligned with
    ``select.expressions``, since both are in source order.

    ``cte`` reads the list inside that CTE's body instead, measured the same way
    from inside its parentheses. Token offsets are offsets into the whole file,
    so the spans need no translating either way.
    """

    try:
        tokens = sqlglot.tokenize(blanked)
    except Exception as exc:
        raise RewriteError(f"{path}: dex could not read this model's SQL") from exc
    if cte is not None:
        tokens = _cte_body(tokens, cte, path)

    depth = 0
    inside: list[Any] = []
    started = False
    for token in tokens:
        if token.token_type is sqlglot.TokenType.L_PAREN:
            depth += 1
        elif token.token_type is sqlglot.TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and token.token_type is sqlglot.TokenType.SELECT:
            inside, started = [], True
            continue
        elif depth == 0 and started and token.token_type is sqlglot.TokenType.FROM:
            break
        if started:
            inside.append((token, depth))

    if not started:
        raise RewriteError(
            f"{path}: dex could not find this model's column list, so it will "
            "not change it"
        )

    spans: list[tuple[int, int]] = []
    current: list[Any] = []
    for token, token_depth in inside:
        if token_depth == 0 and token.token_type is sqlglot.TokenType.COMMA:
            if current:
                spans.append((current[0].start, current[-1].end + 1))
            current = []
            continue
        current.append(token)
    if current:
        spans.append((current[0].start, current[-1].end + 1))
    return spans


def _cte_body(tokens: list[Any], cte: str, path: str) -> list[Any]:
    """The tokens between the parentheses of ``<cte> as ( ... )``.

    Matched at the depth a ``with`` list sits at, so a subquery aliased the same
    way inside some other CTE is never mistaken for it.
    """

    depth = 0
    for index, token in enumerate(tokens):
        if token.token_type is sqlglot.TokenType.L_PAREN:
            depth += 1
        elif token.token_type is sqlglot.TokenType.R_PAREN:
            depth -= 1
        elif (
            depth == 0
            and token.text.strip('"`').lower() == cte
            and index + 2 < len(tokens)
            and tokens[index + 1].token_type is sqlglot.TokenType.ALIAS
            and tokens[index + 2].token_type is sqlglot.TokenType.L_PAREN
        ):
            inner, level = [], 0
            for candidate in tokens[index + 3 :]:
                if candidate.token_type is sqlglot.TokenType.L_PAREN:
                    level += 1
                elif candidate.token_type is sqlglot.TokenType.R_PAREN:
                    if level == 0:
                        return inner
                    level -= 1
                inner.append(candidate)
            break
    raise RewriteError(
        f"{path}: dex could not find the body of the '{cte}' CTE, so it will not "
        "change its column list"
    )


def _projection_end(blanked: str, path: str, cte: str | None = None) -> int:
    """The offset just past the last projection of the outermost SELECT, or of
    ``cte``'s SELECT when one is named."""

    spans = _projection_spans(blanked, path, cte)
    if not spans:
        raise RewriteError(
            f"{path}: this model projects no columns, so dex has nothing to add to"
        )
    return spans[-1][1]


def unproject_column_in_sql(
    content: str, path: str, column: str, *, through_ctes: bool = False
) -> SqlRewrite:
    """Remove one projection from a model's outermost SELECT.

    The comma that separated it goes with it, chosen from whichever side exists
    so removing the first or the last projection leaves valid SQL either way.

    A model projecting a star is left alone and says so: the column is not named
    there, and dex will not rewrite a star into an explicit list to exclude one
    column, because that would freeze every other column at today's shape.

    ``through_ctes`` removes it from the list a trailing ``select * from <cte>``
    mirrors instead (see :func:`_projecting_select`), and is the mode for a
    column that is *gone upstream* rather than one being retired by choice. Only
    a projection that is the column itself goes, ``status`` or
    ``cast(paid_at as timestamp(6)) as paid_at``: every column it reads is this
    one and it keeps the name. Any other read of the column anywhere in the model
    (``status as order_status``, ``md5(status)``, a ``where`` on it) is refused
    with what it is, because what that logic should become without its input is
    a decision about the model, and deleting it would be making it.
    """

    blanked, parsed, outer = _parse_model(content, path)
    select, cte = _projecting_select(parsed, outer, through_ctes)
    if _projects_star(select):
        if through_ctes:
            # A star the walk could not see past (one over a join) still leaves
            # every list beneath it naming the column, so a read anywhere is
            # refused here exactly as it is below.
            _refuse_hand_written_reads(select, parsed, [], column, blanked, path)
        return SqlRewrite(content=content, changed=0, star=True)

    spans = _projection_spans(blanked, path, cte)
    if len(spans) != len(select.expressions):
        raise RewriteError(
            f"{path}: dex read {len(spans)} column(s) in this model where its "
            f"parser read {len(select.expressions)}, so it will not edit the "
            "list. Make this edit by hand"
        )
    target = column.lower()
    matched = [
        index
        for index, projection in enumerate(select.expressions)
        if (projection.alias_or_name or "").lower() == target
    ]
    if through_ctes:
        _refuse_hand_written_reads(select, parsed, matched, column, blanked, path)
        if matched and not _reads([select.expressions[matched[0]]], target):
            # `'n/a' as status` keeps its name and reads nothing upstream, so the
            # column going away upstream does not touch it.
            matched = []
    if not matched:
        return SqlRewrite(content=content, changed=0)
    if len(spans) == 1:
        raise RewriteError(
            f"{path}: '{column}' is the only column this model projects, so "
            "removing it would leave no query. Delete the model instead"
        )

    index = matched[0]
    start, end = spans[index]
    # Removed by whole lines where the column owns its lines, which is how dbt
    # models are written and what makes the diff read as "this column is gone"
    # rather than as a reflow. A trailing `-- comment` on that line goes with it,
    # because it was a comment about this column.
    region = _own_lines(content, start, end) or (start, end)
    edits = [(region[0], region[1], "")]
    if index + 1 < len(spans):
        comma = content.find(",", end, spans[index + 1][0])
        if comma >= region[1]:
            edits.append((comma, comma + 1, ""))
    else:
        # The last column carries no comma of its own; the one that separated it
        # from the column above has to go or the list ends on a comma.
        comma = content.rfind(",", spans[index - 1][1] - 1, start)
        if 0 <= comma < region[0]:
            edits.append((comma, comma + 1, ""))
    rewritten = splice(content, edits)

    now = output_columns(rewritten, path, through_ctes=through_ctes)
    if now is None or column.lower() in now:
        raise RewriteError(
            f"{path}: removing '{column}' left it still projected, so nothing "
            "was changed. This is a defect in dex; make this edit by hand and "
            "please report it"
        )
    return SqlRewrite(content=rewritten, changed=1)


def _refuse_hand_written_reads(
    select: exp.Select,
    parsed: exp.Expression,
    matched: list[int],
    column: str,
    blanked: str,
    path: str,
) -> None:
    """Refuse a removal that would take, or leave broken, logic a person wrote.

    The one projection that may go is the column itself under its own name; every
    other read of it is named in the refusal so the reader knows what to look at.
    """

    target = column.lower()
    own = select.expressions[matched[0]] if matched else None
    if own is not None and _reads([own], target) and not _only_reads(own, target):
        raise RewriteError(
            f"{path}: '{column}' is projected from an expression that also reads "
            "other columns, so dex removed nothing: what it becomes without "
            f"'{column}' is a decision about the model. Change it by hand or with "
            "`transform plan`"
        )
    owned = (
        {id(node) for node in own.find_all(exp.Column)} if own is not None else set()
    )
    elsewhere = [
        node
        for node in parsed.find_all(exp.Column)
        if node.name.lower() == target and id(node) not in owned
    ]
    if elsewhere:
        raise RewriteError(
            f"{path}: '{column}' is read by hand-written logic in this model "
            f"({_where_read(elsewhere[0], blanked)}), so dex removed nothing: what "
            "that logic becomes without the column is a decision about the model. "
            "Change it by hand or with `transform plan`"
        )


def _reads(projections: list[exp.Expression], column: str) -> bool:
    """Whether any of ``projections`` reads ``column``, under any alias."""

    target = column.lower()
    return any(
        node.name.lower() == target
        for projection in projections
        for node in projection.find_all(exp.Column)
    )


def _only_reads(projection: exp.Expression, column: str) -> bool:
    """Whether every column ``projection`` reads is ``column``, and it reads one."""

    names = {node.name.lower() for node in projection.find_all(exp.Column)}
    return names == {column}


def _where_read(node: exp.Expression, blanked: str) -> str:
    """A short, honest label for where ``node`` sits: the projection it is part
    of, by output name, or the clause that holds it."""

    current: Any = node
    while current.parent is not None:
        parent = current.parent
        if isinstance(parent, exp.Select):
            slot = next(
                (key for key, value in parent.args.items() if value is current), None
            )
            if any(e is current for e in parent.expressions):
                name = current.alias_or_name
                return f"in the projection '{name}'" if name else "in a projection"
            return f"in a {slot.rstrip('_')} clause" if slot else "in the query"
        if isinstance(parent, (exp.Where, exp.Group, exp.Having, exp.Join)):
            return f"in a {parent.key} clause"
        current = parent
    return "in the query"


def _own_lines(content: str, start: int, end: int) -> tuple[int, int] | None:
    """The whole-line span around ``[start, end)``, when nothing else shares it.

    ``None`` when the column sits on a line with other columns, where taking the
    line would take them too. Trailing content is allowed to be a comma and a
    ``--`` comment, since both belong to the column being removed.
    """

    line_start = content.rfind("\n", 0, start) + 1
    if content[line_start:start].strip():
        return None
    line_end = content.find("\n", end)
    line_end = len(content) if line_end < 0 else line_end
    tail = content[end:line_end].strip()
    if tail.startswith(","):
        tail = tail[1:].strip()
    if tail and not tail.startswith("--"):
        return None
    return (line_start, min(line_end + 1, len(content)))


def _indent_before(content: str, offset: int) -> str:
    """The leading whitespace of the line ``offset`` sits on, or four spaces."""

    line_start = content.rfind("\n", 0, offset) + 1
    line = content[line_start:offset]
    indent = line[: len(line) - len(line.lstrip())]
    return indent or "    "


# --- YAML ---------------------------------------------------------------------


@dataclass
class YamlName:
    """One scalar in a YAML file that *names* something, with its exact span.

    ``form`` uses the same vocabulary ``references.Reference.form`` does, so a
    caller deciding which occurrences a given change touches reasons in one set
    of terms across the report and the rewrite.
    """

    name: str
    form: str
    kind: str
    span: tuple[int, int]
    owner: str | None = None
    #: Inside a versioned model's entry, the versions this name applies to, as
    #: :attr:`~..references.Reference.versions` has them (#1126).
    versions: list[str] | None = None
    #: The version whose ``versions:`` item this is written under, or ``None``
    #: at the model's top level.
    version: str | None = None


def yaml_names(
    content: str, versioned: Mapping[str, mv.VersionedModel] | None = None
) -> list[YamlName]:
    """Every structural position in one YAML file that names something.

    A structural walk, not a text scan, which is the point: a column's
    ``description`` routinely contains the column's own name and must not be
    rewritten, while a name buried in a test's arguments must be. Mirrors what
    :class:`~..references.ReferenceIndex` reads, so the two cannot drift into
    disagreeing about what a file declares.

    ``versioned`` is the project's versioned models, which places a semantic
    model's columns in the version its ``ref()`` resolves to. A model's own
    versions are read from its entry and need nothing passed.
    """

    try:
        root = yaml.compose(content)
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict) or root is None:
        return []

    found: list[YamlName] = []
    for key, node in _mapping(root):
        if key in ("models", "seeds", "snapshots"):
            for entry in _sequence(node):
                _walk_node_entry(entry, found)
        elif key == "sources":
            for entry in _sequence(node):
                _walk_source_entry(entry, found)
        elif key == "semantic_models":
            for entry in _sequence(node):
                _walk_semantic_model(entry, found, versioned or {})
        elif key == "metrics":
            for entry in _sequence(node):
                _walk_metric(entry, found)
        elif key in ("vars", "+vars"):
            for name, _value in _mapping(node, spans=True):
                found.append(
                    YamlName(
                        name=name[0], form="project_yml_var", kind="var", span=name[1]
                    )
                )
    return found


def _walk_node_entry(entry: Any, found: list[YamlName]) -> None:
    fields = dict(_mapping(entry))
    owner = _scalar_value(fields.get("name"))
    parsed = _construct(entry)
    if owner and isinstance(parsed, dict) and mv.version_items(parsed):
        found.append(
            YamlName(
                name=owner,
                form="yaml_model_entry",
                kind="model",
                span=_span(fields["name"]),
            )
        )
        _walk_versioned_entry(parsed, fields, found, owner)
        return
    if "name" in fields:
        found.append(
            YamlName(
                name=_scalar_value(fields["name"]) or "",
                form="yaml_model_entry",
                kind="model",
                span=_span(fields["name"]),
            )
        )
    # dbt 1.9 YAML snapshots name their parent through ``relation:``. Mirror
    # references._record_snapshot_relation so rename rewrites the call the same
    # way it rewrites a relationship test's ``to:``.
    if "relation" in fields:
        relation_node = fields["relation"]
        target = _relation_target(_scalar_value(relation_node))
        if target is not None:
            found.append(
                YamlName(
                    name=target,
                    form="yaml_snapshot_relation",
                    kind="source" if "." in target else "model",
                    span=_relation_span(relation_node),
                )
            )
    _walk_columns(fields, found, owner)
    for key in ("tests", "data_tests"):
        for test in _sequence(fields.get(key)):
            _walk_test(test, found, owner)


def _walk_source_entry(entry: Any, found: list[YamlName]) -> None:
    fields = dict(_mapping(entry))
    source_name = _scalar_value(fields.get("name"))
    for table in _sequence(fields.get("tables")):
        table_fields = dict(_mapping(table))
        table_name = _scalar_value(table_fields.get("name"))
        if source_name and table_name and "name" in table_fields:
            found.append(
                YamlName(
                    name=f"{source_name}.{table_name}",
                    form="definition",
                    kind="source",
                    span=_span(table_fields["name"]),
                )
            )
        owner = f"{source_name}.{table_name}" if source_name and table_name else None
        _walk_columns(table_fields, found, owner)


def _walk_columns(
    fields: dict[str, Any],
    found: list[YamlName],
    owner: str | None,
    versions: Any = None,
    version: str | None = None,
) -> None:
    for column in _sequence(fields.get("columns")):
        column_fields = dict(_mapping(column))
        name_node = column_fields.get("name")
        if name_node is None:
            continue
        name = _scalar_value(name_node) or ""
        reached = versions(name) if versions is not None else None
        found.append(
            YamlName(
                name=name,
                form="yaml_column",
                kind="column",
                span=_span(name_node),
                owner=owner,
                versions=reached,
                version=version,
            )
        )
        for key in ("tests", "data_tests"):
            for test in _sequence(column_fields.get(key)):
                _walk_test(test, found, owner, reached, version)


def _walk_versioned_entry(
    parsed: dict[str, Any], fields: dict[str, Any], found: list[YamlName], owner: str
) -> None:
    """A versioned model's columns, each placed in the versions it applies to.

    Mirrors ``ReferenceIndex._scan_versioned_entry``: a top-level column entry
    applies to every version that inherits it, a version's own entries and tests
    to that version, and a name in an ``include`` or ``exclude`` list to the
    versions the top-level entry of that name reaches (#1126).
    """

    _walk_columns(fields, found, owner, lambda c: mv.column_versions(parsed, c))
    shared = mv.versions_inheriting_tests(parsed)
    for key in ("tests", "data_tests"):
        for test in _sequence(fields.get(key)):
            _walk_test(test, found, owner, shared)
    for item in _sequence(fields.get("versions")):
        label = _version_label(owner, item)
        if label is None:
            continue
        item_fields = dict(_mapping(item))
        element = _include_element(item_fields.get("columns"))
        for key, form in (
            ("include", "yaml_version_include"),
            ("exclude", "yaml_version_exclude"),
        ):
            listed = dict(_mapping(element)).get(key) if element is not None else None
            found.extend(
                YamlName(
                    name=node.value,
                    form=form,
                    kind="column",
                    span=_span(node),
                    owner=owner,
                    versions=mv.column_versions(parsed, node.value),
                    version=label,
                )
                for node in _sequence(listed)
                if isinstance(node, yaml.ScalarNode)
            )
        _walk_columns(item_fields, found, owner, lambda _c, lb=label: [lb], label)
        for key in ("tests", "data_tests"):
            for test in _sequence(item_fields.get(key)):
                _walk_test(test, found, owner, [label], label)


def _construct(node: Any) -> Any:
    """The Python value of one composed node, as ``yaml.safe_load`` reads it."""

    if node is None:
        return None
    loader = yaml.SafeLoader("")
    try:
        return loader.construct_document(node)
    except yaml.YAMLError:
        return None
    finally:
        loader.dispose()


def _version_label(base: str, item: Any) -> str | None:
    """``base.v<v>`` for one ``versions:`` item node, or ``None``."""

    parsed = _construct(item)
    if not isinstance(parsed, dict) or "v" not in parsed:
        return None
    return f"{base}.v{mv.version_text(parsed['v'])}"


def _include_element(columns: Any) -> Any:
    """The ``include``/``exclude`` element of a version's ``columns:``."""

    return next(
        (node for node in _sequence(columns) if "include" in dict(_mapping(node))),
        None,
    )


#: Test argument keys whose value names a column. The same set
#: ``references._column_names`` reads, for the same reason: everything else a
#: generic test carries is configuration and names nothing.
_COLUMN_ARG_KEYS = ("field", "column_name", "combination_of_columns", "compare_columns")


def _walk_test(
    test: Any,
    found: list[YamlName],
    owner: str | None,
    versions: list[str] | None = None,
    version: str | None = None,
) -> None:
    if isinstance(test, yaml.ScalarNode):
        found.append(
            YamlName(
                name=test.value, form="yaml_test_ref", kind="macro", span=_span(test)
            )
        )
        return
    for name, body in _mapping(test, spans=True):
        found.append(
            YamlName(name=name[0], form="yaml_test_ref", kind="macro", span=name[1])
        )
        for key, value in _mapping(body):
            if key == "to":
                target = _relation_target(_scalar_value(value))
                if target is not None:
                    found.append(
                        YamlName(
                            name=target,
                            form="yaml_relationship_to",
                            kind="source" if "." in target else "model",
                            span=_relation_span(value),
                        )
                    )
            elif key in _COLUMN_ARG_KEYS:
                carried = (
                    [value] if isinstance(value, yaml.ScalarNode) else _sequence(value)
                )
                found.extend(
                    YamlName(
                        name=node.value,
                        form="yaml_test_column",
                        kind="column",
                        span=_span(node),
                        owner=owner,
                        versions=versions,
                        version=version,
                    )
                    for node in carried
                    if isinstance(node, yaml.ScalarNode)
                )


def _walk_semantic_model(
    entry: Any, found: list[YamlName], versioned: Mapping[str, mv.VersionedModel]
) -> None:
    fields = dict(_mapping(entry))
    model_node = fields.get("model")
    owner = None
    versions = None
    if model_node is not None:
        owner = _relation_target(_scalar_value(model_node))
        if owner is not None and "." not in owner:
            label = mv.ref_target(
                owner,
                mv.written_ref_version(_scalar_value(model_node) or ""),
                versioned,
            )
            versions = [label] if label else None
        if owner is not None:
            found.append(
                YamlName(
                    name=owner,
                    form="semantic_model_ref",
                    kind="source" if "." in owner else "model",
                    span=_relation_span(model_node),
                )
            )
    for role, kind in (
        ("entities", "entity"),
        ("dimensions", "dimension"),
        ("measures", "measure"),
    ):
        for item in _sequence(fields.get(role)):
            item_fields = dict(_mapping(item))
            if "name" in item_fields:
                found.append(
                    YamlName(
                        name=_scalar_value(item_fields["name"]) or "",
                        form="semantic_definition",
                        kind=kind,
                        span=_span(item_fields["name"]),
                    )
                )
            column = physical_column(
                {key: _scalar_value(value) for key, value in _mapping(item)}
            )
            expr_node = item_fields.get("expr") or item_fields.get("name")
            if column and expr_node is not None:
                found.append(
                    YamlName(
                        name=column,
                        form="semantic_expr",
                        kind="column",
                        span=_span(expr_node),
                        owner=owner,
                        versions=versions,
                    )
                )


def _walk_metric(entry: Any, found: list[YamlName]) -> None:
    fields = dict(_mapping(entry))
    if "name" in fields:
        found.append(
            YamlName(
                name=_scalar_value(fields["name"]) or "",
                form="definition",
                kind="metric",
                span=_span(fields["name"]),
            )
        )
    plain = yaml.safe_load(yaml.serialize(entry)) if entry is not None else None
    if not isinstance(plain, dict):
        return
    measures, metrics = metric_inputs(plain)
    wanted = dict.fromkeys(measures, "measure")
    wanted.update(dict.fromkeys(metrics, "metric"))
    for node in _all_scalars(entry):
        kind = wanted.get(node.value)
        if kind is not None:
            found.append(
                YamlName(
                    name=node.value,
                    form=f"metric_input_{kind}",
                    kind=kind,
                    span=_span(node),
                )
            )


def _relation_target(value: str | None) -> str | None:
    """A ``ref()`` / ``source()`` string read as the name it points at."""

    if not isinstance(value, str):
        return None
    text = value if "{{" in value else "{{ " + value + " }}"
    for region in jinja_regions(text)[0]:
        for call in region.calls:
            if call.callee == "ref" and call.args and call.args[-1]:
                return call.args[-1]
            if (
                call.callee == "source"
                and len(call.args) >= 2
                and None not in call.args
            ):
                return f"{call.args[-2]}.{call.args[-1]}"
    return None


def _relation_span(node: Any) -> tuple[int, int]:
    """The span of the *name* inside a ``{{ ref('x') }}`` scalar, not the whole scalar.

    So renaming a model rewrites the argument and leaves the call, the quoting
    and any surrounding text exactly as the author wrote them.
    """

    outer = _span(node)
    text = node.value if isinstance(node, yaml.ScalarNode) else ""
    body = text if "{{" in text else "{{ " + text + " }}"
    shift = outer[0] - (0 if "{{" in text else 3)
    for region in jinja_regions(body)[0]:
        for call in region.calls:
            if call.callee in ("ref", "source") and call.arg_spans:
                span = call.arg_spans[-1]
                if span is not None:
                    return (shift + span[0], shift + span[1])
    return outer


def _mapping(node: Any, *, spans: bool = False) -> list[Any]:
    """A mapping node's pairs. ``spans`` keys each pair by ``(name, span)``."""

    if not isinstance(node, yaml.MappingNode):
        return []
    pairs = []
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            continue
        pairs.append(((key.value, _span(key)) if spans else key.value, value))
    return pairs


def _sequence(node: Any) -> list[Any]:
    return list(node.value) if isinstance(node, yaml.SequenceNode) else []


def _all_scalars(node: Any) -> list[yaml.ScalarNode]:
    if isinstance(node, yaml.ScalarNode):
        return [node]
    if isinstance(node, yaml.SequenceNode):
        return [s for item in node.value for s in _all_scalars(item)]
    if isinstance(node, yaml.MappingNode):
        return [s for _key, value in node.value for s in _all_scalars(value)]
    return []


def _scalar_value(node: Any) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _span(node: Any) -> tuple[int, int]:
    """A scalar's span, narrowed past its quotes so the quoting style survives."""

    start, end = node.start_mark.index, node.end_mark.index
    return (start, end)


@dataclass
class YamlBlock:
    """A whole YAML declaration, spanning every line it occupies.

    Distinct from :class:`YamlName`, which spans only the scalar that names the
    thing. A rename edits the name; a removal has to take the block, and the
    block is what carries the tests, the description and the meta that only
    existed to describe what is being removed.

    ``span`` covers the entry's own lines including its ``- `` bullet and its
    trailing newline, so splicing it out leaves neither a stranded bullet nor a
    blank line where the entry was.
    """

    name: str
    form: str
    kind: str
    span: tuple[int, int]
    owner: str | None = None
    #: As on :class:`YamlName`: the versions a column entry of a versioned
    #: model applies to, and the version it is written under.
    versions: list[str] | None = None
    version: str | None = None


def yaml_blocks(content: str, *, versions: bool = False) -> list[YamlBlock]:
    """Every removable declaration in one YAML file, with its full span.

    The set a removal needs, and no more: a model, seed or snapshot entry, a
    column entry, a source table entry, a semantic definition, a metric, and a
    declared var. Anything else is either not removable on its own or is reached
    by removing one of these.

    ``versions`` also returns the column entries a versioned model's versions
    write under ``versions:`` (#1126), which only a caller that reads
    :attr:`YamlBlock.version` can tell from the model's top-level columns.
    """

    try:
        root = yaml.compose(content)
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []

    found: list[YamlBlock] = []
    for key, node in _mapping(root):
        if key in ("models", "seeds", "snapshots"):
            for entry in _sequence(node):
                fields = dict(_mapping(entry))
                name = _scalar_value(fields.get("name"))
                if name:
                    found.append(
                        YamlBlock(
                            name,
                            "yaml_model_entry",
                            "model",
                            _entry_span(content, entry),
                        )
                    )
                parsed = _construct(entry)
                if not (name and isinstance(parsed, dict) and mv.version_items(parsed)):
                    found.extend(_column_blocks(content, fields, name))
                    continue
                for block in _column_blocks(content, fields, name):
                    block.versions = mv.column_versions(parsed, block.name)
                    found.append(block)
                if not versions:
                    continue
                for item in _sequence(fields.get("versions")):
                    label = _version_label(name, item)
                    if label is None:
                        continue
                    for block in _column_blocks(content, dict(_mapping(item)), name):
                        block.versions = [label]
                        block.version = label
                        found.append(block)
        elif key == "sources":
            for entry in _sequence(node):
                fields = dict(_mapping(entry))
                source_name = _scalar_value(fields.get("name"))
                for table in _sequence(fields.get("tables")):
                    table_fields = dict(_mapping(table))
                    table_name = _scalar_value(table_fields.get("name"))
                    owner = (
                        f"{source_name}.{table_name}"
                        if source_name and table_name
                        else None
                    )
                    if owner:
                        found.append(
                            YamlBlock(
                                owner,
                                "definition",
                                "source",
                                _entry_span(content, table),
                            )
                        )
                    found.extend(_column_blocks(content, table_fields, owner))
        elif key == "metrics":
            for entry in _sequence(node):
                name = _scalar_value(dict(_mapping(entry)).get("name"))
                if name:
                    found.append(
                        YamlBlock(
                            name, "definition", "metric", _entry_span(content, entry)
                        )
                    )
        elif key in ("vars", "+vars"):
            for pair, value in _mapping(node, spans=True):
                found.append(
                    YamlBlock(
                        pair[0],
                        "project_yml_var",
                        "var",
                        _pair_span(content, pair[1], value),
                    )
                )
    return found


def _column_blocks(
    content: str, fields: dict[str, Any], owner: str | None
) -> list[YamlBlock]:
    blocks = []
    for column in _sequence(fields.get("columns")):
        name = _scalar_value(dict(_mapping(column)).get("name"))
        if name:
            blocks.append(
                YamlBlock(
                    name, "yaml_column", "column", _entry_span(content, column), owner
                )
            )
    return blocks


def stop_inheriting_column(
    content: str, base: str, label: str, column: str, new: str | None
) -> list[tuple[int, int, str]]:
    """Splices that stop version ``label`` inheriting the top-level ``column``.

    For a change to one version of a column every version shares through
    ``include`` (#1126): the top-level entry stays for the others, and this
    version drops it from its ``include`` list, or adds it to its ``exclude``
    list, or gains an ``include: all`` element excluding it. With ``new`` the
    version also declares its own entry, a copy of the top-level one under the
    new name, so the rename carries its description, tests and meta.

    Only block-style YAML is edited. A flow-style entry shares its lines with
    what is not being changed, so it is refused rather than rewritten.
    """

    where = f"'{label}' in the YAML entry of '{base}'"
    try:
        root = yaml.compose(content)
    except yaml.YAMLError as exc:
        raise RewriteError(f"{where}: the YAML did not parse") from exc
    entry = _model_entry(root, base)
    if entry is None:
        raise RewriteError(f"{where}: dex could not find the entry")
    fields = dict(_mapping(entry))
    item = next(
        (
            node
            for node in _sequence(fields.get("versions"))
            if _version_label(base, node) == label
        ),
        None,
    )
    top = next(
        (
            node
            for node in _sequence(fields.get("columns"))
            if _scalar_value(dict(_mapping(node)).get("name")) == column
        ),
        None,
    )
    if item is None or top is None:
        raise RewriteError(f"{where}: dex could not find the version or the column")
    for node in (entry, fields.get("versions"), item, fields.get("columns")):
        if getattr(node, "flow_style", False):
            raise RewriteError(
                f"{where} is written in flow style, and dex only splices "
                "block-style YAML. Make this edit by hand"
            )
    own = None
    if new is not None:
        copied = _construct(top)
        own = {"name": new, **{k: v for k, v in copied.items() if k != "name"}}

    edits: list[tuple[int, int, str]] = []
    item_fields = dict(_mapping(item))
    columns = item_fields.get("columns")
    if columns is None:
        line_start = content.rfind("\n", 0, item.start_mark.index) + 1
        end = before_trailing_blanks(
            content, block_end(content, line_start, indent_of(content, line_start))
        )
        block = {"columns": [{"include": "all", "exclude": [column]}]}
        if own is not None:
            block["columns"].append(own)
        edits.append((end, end, dump_items(block, item.start_mark.column)))
        return _merged(content, edits)
    if (
        not isinstance(columns, yaml.SequenceNode)
        or columns.flow_style
        or not columns.value
    ):
        raise RewriteError(
            f"{where}: its `columns:` is not a block-style list, and dex only "
            "splices block-style YAML. Make this edit by hand"
        )
    dash = _dash_indent(content, columns.value[0], where)
    element = _include_element(columns)
    if element is None:
        first = content.rfind("\n", 0, columns.value[0].start_mark.index) + 1
        edits.append(
            (first, first, dump_items([{"include": "all", "exclude": [column]}], dash))
        )
    else:
        edits.extend(_exclude_from(content, element, column, where))
    if own is not None:
        last = columns.value[-1]
        line_start = content.rfind("\n", 0, last.start_mark.index) + 1
        end = before_trailing_blanks(content, block_end(content, line_start, dash))
        edits.append((end, end, dump_items([own], dash)))
    return _merged(content, edits)


#: The reference form of a name in a version's ``include`` or ``exclude`` list.
_LIST_FORMS = {"include": "yaml_version_include", "exclude": "yaml_version_exclude"}


def version_list_removals(
    content: str, base: str, column: str
) -> list[tuple[str, tuple[int, int, str]]]:
    """``(form, splice)`` taking ``column`` out of every version's ``include``
    and ``exclude`` lists, for a removal of the top-level entry they name."""

    try:
        root = yaml.compose(content)
    except yaml.YAMLError:
        return []
    entry = _model_entry(root, base)
    if entry is None:
        return []
    out = []
    for item in _sequence(dict(_mapping(entry)).get("versions")):
        element = _include_element(dict(_mapping(item)).get("columns"))
        if element is None:
            continue
        for key, value in element.value:
            if _scalar_value(key) not in _LIST_FORMS or not isinstance(
                value, yaml.SequenceNode
            ):
                continue
            target = next((n for n in value.value if _scalar_value(n) == column), None)
            if target is not None:
                out.append(
                    (
                        _LIST_FORMS[key.value],
                        _list_item_removal(content, key, value, target, base),
                    )
                )
    return out


def _model_entry(root: Any, base: str) -> Any:
    for key, node in _mapping(root):
        if key != "models":
            continue
        for entry in _sequence(node):
            if _scalar_value(dict(_mapping(entry)).get("name")) == base:
                return entry
    return None


def _dash_indent(content: str, item: Any, where: str) -> int:
    """The column of the dash that opens a block-sequence item."""

    span = sequence_item_span(content, item.start_mark.index)
    if span is None:
        raise RewriteError(
            f"{where}: a list item shares its line with another, and dex only "
            "splices one item per line. Make this edit by hand"
        )
    return indent_of(content, span[0])


def _exclude_from(
    content: str, element: Any, column: str, where: str
) -> list[tuple[int, int, str]]:
    """Splices that make one ``include``/``exclude`` element drop ``column``."""

    if element.flow_style:
        raise RewriteError(
            f"{where}: its `include` element is written in flow style, and dex "
            "only splices block-style YAML. Make this edit by hand"
        )
    pairs = {_scalar_value(key): (key, value) for key, value in element.value}
    include_key, include = pairs["include"]
    if isinstance(include, yaml.SequenceNode):
        target = next((n for n in include.value if _scalar_value(n) == column), None)
        if target is None:
            return []
        return [_list_item_removal(content, include_key, include, target, where)]
    if "exclude" not in pairs:
        line_start = content.rfind("\n", 0, element.start_mark.index) + 1
        end = before_trailing_blanks(
            content, block_end(content, line_start, indent_of(content, line_start))
        )
        return [
            (end, end, dump_items({"exclude": [column]}, element.start_mark.column))
        ]
    _key, exclude = pairs["exclude"]
    if not isinstance(exclude, yaml.SequenceNode):
        raise RewriteError(
            f"{where}: its `exclude` is not a list. Make this edit by hand"
        )
    if exclude.flow_style:
        close = exclude.end_mark.index - 1
        if content[close] != "]":
            raise RewriteError(f"{where}: dex could not read its `exclude` list")
        return [(close, close, (", " if exclude.value else "") + column)]
    last = exclude.value[-1]
    line_start = content.rfind("\n", 0, last.start_mark.index) + 1
    indent = indent_of(content, line_start)
    end = before_trailing_blanks(content, block_end(content, line_start, indent))
    return [(end, end, dump_items([column], indent))]


def _list_item_removal(
    content: str, key: Any, sequence: Any, item: Any, where: str
) -> tuple[int, int, str]:
    """The splice taking one scalar out of a list, leaving ``[]`` for the last."""

    kept = [node for node in sequence.value if node is not item]
    if sequence.flow_style:
        text = ", ".join(
            content[node.start_mark.index : node.end_mark.index] for node in kept
        )
        return (sequence.start_mark.index, sequence.end_mark.index, f"[{text}]")
    span = sequence_item_span(content, item.start_mark.index)
    if span is None:
        raise RewriteError(
            f"{where}: a list item shares its line with another, and dex only "
            "splices one item per line. Make this edit by hand"
        )
    end = before_trailing_blanks(content, span[1])
    if kept:
        return (span[0], end, "")
    newline = "\n" if content[end - 1 : end] == "\n" else ""
    return (key.end_mark.index, end, ": []" + newline)


def _merged(
    content: str, edits: list[tuple[int, int, str]]
) -> list[tuple[int, int, str]]:
    """Insertions at one offset folded into one, in the order they were made.

    ``splice`` applies equal offsets right to left, which would put the second
    insertion ahead of the first. An inserted line is given the line break it
    needs where it lands at the end of a file that has none.
    """

    out: list[tuple[int, int, str]] = []
    inserted: dict[int, int] = {}
    for start, end, text in edits:
        if start != end:
            out.append((start, end, text))
            continue
        if start in inserted:
            position = inserted[start]
            out[position] = (start, end, out[position][2] + text)
            continue
        if text.endswith("\n") and start and content[start - 1] != "\n":
            text = "\n" + text
        inserted[start] = len(out)
        out.append((start, end, text))
    return out


def column_anchor(content: str, model: str) -> tuple[int, str] | None:
    """Where a new column entry goes under ``model``, and the indent to write it at.

    The indent is read from the entries already there rather than assumed, because
    this runs against files dex did not write. A model declaring no columns has no
    precedent to read and no contract to keep current, so it answers ``None``
    rather than inventing one.

    The offset is backed up over trailing blank lines: :func:`block_end` walks
    past them to find the next line with content, so the raw span end sits *after*
    the separator a human put before the next key, and an insertion there lands the
    new column on the far side of the gap.
    """

    blocks = [
        block
        for block in yaml_blocks(content)
        if block.form == "yaml_column" and block.owner == model
    ]
    if not blocks:
        return None
    last = max(blocks, key=lambda block: block.span[1])
    line_start = content.rfind("\n", 0, last.span[0]) + 1
    indent = content[line_start:][
        : len(content[line_start:]) - len(content[line_start:].lstrip(" "))
    ]
    return (before_trailing_blanks(content, last.span[1]), indent)


@dataclass
class ColumnTests:
    """One column's declared tests, and the spans needed to change the list.

    ``spans`` carries only the *scalar* items. A test written as a mapping
    (``- accepted_values: {...}``) is counted in ``values`` under its single key
    and carries no span, so it is never rewritten and never removed: dex changes
    the tests it can name and leaves configured ones alone.
    """

    key: str | None
    values: list[str]
    spans: dict[str, tuple[int, int]]
    key_span: tuple[int, int] | None
    value_span: tuple[int, int] | None
    items: list[tuple[int, int]]
    entry_span: tuple[int, int]
    bullet_indent: str
    flow: bool


def column_tests(content: str, model: str, column: str) -> ColumnTests | None:
    """One column's test list, resolved inside that column's own bytes.

    Scoped through :func:`yaml_blocks` first, which bounds the entry by
    indentation. That is what keeps two columns of the same model that both carry
    ``not_null`` from being confused for each other: the search never leaves the
    one column's span, so the ambiguity cannot arise rather than being resolved.
    """

    try:
        root = yaml.compose(content)
    except yaml.YAMLError:
        return None
    entry_span = next(
        (
            block.span
            for block in yaml_blocks(content)
            if block.form == "yaml_column"
            and block.owner == model
            and block.name == column
        ),
        None,
    )
    if entry_span is None:
        return None
    line = content[entry_span[0] :]
    bullet_indent = line[: len(line) - len(line.lstrip(" "))]

    node = _column_node(root, model, column)
    for key in ("tests", "data_tests"):
        listed = dict(_mapping(node, spans=True)) if node is not None else {}
        found = next((v for k, v in listed.items() if k[0] == key), None)
        if found is None:
            continue
        key_span = next(k[1] for k in listed if k[0] == key)
        values: list[str] = []
        spans: dict[str, tuple[int, int]] = {}
        items: list[tuple[int, int]] = []
        for item in _sequence(found):
            if isinstance(item, yaml.ScalarNode):
                values.append(item.value)
                spans[item.value] = _span(item)
                items.append(_span(item))
            else:
                named = [name for name, _body in _mapping(item)]
                values.append(named[0] if named else "")
                items.append((item.start_mark.index, item.end_mark.index))
        return ColumnTests(
            key=key,
            values=values,
            spans=spans,
            key_span=key_span,
            value_span=(found.start_mark.index, found.end_mark.index),
            items=items,
            entry_span=entry_span,
            bullet_indent=bullet_indent,
            flow=bool(getattr(found, "flow_style", False)),
        )
    return ColumnTests(
        key=None,
        values=[],
        spans={},
        key_span=None,
        value_span=None,
        items=[],
        entry_span=entry_span,
        bullet_indent=bullet_indent,
        flow=False,
    )


def prevailing_test_key(content: str) -> str:
    """Whether this file spells its tests ``tests`` or ``data_tests``.

    dbt accepts both and a file that already chose one should not gain the other.
    Decided by which spelling more entries use. With no precedent, or a tie,
    ``data_tests``: it is dbt's current key, ``tests`` is a silent alias for it,
    and it is what dex's own scaffold writes. Only a column with no test list
    takes this answer; a list that exists keeps its own key.
    """

    counts = {
        key: len(re.findall(rf"(?<![\w-]){key}:", content))
        for key in ("tests", "data_tests")
    }
    return "tests" if counts["tests"] > counts["data_tests"] else "data_tests"


def column_tests_span(
    content: str, current: ColumnTests, wanted: list[str], *, key: str
) -> tuple[int, int, str] | None:
    """One replacement span that makes ``current`` say ``wanted``.

    Surgical rather than a re-render, so a mapping test sitting in the same list
    survives untouched and a flow list stays on its line. ``None`` when the file
    already says ``wanted``, which is what keeps a no-op out of the plan.
    """

    if current.values == wanted:
        return None
    added = [name for name in wanted if name not in current.values]
    removed = [name for name in current.values if name not in wanted]
    if len(added) + len(removed) != 1:
        raise RewriteError(
            "a test list changes one name at a time; dex would have produced a "
            "file it could not predict, so nothing was changed"
        )

    if removed:
        name = removed[0]
        span = current.spans.get(name)
        if span is None or current.key_span is None or current.value_span is None:
            return None
        if len(current.values) == 1:
            # The whole key goes rather than being left as an empty list, which
            # declares nothing and reads as a test someone deleted the body of.
            start, end = _pair_span(content, current.key_span, None)
            return (start, before_trailing_blanks(content, end), "")
        if current.flow:
            index = current.items.index(span)
            if index > 0:
                return (current.items[index - 1][1], span[1], "")
            return (span[0], current.items[index + 1][0], "")
        start = content.rfind("\n", 0, span[0]) + 1
        end = block_end(content, start, indent_of(content, start))
        return (start, before_trailing_blanks(content, end), "")

    name = added[0]
    if current.key is None:
        field_indent = current.bullet_indent + "  "
        at = before_trailing_blanks(content, current.entry_span[1])
        return (at, at, f"{field_indent}{key}: [{name}]\n")
    if current.value_span is None:
        return None
    if current.flow:
        end = current.value_span[1]
        if content[end - 1] != "]":
            raise RewriteError(
                f"the test list for a column is not closed at {end}; dex would "
                "have produced a file it could not predict, so nothing was changed"
            )
        return (end - 1, end - 1, f", {name}" if current.values else name)
    last = max(current.items, key=lambda span: span[1])
    start = content.rfind("\n", 0, last[0]) + 1
    item_indent = content[start : start + indent_of(content, start)]
    at = block_end(content, start, indent_of(content, start))
    return (at, at, f"{item_indent}- {name}\n")


def _column_node(root: Any, model: str, column: str) -> Any:
    for key, node in _mapping(root):
        if key not in ("models", "seeds", "snapshots"):
            continue
        for entry in _sequence(node):
            fields = dict(_mapping(entry))
            if _scalar_value(fields.get("name")) != model:
                continue
            for candidate in _sequence(fields.get("columns")):
                candidate_fields = dict(_mapping(candidate))
                if _scalar_value(candidate_fields.get("name")) == column:
                    return candidate
    return None


def _entry_span(content: str, node: Any) -> tuple[int, int]:
    """A block entry's span: its own lines, and none of its sibling's.

    Measured by indentation rather than from the node's marks, because PyYAML's
    ``end_mark`` on a block collection is where the parser stopped reading, which
    is inside the *next* entry. Trusting it makes removing one column take the
    column below it with it, silently, in a file the reviewer sees as one
    deletion.

    The rule is YAML's own: an entry owns every following line indented deeper
    than the line it starts on, and ends at the first line that is not.
    """

    start = node.start_mark.index
    line_start = content.rfind("\n", 0, start) + 1
    indent = len(content[line_start:]) - len(content[line_start:].lstrip(" "))
    return (line_start, block_end(content, line_start, indent))


def _pair_span(content: str, key_span: tuple[int, int], _value: Any) -> tuple[int, int]:
    """A mapping entry's span, by the same indentation rule as a sequence item."""

    line_start = content.rfind("\n", 0, key_span[0]) + 1
    indent = len(content[line_start:]) - len(content[line_start:].lstrip(" "))
    return (line_start, block_end(content, line_start, indent))


def narrow_quotes(content: str, span: tuple[int, int]) -> tuple[int, int]:
    """``span`` without its surrounding quote characters, if it has any."""

    start, end = span
    if (
        end - start >= 2
        and content[start] == content[end - 1]
        and content[start] in "'\""
    ):
        return (start + 1, end - 1)
    return span


# --- Jinja --------------------------------------------------------------------


@dataclass
class JinjaName:
    """One name written inside a jinja call, with the span of that name alone."""

    name: str
    form: str
    kind: str
    span: tuple[int, int]
    node: str | None = None


#: dbt's own callees, which name nothing a project can rename. `config()`
#: configures the node it sits in; the other three are read as their own kinds.
_BUILTINS = frozenset({"ref", "source", "var", "config"})

#: Statement keywords that *define* rather than call. Renaming a macro has to
#: rewrite its definition too, so these are reported, distinguished by form.
_DEFINING = ("macro", "test", "materialization")


def jinja_names(content: str, node: str | None = None) -> list[JinjaName]:
    """Every name a jinja call in ``content`` refers to, with its exact span.

    Argument spans cover what is inside the quotes, so a rewrite changes the name
    and leaves the author's quoting alone. A call whose argument dex did not
    resolve contributes nothing here: there is no name to point at, and the
    reference index is what reports its existence.
    """

    regions, _masked = jinja_regions(content)
    found: list[JinjaName] = []
    for region in regions:
        keyword = region.body.strip().lstrip("-").strip().split("(")[0].split()
        defining = region.kind == "statement" and keyword and keyword[0] in _DEFINING
        for index, call in enumerate(region.calls):
            if call.callee == "config" or call.callee_span is None:
                continue
            if defining and index == 0:
                if keyword[0] in ("macro", "test", "materialization"):
                    found.append(
                        JinjaName(
                            name=call.callee,
                            form="definition",
                            kind="macro",
                            span=call.callee_span,
                            node=node,
                        )
                    )
                continue
            found.extend(_call_names(call, node))
    return found


def _call_names(call: Any, node: str | None) -> list[JinjaName]:
    if call.callee == "ref":
        span = call.arg_spans[-1] if call.arg_spans else None
        if span is None or not call.args or call.args[-1] is None:
            return []
        return [JinjaName(call.args[-1], "ref_call", "model", span, node)]
    if call.callee == "source":
        if len(call.args) < 2 or None in call.args[-2:]:
            return []
        source_span, table_span = call.arg_spans[-2], call.arg_spans[-1]
        if source_span is None or table_span is None:
            return []
        name = f"{call.args[-2]}.{call.args[-1]}"
        return [
            JinjaName(name, "source_call_namespace", "source", source_span, node),
            JinjaName(name, "source_call", "source", table_span, node),
        ]
    if call.callee == "var":
        span = call.arg_spans[0] if call.arg_spans else None
        if span is None or not call.args or call.args[0] is None:
            return []
        return [JinjaName(call.args[0], "var_call", "var", span, node)]
    if call.callee in _BUILTINS:
        return []
    return [JinjaName(call.callee, "macro_call", "macro", call.callee_span, node)]
