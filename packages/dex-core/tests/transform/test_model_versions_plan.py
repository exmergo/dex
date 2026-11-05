"""Native dbt model versions in a plan, read from the files alone (#859).

What a plan writes for a versioned model is reported under
``data.model_versions`` and refused where dbt would build it wrong or drop it
silently. Every case here is static: no dbt runs. The manifest half is driven
with a hand-written manifest; the real parse and build are in
``test_model_versions_build.py``.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.transform import model_versions as versions_mod
from exmergo_dex_core.transform.completion import node_answers
from exmergo_dex_core.transform.evidence import BuildNode, required_nodes
from exmergo_dex_core.transform.plans import (
    EditKind,
    EditOp,
    PlanEdit,
    PlanError,
    validate_deletions,
)

DIM = "select * from (values (1, 'a')) as t(team_id, team_label)\n"
V2 = "select team_id, team_label as display_label from {{ ref('dim_teams', v=1) }}\n"
RPT = "select team_id, team_label from {{ ref('dim_teams') }}\n"

PLAIN_YML = """\
version: 2
models:
  - name: dim_teams
    columns:
      - name: team_id
      - name: team_label
"""

VERSIONED_YML = """\
version: 2
models:
  - name: dim_teams
    latest_version: 1
    columns:
      - name: team_id
      - name: team_label
    versions:
      - v: 1
        defined_in: dim_teams
        config:
          alias: dim_teams
      - v: 2
        columns:
          - include: all
            exclude: [team_label]
          - name: display_label
"""


def _project() -> dict[str, str]:
    return {
        "models/dim_teams.sql": DIM,
        "models/rpt_teams.sql": RPT,
        "models/_models.yml": PLAIN_YML,
    }


def _view(files: dict[str, str]) -> DbtProjectView:
    return DbtProjectView(
        root=".",
        project_name="shop",
        profile_name="shop",
        files={
            path: SourceFile(path=path, content=content, sha256="x")
            for path, content in files.items()
        },
    )


def _sql(path: str, content: str) -> PlanEdit:
    return PlanEdit(path=path, kind=EditKind.MODEL_SQL, new_content=content)


def _yml(content: str, path: str = "models/_models.yml") -> PlanEdit:
    return PlanEdit(path=path, kind=EditKind.SCHEMA_YML, new_content=content)


def _initial() -> list[PlanEdit]:
    return [
        _sql("models/dim_teams_v2.sql", V2),
        _sql(
            "models/rpt_display.sql",
            "select team_id, display_label from {{ ref('dim_teams', v=2) }}\n",
        ),
        _yml(VERSIONED_YML),
    ]


# --- the report -------------------------------------------------------------------


def test_initial_versioning_reports_versions_default_and_consumers():
    report, warnings = versions_mod.analyze(_view(_project()), _initial())
    entry = report["dim_teams"]
    assert [
        (v["label"], v["file"], v["file_source"], v["relation"])
        for v in entry["versions"]
    ] == [
        ("dim_teams.v1", "models/dim_teams.sql", "defined_in", "dim_teams"),
        ("dim_teams.v2", "models/dim_teams_v2.sql", "suffix", "dim_teams_v2"),
    ]
    assert entry["latest_version"] == {"before": None, "after": "1"}
    assert entry["default"] == {"before": "dim_teams", "after": "dim_teams.v1"}
    consumers = {c["model"]: c for c in entry["consumers"]}
    assert consumers["rpt_teams"]["after"] == "dim_teams.v1"
    assert consumers["rpt_teams"]["changed"] is False
    assert consumers["rpt_display"]["after"] == "dim_teams.v2"
    # The pinned alias keeps the old relation's name, and the old columns hold.
    assert entry["relation_changes"] == []
    assert entry["contract_changes"] == []
    assert entry["manifest"]["status"] == "unchecked"
    assert warnings == []


def test_versioning_without_pinning_the_alias_warns_the_relation_moves():
    yml = VERSIONED_YML.replace("        config:\n          alias: dim_teams\n", "")
    report, warnings = versions_mod.analyze(
        _view(_project()), [*_initial()[:2], _yml(yml)]
    )
    assert report["dim_teams"]["relation_changes"] == [
        {
            "file": "models/dim_teams.sql",
            "before": {"model": "dim_teams", "relation": "dim_teams"},
            "after": {"model": "dim_teams.v1", "relation": "dim_teams_v1"},
        }
    ]
    assert any("alias: dim_teams" in w for w in warnings)


def test_changing_the_old_versions_columns_warns():
    yml = VERSIONED_YML.replace(
        "      - name: team_label\n", "      - name: team_name\n", 1
    )
    report, warnings = versions_mod.analyze(
        _view(_project()), [*_initial()[:2], _yml(yml)]
    )
    (change,) = report["dim_teams"]["contract_changes"]
    assert change["model"] == "dim_teams.v1"
    assert change["removed"] == ["team_label"]
    assert any("reads a different contract" in w for w in warnings)


def test_a_promotion_reports_every_consumer_whose_resolution_changes():
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    entry = report["dim_teams"]
    assert entry["default"] == {"before": "dim_teams.v1", "after": "dim_teams.v2"}
    (consumer,) = entry["consumers"]
    assert (
        consumer["model"],
        consumer["before"],
        consumer["after"],
        consumer["changed"],
    ) == (
        "rpt_teams",
        "dim_teams.v1",
        "dim_teams.v2",
        True,
    )
    assert any("(a promotion)" in w and "rpt_teams" in w for w in warnings)


def test_an_untouched_versioned_project_reports_nothing():
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    edit = _sql("models/stg_other.sql", "select 1 as id\n")
    assert versions_mod.analyze(_view(files), [edit]) == (None, [])


def test_adding_a_third_version_to_a_versioned_model():
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    three = VERSIONED_YML + "      - v: 3\n"
    report, _ = versions_mod.analyze(
        _view(files),
        [_sql("models/dim_teams_v3.sql", "select 1 as team_id\n"), _yml(three)],
    )
    labels = [v["label"] for v in report["dim_teams"]["versions"]]
    assert labels == ["dim_teams.v1", "dim_teams.v2", "dim_teams.v3"]
    assert report["dim_teams"]["default"] == {
        "before": "dim_teams.v1",
        "after": "dim_teams.v1",
    }


def test_a_custom_defined_in_maps_the_version_to_its_file():
    yml = VERSIONED_YML.replace(
        "      - v: 2\n", "      - v: 2\n        defined_in: teams_next\n"
    )
    report, _ = versions_mod.analyze(
        _view(_project()), [_sql("models/teams_next.sql", V2), _yml(yml)]
    )
    v2 = report["dim_teams"]["versions"][1]
    assert (v2["file"], v2["file_source"]) == ("models/teams_next.sql", "defined_in")


# --- what is refused, and what is only named -------------------------------------


def test_a_suffixed_copy_is_named_as_an_independent_model():
    report, warnings = versions_mod.analyze(
        _view(_project()), [_sql("models/dim_teams_v2.sql", V2)]
    )
    assert report["independent_models"] == [
        {
            "file": "models/dim_teams_v2.sql",
            "model": "dim_teams_v2",
            "resembles": "dim_teams.v2",
            "status": "independent_model",
        }
    ]
    assert any("not version 2 of 'dim_teams'" in w for w in warnings)


def test_a_version_with_no_file_is_refused_where_dbt_only_warns():
    edits = [_yml(VERSIONED_YML)]  # no dim_teams_v2.sql
    with pytest.raises(PlanError, match="version_file_missing"):
        versions_mod.analyze(_view(_project()), edits)


def test_promoting_with_the_old_version_in_the_unsuffixed_file_is_refused():
    yml = VERSIONED_YML.replace("        defined_in: dim_teams\n", "").replace(
        "latest_version: 1", "latest_version: 2"
    )
    edits = [_sql("models/dim_teams_v2.sql", V2), _yml(yml)]
    with pytest.raises(PlanError, match="defined_in: dim_teams"):
        versions_mod.analyze(_view(_project()), edits)


def test_a_plain_entry_beside_a_version_of_the_same_file_is_refused():
    yml = VERSIONED_YML + "  - name: dim_teams_v2\n"
    with pytest.raises(PlanError, match="plain model entry"):
        versions_mod.analyze(
            _view(_project()), [_sql("models/dim_teams_v2.sql", V2), _yml(yml)]
        )


# --- the manifest ------------------------------------------------------------------


def _manifest(default: int = 1, rpt_depends: str = "model.shop.dim_teams.v1") -> dict:
    def model(uid: str, path: str, **extra) -> dict:
        return {"resource_type": "model", "original_file_path": path, **extra}

    return {
        "metadata": {"dbt_version": "1.12.5"},
        "nodes": {
            "model.shop.dim_teams.v1": model(
                "",
                "models/dim_teams.sql",
                latest_version=default,
                relation_name='"dev"."main"."dim_teams"',
            ),
            "model.shop.dim_teams.v2": model(
                "",
                "models/dim_teams_v2.sql",
                latest_version=default,
                relation_name='"dev"."main"."dim_teams_v2"',
            ),
            "model.shop.rpt_teams": model(
                "", "models/rpt_teams.sql", depends_on={"nodes": [rpt_depends]}
            ),
            "model.shop.rpt_display": model(
                "",
                "models/rpt_display.sql",
                depends_on={"nodes": ["model.shop.dim_teams.v2"]},
            ),
        },
    }


def test_the_manifest_confirms_ids_relations_and_resolution():
    report, _ = versions_mod.analyze(_view(_project()), _initial())
    assert versions_mod.confirm(report, _manifest(), "shop") == []
    entry = report["dim_teams"]
    assert entry["manifest"] == {"status": "confirmed"}
    assert [v["unique_id"] for v in entry["versions"]] == [
        "model.shop.dim_teams.v1",
        "model.shop.dim_teams.v2",
    ]
    assert entry["versions"][0]["relation"] == "dev.main.dim_teams"
    assert entry["versions"][0]["relation_source"] == "manifest"


def test_a_windows_manifest_path_confirms_like_a_posix_one():
    # dbt writes original_file_path with the platform's separator, so a
    # Windows manifest says models\dim_teams_v2.sql where the plan says
    # models/dim_teams_v2.sql. Read as different files, every version would be
    # a file mismatch and every consumer missing.
    manifest = _manifest()
    for node in manifest["nodes"].values():
        node["original_file_path"] = node["original_file_path"].replace("/", "\\")
    report, _ = versions_mod.analyze(_view(_project()), _initial())
    assert versions_mod.confirm(report, manifest, "shop") == []
    assert report["dim_teams"]["manifest"] == {"status": "confirmed"}


def test_the_manifest_contradicts_a_misresolved_ref_and_default():
    report, _ = versions_mod.analyze(_view(_project()), _initial())
    lines = versions_mod.confirm(
        report, _manifest(default=2, rpt_depends="model.shop.dim_teams.v2"), "shop"
    )
    codes = {m["code"] for m in report["dim_teams"]["manifest"]["mismatches"]}
    assert codes == {"version_default_contradicted", "version_ref_misresolved"}
    assert lines


def test_build_records_hold_status_and_findings_name_the_failure():
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    report = versions_mod.build_report(files, [_yml(VERSIONED_YML)], ["models"])
    versions_mod.confirm(
        report,
        _manifest(),
        "shop",
        statuses={
            "model.shop.dim_teams.v1": "success",
            "model.shop.dim_teams.v2": "error",
        },
    )
    records = {(r["model"], r["check"]): r for r in versions_mod.records(report)}
    assert records[("dim_teams.v1", "version")]["status"] == "satisfied"
    assert records[("dim_teams.v2", "version")]["status"] == "contradicted"
    assert records[("dim_teams", "default")]["status"] == "satisfied"
    assert records[("rpt_teams", "ref")]["status"] == "satisfied"
    assert [f["code"] for f in versions_mod.findings(report)] == ["version_not_built"]


def test_a_dbt_without_model_versions_is_named():
    assert versions_mod.dbt_version_supported(
        {"metadata": {"dbt_version": "1.4.9"}}
    ) == (
        False,
        "1.4.9",
    )
    assert versions_mod.dbt_version_supported({"metadata": {"dbt_version": "1.12.5"}})[
        0
    ]


# --- evidence and deletions ---------------------------------------------------------


def test_required_nodes_name_each_version_by_label(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0.0"\nprofile: shop\n'
    )
    for path, content in _project().items():
        (tmp_path / path).write_text(content)
    required, _basis, undetermined = required_nodes(_initial(), tmp_path)
    assert not undetermined
    assert set(required) == {"dim_teams.v1", "dim_teams.v2", "rpt_display"}


def test_a_custom_defined_in_file_answers_for_its_version():
    node = BuildNode(
        unique_id="model.shop.dim_teams.v2", name="dim_teams.v2", status="success"
    )
    assert "dim_teams.v2" in node_answers(node)


def test_renaming_the_file_of_a_now_versioned_model_is_not_a_dangling_ref():
    yml = VERSIONED_YML.replace("        defined_in: dim_teams\n", "").replace(
        "        config:\n          alias: dim_teams\n", ""
    )
    files = _project()
    edits = [
        PlanEdit(
            path="models/dim_teams.sql", kind=EditKind.MODEL_SQL, op=EditOp.DELETE
        ),
        _sql("models/dim_teams_v1.sql", DIM),
        _sql("models/dim_teams_v2.sql", V2),
        _yml(yml),
    ]
    assert validate_deletions(_view(files), edits) == []


# --- a dotted source name is not a version label -----------------------------------


def test_a_source_parent_is_not_split_as_a_version():
    from exmergo_dex_core.explore.relationships import declaration_relationships
    from exmergo_dex_core.project_definitions import (
        DeclaredColumnMeaning,
        ModelDeclaration,
        ProjectDefinitions,
    )

    defs = ProjectDefinitions(
        present=True,
        model_declarations={
            "fct_orders": ModelDeclaration(
                model="fct_orders",
                source="yaml",
                columns={
                    "vendor_id": DeclaredColumnMeaning(
                        references="raw.vendors.vendor_id"
                    )
                },
            )
        },
    )
    edges, notes = declaration_relationships(defs, ["main.fct_orders", "raw.vendors"])
    assert [(e.from_dataset, e.to_dataset, e.to_columns) for e in edges] == [
        ("main.fct_orders", "raw.vendors", ["vendor_id"])
    ]
    assert not notes, notes


# --- promotion consumer column checks (#1124) -------------------------------------


def test_promotion_moving_consumer_reading_lacking_column_warns():
    """a promotion moving a consumer reading a dropped column warns."""
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    assert report is not None
    assert any(
        "models/rpt_teams.sql: 'rpt_teams' reads column(s) team_label from "
        "dim_teams.v1, which dim_teams.v2 (the new default) lacks" in w
        for w in warnings
    )


def test_promotion_moving_consumer_reading_present_column_does_not_warn():
    """a promotion whose moved consumers read only present columns adds no warning."""
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    files["models/rpt_teams.sql"] = "select team_id from {{ ref('dim_teams') }}\n"
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    assert report is not None
    assert not any("the new default) lacks" in w for w in warnings)
    assert not any("consumer was unchecked" in w for w in warnings)


def test_promotion_moving_consumer_with_star_does_not_warn():
    """a moving consumer reading star expands against the new default
    without warning.
    """
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    files["models/rpt_teams.sql"] = "select * from {{ ref('dim_teams') }}\n"
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    assert report is not None
    assert not any("the new default) lacks" in w for w in warnings)
    assert not any("consumer was unchecked" in w for w in warnings)


def test_promotion_pinned_consumer_reading_dropped_column_does_not_warn():
    """a pinned consumer is not checked even if reading dropped columns."""
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    files["models/rpt_pinned.sql"] = (
        "select team_id, team_label from {{ ref('dim_teams', v=1) }}\n"
    )
    files["models/rpt_teams.sql"] = "select team_id from {{ ref('dim_teams') }}\n"
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    assert report is not None
    assert not any("rpt_pinned" in w for w in warnings)
    assert not any("the new default) lacks" in w for w in warnings)


def test_promotion_consumer_with_unresolvable_select_list_warns_unchecked():
    """a consumer whose select list cannot be read is named as unchecked."""
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    files["models/rpt_teams.sql"] = (
        "select {{ my_macro('team_id') }} from {{ ref('dim_teams') }}\n"
    )
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    assert report is not None
    assert any(
        "models/rpt_teams.sql: 'rpt_teams' moves from dim_teams.v1 to "
        "dim_teams.v2, but its SELECT list could not be read"
        in w
        and "consumer was unchecked" in w
        for w in warnings
    )


def test_promotion_consumer_reading_lacking_column_via_cte_warns():
    """a moving consumer reading a lacking column through a cte warns."""
    files = _project()
    files["models/_models.yml"] = VERSIONED_YML
    files["models/dim_teams_v2.sql"] = V2
    files["models/rpt_teams.sql"] = (
        "with base as (\n"
        "    select team_id, team_label from {{ ref('dim_teams') }}\n"
        ")\n"
        "select team_id, team_label from base\n"
    )
    promoted = VERSIONED_YML.replace("latest_version: 1", "latest_version: 2")
    report, warnings = versions_mod.analyze(_view(files), [_yml(promoted)])
    assert report is not None
    assert any(
        "models/rpt_teams.sql: 'rpt_teams' reads column(s) team_label from "
        "dim_teams.v1, which dim_teams.v2 (the new default) lacks" in w
        for w in warnings
    )
