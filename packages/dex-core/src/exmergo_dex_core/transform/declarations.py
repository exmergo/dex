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

from .. import model_versions as mv
from ..dbt_project import DbtProjectView
from ..guards.pii_meta import entry_meta

# The declaration model and its parser live in a leaf (#927), so a semantic
# layer's own authoring route can read a declaration without importing the dbt
# rewriter and sqlglot behind it. Re-exported here, where the dbt callers look.
from ..semantic_declarations import Additivity as Additivity
from ..semantic_declarations import Assumption as Assumption
from ..semantic_declarations import ColumnDeclaration as ColumnDeclaration
from ..semantic_declarations import Declaration as Declaration
from ..semantic_declarations import DeclarationError as DeclarationError
from ..semantic_declarations import Evidence as Evidence
from ..semantic_declarations import Population as Population
from ..semantic_declarations import PopulationFilter as PopulationFilter
from ..semantic_declarations import Role as Role
from ..semantic_declarations import parse_declarations as parse_declarations
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


# --- where a model is declared --------------------------------------------------


def schema_path(files: Mapping[str, str], model: str) -> str | None:
    """The YAML file whose ``models:`` list declares ``model``, if one does.

    The lookup `transform place` and `maintain reconcile` share, so the three
    agree on where a model is declared. Searched by content rather than derived
    from a naming convention, because a project is free to declare every staging
    model in one ``_staging.yml``.
    """

    base, version = mv.split_label(model)
    if version is not None:
        versioned = mv.lookup(mv.cached_version_map(files), base)
        if versioned is not None:
            return versioned.yaml_path
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
    """The ``.sql`` file that defines ``model``, if the project has one.

    A version label (``dim_teams.v2``) is that version's file, and the bare name
    of a versioned model is its default version's, the file an unversioned
    ``ref()`` reads (#895).
    """

    versioned_path = _version_sql_path(files, model_paths, model)
    if versioned_path is not None:
        return versioned_path or None
    roots = [PurePosixPath(p) for p in model_paths]
    for path in sorted(files):
        candidate = PurePosixPath(path)
        if candidate.suffix != ".sql" or candidate.stem != model:
            continue
        if any(root == candidate or root in candidate.parents for root in roots):
            return path
    return None


def _version_sql_path(
    files: Mapping[str, str], model_paths: Iterable[str], model: str
) -> str | None:
    """A versioned model's file for ``model``: ``None`` when ``model`` names no
    versioned model, and ``""`` when it names one no ``.sql`` file builds."""

    vmap = mv.cached_version_map(files, list(model_paths))
    if not vmap:
        return None
    base, version = mv.split_label(model)
    versioned = mv.lookup(vmap, base)
    if versioned is None:
        return None
    item = versioned.version(version if version is not None else versioned.default)
    if item is None or item.file is None or not item.file.endswith(".sql"):
        return ""
    return item.file


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
            files[path] = render(files.get(path), declaration, notes=warnings)
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
    vmap = mv.cached_version_map(files, view.model_paths)
    named = mv.expand(declared, vmap)
    warnings: list[str] = []
    for edit in edits:
        if (
            edit.kind is not EditKind.MODEL_SQL
            or edit.op is not EditOp.UPSERT
            or not edit.path.endswith(".sql")
        ):
            continue
        model = mv.label_for_file(edit.path, vmap) or PurePosixPath(edit.path).stem
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
    warnings: list[str] = []
    target = _version_target(declaration, files, view)
    if target is not None:
        sql_path, produced, unread = target
        warnings.extend(
            f"{path}: the SELECT list could not be read statically (a `select *` "
            f"or a macro standing in for a column), so the columns declared for "
            f"'{model}' were not checked against it"
            for path in unread
        )
    else:
        sql_path = model_sql_path(files, view.model_paths, model)
        if sql_path is None:
            raise DeclarationError(
                f"'{model}' is not a model in this project or in this plan; declare "
                "a model the project has, or add its SQL to the same payload"
            )
        produced = select_columns(files[sql_path])
        if produced is None:
            warnings.append(
                f"{sql_path}: the SELECT list could not be read statically (a "
                f"`select *` or a macro standing in for a column), so the columns "
                f"declared for '{model}' were not checked against it"
            )
    if produced is not None:
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
        parent = declaration.population.parent
        if parent is not None:
            warnings.extend(
                _check_parent(model, declaration, parent, produced, files, view)
            )

    if declaration.history is not None:
        warnings.extend(
            _check_history(
                model, declaration, produced, files, view, cache, pii_overrides
            )
        )

    declared_columns = _declared_column_roles(files, declaration.model)
    declared_columns.update(name.lower() for name in declaration.columns)
    warnings.extend(_partial_intent(declaration, produced, declared_columns))
    return warnings


#: Model-level dex keys dbt merges across a version and its shared entry by
#: concatenating lists and merging mappings, so one stated at both levels is read
#: back as neither statement.
_MERGED_KEYS = ("grain", "population", "assumptions", "history")


def _version_target(
    declaration: Declaration, files: Mapping[str, str], view: DbtProjectView
) -> tuple[str, set[str] | None, list[str]] | None:
    """Where a declaration on a versioned model lands, and what it may name (#895).

    ``None`` when the declaration names no versioned model. Otherwise the file
    the column checks read (a version's SQL, or the YAML entry for the shared
    entry), the columns the declaration may name, and the files whose SELECT list
    could not be read. ``dim_teams.v2`` names one version; the bare
    ``dim_teams`` names the shared top-level entry every version inherits, so its
    columns are those any version produces and its grain one every version
    produces. A version's file stem (``dim_teams_v2``) is refused with the label,
    because a declaration under that name would create a second, plain entry.
    """

    model = declaration.model
    vmap = mv.cached_version_map(files, view.model_paths)
    base, version = mv.split_label(model)
    versioned = mv.lookup(vmap, base)
    if versioned is None:
        if version is not None and "." in model:
            raise DeclarationError(
                f"'{model}' names a version, and the project declares no versioned "
                f"model '{base}'; declare '{base}' under `models:` with a "
                "`versions:` list first"
            )
        label = mv.label_for_stem(model, vmap)
        if label is not None:
            raise DeclarationError(
                f"'{model}' is the file that builds '{label}', a version of a "
                f'versioned model; declare it as "model": "{label}" so it '
                "renders into that version's entry"
            )
        return None

    model_dex = set(_model_dex(declaration))
    if version is not None:
        item = versioned.version(version)
        if item is None:
            raise DeclarationError(
                f"'{model}' names version {version} of '{versioned.base}', which "
                f"lists versions {', '.join(v.v for v in versioned.versions)}"
            )
        shared = _dex_of(versioned.entry)
        clash = sorted(model_dex & set(shared) & set(_MERGED_KEYS))
        if clash:
            raise DeclarationError(
                f"'{versioned.base}' states {', '.join(clash)} on its shared "
                f"top-level entry, which dbt merges into every version (lists "
                f"concatenated), so a {', '.join(clash)} declared on '{model}' "
                "would be read back as neither. Move it off the shared entry and "
                "declare it on each version instead"
            )
        if item.file is None or not item.file.endswith(".sql"):
            return (versioned.yaml_path, None, [item.file] if item.file else [])
        produced = select_columns(files[item.file])
        return (item.file, produced, [] if produced is not None else [item.file])

    clash = sorted(
        {
            key
            for item in versioned.versions
            for key in _dex_of(item.item)
            if key in model_dex and key in _MERGED_KEYS
        }
    )
    if clash:
        raise DeclarationError(
            f"a version of '{model}' states {', '.join(clash)} itself, and dbt "
            "merges the shared entry into every version (lists concatenated), so "
            f"a {', '.join(clash)} declared on the shared '{model}' would be read "
            "back as neither. Declare it on each version (`<model>.v<N>`) instead"
        )
    union: set[str] = set()
    every: set[str] | None = None
    unread: list[str] = []
    for item in versioned.versions:
        produced = (
            select_columns(files[item.file])
            if item.file is not None and item.file.endswith(".sql")
            else None
        )
        if produced is None:
            unread.append(item.file or versioned.yaml_path)
            continue
        union |= produced
        every = produced if every is None else every & produced
    if unread:
        return (versioned.yaml_path, None, unread)
    missing_grain = [
        g
        for g in declaration.grain or []
        if every is not None and g.lower() not in every
    ]
    if missing_grain:
        raise DeclarationError(
            f"'{model}' is versioned, and a grain declared on its shared entry "
            f"applies to every version, but not every version produces "
            f"{', '.join(missing_grain)}; declare the grain on each version"
        )
    return (versioned.yaml_path, union, [])


def _dex_of(node: Any) -> dict[str, Any]:
    dex = entry_meta(node).get(DEX_META_KEY) if isinstance(node, dict) else None
    return dex if isinstance(dex, dict) else {}


def _check_reference(
    model: str,
    column: str,
    target: str,
    files: Mapping[str, str],
    view: DbtProjectView,
    *,
    claim: str | None = None,
) -> None:
    """Refuse a reference whose parent or parent column the project lacks.

    ``claim`` is how the refusal names what made the reference, so a
    ``population.parent`` key reads as that rather than as a column's
    ``references``."""

    said = claim or f"'{model}.{column}' references '{target}'"
    parent, _, parent_column = target.rpartition(".")
    if not parent or not parent_column:
        raise DeclarationError(f"{said}; write the target as <model>.<column>")
    parent_sql = model_sql_path(files, view.model_paths, parent)
    if parent_sql is not None:
        produced = select_columns(files[parent_sql])
        if produced is not None and parent_column.lower() not in produced:
            raise DeclarationError(
                f"{said}, and {parent_sql} does not produce a column '{parent_column}'"
            )
        return
    if _is_seed_or_snapshot(files, view, parent):
        return
    raise DeclarationError(
        f"{said}, and the project has no model, seed or snapshot named '{parent}'"
    )


def _check_parent(
    model: str,
    declaration: Declaration,
    parent: Any,
    produced: set[str] | None,
    files: Mapping[str, str],
    view: DbtProjectView,
) -> list[str]:
    """Validate ``population.parent`` (#857): every output key column is one the
    model produces, and every parent column is one the parent produces.

    A parent that is the model itself is refused: a key set always equals
    itself, so the check would prove nothing. A key that is not the declared
    grain is a warning rather than a refusal, since a model may legitimately
    hold one row per parent at a grain written differently, but the check then
    counts entities the grain does not name.
    """

    if parent.model.lower() == model.lower():
        raise DeclarationError(
            f"'{model}' declares itself as its population.parent; name the model, "
            "seed or snapshot its rows are drawn from"
        )
    if produced is not None:
        unknown = [c for c in parent.key if c.lower() not in produced]
        if unknown:
            raise DeclarationError(
                f"'{model}' declares population.parent key column(s) "
                f"{', '.join(unknown)} that its SELECT list does not produce; the "
                "key maps output columns to the parent's"
            )
    for output_column, parent_column in parent.key.items():
        _check_reference(
            model,
            output_column,
            f"{parent.model}.{parent_column}",
            files,
            view,
            claim=(
                f"'{model}' declares population.parent {parent.model} keyed "
                f"{output_column} -> {parent_column}"
            ),
        )
    grain = [g.lower() for g in declaration.grain or []]
    keys = [k.lower() for k in parent.key]
    if grain and sorted(grain) != sorted(keys):
        return [
            f"'{model}' declares population.parent keyed on {', '.join(parent.key)} "
            f"but its grain is {', '.join(declaration.grain or [])}; the parent "
            "check counts entities by the key, so the two describe different rows "
            "unless the key is the grain"
        ]
    return []


def _check_history(
    model: str,
    declaration: Declaration,
    produced: set[str] | None,
    files: Mapping[str, str],
    view: DbtProjectView,
    cache: Any,
    pii_overrides: Any,
) -> list[str]:
    """Validate a ``history`` block (#1027): a real snapshot, output columns the
    model produces, and a target that is safe to commit. A policy left out is a
    warning, since it is intent still owed rather than a malformed statement."""

    history = declaration.history
    if history is None:  # pragma: no cover - the caller only passes a history
        return []
    if not snapshot_defined(files, history.snapshot, view.snapshot_paths):
        raise DeclarationError(
            f"'{model}' declares history from '{history.snapshot}', and the project "
            "has no snapshot by that name; a temporal contract is verified against "
            "a dbt snapshot's versions"
        )
    measures = history.measures
    named = [
        *history.entity,
        *(
            column
            for column in (
                measures.transition_count,
                measures.latest_target_transition_at,
                measures.first_target_at,
            )
            if column is not None
        ),
    ]
    if produced is not None:
        unknown = [c for c in named if c.lower() not in produced]
        if unknown:
            raise DeclarationError(
                f"'{model}' declares history column(s) {', '.join(unknown)} that its "
                "SELECT list does not produce; `entity` keys and `measures` name "
                "output columns of the model"
            )
    try:
        _check_filters(
            model,
            Population(
                filters=[
                    PopulationFilter(
                        column=history.status.column,
                        includes=[history.status.target],
                    )
                ]
            ),
            cache,
            pii_overrides,
        )
    except DeclarationError as exc:
        raise DeclarationError(
            f"'{model}' declares history on status column "
            f"'{history.status.column}', whose target would be committed to git, "
            f"and the column looks like personal data: {exc}"
        ) from exc

    owed: list[str] = []
    if history.initial_observation is None:
        owed.append("initial_observation (`baseline` or `transition`)")
    if history.null_status is None:
        owed.append("null_status (`ignore` or `state`)")
    if history.population is None:
        owed.append("population (`ever_target` or `all_observed`)")
    timed = measures.latest_target_transition_at or measures.first_target_at
    if timed and (history.time is None or history.time.provenance is None):
        owed.append("time, with its provenance (`business` or `capture`)")
    if owed:
        return [
            f"'{model}' declares history without stating {', '.join(owed)}; Dex "
            "assumes no temporal policy, so verification reports what depends on "
            "it as unchecked"
        ]
    return []


def _is_seed_or_snapshot(
    files: Mapping[str, str], view: DbtProjectView, name: str
) -> bool:
    seed_roots = [PurePosixPath(p) for p in view.seed_paths]
    for path in files:
        candidate = PurePosixPath(path)
        if (
            candidate.suffix == ".csv"
            and candidate.stem == name
            and any(root in candidate.parents for root in seed_roots)
        ):
            return True
    return snapshot_defined(files, name, view.snapshot_paths)


def snapshot_defined(
    files: Mapping[str, str], name: str, snapshot_paths: Iterable[str] | None = None
) -> bool:
    """Whether the project defines a snapshot called ``name``: a ``{% snapshot %}``
    block (under ``snapshot_paths``, where given), or a ``snapshots:`` entry in a
    YAML file, which is how dbt 1.9 and later define one."""

    roots = (
        None if snapshot_paths is None else [PurePosixPath(p) for p in snapshot_paths]
    )
    block = re.compile(rf"{{%-?\s*snapshot\s+{re.escape(name)}\s*-?%}}")
    for path, content in files.items():
        candidate = PurePosixPath(path)
        if candidate.suffix == ".sql":
            placed = roots is None or any(r in candidate.parents for r in roots)
            if placed and block.search(content):
                return True
        elif candidate.suffix in (".yml", ".yaml") and "snapshots" in content:
            try:
                parsed = yaml.safe_load(content)
            except yaml.YAMLError:
                continue
            entries = parsed.get("snapshots") if isinstance(parsed, dict) else None
            if any(
                isinstance(entry, dict) and entry.get("name") == name
                for entry in (entries if isinstance(entries, list) else [])
            ):
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


def _declared_column_roles(files: Mapping[str, str], model: str) -> set[str]:
    """Column names that already have a role in the model's YAML declaration."""
    declared: set[str] = set()

    for _label, entry in _entries(files, {model}):
        for column in entry.get("columns") or []:
            if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                continue

            column_dex = entry_meta(column).get(DEX_META_KEY)
            if isinstance(column_dex, dict) and column_dex.get("role"):
                declared.add(column["name"].lower())

    return declared


def _partial_intent(
    declaration: Declaration,
    produced: set[str] | None,
    declared_columns: set[str] | None = None,
) -> list[str]:
    # A missing model-level field (description, grain, population, assumptions)
    # is warned once per field by `drafting`, which reads the post-change YAML
    # and so also counts what the entry declared before this payload.
    warnings = []

    if produced is not None:
        declared = (
            {column.lower() for column in declaration.columns}
            if declared_columns is None
            else {column.lower() for column in declared_columns}
        )
        undeclared = sorted(column for column in produced if column not in declared)

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


def render(
    content: str | None, declaration: Declaration, *, notes: list[str] | None = None
) -> str:
    """``content`` with ``declaration`` spliced into its entry for the model.

    Each key is written by its own splice against a fresh parse of the text the
    previous splice produced, so no offset is ever measured against bytes that
    have since moved. A version label (``dim_teams.v2``) renders into that item
    of the model's ``versions:`` list (#895); ``notes`` collects what a reviewer
    should be told about how (a column forked from the shared entry).
    """

    model = declaration.model
    base, version = mv.split_label(model)
    if version is not None and content is not None:
        return _render_version(content, declaration, base, version, notes)
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


def _render_version(
    content: str,
    declaration: Declaration,
    base: str,
    version: str,
    notes: list[str] | None,
) -> str:
    """Render a declaration into one item of ``base``'s ``versions:`` list.

    The version's own ``description`` and ``config.meta.dex`` take the model
    facts, and its own ``columns`` the column facts. A column the version only
    inherits from the shared entry is overridden the way dbt reads an override:
    excluded from the version's ``include``/``exclude`` element and written as
    the version's own entry, carrying the inherited description, tests and meta
    forward, since dbt replaces an inherited column wholesale with a same-named
    version entry and would otherwise also parse its tests twice. A fact the
    inherited entry already states forks nothing.
    """

    item = lambda root: _version_item(root, base, version)  # noqa: E731
    text = content
    if item(_compose(text)) is None:
        raise RewriteError(f"it has no version {version} under '{base}'")
    label = f"{base}.v{version}"
    if declaration.description is not None:
        text = _set_key(text, item, "description", declaration.description)
    model_dex = _model_dex(declaration)
    if model_dex:
        text = _set_dex(text, item, model_dex)

    for name, column in _rendered_columns(declaration).items():
        text, owned = _ensure_version_column(text, base, version, name, column)
        if not owned:
            continue
        if owned == "forked" and notes is not None:
            notes.append(
                f"'{label}.{name}' was inherited from the shared '{base}' entry; it "
                f"is now excluded from v{version}'s include/exclude and declared as "
                f"v{version}'s own column, with the inherited description and tests "
                "carried over, so the declared facts apply to this version only"
            )
        locate = _version_column_locator(base, version, name)
        if column["description"] is not None:
            text = _set_key(text, locate, "description", column["description"])
        if column["dex"]:
            text = _set_dex(text, locate, column["dex"])
        if column["tests"]:
            text = _add_tests(text, locate, column["tests"])
    return text


def _ensure_version_column(
    text: str, base: str, version: str, name: str, column: dict[str, Any]
) -> tuple[str, str | None]:
    """The version's own entry for ``name``: ``owned``, ``forked``, ``created``,
    or ``None`` where the inherited entry already says everything declared."""

    root = _compose(text)
    if _version_column_locator(base, version, name)(root) is not None:
        return text, "owned"
    entry = _plain(_model_entry(root, base))
    item = _plain(_version_item(root, base, version))
    inherited = next(
        (c for c in mv.inherited_columns(entry, item) if c.get("name") == name),
        None,
    )
    if inherited is None:
        return _append_version_column(text, base, version, {"name": name}), "created"
    if _already_states(inherited, column):
        return text, None
    text = _exclude_from_version(text, base, version, name)
    return _append_version_column(text, base, version, dict(inherited)), "forked"


def _already_states(entry: Mapping[str, Any], column: dict[str, Any]) -> bool:
    """Whether a column entry already carries every fact ``column`` would write."""

    if (
        column["description"] is not None
        and entry.get("description") != column["description"]
    ):
        return False
    dex = _dex_of(entry)
    if any(dex.get(key) != value for key, value in column["dex"].items()):
        return False
    tests = entry.get("data_tests") or entry.get("tests") or []
    present = {
        t if isinstance(t, str) else next(iter(t), "")
        for t in tests
        if isinstance(t, (str, dict))
    }
    return all(_spec_name(spec) in present for spec in column["tests"])


def _exclude_from_version(text: str, base: str, version: str, name: str) -> str:
    """``text`` with ``name`` no longer inherited by version ``version``."""

    root = _compose(text)
    item_node = _version_item(root, base, version)
    columns = _child(item_node, "columns")
    item = _plain(item_node)
    include, exclude, position = mv.include_exclude(item)
    if isinstance(include, list):
        rewritten: dict[str, Any] = {"include": [c for c in include if c != name]}
    else:
        # A version with no include/exclude element includes everything; it
        # is written as `all`, the spelling dbt documents (an unquoted `*` is
        # a YAML alias).
        written = include if position is not None and include else "all"
        rewritten = {"include": written, "exclude": [*exclude, name]}
    if columns is None:
        return _set_key(
            text, lambda r: _version_item(r, base, version), "columns", [rewritten]
        )
    listed = columns[1]
    if listed.flow_style or not listed.value:
        raise RewriteError(f"version {version}'s `columns:` is not a block list")
    if position is not None:
        node = listed.value[position]
        start, end = _item_span(text, node)
        dash = text[start : node.start_mark.index]
        dash_indent = dash[: len(dash) - len(dash.lstrip(" "))]
        return splice(
            text,
            [
                (
                    start,
                    before_trailing_blanks(text, end),
                    _render_flow_item(rewritten, dash_indent),
                )
            ],
        )
    first = listed.value[0]
    start, _end = _item_span(text, first)
    line = text[start : first.start_mark.index]
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    return splice(text, [(start, start, _render_flow_item(rewritten, dash_indent))])


def _render_flow_item(value: dict[str, Any], dash_indent: str) -> str:
    """An include/exclude element, with its lists written inline."""

    # One line each; a scalar's dump trails a `...` document end marker.
    pairs = [
        f"{key}: {_dump(v, flow=True).splitlines()[0]}" for key, v in value.items()
    ]
    lines = [f"{dash_indent}- {pairs[0]}\n"]
    lines += [f"{dash_indent}  {pair}\n" for pair in pairs[1:]]
    return "".join(lines)


def _append_version_column(
    text: str, base: str, version: str, column: dict[str, Any]
) -> str:
    """``text`` with ``column`` appended to version ``version``'s own columns."""

    root = _compose(text)
    columns = _child(_version_item(root, base, version), "columns")
    if columns is None or not _sequence(columns[1]):
        return _set_key(
            text, lambda r: _version_item(r, base, version), "columns", [column]
        )
    listed = columns[1]
    if listed.flow_style:
        raise RewriteError(f"version {version}'s `columns:` is written in flow style")
    first = listed.value[0]
    line_start = text.rfind("\n", 0, first.start_mark.index) + 1
    line = text[line_start : first.start_mark.index]
    dash_indent = line[: len(line) - len(line.lstrip(" "))]
    end = max(_item_span(text, node)[1] for node in listed.value)
    at = before_trailing_blanks(text, end)
    prefix = "" if at == 0 or text[at - 1] == "\n" else "\n"
    return splice(text, [(at, at, prefix + _render_item(column, dash_indent))])


def _item_span(text: str, node: Any) -> tuple[int, int]:
    from .rewrite import _entry_span

    return _entry_span(text, node)


def _plain(node: Any) -> dict[str, Any]:
    if node is None:
        return {}
    value = yaml.safe_load(yaml.serialize(node))
    return value if isinstance(value, dict) else {}


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
            parent_base, parent_version = mv.split_label(parent)
            to = (
                f"ref('{parent_base}', v={parent_version})"
                if parent_version is not None
                else f"ref('{parent}')"
            )
            entry["tests"].append(
                {"relationships": {"arguments": {"to": to, "field": parent_column}}}
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
    if declaration.history is not None:
        dex["history"] = declaration.history.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True
        )
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


def _version_item(root: Any, base: str, version: str) -> Any:
    entry = _model_entry(root, base)
    for item in _sequence(dict(_mapping(entry)).get("versions")):
        value = dict(_mapping(item)).get("v")
        if isinstance(value, yaml.ScalarNode) and value.value == version:
            return item
    return None


def _version_column_locator(base: str, version: str, column: str):
    def locate(root: Any) -> Any:
        item = _version_item(root, base, version)
        for entry in _sequence(dict(_mapping(item)).get("columns")):
            if _scalar_value(dict(_mapping(entry)).get("name")) == column:
                return entry
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
    if isinstance(existing, dict):
        merged = _merge_population(existing, merged)

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


def _merge_population(existing: Mapping[str, Any], merged: dict[str, Any]) -> dict:
    """Keep ``population``'s own keys a later declaration leaves out (#857).

    A model-level key is replaced whole, but ``population`` holds three
    independent statements (a rule, filters, a parent), and a payload that adds
    a parent to a model whose filters were declared earlier would otherwise
    erase the filters. One level, the same rule one level down."""

    before, after = existing.get("population"), merged.get("population")
    if isinstance(before, dict) and isinstance(after, dict) and before is not after:
        merged["population"] = {**before, **after}
    return merged


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
    from ..yaml_spans import block_end

    end = block_end(text, last_key_line, len(dash_indent))
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
    return any(
        isinstance(entry_meta(entry).get(DEX_META_KEY), dict)
        for _label, entry in _entries(files, {model})
    )


def _entries(
    files: Mapping[str, str], models: set[str]
) -> Iterable[tuple[str, dict[str, Any]]]:
    """``(name, entry)`` for each model entry ``models`` names.

    A version is read as dbt builds it (its effective entry, under its label),
    so a name a version answers to (the label, the bare versioned name, or the
    version's file stem) yields what that version declares (#895).
    """

    wanted = mv.expand(models, mv.cached_version_map(files))
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
            if not isinstance(entry, dict):
                continue
            for label, variant in mv.entry_variants(entry):
                if label in wanted:
                    yield label, variant


def decisions(files: Mapping[str, str], models: Iterable[str]) -> list[dict[str, Any]]:
    """Every assumption declared on ``models``, as the project's YAML records it.

    Read from ``config.meta.dex.assumptions`` rather than from any payload, so a
    plan, the apply that writes it and a later build all report the same list.
    """

    out: list[dict[str, Any]] = []
    for label, entry in _entries(files, set(models)):
        dex = entry_meta(entry).get(DEX_META_KEY)
        if not isinstance(dex, dict):
            continue
        for assumption in dex.get("assumptions") or []:
            if not isinstance(assumption, dict):
                continue
            out.append(
                {
                    "model": label,
                    "decision": assumption.get("decision"),
                    "chosen": assumption.get("chosen"),
                    "evidence": assumption.get("evidence"),
                    **(
                        {"column": assumption["column"]}
                        if isinstance(assumption.get("column"), str)
                        else {}
                    ),
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
