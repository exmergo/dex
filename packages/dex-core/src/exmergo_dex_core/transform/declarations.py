"""Declarations: what a model means, stated once and rendered into the project.

A model dex helps build used to carry almost nothing about what it means: which
rows are in, what one row is, what each column is, and which decisions the SQL
rests on. Those decisions lived only in an agent's transcript, so the next
agent, the next reviewer and `maintain` all started from nothing, and they are
exactly the decisions agents get wrong (#491).

A declaration is that statement, authored beside the edits in a plan payload:

    {"edits": [...], "declarations": [{"model": ..., "grain": [...], ...}]}

It is rendered into the model's **existing** YAML entry, wherever in the project
that entry lives, by splicing only the keys dex owns: the model and column
``description``, the tests a declaration implies, and ``config.meta.dex``. Every
other byte of the file stays where it was, for the reason every rewrite in this
package gives: re-serialising a ``schema.yml`` reflows it and drops the comments,
and the diff stops showing what changed. A model with no entry gets one where the
scaffold would put it, and never a second one beside an existing entry, which dbt
refuses (``DuplicatePatchPathError``).

Three rules hold throughout:

- **Missing intent warns, it never refuses.** A partial declaration, or none, is a
  warning. A *malformed* one (a column the model does not produce, an unknown
  role, a reference that does not resolve) is refused with the fix named.
- **No values from data.** A declaration is authored text. A population filter on
  a column that looks like personal data is refused the way a PII-shaped seed
  column is, because its values would land in git.
- **The YAML is the record.** ``data.decisions`` is read back out of the rendered
  ``config.meta.dex.assumptions`` rather than out of the payload, so plan, apply
  and build all report the same thing from the same place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from ..dbt_project import DbtProjectView
from ..errors import DexError
from ..guards.pii_meta import entry_meta
from .plans import EditKind, EditOp, PlanEdit, select_columns
from .rewrite import (
    RewriteError,
    _mapping,
    _pair_span,
    _scalar_value,
    _sequence,
    _span,
    before_trailing_blanks,
    column_anchor,
    prevailing_test_key,
    splice,
    yaml_blocks,
)

#: The namespace under ``config.meta`` that dex owns. Everything a declaration
#: says that dbt has no construct for lands here, and nothing else does.
DEX_META_KEY = "dex"


class DeclarationError(DexError, ValueError):
    """A declaration dex refuses, with the fix named in the message.

    A ``ValueError`` as well, so it reaches the same request-error envelope every
    other malformed payload does on the commands that already catch that.
    """


class Role(str, Enum):
    KEY = "key"
    FOREIGN_KEY = "foreign_key"
    DIMENSION = "dimension"
    TIME = "time"
    MEASURE = "measure"
    ATTRIBUTE = "attribute"


class Additivity(str, Enum):
    ADDITIVE = "additive"
    # Does not add across time: a balance, a running or season-to-date total.
    SEMI_ADDITIVE = "semi_additive"
    # Does not add at all: a ratio, a share, a distinct count.
    NON_ADDITIVE = "non_additive"


class Evidence(str, Enum):
    """Where a decision came from, strongest first."""

    REQUEST = "request"
    CONTRACT = "contract"
    PRECEDENT = "precedent"
    DATA = "data"
    DEFAULT = "default"


class _Strict(BaseModel):
    # An unknown key is a typo far more often than a deliberate extension, and a
    # typo silently dropped is a declaration that says less than its author meant.
    model_config = ConfigDict(extra="forbid")


class PopulationFilter(_Strict):
    column: str
    includes: list[str | int | float | bool] | None = None
    excludes: list[str | int | float | bool] | None = None


class Population(_Strict):
    rule: str | None = None
    filters: list[PopulationFilter] = []


class ColumnDeclaration(_Strict):
    role: Role | None = None
    description: str | None = None
    references: str | None = None
    # `never`, or a sentence saying what NULL means in this column.
    null_rule: str | None = None
    aggregation: str | None = None
    additivity: Additivity | None = None
    # `currency:<ISO code>`, `fraction`, `percent`, `count`, or a plain unit.
    unit: str | None = None


class Assumption(_Strict):
    decision: str | None = None
    chosen: str | None = None
    evidence: Evidence | None = None
    detail: str | None = None


class Declaration(_Strict):
    model: str
    description: str | None = None
    grain: list[str] | None = None
    population: Population | None = None
    columns: dict[str, ColumnDeclaration] = {}
    assumptions: list[Assumption] = []


# --- reading the payload ------------------------------------------------------


def parse_declarations(entries: Any) -> list[Declaration]:
    """The payload's ``declarations`` list, validated, or a refusal naming the fix."""

    if entries is None:
        return []
    if not isinstance(entries, list):
        raise DeclarationError(
            'declarations must be a list: {"declarations": [{"model": ...}, ...]}'
        )
    parsed: list[Declaration] = []
    for index, entry in enumerate(entries):
        try:
            parsed.append(Declaration.model_validate(entry))
        except ValidationError as exc:
            raise DeclarationError(_validation_message(index, entry, exc)) from exc
    seen: set[str] = set()
    for declaration in parsed:
        if declaration.model in seen:
            raise DeclarationError(
                f"'{declaration.model}' is declared twice in this payload; merge the "
                "two into one declaration"
            )
        seen.add(declaration.model)
    return parsed


def _validation_message(index: int, entry: Any, exc: ValidationError) -> str:
    model = entry.get("model") if isinstance(entry, dict) else None
    where = f"declarations[{index}]" + (f" ('{model}')" if model else "")
    problems = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            problems.append(f"{loc} is not a declaration field")
        elif error["type"] == "missing":
            problems.append(f"{loc} is required")
        else:
            problems.append(f"{loc}: {error['msg']}")
    return f"{where} is malformed: " + "; ".join(problems)


# --- where a model is declared --------------------------------------------------


def schema_path(files: Mapping[str, str], model: str) -> str | None:
    """The YAML file whose ``models:`` list declares ``model``, if one does.

    The lookup `transform place` and `maintain reconcile` share, so the three
    agree on where a model is declared. Searched by content rather than derived
    from a naming convention, because a project is free to declare every staging
    model in one ``_staging.yml``.
    """

    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        for block in yaml_blocks(files[path]):
            if block.form == "yaml_model_entry" and block.name == model:
                return path
    return None


def model_sql_path(
    files: Mapping[str, str], model_paths: Iterable[str], model: str
) -> str | None:
    """The ``.sql`` file that defines ``model``, if the project has one."""

    roots = [PurePosixPath(p) for p in model_paths]
    for path in sorted(files):
        candidate = PurePosixPath(path)
        if candidate.suffix != ".sql" or candidate.stem != model:
            continue
        if any(root == candidate or root in candidate.parents for root in roots):
            return path
    return None


# --- the whole pass -------------------------------------------------------------


def apply_declarations(
    declarations: list[Declaration],
    view: DbtProjectView,
    edits: list[PlanEdit],
    *,
    cache: Any = None,
    pii_overrides: Any = None,
) -> tuple[list[PlanEdit], list[str]]:
    """Validate ``declarations`` and render them into the plan's edits.

    Returns the edits with each declaration folded in (one edit per path, merged
    into a ``schema_yml`` edit the payload already carries for that file) and the
    warnings for intent that was left unstated.
    """

    files = post_change_files(view, edits)
    warnings: list[str] = []
    for declaration in declarations:
        warnings.extend(
            _check(declaration, files, view, cache=cache, pii_overrides=pii_overrides)
        )

    rendered: dict[str, str] = {}
    for declaration in declarations:
        sql_path = model_sql_path(files, view.model_paths, declaration.model)
        path = schema_path(files, declaration.model)
        if path is None:
            path = _default_schema_path(sql_path or "", declaration.model)
        try:
            files[path] = render(files.get(path), declaration)
        except RewriteError as exc:
            raise DeclarationError(
                f"dex could not write the declaration of '{declaration.model}' into "
                f"{path} predictably ({exc}); make that change by hand, then "
                "declare again"
            ) from exc
        rendered[path] = files[path]

    merged = list(edits)
    for path, content in rendered.items():
        existing = next(
            (
                i
                for i, edit in enumerate(merged)
                if edit.path == path and edit.op is EditOp.UPSERT
            ),
            None,
        )
        if existing is not None:
            merged[existing] = merged[existing].model_copy(
                update={"new_content": content}
            )
        else:
            merged.append(
                PlanEdit(path=path, kind=EditKind.SCHEMA_YML, new_content=content)
            )
    return merged, warnings


def undeclared_model_warnings(
    edits: list[PlanEdit], view: DbtProjectView, declared: Iterable[str]
) -> list[str]:
    """A warning per model this plan writes SQL for and declares nothing about.

    Only models the plan authors: dex makes its own output well declared and does
    not police the models a human wrote. A model whose YAML already carries a dex
    declaration from an earlier plan is declared, and is left alone.
    """

    files = post_change_files(view, edits)
    named = set(declared)
    warnings: list[str] = []
    for edit in edits:
        if (
            edit.kind is not EditKind.MODEL_SQL
            or edit.op is not EditOp.UPSERT
            or not edit.path.endswith(".sql")
        ):
            continue
        model = PurePosixPath(edit.path).stem
        if model in named or _carries_declaration(files, model):
            continue
        warnings.append(
            f"{edit.path}: this plan writes '{model}' and declares nothing about what "
            "it means; add a `declarations` entry (description, grain, columns, "
            "assumptions) so a reviewer and `maintain` can check it"
        )
    return warnings


def post_change_files(view: DbtProjectView, edits: list[PlanEdit]) -> dict[str, str]:
    """Every project file as it will read once ``edits`` are applied."""

    files = {path: source.content for path, source in view.files.items()}
    for edit in edits:
        if edit.op is EditOp.DELETE:
            files.pop(edit.path, None)
        elif edit.new_content is not None:
            files[edit.path] = edit.new_content
    return files


# --- validation -----------------------------------------------------------------


def _check(
    declaration: Declaration,
    files: Mapping[str, str],
    view: DbtProjectView,
    *,
    cache: Any,
    pii_overrides: Any,
) -> list[str]:
    model = declaration.model
    sql_path = model_sql_path(files, view.model_paths, model)
    if sql_path is None:
        raise DeclarationError(
            f"'{model}' is not a model in this project or in this plan; declare a "
            "model the project has, or add its SQL to the same payload"
        )

    warnings: list[str] = []
    produced = select_columns(files[sql_path])
    if produced is None:
        warnings.append(
            f"{sql_path}: the SELECT list could not be read statically (a `select *` "
            f"or a macro standing in for a column), so the columns declared for "
            f"'{model}' were not checked against it"
        )
    else:
        unknown = [c for c in declaration.columns if c.lower() not in produced]
        if unknown:
            raise DeclarationError(
                f"'{model}' declares column(s) {', '.join(unknown)} that its SELECT "
                f"list in {sql_path} does not produce; declare only output columns, "
                "or add them to the model first"
            )
        missing_grain = [
            g for g in declaration.grain or [] if g.lower() not in produced
        ]
        if missing_grain:
            raise DeclarationError(
                f"'{model}' declares grain column(s) {', '.join(missing_grain)} that "
                f"its SELECT list in {sql_path} does not produce; a grain names "
                "columns of the model"
            )

    for name, column in declaration.columns.items():
        if column.references is not None:
            _check_reference(model, name, column.references, files, view)
        elif column.role is Role.FOREIGN_KEY:
            warnings.append(
                f"'{model}.{name}' is a foreign_key with no `references`, so no "
                "relationships test was written for it"
            )

    if declaration.population is not None:
        _check_filters(model, declaration.population, cache, pii_overrides)

    warnings.extend(_partial_intent(declaration, produced))
    return warnings


def _check_reference(
    model: str,
    column: str,
    target: str,
    files: Mapping[str, str],
    view: DbtProjectView,
) -> None:
    parent, _, parent_column = target.rpartition(".")
    if not parent or not parent_column:
        raise DeclarationError(
            f"'{model}.{column}' references '{target}'; write the target as "
            "<model>.<column>"
        )
    parent_sql = model_sql_path(files, view.model_paths, parent)
    if parent_sql is not None:
        produced = select_columns(files[parent_sql])
        if produced is not None and parent_column.lower() not in produced:
            raise DeclarationError(
                f"'{model}.{column}' references '{target}', and {parent_sql} does not "
                f"produce a column '{parent_column}'"
            )
        return
    if _is_seed_or_snapshot(files, view, parent):
        return
    raise DeclarationError(
        f"'{model}.{column}' references '{target}', and the project has no model, "
        f"seed or snapshot named '{parent}'"
    )


def _is_seed_or_snapshot(
    files: Mapping[str, str], view: DbtProjectView, name: str
) -> bool:
    seed_roots = [PurePosixPath(p) for p in view.seed_paths]
    snapshot_roots = [PurePosixPath(p) for p in view.snapshot_paths]
    for path, content in files.items():
        candidate = PurePosixPath(path)
        is_seed = (
            candidate.suffix == ".csv"
            and candidate.stem == name
            and any(root in candidate.parents for root in seed_roots)
        )
        is_snapshot = (
            candidate.suffix == ".sql"
            and any(root in candidate.parents for root in snapshot_roots)
            and re.search(rf"{{%-?\s*snapshot\s+{re.escape(name)}\s*-?%}}", content)
        )
        if is_seed or is_snapshot:
            return True
    return False


def _check_filters(
    model: str, population: Population, cache: Any, pii_overrides: Any
) -> None:
    """Refuse a filter list on a column that looks like personal data.

    The seed gate's reasoning, for the same reason: a filter's values are authored
    text that lands in git and stays there. Read from names and types and the
    cache's flags, never from a value.
    """

    from ..explore.profile import classify_pii
    from ..guards import PII_BLOCK_CONFIDENCE

    flagged: dict[str, Any] = {}
    for dataset in getattr(cache, "datasets", None) or []:
        for column in dataset.columns:
            if column.pii is None:
                continue
            known = flagged.get(column.name.lower())
            if known is None or column.pii.confidence > known.confidence:
                flagged[column.name.lower()] = column.pii

    for entry in population.filters:
        if not (entry.includes or entry.excludes):
            continue
        if pii_overrides is not None and f"{model}.{entry.column}" in pii_overrides:
            continue
        detected, provisional = classify_pii(entry.column, "varchar")
        candidates = [
            flag
            for flag in (
                flagged.get(entry.column.lower()),
                None if provisional else detected,
            )
            if flag is not None
        ]
        strongest = max(candidates, key=lambda flag: flag.confidence, default=None)
        if strongest is not None and strongest.confidence >= PII_BLOCK_CONFIDENCE:
            raise DeclarationError(
                f"'{model}' filters its population on '{entry.column}', which looks "
                f"like {strongest.category.value} (confidence "
                f"{strongest.confidence:g}); a filter's values are committed to git, "
                "so state the rule in `population.rule` without listing them. If the "
                f"review says it is not personal data, add `- {{column: "
                f"{model}.{entry.column}}}` under pii_overrides in .dex/config.yml"
            )


def _partial_intent(declaration: Declaration, produced: set[str] | None) -> list[str]:
    missing = []
    if not declaration.description:
        missing.append("description")
    if not declaration.grain:
        missing.append("grain")
    if not declaration.assumptions:
        missing.append("assumptions")
    warnings = []
    if missing:
        warnings.append(
            f"'{declaration.model}' declares no {', '.join(missing)}; a reader "
            "cannot check what the model leaves unsaid"
        )
    if produced is not None:
        declared = {c.lower() for c in declaration.columns}
        undeclared = sorted(c for c in produced if c not in declared)
        if undeclared:
            warnings.append(
                f"'{declaration.model}' produces column(s) {', '.join(undeclared)} "
                "with no declared role"
            )
    for index, assumption in enumerate(declaration.assumptions):
        absent = [
            field
            for field in ("decision", "chosen", "evidence")
            if getattr(assumption, field) is None
        ]
        if absent:
            warnings.append(
                f"'{declaration.model}' assumption {index + 1} has no "
                f"{', '.join(absent)}"
            )
    return warnings


# --- rendering ------------------------------------------------------------------


def render(content: str | None, declaration: Declaration) -> str:
    """``content`` with ``declaration`` spliced into its entry for the model.

    Each key is written by its own splice against a fresh parse of the text the
    previous splice produced, so no offset is ever measured against bytes that
    have since moved.
    """

    model = declaration.model
    text = _with_entry(content, model)
    entry = lambda root: _model_entry(root, model)  # noqa: E731

    if declaration.description is not None:
        text = _set_key(text, entry, "description", declaration.description)
    model_dex = _model_dex(declaration)
    if model_dex:
        text = _set_dex(text, entry, model_dex)

    for name, column in _rendered_columns(declaration).items():
        text = _ensure_column(text, model, name)
        locate = _column_locator(model, name)
        if column["description"] is not None:
            text = _set_key(text, locate, "description", column["description"])
        if column["dex"]:
            text = _set_dex(text, locate, column["dex"])
        if column["tests"]:
            text = _add_tests(text, locate, column["tests"])
    return text


def _rendered_columns(declaration: Declaration) -> dict[str, dict[str, Any]]:
    """What each column receives: its description, its dex meta, and its tests."""

    out: dict[str, dict[str, Any]] = {}

    def slot(name: str) -> dict[str, Any]:
        return out.setdefault(name, {"description": None, "dex": {}, "tests": []})

    for name, column in declaration.columns.items():
        entry = slot(name)
        entry["description"] = column.description
        entry["dex"] = {
            key: value.value if isinstance(value, Enum) else value
            for key, value in (
                ("role", column.role),
                ("null_rule", column.null_rule),
                ("aggregation", column.aggregation),
                ("additivity", column.additivity),
                ("unit", column.unit),
                ("references", column.references),
            )
            if value is not None
        }
        if column.null_rule is not None and column.null_rule.strip().lower() == "never":
            entry["tests"].append("not_null")
        if column.references is not None:
            parent, _, parent_column = column.references.rpartition(".")
            entry["tests"].append(
                {
                    "relationships": {
                        "arguments": {
                            "to": f"ref('{parent}')",
                            "field": parent_column,
                        }
                    }
                }
            )

    grain = declaration.grain or []
    if len(grain) == 1:
        tests = slot(grain[0])["tests"]
        for name in ("unique", "not_null"):
            if name not in tests:
                tests.insert(0 if name == "unique" else 1, name)
    return out


def _model_dex(declaration: Declaration) -> dict[str, Any]:
    dex: dict[str, Any] = {}
    if declaration.grain:
        dex["grain"] = list(declaration.grain)
    if declaration.population is not None:
        dex["population"] = declaration.population.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
    if declaration.assumptions:
        dex["assumptions"] = [
            assumption.model_dump(mode="json", exclude_none=True)
            for assumption in declaration.assumptions
        ]
    return dex


# The splices below share one shape: compose the current text, locate a node,
# splice one key, return the new text. A node is located by a function rather
# than held, because every splice moves the bytes after it.


def _model_entry(root: Any, model: str) -> Any:
    for key, node in _mapping(root):
        if key != "models":
            continue
        for entry in _sequence(node):
            if _scalar_value(dict(_mapping(entry)).get("name")) == model:
                return entry
    return None


def _column_locator(model: str, column: str):
    def locate(root: Any) -> Any:
        entry = _model_entry(root, model)
        for item in _sequence(dict(_mapping(entry)).get("columns")):
            if _scalar_value(dict(_mapping(item)).get("name")) == column:
                return item
        return None

    return locate


def _compose(text: str) -> Any:
    try:
        return yaml.compose(text)
    except yaml.YAMLError as exc:
        raise RewriteError(
            f"it does not parse as YAML: {exc.__class__.__name__}"
        ) from exc


def _child(mapping: Any, key: str) -> tuple[Any, Any] | None:
    if not isinstance(mapping, yaml.MappingNode):
        return None
    for k, v in mapping.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return k, v
    return None


def _set_key(text: str, locate, key: str, value: Any) -> str:
    """Write ``key: value`` into the located mapping, replacing any prior value."""

    mapping = locate(_compose(text))
    if not isinstance(mapping, yaml.MappingNode) or not mapping.value:
        raise RewriteError(f"the entry that would hold '{key}' is not a mapping")
    if mapping.flow_style:
        raise RewriteError(
            f"the entry that would hold '{key}' is written in flow style; rewrite it "
            "in block style"
        )
    indent = " " * mapping.value[0][0].start_mark.column
    rendered = _render_pair(key, value, indent)

    found = _child(mapping, key)
    if found is not None:
        k, v = found
        line_start = text.rfind("\n", 0, k.start_mark.index) + 1
        if text[line_start : k.start_mark.index].strip():
            raise RewriteError(f"'{key}' shares its line with the entry's bullet")
        start, end = _pair_span(text, _span(k), v)
        return splice(text, [(start, before_trailing_blanks(text, end), rendered)])

    # A new key goes before `columns:` where there is one, so a model's own
    # description and config read above its column list, the way dbt documents it.
    anchor = _child(mapping, "columns")
    if anchor is not None:
        line_start = text.rfind("\n", 0, anchor[0].start_mark.index) + 1
        if not text[line_start : anchor[0].start_mark.index].strip():
            return splice(text, [(line_start, line_start, rendered)])
    last_key, last_value = mapping.value[-1]
    _start, end = _pair_span(text, _span(last_key), last_value)
    at = before_trailing_blanks(text, end)
    prefix = "" if at == 0 or text[at - 1] == "\n" else "\n"
    return splice(text, [(at, at, prefix + rendered)])


def _set_dex(text: str, locate, dex: dict[str, Any]) -> str:
    """Merge ``dex`` into the located entry's ``config.meta.dex``.

    Shallow: a key the declaration states replaces the one already there, and a
    key it leaves out is kept, so a later partial declaration does not erase an
    earlier one. Only the ``dex`` namespace is rewritten; a PII stamp or any other
    key under ``config.meta`` is untouched.
    """

    root = _compose(text)
    node = locate(root)
    plain = yaml.safe_load(yaml.serialize(node)) if node is not None else None
    existing = (entry_meta(plain) or {}).get(DEX_META_KEY)
    merged = {**existing, **dex} if isinstance(existing, dict) else dict(dex)

    config = _child(node, "config")
    if config is None:
        return _set_key(text, locate, "config", {"meta": {DEX_META_KEY: merged}})

    def config_node(r: Any) -> Any:
        found = _child(locate(r), "config")
        return found[1] if found else None

    meta = _child(config[1], "meta")
    if meta is None:
        return _set_key(text, config_node, "meta", {DEX_META_KEY: merged})

    def meta_node(r: Any) -> Any:
        found = _child(config_node(r), "meta")
        return found[1] if found else None

    return _set_key(text, meta_node, DEX_META_KEY, merged)


def _add_tests(text: str, locate, specs: list[Any]) -> str:
    """Add the tests ``specs`` names to the located column, keeping its own.

    A test the column already lists, by name, is left exactly as the author wrote
    it. An existing list keeps its key (``tests`` or ``data_tests``); a column with
    no list takes the spelling the file already uses.
    """

    node = locate(_compose(text))
    found = None
    for key in ("data_tests", "tests"):
        found = _child(node, key)
        if found is not None:
            break
    if found is None:
        return _set_key(text, locate, prevailing_test_key(text), specs)

    _key, listed = found
    present = {_test_name(item) for item in _sequence(listed)}
    todo = [spec for spec in specs if _spec_name(spec) not in present]
    if not todo:
        return text
    if not isinstance(listed, yaml.SequenceNode):
        raise RewriteError("a column's test list is not a list")

    if listed.flow_style:
        end = listed.end_mark.index
        if text[end - 1] != "]":
            raise RewriteError("a column's test list is not closed")
        items = ", ".join(_flow(spec) for spec in todo)
        joiner = ", " if listed.value else ""
        return splice(text, [(end - 1, end - 1, joiner + items)])

    first = listed.value[0]
    line_start = text.rfind("\n", 0, first.start_mark.index) + 1
    line = text[line_start : first.start_mark.index]
    if line.strip() != "-":
        raise RewriteError(
            "a column's test list does not start its items on their own lines"
        )
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    last_key_line = text.rfind("\n", 0, listed.value[-1].start_mark.index) + 1
    from .rewrite import _block_end

    end = _block_end(text, last_key_line, len(dash_indent))
    at = before_trailing_blanks(text, end)
    rendered = "".join(_render_item(spec, dash_indent) for spec in todo)
    prefix = "" if at == 0 or text[at - 1] == "\n" else "\n"
    return splice(text, [(at, at, prefix + rendered)])


def _test_name(item: Any) -> str:
    if isinstance(item, yaml.ScalarNode):
        return item.value
    names = [name for name, _body in _mapping(item)]
    return names[0] if names else ""


def _spec_name(spec: Any) -> str:
    return spec if isinstance(spec, str) else next(iter(spec))


# --- creating an entry ----------------------------------------------------------


def _default_schema_path(sql_path: str, model: str) -> str:
    """Where the scaffold would put ``model``'s YAML: beside its SQL, named for it."""

    directory = PurePosixPath(sql_path).parent if sql_path else PurePosixPath("models")
    return str(directory / f"{model}.yml")


def _with_entry(content: str | None, model: str) -> str:
    """``content`` holding an entry for ``model``, adding one only if it has none."""

    if content is None or not content.strip():
        return "version: 2\n\nmodels:\n" + _render_item({"name": model}, "  ")
    root = _compose(content)
    if _model_entry(root, model) is not None:
        return content
    models = _child(root, "models")
    if models is None or not _sequence(models[1]):
        if models is not None:
            raise RewriteError("its `models:` key holds no list to add an entry to")
        prefix = "" if content.endswith("\n") else "\n"
        return content + prefix + "models:\n" + _render_item({"name": model}, "  ")
    listed = models[1]
    if listed.flow_style:
        raise RewriteError("its `models:` list is written in flow style")
    first = listed.value[0]
    line_start = content.rfind("\n", 0, first.start_mark.index) + 1
    line = content[line_start : first.start_mark.index]
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    from .rewrite import _entry_span

    end = max(_entry_span(content, item)[1] for item in listed.value)
    at = before_trailing_blanks(content, end)
    prefix = "" if at == 0 or content[at - 1] == "\n" else "\n"
    return splice(
        content, [(at, at, prefix + _render_item({"name": model}, dash_indent))]
    )


def _ensure_column(text: str, model: str, column: str) -> str:
    """``text`` with a column entry for ``column`` under ``model``."""

    if _column_locator(model, column)(_compose(text)) is not None:
        return text
    anchored = column_anchor(text, model)
    if anchored is not None:
        at, indent = anchored
        bullet = indent[: len(indent) - len(indent.lstrip(" "))]
        return splice(text, [(at, at, _render_item({"name": column}, bullet))])
    return _set_key(
        text, lambda root: _model_entry(root, model), "columns", [{"name": column}]
    )


# --- YAML text ------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Block YAML with sequences indented under their key, the way dbt docs read."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _dump(value: Any, *, flow: bool = False) -> str:
    return yaml.dump(
        value,
        Dumper=_Dumper,
        default_flow_style=flow,
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    )


def _render_pair(key: str, value: Any, indent: str) -> str:
    return "".join(f"{indent}{line}\n" for line in _dump({key: value}).splitlines())


def _render_item(value: Any, dash_indent: str) -> str:
    return "".join(f"{dash_indent}{line}\n" for line in _dump([value]).splitlines())


def _flow(spec: Any) -> str:
    if isinstance(spec, str):
        return spec
    return _dump(spec, flow=True).strip()


# --- reading decisions back -----------------------------------------------------


def _carries_declaration(files: Mapping[str, str], model: str) -> bool:
    for entry in _entries(files, {model}):
        if isinstance(entry_meta(entry).get(DEX_META_KEY), dict):
            return True
    return False


def _entries(files: Mapping[str, str], models: set[str]) -> Iterable[dict[str, Any]]:
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        if not isinstance(parsed, dict):
            continue
        for entry in parsed.get("models") or []:
            if isinstance(entry, dict) and entry.get("name") in models:
                yield entry


def decisions(files: Mapping[str, str], models: Iterable[str]) -> list[dict[str, Any]]:
    """Every assumption declared on ``models``, as the project's YAML records it.

    Read from ``config.meta.dex.assumptions`` rather than from any payload, so a
    plan, the apply that writes it and a later build all report the same list.
    """

    out: list[dict[str, Any]] = []
    for entry in _entries(files, set(models)):
        dex = entry_meta(entry).get(DEX_META_KEY)
        if not isinstance(dex, dict):
            continue
        for assumption in dex.get("assumptions") or []:
            if not isinstance(assumption, dict):
                continue
            out.append(
                {
                    "model": entry["name"],
                    "decision": assumption.get("decision"),
                    "chosen": assumption.get("chosen"),
                    "evidence": assumption.get("evidence"),
                }
            )
    return out


def touched_models(edits: Iterable[PlanEdit], declared: Iterable[str] = ()) -> set[str]:
    """The models a set of edits writes SQL or YAML for, plus any declared."""

    models = set(declared)
    for edit in edits:
        if edit.op is not EditOp.UPSERT or edit.new_content is None:
            continue
        if edit.kind is EditKind.MODEL_SQL and edit.path.endswith(".sql"):
            models.add(PurePosixPath(edit.path).stem)
        elif edit.kind in (EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML):
            for block in yaml_blocks(edit.new_content):
                if block.form == "yaml_model_entry":
                    models.add(block.name)
    return models
