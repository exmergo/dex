"""SLayer model authoring through dex's reviewable plan/apply spine.

The repository's SLayer model files are the source of truth, and dex writes them
the way it writes everything else: a stored plan of diffs, applied with
`transform apply`. They are not generated from dbt with `slayer import-dbt`,
which is one-way and loses custom schemas and aliases.

**Edits are whole files, written byte-for-byte.** The accepted payload is never
parsed and re-serialized for the write, so its comments, key order, quoting and
whitespace land as authored.

**The write surface is exactly `semantic.slayer.models`.** Edit paths are keyed
as that list spells them, relative to `semantic.slayer.base_dir`, and anything
else is refused: a datasource file (it carries credentials), `priority.yaml`,
SLayer's search index and memories, another model file nobody configured, and
any path that resolves outside the storage root, symlinks included.

**The whole prospective set is validated, not the edited files alone,** by the
same checks the reader runs (:mod:`.loader`): every model parses as the pinned
release's current `SlayerModel`, names are unique, each file sits at
`models/<data_source>/<name>.yaml`, every join resolves within the set onto
columns both sides have. Authoring holds edited models to three more: a
`default_time_dimension` has to name a column and every key has to be a field the
pinned release declares, where the reader only warns about either, and every
saved measure's formula has to parse.

**dex owns the configured files.** SLayer's own tooling rewrites model files
(`slayer serve`, `slayer ingest`, `validate-models --persist-cardinality`, a
storage migration on open), and a file rewritten after planning no longer
hashes to what the plan pinned. That refusal is correct, and the conflict says
what most likely happened rather than blaming a human edit.

Nothing here opens SLayer storage or a warehouse connection.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import yaml

from ..diffs import file_diff
from ..edits import (
    ApplyResult,
    Conflict,
    SemanticDocumentView,
    SemanticEditValidation,
    SemanticSourceFile,
    content_hash,
)
from ..errors import ProjectError

if TYPE_CHECKING:
    from ..transform.plans import PlanEdit
    from .loader import Diagnostic, LoadResult
    from .project import SlayerSemanticLayer

#: What SLayer keeps under its storage root besides models. None of it is a
#: model, and a datasource carries connection credentials, so each is named in
#: its refusal rather than reported as merely unconfigured.
_STORAGE_ONLY = {
    "datasources": "a SLayer datasource file, which carries connection credentials",
    "priority.yaml": "SLayer's priority.yaml",
    "embeddings.db": "SLayer's search index",
    "memories": "SLayer's memories",
}

#: Reader warnings that refuse a model this plan edits.
_STRICT_RULES = frozenset({"unknown_column", "unknown_key"})

#: Said on every conflict this writer reports, because SLayer's own storage is
#: the likeliest author of a change to a file dex wrote.
CONFLICT_HINT = (
    "these SLayer model files changed after the plan was made. dex owns the "
    "files configured under semantic.slayer.models, and SLayer's own storage "
    "rewrites them when it saves (slayer serve, slayer ingest, "
    "validate-models --persist-cardinality, or a format migration on open), so "
    "that is the likely author if no person edited them. Remake the plan against "
    "the files as they are now with `semantic slayer plan`, or re-run with "
    "--confirm to overwrite them deliberately"
)


def load_edit_view(layer: SlayerSemanticLayer) -> SemanticDocumentView:
    """The configured model files as bytes to be pinned, without validating them.

    Keyed exactly as `semantic.slayer.models` spells them, relative to the
    storage root, which is the view's root and therefore the plan's project
    directory. A configured file that does not exist yet is absent, so a plan
    creating it pins it as a create.
    """

    root, storage = _roots(layer)
    files: dict[str, SemanticSourceFile] = {}
    for name in layer.models:
        target = _target(root, storage, name)
        if not target.is_file():
            continue
        try:
            content = _read(target)
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjectError(
                f"could not read configured SLayer model '{name}': {exc}"
            ) from exc
        files[name] = SemanticSourceFile(
            path=name, content=content, sha256=content_hash(content)
        )
    return SemanticDocumentView(root=str(storage), files=files)


def validate_plan(
    layer: SlayerSemanticLayer, edits: list[PlanEdit], mode: str
) -> SemanticEditValidation:
    """Validate the set of models these edits would leave, and classify names.

    Raises ``ValueError`` naming every problem, with the file and the element,
    so nothing is stored for a plan that would leave a broken layer.
    """

    paths = [edit.path for edit in edits]
    if duplicates := sorted({p for p in paths if paths.count(p) > 1}):
        raise ValueError(
            "a SLayer authoring plan may edit each configured model file once; "
            "got more than one edit for: " + ", ".join(duplicates)
        )
    configured = set(layer.models)
    if outside := [p for p in paths if p not in configured]:
        raise ValueError(
            "semantic slayer authoring edits only the model files listed under "
            "semantic.slayer.models, named relative to semantic.slayer.base_dir; "
            + "; ".join(_why_not_configured(p) for p in outside)
        )

    current = {
        name: source.content for name, source in load_edit_view(layer).files.items()
    }
    prospective = {name: current.get(name) for name in layer.models}
    for edit in edits:
        prospective[edit.path] = edit.new_content
    if missing := [name for name, text in prospective.items() if text is None]:
        raise ValueError(
            "configured SLayer model files are missing and this plan does not "
            "supply them: " + ", ".join(missing) + ". Every configured model is "
            "validated together, so author it in this plan or remove it from "
            "semantic.slayer.models"
        )

    edited = set(paths)
    validated, refusals, skipped_formulas = refusals_for(prospective, edited)
    if refusals:
        listed = "\n  ".join(d.render() for d in refusals)
        raise ValueError(
            "the SLayer models this plan would leave are invalid, so no plan was "
            f"stored:\n  {listed}"
        )

    existing = {n for n in (_model_name(text) for text in current.values()) if n}
    touched = {
        loaded.model.name for loaded in validated.models if loaded.file in edited
    }
    defined = sorted(touched - existing)
    updated = sorted(touched & existing)
    if mode == "define" and updated:
        raise ValueError(
            "semantic slayer define found models already defined: "
            + ", ".join(updated)
            + "; use `semantic slayer update` or `semantic slayer plan`"
        )
    if mode == "update" and defined:
        raise ValueError(
            "semantic slayer update found models that do not exist yet: "
            + ", ".join(defined)
            + "; use `semantic slayer define` or `semantic slayer plan`"
        )

    notes = [
        "dex owns the model files configured under semantic.slayer.models. SLayer "
        "tooling that rewrites them (slayer serve, slayer ingest, "
        "validate-models --persist-cardinality, a storage migration) makes the "
        "next apply refuse, and the plan has to be remade"
    ]
    if skipped_formulas:
        notes.append(
            "measure formulas were not parsed: SLayer checks a formula's scalar "
            "calls with sqlglot, and the [sql] extra that carries it is not "
            "installed. The models were validated in every other respect"
        )
    warnings = [
        d.render()
        for d in validated.diagnostics
        if d.severity != "error" and d not in refusals
    ]
    warnings.append(
        "SLayer model edits are whole-file replacements written byte-for-byte; "
        "dex does not reformat the YAML or modify unedited model files"
    )
    return SemanticEditValidation(
        defined=defined, updated=updated, notes=notes, warnings=warnings
    )


def refusals_for(
    prospective: dict[str, str], edited: set[str]
) -> tuple[LoadResult, list[Diagnostic], bool]:
    """Validate a prospective model set the way authoring does.

    Returns the load result, every diagnostic that refuses it (the reader's
    errors, plus the strict warnings and formula problems of the files in
    ``edited``), and whether formulas went unparsed for want of the [sql] extra.
    Shared with the derivation, which holds a model it proposes but cannot yet
    write to exactly the checks a plan writing it would run.
    """

    from .loader import formula_problems, validate_model_contents

    validated = validate_model_contents(prospective)
    refusals = [*validated.errors, *_strict_warnings(validated, edited)]
    skipped_formulas = False
    for loaded in validated.models:
        if loaded.file not in edited:
            continue
        found = formula_problems(loaded)
        if found is None:
            skipped_formulas = True
        else:
            refusals.extend(found)
    return validated, refusals, skipped_formulas


def write_edits(
    layer: SlayerSemanticLayer, edits: list[PlanEdit], *, confirmed: bool = False
) -> ApplyResult:
    """Write the authored bytes, all of them or none.

    Every edit is placed and hash-checked before anything is written. A conflict
    writes nothing unless confirmed. The set the write would leave is validated
    again, because the files around the edited ones may have changed since
    planning. The writes themselves are staged to temporary files beside their
    targets and then moved into place, so a failure while staging leaves every
    target as it was.
    """

    configured = set(layer.models)
    root, storage = _roots(layer)
    staged: list[tuple[Path, PlanEdit]] = []
    conflicts: list[Conflict] = []
    diffs: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for name in layer.models:
        target = _target(root, storage, name)
        if target.is_file():
            current[name] = _read(target)

    for edit in edits:
        if edit.path not in configured:
            raise ProjectError(
                f"'{edit.path}' is not listed under semantic.slayer.models; SLayer "
                "apply writes only configured model files"
            )
        if edit.new_content is None:
            raise ProjectError(
                f"'{edit.path}': SLayer authoring writes model files and does not "
                "delete them"
            )
        target = _target(root, storage, edit.path)
        found_text = current.get(edit.path)
        if found_text == edit.new_content:
            continue
        found = content_hash(found_text) if found_text is not None else None
        if found != edit.old_content_hash:
            conflicts.append(
                Conflict(
                    path=edit.path,
                    expected_sha256=edit.old_content_hash,
                    found_sha256=found,
                )
            )
        diffs.append(file_diff(edit.path, found_text, edit.new_content))
        staged.append((target, edit))

    hint = CONFLICT_HINT if conflicts else None
    if conflicts and not confirmed:
        return ApplyResult(
            written=[], diffs=diffs, conflicts=conflicts, conflict_hint=hint
        )

    prospective = dict(current)
    prospective.update({edit.path: edit.new_content for _, edit in staged})
    if missing := sorted(configured - set(prospective)):
        raise ProjectError(
            "configured SLayer model files are missing at apply time, so nothing "
            "was written: " + ", ".join(missing)
        )
    from .loader import validate_model_contents

    validated = validate_model_contents(prospective)
    if validated.errors:
        listed = "\n  ".join(d.render() for d in validated.errors)
        raise ProjectError(
            "the SLayer models this apply would leave are invalid, so nothing was "
            f"written:\n  {listed}"
        )

    written = _write_all(staged)
    return ApplyResult(
        written=written, diffs=diffs, conflicts=conflicts, conflict_hint=hint
    )


def _write_all(staged: list[tuple[Path, PlanEdit]]) -> list[str]:
    """Stage every file, then move each into place.

    Bytes rather than text, so no newline translation stands between the
    authored content and the file.
    """

    temporaries: list[tuple[Path, Path, PlanEdit]] = []
    try:
        for target, edit in staged:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".dex"
            )
            with os.fdopen(handle, "wb") as out:
                out.write(edit.new_content.encode("utf-8"))
            temporaries.append((Path(name), target, edit))
    except OSError:
        for temporary, _, _ in temporaries:
            with contextlib.suppress(OSError):
                temporary.unlink()
        raise
    written: list[str] = []
    for temporary, target, edit in temporaries:
        temporary.replace(target)
        written.append(edit.path)
    return written


def _roots(layer: SlayerSemanticLayer) -> tuple[Path, Path]:
    root = layer.repo_root.resolve()
    return root, (root / layer.base_dir).resolve()


def _target(root: Path, storage: Path, name: str) -> Path:
    """The configured name as a path inside the storage root, symlinks resolved."""

    target = (storage / name).resolve()
    for boundary, what in ((root, "the repository"), (storage, "the storage root")):
        try:
            target.relative_to(boundary)
        except ValueError as exc:
            raise ProjectError(
                f"configured SLayer model '{name}' resolves outside {what} "
                f"({target}); dex writes SLayer models only inside "
                "semantic.slayer.base_dir"
            ) from exc
    if target.relative_to(storage).parts[:1] != ("models",):
        # A model symlinked to a datasource file sits inside the storage root
        # and would still be a write over connection credentials.
        raise ProjectError(
            f"configured SLayer model '{name}' resolves to {target}, which is not "
            "under the storage root's models/ directory; dex writes SLayer models "
            "only there"
        )
    return target


def _read(path: Path) -> str:
    # Bytes decoded, not text mode, so the pinned hash is of the file's own
    # newlines and a CRLF file does not read as a change.
    return path.read_bytes().decode("utf-8")


def _why_not_configured(path: str) -> str:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return f"'{path}' is outside semantic.slayer.base_dir"
    if parts[0] in _STORAGE_ONLY:
        return (
            f"'{path}' is {_STORAGE_ONLY[parts[0]]}, which dex neither reads nor writes"
        )
    return (
        f"'{path}' is not configured; add it to semantic.slayer.models first "
        "(`dex config set`) if it is a model this repository should own"
    )


def _strict_warnings(validated: LoadResult, edited: set[str]) -> list[Diagnostic]:
    """Reader warnings that refuse an authored model.

    The reader reads past a `default_time_dimension` naming no column, and past
    a key SLayer does not declare, and says so, because a raw layer should still
    be readable. A model dex is about to write is held to more: it would be
    writing the defect.
    """

    return [
        d for d in validated.diagnostics if d.file in edited and d.rule in _STRICT_RULES
    ]


def _model_name(text: str) -> str | None:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    name = parsed.get("name") if isinstance(parsed, dict) else None
    return name if isinstance(name, str) else None
