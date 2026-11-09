"""Native dbt model versions in a plan and a build (#859).

A request for "v2 of ``dim_teams``" is met by native dbt versions: one YAML entry
with a ``versions:`` list, a file per version, and references that resolve the
way dbt resolves them, an unversioned ``ref('dim_teams')`` to the default and
``ref('dim_teams', v=2)`` to v2. An independent ``dim_teams_v2.sql`` builds, and
its declaration and tests can pass, while none of that holds. A green build
cannot tell the two apart, so this module says which one a plan writes:

- **Before the parse**, from the files alone: what dbt would build wrong or drop
  silently is refused (a version with no file, which dbt only warns about; two
  versions on one file; an unmapped ``dim_teams.sql`` beside the versioned
  model), and the rest is reported under ``data.model_versions``: each version's
  file and relation, the default before and after, every model that refs the
  versioned model and what each ref resolves to before and after, a relation
  the warehouse would see move, and a pre-existing version whose declared
  columns change. A new ``<model>_v<N>`` file that no ``versions:`` entry maps
  is named as an independent model, not a version.
- **From dbt's own parse** of the post-change project: each version's node id,
  file and relation, the default, and every consumer's ``depends_on`` are
  confirmed in the manifest, never inferred from file names alone.
- **After a build** (``--verify --for-plan``): the same confirmation against the
  manifest the build wrote, with each version's build status.

Promotion is the same machinery: a plan that moves ``latest_version`` reports
the default and every consumer whose resolution changes, and warns, so a
promotion is never a side effect of another change.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from .. import model_versions as mv
from ..dbt_project import DbtProjectView, jinja_regions, strip_relation_quoting
from .plans import EditKind, EditOp, PlanEdit, PlanError

#: The model versions need: dbt added them in 1.5.
MIN_DBT = (1, 5)

RECIPE = "skills/transform/references/model-versions.md"

_REF_VERSION = re.compile(r"\b(?:v|version)\s*=\s*['\"]?([A-Za-z0-9_.]+)")
_SUFFIXED = re.compile(r"^(?P<base>.+)_v(?P<v>[A-Za-z0-9.]+)$")


class ModelVersionError(PlanError):
    """A versioned model the plan would leave dbt to build wrong or drop."""


# --- reading the project -----------------------------------------------------------


def _manifest_path(node: Mapping[str, Any]) -> str:
    """A manifest node's file, with slashes as every plan and version names it.

    dbt writes ``original_file_path`` with the platform's separator, so on
    Windows it reads ``models\\dim_teams_v2.sql`` and would never equal the
    ``models/dim_teams_v2.sql`` a version entry records.
    """

    return str(node.get("original_file_path") or "").replace("\\", "/")


def _files(view: DbtProjectView) -> dict[str, str]:
    return {path: source.content for path, source in view.files.items()}


def _post_change(files: Mapping[str, str], edits: Iterable[PlanEdit]) -> dict[str, str]:
    out = dict(files)
    for edit in edits:
        if edit.op is EditOp.DELETE:
            out.pop(edit.path, None)
        elif edit.new_content is not None:
            out[edit.path] = edit.new_content
    return out


def model_refs(content: str) -> list[tuple[str, str | None, str]]:
    """Each ``ref()`` a model writes: ``(name, version, as written)``.

    The two-argument package form keeps its last argument as the name. A ref
    whose name is not a literal is not reported, since nothing can say what it
    resolves to statically.
    """

    try:
        regions, text = jinja_regions(content)
    except Exception:
        return []
    out: list[tuple[str, str | None, str]] = []
    for region in regions:
        for call in region.calls:
            if call.callee != "ref" or not call.args or call.args[-1] is None:
                continue
            start = call.callee_span[0] if call.callee_span else region.start
            close = text.find(")", start)
            written = text[start : close + 1 if close != -1 else region.end].strip()
            pinned = _REF_VERSION.search(written)
            out.append((call.args[-1], pinned.group(1) if pinned else None, written))
    return out


def _relation(entry: Mapping[str, Any] | None, fallback: str) -> str:
    config = (entry or {}).get("config")
    alias = config.get("alias") if isinstance(config, dict) else None
    return alias if isinstance(alias, str) and alias else fallback


def _plain_entries(files: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Each model entry by name, read once (the first path in sorted order)."""

    out: dict[str, dict[str, Any]] = {}
    for _path, parsed in mv._yaml_documents(files):
        for entry in parsed.get("models") or []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                out.setdefault(entry["name"], entry)
    return out


def _declared_columns(entry: Mapping[str, Any] | None) -> list[str]:
    return [
        c["name"]
        for c in (entry or {}).get("columns") or []
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    ]


def _builds(
    files: Mapping[str, str],
    vmap: Mapping[str, mv.VersionedModel],
    model_paths: list[str],
) -> dict[str, dict[str, Any]]:
    """Model file -> what it builds: its model name, relation and columns."""

    out: dict[str, dict[str, Any]] = {}
    entries = _plain_entries(files)
    for stem, paths in mv.model_files(files, model_paths).items():
        for path in paths:
            label = mv.label_for_file(path, vmap)
            if label is not None:
                base, v = mv.split_label(label)
                model = mv.lookup(vmap, base)
                item = model.version(v) if model and v else None
                effective = (
                    mv.effective_entry(model.entry, item.item) if model and item else {}
                )
                out[path] = {
                    "model": label,
                    "relation": _relation(effective, f"{base}_v{v}"),
                    "columns": _declared_columns(effective),
                }
            else:
                entry = entries.get(stem)
                out[path] = {
                    "model": stem,
                    "relation": _relation(entry, stem),
                    "columns": _declared_columns(entry),
                }
    return out


def _resolves_to(
    name: str,
    version: str | None,
    vmap: Mapping[str, mv.VersionedModel],
    builds: Mapping[str, dict[str, Any]],
) -> str | None:
    if mv.lookup(vmap, name) is not None:
        return mv.ref_target(name, version, vmap)
    if version is not None:
        return None
    return name if any(b["model"] == name for b in builds.values()) else None


def _file_of(model: str | None, builds: Mapping[str, dict[str, Any]]) -> str | None:
    return next((p for p, b in builds.items() if b["model"] == model), None)


# --- the plan ---------------------------------------------------------------------


def touched(
    edits: Iterable[PlanEdit],
    before: Mapping[str, str],
    after: Mapping[str, str],
    model_paths: list[str],
) -> set[str]:
    """The versioned models a plan touches: its YAML entry, a version's file, or
    a model the plan writes that refs it. Also a model the plan versions or
    stops versioning."""

    vb = mv.version_map(before, model_paths)
    va = mv.version_map(after, model_paths)
    bases: set[str] = set()
    for edit in edits:
        if edit.kind in (EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML):
            for vmap in (vb, va):
                for base, model in vmap.items():
                    if model.yaml_path == edit.path:
                        other = va if vmap is vb else vb
                        twin = mv.lookup(other, base)
                        if twin is None or twin.entry != model.entry:
                            bases.add(base)
            continue
        if edit.kind is not EditKind.MODEL_SQL:
            continue
        for vmap in (vb, va):
            label = mv.label_for_file(edit.path, vmap)
            if label is not None:
                bases.add(mv.split_label(label)[0])
        content = after.get(edit.path) or ""
        for name, _version, _written in model_refs(content):
            versioned = mv.lookup(va, name)
            if versioned is not None:
                bases.add(versioned.base)
    return bases


def independent_models(
    edits: Iterable[PlanEdit],
    before: Mapping[str, str],
    after: Mapping[str, str],
    model_paths: list[str],
) -> list[dict[str, Any]]:
    """New ``<model>_v<N>`` files that no ``versions:`` entry maps, beside an
    existing ``<model>``: a copy, not a version."""

    va = mv.version_map(after, model_paths)
    vb = mv.version_map(before, model_paths)
    stems_before = mv.model_files(before, model_paths)
    out: list[dict[str, Any]] = []
    for edit in edits:
        if (
            edit.kind is not EditKind.MODEL_SQL
            or edit.op is not EditOp.UPSERT
            or edit.path in before
        ):
            continue
        stem = PurePosixPath(edit.path).stem
        match = _SUFFIXED.match(stem)
        if match is None or mv.label_for_file(edit.path, va) is not None:
            continue
        base = match.group("base")
        if base not in stems_before and mv.lookup(vb, base) is None:
            continue
        out.append(
            {
                "file": edit.path,
                "model": stem,
                "resembles": f"{base}.v{match.group('v')}",
                "status": "independent_model",
            }
        )
    return out


def analyze(
    view: DbtProjectView,
    edits: list[PlanEdit],
    dialect: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """``data.model_versions`` for a plan, and its warnings.

    ``None`` when the plan touches no versioned model and writes no
    ``<model>_v<N>`` copy. Raises :class:`ModelVersionError` for a versioned
    model dbt would build wrong or silently drop.
    """

    model_paths = list(view.model_paths)
    before = _files(view)
    after = _post_change(before, edits)
    bases = touched(edits, before, after, model_paths)
    copies = independent_models(edits, before, after, model_paths)
    if not bases and not copies:
        return None, []

    problems = mv.structural_problems(after, model_paths, models=bases)
    if problems:
        raise ModelVersionError(
            "; ".join(
                f"{p.path + ': ' if p.path else ''}{p.detail} ({p.code})"
                for p in problems
            )
            + f". See {RECIPE}"
        )

    vb = mv.version_map(before, model_paths)
    va = mv.version_map(after, model_paths)
    builds_before = _builds(before, vb, model_paths)
    builds_after = _builds(after, va, model_paths)
    report: dict[str, Any] = {}
    warnings: list[str] = []
    for base in sorted(bases):
        entry, notes = _model_report(
            base,
            before,
            after,
            vb,
            va,
            builds_before,
            builds_after,
            model_paths,
            dialect=dialect,
        )
        report[base] = entry
        warnings.extend(notes)
    if copies:
        report["independent_models"] = copies
        warnings.extend(
            f"{c['file']} creates '{c['model']}' as an independent model, not "
            f"version {mv.split_label(c['resembles'])[1]} of "
            f"'{mv.split_label(c['resembles'])[0]}': `ref('"
            f"{mv.split_label(c['resembles'])[0]}', v="
            f"{mv.split_label(c['resembles'])[1]})` will not resolve to it, and the "
            "default reference is unchanged. If the request asks for a new "
            f"version, declare it under `versions:` instead ({RECIPE})"
            for c in copies
        )
    return report, warnings


def _model_report(
    base: str,
    before: Mapping[str, str],
    after: Mapping[str, str],
    vb: Mapping[str, mv.VersionedModel],
    va: Mapping[str, mv.VersionedModel],
    builds_before: Mapping[str, dict[str, Any]],
    builds_after: Mapping[str, dict[str, Any]],
    model_paths: list[str],
    dialect: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """report version, consumer, relation and contract changes for base."""
    now = mv.lookup(va, base)
    was = mv.lookup(vb, base)
    warnings: list[str] = []
    out: dict[str, Any] = {}

    if now is not None:
        out["versions"] = [
            {
                "label": item.label,
                "v": item.v,
                "file": item.file,
                "file_source": item.file_source,
                "relation": builds_after.get(item.file or "", {}).get("relation"),
                "relation_source": "predicted",
                "columns": builds_after.get(item.file or "", {}).get("columns", []),
            }
            for item in now.versions
        ]
    else:
        out["versions"] = []
        out["unversioned"] = True
    out["latest_version"] = {
        "before": was.latest_version if was else None,
        "after": now.latest_version if now else None,
    }
    default_before = was.default_label if was else _plain_name(base, builds_before)
    default_after = now.default_label if now else _plain_name(base, builds_after)
    out["default"] = {"before": default_before, "after": default_after}

    consumers: list[dict[str, Any]] = []
    for path in sorted(after):
        if PurePosixPath(path).suffix not in (".sql", ".py") or base not in after[path]:
            continue
        own = builds_after.get(path, {}).get("model")
        if own is None or (own == base or mv.split_label(own)[0] == base):
            continue
        for name, version, written in model_refs(after[path]):
            if name.lower() != base.lower():
                continue
            resolved_after = _resolves_to(name, version, va, builds_after)
            resolved_before = (
                _resolves_to(name, version, vb, builds_before)
                if path in before
                else None
            )
            file_before = _file_of(resolved_before, builds_before)
            file_after = _file_of(resolved_after, builds_after)
            consumers.append(
                {
                    "model": own,
                    "path": path,
                    "ref": written,
                    "before": resolved_before,
                    "after": resolved_after,
                    "changed": path in before and file_before != file_after,
                }
            )
    out["consumers"] = consumers

    relation_changes: list[dict[str, Any]] = []
    contract_changes: list[dict[str, Any]] = []
    for path, built in builds_after.items():
        if not (built["model"] == base or mv.split_label(built["model"])[0] == base):
            continue
        prior = builds_before.get(path)
        if prior is None:
            continue
        if prior["relation"] != built["relation"]:
            relation_changes.append(
                {
                    "file": path,
                    "before": {"model": prior["model"], "relation": prior["relation"]},
                    "after": {"model": built["model"], "relation": built["relation"]},
                }
            )
            warnings.append(
                f"{path}: '{prior['model']}' was built as {prior['relation']}, and as "
                f"'{built['model']}' dbt builds it as {built['relation']}; anything "
                f"reading {prior['relation']} in the warehouse would read a table dbt "
                f"no longer updates. Pin `config: {{alias: {prior['relation']}}}` on "
                "that version to keep the name, or record the move as intended"
            )
        removed = [c for c in prior["columns"] if c not in built["columns"]]
        added = [c for c in built["columns"] if c not in prior["columns"]]
        if prior["columns"] and built["columns"] and (removed or added):
            contract_changes.append(
                {
                    "file": path,
                    "model": built["model"],
                    "removed": removed,
                    "added": added,
                }
            )
            warnings.append(
                f"{path}: '{built['model']}' (was '{prior['model']}') now declares "
                + ", ".join(
                    part
                    for part in (
                        f"no {', '.join(removed)}" if removed else "",
                        f"new {', '.join(added)}" if added else "",
                    )
                    if part
                )
                + "; a consumer pinned to it reads a different contract. Put a "
                "changed contract in a new version, unless this change is the request"
            )
    out["relation_changes"] = relation_changes
    out["contract_changes"] = contract_changes

    changed = [c for c in consumers if c["changed"]]
    if was is not None and now is not None and default_before != default_after:
        warnings.append(
            f"'{base}': an unversioned ref('{base}') now resolves to "
            f"{default_after} instead of {default_before} (a promotion); "
            + (
                f"{len(changed)} consumer(s) change what they read: "
                + ", ".join(
                    f"{c['model']} ({c['before']} -> {c['after']})" for c in changed
                )
                if changed
                else "no consumer in the project changes what it reads"
            )
        )
        if changed:
            warnings.extend(
                _check_moving_consumers(
                    changed,
                    before,
                    after,
                    va,
                    builds_before,
                    builds_after,
                    dialect=dialect,
                )
            )
    elif changed:
        warnings.append(
            f"'{base}': {len(changed)} consumer(s) read a different file after this "
            "plan: "
            + ", ".join(
                f"{c['model']} ({c['before']} -> {c['after']})" for c in changed
            )
        )
    out["manifest"] = {"status": "unchecked", "reason_code": "parse_not_run"}
    return out, warnings


def _check_moving_consumers(
    changed: list[dict[str, Any]],
    before: Mapping[str, str],
    after: Mapping[str, str],
    va: Mapping[str, mv.VersionedModel],
    builds_before: Mapping[str, dict[str, Any]],
    builds_after: Mapping[str, dict[str, Any]],
    dialect: str | None = None,
) -> list[str]:
    """warn when a promotion moves a consumer reading columns the
    new default lacks (#1124).
    """

    from pathlib import PurePosixPath

    import sqlglot
    from sqlglot import expressions as exp

    from .select_list import (
        RelationColumns,
        RelationRef,
        _tokenize_relations,
        resolve_select_list,
    )
    from .validate import strip_jinja

    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()

    def _relation_columns(relation: RelationRef) -> RelationColumns:
        if relation.kind == "ref" and len(relation.parts) == 1:
            target = _resolves_to(relation.name, relation.version, va, builds_after)
            if target:
                built = next(
                    (b for b in builds_after.values() if b["model"] == target),
                    None,
                )
                if built and built.get("columns"):
                    return RelationColumns(columns=list(built["columns"]))
                t_file = _file_of(target, builds_after)
                if t_file and t_file in after:
                    res = resolve_select_list(after[t_file], dialect=dialect)
                    if res.columns is not None:
                        return RelationColumns(columns=list(res.columns))
                    return RelationColumns(reason=res.reason)
        return RelationColumns(
            reason=f"a `*` over {relation}, which could not be resolved"
        )

    for c in changed:
        key = (c["path"], c["model"])
        if key in seen:
            continue
        seen.add(key)

        path = c["path"]
        model = c["model"]
        before_model = c["before"]
        after_model = c["after"]

        if PurePosixPath(path).suffix != ".sql":
            warnings.append(
                f"{path}: '{model}' moves from {before_model} to {after_model}, "
                f"but has no SQL to read; consumer was unchecked"
            )
            continue

        consumer_sql = after.get(path)
        if consumer_sql is None:
            warnings.append(
                f"{path}: '{model}' moves from {before_model} to {after_model}, "
                f"but the file is absent; consumer was unchecked"
            )
            continue

        resolved = resolve_select_list(
            consumer_sql,
            dialect=dialect,
            relation_columns=_relation_columns,
        )
        if resolved.columns is None:
            warnings.append(
                f"{path}: '{model}' moves from {before_model} to {after_model}, "
                f"but its SELECT list could not be read ({resolved.reason}); "
                "consumer was unchecked"
            )
            continue

        consumer_cols = {col.lower() for col in resolved.columns}
        with contextlib.suppress(Exception):
            text, _rels = _tokenize_relations(consumer_sql)
            stripped = strip_jinja(text)
            if stripped:
                parsed = sqlglot.parse_one(stripped, read=dialect or "duckdb")
                for col in parsed.find_all(exp.Column):
                    if col.name:
                        consumer_cols.add(col.name.lower())

        after_built = next(
            (b for b in builds_after.values() if b["model"] == after_model),
            None,
        )
        new_cols = {col.lower() for col in (after_built or {}).get("columns") or []}
        if not new_cols:
            after_file = _file_of(after_model, builds_after)
            if after_file and after_file in after:
                res_after = resolve_select_list(
                    after[after_file],
                    dialect=dialect,
                    relation_columns=_relation_columns,
                )
                if res_after.columns:
                    new_cols = {col.lower() for col in res_after.columns}

        if not new_cols:
            warnings.append(
                f"{path}: '{model}' moves from {before_model} to {after_model}, "
                f"but the columns of {after_model} could not be read; "
                "consumer was unchecked"
            )
            continue

        before_built = next(
            (b for b in builds_before.values() if b["model"] == before_model),
            None,
        )
        old_cols = {col.lower() for col in (before_built or {}).get("columns") or []}
        if not old_cols:
            before_file = _file_of(before_model, builds_before)
            if before_file and before_file in before:
                res_before = resolve_select_list(
                    before[before_file],
                    dialect=dialect,
                    relation_columns=_relation_columns,
                )
                if res_before.columns:
                    old_cols = {col.lower() for col in res_before.columns}

        if not old_cols:
            warnings.append(
                f"{path}: '{model}' moves from {before_model} to {after_model}, "
                f"but the columns of {before_model} could not be read; "
                "consumer was unchecked"
            )
            continue

        dropped = old_cols - new_cols
        lacks = sorted(col for col in dropped if col in consumer_cols)

        if lacks:
            col_str = ", ".join(lacks)
            warnings.append(
                f"{path}: '{model}' reads column(s) {col_str} from "
                f"{before_model}, which {after_model} (the new default) lacks"
            )

    return warnings


def _plain_name(base: str, builds: Mapping[str, dict[str, Any]]) -> str | None:
    return base if any(b["model"] == base for b in builds.values()) else None


def needs_parse(report: Mapping[str, Any] | None) -> bool:
    return bool(report)


# --- confirming against a manifest ---------------------------------------------------


def dbt_version_supported(manifest: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Whether the dbt that wrote ``manifest`` has model versions, and its version."""

    raw = (manifest.get("metadata") or {}).get("dbt_version")
    if not isinstance(raw, str):
        return True, None
    parts = re.findall(r"\d+", raw)[:2]
    if len(parts) < 2:
        return True, raw
    return (int(parts[0]), int(parts[1])) >= MIN_DBT, raw


def confirm(
    report: dict[str, Any],
    manifest: Mapping[str, Any],
    package: str,
    *,
    statuses: Mapping[str, str] | None = None,
) -> list[str]:
    """Hold ``report`` to ``manifest`` in place, returning a line per mismatch.

    Each versioned model's ``manifest`` key becomes ``confirmed`` or
    ``contradicted`` with its ``mismatches`` (``code`` and ``detail``); each
    version gains its ``unique_id`` and, where the manifest names one, its real
    ``relation``; each consumer gains the node ids it actually depends on.
    ``statuses`` (a build's unique id -> status) adds each version's build
    status. Codes: ``version_missing``, ``version_file_mismatch``,
    ``version_default_contradicted``, ``consumer_missing``,
    ``version_ref_misresolved``.
    """

    nodes = manifest.get("nodes") or {}
    by_file = {
        _manifest_path(node): uid
        for uid, node in nodes.items()
        if isinstance(node, dict) and node.get("resource_type") == "model"
    }
    lines: list[str] = []
    for base, entry in report.items():
        if base == "independent_models" or not isinstance(entry, dict):
            continue
        mismatches: list[dict[str, str]] = []

        def miss(code: str, detail: str, subject: str, into=mismatches) -> None:
            into.append({"code": code, "subject": subject, "detail": detail})

        expected_uid: dict[str, str] = {}
        default = entry.get("default", {}).get("after")
        for version in entry.get("versions") or []:
            uid = f"model.{package}.{base}.v{version['v']}"
            expected_uid[version["label"]] = uid
            node = nodes.get(uid)
            version["unique_id"] = uid
            if statuses is not None:
                version["build_status"] = statuses.get(uid, "not_run")
            if not isinstance(node, dict):
                miss("version_missing", f"no manifest node {uid}", version["label"])
                version["in_manifest"] = False
                continue
            version["in_manifest"] = True
            if version.get("file") and _manifest_path(node) != version["file"]:
                miss(
                    "version_file_mismatch",
                    f"{uid} is built from {_manifest_path(node)}, not "
                    f"{version['file']}",
                    version["label"],
                )
            relation = node.get("relation_name")
            if isinstance(relation, str) and relation:
                version["relation"] = strip_relation_quoting(relation)
                version["relation_source"] = "manifest"
            latest = node.get("latest_version")
            if (
                default is not None
                and latest is not None
                and f"{base}.v{latest}" != default
            ):
                miss(
                    "version_default_contradicted",
                    f"{uid} reports latest_version {latest}, and the default "
                    f"expected is {default}",
                    version["label"],
                )
        if entry.get("unversioned"):
            expected_uid[base] = f"model.{package}.{base}"
        for consumer in entry.get("consumers") or []:
            uid = by_file.get(consumer["path"])
            if uid is None:
                miss(
                    "consumer_missing",
                    f"no manifest node is built from {consumer['path']}",
                    consumer["model"],
                )
                continue
            consumer["unique_id"] = uid
            depends = [
                d
                for d in (nodes[uid].get("depends_on") or {}).get("nodes") or []
                if isinstance(d, str)
                and (
                    d == f"model.{package}.{base}"
                    or d.startswith(f"model.{package}.{base}.v")
                )
            ]
            consumer["depends_on"] = depends
            want = expected_uid.get(consumer.get("after") or "")
            if want is not None and want not in depends:
                miss(
                    "version_ref_misresolved",
                    f"{consumer['model']}'s {consumer['ref']} resolves to "
                    f"{', '.join(depends) or 'nothing'} in the manifest, not {want}",
                    consumer["model"],
                )
        entry["manifest"] = (
            {"status": "contradicted", "mismatches": mismatches}
            if mismatches
            else {"status": "confirmed"}
        )
        lines.extend(f"'{base}': {m['detail']}" for m in mismatches)
    return lines


#: How severe each build-time mismatch is.
_SEVERITY = {
    "version_missing": "high",
    "version_not_built": "high",
    "version_file_mismatch": "high",
    "version_default_contradicted": "high",
    "version_ref_misresolved": "high",
    "consumer_missing": "medium",
}


def records(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One record per version, default and consumer a confirmed report holds.

    Shaped like the other verification records (``model``, ``status``,
    ``finding`` or ``reason_code``), so each reaches the build's completion
    obligations. A version the build did not run is ``unchecked``
    (``not_built``), never satisfied by an earlier relation.
    """

    out: list[dict[str, Any]] = []
    for base, entry in report.items():
        if base == "independent_models" or not isinstance(entry, dict):
            continue
        manifest = entry.get("manifest") or {}
        if manifest.get("status") == "unchecked":
            out.append(
                {
                    "model": base,
                    "check": "manifest",
                    "status": "unchecked",
                    "reason_code": manifest.get("reason_code"),
                }
            )
            continue
        by_subject: dict[str, dict[str, str]] = {
            m["subject"]: m for m in manifest.get("mismatches") or []
        }
        for version in entry.get("versions") or []:
            record: dict[str, Any] = {
                "model": version["label"],
                "check": "version",
                "unique_id": version.get("unique_id"),
                "file": version.get("file"),
                "relation": version.get("relation"),
                "build_status": version.get("build_status"),
            }
            mismatch = by_subject.get(version["label"])
            status = version.get("build_status")
            if mismatch is not None:
                record.update(status="contradicted", finding=mismatch["code"])
            elif status not in (None, "success"):
                if status == "not_run":
                    record.update(status="unchecked", reason_code="not_built")
                else:
                    record.update(status="contradicted", finding="version_not_built")
            else:
                record["status"] = "satisfied"
            out.append(record)
        default = entry.get("default", {}).get("after")
        if default is not None and entry.get("versions"):
            clash = next(
                (
                    m
                    for m in manifest.get("mismatches") or []
                    if m["code"] == "version_default_contradicted"
                ),
                None,
            )
            out.append(
                {
                    "model": base,
                    "check": "default",
                    "expected": default,
                    "status": "contradicted" if clash else "satisfied",
                    **({"finding": clash["code"]} if clash else {}),
                }
            )
        for consumer in entry.get("consumers") or []:
            mismatch = by_subject.get(consumer["model"])
            out.append(
                {
                    "model": consumer["model"],
                    "check": "ref",
                    "ref": consumer["ref"],
                    "expected": consumer.get("after"),
                    "depends_on": consumer.get("depends_on", []),
                    "status": "contradicted" if mismatch else "satisfied",
                    **({"finding": mismatch["code"]} if mismatch else {}),
                }
            )
    return out


def findings(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each contradicted record as a finding: ``code``, ``model``, ``severity``,
    ``detail``."""

    out: list[dict[str, Any]] = []
    for base, entry in report.items():
        if base == "independent_models" or not isinstance(entry, dict):
            continue
        out.extend(
            {
                "code": m["code"],
                "model": m["subject"],
                "severity": _SEVERITY.get(m["code"], "high"),
                "detail": f"'{base}': {m['detail']}",
            }
            for m in (entry.get("manifest") or {}).get("mismatches") or []
        )
        out.extend(
            {
                "code": "version_not_built",
                "model": version["label"],
                "severity": "high",
                "detail": (
                    f"'{version['label']}' did not build in this run "
                    f"({version.get('build_status')})"
                ),
            }
            for version in entry.get("versions") or []
            if version.get("build_status") not in (None, "success", "not_run")
        )
    return out


def unchecked(report: dict[str, Any] | None, reason_code: str, detail: str) -> None:
    for base, entry in (report or {}).items():
        if base != "independent_models" and isinstance(entry, dict):
            entry["manifest"] = {
                "status": "unchecked",
                "reason_code": reason_code,
                "detail": detail,
            }


def build_report(
    project_files: Mapping[str, str],
    edits: Iterable[PlanEdit],
    model_paths: list[str],
) -> dict[str, Any]:
    """What a plan's versioned models should be, read from the tree it left.

    For a ``--for-plan`` build: the plan's edits name what it touched, and the
    applied tree says what each version, default and consumer should be, so a
    plan document needs to carry nothing more than its edits.
    """

    edits = list(edits)
    bases = touched(edits, project_files, project_files, model_paths)
    vmap = mv.version_map(project_files, model_paths)
    for edit in edits:
        if edit.kind in (EditKind.SCHEMA_YML, EditKind.SEMANTIC_YML):
            bases.update(
                base for base, model in vmap.items() if model.yaml_path == edit.path
            )
    if not bases:
        return {}
    builds = _builds(project_files, vmap, model_paths)
    report: dict[str, Any] = {}
    for base in sorted(bases):
        entry, _ = _model_report(
            base, project_files, project_files, vmap, vmap, builds, builds, model_paths
        )
        for consumer in entry.get("consumers") or []:
            consumer.pop("before", None)
            consumer.pop("changed", None)
        for key in ("relation_changes", "contract_changes"):
            entry.pop(key, None)
        entry["default"] = {"after": entry["default"]["after"]}
        entry.pop("latest_version", None)
        report[base] = entry
    return report
