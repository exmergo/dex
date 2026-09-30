"""Deterministic authoring paths that need no agent-written content.

Staging-model skeletons from the exploration cache: given a profiled table,
emit a `stg_<table>.sql` skeleton (explicit column list over a source())
and its per-model YAML with key tests and PII flags propagated into column
`meta`. The cache is the only place PII flags live, so this is the mechanical
bridge that carries them into emitted dbt; the agent then refines the skeleton
through the normal edits-file flow.

Per-model YAML files keep the scaffold merge-free: it never has to rewrite an
existing hand-written schema.yml. The one shared sources file is the exception,
since every scaffolded model needs it to declare the same source consistently;
`_merge_sources` inserts what a call adds into it rather than reprinting it.

Shipped macros: dbt macro files carried as package assets and scaffolded into
the user's project through the same plan/apply flow (`transform macro <name>`).
The user's copy is theirs to edit; re-scaffolding proposes a diff back to the
shipped version.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

from ..cache import Dataset
from ..dbt_project import DbtProjectError, DbtProjectView
from ..dbt_project import load as load_dbt_project
from ..errors import DexError
from ..storage import ExploreStore, readable_cache
from .plans import EditKind, PlanEdit

_SOURCES_FILE = "models/staging/_dex_sources.yml"

# name -> one-line description, surfaced by `transform macro` with no argument.
MACRO_ASSETS: dict[str, str] = {
    "generate_schema_name": (
        "route each model layer to its own schema, <layer>_<target name> "
        "(staging_dev / intermediate_dev / marts_dev on the dev target); "
        "models with no custom schema fall back to target.schema"
    ),
    "unpivot_json_object": (
        "unpivot a JSON object column with dynamic keys into (key, value) "
        "rows, top-level keys only; native semi-structured value type on "
        "every connector"
    ),
    "drop_orphan_relations": (
        "drop named warehouse relations that no longer have a backing model "
        "or source; dry-run by default, refuses to run at all if any named "
        "relation is still a live model, seed, or snapshot"
    ),
}


class ScaffoldError(DexError):
    pass


def scaffold_edits(
    tables: list[str], store: ExploreStore, project_dir: Path | None = None
) -> list[PlanEdit]:
    """Build the plan edits that scaffold staging models for the named tables.

    ``project_dir``, when given, lets the shared sources file be merged with
    whatever it already declares (see `_sources_edit`) rather than reprinted
    from only this call's tables; omitted, the first call in a project with no
    sources file yet behaves exactly as before.
    """

    cache = readable_cache(store)
    if cache is None:
        raise ScaffoldError(
            "no exploration cache yet; run `explore map` first so the scaffold has "
            "profiles and PII flags to build from"
        )

    datasets = [_resolve_dataset(cache.datasets, name) for name in tables]
    unprofiled = [d.identifier for d in datasets if not d.columns]
    if unprofiled:
        raise ScaffoldError(
            "no column profiles cached for: "
            + ", ".join(unprofiled)
            + "; re-run `explore map` (or `explore profile`) on them first"
        )

    sources_edit = _sources_edit(datasets, _existing_sources_content(project_dir))
    edits = [sources_edit] if sources_edit is not None else []
    for dataset in datasets:
        edits.extend(model_edits(dataset))
    return edits


def macro_edit(name: str, macro_dir: str) -> PlanEdit:
    """The plan edit that scaffolds a shipped macro into the project."""

    if name not in MACRO_ASSETS:
        raise ScaffoldError(
            f"no shipped macro named '{name}'; available: "
            + ", ".join(sorted(MACRO_ASSETS))
        )
    asset = (
        resources.files("exmergo_dex_core.transform")
        / "assets"
        / "macros"
        / f"{name}.sql"
    )
    return PlanEdit(
        path=f"{macro_dir}/{name}.sql",
        kind=EditKind.MACRO_SQL,
        new_content=asset.read_text(encoding="utf-8"),
    )


def missing_macro_warnings(edits: list[PlanEdit], view: DbtProjectView) -> list[str]:
    """Warn when a planned model calls a shipped macro the project lacks.

    A warning, never an injected edit: plans hold exactly what their caller
    submitted. The check is presence-based (the call spelled anywhere in the
    model, the definition spelled anywhere under a macro path or in this same
    plan), which is as much as static text can say."""

    warnings: list[str] = []
    macro_files = [
        f
        for f in view.files.values()
        if any(f.path.startswith(f"{mp}/") for mp in view.macro_paths)
    ]
    for name in MACRO_ASSETS:
        called = any(
            e.kind is EditKind.MODEL_SQL
            and e.new_content is not None
            and f"{name}(" in e.new_content
            for e in edits
        )
        if not called:
            continue
        defined_in_project = any(f"macro {name}(" in f.content for f in macro_files)
        defined_in_plan = any(
            e.kind is EditKind.MACRO_SQL
            and e.new_content is not None
            and f"macro {name}(" in e.new_content
            for e in edits
        )
        if not defined_in_project and not defined_in_plan:
            warnings.append(
                f"a planned model calls {name}() but the project has no such "
                f"macro; run `transform macro {name}` and apply it first"
            )
    return warnings


def model_edits(dataset: Dataset) -> list[PlanEdit]:
    """The scaffold pair (model SQL + per-model YAML) for one profiled dataset.

    Shared with maintain's reconcile, which regenerates a staging model from a
    drift-patched dataset without touching the shared sources file.
    """

    table = _table_name(dataset.identifier)
    return [
        PlanEdit(
            path=f"models/staging/stg_{table}.sql",
            kind=EditKind.MODEL_SQL,
            new_content=_model_sql(dataset),
        ),
        PlanEdit(
            path=f"models/staging/stg_{table}.yml",
            kind=EditKind.SCHEMA_YML,
            new_content=_model_yaml(dataset),
        ),
    ]


# --- helpers -----------------------------------------------------------------


def _resolve_dataset(datasets: list[Dataset], name: str) -> Dataset:
    matches = sorted(
        {
            d.identifier
            for d in datasets
            if d.identifier == name
            or d.identifier.endswith(f".{name}")
            or d.identifier.split(".")[-1] == name
        }
    )
    if not matches:
        raise ScaffoldError(f"no cached object named '{name}'; run `explore map` first")
    if len(matches) > 1:
        raise ScaffoldError(f"'{name}' is ambiguous: {', '.join(matches)}; qualify it")
    by_id = {d.identifier: d for d in datasets}
    return by_id[matches[0]]


def _table_name(identifier: str) -> str:
    return identifier.split(".")[-1]


def _source_schema(identifier: str) -> str:
    parts = identifier.split(".")
    return parts[-2] if len(parts) >= 2 else "main"


def _sources_edit(
    datasets: list[Dataset], existing_content: str | None
) -> PlanEdit | None:
    requested: dict[str, set[str]] = {}
    for dataset in datasets:
        requested.setdefault(_source_schema(dataset.identifier), set()).add(
            _table_name(dataset.identifier)
        )
    content = _merge_sources(existing_content, requested)
    if content == existing_content:
        return None
    return PlanEdit(
        path=_SOURCES_FILE,
        kind=EditKind.SCHEMA_YML,
        new_content=content,
    )


def _merge_sources(content: str | None, requested: dict[str, set[str]]) -> str:
    """Insert missing declarations without serializing existing user content.

    Match the logical source name used by the generated source() calls, not
    its physical schema: several independent sources can share a schema.
    YAML marks locate block insertions; unsupported shapes refuse a rewrite.
    """
    original = content or "version: 2\n\n"
    try:
        root = yaml.compose(original)
    except yaml.YAMLError as exc:
        raise ScaffoldError("cannot merge _dex_sources.yml: invalid YAML") from exc

    def fields(node):
        if not isinstance(node, yaml.MappingNode):
            raise ScaffoldError("cannot merge _dex_sources.yml: expected a mapping")
        result = {}
        for key, value in node.value:
            if key.value in result or key.value == "<<":
                raise ScaffoldError("cannot merge duplicate keys or YAML merge keys")
            result[key.value] = value
        return result

    def scalar(value: str) -> str:
        return yaml.safe_dump(value, default_flow_style=True).split("\n...")[0].strip()

    def line_start(node):
        return original.rfind("\n", 0, node.start_mark.index) + 1

    edits: list[tuple[int, int, str]] = []
    newline = "\r\n" if "\r\n" in original else "\n"
    sources = fields(root).get("sources")
    if sources is not None and not isinstance(sources, yaml.SequenceNode):
        raise ScaffoldError("cannot merge _dex_sources.yml: sources must be a list")
    by_name = {}
    for source in sources.value if sources is not None else []:
        source_fields = fields(source)
        name = source_fields.get("name")
        if not isinstance(name, yaml.ScalarNode) or name.value in by_name:
            raise ScaffoldError("cannot merge unnamed or duplicate sources")
        by_name[name.value] = (source, source_fields)

    new_sources = []
    for name, tables in sorted(requested.items()):
        if name not in by_name:
            new_sources.append((name, sorted(tables)))
            continue
        source, source_fields = by_name[name]
        table_node = source_fields.get("tables")
        if table_node is not None and not isinstance(table_node, yaml.SequenceNode):
            raise ScaffoldError("cannot merge _dex_sources.yml: tables must be a list")
        declared = set()
        for table in table_node.value if table_node is not None else []:
            table_name = fields(table).get("name")
            if not isinstance(table_name, yaml.ScalarNode):
                raise ScaffoldError("cannot merge a table without a name")
            declared.add(table_name.value)
        missing = sorted(tables - declared)
        if not missing:
            continue
        if root.flow_style or sources.flow_style or source.flow_style:
            raise ScaffoldError("cannot extend flow-style sources; use block YAML")
        if table_node is not None and table_node.value:
            if table_node.flow_style:
                raise ScaffoldError("cannot extend flow-style tables; use block YAML")
            first = table_node.value[0]
            indent = first.start_mark.column - 2
            offset = line_start(first)
            addition = "".join(
                " " * indent + "- name: " + scalar(t) + newline for t in missing
            )
            edits.append((offset, offset, addition))
        elif table_node is not None:
            indent = source.start_mark.column + 2
            addition = newline + "".join(
                " " * indent + "- name: " + scalar(t) + newline for t in missing
            )
            edits.append(
                (
                    table_node.start_mark.index,
                    table_node.end_mark.index,
                    addition.rstrip("\r\n"),
                )
            )
        else:
            # Insert the field before the second key, or after the source's
            # final line when name is its only key.
            if len(source.value) > 1:
                offset = line_start(source.value[1][0])
            else:
                end = original.find("\n", source.value[0][1].end_mark.index)
                offset = end + 1 if end >= 0 else len(original)
            indent = source.start_mark.column
            addition = " " * indent + "tables:" + newline
            addition += "".join(
                " " * (indent + 2) + "- name: " + scalar(t) + newline for t in missing
            )
            if offset and original[offset - 1] != "\n":
                addition = newline + addition
            edits.append((offset, offset, addition))

    if new_sources:
        if root.flow_style or (
            sources is not None and sources.flow_style and sources.value
        ):
            raise ScaffoldError("cannot extend flow-style sources; use block YAML")
        indent = (
            sources.value[0].start_mark.column - 2
            if sources is not None and sources.value
            else 2
        )
        addition = ""
        for name, tables in new_sources:
            addition += " " * indent + "- name: " + scalar(name) + newline
            addition += " " * (indent + 2) + "schema: " + scalar(name) + newline
            addition += " " * (indent + 2) + "tables:" + newline
            addition += "".join(
                " " * (indent + 4) + "- name: " + scalar(t) + newline for t in tables
            )
        if sources is not None and sources.value:
            offset = line_start(sources.value[0])
            edits.append((offset, offset, addition))
        elif sources is not None:
            edits.append(
                (
                    sources.start_mark.index,
                    sources.end_mark.index,
                    newline + addition.rstrip("\r\n"),
                )
            )
        else:
            prefix = "" if original.endswith("\n") else newline
            edits.append(
                (len(original), len(original), prefix + "sources:" + newline + addition)
            )
    if edits and any(
        isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None)
        for event in yaml.parse(original)
    ):
        raise ScaffoldError("cannot extend YAML anchors or aliases; expand them first")
    for start, end, addition in sorted(edits, reverse=True):
        original = original[:start] + addition + original[end:]
    return original


def _existing_sources_content(project_dir: Path | None) -> str | None:
    """The shared sources file's current content, or ``None`` if there is none.

    A missing dbt project (scaffold run before `transform init`) is not an
    error here: it means there is nothing to merge with yet, the same as a
    project that exists but has not scaffolded a source file so far.
    """

    if project_dir is None:
        return None
    try:
        view = load_dbt_project(project_dir)
    except DbtProjectError:
        return None
    existing = view.files.get(_SOURCES_FILE)
    return existing.content if existing is not None else None


def _model_sql(dataset: Dataset) -> str:
    table = _table_name(dataset.identifier)
    schema = _source_schema(dataset.identifier)
    columns = ",\n".join(f"        {c.name}" for c in dataset.columns)
    # This renders a dbt model source file, never SQL that gets executed; the
    # interpolated names come from the adapter's own catalog metadata.
    return (
        "with source as (\n"  # noqa: S608
        f"    select * from {{{{ source('{schema}', '{table}') }}}}\n"
        "),\n\n"
        "renamed as (\n"
        "    select\n"
        f"{columns}\n"
        "    from source\n"
        ")\n\n"
        "select * from renamed\n"
    )


def _model_yaml(dataset: Dataset) -> str:
    table = _table_name(dataset.identifier)
    key_columns = set(dataset.candidate_keys[0]) if dataset.candidate_keys else set()

    lines = ["version: 2", "", "models:", f"  - name: stg_{table}"]
    if any(c.pii for c in dataset.columns):
        lines += ["    meta:", "      contains_pii: true"]
    lines.append("    columns:")
    for column in dataset.columns:
        lines.append(f"      - name: {column.name}")
        if column.pii is not None:
            # The flag propagates, never an example value (PII is flagged, not
            # surfaced); confidence is the profiler's, recorded for reviewers.
            lines += [
                "        meta:",
                "          contains_pii: true",
                f"          pii_category: {column.pii.category.value}",
            ]
        tests = []
        if column.name in key_columns:
            tests = ["unique", "not_null"] if len(key_columns) == 1 else ["not_null"]
        elif column.nullable is False or column.null_fraction == 0.0:
            tests = ["not_null"]
        if tests:
            lines.append(f"        tests: [{', '.join(tests)}]")
    return "\n".join(lines) + "\n"
