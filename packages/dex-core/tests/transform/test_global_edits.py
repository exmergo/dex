"""Which edits make a transform plan global, held in one place (#1581).

Each check's set is pinned to the members it held before the sets moved into
``transform/global_edits.py``, written as literals: a change to one is a change
of behaviour for that check, and has to be made here on purpose.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_refactor_comparison_adds_the_profiles() -> None:
    assert _values(global_edits.COMPARE_GLOBAL_KINDS) == {
        "macro_sql",
        "project_yml",
        "packages_yml",
        "profiles_yml",
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
    # ``commands`` is not in this list: it reads the comparison set inside
    # ``_offer_refactor_comparison``, never at module level, so an attribute
    # check says nothing about it. Its call-site case below guards it instead.
    copies = [
        module.__name__.rsplit(".", 1)[-1]
        for module in (
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


MODEL = "models/m.sql"
#: One model whose file and YAML are both at ``MODEL``, so an edit of any kind
#: there names a node the lineage can bound.
MANIFEST = {
    "nodes": {
        "model.shop.m": {
            "name": "m",
            "resource_type": "model",
            "original_file_path": MODEL,
            "patch_path": "shop://" + MODEL,
        }
    }
}


def _each_kind() -> list[PlanEdit]:
    return [PlanEdit(path=MODEL, kind=k, new_content="version: 2\n") for k in EditKind]


def test_downstream_impact_calls_a_plan_global_on_the_core_alone() -> None:
    """Read at the call site, over every kind, so a check that stops reading the
    shared set is caught by what it does."""

    assert {
        edit.kind.value
        for edit in _each_kind()
        if downstream_impact.select_targets([edit], MANIFEST).reason_code
        == "global_edit"
    } == {"macro_sql", "project_yml", "packages_yml"}


def test_prerequisites_reach_any_node_on_their_set() -> None:
    assert {
        edit.kind.value
        for edit in _each_kind()
        if prerequisites._reaches_any_node(edit)
    } == {"macro_sql", "project_yml", "packages_yml", "profiles_yml", "test_sql"}


def test_finding_attribution_leaves_reach_unbounded_on_its_set() -> None:
    assert {
        edit.kind.value
        for edit in _each_kind()
        if finding_attribution.plan_reach([edit], MANIFEST).reached is None
    } == {"macro_sql", "project_yml", "packages_yml", "profiles_yml", "semantic_yml"}


def test_the_comparison_offer_calls_a_plan_mixed_with_its_repair_on_its_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offer's second reader of the comparison set (#1114): with an input
    blocked, a plan that edits a kind in the set carries the repair beside the
    rewrite. Read over every kind, at a path naming no blocked model, so only
    the kind can make the plan mixed."""

    monkeypatch.setattr(
        commands, "_record_refactor_models", lambda engine, plan_id, per_model: None
    )
    monkeypatch.setattr(
        refactor_compare, "edited_existing_models", lambda edits, project: ["summary"]
    )
    monkeypatch.setattr(
        refactor_compare,
        "_blocked_parents",
        lambda project, model, excluded: [
            refactor_compare.BlockedInput("users", "error")
        ],
    )

    def mixed(edit: PlanEdit) -> bool:
        result = SimpleNamespace(plan_id="p1", row_attribution=[], warnings=[])
        engine = SimpleNamespace(project_dir=Path)
        commands._offer_refactor_comparison(engine, result, [edit])
        return result.refactor_comparison_offer.get("mixed_with_repair") == ["users"]

    project = PlanEdit(path=PROJECT, kind=EditKind.PROJECT_YML, new_content="x")
    assert mixed(project)
    assert {edit.kind.value for edit in _each_kind() if mixed(edit)} == {
        "macro_sql",
        "project_yml",
        "packages_yml",
        "profiles_yml",
    }


def test_a_changed_project_file_makes_the_plan_global() -> None:
    edit = PlanEdit(path=PROJECT, kind=EditKind.PROJECT_YML, new_content="name: x\n")
    assert global_edits.changes_global([edit], {PROJECT: BEFORE})


@pytest.mark.parametrize(
    ("prior", "resent"),
    [
        (BEFORE, BEFORE.replace("\n", "\r\n")),
        (BEFORE.replace("\n", "\r\n"), BEFORE),
    ],
    ids=["lf_resent_as_crlf", "crlf_resent_as_lf"],
)
def test_a_project_file_resent_unchanged_does_not(prior: str, resent: str) -> None:
    """Line endings are no change on either side: the plan drops such an edit
    before storing."""

    edit = PlanEdit(path=PROJECT, kind=EditKind.PROJECT_YML, new_content=resent)
    assert not global_edits.changes_global([edit], {PROJECT: prior})


def test_a_new_macro_and_a_deleted_one_are_changes() -> None:
    """A deletion is a change even of a file that was empty."""

    new = PlanEdit(path="macros/m.sql", kind=EditKind.MACRO_SQL, new_content="x")
    gone = PlanEdit(path="macros/n.sql", kind=EditKind.MACRO_SQL, op=EditOp.DELETE)
    assert (
        global_edits.changes_global([new], {}),
        global_edits.changes_global([gone], {"macros/n.sql": ""}),
    ) == (True, True)


def test_a_kind_outside_the_core_is_not_asked_about() -> None:
    """The question is the core's: a profiles edit is global to the comparison,
    prerequisites and attribution, never to the downstream impact a switch would
    cost."""

    edits = [
        PlanEdit(path="profiles.yml", kind=EditKind.PROFILES_YML, new_content="x"),
        PlanEdit(path="models/m.sql", kind=EditKind.MODEL_SQL, new_content="x"),
    ]
    assert not global_edits.changes_global(edits, {})
