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

import datetime
import re
from typing import NamedTuple

import sqlglot
import yaml
from sqlglot import exp

from ..cache import DexCache, match_identifier
from ..dbt_project import (
    REF_PATTERN,
    SOURCE_PATTERN,
    DbtProjectView,
    jinja_regions,
    manifest_node_label,
    model_file,
)
from ..errors import DexError
from . import window_fixture
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
    #: What the exploration cache is searched for: the relation a ``source()``
    #: declares, where the cache holds it (its ``identifier`` can differ from
    #: the table name the model writes), otherwise ``name``.
    lookup: str = ""

    @property
    def cached_as(self) -> str:
        return self.lookup or self.name


class Watermark(NamedTuple):
    """The comparison an incremental branch selects its new rows with.

    ``column`` is the model-side column compared, ``operator`` the comparison
    as read with that column on the left (``>`` or ``>=`` for the usual
    high-water mark), ``input`` the model input the column is read from
    (``None`` where no input, or more than one, reads a column of that name),
    and ``this_column`` the column of ``{{ this }}`` the bound reads.
    """

    column: str
    operator: str
    input: str | None
    this_column: str | None


class UnitTestScaffold(NamedTuple):
    """What :func:`unit_test_scaffold` planned, and what it reached.

    ``window_boundaries`` and ``window_notes`` come from
    :mod:`.window_fixture`: the calendar windows whose boundary rows were
    placed, and those it could not place rows for, each with a reason code.
    ``incremental`` is ``None`` for a model with no ``is_incremental()``
    branch, otherwise ``{"tests": [...], "watermark": {...} | None}``.
    """

    edits: list[PlanEdit]
    inputs: list[str]
    window_boundaries: list[dict[str, object]]
    window_notes: list[dict[str, str]]
    incremental: dict[str, object] | None = None


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

    scaffold = unit_test_scaffold(view, cache, model_name)
    return scaffold.edits, scaffold.inputs


def unit_test_scaffold(
    view: DbtProjectView, cache: DexCache | None, model_name: str
) -> UnitTestScaffold:
    """:func:`unit_test_scaffold_edits`, plus what it read of an incremental
    model (issue #1111).

    A model whose only jinja control flow is ``{% if is_incremental() %}``
    (with an optional ``{% else %}``) is two programs: the full build and the
    next incremental run. A full build, and so a ``--full-refresh``, never runs
    the second, so this scaffolds a test for each. The incremental one carries
    dbt's ``is_incremental`` override and a ``this`` fixture. Where the branch
    compares a column with a subquery over ``{{ this }}``, the watermark's input
    gets rows before, on and after it plus a NULL, so the expectation filled in
    pins which side of the boundary each one lands on.

    Calendar windows the full program reads get boundary rows from
    :mod:`.window_fixture` (#1112), in the full-build test.
    """

    path = model_file(view, model_name)
    sql = view.files[path].content

    inputs = _find_inputs(sql)
    if not inputs:
        raise TestScaffoldError(
            f"'{model_name}' has no ref()/source() inputs; there is nothing "
            "to scaffold a fixture from"
        )
    cache = _pinned_to_sources(cache, view, sql)
    declared = _declared_source_relations(view)
    known = [d.identifier for d in cache.datasets] if cache is not None else []
    inputs = [
        i._replace(lookup=_cached_relation(declared.get(_source_key(i), ""), known))
        if i.literal.startswith("source(")
        else i
        for i in inputs
    ]

    variants = _incremental_variants(sql)
    parsed = _parse_model(model_name, variants.full if variants else sql)

    inputs_by_lower = {i.name.lower(): i for i in inputs}
    reads: dict[str, set[str]] = {}
    full_output = _resolve_reads(
        parsed, {}, inputs_by_lower, reads, cache, outermost=True
    )

    this_reads: set[str] = set()
    watermark: Watermark | None = None
    if variants is not None:
        incremental = _parse_model(model_name, variants.incremental)
        incremental_reads: dict[str, set[str]] = {}
        _resolve_reads(
            incremental,
            {},
            {
                **inputs_by_lower,
                THIS_PLACEHOLDER: ModelInput(THIS, THIS, lookup=model_name),
            },
            incremental_reads,
            cache,
            outermost=True,
        )
        this_reads = incremental_reads.pop(THIS, set()) | _subquery_reads_of_this(
            incremental
        )
        for name, columns in incremental_reads.items():
            reads.setdefault(name, set()).update(columns)
        watermark = _watermark(incremental, reads)

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
    windows = window_fixture.boundary_rows(
        parsed,
        {key: found.name for key, found in inputs_by_lower.items()},
        reads,
        types,
        _example_value,
    )
    full_given = []
    for i in inputs:
        rows = windows.rows.get(i.name) or [_example_row(types[i.name], reads[i.name])]
        full_given.append(_Given(i.literal, rows, [None] * len(rows)))
    is_incremental_model = (
        _model_materialization(view, model_name, sql, path) == "incremental"
    )
    full_test = _test_block(
        f"test_{model_name}",
        model_name,
        full_given,
        is_incremental=False
        if (variants is not None or is_incremental_model)
        else None,
    )
    if variants is None:
        edit = PlanEdit(
            path=yaml_path,
            kind=EditKind.SCHEMA_YML,
            new_content=_unit_tests_yaml([full_test]),
        )
        return UnitTestScaffold(
            edits=[edit],
            inputs=[i.name for i in inputs],
            window_boundaries=windows.boundaries,
            window_notes=windows.notes,
        )

    this_types = _this_types(model_name, this_reads, full_output, types, cache)
    incremental_name = f"test_{model_name}_incremental"
    given = [
        _incremental_given(i, reads[i.name], types[i.name], watermark) for i in inputs
    ]
    if this_types:
        given.append(_this_given(this_types, watermark))
    incremental_test = _test_block(
        incremental_name,
        model_name,
        given,
        is_incremental=True,
        expect_note=(
            "for this test: the rows this incremental run inserts or merges, "
            "not the whole table"
        ),
    )
    edit = PlanEdit(
        path=yaml_path,
        kind=EditKind.SCHEMA_YML,
        new_content=_unit_tests_yaml([full_test, incremental_test]),
    )
    return UnitTestScaffold(
        edits=[edit],
        inputs=[i.name for i in inputs],
        window_boundaries=windows.boundaries,
        window_notes=windows.notes,
        incremental={
            "tests": [f"test_{model_name}", incremental_name],
            "watermark": watermark._asdict() if watermark is not None else None,
        },
    )


def _parse_model(model_name: str, sql: str) -> exp.Expression:
    """One program of the model, rendered and parsed, or a scaffold refusal."""

    try:
        rendered = render_model_sql(sql, naming_resolver)
    except UnattributableError as exc:
        raise TestScaffoldError(
            f"'{model_name}': {exc}; a unit test needs a statically readable SELECT"
        ) from exc
    try:
        return sqlglot.parse_one(rendered, read="duckdb")
    except Exception as exc:
        raise TestScaffoldError(
            f"'{model_name}': the rendered SQL could not be parsed ({exc})"
        ) from exc


# --- the incremental branch (issue #1111) ----------------------------------------

#: What ``{{ this }}`` is read as while the incremental branch is parsed. It
#: never leaves this module, and the leading ``__dex`` keeps it apart from any
#: name a project would give a model.
THIS_PLACEHOLDER = "__dex_this"
#: How a dbt unit test names the model's own relation in ``given``.
THIS = "this"

_CONFIG_CALL = re.compile(r"\{\{-?\s*config\s*\(", re.DOTALL)


def _extract_in_model_materialization(sql: str) -> str | None:
    """Read `materialized` from in-model `config(...)` calls in SQL.

    Handles single and double-quoted strings and nested parens so that
    complex arguments do not close the call early. When multiple config()
    calls are present, the last scalar assignment wins.
    """

    last_mat: str | None = None
    for m in _CONFIG_CALL.finditer(sql):
        pos = m.end()
        depth = 1
        in_quote: str | None = None
        escape = False
        while pos < len(sql) and depth > 0:
            ch = sql[pos]
            if in_quote:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == in_quote:
                    in_quote = None
            else:
                if ch in ("'", '"'):
                    in_quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
            pos += 1
        if depth != 0:
            continue
        inner = sql[m.end() : pos - 1]
        depth = 0
        in_quote = None
        escape = False
        start = 0
        args: list[str] = []
        for i, ch in enumerate(inner):
            if in_quote:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == in_quote:
                    in_quote = None
            else:
                if ch in ("'", '"'):
                    in_quote = ch
                elif ch in ("(", "[", "{"):
                    depth += 1
                elif ch in (")", "]", "}"):
                    depth -= 1
                elif ch == "," and depth == 0:
                    args.append(inner[start:i])
                    start = i + 1
        args.append(inner[start:])
        for arg in args:
            if "=" not in arg:
                continue
            key, val = arg.split("=", 1)
            if key.strip() == "materialized":
                cleaned = val.strip()
                if (cleaned.startswith("'") and cleaned.endswith("'")) or (
                    cleaned.startswith('"') and cleaned.endswith('"')
                ):
                    cleaned = cleaned[1:-1].strip()
                last_mat = cleaned.lower()
    return last_mat


def _manifest_model_materialization(
    view: DbtProjectView, model_name: str, path: str
) -> str | None:
    """Read the model's materialization from compiled manifest nodes."""
    if not view.manifest or not isinstance(view.manifest.get("nodes"), dict):
        return None
    for uid, node in view.manifest["nodes"].items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        if node.get("package_name") != view.project_name:
            continue
        name_match = node.get("name") == model_name
        if not name_match and manifest_node_label(uid, node) != model_name:
            continue
        orig = node.get("original_file_path")
        if orig and orig != path:
            continue
        config = node.get("config")
        mat = config.get("materialized") if isinstance(config, dict) else None
        if isinstance(mat, str) and mat.strip():
            return mat.strip().lower()
    return None


def _schema_yaml_model_materialization(
    view: DbtProjectView, model_name: str
) -> str | None:
    """Read the model's materialization from authored schema YAML files."""
    for rel_path, file in view.files.items():
        if not rel_path.endswith((".yml", ".yaml")):
            continue
        if not any(
            rel_path.startswith(f"{mp}/") or rel_path == mp for mp in view.model_paths
        ):
            continue
        try:
            parsed = yaml.safe_load(file.content)
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        models_entries = parsed.get("models")
        if not isinstance(models_entries, list):
            continue
        for entry in models_entries:
            if not isinstance(entry, dict) or entry.get("name") != model_name:
                continue
            config = entry.get("config")
            mat = (
                config.get("materialized") if isinstance(config, dict) else None
            ) or entry.get("materialized")
            if isinstance(mat, str) and mat.strip():
                return mat.strip().lower()
    return None


def _dbt_project_model_materialization(
    view: DbtProjectView, model_name: str, path: str
) -> str | None:
    """Read the model's materialization from dbt_project.yml model path hierarchies."""
    proj_file = view.files.get("dbt_project.yml")
    if not proj_file:
        return None
    try:
        raw = yaml.safe_load(proj_file.content)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    models_root = raw.get("models")
    if not isinstance(models_root, dict):
        return None
    proj_models = models_root.get(view.project_name)
    if not isinstance(proj_models, dict):
        return None

    rel_parts: list[str] = []
    for mp in view.model_paths:
        prefix = f"{mp}/"
        if path.startswith(prefix):
            without_mp = path[len(prefix) :]
            rel_parts = without_mp.split("/")
            if rel_parts and rel_parts[-1].endswith(".sql"):
                rel_parts[-1] = rel_parts[-1][:-4]
            break
    if not rel_parts:
        rel_parts = [model_name]

    resolved: str | None = None

    def check_mat(curr: dict) -> str | None:
        val = curr.get("+materialized") or curr.get("materialized")
        return val.strip().lower() if isinstance(val, str) and val.strip() else None

    mat = check_mat(proj_models)
    if mat:
        resolved = mat

    curr = proj_models
    for part in rel_parts:
        if not isinstance(curr, dict) or part not in curr:
            break
        curr = curr[part]
        if isinstance(curr, dict):
            mat = check_mat(curr)
            if mat:
                resolved = mat

    return resolved


def _model_materialization(
    view: DbtProjectView, model_name: str, sql: str, path: str
) -> str:
    """Resolve the model's materialization across in-model config(), schema YAML,
    dbt_project.yml settings, and compiled manifest nodes."""

    in_model = _extract_in_model_materialization(sql)
    if in_model:
        return in_model

    yaml_mat = _schema_yaml_model_materialization(view, model_name)
    if yaml_mat:
        return yaml_mat

    proj_mat = _dbt_project_model_materialization(view, model_name, path)
    if proj_mat:
        return proj_mat

    manifest_mat = _manifest_model_materialization(view, model_name, path)
    if manifest_mat:
        return manifest_mat

    return "view"


_IF_INCREMENTAL = re.compile(r"if\s+is_incremental\s*\(\s*\)")


class _Variants(NamedTuple):
    full: str
    incremental: str


def _incremental_variants(sql: str) -> _Variants | None:
    """The model as its full build and as its incremental run, or ``None``.

    Only a flat ``{% if is_incremental() %} ... [{% else %} ...] {% endif %}``
    is read, any number of times. Any other statement block (another ``if``, a
    ``for``, a nested ``if``) returns ``None``, so the caller's own render
    refuses it exactly as before. ``{{ this }}`` becomes
    :data:`THIS_PLACEHOLDER`, and a ``{{ this }}`` outside the branch also
    returns ``None``, since the full build has no ``this`` to fixture.
    """

    regions, text = jinja_regions(sql)
    if not any(r.kind == "statement" for r in regions):
        return None
    full: list[str] = []
    incremental: list[str] = []
    branch: str | None = None
    seen = False

    def emit(piece: str) -> None:
        """Write ``piece`` into each program the current branch belongs to."""

        if branch != "else":
            incremental.append(piece)
        if branch != "if":
            full.append(piece)

    cursor = 0
    for region in regions:
        emit(text[cursor : region.start])
        cursor = region.end
        word = " ".join(region.body.strip().strip("-").split())
        if region.kind == "expression":
            emit(THIS_PLACEHOLDER if word == THIS else text[region.start : region.end])
            continue
        if branch is None and _IF_INCREMENTAL.fullmatch(word):
            branch, seen = "if", True
        elif branch == "if" and word == "else":
            branch = "else"
        elif branch is not None and word == "endif":
            branch = None
        else:
            return None
    emit(text[cursor:])
    if branch is not None or not seen:
        return None
    full_text = "".join(full)
    if THIS_PLACEHOLDER in full_text:
        return None
    return _Variants(full_text, "".join(incremental))


def _is_this(node: exp.Expression) -> bool:
    """Whether ``node`` is the relation ``{{ this }}`` was read as."""

    return isinstance(node, exp.Table) and node.name.lower() == THIS_PLACEHOLDER


def _subquery_reads_of_this(parsed: exp.Expression) -> set[str]:
    """Columns read from ``{{ this }}`` in a SELECT whose only source it is:
    the watermark shape ``(select max(d) from {{ this }})``, which
    :func:`_resolve_reads` does not walk into."""

    columns: set[str] = set()
    for table in parsed.find_all(exp.Table):
        if not _is_this(table):
            continue
        select = table.find_ancestor(exp.Select)
        if select is None or len(_from_and_join_nodes(select)) != 1:
            continue
        alias = (table.alias_or_name or "").lower()
        for column in select.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue
            if column.find_ancestor(exp.Select) is not select:
                continue
            if column.table and column.table.lower() != alias:
                continue
            columns.add(column.name)
    return columns


_OPERATORS: dict[type, str] = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
_FLIPPED = {">": "<", ">=": "<=", "<": ">", "<=": ">="}


def _watermark(parsed: exp.Expression, reads: dict[str, set[str]]) -> Watermark | None:
    """The one comparison of a column with a subquery over ``{{ this }}``.

    ``None`` where there is no such comparison, or more than one, since then
    there is no single boundary for the fixture rows to sit on.
    """

    found: list[Watermark] = []
    for where in parsed.find_all(exp.Where):
        for predicate in where.find_all(*_OPERATORS):
            if predicate.find_ancestor(exp.Where) is not where:
                continue
            operator = _OPERATORS[type(predicate)]
            sides = (
                (predicate.this, predicate.expression, operator),
                (predicate.expression, predicate.this, _FLIPPED[operator]),
            )
            for column, bound, op in sides:
                if not isinstance(column, exp.Column):
                    continue
                if not any(_is_this(t) for t in bound.find_all(exp.Table)):
                    continue
                found.append(
                    Watermark(
                        column=column.name,
                        operator=op,
                        input=_owner(reads, column.name),
                        this_column=_bound_column(bound),
                    )
                )
    return found[0] if len(set(found)) == 1 else None


def _owner(reads: dict[str, set[str]], column: str) -> str | None:
    """The one input that reads ``column``, or ``None`` where none or several do."""

    owners = [
        name
        for name, columns in reads.items()
        if column.lower() in {c.lower() for c in columns}
    ]
    return owners[0] if len(owners) == 1 else None


def _bound_column(bound: exp.Expression) -> str | None:
    """The ``{{ this }}`` column a watermark bound reads, as in
    ``(select max(d) from {{ this }})``, where exactly one is read."""

    subquery = bound if isinstance(bound, exp.Subquery) else bound.find(exp.Subquery)
    if subquery is None or not isinstance(subquery.this, exp.Select):
        return None
    projections = subquery.this.expressions
    if len(projections) != 1:
        return None
    names = {c.name for c in projections[0].find_all(exp.Column)}
    return names.pop() if len(names) == 1 else None


def _this_types(
    model_name: str,
    columns: set[str],
    full_output: _Derived,
    types: dict[str, dict[str, str]],
    cache: DexCache | None,
) -> dict[str, str]:
    """Each ``{{ this }}`` column's type: the type of the input column the full
    build passes through to it first, then the model's own cached relation.
    Never guessed: a column neither settles is refused, naming the profile that
    would settle it."""

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for column in sorted(columns):
        provenance = full_output.get(column.lower())
        data_type = (
            types.get(provenance[0], {}).get(provenance[1])
            if provenance is not None
            else None
        )
        if data_type:
            resolved[column] = data_type
        else:
            unresolved.append(column)
    if unresolved and cache is not None:
        matches = match_identifier(model_name, [d.identifier for d in cache.datasets])
        if len(matches) == 1:
            dataset = next(d for d in cache.datasets if d.identifier == matches[0])
            cached = {c.name.lower(): c.data_type for c in dataset.columns}
            for column in list(unresolved):
                if cached.get(column.lower()):
                    resolved[column] = cached[column.lower()]
                    unresolved.remove(column)
    if unresolved:
        raise TestScaffoldError(
            f"'{model_name}': the incremental branch reads "
            f"{', '.join(unresolved)} from {{{{ this }}}}, which no input column "
            "passes through and the exploration cache does not type; profile the "
            "model's built relation first (`explore profile "
            f"<dev namespace>.{model_name} --target dev`)"
        )
    return resolved


# --- model + input discovery ---------------------------------------------------


def _pinned_to_sources(
    cache: DexCache | None, view: DbtProjectView, sql: str
) -> DexCache | None:
    """The cache, with each ``source()`` input narrowed to the relation its
    declaration names.

    Inputs are looked up by bare table name, which is ambiguous wherever two
    cached relations share one: two schemas, or on Trino two catalogs. A
    source declares exactly which relation it is, so where that relation is
    cached, every other dataset sharing its table name is dropped from the
    lookup. A source whose relation is not cached is left to the name lookup,
    which refuses with the fix as before.
    """

    if cache is None:
        return None
    declared = _declared_source_relations(view)
    known = {d.identifier.lower() for d in cache.datasets}
    pinned: dict[str, str] = {}
    for match in SOURCE_PATTERN.finditer(sql):
        relation = declared.get((match.group(1), match.group(2)))
        if relation is not None and relation.lower() in known:
            pinned[match.group(2).lower()] = relation.lower()
    if not pinned:
        return cache
    kept = [
        d
        for d in cache.datasets
        if pinned.get(d.identifier.rsplit(".", 1)[-1].lower(), d.identifier.lower())
        == d.identifier.lower()
    ]
    return cache.model_copy(update={"datasets": kept})


def _cached_relation(relation: str, known: list[str]) -> str:
    """``relation`` where the cache holds it, otherwise ``""``, so an input
    whose declared relation was never cached keeps the bare-name lookup."""

    return relation if relation and match_identifier(relation, known) else ""


def _source_key(model_input: ModelInput) -> tuple[str, str]:
    """The ``(source, table)`` a ``source()`` input names."""

    match = SOURCE_PATTERN.search("{{ " + model_input.literal + " }}")
    return (match.group(1), match.group(2)) if match else ("", model_input.name)


def _declared_source_relations(view: DbtProjectView) -> dict[tuple[str, str], str]:
    """``(source, table)`` -> the dotted relation it declares, for every source
    the project's YAML declares. dbt's own defaults apply: ``schema`` is the
    source's name when unset, and a table's ``identifier`` overrides its name.
    A file that does not parse declares nothing here."""

    relations: dict[tuple[str, str], str] = {}
    for path, file in view.files.items():
        if not path.endswith((".yml", ".yaml")) or "sources:" not in file.content:
            continue
        try:
            doc = yaml.safe_load(file.content) or {}
        except yaml.YAMLError:
            continue
        for source in doc.get("sources") or [] if isinstance(doc, dict) else []:
            if not isinstance(source, dict) or not source.get("name"):
                continue
            name = str(source["name"])
            schema = str(source.get("schema") or name)
            database = source.get("database")
            for table in source.get("tables") or []:
                if not isinstance(table, dict) or not table.get("name"):
                    continue
                identifier = str(table.get("identifier") or table["name"])
                parts = [str(database)] if database else []
                relations[(name, str(table["name"]))] = ".".join(
                    [*parts, schema, identifier]
                )
    return relations


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
    lookup: str = ""


# A resolved CTE/subquery: its own output columns, each either a direct
# passthrough of an input's column (traced through as many CTEs as it takes)
# or None (computed, aggregated, or otherwise not a single column's identity).
_Derived = dict


def _resolve_reads(
    node: exp.Expression,
    env: dict[str, object],
    inputs_by_lower: dict[str, ModelInput],
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
                found = inputs_by_lower[table_name.lower()]
                sources[alias] = _BaseInput(found.name, found.cached_as)
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
    """A ``*`` over one source, as output name -> the input column it passes through."""

    if isinstance(source, _BaseInput):
        dataset = _lookup_dataset(cache, source.lookup or source.name, source.name)
        return {c.name.lower(): (source.name, c.name) for c in dataset.columns}
    return dict(source)  # a CTE/subquery: already a name -> provenance map


def _expand_all(sources: dict[str, object], cache: DexCache | None) -> _Derived:
    """A bare ``*`` over every source a SELECT reads."""

    expanded: _Derived = {}
    for source in sources.values():
        expanded.update(_expand_source(source, cache))
    return expanded


def _from_and_join_nodes(select: exp.Select) -> list[exp.Expression]:
    """The relations a SELECT reads in its FROM and JOIN clauses."""

    nodes: list[exp.Expression] = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        nodes.append(from_clause.this)
    nodes.extend(join.this for join in select.args.get("joins") or [])
    return nodes


# --- typing, from the exploration cache only ------------------------------------


def _lookup_dataset(cache: DexCache | None, name: str, label: str) -> object:
    """The cached dataset ``name`` matches, or a refusal naming the profile to run."""

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
    dataset = _lookup_dataset(cache, model_input.cached_as, model_input.literal)
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


class _Given(NamedTuple):
    """One ``given`` block: an input, its rows, and a note per row."""

    literal: str
    rows: list[dict[str, object]]
    #: One YAML comment per row, written above it, or ``None``.
    notes: list[str | None]
    #: Written as ``format: sql`` with each value cast to its type instead,
    #: for an input dbt would otherwise type from a relation that may not
    #: exist yet (``this``).
    types: dict[str, str] | None = None


def _test_block(
    name: str,
    model_name: str,
    given: list[_Given],
    *,
    is_incremental: bool | None = None,
    expect_note: str | None = None,
) -> list[str]:
    """One unit test's YAML lines. ``is_incremental`` sets dbt's override,
    which is left out where it is ``None``."""

    lines = [f"  - name: {name}", f"    model: {model_name}"]
    if is_incremental is not None:
        # dbt refuses a unit test on an incremental model that leaves this
        # override out, so the full-build test states `false` as well.
        note = (
            "runs the {% if is_incremental() %} branch, which a full build and "
            "a --full-refresh never execute"
            if is_incremental
            else "the full build: the program a --full-refresh runs"
        )
        lines += [
            "    overrides:",
            "      macros:",
            f"        # {note}",
            f"        is_incremental: {'true' if is_incremental else 'false'}",
        ]
    lines.append("    given:")
    for block in given:
        lines.append(f"      - input: {block.literal}")
        if block.types is not None:
            lines += [
                "        # format: sql, typed here, because dbt types a dict fixture",
                "        # for `this` from the model's relation, which a cold dev",
                "        # target does not have yet",
                "        format: sql",
                "        rows: |",
            ]
            selects = []
            for row, note in zip(block.rows, block.notes, strict=True):
                if note:
                    lines.append(f"          -- {note}")
                selects.append(
                    "select "
                    + ", ".join(
                        f"{_sql_literal(value, block.types[column])} as {column}"
                        for column, value in row.items()
                    )
                )
            lines += [
                f"          {line}"
                for line in "\nunion all\n".join(selects).split("\n")
            ]
            continue
        lines.append("        rows:")
        for row, note in zip(block.rows, block.notes, strict=True):
            if note:
                lines.append(f"          # {note}")
            lines.append(f"          - {_yaml_flow_mapping(row)}")
    lines += [
        "    expect:",
        "      rows:",
        "        # TODO: this stub is deliberately empty and fails until you",
        "        # replace it with the model's actual expected output for",
        "        # the given fixtures above; dex never invents an expectation",
    ]
    if expect_note:
        lines.append(f"        # ({expect_note})")
    lines.append("        - {}")
    return lines


def _unit_tests_yaml(tests: list[list[str]]) -> str:
    """The ``unit_tests:`` document holding ``tests``, each a block of lines."""

    lines = ["version: 2", "", "unit_tests:"]
    for test in tests:
        lines += test
    return "\n".join(lines) + "\n"


def _example_row(
    types: dict[str, str], columns: set[str], index: int | None = None
) -> dict[str, object]:
    """One example value per column, numbered by ``index`` in a multi-row fixture."""

    return {column: _example_value(types[column], index) for column in sorted(columns)}


_BOUNDARY_NOTES: tuple[str, ...] = (
    "before the current watermark in `this`: an incremental run skips it "
    "unless a lookback window reaches back to it",
    "exactly on the current watermark: `>` skips it and `>=` loads it (a "
    "unique_key keeps a row loaded before from duplicating)",
    "after the current watermark: every incremental run loads it",
    "no watermark value: neither `>` nor `>=` is true for NULL, so only a "
    "full build loads it",
)


def _incremental_given(
    model_input: ModelInput,
    columns: set[str],
    types: dict[str, str],
    watermark: Watermark | None,
) -> _Given:
    """The input's rows for the incremental test. On the watermark's input,
    one row before, on and after the current watermark, and one NULL; on any
    other input, the single example row the full test has."""

    column = _actual(columns, watermark.column) if watermark is not None else None
    if (
        watermark is None
        or watermark.input != model_input.name
        or column not in types
        or _ordered_value(types[column], 0) is None
    ):
        return _Given(model_input.literal, [_example_row(types, columns)], [None])
    rows = []
    for index, offset in enumerate((-1, 0, 1, None), start=1):
        row = _example_row(types, columns, index)
        row[column] = None if offset is None else _ordered_value(types[column], offset)
        rows.append(row)
    return _Given(model_input.literal, rows, list(_BOUNDARY_NOTES))


def _this_given(this_types: dict[str, str], watermark: Watermark | None) -> _Given:
    """The ``this`` fixture: one row of what earlier runs loaded, on the
    watermark where one was read."""

    row = _example_row(this_types, set(this_types))
    note = None
    if watermark is not None and watermark.this_column is not None:
        column = _actual(set(this_types), watermark.this_column)
        value = _ordered_value(this_types.get(column, ""), 0)
        if value is not None:
            row[column] = value
            note = "what earlier runs loaded: its max is the current watermark"
    return _Given(THIS, [row], [note], types=this_types)


def _sql_literal(value: object, data_type: str) -> str:
    """``value`` as a SQL literal cast to ``data_type``, the column's type as
    the warehouse reported it, so the fixture carries the type dbt would
    otherwise read from a relation."""

    if value is None:
        literal = "null"
    elif isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, (int, float)):
        literal = str(value)
    else:
        literal = "'" + str(value).replace("'", "''") + "'"
    return f"cast({literal} as {data_type})"


def _actual(columns: set[str], name: str) -> str:
    """``name`` as ``columns`` spells it, matched case-insensitively."""

    return next((c for c in columns if c.lower() == name.lower()), name)


def _ordered_value(data_type: str, offset: int) -> object | None:
    """A value ``offset`` steps from the fixture's watermark, in ``data_type``,
    or ``None`` for a type with no obvious order (text, boolean)."""

    upper = data_type.upper()
    day = datetime.date(2024, 1, 2) + datetime.timedelta(days=offset)
    if "TIMESTAMP" in upper or "DATETIME" in upper:
        return f"{day.isoformat()} 00:00:00"
    if "DATE" in upper:
        return day.isoformat()
    if any(h in upper for h in ("INT", "DECIMAL", "NUMERIC", "NUMBER")):
        return 2 + offset
    if any(h in upper for h in ("DOUBLE", "FLOAT", "REAL")):
        return 2.0 + offset
    return None


def _example_value(data_type: str, index: int | None = None) -> object:
    """One example value for ``data_type``. ``index`` numbers the rows of a
    multi-row fixture, so keys and labels stay distinct across them."""

    upper = data_type.upper()
    if "BOOL" in upper:
        return True
    if any(h in upper for h in ("INT", "HUGEINT", "DECIMAL", "NUMERIC")):
        return index or 1
    if any(h in upper for h in ("DOUBLE", "FLOAT", "REAL")):
        return float(index or 1)
    if "TIMESTAMP" in upper:
        return "2024-01-01 00:00:00"
    if "DATE" in upper:
        return "2024-01-01"
    return "example" if index is None else f"example_{index}"


def _yaml_scalar(value: object) -> str:
    """``value`` as a YAML flow scalar."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_flow_mapping(row: dict[str, object]) -> str:
    """``row`` as a one-line YAML flow mapping."""

    if not row:
        return "{}"
    return (
        "{"
        + ", ".join(f"{key}: {_yaml_scalar(value)}" for key, value in row.items())
        + "}"
    )
