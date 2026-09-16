"""Native Ossie authoring through Dex's reviewable plan/apply spine.

Edits are whole configured documents. Dex never parses and re-serializes the
accepted payload, so its comments, key order, quoting, and whitespace are
written exactly as authored. Other configured documents are not touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..diffs import file_diff
from ..edits import ApplyResult, Conflict, content_hash
from ..errors import ProjectError
from .loader import ERROR, LoadedDocument, validate_document_contents

if TYPE_CHECKING:
    from ..cache import DexCache
    from ..transform.plans import PlanEdit
    from .project import OssieSemanticLayer


@dataclass(frozen=True)
class SemanticSourceFile:
    path: str
    content: str
    sha256: str


@dataclass(frozen=True)
class SemanticDocumentView:
    """The configured semantic-document keyspace used for hash pinning."""

    root: str
    files: dict[str, SemanticSourceFile]


def load_edit_view(layer: OssieSemanticLayer) -> SemanticDocumentView:
    """Read configured files as bytes-to-be-pinned, without validating them."""

    root = layer.repo_root.resolve()
    files: dict[str, SemanticSourceFile] = {}
    for name in layer.files:
        target = _target(root, name)
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjectError(
                f"could not read configured Ossie document '{name}': {exc}"
            ) from exc
        files[name] = SemanticSourceFile(
            path=name, content=content, sha256=content_hash(content)
        )
    return SemanticDocumentView(root=str(root), files=files)


def validate_plan(
    layer: OssieSemanticLayer,
    edits: list[PlanEdit],
    mode: str,
    *,
    cache: DexCache | None = None,
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """Validate the prospective configured set and classify model names."""

    configured = set(layer.files)
    paths = [edit.path for edit in edits]
    if duplicates := sorted({path for path in paths if paths.count(path) > 1}):
        raise ValueError(
            "an Ossie authoring plan may edit each configured document once; got "
            + ", ".join(duplicates)
        )
    outside = sorted(set(paths) - configured)
    if outside:
        raise ValueError(
            "semantic ossie authoring edits only documents listed under "
            "`semantic.ossie.files`; not configured: " + ", ".join(outside)
        )

    view = load_edit_view(layer)
    current = {name: source.content for name, source in view.files.items()}
    prospective = dict(current)
    for edit in edits:
        prospective[edit.path] = edit.new_content

    missing = sorted(configured - set(prospective))
    if missing:
        raise ValueError(
            "configured Ossie documents are missing and were not supplied by this "
            "plan: " + ", ".join(missing)
        )

    validated = validate_document_contents(prospective, connector=layer.connector)
    if validated.errors:
        details = "; ".join(d.render() for d in validated.errors)
        raise ValueError(
            "the prospective Ossie semantic layer is invalid, so no plan was "
            f"stored: {details}"
        )

    proposed_by_file = {doc.file: _model_names(doc) for doc in validated.documents}
    current_by_file = {
        name: _model_names_from_text(name, content) for name, content in current.items()
    }
    removed: list[str] = []
    for path in paths:
        removed.extend(
            sorted(current_by_file.get(path, set()) - proposed_by_file[path])
        )
    if removed:
        raise ValueError(
            "semantic ossie define|update|plan does not remove semantic models; "
            "the proposed whole-document edits remove: " + ", ".join(sorted(removed))
        )

    existing = set().union(*current_by_file.values()) if current_by_file else set()
    touched = set().union(*(proposed_by_file[path] for path in paths))
    defined = sorted(touched - existing)
    updated = sorted(touched & existing)
    if mode == "define" and updated:
        raise ValueError(
            "semantic ossie define found names already defined: "
            + ", ".join(updated)
            + "; use `semantic ossie update` or `semantic ossie plan`"
        )
    if mode == "update" and defined:
        raise ValueError(
            "semantic ossie update found names that do not exist: "
            + ", ".join(defined)
            + "; use `semantic ossie define` or `semantic ossie plan`"
        )

    warnings = [d.render() for d in validated.diagnostics if d.severity != ERROR]
    from .cache_validation import validate_cached_references

    cache_validation = validate_cached_references(
        validated.documents, cache, connector=layer.connector
    )
    if cache_validation.errors:
        raise ValueError(
            "the prospective Ossie semantic layer has references contradicted "
            "by the exploration cache, so no plan was stored: "
            + "; ".join(cache_validation.errors)
        )
    warnings.append(
        "Ossie edits are whole-document replacements written byte-for-byte; dex "
        "does not reformat YAML/JSON or modify unedited configured documents"
    )
    return (
        {"defined": defined, "updated": updated},
        cache_validation.notes,
        warnings,
    )


def write_edits(
    layer: OssieSemanticLayer,
    edits: list[PlanEdit],
    *,
    confirmed: bool = False,
) -> ApplyResult:
    """Apply configured-document edits atomically with stale-hash protection."""

    configured = set(layer.files)
    staged: list[tuple[Path, PlanEdit]] = []
    conflicts: list[Conflict] = []
    diffs: list[dict[str, Any]] = []
    root = layer.repo_root.resolve()
    current_documents: dict[str, str] = {}

    for name in layer.files:
        target = _target(root, name)
        if target.is_file():
            current_documents[name] = target.read_text(encoding="utf-8")
    prospective = dict(current_documents)
    prospective.update({edit.path: edit.new_content for edit in edits})
    missing = sorted(configured - set(prospective))
    if missing:
        raise ProjectError(
            "configured Ossie documents are missing at apply time: "
            + ", ".join(missing)
        )
    validated = validate_document_contents(prospective, connector=layer.connector)
    if validated.errors:
        details = "; ".join(d.render() for d in validated.errors)
        raise ProjectError(
            "the prospective Ossie semantic layer is invalid at apply time; "
            f"nothing was written: {details}"
        )

    for edit in edits:
        if edit.path not in configured:
            raise ProjectError(
                f"'{edit.path}' is not listed under `semantic.ossie.files`; "
                "native Ossie apply writes only configured documents"
            )
        target = _target(root, edit.path)
        current = current_documents.get(edit.path)
        if current == edit.new_content:
            continue
        found = content_hash(current) if current is not None else None
        if found != edit.old_content_hash:
            conflicts.append(
                Conflict(
                    path=edit.path,
                    expected_sha256=edit.old_content_hash,
                    found_sha256=found,
                )
            )
        diffs.append(file_diff(edit.path, current, edit.new_content))
        staged.append((target, edit))

    if conflicts and not confirmed:
        return ApplyResult(written=[], diffs=diffs, conflicts=conflicts)

    written: list[str] = []
    for target, edit in staged:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit.new_content, encoding="utf-8")
        written.append(edit.path)
    return ApplyResult(written=written, diffs=diffs, conflicts=conflicts)


def _target(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ProjectError(
            f"configured Ossie document '{name}' resolves outside the repository"
        ) from exc
    return target


def _model_names(document: LoadedDocument) -> set[str]:
    return {
        model["name"]
        for model in document.data.get("semantic_model") or []
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }


def _model_names_from_text(name: str, content: str) -> set[str]:
    try:
        parsed = (
            json.loads(content) if name.endswith(".json") else yaml.safe_load(content)
        )
    except (ValueError, yaml.YAMLError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    return {
        model["name"]
        for model in parsed.get("semantic_model") or []
        if isinstance(model, dict) and isinstance(model.get("name"), str)
    }
