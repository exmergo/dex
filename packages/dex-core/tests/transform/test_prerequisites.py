"""Repair ownership read from a manifest, and its place in the bounded block (#860)."""

from __future__ import annotations

import json

import pytest

from exmergo_dex_core.edits import EditOp
from exmergo_dex_core.transform.classify import classify_content
from exmergo_dex_core.transform.completion import (
    COMPLETION_BUDGET_BYTES,
    summarize_completion,
)
from exmergo_dex_core.transform.evidence import BuildEvidence, BuildNode, BuildOutcome
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.prerequisites import diagnose

PKG = "model.pkg.pkg_task"
RPT = "model.proj.rpt"
OWN = "model.proj.stg"


def _node(unique_id: str, *parents: str, **extra) -> dict:
    return {
        "name": unique_id.split(".")[2],
        "resource_type": unique_id.split(".")[0],
        "package_name": unique_id.split(".")[1],
        "depends_on": {"nodes": list(parents), "macros": extra.pop("macros", [])},
        "refs": extra.pop("refs", []),
        **extra,
    }


def _manifest(**extra_nodes) -> dict:
    nodes = {
        PKG: _node(PKG, original_file_path="models/pkg_task.sql"),
        OWN: _node(OWN),
        RPT: _node(RPT, PKG, OWN),
        **extra_nodes,
    }
    return {"metadata": {"project_name": "proj"}, "nodes": nodes, "macros": {}}


def _edit(path: str, kind: EditKind = EditKind.MODEL_SQL) -> PlanEdit:
    return PlanEdit(path=path, kind=kind, new_content="select 1")


def _one(manifest, *, node=PKG, required=None, edits=None) -> dict:
    [entry] = diagnose(
        [(node, "error", "boom")], manifest, required=required, edits=edits
    )
    return entry


def _option(entry: dict, kind: str) -> dict:
    return next(o for o in entry["options"] if o["kind"] == kind)


def test_a_failure_the_plan_cannot_reach_is_independent():
    entry = _one(_manifest(), required=["stg"], edits=[_edit("models/stg.sql")])
    assert entry["plan_relation"] == "independent"


def test_a_failure_behind_an_edit_may_be_a_regression():
    entry = _one(
        _manifest(),
        node=RPT,
        required=["stg"],
        edits=[_edit("models/stg.sql")],
    )
    assert entry["plan_relation"] == "downstream_of_edit"


def test_the_edited_node_is_edited_and_a_package_namesake_is_not():
    edits = [_edit("models/pkg_task.sql")]
    assert (
        _one(_manifest(), required=["pkg_task"], edits=edits)["plan_relation"]
        == "independent"
    )
    assert (
        _one(_manifest(), node=OWN, required=["stg"], edits=[_edit("models/stg.sql")])[
            "plan_relation"
        ]
        == "edited"
    )


@pytest.mark.parametrize(
    "kind",
    [
        EditKind.MACRO_SQL,
        EditKind.PROJECT_YML,
        EditKind.PACKAGES_YML,
        EditKind.TEST_SQL,
    ],
)
def test_a_plan_that_can_reach_any_node_claims_nothing(kind):
    entry = _one(_manifest(), required=[], edits=[_edit("x", kind)])
    assert entry["plan_relation"] == "undetermined"


def test_no_plan_claims_nothing():
    assert _one(_manifest())["plan_relation"] == "undetermined"


def test_blocks_names_required_descendants_only():
    entry = _one(_manifest(), required=["rpt", "stg"], edits=[_edit("models/rpt.sql")])
    assert entry["blocks"] == ["rpt"]


def _versioned_manifest() -> dict:
    v2 = "model.proj.dim_customers.v2"
    v1 = "model.proj.dim_customers.v1"
    return _manifest(
        **{
            v1: _node(v1, original_file_path="models/dim_customers_v1.sql"),
            v2: _node(v2, PKG, original_file_path="models/dim_customers_v2.sql"),
            "model.proj.reader": _node("model.proj.reader", v2),
        }
    )


def test_an_edited_versioned_model_is_not_independent():
    edit = {"required": ["dim_customers_v2"], "edits": [_edit("models/x_v2.sql")]}
    manifest = _versioned_manifest()
    assert (
        _one(manifest, node="model.proj.dim_customers.v2", **edit)["plan_relation"]
        == "edited"
    )
    assert _one(manifest, node="model.proj.reader", **edit)["plan_relation"] == (
        "downstream_of_edit"
    )
    # The sibling version is not what the plan edited.
    assert _one(manifest, node="model.proj.dim_customers.v1", **edit)[
        "plan_relation"
    ] == ("independent")


def test_a_versioned_deliverable_is_named_in_blocks():
    entry = _one(
        _versioned_manifest(),
        required=["dim_customers_v2"],
        edits=[_edit("models/dim_customers_v2.sql")],
    )
    assert entry["blocks"] == ["dim_customers_v2"]


def test_a_model_whose_file_stem_is_its_required_name_is_matched_with_no_path():
    """dbt's convention alone, for a manifest that carries no file path."""

    v2 = "model.proj.dim_customers.v2"
    entry = _one(
        _manifest(**{v2: _node(v2, PKG)}),
        required=["dim_customers_v2"],
        edits=[_edit("models/dim_customers_v2.sql")],
    )
    assert entry["blocks"] == ["dim_customers_v2"]


def test_a_failed_unit_test_of_an_edited_model_is_not_independent():
    test = "unit_test.proj.stg.t1"
    manifest = _manifest()
    manifest["unit_tests"] = {
        test: {
            "name": "t1",
            "resource_type": "unit_test",
            "depends_on": {"nodes": [OWN]},
        }
    }
    entry = _one(manifest, node=test, required=["stg"], edits=[_edit("models/stg.sql")])
    assert entry["plan_relation"] == "downstream_of_edit"
    assert entry["node"] == "t1"


@pytest.mark.parametrize(
    "content",
    ["sources:\n  - name: raw\n    schema: other\n", ": not yaml: ["],
)
def test_a_schema_yaml_beyond_its_named_nodes_claims_nothing(content):
    edits = [
        _edit("models/stg.sql"),
        PlanEdit(path="models/s.yml", kind=EditKind.SCHEMA_YML, new_content=content),
    ]
    assert _one(_manifest(), required=["stg"], edits=edits)["plan_relation"] == (
        "undetermined"
    )


def test_a_schema_yaml_describing_its_model_keeps_the_claim():
    edits = [
        PlanEdit(
            path="models/s.yml",
            kind=EditKind.SCHEMA_YML,
            new_content="version: 2\nmodels:\n  - name: stg\n",
        )
    ]
    assert _one(_manifest(), required=["stg"], edits=edits)["plan_relation"] == (
        "independent"
    )


def test_a_deletion_claims_nothing():
    edits = [PlanEdit(path="models/stg.sql", kind=EditKind.MODEL_SQL, op=EditOp.DELETE)]
    assert _one(_manifest(), required=["stg"], edits=edits)["plan_relation"] == (
        "undetermined"
    )


def test_a_versioned_model_cannot_be_overridden():
    manifest = _manifest()
    manifest["nodes"][PKG]["version"] = 2
    assert _option(_one(manifest), "project_override")["supported"] is False


def test_a_python_model_cannot_be_overridden():
    manifest = _manifest()
    manifest["nodes"][PKG]["language"] = "python"
    assert _option(_one(manifest), "project_override")["supported"] is False


def test_a_package_test_cannot_be_overridden():
    test = "test.pkg.not_null_x.abc"
    entry = _one(_manifest(**{test: _node(test, PKG)}), node=test)
    assert _option(entry, "project_override")["supported"] is False


def test_a_dispatching_macro_is_named_and_a_plain_one_is_not():
    manifest = _manifest()
    manifest["nodes"][PKG]["depends_on"]["macros"] = [
        "macro.pkg.fix",
        "macro.pkg.plain",
    ]
    manifest["macros"] = {
        "macro.pkg.fix": {
            "name": "fix",
            "package_name": "pkg",
            "macro_sql": "{{ return(adapter.dispatch('fix', 'pkg')()) }}",
        },
        "macro.pkg.plain": {
            "name": "plain",
            "package_name": "pkg",
            "macro_sql": "select 1",
        },
    }
    option = _option(_one(manifest), "macro_dispatch")
    assert option["supported"] is True
    assert option["macros"] == ["pkg.fix"]


def test_a_nested_dispatching_macro_is_found_transitively():
    """A plain wrapper that depends on a dispatching package macro still
    supports macro_dispatch (#1063)."""
    manifest = _manifest()
    manifest["nodes"][PKG]["depends_on"]["macros"] = ["macro.pkg.wrapper"]
    manifest["macros"] = {
        "macro.pkg.wrapper": {
            "name": "wrapper",
            "package_name": "pkg",
            "macro_sql": "{{ pkg.dispatching() }}",
            "depends_on": {"macros": ["macro.pkg.dispatching"]},
        },
        "macro.pkg.dispatching": {
            "name": "dispatching",
            "package_name": "pkg",
            "macro_sql": "{{ return(adapter.dispatch('dispatching', 'pkg')()) }}",
        },
    }
    option = _option(_one(manifest), "macro_dispatch")
    assert option["supported"] is True
    assert option["macros"] == ["pkg.dispatching"]


def test_a_macro_dependency_cycle_does_not_hang():
    """Cycle through depends_on.macros must terminate (#1063)."""
    manifest = _manifest()
    manifest["nodes"][PKG]["depends_on"]["macros"] = ["macro.pkg.a"]
    manifest["macros"] = {
        "macro.pkg.a": {
            "name": "a",
            "package_name": "pkg",
            "macro_sql": "{{ pkg.b() }}",
            "depends_on": {"macros": ["macro.pkg.b"]},
        },
        "macro.pkg.b": {
            "name": "b",
            "package_name": "pkg",
            "macro_sql": "{{ pkg.a() }}",
            "depends_on": {"macros": ["macro.pkg.a"]},
        },
    }
    option = _option(_one(manifest), "macro_dispatch")
    assert option["supported"] is False


def test_dbt_utils_counts_as_a_package_and_an_adapter_does_not():
    manifest = _manifest()
    manifest["nodes"][PKG]["depends_on"]["macros"] = [
        "macro.dbt_utils.star",
        "macro.dbt_duckdb.internal",
    ]
    dispatching = "{{ return(adapter.dispatch('m', 'p')()) }}"
    manifest["macros"] = {
        "macro.dbt_utils.star": {
            "name": "star",
            "package_name": "dbt_utils",
            "macro_sql": dispatching,
        },
        "macro.dbt_duckdb.internal": {
            "name": "internal",
            "package_name": "dbt_duckdb",
            "macro_sql": dispatching,
        },
    }
    assert _option(_one(manifest), "macro_dispatch")["macros"] == ["dbt_utils.star"]


def test_an_unknown_root_never_guesses_an_owner():
    manifest = _manifest()
    del manifest["metadata"]
    entry = _one(manifest)
    assert entry["owner"] == "unknown"
    assert "options" not in entry


def test_nothing_offers_an_edit_under_dbt_packages():
    assert "dbt_packages" not in json.dumps(_one(_manifest()))


@pytest.mark.parametrize(
    ("path", "content"),
    [
        (
            "dbt_project.yml",
            "name: p\ndispatch:\n  - macro_namespace: pkg\n"
            "    search_order: [p, pkg]\n",
        ),
        (
            "packages.yml",
            "packages:\n  - git: https://example.com/pkg.git\n    revision: abc\n",
        ),
    ],
)
def test_a_repairs_dependency_edits_stay_classified(path, content):
    assert classify_content(content, path=path).artifact_class != "declarative"


def _many_failures(count: int) -> list[BuildNode]:
    return [
        BuildNode(
            unique_id=f"model.pkg.a_rather_long_package_model_name_{i}",
            name=f"a_rather_long_package_model_name_{i}",
            status="error",
            message="x" * 150,
        )
        for i in range(count)
    ]


def test_new_fields_stay_inside_the_budget():
    nodes = _many_failures(40)
    prerequisites = [
        {
            "unique_id": n.unique_id,
            "owner": "package",
            "package": "pkg",
            "plan_relation": "independent",
        }
        for n in nodes
    ]
    cold = {
        "ran": True,
        "reinstalled": True,
        "installed_edits": {
            "changed": {
                "count": 30,
                "paths": [f"pkg/models/m{i}.sql" for i in range(20)],
            },
            "not_compared": [f"local_package_{i}" for i in range(20)],
        },
    }
    completion = summarize_completion(
        BuildEvidence(outcome=BuildOutcome.FAILED, nodes=nodes),
        None,
        prerequisites=prerequisites,
        cold=cold,
        prose_population=[f"some_rather_long_model_{i}" for i in range(50)],
    )

    assert completion.size() <= COMPLETION_BUDGET_BYTES
    assert completion.cold.installed_edits_lost == 30
    assert completion.obligations.prose_population.count == 50
    assert completion.details[:2] == ["data.cold_verification", "data.prerequisites"]
