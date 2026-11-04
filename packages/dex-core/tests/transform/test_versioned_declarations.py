"""Declarations for native dbt model versions (#895).

A version is declared under its label (``dim_teams.v2``) and rendered into that
item of the model's ``versions:`` list, every other byte kept. Every reader of a
declaration then reads it per version, the way dbt builds each one. One rule per
case, over an in-memory project; the end-to-end half with real dbt is in
``test_model_versions_build.py``.
"""

from __future__ import annotations

import difflib

import pytest
import yaml

from exmergo_dex_core import model_versions as mv
from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
from exmergo_dex_core.maintain.drift import (
    declaration_drift,
    rekeyed_versioned_declarations,
)
from exmergo_dex_core.maintain.snapshot import (
    Snapshot,
    TransformLayer,
    declaration_fingerprints,
)
from exmergo_dex_core.transform import declarations as d
from exmergo_dex_core.transform import declare as declare_mod
from exmergo_dex_core.transform.drafting import read_declared

YML = """\
version: 2
models:
  - name: dim_teams
    description: One row per team.  # the shared entry
    latest_version: 1
    columns:
      - name: team_id
        description: The team.
        tests: [unique, not_null]
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
  - name: stg_teams
"""

V1 = "select team_id, team_label from {{ ref('stg_teams') }}\n"
V2 = "select team_id, team_label as display_label from {{ ref('stg_teams') }}\n"


def _files(yml: str = YML) -> dict[str, str]:
    return {
        "models/marts/dim_teams.sql": V1,
        "models/marts/dim_teams_v2.sql": V2,
        "models/staging/stg_teams.sql": "select 1 as team_id, 'a' as team_label\n",
        "models/marts/_marts.yml": yml,
    }


def _view(files: dict[str, str]) -> DbtProjectView:
    return DbtProjectView(
        root=".",
        project_name="p",
        profile_name="p",
        files={
            path: SourceFile(path=path, content=content, sha256="x")
            for path, content in files.items()
        },
    )


def _declare(payload: list[dict], files: dict[str, str] | None = None):
    view = _view(files or _files())
    edits, warnings = d.apply_declarations(d.parse_declarations(payload), view, [])
    return edits, warnings


def _rendered(payload: list[dict], files: dict[str, str] | None = None) -> str:
    edits, _ = _declare(payload, files)
    (edit,) = edits
    assert edit.path == "models/marts/_marts.yml"
    return edit.new_content


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


# --- rendering --------------------------------------------------------------------


def test_a_version_declaration_renders_into_its_versions_item_only():
    text = _rendered(
        [
            {
                "model": "dim_teams.v2",
                "description": "One row per team, label renamed.",
                "grain": ["team_id"],
                "columns": {"display_label": {"role": "dimension"}},
            }
        ]
    )
    entry = yaml.safe_load(text)["models"][0]
    v1, v2 = entry["versions"]
    assert v1 == {"v": 1, "defined_in": "dim_teams", "config": {"alias": "dim_teams"}}
    assert v2["description"] == "One row per team, label renamed."
    assert v2["config"]["meta"]["dex"] == {"grain": ["team_id"]}
    assert v2["columns"][1] == {
        "name": "display_label",
        "config": {"meta": {"dex": {"role": "dimension"}}},
    }
    # The shared entry and the comment on it are untouched: every changed line
    # is an addition inside v2's item.
    assert entry["description"] == "One row per team."
    assert "# the shared entry" in text
    assert all(line.startswith("+") for line in _changed_lines(YML, text))


def test_an_inherited_column_is_forked_into_the_version_with_its_tests():
    edits, warnings = _declare(
        [{"model": "dim_teams.v2", "columns": {"team_id": {"role": "key"}}}]
    )
    v2 = yaml.safe_load(edits[0].new_content)["models"][0]["versions"][1]
    assert v2["columns"][0] == {"include": "all", "exclude": ["team_label", "team_id"]}
    forked = next(c for c in v2["columns"] if c.get("name") == "team_id")
    assert forked["description"] == "The team."
    assert forked["tests"] == ["unique", "not_null"]
    assert forked["config"]["meta"]["dex"] == {"role": "key"}
    assert any("inherited from the shared 'dim_teams' entry" in w for w in warnings)
    # As dbt reads it: v2 has team_id once, with the declared role; v1 keeps
    # the shared entry, with no role.
    variants = dict(
        mv.entry_variants(yaml.safe_load(edits[0].new_content)["models"][0])
    )
    v2_columns = variants["dim_teams.v2"]["columns"]
    assert [c["name"] for c in v2_columns] == ["display_label", "team_id"]
    v1_team = variants["dim_teams.v1"]["columns"][0]
    assert "config" not in v1_team


def test_a_fact_the_inherited_column_already_states_forks_nothing():
    text = _rendered([{"model": "dim_teams.v2", "grain": ["team_id"]}])
    v2 = yaml.safe_load(text)["models"][0]["versions"][1]
    # unique and not_null are already inherited, so the grain adds no column.
    assert v2["columns"] == [
        {"include": "all", "exclude": ["team_label"]},
        {"name": "display_label"},
    ]


def test_an_include_list_drops_the_column_it_forks():
    yml = YML.replace(
        "          - include: all\n            exclude: [team_label]\n",
        "          - include: [team_id]\n",
    )
    text = _rendered(
        [{"model": "dim_teams.v2", "columns": {"team_id": {"role": "key"}}}],
        _files(yml),
    )
    v2 = yaml.safe_load(text)["models"][0]["versions"][1]
    assert v2["columns"][0] == {"include": []}
    assert any(c.get("name") == "team_id" for c in v2["columns"])


def test_a_version_with_no_columns_key_gets_one():
    yml = YML.replace(
        "        columns:\n          - include: all\n"
        "            exclude: [team_label]\n          - name: display_label\n",
        "        description: second\n",
    )
    files = _files(yml)
    files["models/marts/dim_teams_v2.sql"] = V1
    text = _rendered(
        [{"model": "dim_teams.v2", "columns": {"team_label": {"role": "dimension"}}}],
        files,
    )
    v2 = yaml.safe_load(text)["models"][0]["versions"][1]
    assert v2["columns"][0] == {"include": "all", "exclude": ["team_label"]}
    assert v2["columns"][1]["name"] == "team_label"


def test_rendering_twice_is_a_no_op():
    payload = [
        {
            "model": "dim_teams.v2",
            "grain": ["team_id"],
            "columns": {
                "team_id": {"role": "key"},
                "display_label": {"role": "dimension"},
            },
        }
    ]
    once = _rendered(payload)
    files = _files(once)
    assert _rendered(payload, files) == once


def test_a_reference_to_a_version_writes_a_versioned_ref():
    files = _files()
    files["models/marts/fct_games.sql"] = (
        "select 1 as game_id, team_id from {{ ref('dim_teams', v=2) }}\n"
    )
    edits, _ = _declare(
        [
            {
                "model": "fct_games",
                "columns": {
                    "team_id": {
                        "role": "foreign_key",
                        "references": "dim_teams.v2.team_id",
                    }
                },
            }
        ],
        files,
    )
    rendered = next(e for e in edits if e.path.endswith("fct_games.yml")).new_content
    column = yaml.safe_load(rendered)["models"][0]["columns"][0]
    test = (column.get("data_tests") or column["tests"])[0]
    assert test == {
        "relationships": {
            "arguments": {"to": "ref('dim_teams', v=2)", "field": "team_id"}
        }
    }


def test_the_shared_entry_takes_a_bare_name_declaration():
    text = _rendered([{"model": "dim_teams", "columns": {"team_id": {"role": "key"}}}])
    entry = yaml.safe_load(text)["models"][0]
    assert entry["columns"][0]["config"]["meta"]["dex"] == {"role": "key"}
    assert entry["versions"][1]["columns"][0] == {
        "include": "all",
        "exclude": ["team_label"],
    }


# --- refusals ---------------------------------------------------------------------


def test_a_version_file_stem_is_refused_with_its_label():
    with pytest.raises(d.DeclarationError, match=r'"model": "dim_teams.v2"'):
        _declare([{"model": "dim_teams_v2", "grain": ["team_id"]}])


def test_a_version_the_model_does_not_list_is_refused():
    with pytest.raises(d.DeclarationError, match="lists versions 1, 2"):
        _declare([{"model": "dim_teams.v3", "grain": ["team_id"]}])


def test_a_label_on_an_unversioned_model_is_refused():
    with pytest.raises(d.DeclarationError, match="declares no versioned model"):
        _declare([{"model": "stg_teams.v2", "grain": ["team_id"]}])


def test_a_column_the_version_does_not_produce_is_refused():
    with pytest.raises(d.DeclarationError, match="team_label"):
        _declare(
            [{"model": "dim_teams.v2", "columns": {"team_label": {"role": "key"}}}]
        )


def test_a_model_fact_stated_on_the_shared_entry_is_refused_per_version():
    shared = YML.replace(
        "    latest_version: 1\n",
        "    latest_version: 1\n    config:\n      meta:\n        dex:\n"
        "          grain: [team_id]\n",
    )
    with pytest.raises(d.DeclarationError, match="concatenated"):
        _declare([{"model": "dim_teams.v2", "grain": ["team_id"]}], _files(shared))


def test_a_shared_fact_a_version_already_states_is_refused():
    own = YML.replace(
        "      - v: 2\n",
        "      - v: 2\n        config:\n          meta:\n            dex:\n"
        "              grain: [team_id]\n",
    )
    with pytest.raises(d.DeclarationError, match="each version"):
        _declare([{"model": "dim_teams", "grain": ["team_id"]}], _files(own))


def test_a_shared_grain_needs_every_version_to_produce_it():
    files = _files()
    files["models/marts/dim_teams_v2.sql"] = (
        "select team_label as display_label from x\n"
    )
    with pytest.raises(d.DeclarationError, match="not every version produces"):
        _declare([{"model": "dim_teams", "grain": ["team_id"]}], files)


# --- reading it back --------------------------------------------------------------


def _declared_files() -> dict[str, str]:
    return _files(
        _rendered(
            [
                {
                    "model": "dim_teams.v2",
                    "grain": ["team_id"],
                    "assumptions": [
                        {
                            "decision": "label",
                            "chosen": "renamed",
                            "evidence": "request",
                        }
                    ],
                    "columns": {"display_label": {"role": "dimension"}},
                }
            ]
        )
    )


@pytest.mark.parametrize("name", ["dim_teams.v2", "dim_teams_v2"])
def test_read_declared_reads_the_version_as_dbt_builds_it(name):
    declared = read_declared(_declared_files(), name)
    assert declared["grain"] == ["team_id"]
    assert declared["columns"]["display_label"]["role"] == "dimension"


def test_the_bare_name_reads_the_shared_entry():
    assert "grain" not in read_declared(_declared_files(), "dim_teams")


def test_decisions_are_reported_under_the_version_label():
    decisions = d.decisions(_declared_files(), ["dim_teams"])
    assert [x["model"] for x in decisions] == ["dim_teams.v2"]


def test_the_version_file_is_declared_when_its_version_is():
    from exmergo_dex_core.transform.plans import EditKind, PlanEdit

    files = _declared_files()
    edit = PlanEdit(
        path="models/marts/dim_teams_v2.sql", kind=EditKind.MODEL_SQL, new_content=V2
    )
    assert d.undeclared_model_warnings([edit], _view(files), []) == []


def test_snapshot_fingerprints_each_version_under_its_label():
    files = _declared_files()
    prints = declaration_fingerprints(
        [(yaml.safe_load(files["models/marts/_marts.yml"]), "models/marts/_marts.yml")]
    )
    assert "dim_teams.v2" in prints
    assert "dim_teams" not in prints
    assert set(prints["dim_teams.v2"].fields) == {"description", "grain", "assumptions"}


def _layer(files: dict[str, str], versions: list[str] | None) -> TransformLayer:
    path = "models/marts/_marts.yml"
    return TransformLayer(
        models=["dim_teams", "dim_teams_v2", "stg_teams"],
        declarations=declaration_fingerprints([(yaml.safe_load(files[path]), path)]),
        versions=versions,
    )


def test_a_changed_version_declaration_is_drift_on_that_version():
    before = _declared_files()
    after = dict(before)
    after["models/marts/_marts.yml"] = before["models/marts/_marts.yml"].replace(
        "decision: label", "decision: which label"
    )
    labels = ["dim_teams.v1", "dim_teams.v2"]
    snap = Snapshot(
        created_at="2026-11-01T00:00:00+00:00", transform_layer=_layer(before, labels)
    )
    findings = declaration_drift(_layer(after, labels), snap)
    assert [(f.code, f.identifier, f.data.get("field")) for f in findings] == [
        ("declaration_changed", "dim_teams.v2", "assumptions")
    ]


def test_a_baseline_keyed_by_the_bare_name_is_unchecked_not_removed():
    old = TransformLayer(
        models=["dim_teams"],
        declarations=declaration_fingerprints(
            [
                (
                    {"models": [{"name": "dim_teams", "description": "x"}]},
                    "models/marts/_marts.yml",
                )
            ]
        ),
        versions=None,
    )
    snap = Snapshot(created_at="2026-11-01T00:00:00+00:00", transform_layer=old)
    current = _layer(_declared_files(), ["dim_teams.v1", "dim_teams.v2"])
    assert rekeyed_versioned_declarations(current, snap) == ["dim_teams"]
    assert declaration_drift(current, snap) == []


def test_declare_proposes_into_the_version():
    files = _files()
    outcome = declare_mod.propose(
        _view(files),
        ["dim_teams.v2"],
        [],
        draft=lambda model: (
            {
                "drafted": {
                    "grain": {"value": ["team_id"], "evidence": "a proven key"}
                },
                "declared": read_declared(files, model),
                "missing": [],
            },
            [],
        ),
        cap=25,
    )
    (proposal,) = outcome.proposals
    assert proposal.model == "dim_teams.v2"
    assert proposal.path == "models/marts/_marts.yml"
    assert proposal.declaration.grain == ["team_id"]


def test_declare_does_not_repropose_an_inherited_fact():
    files = _files(
        YML.replace(
            "        tests: [unique, not_null]\n",
            "        tests: [unique, not_null]\n        config:\n          meta:\n"
            "            dex:\n              role: key\n",
        )
    )
    declared = read_declared(files, "dim_teams.v2")
    assert declared["columns"]["team_id"]["role"] == "key"
