"""Native semantic-document authoring: whole documents in, a reviewable plan out.

Its own module rather than a branch inside :mod:`.commands`, for the reason
``transform references`` is one: a vendor whose semantic layer is its own source
(:data:`..config.SEMANTIC_SOURCE_FACTORIES`) authors documents, not SQL, so this
route owes nothing to the dialect engine or to dbt. Importing `.commands` would
pull the whole dbt authoring surface and sqlglot with it, which an install
carrying only the vendor's own reader does not have, and the one command that
install exists to run would be unreachable.

The edits payload reader lives here for the same reason and is imported back by
`.commands`: turning ``{"edits": [...]}`` into plan edits parses nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from .. import envelope as env
from ..edits import EditOp, SemanticEditTarget
from ..errors import DexError
from ..results import to_envelope
from ..storage import readable_cache
from . import plans as plans_mod
from .plans import EditKind, PlanEdit
from .results import PlanResult

if TYPE_CHECKING:
    from ..engine import DexEngine

#: The authoring modes every semantic surface offers, native or dbt. Declared
#: here rather than read off the dbt authoring table: the two surfaces happen to
#: agree today, and a native route that silently followed the dbt one's modes
#: would change shape the next time that table did.
MODES = ("define", "update", "plan")


def semantic_ossie(
    engine: DexEngine, intent: str, edits: list[PlanEdit], *, mode: str
) -> PlanResult:
    """Author configured native Ossie documents without involving dbt."""

    if mode not in MODES:
        raise ValueError(f"unknown semantic ossie mode '{mode}'")
    if not edits:
        raise ValueError(
            f"semantic ossie {mode} needs content: pass --edits-file <path|-> "
            "with whole configured Ossie documents"
        )
    wrong = [e.path for e in edits if e.kind is not EditKind.SEMANTIC_DOCUMENT]
    if wrong:
        raise ValueError(
            f"semantic ossie {mode} takes only semantic_document edits; got "
            "other kinds for: " + ", ".join(wrong)
        )
    deleted = [e.path for e in edits if e.op is EditOp.DELETE]
    if deleted:
        raise ValueError(
            f"semantic ossie {mode} authors content and does not delete: "
            + ", ".join(deleted)
        )

    layer = engine.semantic_catalog_source()
    if not isinstance(layer, SemanticEditTarget):
        # The vendor, not the class: the caller configured a vendor name and that
        # is the line they would edit. A class name would send someone reading
        # this into the engine looking for a setting that is not there.
        named = getattr(engine.config.semantic, "vendor", None) or "dbt"
        raise ValueError(
            f"the configured '{named}' semantic layer does not support native "
            "semantic-document authoring; configure `semantic.vendor: ossie`"
        )

    from ..ossie.authoring import validate_plan

    classification, validation_notes, validation_warnings = validate_plan(
        layer, edits, mode, cache=readable_cache(engine.store)
    )
    repo_root = engine.require_repo_root("storing a native semantic plan")
    stored, diffs, plan_warnings = plans_mod.plan(
        intent,
        edits,
        repo_root=repo_root,
        store=engine.require_full_store("storing a native semantic plan"),
        semantic_layer=layer,
        edit_target="semantic",
    )
    return PlanResult(
        plan_id=stored.plan_id,
        intent=stored.intent,
        paths=[edit.path for edit in stored.edits],
        plan_path=engine.require_full_store("locating a plan").plan_locator(
            stored.plan_id
        ),
        diffs=diffs,
        notes=validation_notes,
        warnings=[*plan_warnings, *validation_warnings],
        defined=classification["defined"],
        updated=classification["updated"],
    )


def cmd_semantic_ossie(args: argparse.Namespace, engine: DexEngine) -> env.Envelope:
    try:
        result = semantic_ossie(
            engine,
            getattr(args, "argument", None) or "",
            edits_from_payload(
                getattr(args, "edits_file", None),
                default_kind=EditKind.SEMANTIC_DOCUMENT,
            ),
            mode=args.mode,
        )
        return to_envelope(result, hints=plan_hint(result))
    except (DexError, ValueError) as exc:
        return env.error_for(exc)


def plan_hint(result: PlanResult) -> dict[str, str]:
    return {"next": f"review the diffs, then `transform apply {result.plan_id}`"}


def edits_from_payload(
    edits_file: str | None, default_kind: EditKind | None = None
) -> list[PlanEdit]:
    """Read the agent-authored edits payload (a file path, or ``-`` for stdin).

    Shape: ``{"edits": [{"path": ..., "kind": ..., "op": ..., "content": ...},
    ...]}``. ``op`` defaults to ``"upsert"`` (create or update): those carry
    ``content``. An ``op`` of ``"delete"`` removes the file and carries no
    ``content``. ``kind`` may be omitted when the command implies it (semantic
    define/update).
    """

    if edits_file is None:
        return []
    raw = sys.stdin.read() if edits_file == "-" else read_payload_file(edits_file)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"edits payload is not valid JSON: {exc}") from exc
    entries = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError('edits payload must be {"edits": [...]}')

    edits: list[PlanEdit] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(f"edits[{i}] needs at least a path")
        try:
            op = EditOp(entry.get("op") or EditOp.UPSERT.value)
        except ValueError as exc:
            raise ValueError(
                f"edits[{i}] has an unknown op '{entry.get('op')}': one of "
                + ", ".join(o.value for o in EditOp)
            ) from exc
        kind = entry.get("kind") or default_kind
        if kind is None:
            raise ValueError(
                f"edits[{i}] needs a kind: one of "
                + ", ".join(k.value for k in EditKind)
            )
        has_content = "content" in entry
        if op is EditOp.UPSERT and not has_content:
            raise ValueError(f"edits[{i}] is an upsert and needs content")
        if op is EditOp.DELETE and has_content:
            raise ValueError(f"edits[{i}] is a delete and must not carry content")
        edits.append(
            PlanEdit(
                path=entry["path"],
                kind=EditKind(kind),
                op=op,
                new_content=entry.get("content"),
            )
        )
    return edits


def read_payload_file(path: str) -> str:
    """Read a payload file, refusing a missing path as a request error.

    Shared with `.commands`'s definitions reader: both take the same
    ``<path|->`` argument and owe the caller the same message when the path is
    wrong, and neither is a SQL concern.
    """

    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise ValueError(f"edits file not found: {path}")
    return p.read_text(encoding="utf-8")
