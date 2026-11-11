"""Databricks Metric View authoring through dex's plan/apply spine (#1258).

The repository's Metric View YAML files are the source of truth, and dex writes
them the way it writes everything else: a stored plan of diffs, applied with
`transform apply`. **dex never deploys**: nothing here runs ``CREATE VIEW ...
WITH METRICS``, materializes a view, changes a Unity Catalog privilege, or opens
a workspace connection. Deploying the file is the author's step.

**Edits are whole files, written byte-for-byte.** The accepted payload is never
parsed and re-serialized for the write, so its comments, key order, quoting and
whitespace land as authored. That also keeps a join's ``on:`` key as written:
a YAML 1.1 round trip would turn it into ``true:`` (#1262).

**The write surface is exactly the files in `semantic.databricks.files`.** Edit
paths are keyed as that list spells them, relative to the repository root, and
anything else is refused by name: a file nobody configured, the
``CREATE VIEW`` SQL that deploys a view (dex writes the YAML body between its
``$$`` delimiters), and a path outside the repository.

**The whole prospective set is validated, not the edited files alone,** by the
reader's own port of the Metric View YAML 1.1 rules (:mod:`.loader`). Authoring
holds the files it writes to more than the reader does: an unknown key, and a
view writing both ``fields`` and ``dimensions``, refuse the plan, where reading
only warns. Databricks refuses the first on ``CREATE VIEW`` and silently drops
``fields`` for the second, and dex would be writing the defect.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import replace
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
    from .project import MetricViewSemanticLayer

#: Reader warnings that refuse a view in a file this plan writes: Databricks
#: refuses an unknown key on ``CREATE VIEW`` (``rely`` included), and with both
#: ``fields`` and ``dimensions`` it reads ``dimensions`` and drops ``fields``.
STRICT_RULES = frozenset({"unknown_key", "fields_and_dimensions"})

#: Said on every conflict this writer reports.
CONFLICT_HINT = (
    "these Metric View files changed after the plan was made, so the plan no "
    "longer describes what it would overwrite. Remake it against the files as "
    "they are now with `semantic databricks plan`, or re-run with --confirm to "
    "overwrite them deliberately"
)

#: Said in every validation's notes, so a passing plan is not read as deployed.
VALIDATED_BY = (
    "validated by dex's port of the Databricks Metric View YAML 1.1 rules, with no "
    "workspace connection; nothing was deployed, and `CREATE VIEW ... WITH "
    "METRICS` in the workspace is the final word on a view"
)


def load_edit_view(layer: MetricViewSemanticLayer) -> SemanticDocumentView:
    """The configured files as bytes to be pinned, without validating.

    Keyed exactly as `semantic.databricks.files` spells them, relative to the
    repository root, which is the view's root. A configured file that does not
    exist yet is absent, so a plan creating it pins it as a create.
    """

    root = _root(layer)
    files: dict[str, SemanticSourceFile] = {}
    for name in editing_surface(layer):
        target = _target(root, name)
        if not target.is_file():
            continue
        try:
            content = _read(target)
        except (OSError, UnicodeDecodeError) as exc:
            raise ProjectError(
                f"could not read configured Metric View file '{name}': {exc}"
            ) from exc
        files[name] = SemanticSourceFile(
            path=name, content=content, sha256=content_hash(content)
        )
    return SemanticDocumentView(root=str(root), files=files)


def editing_surface(layer: MetricViewSemanticLayer) -> list[str]:
    """The configured files dex may write: every one of them.

    The configuration already refuses a ``.sql`` file, any other suffix, a path
    outside the repository and two files naming one view, so the list is the
    surface as it stands.
    """

    return list(layer.files)


def validate_plan(
    layer: MetricViewSemanticLayer, edits: list[PlanEdit], mode: str
) -> SemanticEditValidation:
    """Validate the set of files these edits would leave, and classify views.

    Raises ``ValueError`` naming every problem, with the file and the element,
    so nothing is stored for a plan that would leave a broken layer.
    """

    paths = [edit.path for edit in edits]
    if duplicates := sorted({p for p in paths if paths.count(p) > 1}):
        raise ValueError(
            "a Metric View authoring plan may edit each configured file once; got "
            "more than one edit for: " + ", ".join(duplicates)
        )
    surface = editing_surface(layer)
    if outside := [p for p in paths if p not in surface]:
        raise ValueError(
            "semantic databricks authoring edits only the Metric View YAML files "
            "listed under semantic.databricks.files, named relative to the "
            "repository root; "
            + "; ".join(_why_not_writable(layer, p) for p in outside)
        )

    current = {
        name: source.content for name, source in load_edit_view(layer).files.items()
    }
    prospective: dict[str, str | None] = {name: current.get(name) for name in surface}
    for edit in edits:
        prospective[edit.path] = edit.new_content
    if missing := sorted(name for name, text in prospective.items() if text is None):
        raise ValueError(
            "configured Metric View files are missing and this plan does not supply "
            "them: " + ", ".join(missing) + ". Every configured file is validated "
            "together, so author it in this plan or remove it from "
            "semantic.databricks.files"
        )

    edited = set(paths)
    texts = {name: text for name, text in prospective.items() if text is not None}
    validated, refusals = refusals_for(texts, edited)
    if refusals:
        listed = "\n  ".join(d.render() for d in refusals)
        raise ValueError(
            "the Metric Views this plan would leave are invalid, so no plan was "
            f"stored:\n  {listed}"
        )

    before = views_of(current)
    after_edited = views_of({p: texts[p] for p in sorted(edited)})
    after_all = views_of(texts)
    defined = sorted(set(after_edited) - set(before))
    updated = sorted(
        n for n in after_edited if n in before and after_edited[n] != before[n]
    )
    unchanged = sorted(
        n for n in after_edited if n in before and after_edited[n] == before[n]
    )
    removed = sorted(set(before) - set(after_all))
    if mode == "define" and updated:
        raise ValueError(
            "semantic databricks define found Metric Views already defined: "
            + ", ".join(updated)
            + "; use `semantic databricks update` or `semantic databricks plan`"
        )
    if mode == "define" and removed:
        raise ValueError(
            "semantic databricks define adds views, and this plan removes: "
            + ", ".join(removed)
            + "; use `semantic databricks update` or `semantic databricks plan`"
        )
    if mode == "update" and defined:
        raise ValueError(
            "semantic databricks update found Metric Views that do not exist yet: "
            + ", ".join(defined)
            + "; use `semantic databricks define` or `semantic databricks plan`"
        )

    refused = {(d.file, d.element, d.rule) for d in refusals}
    warnings = [
        d.render()
        for d in validated.diagnostics
        if d.severity != "error" and (d.file, d.element, d.rule) not in refused
    ]
    if removed:
        warnings.append(
            "this plan leaves a configured file declaring no view: "
            + ", ".join(removed)
        )
    warnings.append(
        "Metric View edits are whole-file replacements written byte-for-byte; dex "
        "does not reformat the YAML or modify unedited files, and deploys nothing"
    )
    return SemanticEditValidation(
        defined=defined,
        updated=updated,
        unchanged=unchanged,
        notes=[VALIDATED_BY],
        warnings=warnings,
    )


def refusals_for(
    prospective: Mapping[str, str], edited: Iterable[str]
) -> tuple[LoadResult, list[Diagnostic]]:
    """Validate a prospective file set the way authoring does.

    Returns the load result and every diagnostic that refuses it: the reader's
    errors anywhere in the set, and the strict warnings of the files in
    ``edited``. Shared with the derivation, which holds a view it proposes but
    cannot yet write to exactly the checks a plan writing it would run.
    """

    from .loader import validate_view_contents

    written = set(edited)
    validated = validate_view_contents(dict(prospective))
    refusals = [
        *validated.errors,
        *(
            replace(d, severity="error", message=_as_refusal(d.message))
            for d in validated.diagnostics
            if d.file in written and d.rule in STRICT_RULES
        ),
    ]
    return validated, refusals


def _as_refusal(message: str) -> str:
    """The reader's wording, which says what reading does, as a reason to refuse."""

    return (
        f"{message} (refused on write: reading only warns, and dex does not "
        "author what Databricks would refuse or silently drop)"
    )


def write_edits(
    layer: MetricViewSemanticLayer, edits: list[PlanEdit], *, confirmed: bool = False
) -> ApplyResult:
    """Write the authored bytes, all of them or none.

    Every edit is placed and hash-checked before anything is written. A conflict
    writes nothing unless confirmed. The set the write would leave is validated
    again, because the files around the edited ones may have changed since
    planning. Writes are staged to temporary files beside their targets and then
    moved into place, so a failure while staging leaves every target as it was.
    """

    surface = editing_surface(layer)
    root = _root(layer)
    staged: list[tuple[Path, PlanEdit]] = []
    conflicts: list[Conflict] = []
    diffs: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for name in surface:
        target = _target(root, name)
        if target.is_file():
            current[name] = _read(target)

    for edit in edits:
        if edit.path not in surface:
            raise ProjectError(
                f"'{edit.path}' is not listed under semantic.databricks.files; "
                "Metric View apply writes only configured files"
            )
        if edit.new_content is None:
            raise ProjectError(
                f"'{edit.path}': Metric View authoring writes view files and does "
                "not delete them"
            )
        target = _target(root, edit.path)
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
    prospective.update({edit.path: edit.new_content or "" for _, edit in staged})
    if missing := sorted(set(surface) - set(prospective)):
        raise ProjectError(
            "configured Metric View files are missing at apply time, so nothing "
            "was written: " + ", ".join(missing)
        )
    # The same checks a plan is stored under, strict rules included: a plan
    # document applied in another checkout never ran this layer's planning.
    _validated, refusals = refusals_for(prospective, {e.path for _, e in staged})
    if refusals:
        listed = "\n  ".join(d.render() for d in refusals)
        raise ProjectError(
            "the Metric Views this apply would leave are invalid, so nothing was "
            f"written:\n  {listed}"
        )

    written = _write_all(staged)
    return ApplyResult(
        written=written, diffs=diffs, conflicts=conflicts, conflict_hint=hint
    )


def views_of(texts: Mapping[str, str]) -> dict[str, str]:
    """Each file's view, by name (its stem, lowered), as a normalized form.

    Read as Databricks reads it (YAML 1.2, :func:`.loader.read_yaml`) and
    written as canonical JSON, so reformatting or reordering keys is not a
    change. A file that does not parse, or is empty, declares no view; the
    validator has already said why.
    """

    from .loader import read_yaml

    out: dict[str, str] = {}
    for name, text in texts.items():
        try:
            raw = read_yaml(text) if text else None
        except yaml.YAMLError:
            # The validator names every parse failure.
            continue
        if not isinstance(raw, dict):
            continue
        try:
            body = json.dumps(raw, sort_keys=True, default=str)
        except (TypeError, ValueError):
            continue
        out[PurePosixPath(name).stem.lower()] = body
    return out


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
            # mkstemp creates the file owner-only; the written file keeps the
            # target's mode, or a new file's usual mode under the umask.
            os.fchmod(handle, _mode_for(target))
            with os.fdopen(handle, "wb") as out:
                out.write((edit.new_content or "").encode("utf-8"))
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


# --- helpers -------------------------------------------------------------------


def _mode_for(target: Path) -> int:
    """The permission bits a write to ``target`` should leave."""

    try:
        return target.stat().st_mode & 0o777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def _root(layer: MetricViewSemanticLayer) -> Path:
    """The repository root, symlinks resolved."""
    return layer.repo_root.resolve()


def _target(root: Path, name: str) -> Path:
    """The configured name as a path inside the repository, symlinks resolved."""

    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ProjectError(
            f"configured Metric View file '{name}' resolves outside the repository "
            f"({target}); dex writes Metric View files only inside it"
        ) from exc
    return target


def _read(path: Path) -> str:
    # Bytes decoded, not text mode, so the pinned hash is of the file's own
    # newlines and a CRLF file does not read as a change.
    """A file's text as its bytes decode, newlines untouched."""
    return path.read_bytes().decode("utf-8")


def _why_not_writable(layer: MetricViewSemanticLayer, path: str) -> str:
    """Why ``path`` is not on the write surface, in one clause."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return f"'{path}' is outside the repository"
    if path.lower().endswith(".sql"):
        return (
            f"'{path}' is SQL: dex writes the Metric View YAML body (what `CREATE "
            "VIEW ... WITH METRICS LANGUAGE YAML` holds between its `$$` "
            "delimiters) in a .yaml file named after the view, never the statement "
            "that deploys it"
        )
    if not path.endswith((".yml", ".yaml")):
        return f"'{path}' is not a Metric View YAML file; they are named .yaml or .yml"
    stem = PurePosixPath(path).stem.lower()
    if clash := [n for n in layer.files if PurePosixPath(n).stem.lower() == stem]:
        return (
            f"'{path}' would name the view '{PurePosixPath(path).stem}', which "
            f"{clash[0]} already names (a view is named after its file's stem)"
        )
    listed = ", ".join([*layer.files, path])
    return (
        f"'{path}' is not configured; add it with `dex config set "
        f"semantic.databricks.files '[{listed}]'` first if it is a Metric View this "
        "repository should own"
    )
