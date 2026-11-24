"""Which edits make a transform plan global, held in one place (#1581).

Each check's set is pinned to the members it held before the sets moved into
``transform/global_edits.py``, written as literals: a change to one is a change
of behaviour for that check, and has to be made here on purpose.
"""

from __future__ import annotations

from exmergo_dex_core.edits import EditOp
from exmergo_dex_core.transform import (
    commands,
    downstream_impact,
    finding_attribution,
    global_edits,
    pii_tags,
    prerequisites,
    refactor_compare,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit

PROJECT = "dbt_project.yml"
BEFORE = "name: shop\nversion: '1.0'\n"


def _values(kinds: frozenset[EditKind]) -> set[str]:
    return {EditKind(kind).value for kind in kinds}


def test_the_core_is_the_macros_the_project_and_the_packages() -> None:
    assert _values(global_edits.GLOBAL_KINDS) == {
        "macro_sql",
        "project_yml",
        "packages_yml",
    }


def test_downstream_impact_holds_the_core() -> None:
    assert _values(global_edits.IMPACT_GLOBAL_KINDS) == {
        "macro_sql",
        "project_yml",
        "packages_yml",
    }


def test_refactor_comparison_holds_the_core() -> None:
    assert _values(global_edits.COMPARE_GLOBAL_KINDS) == {
        "macro_sql",
        "project_yml",
        "packages_yml",
    }


def test_prerequisites_add_the_profiles_and_the_test_files() -> None:
    assert _values(global_edits.PREREQUISITE_GLOBAL_KINDS) == {
        "macro_sql",
        "project_yml",
        "packages_yml",
        "profiles_yml",
        "test_sql",
    }


def test_finding_attribution_adds_the_profiles_and_the_semantic_yaml() -> None:
    assert _values(global_edits.ATTRIBUTION_UNBOUNDED_KINDS) == {
        "macro_sql",
        "project_yml",
        "packages_yml",
        "profiles_yml",
        "semantic_yml",
    }


def test_each_check_reads_its_set_from_the_shared_module() -> None:
    """No check keeps a copy of its own: each binds the shared object."""

    bound = [
        (downstream_impact.IMPACT_GLOBAL_KINDS, global_edits.IMPACT_GLOBAL_KINDS),
        (refactor_compare.COMPARE_GLOBAL_KINDS, global_edits.COMPARE_GLOBAL_KINDS),
        (
            prerequisites.PREREQUISITE_GLOBAL_KINDS,
            global_edits.PREREQUISITE_GLOBAL_KINDS,
        ),
        (
            finding_attribution.ATTRIBUTION_UNBOUNDED_KINDS,
            global_edits.ATTRIBUTION_UNBOUNDED_KINDS,
        ),
        (pii_tags.changes_global, global_edits.changes_global),
    ]
    copies = [
        module.__name__.rsplit(".", 1)[-1]
        for module in (
            commands,
            downstream_impact,
            refactor_compare,
            prerequisites,
            finding_attribution,
            pii_tags,
        )
        for name in ("_GLOBAL_KINDS", "_UNBOUNDED_KINDS", "_edits_project")
        if hasattr(module, name)
    ]
    assert ([local is shared for local, shared in bound], copies) == ([True] * 5, [])


def test_a_changed_project_file_makes_the_plan_global() -> None:
    edit = PlanEdit(path=PROJECT, kind=EditKind.PROJECT_YML, new_content="name: x\n")
    assert global_edits.changes_global([edit], {PROJECT: BEFORE})


def test_a_project_file_resent_unchanged_does_not() -> None:
    """Line endings are no change: the plan drops such an edit before storing."""

    edit = PlanEdit(
        path=PROJECT,
        kind=EditKind.PROJECT_YML,
        new_content=BEFORE.replace("\n", "\r\n"),
    )
    assert not global_edits.changes_global([edit], {PROJECT: BEFORE})


def test_a_new_macro_and_a_deleted_one_are_changes() -> None:
    """A deletion is a change even of a file that was empty."""

    new = PlanEdit(path="macros/m.sql", kind=EditKind.MACRO_SQL, new_content="x")
    gone = PlanEdit(path="macros/n.sql", kind=EditKind.MACRO_SQL, op=EditOp.DELETE)
    assert (
        global_edits.changes_global([new], {}),
        global_edits.changes_global([gone], {"macros/n.sql": ""}),
    ) == (True, True)


def test_a_kind_outside_the_core_is_not_asked_about() -> None:
    """The question is the core's: a profiles edit is global to prerequisites
    and attribution, never to the impact and comparison a switch would cost."""

    edits = [
        PlanEdit(path="profiles.yml", kind=EditKind.PROFILES_YML, new_content="x"),
        PlanEdit(path="models/m.sql", kind=EditKind.MODEL_SQL, new_content="x"),
    ]
    assert not global_edits.changes_global(edits, {})
