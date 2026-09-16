"""A stored plan as a document a second process can carry, check, and apply.

The plan store is right for one process holding one project: ``plan()`` writes
``.dex/plans/<id>.json`` and ``apply()`` reads it back from the same store. A host
that authors where the model runs and applies offline in a disposable checkout has
neither the store nor the original working tree, and its only route today is to
read that private JSON file, whose schema is not a contract and which carries no
way to tell a valid plan from an edited one.

This module is that contract. :func:`plan_document` turns a stored plan into a
:class:`PortablePlan`: every edit's operation, kind, preimage hash, content hash,
content, and classification, plus a digest over the whole thing.
:func:`apply_document` writes one into a checkout that has never seen the plan
store, re-checking the content against the digest first.

**What the digest is, and what it is not.** It proves a document is internally
consistent: recompute every content hash from the content carried, recompute the
digest from those, and a byte changed anywhere fails. It is not a signature, and
it cannot be: anything that can rewrite the content can rewrite the digest beside
it. The host closes that gap by carrying the digest across the boundary through a
channel it trusts and passing it as ``expect_digest``, which is the one place
authenticity can live, because only the host knows which channel that is. Saying
so here rather than implying otherwise is deliberate: a digest presented as
tamper-proof is worse than no digest, since it invites a host to skip the pinning
that actually does the work.

**What the digest deliberately excludes.** Not ``created_at``, because a plan id
is already content-addressed and two identical changes authored an hour apart are
the same change. Not ``engine_version``, because an engine upgrade must not
invalidate a digest a host pinned before it. Not the classification, because that
is derived, and a later engine that reads a hook it used to miss would otherwise
change the digest of a plan whose bytes never moved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .. import __version__
from ..edits import ApplyResult, Edit, EditOp, SemanticEditTarget, content_hash
from .classify import ArtifactClassification, classify_content
from .plans import EditKind, PlanEdit, PlanError, TransformPlan, contained_key

#: Bumped when the document's own shape changes in a way a reader must notice.
#: A reader that does not recognize a version refuses rather than reading the
#: fields it does recognize, because a plan half-understood is a plan applied
#: wrong.
PLAN_DOCUMENT_SCHEMA_VERSION = 1


class PlanDocumentError(PlanError):
    """A plan document could not be read, or does not describe itself."""


class PlanDigestMismatchError(PlanDocumentError):
    """The document's content does not match the digest it carries or was pinned to.

    Carries both digests so a caller can log the pair without re-deriving either.
    """

    def __init__(
        self, message: str, *, expected: str | None = None, found: str | None = None
    ):
        super().__init__(message)
        self.expected = expected
        self.found = found


class PortableEdit(BaseModel):
    """One edit, complete enough to apply somewhere else.

    ``old_content_hash`` is the preimage the plan was authored against, already
    pinned at plan time; ``None`` means the path did not exist, which is how a
    create is expressed and is a fact the target checkout is checked against too.
    ``new_content_hash`` is new here: without it a document carries content no
    digest covers, which is exactly the hole a host had to work around before
    this existed.
    """

    path: str
    op: EditOp = EditOp.UPSERT
    kind: EditKind
    old_content_hash: str | None = None
    new_content_hash: str | None = None
    new_content: str | None = None
    classification: ArtifactClassification | None = None

    def as_edit(self) -> PlanEdit:
        """The engine-side edit this document describes."""

        return PlanEdit(
            path=self.path,
            new_content=self.new_content,
            old_content_hash=self.old_content_hash,
            op=self.op,
            kind=self.kind,
        )


class PortablePlan(BaseModel):
    """A stored plan, complete, self-describing, and checkable.

    ``project_dir`` travels because a plan's paths are relative to the project,
    not the repository root, and a checkout that puts the project somewhere else
    is a different checkout of a different source state. It is a declaration the
    applying side checks against its own layout rather than an instruction to
    write there.
    """

    schema_version: int = PLAN_DOCUMENT_SCHEMA_VERSION
    plan_id: str
    intent: str
    created_at: str
    project_dir: str
    edit_target: str = "project"
    engine_version: str = ""
    edits: list[PortableEdit] = Field(default_factory=list)
    digest: str = ""

    def data(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _canonical(plan: PortablePlan) -> str:
    """The bytes the digest is taken over.

    Sorted by path and reduced to the fields that decide what lands on disk, so
    two exports of one plan from two checkouts are byte-identical and a
    field added to the document later does not move an existing digest.
    """

    payload = {
        "schema_version": plan.schema_version,
        "plan_id": plan.plan_id,
        "intent": plan.intent,
        "project_dir": plan.project_dir,
        "edit_target": plan.edit_target,
        "edits": [
            {
                "path": edit.path,
                "op": edit.op.value,
                "kind": edit.kind.value,
                "old_content_hash": edit.old_content_hash,
                "new_content_hash": edit.new_content_hash,
            }
            for edit in sorted(plan.edits, key=lambda e: e.path)
        ],
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def plan_digest(plan: PortablePlan) -> str:
    """``sha256:<hex>`` over the plan's canonical form."""

    return "sha256:" + hashlib.sha256(_canonical(plan).encode("utf-8")).hexdigest()


def plan_document(
    stored: TransformPlan,
    *,
    read_existing: Callable[[str], str | None] | None = None,
) -> PortablePlan:
    """Turn a stored plan into a portable document.

    ``read_existing`` lets a delete be classified from the file it removes;
    without it a delete classifies as ``absent``, which is honest rather than
    silent. Nothing here touches a warehouse, a store, or a subprocess.
    """

    edits: list[PortableEdit] = []
    for edit in stored.edits:
        existing = None
        if edit.op is EditOp.DELETE and read_existing is not None:
            existing = read_existing(edit.path)
        classification = (
            classify_content(existing, path=edit.path, basis="existing_content")
            if edit.op is EditOp.DELETE
            else classify_content(edit.new_content, path=edit.path)
        )
        edits.append(
            PortableEdit(
                path=edit.path,
                op=edit.op,
                kind=edit.kind,
                old_content_hash=edit.old_content_hash,
                new_content_hash=(
                    content_hash(edit.new_content)
                    if edit.new_content is not None
                    else None
                ),
                new_content=edit.new_content,
                classification=classification,
            )
        )

    plan = PortablePlan(
        plan_id=stored.plan_id,
        intent=stored.intent,
        created_at=stored.created_at,
        project_dir=stored.project_dir,
        edit_target=stored.edit_target,
        engine_version=__version__,
        edits=edits,
    )
    plan.digest = plan_digest(plan)
    return plan


def verify_plan_document(
    document: PortablePlan | dict[str, Any] | str | bytes,
    *,
    expect_digest: str | None = None,
) -> PortablePlan:
    """Parse and check a plan document. Returns it; raises rather than warning.

    Four checks, in the order a reader should think about them: the schema
    version is one this engine understands, every edit's content hashes to the
    hash it carries, the digest recomputes, and the digest is the one the caller
    pinned. The third catches an edit made without care; only the fourth catches
    an edit made with care, which is why a host that skips ``expect_digest`` has
    integrity and not authenticity.

    Pydantic is the only parser reached here. No YAML, no jinja, no SQL, no dbt.
    """

    if isinstance(document, PortablePlan):
        plan = document.model_copy(deep=True)
    else:
        try:
            plan = (
                PortablePlan.model_validate_json(document)
                if isinstance(document, (str, bytes))
                else PortablePlan.model_validate(document)
            )
        except Exception as exc:
            raise PlanDocumentError(f"not a readable plan document: {exc}") from exc

    if plan.schema_version != PLAN_DOCUMENT_SCHEMA_VERSION:
        raise PlanDocumentError(
            f"plan document schema_version {plan.schema_version} is not the "
            f"{PLAN_DOCUMENT_SCHEMA_VERSION} this engine reads; upgrade or "
            "downgrade exmergo-dex-core to the version that wrote it"
        )

    for edit in plan.edits:
        if edit.op is EditOp.DELETE:
            if edit.new_content is not None or edit.new_content_hash is not None:
                raise PlanDocumentError(
                    f"'{edit.path}' is a delete and must carry no content"
                )
            continue
        if edit.new_content is None:
            raise PlanDocumentError(
                f"'{edit.path}' is an upsert and carries no content"
            )
        found = content_hash(edit.new_content)
        if edit.new_content_hash != found:
            raise PlanDigestMismatchError(
                f"'{edit.path}' content does not match its recorded hash: the "
                "document was changed after it was authored",
                expected=edit.new_content_hash,
                found=found,
            )

    recomputed = plan_digest(plan)
    if plan.digest != recomputed:
        raise PlanDigestMismatchError(
            "plan digest does not match the document it covers: the document was "
            "changed after it was authored",
            expected=plan.digest,
            found=recomputed,
        )
    if expect_digest is not None and expect_digest != recomputed:
        raise PlanDigestMismatchError(
            f"plan digest {recomputed} is not the {expect_digest} this caller "
            "pinned: this is a different plan",
            expected=expect_digest,
            found=recomputed,
        )
    return plan


def apply_document(
    document: PortablePlan | dict[str, Any] | str | bytes,
    repo_root: Path | str = ".",
    *,
    expect_digest: str | None = None,
    confirmed: bool = False,
    project_dir: str | None = None,
    semantic_layer: SemanticEditTarget | None = None,
) -> tuple[PortablePlan, ApplyResult]:
    """Apply a plan document in this checkout. Verifies before it writes.

    ``project_dir`` overrides the document's own, for a checkout that lays the
    project out differently; omitting it uses what the document declares.

    Containment and kind placement are re-checked against *this* checkout's
    declared surface rather than trusted from the document, for the same reason
    the preimage hashes are re-checked: the document is an artifact that crossed a
    boundary, and what it was validated against is not what it is being written
    into. Both are hard refusals; ``confirmed`` is the handshake for a human edit
    somebody can look at and accept, and nobody accepts a write outside the
    surface the project itself declares.

    The only repository-controlled content read on this path is ``dbt_project.yml``,
    parsed with YAML's non-constructing loader to learn where files may live.
    Nothing is templated, no SQL is parsed, no dbt runs, no package is installed,
    and no socket is opened.
    """

    from ..dbt_project import load, write_edits
    from .plans import assert_kind_placement

    plan = verify_plan_document(document, expect_digest=expect_digest)
    project = Path(repo_root) / (project_dir or plan.project_dir)
    edits: list[Edit] = [edit.as_edit() for edit in plan.edits]

    if plan.edit_target == "semantic":
        if semantic_layer is None:
            raise PlanDocumentError(
                "this plan targets a semantic layer, but the configured layer "
                "does not provide a semantic-document write surface"
            )
        surface = list(semantic_layer.semantic_editing_surface())
        for edit in edits:
            contained_key(edit.path, surface)
        return plan, semantic_layer.write_semantic_edits(edits, confirmed=confirmed)

    if not (project / "dbt_project.yml").is_file():
        raise PlanDocumentError(
            f"no dbt project at '{project}': this plan was authored against "
            f"'{plan.project_dir}' and this checkout does not have it"
        )
    view = load(project)
    from ..dbt_project import path_family

    for portable in plan.edits:
        assert_kind_placement(
            portable.as_edit(), path_family(project, portable.path, view), view
        )
    return plan, write_edits(edits, project, confirmed=confirmed)
