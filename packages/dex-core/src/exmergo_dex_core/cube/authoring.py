"""Cube model authoring through dex's reviewable plan/apply spine (#911).

The repository's Cube model files are the source of truth, and dex writes them
the way it writes everything else: a stored plan of diffs, applied with
`transform apply`. They are not generated from dbt (`cube_dbt`) or converted
through another format, and dex never writes to a Cube deployment.

**Edits are whole files, written byte-for-byte.** The accepted payload is never
parsed and re-serialized for the write, so its comments, key order, quoting and
whitespace land as authored.

**The write surface is exactly the plain YAML files in `semantic.cube.files`.**
Edit paths are keyed as that list spells them, relative to the repository root,
and anything else is refused by name: a file nobody configured, a JavaScript
model, `globals.py` and `cube.py`/`cube.js` (deployment configuration), and a
configured file that is compiled-only, whether a Jinja template or one holding a
cube or view that `extends` another. Each is refused with the reason the
catalog names it under `compiled_only`, so the two always agree. dex owns plain
YAML files only.

**The whole prospective set is validated, not the edited files alone,** by the
reader's own checks (:mod:`.loader`): every element by dex's port of Cube's
model rules, names unique across cubes and views, every join target and
`{member}` reference resolving, every view `join_path` following declared
joins, at most one join between a pair of cubes. Authoring holds the files it
writes to one more: a key Cube does not declare is a refusal there, where the
reader only warns, because dex would be writing the defect.

Nothing here starts Node, calls a Cube server, or opens a warehouse connection.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
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
from .loader import EXTENDS_REASON, JINJA_REASON, JS_REASON, is_template

if TYPE_CHECKING:
    from ..transform.plans import PlanEdit
    from .loader import Diagnostic, LoadResult
    from .model import Cube
    from .project import CubeSemanticLayer

#: Files a Cube deployment reads that are not models. Named in the refusal
#: rather than reported as merely unconfigured, as the configuration does.
_DEPLOYMENT_FILES = {
    "globals.py": "Cube's globals.py, which feeds the Jinja renderer",
    "cube.py": "Cube's cube.py, which configures the server",
    "cube.js": "Cube's cube.js, which configures the server",
}

#: Reader warnings that refuse an element in a file this plan writes.
STRICT_RULES = frozenset({"unknown_key"})

#: Measure types a fan-out multiplies: each joined row adds the value again.
_FANNED_OUT = frozenset({"count", "sum"})

#: Said on every conflict this writer reports.
CONFLICT_HINT = (
    "these Cube model files changed after the plan was made, so the plan no "
    "longer describes what it would overwrite. Remake it against the files as "
    "they are now with `semantic cube plan`, or re-run with --confirm to "
    "overwrite them deliberately"
)


def load_edit_view(layer: CubeSemanticLayer) -> SemanticDocumentView:
    """The configured YAML model files as bytes to be pinned, without validating.

    Keyed exactly as `semantic.cube.files` spells them, relative to the
    repository root, which is the view's root. A configured file that does not
    exist yet is absent, so a plan creating it pins it as a create. A JavaScript
    model is not on the surface and is not pinned.
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
                f"could not read configured Cube model file '{name}': {exc}"
            ) from exc
        files[name] = SemanticSourceFile(
            path=name, content=content, sha256=content_hash(content)
        )
    return SemanticDocumentView(root=str(root), files=files)


def editing_surface(layer: CubeSemanticLayer) -> list[str]:
    """The configured files dex may write: the YAML ones, never a JS model."""

    return [name for name in layer.files if not name.endswith(".js")]


def validate_plan(
    layer: CubeSemanticLayer, edits: list[PlanEdit], mode: str
) -> SemanticEditValidation:
    """Validate the set of files these edits would leave, and classify names.

    Raises ``ValueError`` naming every problem, with the file and the element,
    so nothing is stored for a plan that would leave a broken layer.
    """

    paths = [edit.path for edit in edits]
    if duplicates := sorted({p for p in paths if paths.count(p) > 1}):
        raise ValueError(
            "a Cube authoring plan may edit each configured model file once; got "
            "more than one edit for: " + ", ".join(duplicates)
        )
    surface = set(editing_surface(layer))
    if outside := [p for p in paths if p not in surface]:
        raise ValueError(
            "semantic cube authoring edits only the plain YAML model files listed "
            "under semantic.cube.files, named relative to the repository root; "
            + "; ".join(_why_not_writable(layer, p) for p in outside)
        )

    current = {
        name: source.content for name, source in load_edit_view(layer).files.items()
    }
    compiled: list[str] = []
    for edit in edits:
        found = current.get(edit.path)
        if found is not None and (hit := _compiled_only(found)):
            element, reason = hit
            compiled.append(
                f"{edit.path}: {element}: the file on disk is compiled-only "
                f"({reason}), and dex writes plain YAML model files only, so it is "
                "not replaced"
            )
        if hit := _compiled_only(edit.new_content or ""):
            element, reason = hit
            compiled.append(
                f"{edit.path}: {element}: the authored content is compiled-only "
                f"({reason}); dex authors plain YAML cubes and views that it can "
                "validate, and never one only Cube's compiler can read"
            )
    if compiled:
        raise ValueError(
            "compiled-only Cube content cannot be authored, so no plan was "
            "stored:\n  " + "\n  ".join(compiled)
        )

    prospective = {name: current.get(name) for name in surface}
    for edit in edits:
        prospective[edit.path] = edit.new_content
    if missing := sorted(name for name, text in prospective.items() if text is None):
        raise ValueError(
            "configured Cube model files are missing and this plan does not "
            "supply them: " + ", ".join(missing) + ". Every configured file is "
            "validated together, so author it in this plan or remove it from "
            "semantic.cube.files"
        )

    edited = set(paths)
    texts = {name: text for name, text in prospective.items() if text is not None}
    validated, refusals = refusals_for(texts, edited)
    if refusals:
        listed = "\n  ".join(d.render() for d in refusals)
        raise ValueError(
            "the Cube models this plan would leave are invalid, so no plan was "
            f"stored:\n  {listed}"
        )

    before = _elements(current.values())
    after_edited = _elements(prospective[p] or "" for p in sorted(edited))
    after_all = _elements(text for text in texts.values())
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
            "semantic cube define found cubes or views already defined: "
            + ", ".join(updated)
            + "; use `semantic cube update` or `semantic cube plan`"
        )
    if mode == "define" and removed:
        raise ValueError(
            "semantic cube define adds definitions, and this plan removes: "
            + ", ".join(removed)
            + "; use `semantic cube update` or `semantic cube plan`"
        )
    if mode == "update" and defined:
        raise ValueError(
            "semantic cube update found cubes or views that do not exist yet: "
            + ", ".join(defined)
            + "; use `semantic cube define` or `semantic cube plan`"
        )

    refused = {(d.file, d.element, d.rule) for d in refusals}
    warnings = [
        d.render()
        for d in validated.diagnostics
        if d.severity != "error" and (d.file, d.element, d.rule) not in refused
    ]
    warnings.extend(fan_out_warnings(validated, edited))
    if removed:
        warnings.append(
            "this plan removes Cube definitions an edited file declared: "
            + ", ".join(removed)
        )
    warnings.append(
        "Cube model edits are whole-file replacements written byte-for-byte; dex "
        "does not reformat the YAML or modify unedited model files"
    )
    notes = [
        "validated by dex's port of Cube's model rules, without Node, a Cube "
        "server or a warehouse connection; Cube's own compiler is the final word "
        "on a deployment"
    ]
    return SemanticEditValidation(
        defined=defined,
        updated=updated,
        unchanged=unchanged,
        notes=notes,
        warnings=warnings,
    )


def refusals_for(
    prospective: Mapping[str, str], edited: set[str]
) -> tuple[LoadResult, list[Diagnostic]]:
    """Validate a prospective file set the way authoring does.

    Returns the load result and every diagnostic that refuses it: the reader's
    errors anywhere in the set, and the strict warnings of the files in
    ``edited``. Shared with the derivation, which holds a cube it proposes but
    cannot yet write to exactly the checks a plan writing it would run.
    """

    from .loader import validate_model_contents

    validated = validate_model_contents(dict(prospective))
    refusals = [
        *validated.errors,
        *(
            # The reader's wording says what reading does with the key; here
            # it is the reason nothing is written.
            replace(d, message=d.message.replace("dex ignores it, and ", ""))
            for d in validated.diagnostics
            if d.file in edited and d.rule in STRICT_RULES
        ),
    ]
    return validated, refusals


def fan_out_warnings(validated: LoadResult, edited: set[str]) -> list[str]:
    """A summing measure on the one side of a join, with no key to dedupe on.

    Cube deduplicates a measure fanned out by a join on its cube's primary key.
    A cube that sits on the one side of a join (it declares ``one_to_many``, or
    another cube declares ``many_to_one`` to it), sums or counts, and declares
    no ``primary_key`` has nothing to deduplicate on. A warning rather than a
    refusal: the cube is valid, and only a query that crosses the join is wrong.
    Reported where the cube or the join's file is one this plan writes.
    """

    by_name = {cube.name: cube for cube in validated.cubes}
    one_side: dict[str, set[str]] = {}
    for cube in validated.cubes:
        for join in cube.joins:
            if join.relationship == "one_to_many":
                one_side.setdefault(cube.name, set()).add(cube.file)
            elif join.relationship == "many_to_one" and join.target in by_name:
                one_side.setdefault(join.target, set()).add(cube.file)
    out: list[str] = []
    for name in sorted(one_side):
        cube = by_name[name]
        if not ({cube.file} | one_side[name]) & edited or _has_key(cube):
            continue
        fanned = [m.name for m in cube.measures if m.type in _FANNED_OUT]
        if fanned:
            out.append(
                f"{cube.file}: cubes['{name}']: [fan_out] sits on the one side of "
                f"a join and declares no primary_key, so {', '.join(fanned)} "
                "cannot be deduplicated when a query crosses that join; declare "
                "the key dimension with `primary_key: true`"
            )
    return out


def write_edits(
    layer: CubeSemanticLayer, edits: list[PlanEdit], *, confirmed: bool = False
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
                f"'{edit.path}' is not a YAML file listed under semantic.cube.files; "
                "Cube apply writes only configured model files"
            )
        if edit.new_content is None:
            raise ProjectError(
                f"'{edit.path}': Cube authoring writes model files and does not "
                "delete them"
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
            "configured Cube model files are missing at apply time, so nothing was "
            "written: " + ", ".join(missing)
        )
    from .loader import validate_model_contents

    validated = validate_model_contents(prospective)
    if validated.errors:
        listed = "\n  ".join(d.render() for d in validated.errors)
        raise ProjectError(
            "the Cube models this apply would leave are invalid, so nothing was "
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


def _root(layer: CubeSemanticLayer) -> Path:
    return layer.repo_root.resolve()


def _target(root: Path, name: str) -> Path:
    """The configured name as a path inside the repository, symlinks resolved."""

    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ProjectError(
            f"configured Cube model file '{name}' resolves outside the repository "
            f"({target}); dex writes Cube model files only inside it"
        ) from exc
    return target


def _read(path: Path) -> str:
    # Bytes decoded, not text mode, so the pinned hash is of the file's own
    # newlines and a CRLF file does not read as a change.
    return path.read_bytes().decode("utf-8")


def _why_not_writable(layer: CubeSemanticLayer, path: str) -> str:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return f"'{path}' is outside the repository"
    if parts[-1] in _DEPLOYMENT_FILES:
        return (
            f"'{path}' is {_DEPLOYMENT_FILES[parts[-1]]}, deployment configuration "
            "rather than a model, which dex neither reads nor writes"
        )
    if path.endswith(".js"):
        return (
            f"'{path}' is compiled-only ({JS_REASON}); dex writes plain YAML model "
            "files only"
        )
    if path in layer.files:
        return f"'{path}' is configured but is not a YAML model file"
    if not path.endswith((".yml", ".yaml")):
        return f"'{path}' is not a Cube model file; they are named .yml or .yaml"
    listed = ", ".join([*layer.files, path])
    return (
        f"'{path}' is not configured; add it with `dex config set "
        f"semantic.cube.files '[{listed}]'` first if it is a model file this "
        "repository should own"
    )


def _compiled_only(text: str) -> tuple[str, str] | None:
    """The element and the reason that make ``text`` compiled-only, if any.

    Named as the catalog's ``compiled_only`` names them: ``(root)`` for a Jinja
    template, and the cube or view for an ``extends``.
    """

    if is_template(text):
        return "(root)", JINJA_REASON
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    for key in ("cubes", "views"):
        for entry in raw.get(key) or []:
            if isinstance(entry, dict) and entry.get("extends") is not None:
                return f"{key}['{entry.get('name')}']", EXTENDS_REASON
    return None


def _elements(texts: Any) -> dict[str, str]:
    """Each cube and view the texts declare, by name, as a normalized form.

    Keys are snake_cased at every level, so respelling ``sqlTable`` as
    ``sql_table`` reads as unchanged, and the form is canonical JSON, so key
    order does not matter. A file that does not parse contributes nothing; the
    validator has already said why.
    """

    from .validator import canonical_key

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {canonical_key(str(k)): normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    out: dict[str, str] = {}
    for text in texts:
        try:
            raw = yaml.safe_load(text) if text else None
        except yaml.YAMLError:
            continue
        if not isinstance(raw, dict):
            continue
        for key in ("cubes", "views"):
            for entry in raw.get(key) or []:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    out[entry["name"]] = json.dumps(
                        normalize(entry), sort_keys=True, default=str
                    )
    return out


def _has_key(cube: Cube) -> bool:
    return any(member.primary_key for member in cube.dimensions)
