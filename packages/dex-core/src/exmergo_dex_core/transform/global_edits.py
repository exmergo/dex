"""Which edits make a transform plan global, held in one place (#1581).

An edit is *global* to a check when the check cannot bound what it changes by
the files and nodes the plan names: a macro, the project config or the packages
can change how any model compiles. Four checks ask that question, and they do
not all draw the line in the same place, so this module holds the kinds every
one of them treats as global (:data:`GLOBAL_KINDS`) and each check's own set,
built from that core and named for the check:

* :data:`IMPACT_GLOBAL_KINDS` (downstream impact, ``global_edit``): the core.
* :data:`COMPARE_GLOBAL_KINDS` (refactor comparison, compiled through
  ancestors): the core.
* :data:`PREREQUISITE_GLOBAL_KINDS` (a failed prerequisite's ``plan_relation``):
  the core, the profiles and the test files, any of which can change a node's
  result wherever it sits.
* :data:`ATTRIBUTION_UNBOUNDED_KINDS` (finding attribution, whose reach the
  manifest's lineage bounds): the core, the profiles and the semantic YAML.

The members are the ones each check held before they moved here. A change to
one is a change of behaviour for that check, and its pin in
``tests/transform/test_global_edits.py`` says so.

:func:`changes_global` asks whether a plan already changes one of the core
kinds, which is what a writer adding a ``project_yml`` edit needs to know: such
a plan is global to every check already, so the added edit costs it no check.
It asks about the core alone, not about any one check's wider set.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from ..edits import EditOp
from .plans import EditKind

if TYPE_CHECKING:
    from .plans import PlanEdit

#: The edit kinds every check treats as global.
GLOBAL_KINDS = frozenset(
    {EditKind.MACRO_SQL, EditKind.PROJECT_YML, EditKind.PACKAGES_YML}
)

#: Edit kinds whose effect on a model the re-executed baseline cannot isolate.
#: A set of its own, so a check that binds the core under this name is caught.
IMPACT_GLOBAL_KINDS = frozenset({*GLOBAL_KINDS})

#: Edits that can change how an untouched parent compiles. Its dev relation keeps
#: what it was built with, so both graphs are compiled through their ancestors and
#: any parent that compiles differently makes the inputs unsynchronized.
COMPARE_GLOBAL_KINDS = frozenset({*GLOBAL_KINDS})

#: The edit kinds that can change any node's result, wherever they sit.
PREREQUISITE_GLOBAL_KINDS = GLOBAL_KINDS | {EditKind.PROFILES_YML, EditKind.TEST_SQL}

#: Edit kinds whose reach the manifest's lineage cannot bound.
ATTRIBUTION_UNBOUNDED_KINDS = GLOBAL_KINDS | {
    EditKind.PROFILES_YML,
    EditKind.SEMANTIC_YML,
}


def changes_global(edits: Iterable[PlanEdit], prior: Mapping[str, str]) -> bool:
    """Whether the plan already changes a kind every check treats as global.

    Only the core counts (:data:`GLOBAL_KINDS`): a ``profiles_yml`` edit is
    global to prerequisites and attribution but not to downstream impact or
    the refactor comparison, so it does not make this true.

    ``prior`` maps each path the plan edits to its content before the plan. An
    edit that resends such a file unchanged is no change: the plan drops it
    before it is stored, so merging another edit into it would be what made the
    plan global."""

    def changes(edit: PlanEdit) -> bool:
        if edit.op is EditOp.DELETE or edit.path not in prior:
            return True
        return _lf(edit.new_content or "") != _lf(prior[edit.path])

    return any(edit.kind in GLOBAL_KINDS and changes(edit) for edit in edits)


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")
