"""Editing the declaration a project format placed, without reprinting it.

The file reconcile writes here is one a person wrote and nothing regenerates.
Every edit is therefore a splice into the original bytes at offsets a reader
computed, the same way `transform/rewrite.py` states it: re-serialising the file
would reflow it and drop the comments, and the diff would stop showing what
changed. A whole-file regeneration is available only where dex authored the file
in the first place, which is the dbt scaffold and nowhere else.

One edit per path is the other half. A table can carry schema drift and a lost
unique key at once, and both land in the same declaration. Two edits on one path
pin the same content hash, so the second overwrites the first and the reviewer's
diff describes a file that never existed. Everything that wants to change a file
stages into one accumulator here, which folds it into a single edit and drops it
when it changed nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

from ..adapters.project import PlacingProject
from ..cache import ColumnProfile
from ..dbt_project import DbtProjectView
from ..transform.plans import EditKind, PlanEdit
from ..transform.rewrite import (
    RewriteError,
    before_trailing_blanks,
    column_anchor,
    column_tests,
    column_tests_span,
    prevailing_test_key,
    splice,
    yaml_blocks,
)

#: Structures a span splice cannot be trusted through. Tabs make indentation
#: ambiguous, a second document makes "which one" unanswerable, and an alias
#: expands somewhere the offsets do not describe.
_UNSPANNABLE = (
    (
        re.compile(r"^\t| \t", re.MULTILINE),
        "indents with tabs, which YAML does not use for structure",
    ),
    (
        re.compile(r"^---\s*$.*^---\s*$", re.MULTILINE | re.DOTALL),
        "holds more than one YAML document",
    ),
    (
        re.compile(r"(?<![\w&*])[&*][A-Za-z_][\w-]*"),
        "uses YAML anchors or aliases, whose expansion dex does not track",
    ),
)


@dataclass(frozen=True)
class Placed:
    """A declaration dex resolved, and the model inside it this table is."""

    path: str
    model: str


@dataclass(frozen=True)
class Declined:
    """Why a table's declaration cannot be edited, as one clause a caller
    composes its own sentence around."""

    path: str | None
    reason: str


@dataclass
class _Staged:
    """One path's pending change: a whole replacement, spans, or both."""

    whole: str | None = None
    spans: list[tuple[int, int, str]] = field(default_factory=list)
    kind: EditKind = EditKind.SCHEMA_YML
    dropped: set[str] = field(default_factory=set)
    columns: dict[str, list[str]] = field(default_factory=dict)
    tests: dict[tuple[str, str], list[str]] = field(default_factory=dict)


class DeclarationEdits:
    """Accumulates every change reconcile wants to make, one edit per path.

    Holds the view and the placement rather than taking them per call: the staged
    spans, the columns being dropped and the warnings all have to survive across
    the schema pass and the grain pass, and it is that shared state which makes
    the two unable to collide.
    """

    def __init__(
        self, view: DbtProjectView | None, placement: PlacingProject | None
    ) -> None:
        self._view = view
        self._placement = placement
        self._staged: dict[str, _Staged] = {}
        self.warnings: list[str] = []

    # --- resolution ----------------------------------------------------------

    def resolve(self, table: str) -> Placed | Declined:
        """The declaration for ``table``, or the one reason there is not one.

        The model is named after the file the format chose rather than after
        dbt's `stg_` convention: the format already answered "where does this
        table's declaration live", and this reads that answer instead of
        asserting a spelling over it. A file packing several models finds no
        entry and is declined, which is a refusal to guess rather than a wrong
        write.
        """

        path = self._placed(EditKind.SCHEMA_YML, table)
        if path is None:
            return Declined(
                None,
                "this project format has nowhere for a declaration of "
                f"'{table}' to land",
            )
        content = self.content_for(path)
        if content is None:
            return Declined(
                path,
                f"{path} is not among the files this project format's view returned",
            )
        for pattern, complaint in _UNSPANNABLE:
            if pattern.search(content):
                return Declined(path, f"{path} {complaint}")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError:
            return Declined(path, f"{path} did not parse as YAML")
        if not isinstance(parsed, dict):
            return Declined(path, f"{path} did not parse as YAML")
        model = PurePosixPath(path).stem
        named = [
            entry
            for entry in parsed.get("models") or []
            if isinstance(entry, dict) and entry.get("name") == model
        ]
        if len(named) != 1:
            return Declined(path, f"{path} declares no model named '{model}'")
        return Placed(path, model)

    def _placed(self, kind: EditKind, table: str) -> str | None:
        if self._placement is None:
            suffix = {EditKind.MODEL_SQL: "sql", EditKind.SCHEMA_YML: "yml"}.get(kind)
            return None if suffix is None else f"models/staging/stg_{table}.{suffix}"
        return self._placement.edit_path(kind, table)

    def content_for(self, path: str) -> str | None:
        """What this path will hold, which is what a span must be measured against.

        A whole-file contributor may already have replaced it, and a span computed
        against the file on disk would then land at an offset in a document that is
        not being written.
        """

        staged = self._staged.get(path)
        if staged is not None and staged.whole is not None:
            return staged.whole
        if self._view is None or path not in self._view.files:
            return None
        return self._view.files[path].content

    # --- staging -------------------------------------------------------------

    def stage_whole(self, path: str, kind: EditKind, content: str) -> None:
        """Replace a path wholesale, for the one case dex authored the file."""

        entry = self._staged.setdefault(path, _Staged())
        entry.whole = content
        entry.kind = kind

    def dropping(self, path: str, column: str) -> bool:
        """Whether this run is removing ``column`` from ``path``."""

        staged = self._staged.get(path)
        return staged is not None and column in staged.dropped

    def add_column(self, placed: Placed, profile: ColumnProfile) -> str | None:
        """Declare a column that appeared upstream. A reason back, or ``None``.

        Mirrors what the scaffold writes for one column and nothing further. A
        `column_added` finding carries the type and no nullability, so the profile
        behind it is nullable by default and neither format proposes a test for it.
        """

        content = self.content_for(placed.path)
        if content is None:
            return f"{placed.path} is no longer readable"
        anchored = column_anchor(content, placed.model)
        if anchored is None:
            return (
                f"{placed.path} declares no columns for '{placed.model}', so there "
                "is no contract to keep current and dex will not invent one"
            )
        at, indent = anchored
        lines = [f"{indent}- name: {profile.name}\n"]
        if profile.pii is not None:
            # The flag propagates, never an example value: PII is flagged, not
            # surfaced, and the category is what a reviewer acts on.
            lines += [
                f"{indent}  meta:\n",
                f"{indent}    contains_pii: true\n",
                f"{indent}    pii_category: {profile.pii.category.value}\n",
            ]
            self._warn_model_meta(placed, profile.name, content)
        entry = self._staged.setdefault(placed.path, _Staged())
        entry.spans.append((at, at, "".join(lines)))
        entry.columns.setdefault(placed.model, self._declared(content, placed.model))
        entry.columns[placed.model].append(profile.name)
        return None

    def drop_column(self, placed: Placed, column: str) -> str | None:
        """Undeclare a column that is gone from the warehouse."""

        content = self.content_for(placed.path)
        if content is None:
            return f"{placed.path} is no longer readable"
        block = next(
            (
                b
                for b in yaml_blocks(content)
                if b.form == "yaml_column"
                and b.owner == placed.model
                and b.name == column
            ),
            None,
        )
        if block is None:
            return f"{placed.path} declares no column '{column}' under '{placed.model}'"
        declared = self._declared(content, placed.model)
        if declared == [column]:
            return (
                f"removing '{column}' would leave '{placed.model}' in "
                f"{placed.path} declaring no columns at all"
            )
        entry = self._staged.setdefault(placed.path, _Staged())
        entry.spans.append(
            (block.span[0], before_trailing_blanks(content, block.span[1]), "")
        )
        entry.dropped.add(column)
        entry.columns.setdefault(placed.model, list(declared))
        if column in entry.columns[placed.model]:
            entry.columns[placed.model].remove(column)
        return None

    def want_tests(
        self,
        placed: Placed,
        column: str,
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
    ) -> str | None:
        """Accumulate what ``column``'s test list should end up saying.

        Every contributor mutates one wanted list rather than computing its own
        span, which is what makes a nullability edit and a `unique` test on the
        same column unable to overlap: one span is computed for the list, once,
        when the edits are folded.
        """

        content = self.content_for(placed.path)
        if content is None:
            return f"{placed.path} is no longer readable"
        if self.dropping(placed.path, column):
            return (
                f"'{column}' is being removed from {placed.path} by schema drift, "
                "so no test was proposed on it"
            )
        current = column_tests(content, placed.model, column)
        if current is None:
            return (
                f"{placed.path} declares no column '{column}' under a model named "
                f"'{placed.model}'"
            )
        entry = self._staged.setdefault(placed.path, _Staged())
        wanted = entry.tests.setdefault((placed.model, column), list(current.values))
        for name in add:
            if name not in wanted:
                wanted.append(name)
        for name in remove:
            if name in wanted:
                wanted.remove(name)
        return None

    # --- folding -------------------------------------------------------------

    def edits(self) -> list[PlanEdit]:
        """Every staged change as one edit per path, no-ops dropped.

        The test spans are computed here rather than when they were asked for,
        because a list two callers touched has one final shape and therefore one
        span. A result that does not re-parse into what was intended is dropped
        with a warning instead of being offered as a plausible diff.
        """

        built: list[PlanEdit] = []
        for path, staged in sorted(self._staged.items()):
            base = staged.whole
            if base is None:
                base = self.content_for(path)
            if base is None:
                continue
            spans = list(staged.spans)
            try:
                spans.extend(self._test_spans(base, staged))
                content = splice(base, spans) if spans else base
            except RewriteError as exc:
                self.warnings.append(
                    f"dex could not rewrite {path} predictably ({exc}), so nothing "
                    "was proposed for that file; make the change by hand or with "
                    "`transform plan`"
                )
                continue
            # Only a splice needs proving. A whole-file replacement is content dex
            # generated for a file dex owns, and it is not YAML in the first place
            # when the file is a model's SQL.
            detail = self._disagreement(base, content, staged) if spans else None
            if detail is not None:
                self.warnings.append(
                    f"dex rewrote {path} and the result did not say what the edit "
                    f"intended ({detail}), so nothing was proposed for that file; "
                    "make the change by hand or with `transform plan`"
                )
                continue
            original = self._view.files[path].content if self._original(path) else None
            if original is not None and content == original:
                continue
            built.append(PlanEdit(path=path, kind=staged.kind, new_content=content))
        return built

    # --- helpers -------------------------------------------------------------

    def _test_spans(self, base: str, staged: _Staged) -> list[tuple[int, int, str]]:
        key = prevailing_test_key(base)
        spans = []
        for (model, column), wanted in sorted(staged.tests.items()):
            current = column_tests(base, model, column)
            if current is None:
                continue
            span = column_tests_span(base, current, wanted, key=key)
            if span is not None:
                spans.append(span)
        return spans

    def _original(self, path: str) -> bool:
        return self._view is not None and path in self._view.files

    def _declared(self, content: str, model: str) -> list[str]:
        return [
            block.name
            for block in yaml_blocks(content)
            if block.form == "yaml_column" and block.owner == model
        ]

    def _warn_model_meta(self, placed: Placed, column: str, content: str) -> None:
        parsed = yaml.safe_load(content)
        entry = next(
            (
                m
                for m in (parsed or {}).get("models") or []
                if isinstance(m, dict) and m.get("name") == placed.model
            ),
            {},
        )
        if (entry.get("meta") or {}).get("contains_pii"):
            return
        self.warnings.append(
            f"'{column}' is flagged as possible PII, so its new entry in "
            f"{placed.path} carries contains_pii; dex did not add a model-level "
            f"meta block to '{placed.model}', so add contains_pii there yourself "
            "if this format's readers expect it"
        )

    def _disagreement(self, base: str, content: str, staged: _Staged) -> str | None:
        """What the spliced result says that the edit did not intend."""

        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return f"it no longer parses as YAML: {exc.__class__.__name__}"
        if not isinstance(parsed, dict):
            return "it no longer parses as a YAML mapping"
        entries = {}
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or "name" not in entry:
                continue
            if entry["name"] in entries:
                return f"'{entry['name']}' now appears more than once"
            entries[entry["name"]] = entry
        for model, columns in staged.columns.items():
            entry = entries.get(model)
            if entry is None:
                return f"'{model}' is no longer declared"
            found = [
                c.get("name") for c in entry.get("columns") or [] if isinstance(c, dict)
            ]
            if found != columns:
                return f"'{model}' declares {found} rather than {columns}"
        for (model, column), wanted in staged.tests.items():
            entry = entries.get(model, {})
            found = next(
                (
                    c
                    for c in entry.get("columns") or []
                    if isinstance(c, dict) and c.get("name") == column
                ),
                None,
            )
            if found is None:
                return f"'{model}.{column}' is no longer declared"
            listed = [
                t if isinstance(t, str) else next(iter(t), "")
                for t in (found.get("tests") or found.get("data_tests") or [])
            ]
            if listed != wanted:
                return f"'{model}.{column}' tests are {listed} rather than {wanted}"
        untouched = set(self._untouched(base)) - set(staged.columns)
        for model in untouched:
            if model not in entries:
                return f"'{model}' was not being edited and is no longer declared"
        return None

    def _untouched(self, base: str) -> list[str]:
        try:
            parsed = yaml.safe_load(base)
        except yaml.YAMLError:
            return []
        if not isinstance(parsed, dict):
            return []
        return [
            entry["name"]
            for entry in parsed.get("models") or []
            if isinstance(entry, dict) and "name" in entry
        ]
