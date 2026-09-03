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

from dataclasses import dataclass
from typing import Any

import sqlglot
import yaml
from sqlglot import expressions as exp

from ..dbt_project import jinja_regions, metric_inputs, physical_column
from ..errors import DexError
from ..references import blank_jinja


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


def output_columns(content: str, path: str) -> set[str] | None:
    """The lowercased output column names, or ``None`` where a star hides them.

    ``None`` is *unknown*, never *empty*. A caller proving an ancestor has the
    inputs a derivation needs cannot treat a star as proof of anything.
    """

    _blanked, _parsed, select = _parse_model(content, path)
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
    content: str, path: str, expression: str, alias: str
) -> SqlRewrite:
    """Add one projection to a model's outermost SELECT.

    Inserted before the top-level ``FROM``, at the indentation the last existing
    projection uses, so a threaded column lands where a human would have put it.

    A model projecting a star is returned untouched: it already carries the
    column, and adding it explicitly would shadow the star's copy with a second
    one of the same name.
    """

    blanked, _parsed, select = _parse_model(content, path)
    if _projects_star(select):
        return SqlRewrite(content=content, changed=0, star=True)
    if alias.lower() in (output_columns(content, path) or set()):
        return SqlRewrite(content=content, changed=0)
    _refuse_ungrouped(select, path, expression, alias)

    insert_at = _projection_end(blanked, path)
    projection = expression if expression == alias else f"{expression} as {alias}"
    indent = _indent_before(content, insert_at)
    rewritten = splice(content, [(insert_at, insert_at, f",\n{indent}{projection}")])

    now = output_columns(rewritten, path)
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


def _projection_spans(blanked: str, path: str) -> list[tuple[int, int]]:
    """The span of each projection in the outermost SELECT, in source order.

    Read off the token stream rather than the parse tree because a projection is
    not always an identifier: ``1 as n`` and ``count(*)`` carry no span the tree
    would report, and removing one means removing all of its text, not the part
    that happens to be a name.

    Depth tracking is what keeps a CTE's own ``SELECT``/``FROM`` and every
    subquery's out of this: only the top-level pair bounds the list, and only a
    top-level comma separates it. Positionally aligned with
    ``select.expressions``, since both are in source order.
    """

    try:
        tokens = sqlglot.tokenize(blanked)
    except Exception as exc:
        raise RewriteError(f"{path}: dex could not read this model's SQL") from exc

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


def _projection_end(blanked: str, path: str) -> int:
    """The offset just past the last projection of the outermost SELECT."""

    spans = _projection_spans(blanked, path)
    if not spans:
        raise RewriteError(
            f"{path}: this model projects no columns, so dex has nothing to add to"
        )
    return spans[-1][1]


def unproject_column_in_sql(content: str, path: str, column: str) -> SqlRewrite:
    """Remove one projection from a model's outermost SELECT.

    The comma that separated it goes with it, chosen from whichever side exists
    so removing the first or the last projection leaves valid SQL either way.

    A model projecting a star is left alone and says so: the column is not named
    there, and dex will not rewrite a star into an explicit list to exclude one
    column, because that would freeze every other column at today's shape.
    """

    blanked, _parsed, select = _parse_model(content, path)
    if _projects_star(select):
        return SqlRewrite(content=content, changed=0, star=True)

    spans = _projection_spans(blanked, path)
    if len(spans) != len(select.expressions):
        raise RewriteError(
            f"{path}: dex read {len(spans)} column(s) in this model where its "
            f"parser read {len(select.expressions)}, so it will not edit the "
            "list. Make this edit by hand"
        )
    matched = [
        index
        for index, projection in enumerate(select.expressions)
        if (projection.alias_or_name or "").lower() == column.lower()
    ]
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

    now = output_columns(rewritten, path)
    if now is None or column.lower() in now:
        raise RewriteError(
            f"{path}: removing '{column}' left it still projected, so nothing "
            "was changed. This is a defect in dex; make this edit by hand and "
            "please report it"
        )
    return SqlRewrite(content=rewritten, changed=1)


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


def yaml_names(content: str) -> list[YamlName]:
    """Every structural position in one YAML file that names something.

    A structural walk, not a text scan, which is the point: a column's
    ``description`` routinely contains the column's own name and must not be
    rewritten, while a name buried in a test's arguments must be. Mirrors what
    :class:`~..references.ReferenceIndex` reads, so the two cannot drift into
    disagreeing about what a file declares.
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
                _walk_semantic_model(entry, found)
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
    if "name" in fields:
        found.append(
            YamlName(
                name=_scalar_value(fields["name"]) or "",
                form="yaml_model_entry",
                kind="model",
                span=_span(fields["name"]),
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
    fields: dict[str, Any], found: list[YamlName], owner: str | None
) -> None:
    for column in _sequence(fields.get("columns")):
        column_fields = dict(_mapping(column))
        name_node = column_fields.get("name")
        if name_node is None:
            continue
        found.append(
            YamlName(
                name=_scalar_value(name_node) or "",
                form="yaml_column",
                kind="column",
                span=_span(name_node),
                owner=owner,
            )
        )
        for key in ("tests", "data_tests"):
            for test in _sequence(column_fields.get(key)):
                _walk_test(test, found, owner)


#: Test argument keys whose value names a column. The same set
#: ``references._column_names`` reads, for the same reason: everything else a
#: generic test carries is configuration and names nothing.
_COLUMN_ARG_KEYS = ("field", "column_name", "combination_of_columns", "compare_columns")


def _walk_test(test: Any, found: list[YamlName], owner: str | None) -> None:
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
                    )
                    for node in carried
                    if isinstance(node, yaml.ScalarNode)
                )


def _walk_semantic_model(entry: Any, found: list[YamlName]) -> None:
    fields = dict(_mapping(entry))
    model_node = fields.get("model")
    owner = None
    if model_node is not None:
        owner = _relation_target(_scalar_value(model_node))
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


def yaml_blocks(content: str) -> list[YamlBlock]:
    """Every removable declaration in one YAML file, with its full span.

    The set a removal needs, and no more: a model, seed or snapshot entry, a
    column entry, a source table entry, a semantic definition, a metric, and a
    declared var. Anything else is either not removable on its own or is reached
    by removing one of these.
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
                found.extend(_column_blocks(content, fields, name))
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
    return (line_start, _block_end(content, line_start, indent))


def _pair_span(content: str, key_span: tuple[int, int], _value: Any) -> tuple[int, int]:
    """A mapping entry's span, by the same indentation rule as a sequence item."""

    line_start = content.rfind("\n", 0, key_span[0]) + 1
    indent = len(content[line_start:]) - len(content[line_start:].lstrip(" "))
    return (line_start, _block_end(content, line_start, indent))


def _block_end(content: str, line_start: int, indent: int) -> int:
    """Where the block starting at ``line_start`` with ``indent`` stops.

    The first following line that carries content at ``indent`` or shallower.
    Blank lines belong to whatever comes next, so a removal does not eat the
    separator a human put between two entries.
    """

    position = content.find("\n", line_start)
    while position >= 0:
        line_begin = position + 1
        if line_begin >= len(content):
            return len(content)
        line_end = content.find("\n", line_begin)
        line = content[line_begin : len(content) if line_end < 0 else line_end]
        if line.strip():
            depth = len(line) - len(line.lstrip(" "))
            if depth <= indent:
                return line_begin
        position = line_end
    return len(content)


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
