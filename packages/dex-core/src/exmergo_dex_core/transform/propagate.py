"""Rename and removal as one plan: every edit the change needs, or none of them.

`transform references` answers "where is this used" and hands the list to a human
to act on. This is the other half: it turns that same index into the edits, so a
rename is reviewed and applied as a unit instead of being retyped file by file.

**The rule that governs everything here is that dex may only act on a report it
believes.** `references` is allowed to answer "here is what I found, and here is
why I might be missing something", because a person reads that and compensates. A
generated plan cannot: it will be applied, and a plan that propagated a rename to
nine of ten sites leaves a project that compiles, passes review, and is wrong. So
every reason the index gives for doubting itself becomes a refusal here, and each
one names what to fix.

That is deliberately stricter than the delete guard in :mod:`.plans`, which
*warns* on a reference it could not resolve. The difference is whether the caller
can do anything about it. A `{{ ref(var('x')) }}` left dangling by a delete is
unsatisfiable, since no edit makes it resolvable and refusing would only block a
legitimate delete forever. The same reference in the path of a rename is
satisfiable: resolve it by hand, then re-run.

**What dex will not author.** A removal takes out the *definition* and verifies
the reads are gone; it never rewrites a read. `{% if var('using_department') %}`
can be deleted or unguarded and only the caller knows which, and a `{{ var('x') }}`
standing in an expression has no value dex may invent. Those edits come in through
``--edits-file`` and are validated in the same plan, so the change is still atomic
without dex guessing at semantics.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from io import StringIO
from typing import TYPE_CHECKING, Any

from ..dbt_project import DbtProjectView, EditOp, node_files, node_name, path_family
from ..errors import RequestError
from ..references import KINDS, ReferenceIndex
from .plans import EditKind, PlanEdit
from .rewrite import (
    RewriteError,
    jinja_names,
    narrow_quotes,
    rename_column_in_sql,
    splice,
    unproject_column_in_sql,
    yaml_blocks,
    yaml_names,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Kinds a rename can address. Every kind the index knows except the semantic
#: ones, which are renamed through `semantic update` where their own validation
#: lives.
RENAMEABLE = ("column", "var", "model", "seed", "snapshot", "macro", "source")

#: Kinds a removal can address. The same set: what can be renamed can be removed,
#: and a caller who can express one can express the other.
REMOVABLE = RENAMEABLE

#: The node kinds a `ref()` reaches, which are the ones whose rename moves a file.
_FILE_NODES = ("model", "seed", "snapshot")

#: Which `EditKind` a project-relative path is authored as. Read from the path
#: family the project itself declares, so a project that configures its
#: directories away from dbt's defaults is still placed correctly.
_FAMILY_KINDS = {
    "model": EditKind.MODEL_SQL,
    "macro": EditKind.MACRO_SQL,
    "snapshot": EditKind.SNAPSHOT_SQL,
    "seed": EditKind.SEED_CSV,
    "test": EditKind.TEST_SQL,
    "analysis": EditKind.ANALYSIS_SQL,
}


@dataclass
class Propagation:
    """One rename or removal, resolved into edits and into what it touched.

    ``sites`` counts occurrences per reference form rather than per file, because
    that is the number a caller checks the plan against: it is the same breakdown
    ``transform references`` reported, and the two agreeing is the evidence that
    nothing was dropped between reading and writing.
    """

    kind: str
    old: str
    new: str | None
    intent: str
    edits: list[PlanEdit] = field(default_factory=list)
    sites: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class PropagationRefusedError(RequestError):
    """A change dex will not make partially. Always names what to fix first."""


def propagate(
    view: DbtProjectView,
    project_dir: Path,
    kind: str,
    old: str,
    new: str | None = None,
    *,
    extra_edits: list[PlanEdit] | None = None,
) -> Propagation:
    """Every edit a rename (``new`` given) or a removal (``new`` omitted) needs.

    Pure: builds edits and writes nothing. The caller stores them as a plan, which
    is what keeps this on the same propose-don't-impose path as hand-authored
    content.

    ``extra_edits`` are the caller's own, and they matter to a removal: dex
    authors the definition removal and refuses while any read survives, so a
    caller who has already decided what each read should become passes those
    edits here and the whole change is checked as one project state. Without them
    the refusal would be unavoidable for every var a project actually reads.
    """

    if kind not in (RENAMEABLE if new is not None else REMOVABLE):
        allowed = ", ".join(RENAMEABLE if new is not None else REMOVABLE)
        raise PropagationRefusedError(
            f"dex cannot {'rename' if new else 'remove'} a {kind}. "
            f"Use one of: {allowed}"
            + (
                f". '{kind}' is a kind `transform references` reports on, but "
                "changing one is a semantic-layer edit: use `semantic update`"
                if kind in KINDS
                else ""
            )
        )
    index = ReferenceIndex(view)
    _refuse_incomplete(index, kind, old)
    owner, column = _resolve_target(index, kind, old)
    # Compared against the *bare* name for a column, since `old` carries the
    # model that scopes it and `new` never does: `stg_orders.order_id` renamed to
    # `order_id` is a no-op that these two strings do not look equal.
    if new is not None and new == (column if column is not None else old):
        raise PropagationRefusedError(f"'{new}' is already its name; nothing to do")

    builder = _Builder(view, project_dir, index, kind, old, new, owner, column)
    builder.run()
    edits = builder.edits() + list(extra_edits or [])
    if new is None:
        _refuse_surviving_reads(view, kind, old, edits)
    verb = "rename" if new is not None else "remove"
    target = f"{kind} {old}" + (f" to {new}" if new else "")
    return Propagation(
        kind=kind,
        old=old,
        new=new,
        intent=f"{verb} {target}",
        edits=edits,
        sites=dict(sorted(builder.sites.items())),
        notes=builder.notes,
    )


#: Per kind, the reference forms that are a *read* rather than the declaration.
#: A removal is refused while any of these survives, because dex will not invent
#: what a read should become.
_READING_FORMS = {
    "column": ("select_column", "yaml_column", "yaml_test_column", "semantic_expr"),
    "var": ("var_call",),
    "macro": ("macro_call", "yaml_test_ref"),
    "source": ("source_call", "semantic_model_ref", "yaml_relationship_to"),
}
_NODE_READING_FORMS = ("ref_call", "semantic_model_ref", "yaml_relationship_to")


def _refuse_surviving_reads(
    view: DbtProjectView, kind: str, name: str, edits: list[PlanEdit]
) -> None:
    """Refuse a removal that would leave a read of the removed thing behind.

    The project *after* this plan is built in memory and re-indexed, exactly as
    the delete guard does, so a caller whose own edits remove the reads is
    accepted and one who removed only the declaration is not. That is what makes
    the removal atomic without dex authoring a read edit it would have to guess
    at.
    """

    from ..dbt_project import SourceFile, content_hash

    deleted = {edit.path for edit in edits if edit.op is EditOp.DELETE}
    surviving = {
        path: source.content
        for path, source in view.files.items()
        if path not in deleted
    }
    for edit in edits:
        if edit.op is EditOp.UPSERT and edit.new_content is not None:
            surviving[edit.path] = edit.new_content

    after = view.model_copy(
        update={
            "files": {
                path: SourceFile(
                    path=path, content=content, sha256=content_hash(content)
                )
                for path, content in surviving.items()
            }
        }
    )
    index = ReferenceIndex(after, scan_packages=False)
    forms = _READING_FORMS.get(kind, _NODE_READING_FORMS)
    hits, _limits = index.references_to(name, kind)
    where = sorted(
        f"{hit.path}:{hit.line}"
        for hit in hits
        if hit.form in forms and hit.note != "same_name_elsewhere"
    )
    if not where:
        return
    raise PropagationRefusedError(
        f"removing the {kind} '{name}' would leave {len(where)} read(s) behind: "
        f"{', '.join(where)}. dex removes the declaration and will not rewrite a "
        "read, because only you know what each one should become (drop the "
        "block, unguard it, substitute a value). Author those edits and pass "
        "them with --edits-file, and they will be checked and stored in this "
        "same plan"
    )


def _refuse_incomplete(index: ReferenceIndex, kind: str, name: str) -> None:
    """Refuse while any reason to doubt the reference report is still standing.

    Each of these is a way the report can be short of complete, and acting on a
    short report is the exact failure this command exists to prevent. They are
    checked before any edit is built so the message is about the project rather
    than about whichever file dex happened to reach first.
    """

    if index.limits:
        raise PropagationRefusedError(
            "dex cannot promise it found every use of "
            f"'{name}': {'; '.join(index.limits)}. Resolve that first, because a "
            "rename applied to most of a project still compiles"
        )

    unresolved = index.indeterminate_for(kind)
    if unresolved:
        where = ", ".join(f"{ref.path}:{ref.line}" for ref in unresolved)
        raise PropagationRefusedError(
            f"{len(unresolved)} reference(s) dex could not resolve statically may "
            f"name '{name}': {where}. Rewrite each one to name its target "
            "directly, then re-run. (The delete guard only warns about these, "
            "because a delete cannot be made satisfiable by editing them; a "
            "rename can.)"
        )

    definitions = index.definitions_of(name, kind)
    packaged = sorted({str(d.package) for d in definitions if d.package})
    local = [d for d in definitions if not d.package]
    if packaged and local:
        here = ", ".join(sorted({d.path for d in local}))
        raise PropagationRefusedError(
            f"'{name}' is defined both in this project ({here}) and in "
            f"{', '.join(packaged)}. Changing this project's copy would stop it "
            "shadowing the package's, which would then resolve under the old "
            "name. dex does not edit installed packages, so it will not make "
            "half of this change"
        )


def _resolve_target(
    index: ReferenceIndex, kind: str, name: str
) -> tuple[str | None, str | None]:
    """The node a change is scoped to, and the bare column where there is one.

    A column has to be addressed as ``model.column``. `transform references`
    answers a bare name across the whole project on purpose, because a person
    reading that list can see which hits are theirs. A rewrite cannot: renaming a
    bare ``id`` would rewrite every unrelated ``id`` in the project, and the
    result would compile.
    """

    if kind != "column":
        return None, None
    node, _, column = name.partition(".")
    if not column:
        owners = sorted(
            {
                reference.owner
                for reference in index.references_to(name, "column")[0]
                if reference.owner
            }
        )
        listed = ", ".join(f"{owner}.{name}" for owner in owners[:8]) or "(none found)"
        raise PropagationRefusedError(
            f"name the model a column belongs to: '{name}' matches a "
            f"column name anywhere in the project, and dex will not rewrite "
            f"every one of them. Try one of: {listed}"
        )
    if index.node_kind(node) is None:
        raise PropagationRefusedError(
            f"'{node}' is not a model, seed or snapshot in this project, so dex "
            f"cannot scope '{name}' to its lineage"
        )
    return node, column


class _Builder:
    """Walks the project once, collecting per-file rewrites for one change.

    A file is rewritten once with every span it contributes, rather than once per
    occurrence, because an edit is a whole-file proposal and two edits to the same
    path would pin the second against content the first had not written yet.
    """

    def __init__(
        self,
        view: DbtProjectView,
        project_dir: Path,
        index: ReferenceIndex,
        kind: str,
        old: str,
        new: str | None,
        owner: str | None,
        column: str | None,
    ) -> None:
        self.view = view
        self.project_dir = project_dir
        self.index = index
        self.kind = kind
        self.old = old
        self.new = new
        self.owner = owner
        self.column = column
        self.sites: dict[str, int] = defaultdict(int)
        self.notes: list[str] = []
        self._spans: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        self._whole: dict[str, str] = {}
        self._moves: list[tuple[str, str]] = []
        self._deletes: list[str] = []
        self._nodes = node_files(view)
        self.lineage = index.descendants_of(owner) if owner is not None else set()

    # --- the walk ------------------------------------------------------------

    def run(self) -> None:
        self._refuse_macro_arguments()
        for path in sorted(self.view.files):
            content = self.view.files[path].content
            if path.endswith(".sql"):
                self._sql_file(path, content)
            elif path.endswith((".yml", ".yaml")):
                self._yaml_file(path, content)
            elif path.endswith(".csv"):
                self._seed_file(path, content)
        if self.kind in _FILE_NODES:
            self._node_file()
        if not (self._spans or self._whole or self._moves or self._deletes):
            raise PropagationRefusedError(
                f"'{self.old}' is not used as a {self.kind} anywhere in this "
                "project, so there is nothing to change. Run `transform "
                f"references {self.old}` to see what it is used as"
            )

    def _refuse_macro_arguments(self) -> None:
        """Refuse while a column's name is being handed to a macro as a string.

        The index reports these because a rename leaves them behind. It cannot
        know the macro treats the string as a column rather than as a label, and
        neither can dex: rewriting one that was a label changes what a report
        says, silently and forever. Naming them is the only honest move.
        """

        if self.kind != "column" or self.column is None:
            return
        hits = [
            reference
            for reference in self.index.references_to(self.old, "column")[0]
            if reference.form == "macro_arg_column"
            and (reference.owner in self.lineage or reference.owner is None)
        ]
        if not hits:
            return
        where = ", ".join(f"{hit.path}:{hit.line}" for hit in hits)
        raise PropagationRefusedError(
            f"'{self.column}' is passed to a macro as a literal string at "
            f"{where}. dex cannot tell whether the macro uses it as a column or "
            "as a label, and rewriting a label would change what the project "
            "reports without changing what it computes. Update those call sites "
            "by hand, then re-run"
        )

    # --- per surface ---------------------------------------------------------

    def _sql_file(self, path: str, content: str) -> None:
        node = node_name(path)
        rewritten = content

        if self.kind == "column":
            if node in self.lineage:
                rewritten = self._column_sql(path, rewritten, node)
        else:
            for name in jinja_names(content, node=node):
                if not self._matches(name.name, name.kind):
                    continue
                if self.new is None:
                    continue
                self.sites[name.form] += 1
                span = self._replacement_span(name)
                if span is not None:
                    self._spans[path].append(span)

        if rewritten is not content:
            self._whole[path] = rewritten

    def _column_sql(self, path: str, content: str, node: str) -> str:
        column = self.column or ""
        try:
            if self.new is not None:
                result = rename_column_in_sql(content, path, column, self.new)
            elif node == self.owner:
                result = unproject_column_in_sql(content, path, column)
            else:
                # A downstream model reading a removed column is a read the caller
                # has to decide about, not one dex removes. `_refuse_reads` names
                # them; here there is simply nothing to author.
                return content
        except RewriteError as exc:
            raise PropagationRefusedError(str(exc)) from exc
        if result.star and result.changed == 0:
            self.notes.append(
                f"{path}: projects `select *`, so it carries the column through "
                "under its new name with no edit"
            )
            return content
        self.sites["select_column"] += result.changed
        return result.content

    def _yaml_file(self, path: str, content: str) -> None:
        if self.new is None:
            self._yaml_removal(path, content)
            return
        for name in yaml_names(content):
            if not self._matches(name.name, name.kind):
                continue
            if name.kind == "column" and not self._owned(name.owner):
                continue
            self.sites[name.form] += 1
            span = self._replacement_span(name, content)
            if span is not None:
                self._spans[path].append(span)

    def _yaml_removal(self, path: str, content: str) -> None:
        for block in yaml_blocks(content):
            if not self._matches(block.name, block.kind):
                continue
            if block.kind == "column" and not self._owned(block.owner, defining=True):
                continue
            self.sites[block.form] += 1
            self._spans[path].append((block.span[0], block.span[1], ""))

    def _seed_file(self, path: str, content: str) -> None:
        """A seed's header row, and never a row below it.

        Data never enters a plan diff for the same reason it never enters agent
        context. The header names columns, so it is the one line of a seed a
        rename may touch.
        """

        if self.kind != "column" or node_name(path) != self.owner:
            return
        header = next(csv.reader(StringIO(content)), [])
        if self.column not in header:
            return
        offset = content.split("\n", 1)[0].find(self.column)
        if offset < 0:
            return
        self.sites["seed_header"] += 1
        replacement = "" if self.new is None else self.new
        self._spans[path].append((offset, offset + len(self.column), replacement))
        if self.new is None:
            self.notes.append(
                f"{path}: the header cell was cleared but the column's data was "
                "left in place; edit the rows yourself if they should go too"
            )

    def _node_file(self) -> None:
        """Moving or deleting the file a node is named after.

        dbt names a node after its file, so renaming one is a delete plus a create
        with the same content. The referrer rewrites are already collected by the
        jinja and YAML walks above, which is what makes the whole rename one plan.
        """

        for path in sorted(self._nodes):
            if node_name(path) != self.old:
                continue
            if self.index.node_kind(self.old) != self.kind:
                continue
            if self.new is None:
                self._deletes.append(path)
                self.sites["definition"] += 1
                continue
            suffix = path.rsplit("/", 1)[-1].split(".", 1)[-1]
            prefix = path.rsplit("/", 1)[0] if "/" in path else ""
            moved = (
                f"{prefix}/{self.new}.{suffix}" if prefix else f"{self.new}.{suffix}"
            )
            self._moves.append((path, moved))
            self.sites["definition"] += 1

    # --- helpers -------------------------------------------------------------

    def _matches(self, name: str, kind: str) -> bool:
        if kind != self.kind:
            return False
        if self.kind == "column":
            return name == self.column
        return name == self.old

    def _owned(self, owner: str | None, *, defining: bool = False) -> bool:
        """Whether an occurrence owned by ``owner`` is inside the change's scope.

        A YAML entry with no owner dex could read is left alone rather than
        assumed to be in scope: a documented column dex cannot attribute is
        exactly the case where a wrong guess renames somebody else's column.
        """

        if self.kind != "column":
            return True
        if owner is None:
            return False
        return owner == self.owner if defining else owner in self.lineage

    def _replacement_span(
        self, name: Any, content: str | None = None
    ) -> tuple[int, int, str] | None:
        """Where the new text goes for one occurrence, or ``None`` for a no-op."""

        if self.new is None:
            return None
        span = name.span
        if content is not None:
            span = narrow_quotes(content, span)
        replacement = self.new
        if self.kind == "source":
            # A source is named in two halves and either half can change. A
            # `source()` call spells them separately and gets the matching half; a
            # YAML `to:` or a semantic `model:` spells the whole thing and gets
            # the whole thing.
            new_source, _, new_table = self.new.partition(".")
            if not new_table:
                raise PropagationRefusedError(
                    f"name a source as `source_name.table_name`: '{self.new}' "
                    f"names only one half of it (renaming from '{self.old}')"
                )
            halves = {"source_call_namespace": new_source, "source_call": new_table}
            replacement = halves.get(getattr(name, "form", ""), self.new)
        return (span[0], span[1], replacement)

    # --- output --------------------------------------------------------------

    def edits(self) -> list[PlanEdit]:
        """The collected rewrites as one edit per path.

        Span rewrites and whole-file rewrites are folded together here: a column
        rename touches a model's SQL through the parse tree (whole file) and its
        `schema.yml` through spans, and both have to arrive as one edit per path
        or the second would be pinned against content the first had not written.
        """

        whole: dict[str, str] = dict(self._whole)
        built: list[PlanEdit] = []

        for path, spans in sorted(self._spans.items()):
            base = whole.pop(path, self.view.files[path].content)
            if spans:
                base = splice(base, spans)
            if base != self.view.files[path].content:
                built.append(self._edit(path, base))
        for path, content in sorted(whole.items()):
            if content != self.view.files[path].content:
                built.append(self._edit(path, content))
        for old_path, new_path in self._moves:
            built.append(
                PlanEdit(path=old_path, op=EditOp.DELETE, kind=self._kind_for(old_path))
            )
            moved = self.view.files[old_path].content
            for edit in built:
                if edit.path == old_path and edit.new_content is not None:
                    moved = edit.new_content
            built.append(self._edit(new_path, moved))
        built.extend(
            PlanEdit(path=path, op=EditOp.DELETE, kind=self._kind_for(path))
            for path in self._deletes
        )
        # A moved file's own rewrite rides on the create, never on the delete.
        return [
            edit
            for edit in built
            if not (
                edit.op is EditOp.UPSERT
                and any(edit.path == old for old, _new in self._moves)
            )
        ]

    def _edit(self, path: str, content: str) -> PlanEdit:
        return PlanEdit(path=path, new_content=content, kind=self._kind_for(path))

    def _kind_for(self, path: str) -> EditKind:
        if path in ("dbt_project.yml", "dbt_project.yaml"):
            return EditKind.PROJECT_YML
        if path in ("packages.yml", "dependencies.yml"):
            return EditKind.PACKAGES_YML
        family = path_family(self.project_dir, path, self.view)
        if path.endswith((".yml", ".yaml")):
            return (
                EditKind.SEMANTIC_YML
                if _is_semantic(self.view.files[path].content)
                else EditKind.SCHEMA_YML
            )
        return _FAMILY_KINDS.get(family or "model", EditKind.MODEL_SQL)


def _is_semantic(content: str) -> bool:
    """Whether a YAML file declares semantic models or metrics.

    Read from the content rather than the filename, because dbt puts no
    constraint on where either lives and projects genuinely mix them.
    """

    return "semantic_models:" in content or content.lstrip().startswith("metrics:")
