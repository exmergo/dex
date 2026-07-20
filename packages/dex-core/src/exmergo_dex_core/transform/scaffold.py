"""Deterministic authoring paths that need no agent-written content.

Staging-model skeletons from the `.dex/` exploration cache: given a profiled
table, emit a `stg_<table>.sql` skeleton (explicit column list over a source())
and its per-model YAML with key tests and PII flags propagated into column
`meta`. The cache is the only place PII flags live, so this is the mechanical
bridge that carries them into emitted dbt; the agent then refines the skeleton
through the normal edits-file flow.

Per-model YAML files (plus one shared sources file) keep the scaffold merge-free:
it never has to rewrite an existing hand-written schema.yml.

Shipped macros: dbt macro files carried as package assets and scaffolded into
the user's project through the same plan/apply flow (`transform macro <name>`).
The user's copy is theirs to edit; re-scaffolding proposes a diff back to the
shipped version.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from ..cache import Dataset, DexStore
from ..dbt_project import DbtProjectView
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
}


class ScaffoldError(Exception):
    pass


def scaffold_edits(tables: list[str], repo_root: Path | str = ".") -> list[PlanEdit]:
    """Build the plan edits that scaffold staging models for the named tables."""

    cache = DexStore(repo_root).load_cache()
    if cache is None:
        raise ScaffoldError(
            "no .dex/cache.json; run `explore map` first so the scaffold has "
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

    edits = [_sources_edit(datasets)]
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
            e.kind is EditKind.MODEL_SQL and f"{name}(" in e.new_content for e in edits
        )
        if not called:
            continue
        defined_in_project = any(f"macro {name}(" in f.content for f in macro_files)
        defined_in_plan = any(
            e.kind is EditKind.MACRO_SQL and f"macro {name}(" in e.new_content
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


def _sources_edit(datasets: list[Dataset]) -> PlanEdit:
    # One shared sources file for everything dex scaffolds, so per-table YAML
    # files never redeclare (and thus never collide on) the source name.
    schemas: dict[str, list[str]] = {}
    for dataset in datasets:
        schema = _source_schema(dataset.identifier)
        schemas.setdefault(schema, []).append(_table_name(dataset.identifier))

    lines = ["version: 2", "", "sources:"]
    for schema in sorted(schemas):
        lines += [
            f"  - name: {schema}",
            f"    schema: {schema}",
            "    tables:",
        ]
        lines += [f"      - name: {table}" for table in sorted(schemas[schema])]
    return PlanEdit(
        path=_SOURCES_FILE,
        kind=EditKind.SCHEMA_YML,
        new_content="\n".join(lines) + "\n",
    )


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
