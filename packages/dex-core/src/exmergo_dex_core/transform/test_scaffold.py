"""Scaffold a dbt unit test from a model's own inputs (issue #215).

Writing a unit test by hand means restating every input's column set with
correctly typed values before the assertion that is the actual point of the
test even starts. That restatement is mechanical: the model's ``ref()``/
``source()`` calls name the inputs, the model's own SQL names which of each
input's columns it reads, and the exploration cache already knows their
types. This module derives all three and emits a ``unit_tests:`` skeleton
with a ``given`` block per input holding only the columns read.

Two decisions the issue calls out explicitly, upheld everywhere below:
never invent the expected output (the stub ``expect:`` is empty, deliberately,
and fails until a human fills it in), and never restate every column of an
input, only the ones the model actually reads. A third follows from the
codebase's own posture (:mod:`.row_attribution`): where a column's read cannot
be resolved statically, or its type is not already known, this refuses with a
clear reason rather than guessing.
"""

from __future__ import annotations

from typing import NamedTuple

import sqlglot
from sqlglot import exp

from ..cache import DexCache, match_identifier
from ..dbt_project import REF_PATTERN, SOURCE_PATTERN, DbtProjectView
from ..errors import DexError
from .plans import EditKind, PlanEdit
from .row_attribution import UnattributableError, naming_resolver, render_model_sql


class TestScaffoldError(DexError):
    """The model's inputs, reads, or their types could not be resolved with
    enough certainty to scaffold a unit test. Always names what and why;
    never falls back to a guess."""


class ModelInput(NamedTuple):
    """One ``ref()``/``source()`` call found in a model, in first-appearance
    order. ``name`` is the bare table/model name (the join key into the
    exploration cache and the column-read map); ``literal`` is the exact
    dbt YAML this input is written back as."""

    name: str
    literal: str


def unit_test_scaffold_edits(
    view: DbtProjectView, cache: DexCache | None, model_name: str
) -> tuple[list[PlanEdit], list[str]]:
    """The plan edit that scaffolds a ``unit_tests:`` skeleton for ``model_name``.

    Returns ``(edits, input_names)``: a single ``SCHEMA_YML`` edit (a new
    sibling file, never merged into an existing hand-written schema.yml, the
    same merge-free posture :mod:`.scaffold` takes) and the bare names of the
    inputs it scaffolded a ``given`` block for, in the order they appear in
    the model.
    """

    path = _find_model_path(view, model_name)
    sql = view.files[path].content

    inputs = _find_inputs(sql)
    if not inputs:
        raise TestScaffoldError(
            f"'{model_name}' has no ref()/source() inputs; there is nothing "
            "to scaffold a fixture from"
        )

    try:
        rendered = render_model_sql(sql, naming_resolver)
    except UnattributableError as exc:
        raise TestScaffoldError(
            f"'{model_name}': {exc}; a unit test needs a statically readable SELECT"
        ) from exc

    try:
        parsed = sqlglot.parse_one(rendered, read="duckdb")
    except Exception as exc:
        raise TestScaffoldError(
            f"'{model_name}': the rendered SQL could not be parsed ({exc})"
        ) from exc

    inputs_by_lower = {i.name.lower(): i.name for i in inputs}
    reads: dict[str, set[str]] = {}
    _resolve_reads(parsed, {}, inputs_by_lower, reads, cache, outermost=True)

    unread = [i.name for i in inputs if not reads.get(i.name)]
    if unread:
        raise TestScaffoldError(
            f"'{model_name}': no column of input(s) {', '.join(unread)} could "
            "be resolved as read; a fixture needs at least one column per input"
        )

    types = {i.name: _column_types(cache, i, reads[i.name]) for i in inputs}

    dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
    yaml_path = (
        f"{dir_path}/test_{model_name}.yml" if dir_path else f"test_{model_name}.yml"
    )
    edit = PlanEdit(
        path=yaml_path,
        kind=EditKind.SCHEMA_YML,
        new_content=_unit_test_yaml(model_name, inputs, reads, types),
    )
    return [edit], [i.name for i in inputs]


# --- model + input discovery ---------------------------------------------------


def _find_model_path(view: DbtProjectView, model_name: str) -> str:
    prefixes = tuple(f"{mp}/" for mp in view.model_paths)
    matches = sorted(
        path
        for path in view.files
        if path.endswith(".sql")
        and path.startswith(prefixes)
        and path.rsplit("/", 1)[-1][: -len(".sql")] == model_name
    )
    if not matches:
        raise TestScaffoldError(
            f"no model named '{model_name}' found under {', '.join(view.model_paths)}"
        )
    if len(matches) > 1:
        raise TestScaffoldError(f"'{model_name}' is ambiguous: {', '.join(matches)}")
    return matches[0]


def _find_inputs(sql: str) -> list[ModelInput]:
    """Every ``ref()``/``source()`` call in ``sql``, deduplicated, in the
    order each is first written. A plain regex scan (the same patterns
    :mod:`..dbt_project` uses elsewhere) rather than a jinja render: it needs
    no resolution, only the call's own literal text, which the render step
    that follows would otherwise discard once it settles on a bare name.
    """

    found: list[tuple[int, ModelInput]] = [
        (m.start(), ModelInput(m.group(1), f"ref('{m.group(1)}')"))
        for m in REF_PATTERN.finditer(sql)
    ] + [
        (m.start(), ModelInput(m.group(2), f"source('{m.group(1)}', '{m.group(2)}')"))
        for m in SOURCE_PATTERN.finditer(sql)
    ]
    found.sort(key=lambda pair: pair[0])

    inputs: list[ModelInput] = []
    seen: set[str] = set()
    for _, model_input in found:
        if model_input.name.lower() not in seen:
            seen.add(model_input.name.lower())
            inputs.append(model_input)
    return inputs


# --- column-read resolution ------------------------------------------------------


# A resolved FROM/JOIN source that *is* one of the model's own ref()/source()
# inputs, unmodified: any column read through this alias is a direct read of
# that input's same-named column.
class _BaseInput(NamedTuple):
    name: str


# A resolved CTE/subquery: its own output columns, each either a direct
# passthrough of an input's column (traced through as many CTEs as it takes)
# or None (computed, aggregated, or otherwise not a single column's identity).
_Derived = dict


def _resolve_reads(
    node: exp.Expression,
    env: dict[str, object],
    inputs_by_lower: dict[str, str],
    reads: dict[str, set[str]],
    cache: DexCache | None,
    *,
    outermost: bool,
) -> _Derived:
    """Walk one SELECT (and everything it CTEs or joins) and fold every column
    it reads into ``reads``, keyed by the original input each one traces back
    to. Returns this SELECT's own output columns as a passthrough map, so an
    enclosing CTE can keep tracing through it.

    Column references are found by walking every source a SELECT actually
    names: its CTEs, and whatever it FROMs or JOINs. A column read only
    inside a WHERE/HAVING subquery is not one of those and is not traced;
    that shape is rare enough in practice, and the failure mode is silence
    (a column goes untested) rather than a wrong fixture, which the
    unqualified-ambiguous and unsupported-shape refusals below guard against
    everywhere they can be detected instead.

    A bare ``select *``/qualified ``t.*`` is expanded against the cache
    (the real column list of a base input, or a CTE's own already-known
    output columns) rather than refused, since the cache makes it resolvable
    (the common staging shape is exactly ``select * from {{ source(...) }}``).
    Expanding it only counts as a *read* at the outermost SELECT, where the
    model's own output really does depend on every one of those columns; a
    ``*`` inside a CTE only widens what a reference through that CTE could
    resolve to, and an enclosing scope that never reaches for a given column
    never marks it read.
    """

    while isinstance(node, exp.Subquery):
        node = node.this
    if not isinstance(node, exp.Select):
        raise TestScaffoldError(f"unsupported query shape: {type(node).__name__}")

    local_env = dict(env)
    for cte in node.ctes:
        local_env[cte.alias_or_name.lower()] = _resolve_reads(
            cte.this, local_env, inputs_by_lower, reads, cache, outermost=False
        )

    sources: dict[str, object] = {}
    for source_node in _from_and_join_nodes(node):
        alias = (source_node.alias_or_name or "").lower()
        if not alias:
            raise TestScaffoldError(
                "every FROM/JOIN source needs a name or alias to scaffold a unit test"
            )
        if isinstance(source_node, exp.Table):
            table_name = source_node.name
            if table_name.lower() in local_env:
                sources[alias] = local_env[table_name.lower()]
            elif table_name.lower() in inputs_by_lower:
                sources[alias] = _BaseInput(inputs_by_lower[table_name.lower()])
            else:
                raise TestScaffoldError(
                    f"'{table_name}' is neither a ref()/source() input nor a "
                    "CTE in this model"
                )
        elif isinstance(source_node, exp.Subquery):
            sources[alias] = _resolve_reads(
                source_node, local_env, inputs_by_lower, reads, cache, outermost=False
            )
        else:
            raise TestScaffoldError(
                f"unsupported FROM/JOIN source: {type(source_node).__name__}"
            )

    def resolve(col: exp.Column) -> tuple[str, str] | None:
        alias = col.table.lower() if col.table else None
        if alias:
            if alias not in sources:
                raise TestScaffoldError(f"unknown table or alias '{col.table}'")
            source = sources[alias]
        elif len(sources) == 1:
            source = next(iter(sources.values()))
        else:
            raise TestScaffoldError(
                f"column '{col.name}' is unqualified and ambiguous across "
                f"{len(sources)} joined sources; qualify it"
            )
        if isinstance(source, _BaseInput):
            reads.setdefault(source.name, set()).add(col.name)
            return (source.name, col.name)
        provenance = source.get(col.name.lower())
        if provenance is not None:
            reads.setdefault(provenance[0], set()).add(provenance[1])
        return provenance

    for col in node.find_all(exp.Column):
        # A bare/qualified star is a projection-level construct (`select *`,
        # `select t.*`), never a scalar reference, so it is expanded below
        # with the other projections instead of resolved column-by-column.
        if isinstance(col.this, exp.Star):
            continue
        if col.find_ancestor(exp.Select) is node:
            resolve(col)

    output: _Derived = {}
    for projection in node.expressions:
        if isinstance(projection, exp.Star):
            expanded = _expand_all(sources, cache)
        elif isinstance(projection, exp.Column) and isinstance(
            projection.this, exp.Star
        ):
            alias = projection.table.lower()
            if alias not in sources:
                raise TestScaffoldError(f"unknown table or alias '{projection.table}'")
            expanded = _expand_source(sources[alias], cache)
        else:
            name = (projection.alias_or_name or "").lower()
            if not name:
                raise TestScaffoldError("a projection has no resolvable output name")
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            output[name] = resolve(inner) if isinstance(inner, exp.Column) else None
            continue
        output.update(expanded)
        if outermost:
            for input_name, column_name in filter(None, expanded.values()):
                reads.setdefault(input_name, set()).add(column_name)
    return output


def _expand_source(source: object, cache: DexCache | None) -> _Derived:
    if isinstance(source, _BaseInput):
        dataset = _lookup_dataset(cache, source.name, source.name)
        return {c.name.lower(): (source.name, c.name) for c in dataset.columns}
    return dict(source)  # a CTE/subquery: already a name -> provenance map


def _expand_all(sources: dict[str, object], cache: DexCache | None) -> _Derived:
    expanded: _Derived = {}
    for source in sources.values():
        expanded.update(_expand_source(source, cache))
    return expanded


def _from_and_join_nodes(select: exp.Select) -> list[exp.Expression]:
    nodes: list[exp.Expression] = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        nodes.append(from_clause.this)
    nodes.extend(join.this for join in select.args.get("joins") or [])
    return nodes


# --- typing, from the exploration cache only ------------------------------------


def _lookup_dataset(cache: DexCache | None, name: str, label: str) -> object:
    known = [d.identifier for d in cache.datasets] if cache is not None else []
    matches = match_identifier(name, known)
    if not matches:
        raise TestScaffoldError(
            f"'{label}' is not in the exploration cache; run `explore map` "
            f"(or `explore profile {name}`) first so its columns are known"
        )
    if len(matches) > 1:
        raise TestScaffoldError(
            f"'{name}' is ambiguous in the exploration cache "
            f"({', '.join(matches)}); qualify it"
        )
    return next(d for d in cache.datasets if d.identifier == matches[0])


def _column_types(
    cache: DexCache | None, model_input: ModelInput, columns: set[str]
) -> dict[str, str]:
    dataset = _lookup_dataset(cache, model_input.name, model_input.literal)
    by_name = {c.name.lower(): c for c in dataset.columns}

    types: dict[str, str] = {}
    missing: list[str] = []
    for column in sorted(columns):
        profile = by_name.get(column.lower())
        if profile is None or not profile.data_type:
            missing.append(column)
        else:
            types[column] = profile.data_type
    if missing:
        raise TestScaffoldError(
            f"'{dataset.identifier}' has no cached type for column(s) "
            f"{', '.join(missing)} that '{model_input.name}' reads; the cache "
            "may be stale, re-run `explore map`"
        )
    return types


# --- YAML rendering --------------------------------------------------------------


def _unit_test_yaml(
    model_name: str,
    inputs: list[ModelInput],
    reads: dict[str, set[str]],
    types: dict[str, dict[str, str]],
) -> str:
    lines = [
        "version: 2",
        "",
        "unit_tests:",
        f"  - name: test_{model_name}",
        f"    model: {model_name}",
        "    given:",
    ]
    for model_input in inputs:
        lines.append(f"      - input: {model_input.literal}")
        lines.append("        rows:")
        row = {
            column: _example_value(types[model_input.name][column])
            for column in sorted(reads[model_input.name])
        }
        lines.append(f"          - {_yaml_flow_mapping(row)}")
    lines += [
        "    expect:",
        "      rows:",
        "        # TODO: this stub is deliberately empty and fails until you",
        "        # replace it with the model's actual expected output for",
        "        # the given fixtures above; dex never invents an expectation",
        "        - {}",
    ]
    return "\n".join(lines) + "\n"


def _example_value(data_type: str) -> object:
    upper = data_type.upper()
    if "BOOL" in upper:
        return True
    if any(h in upper for h in ("INT", "HUGEINT", "DECIMAL", "NUMERIC")):
        return 1
    if any(h in upper for h in ("DOUBLE", "FLOAT", "REAL")):
        return 1.0
    if "TIMESTAMP" in upper:
        return "2024-01-01 00:00:00"
    if "DATE" in upper:
        return "2024-01-01"
    return "example"


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_flow_mapping(row: dict[str, object]) -> str:
    if not row:
        return "{}"
    return (
        "{"
        + ", ".join(f"{key}: {_yaml_scalar(value)}" for key, value in row.items())
        + "}"
    )
