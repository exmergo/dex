"""`transform references`, `rename` and `remove` on a versioned model (#1126).

A versioned model is built from one file per version, so no node carries its base
name: `dim_teams_v1.sql` and `dim_teams_v2.sql` build `dim_teams.v1` and
`dim_teams.v2`, and a consumer reaches one of them through `ref('dim_teams')`
(the latest) or `ref('dim_teams', v=1)`. The base name selects every version and
a label selects one, the way `--select` and `transform declare` read them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import EditOp, load
from exmergo_dex_core.model_versions import entry_variants
from exmergo_dex_core.references import find_references
from exmergo_dex_core.transform.propagate import PropagationRefusedError, propagate

SCHEMA = (
    "version: 2\n"
    "sources:\n"
    "  - name: raw\n"
    "    tables:\n"
    "      - name: teams\n"
    "models:\n"
    "  - name: dim_teams\n"
    "    latest_version: 2\n"
    "    columns:\n"
    "      - name: team_id\n"
    "        data_tests:\n"
    "          - unique\n"
    "      - name: team_label\n"
    "        description: what the team is called\n"
    "        data_tests:\n"
    "          - not_null\n"
    "      - name: team_code\n"
    "    versions:\n"
    "      - v: 1\n"
    "      - v: 2\n"
    "        columns:\n"
    "          - include: all\n"
    "          - name: region\n"
    "      - v: 3\n"
    "        columns:\n"
    "          - include: all\n"
    "            exclude: [team_label]\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """`dim_teams` in three versions, each read by its own consumer.

    v1 and v2 select `team_label` and inherit its YAML entry through `include`;
    v3 neither selects it nor inherits it. `fct_latest` reads the latest version
    (v2) through the base name, `fct_pinned` pins v1, `fct_v3` pins v3, and
    `mart_latest` reads `fct_latest`, one step further down.
    """

    root = tmp_path / "league"
    _write(
        root / "dbt_project.yml",
        'name: league\nprofile: league\nversion: "1.0"\nmodel-paths: [models]\n',
    )
    _write(
        root / "models/stg_teams.sql",
        "select team_id, team_label, team_code, region\n"
        "from {{ source('raw', 'teams') }}\n",
    )
    _write(
        root / "models/dim_teams_v1.sql",
        "select\n"
        "    team_id,\n"
        "    team_label,\n"
        "    team_code\n"
        "from {{ ref('stg_teams') }}\n",
    )
    _write(
        root / "models/dim_teams_v2.sql",
        "select\n"
        "    team_id,\n"
        "    team_label,\n"
        "    team_code,\n"
        "    region\n"
        "from {{ ref('stg_teams') }}\n",
    )
    _write(
        root / "models/dim_teams_v3.sql",
        "select\n    team_id,\n    team_code\nfrom {{ ref('stg_teams') }}\n",
    )
    _write(
        root / "models/fct_latest.sql",
        "select\n    t.team_label\nfrom {{ ref('dim_teams') }} t\n",
    )
    _write(
        root / "models/fct_pinned.sql",
        "select\n    team_label\nfrom {{ ref('dim_teams', v=1) }}\n",
    )
    _write(
        root / "models/fct_v3.sql",
        "select\n    team_id\nfrom {{ ref('dim_teams', version=3) }}\n",
    )
    _write(
        root / "models/mart_latest.sql",
        "select\n    team_label\nfrom {{ ref('fct_latest') }}\n",
    )
    _write(root / "models/schema.yml", SCHEMA)
    return root


def _occurrences(root: Path, name: str) -> list[tuple[str, int, str, str, list]]:
    report = find_references(load(root), [name], kind="column", full=True)
    (target,) = report.targets
    return [
        (
            entry["path"],
            occurrence["line"],
            occurrence["form"],
            occurrence["note"],
            occurrence.get("versions"),
        )
        for entry in target["files"]
        for occurrence in entry["occurrences"]
    ]


def _edits(result) -> dict[str, str | None]:
    return {
        edit.path: edit.new_content if edit.op is EditOp.UPSERT else None
        for edit in result.edits
    }


def _columns(schema: str) -> dict[str, list[str]]:
    """Each version's columns as dbt builds them from ``schema``."""

    entry = yaml.safe_load(schema)["models"][0]
    return {
        label: [column["name"] for column in variant.get("columns", [])]
        for label, variant in entry_variants(entry)
    }


def _rename(root: Path, name: str, new: str):
    return propagate(load(root), root, "column", name, new)


class TestReferences:
    def test_the_base_name_lists_every_version_entry_and_consumer(self, project):
        """The issue's first criterion: every version file, the `versions:`
        entries and the consumers, each attributed to the version it reaches."""

        found = _occurrences(project, "dim_teams.team_label")
        resolved = {
            (o[0], o[2], tuple(o[4] or ()))
            for o in found
            if o[3] != "same_name_elsewhere"
        }

        assert resolved == {
            ("models/dim_teams_v1.sql", "select_column", ("dim_teams.v1",)),
            ("models/dim_teams_v2.sql", "select_column", ("dim_teams.v2",)),
            ("models/fct_latest.sql", "select_column", ("dim_teams.v2",)),
            ("models/mart_latest.sql", "select_column", ("dim_teams.v2",)),
            ("models/fct_pinned.sql", "select_column", ("dim_teams.v1",)),
            (
                "models/schema.yml",
                "yaml_column",
                ("dim_teams.v1", "dim_teams.v2"),
            ),
            # v3's `exclude` names the top-level entry, which reaches v1 and
            # v2; a rename that left it behind would hand v3 the column.
            (
                "models/schema.yml",
                "yaml_version_exclude",
                ("dim_teams.v1", "dim_teams.v2"),
            ),
        }
        assert ("models/schema.yml", 27, "yaml_version_exclude") in {
            (o[0], o[1], o[2]) for o in found
        }
        # The staging model upstream shares the name and is not in the lineage.
        assert (
            "models/stg_teams.sql",
            1,
            "select_column",
            "same_name_elsewhere",
            None,
        ) in found

    def test_a_label_selects_one_version_and_its_consumers(self, project):
        found = _occurrences(project, "dim_teams.v1.team_label")
        notes = {o[0]: o[3] for o in found if o[2] == "select_column"}

        assert notes["models/dim_teams_v1.sql"] == "lineage_resolved"
        assert notes["models/fct_pinned.sql"] == "lineage_resolved"
        assert notes["models/dim_teams_v2.sql"] == "same_name_elsewhere"
        assert notes["models/fct_latest.sql"] == "same_name_elsewhere"
        assert notes["models/mart_latest.sql"] == "same_name_elsewhere"
        yml = [o for o in found if o[2] == "yaml_column"]
        assert yml == [
            (
                "models/schema.yml",
                13,
                "yaml_column",
                "lineage_resolved",
                ["dim_teams.v1"],
            )
        ]

    def test_a_versions_own_entry_belongs_to_that_version(self, project):
        found = _occurrences(project, "dim_teams.region")
        yml = [o for o in found if o[0] == "models/schema.yml"]

        assert yml == [
            (
                "models/schema.yml",
                23,
                "yaml_column",
                "lineage_resolved",
                ["dim_teams.v2"],
            )
        ]

    def test_an_unknown_version_is_a_limit_not_a_guess(self, project):
        report = find_references(load(project), ["dim_teams.v9.team_label"])

        assert report.completeness == "incomplete"
        assert any("has no version 'v9'" in limit for limit in report.limits)

    def test_a_ref_to_the_base_name_names_the_version_it_resolves_to(self, project):
        report = find_references(load(project), ["dim_teams"], kind="model")
        (target,) = report.targets
        refs = {
            entry["path"]: occurrence.get("versions")
            for entry in target["files"]
            for occurrence in entry["occurrences"]
            if occurrence["form"] == "ref_call"
        }

        assert target["versions"] == ["dim_teams.v1", "dim_teams.v2", "dim_teams.v3"]
        assert refs == {
            "models/fct_latest.sql": ["dim_teams.v2"],
            "models/fct_pinned.sql": ["dim_teams.v1"],
            "models/fct_v3.sql": ["dim_teams.v3"],
        }

    def test_a_label_is_answered_as_that_version_of_the_model(self, project):
        """`dim_teams.v1` with no `--kind` names a model: the file that builds
        it and the `ref()` calls that resolve to it, not a column called `v1`."""

        report = find_references(load(project), ["dim_teams.v1"])
        (target,) = report.targets
        found = {
            (entry["path"], occurrence["form"])
            for entry in target["files"]
            for occurrence in entry["occurrences"]
        }

        assert target["kind"] == "model"
        assert target["versions"] == ["dim_teams.v1"]
        assert found == {
            ("models/dim_teams_v1.sql", "definition"),
            ("models/fct_pinned.sql", "ref_call"),
        }

    @pytest.mark.parametrize(
        ("path", "text", "line"),
        [
            (
                "models/fct_dynamic.sql",
                "select team_label from {{ ref('dim_teams', v=var('which')) }}\n",
                1,
            ),
            (
                "models/fct_dynamic.sql",
                "select team_label from {{ ref('dim_teams', v=1 + var('step')) }}\n",
                1,
            ),
            (
                "models/fct_dynamic.sql",
                "select team_label\n"
                "from {{ ref('dim_teams', v=1, version=var('wanted')) }}\n",
                2,
            ),
            (
                "models/fct_dynamic.py",
                "def model(dbt, session):\n"
                "    return dbt.ref('dim_teams', v=dbt.config.get('which'))\n",
                2,
            ),
        ],
        ids=["jinja", "jinja_literal_prefix", "jinja_both_selectors", "python"],
    )
    def test_a_version_pinned_by_an_expression_is_a_limit(
        self, project, path, text, line
    ):
        """Which version such a `ref()` reads is unknown until it compiles, so
        it is neither the latest version nor dropped."""

        _write(project / path, text)

        report = find_references(load(project), ["dim_teams.v1.team_label"])

        assert report.completeness == "incomplete"
        assert any(limit.startswith(f"{path}:{line}:") for limit in report.limits)
        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.v1.team_label", "team_name")
        assert f"{path}:{line}" in str(exc.value)
        assert "pin a literal version" in str(exc.value)

    def test_a_numeric_pin_is_read_as_jinja_reads_it(self, project):
        """`v=1.50` is the number 1.5, which is how YAML reads `v: 1.5`."""

        _write(
            project / "models/schema.yml",
            SCHEMA.replace("      - v: 1\n", "      - v: 1.5\n"),
        )
        _write(project / "models/dim_teams_v1.5.sql", "select 1 as team_label\n")
        _write(
            project / "models/fct_pinned.sql",
            "select\n    team_label\nfrom {{ ref('dim_teams', v=1.50) }}\n",
        )

        report = find_references(load(project), ["dim_teams"], kind="model")
        pinned = [
            occurrence.get("versions")
            for entry in report.targets[0]["files"]
            if entry["path"] == "models/fct_pinned.sql"
            for occurrence in entry["occurrences"]
        ]
        found = _occurrences(project, "dim_teams.v1.5.team_label")

        assert pinned == [["dim_teams.v1.5"]]
        assert (
            "models/fct_pinned.sql",
            2,
            "select_column",
            "lineage_resolved",
            ["dim_teams.v1.5"],
        ) in found

    def test_the_command_reports_the_versions(self, project, capsys):
        rc = main(
            [
                "--repo-root",
                str(project.parent),
                "transform",
                "references",
                "dim_teams.v2.team_label",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        (target,) = payload["data"]["targets"]
        assert target["kind"] == "column"
        assert target["versions"] == ["dim_teams.v2"]


class TestRename:
    def test_a_label_renames_that_version_and_its_consumers_only(self, project):
        """The issue's second criterion, on one version. v1 still inherits the
        top-level entry, so v2 stops inheriting it and declares its own."""

        result = _rename(project, "dim_teams.v2.team_label", "team_name")
        edits = _edits(result)

        assert set(edits) == {
            "models/dim_teams_v2.sql",
            "models/fct_latest.sql",
            "models/mart_latest.sql",
            "models/schema.yml",
        }
        assert "    team_name,\n" in edits["models/dim_teams_v2.sql"]
        assert "t.team_name" in edits["models/fct_latest.sql"]
        columns = _columns(edits["models/schema.yml"])
        assert columns["dim_teams.v1"] == ["team_id", "team_label", "team_code"]
        assert columns["dim_teams.v2"] == [
            "team_id",
            "team_code",
            "region",
            "team_name",
        ]
        assert columns["dim_teams.v3"] == ["team_id", "team_code"]
        own = yaml.safe_load(edits["models/schema.yml"])["models"][0]["versions"][1]
        assert own["columns"][-1] == {
            "name": "team_name",
            "description": "what the team is called",
            "data_tests": ["not_null"],
        }
        assert any("shared with dim_teams.v1" in note for note in result.notes)

    def test_the_base_name_renames_every_version_that_has_the_column(self, project):
        """v3 neither selects `team_label` nor inherits it, so neither its SQL
        nor its consumer is edited; the name in its `exclude` list follows the
        renamed top-level entry, or v3 would start inheriting it."""

        result = _rename(project, "dim_teams.team_label", "team_name")
        edits = _edits(result)

        assert set(edits) == {
            "models/dim_teams_v1.sql",
            "models/dim_teams_v2.sql",
            "models/fct_latest.sql",
            "models/fct_pinned.sql",
            "models/mart_latest.sql",
            "models/schema.yml",
        }
        schema = edits["models/schema.yml"]
        assert schema == SCHEMA.replace("team_label", "team_name")
        columns = _columns(schema)
        assert columns["dim_teams.v1"] == ["team_id", "team_name", "team_code"]
        assert columns["dim_teams.v3"] == ["team_id", "team_code"]
        assert any("'dim_teams.v3' has no column" in note for note in result.notes)

    def test_an_include_list_loses_the_name_for_one_version_and_keeps_it_for_all(
        self, project
    ):
        _write(
            project / "models/schema.yml",
            SCHEMA.replace(
                "          - include: all\n          - name: region\n",
                "          - include: [team_id, team_label]\n"
                "          - name: region\n",
            ),
        )

        one = _edits(_rename(project, "dim_teams.v2.team_label", "team_name"))[
            "models/schema.yml"
        ]
        every = _edits(_rename(project, "dim_teams.team_label", "team_name"))[
            "models/schema.yml"
        ]

        assert "include: [team_id]\n" in one
        assert _columns(one)["dim_teams.v2"] == ["team_id", "region", "team_name"]
        assert _columns(one)["dim_teams.v1"] == ["team_id", "team_label", "team_code"]
        assert "include: [team_id, team_name]\n" in every
        assert _columns(every)["dim_teams.v2"] == ["team_id", "team_name", "region"]

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            (
                "            exclude: [team_code]\n",
                "            exclude: [team_code, team_label]\n",
            ),
            (
                "            exclude:\n              - team_code\n",
                "            exclude:\n              - team_code\n"
                "              - team_label\n",
            ),
        ],
        ids=["flow", "block"],
    )
    def test_an_exclude_list_gains_the_name_in_its_own_style(
        self, project, written, expected
    ):
        _write(
            project / "models/schema.yml",
            SCHEMA.replace(
                "          - include: all\n          - name: region\n",
                f"          - include: all\n{written}          - name: region\n",
            ),
        )

        schema = _edits(_rename(project, "dim_teams.v2.team_label", "team_name"))[
            "models/schema.yml"
        ]

        assert expected in schema
        assert _columns(schema)["dim_teams.v2"] == ["team_id", "region", "team_name"]

    def test_a_version_with_no_columns_block_gains_one(self, project):
        result = _rename(project, "dim_teams.v1.team_label", "team_name")
        schema = _edits(result)["models/schema.yml"]

        assert _columns(schema)["dim_teams.v1"] == ["team_id", "team_code", "team_name"]
        assert _columns(schema)["dim_teams.v2"] == [
            "team_id",
            "team_label",
            "team_code",
            "region",
        ]
        assert set(_edits(result)) == {
            "models/dim_teams_v1.sql",
            "models/fct_pinned.sql",
            "models/schema.yml",
        }

    def test_a_versions_own_entry_is_renamed_in_place(self, project):
        result = _rename(project, "dim_teams.v2.region", "area")
        schema = _edits(result)["models/schema.yml"]

        assert schema == SCHEMA.replace("- name: region", "- name: area")

    def test_an_upstream_rename_reaches_the_versions_and_their_consumers(self, project):
        """The lineage crosses the versioned model: a consumer of `ref('dim_teams')`
        is downstream of `dim_teams_v2.sql`, which reads `stg_teams`."""

        edits = _edits(_rename(project, "stg_teams.team_label", "team_name"))

        assert {
            "models/fct_latest.sql",
            "models/fct_pinned.sql",
            "models/mart_latest.sql",
        } <= set(edits)
        assert _columns(edits["models/schema.yml"])["dim_teams.v2"][1] == "team_name"

    def test_the_command_stores_the_plan(self, project, capsys):
        rc = main(
            [
                "--repo-root",
                str(project.parent),
                "transform",
                "rename",
                "column",
                "dim_teams.v2.team_label",
                "team_name",
            ]
        )
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0, payload
        assert payload["data"]["change"] == "dim_teams.v2.team_label -> team_name"
        assert payload["data"]["plan_id"]


class TestRemove:
    def test_a_versions_column_is_removed_from_that_version_only(self, project):
        result = propagate(load(project), project, "column", "dim_teams.v2.team_code")
        edits = _edits(result)

        assert set(edits) == {"models/dim_teams_v2.sql", "models/schema.yml"}
        assert "team_code" not in edits["models/dim_teams_v2.sql"]
        columns = _columns(edits["models/schema.yml"])
        assert columns["dim_teams.v1"] == ["team_id", "team_label", "team_code"]
        assert columns["dim_teams.v2"] == ["team_id", "team_label", "region"]
        assert columns["dim_teams.v3"] == ["team_id", "team_code"]

    def test_a_column_removed_from_every_version_leaves_no_list_behind(self, project):
        _write(
            project / "models/fct_latest.sql",
            "select\n    t.team_id\nfrom {{ ref('dim_teams') }} t\n",
        )
        _write(project / "models/mart_latest.sql", "select 1 as x\n")
        _write(project / "models/fct_pinned.sql", "select 1 as x\n")

        result = propagate(load(project), project, "column", "dim_teams.team_label")
        schema = _edits(result)["models/schema.yml"]

        assert "team_label" not in schema
        assert "include: all\n" in schema
        assert "exclude: []\n" in schema

    def test_a_read_through_the_base_name_still_refuses_the_removal(self, project):
        with pytest.raises(PropagationRefusedError) as exc:
            propagate(load(project), project, "column", "dim_teams.v2.team_label")

        assert "models/fct_latest.sql:2" in str(exc.value)
        assert "fct_pinned" not in str(exc.value)


class TestRefusals:
    """The issue's last criterion: the existing refusals still apply."""

    def test_a_column_passed_to_a_macro_in_a_consumer_refuses(self, project):
        _write(
            project / "models/fct_macro.sql",
            "select {{ upper_of('team_label') }} as label\n"
            "from {{ ref('dim_teams') }}\n",
        )

        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.v2.team_label", "team_name")

        assert "fct_macro.sql" in str(exc.value)
        assert "as a label" in str(exc.value)

    def test_sql_dex_cannot_read_refuses(self, project):
        _write(project / "models/broken.sql", "select from where (\n")

        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.team_label", "team_name")

        assert "could not parse" in str(exc.value)

    def test_an_unknown_version_refuses(self, project):
        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.v9.team_label", "x")

        assert "has no version 'v9'" in str(exc.value)

    def test_a_version_without_the_column_refuses(self, project):
        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.v3.team_label", "x")

        assert "'dim_teams.v3' does not have a column 'team_label'" in str(exc.value)

    def test_a_flow_style_version_is_refused_not_rewritten(self, project):
        _write(
            project / "models/schema.yml",
            SCHEMA.replace(
                "      - v: 1\n", "      - {v: 1, description: the first}\n"
            ),
        )

        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.v1.team_label", "x")

        assert "flow style" in str(exc.value)

    def test_a_test_the_versions_share_is_not_split(self, project):
        _write(
            project / "models/schema.yml",
            SCHEMA.replace(
                "    latest_version: 2\n",
                "    latest_version: 2\n"
                "    data_tests:\n"
                "      - dbt_utils.unique_combination_of_columns:\n"
                "          combination_of_columns: [team_id, team_label]\n",
            ),
        )

        with pytest.raises(PropagationRefusedError) as exc:
            _rename(project, "dim_teams.v2.team_label", "x")

        assert "will not split one test between versions" in str(exc.value)
