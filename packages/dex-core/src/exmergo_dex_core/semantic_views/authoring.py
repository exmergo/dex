"""Snowflake Semantic View authoring through dex's plan/apply spine (#1292).

The repository's Semantic View YAML files are the source of truth, and dex writes
them the way it writes everything else: a stored plan of diffs, applied with
`transform apply`. **dex never deploys**: nothing here runs ``CREATE SEMANTIC
VIEW``, calls ``SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML``, grants a privilege, or
opens a Snowflake connection. Deploying the file is the author's step.

**Edits are whole files, written byte-for-byte.** The accepted payload is never
parsed and re-serialized for the write, so its comments, key order, quoting and
whitespace land as authored, and the file keeps its mode.

**The write surface is exactly the files in `semantic.snowflake.files`.** Edit
paths are keyed as that list spells them, relative to the repository root, and
anything else is refused by name: a file nobody configured (with the
`dex config set` that adopts it), the ``CREATE SEMANTIC VIEW`` DDL (dex writes
the YAML the procedure takes), another suffix, and a path outside the
repository. A view is named by its document's ``name``, not its file, so there
is no stem rule; two files declaring one view are the reader's
``duplicate_view``.

**The whole prospective set is validated, not the edited files alone,** by the
reader's port of Snowflake's specification and validation rules
(:mod:`.loader`). Every error anywhere in the set refuses the plan. Authoring
holds the files it writes to more than the reader does: an unknown key,
``access_modifier`` on a dimension, and a qualified read naming no member refuse
the plan where reading only warns. Snowflake accepts the second and keeps it
private, and accepts the first or third or not depending on what the warehouse
holds; dex does not write what it cannot vouch for.

**Classified by element.** Each logical table (``view.table``) and derived metric
(``view.metric``) in the edited files is ``defined``, ``updated`` or
``unchanged``, compared as YAML 1.2 values, so reformatting is not a change. A
change only to a view's other parts (relationships, description, Cortex
Analyst's keys) reports the view itself as ``updated``. ``define`` refuses an
existing view and a removal; ``update`` refuses a new view.
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
    from .project import SemanticViewSemanticLayer

#: Reader warnings that refuse an element in a file this plan writes: a key dex
#: does not know (Snowflake refuses some, drops others), ``access_modifier`` on
#: a dimension (Snowflake keeps it private, which hides the dimension from every
#: query), and a qualified read naming no member (a column only the warehouse
#: can vouch for).
STRICT_RULES = frozenset({"unknown_key", "dimension_access", "unresolved_member"})

#: Said on every conflict this writer reports.
CONFLICT_HINT = (
    "these Semantic View files changed after the plan was made, so the plan no "
    "longer describes what it would overwrite. Remake it against the files as "
    "they are now with `semantic snowflake plan`, or re-run with --confirm to "
    "overwrite them deliberately"
)

#: Said in every validation's notes, so a passing plan is not read as deployed.
VALIDATED_BY = (
    "validated by dex's port of Snowflake's Semantic View YAML specification and "
    "validation rules, with no Snowflake connection; nothing was deployed, and "
    "`SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` (with verify_only) on the account is "
    "the final word on a view"
)


def load_edit_view(layer: SemanticViewSemanticLayer) -> SemanticDocumentView:
    """The configured files as bytes to be pinned, without validating.

    Keyed exactly as `semantic.snowflake.files` spells them. A configured file
    that does not exist yet is absent, so a plan creating it pins it as a create.
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
                f"could not read configured Semantic View file '{name}': {exc}"
            ) from exc
        files[name] = SemanticSourceFile(
            path=name, content=content, sha256=content_hash(content)
        )
    return SemanticDocumentView(root=str(root), files=files)


def editing_surface(layer: SemanticViewSemanticLayer) -> list[str]:
    """The configured files dex may write: every one of them.

    The configuration already refuses a ``.sql`` file, another suffix, a path
    outside the repository and a duplicate, so the list is the surface.
    """

    return list(layer.files)


def validate_plan(
    layer: SemanticViewSemanticLayer, edits: list[PlanEdit], mode: str
) -> SemanticEditValidation:
    """Validate the set of files these edits would leave, and classify elements.

    Raises ``ValueError`` naming every problem, with the file and the element,
    so nothing is stored for a plan that would leave a broken layer.
    """

    paths = [edit.path for edit in edits]
    if duplicates := sorted({p for p in paths if paths.count(p) > 1}):
        raise ValueError(
            "a Semantic View authoring plan may edit each configured file once; got "
            "more than one edit for: " + ", ".join(duplicates)
        )
    surface = editing_surface(layer)
    if outside := [p for p in paths if p not in surface]:
        raise ValueError(
            "semantic snowflake authoring edits only the Semantic View YAML files "
            "listed under semantic.snowflake.files, named relative to the "
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
            "configured Semantic View files are missing and this plan does not "
            "supply them: " + ", ".join(missing) + ". Every configured file is "
            "validated together, so author it in this plan or remove it from "
            "semantic.snowflake.files"
        )

    edited = set(paths)
    texts = {name: text for name, text in prospective.items() if text is not None}
    validated, refusals = refusals_for(texts, edited)
    if refusals:
        listed = "\n  ".join(d.render() for d in refusals)
        raise ValueError(
            "the Semantic Views this plan would leave are invalid, so no plan was "
            f"stored:\n  {listed}"
        )

    before = elements_of(current)
    before_views = {view for view, _element in before}
    after_edited = elements_of({p: texts[p] for p in sorted(edited)})
    after_all = elements_of(texts)
    edited_views = {view for view, _element in after_edited}
    defined = sorted(_label(k) for k in set(after_edited) - set(before))
    updated = sorted(
        _label(k) for k in after_edited if k in before and after_edited[k] != before[k]
    )
    unchanged = sorted(
        _label(k) for k in after_edited if k in before and after_edited[k] == before[k]
    )
    removed = sorted(_label(k) for k in set(before) - set(after_all))
    new_views = sorted(edited_views - before_views)
    existing = sorted(edited_views & before_views)
    if mode == "define" and (
        touched := [v for v in existing if _touched(v, updated, defined)]
    ):
        raise ValueError(
            "semantic snowflake define found Semantic Views already defined: "
            + ", ".join(touched)
            + "; use `semantic snowflake update` or `semantic snowflake plan`"
        )
    if mode == "define" and removed:
        raise ValueError(
            "semantic snowflake define adds views, and this plan removes: "
            + ", ".join(removed)
            + "; use `semantic snowflake update` or `semantic snowflake plan`"
        )
    if mode == "update" and new_views:
        raise ValueError(
            "semantic snowflake update found Semantic Views that do not exist yet: "
            + ", ".join(new_views)
            + "; use `semantic snowflake define` or `semantic snowflake plan`"
        )

    refused = {(d.file, d.element, d.rule) for d in refusals}
    warnings = [
        d.render()
        for d in validated.diagnostics
        if d.severity != "error" and (d.file, d.element, d.rule) not in refused
    ]
    if removed:
        warnings.append("this plan removes: " + ", ".join(removed))
    warnings.append(
        "Semantic View edits are whole-file replacements written byte-for-byte; dex "
        "does not reformat the YAML or modify unedited files, and deploys nothing"
    )
    return SemanticEditValidation(
        defined=defined,
        updated=updated,
        unchanged=unchanged,
        notes=[VALIDATED_BY],
        warnings=warnings,
    )


def _touched(view: str, updated: list[str], defined: list[str]) -> bool:
    """Whether an existing view gains or changes anything in this plan."""
    prefix = f"{view}."
    return any(n == view or n.startswith(prefix) for n in [*updated, *defined])


def refusals_for(
    prospective: Mapping[str, str], edited: Iterable[str]
) -> tuple[LoadResult, list[Diagnostic]]:
    """Validate a prospective file set the way authoring does.

    Returns the load result and every diagnostic that refuses it: the reader's
    errors anywhere in the set, and the strict warnings of the files in
    ``edited``. Shared with the derivation, which holds a table it proposes to
    exactly the checks a plan writing it would run.
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
        "author what it cannot vouch Snowflake will read as written)"
    )


def write_edits(
    layer: SemanticViewSemanticLayer,
    edits: list[PlanEdit],
    *,
    confirmed: bool = False,
) -> ApplyResult:
    """Write the authored bytes, all of them or none.

    Every edit is placed and hash-checked before anything is written. A conflict
    writes nothing unless confirmed. The set the write would leave is validated
    again, strict rules included, because the files around the edited ones may
    have changed since planning and a plan document applied in another checkout
    never ran this layer's planning. Writes are staged beside their targets and
    then moved into place, so a failure while staging leaves every target as it
    was.
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
                f"'{edit.path}' is not listed under semantic.snowflake.files; "
                "Semantic View apply writes only configured files"
            )
        if edit.new_content is None:
            raise ProjectError(
                f"'{edit.path}': Semantic View authoring writes view files and does "
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
            "configured Semantic View files are missing at apply time, so nothing "
            "was written: " + ", ".join(missing)
        )
    _validated, refusals = refusals_for(prospective, {e.path for _, e in staged})
    if refusals:
        listed = "\n  ".join(d.render() for d in refusals)
        raise ProjectError(
            "the Semantic Views this apply would leave are invalid, so nothing was "
            f"written:\n  {listed}"
        )

    written = _write_all(staged)
    return ApplyResult(
        written=written, diffs=diffs, conflicts=conflicts, conflict_hint=hint
    )


def elements_of(texts: Mapping[str, str]) -> dict[tuple[str, str | None], str]:
    """Each view's elements as a normalized form, keyed ``(view, element)``.

    A logical table is ``(view, "table:<name>")``, a derived metric
    ``(view, "metric:<name>")``, and the view's other parts together
    ``(view, None)``. Names are lowered, as Snowflake folds them. Read as YAML
    1.2 and written as canonical JSON, so reformatting or reordering keys is not
    a change. A file that does not parse declares nothing; the validator has
    already said why.
    """

    from ..native_yaml import MAX_EXPANDED_VALUES, expansion
    from .loader import read_yaml

    out: dict[tuple[str, str | None], str] = {}
    for text in texts.values():
        try:
            raw = read_yaml(text) if text else None
        except yaml.YAMLError:
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            continue
        # The file being replaced was never validated by this plan, so its
        # aliases are bounded here too before anything walks them.
        expanded = expansion(raw)
        if expanded is None or expanded > MAX_EXPANDED_VALUES:
            continue
        view = raw["name"].lower()
        rest = {k: v for k, v in raw.items() if k not in ("tables", "metrics")}
        out[(view, None)] = _canonical(rest)
        for key, kind in (("tables", "table"), ("metrics", "metric")):
            items = raw.get(key)
            if not isinstance(items, list):
                # A malformed section declares nothing; the validator says why.
                continue
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    out[(view, f"{kind}:{item['name'].lower()}")] = _canonical(item)
    return out


def _canonical(value: Any) -> str:
    """``value`` as canonical JSON."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _label(key: tuple[str, str | None]) -> str:
    """An element as the envelope names it: ``view.table``, ``view.metric``, or
    the view itself."""
    view, element = key
    return view if element is None else f"{view}.{element.split(':', 1)[1]}"


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
    # A copy of each existing target, staged beside it before anything moves, so
    # a failure part way through puts every moved file back with one atomic
    # rename each: all files or none, and no reader sees a half-written file.
    try:
        backups = [_backup(target) for _temporary, target, _edit in temporaries]
    except OSError:
        for temporary, _, _ in temporaries:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise
    moved: list[tuple[Path, Path | None]] = []
    written: list[str] = []
    try:
        for (temporary, target, edit), backup in zip(temporaries, backups, strict=True):
            temporary.replace(target)
            moved.append((target, backup))
            written.append(edit.path)
    except OSError:
        stranded: list[str] = []
        for target, backup in reversed(moved):
            try:
                if backup is None:
                    target.unlink()
                else:
                    backup.replace(target)
            except OSError:
                if backup is not None:
                    stranded.append(f"{target} (its previous bytes are in {backup})")
        for temporary, _, _ in temporaries:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
        for backup in backups:
            if backup is not None and not any(str(backup) in s for s in stranded):
                with contextlib.suppress(OSError):
                    backup.unlink(missing_ok=True)
        if stranded:
            raise ProjectError(
                "a Semantic View apply failed part way and could not put back: "
                + "; ".join(stranded)
                + ". Move each copy back over its file by hand"
            ) from None
        raise
    for backup in backups:
        if backup is not None:
            with contextlib.suppress(OSError):
                backup.unlink()
    return written


def _backup(target: Path) -> Path | None:
    """A copy of ``target`` staged beside it with its mode, or ``None`` when it
    does not exist yet (a create, undone by removing the file)."""

    if not target.is_file():
        return None
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".dex-backup"
    )
    try:
        os.fchmod(handle, _mode_for(target))
        with os.fdopen(handle, "wb") as out:
            out.write(target.read_bytes())
    except OSError:
        with contextlib.suppress(OSError):
            Path(name).unlink()
        raise
    return Path(name)


# --- helpers -------------------------------------------------------------------


def _mode_for(target: Path) -> int:
    """The permission bits a write to ``target`` should leave."""

    try:
        return target.stat().st_mode & 0o777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def _root(layer: SemanticViewSemanticLayer) -> Path:
    """The repository root, symlinks resolved."""
    return layer.repo_root.resolve()


def _target(root: Path, name: str) -> Path:
    """The configured name as a path inside the repository, symlinks resolved."""

    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ProjectError(
            f"configured Semantic View file '{name}' resolves outside the "
            f"repository ({target}); dex writes Semantic View files only inside it"
        ) from exc
    return target


def _read(path: Path) -> str:
    """A file's text as its bytes decode, newlines untouched, so the pinned hash
    is of the file's own newlines and a CRLF file does not read as a change."""
    return path.read_bytes().decode("utf-8")


def _why_not_writable(layer: SemanticViewSemanticLayer, path: str) -> str:
    """Why ``path`` is not on the write surface, in one clause."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return f"'{path}' is outside the repository"
    if path.lower().endswith(".sql"):
        return (
            f"'{path}' is SQL: dex writes the Semantic View YAML that "
            "`SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML` takes, never the `CREATE "
            "SEMANTIC VIEW` statement that deploys one"
        )
    if not path.endswith((".yml", ".yaml")):
        return (
            f"'{path}' is not a Semantic View YAML file; they are named .yaml or .yml"
        )
    return f"'{path}' is not configured; " + adopt_hint(layer, [path])


def adopt_hint(layer: SemanticViewSemanticLayer, paths: list[str]) -> str:
    """The `dex config set` that puts ``paths`` on the write surface."""
    listed = ", ".join(dict.fromkeys([*layer.files, *paths]))
    return (
        f"add it with `dex config set semantic.snowflake.files '[{listed}]'` "
        "first if it is a Semantic View this repository should own"
    )
