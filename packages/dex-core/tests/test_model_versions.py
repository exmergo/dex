"""Native dbt model versions read from the repository (#859, #895).

The unit half pins dbt's rules one at a time: which file builds a version, which
version an unversioned ``ref()`` resolves to, and what a version's effective
entry holds. The parity half runs dbt's own parser over the same fixture and
asserts every version's columns, description and meta equal what
:func:`effective_entry` reads, so a disagreement with dbt fails here rather than
in a reader downstream.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from exmergo_dex_core import model_versions as mv

TEAMS_YML = """\
version: 2
models:
  - name: dim_teams
    description: top
    latest_version: 1
    config:
      meta:
        dex:
          grain: [team_id]
          population: {rule: top, filters: [{column: a, excludes: [x]}]}
    columns:
      - name: team_id
        description: id
        tests: [unique]
      - name: team_label
    versions:
      - v: 1
        defined_in: dim_teams
        config: {alias: dim_teams}
      - v: 2
        description: v2 desc
        config:
          meta:
            dex:
              grain: [team_id]
              population: {rule: v2}
        columns:
          - include: all
            exclude: [team_label]
          - name: display_label
            description: shown
"""


def _files(yml: str = TEAMS_YML, **extra: str) -> dict[str, str]:
    files = {
        "models/dim_teams.sql": "select 1 as team_id, 'a' as team_label\n",
        "models/dim_teams_v2.sql": "select 1 as team_id, 'a' as display_label\n",
        "models/schema.yml": yml,
    }
    files.update(extra)
    return files


def test_each_version_is_mapped_to_the_file_dbt_builds_it_from():
    model = mv.version_map(_files(), ["models"])["dim_teams"]
    assert [(v.label, v.file, v.file_source) for v in model.versions] == [
        ("dim_teams.v1", "models/dim_teams.sql", "defined_in"),
        ("dim_teams.v2", "models/dim_teams_v2.sql", "suffix"),
    ]
    assert model.default_label == "dim_teams.v1"


def test_only_the_latest_version_may_live_in_the_unsuffixed_file():
    yml = (
        "version: 2\nmodels:\n  - name: m\n    latest_version: 2\n"
        "    versions:\n      - v: 1\n      - v: 2\n"
    )
    files = {"models/m.sql": "select 1 as id\n", "models/s.yml": yml}
    model = mv.version_map(files, ["models"])["m"]
    assert [(v.v, v.file, v.file_source) for v in model.versions] == [
        ("1", None, "missing"),
        ("2", "models/m.sql", "unsuffixed_latest"),
    ]
    problems = mv.structural_problems(files, ["models"])
    assert [p.code for p in problems] == ["version_file_missing"]
    assert "defined_in: m" in problems[0].detail


@pytest.mark.parametrize(
    ("versions", "latest", "default"),
    [
        (["1", "2"], None, "2"),
        (["1", "2", "10"], None, "10"),
        (["1", "2"], 1, "1"),
        # Mixed identifiers: dbt falls back to alphabetical, so ``2`` beats ``1-beta``.
        (["2", "1-beta"], None, "2"),
        (["1-beta", "2"], None, "2"),
        (["beta", "alpha"], None, "beta"),
    ],
)
def test_the_default_is_latest_version_else_dbt_version_order(
    versions, latest, default
):
    entry = {"name": "m", "versions": [{"v": v} for v in versions]}
    if latest is not None:
        entry["latest_version"] = latest
    assert mv.default_version(entry) == default


def test_refs_resolve_the_way_dbt_resolves_them():
    vmap = mv.version_map(_files(), ["models"])
    assert mv.ref_target("dim_teams", None, vmap) == "dim_teams.v1"
    assert mv.ref_target("dim_teams", 2, vmap) == "dim_teams.v2"
    assert mv.ref_target("dim_teams", "9", vmap) is None
    assert mv.ref_target("stg_teams", None, vmap) is None


@pytest.mark.parametrize(
    ("name", "labels"),
    [
        ("dim_teams.v2", ["dim_teams.v2"]),
        ("DIM_TEAMS", ["dim_teams.v1", "dim_teams.v2"]),
        ("dim_teams_v2", ["dim_teams.v2"]),
        ("dim_teams.v9", []),
        ("stg_teams", None),
    ],
)
def test_a_version_is_named_by_label_bare_name_or_file_stem(name, labels):
    assert mv.resolve(name, mv.version_map(_files(), ["models"])) == labels


def test_a_version_inherits_merges_and_overrides_as_dbt_does():
    entry = mv.version_map(_files(), ["models"])["dim_teams"].entry
    variants = dict(mv.entry_variants(entry))
    v1, v2 = variants["dim_teams.v1"], variants["dim_teams.v2"]
    assert v1["description"] == "top"
    assert [c["name"] for c in v1["columns"]] == ["team_id", "team_label"]
    assert v1["config"]["alias"] == "dim_teams"
    assert v2["description"] == "v2 desc"
    assert [c["name"] for c in v2["columns"]] == ["team_id", "display_label"]
    # dbt's deep_merge concatenates a list stated at both levels and merges a
    # mapping key by key: a shared grain doubles and a shared filter leaks in.
    assert v2["config"]["meta"]["dex"]["grain"] == ["team_id", "team_id"]
    assert v2["config"]["meta"]["dex"]["population"] == {
        "rule": "v2",
        "filters": [{"column": "a", "excludes": ["x"]}],
    }


def test_a_plain_entry_is_its_own_variant():
    assert mv.entry_variants({"name": "stg", "columns": []}) == [
        ("stg", {"name": "stg", "columns": []})
    ]


@pytest.mark.parametrize(
    ("yml", "extra", "code"),
    [
        (
            "version: 2\nmodels:\n  - name: m\n    latest_version: 3\n"
            "    versions:\n      - v: 1\n        defined_in: m\n",
            {"models/m.sql": "select 1 as id\n"},
            "latest_version_unknown",
        ),
        (
            "version: 2\nmodels:\n  - name: m\n    versions:\n"
            "      - v: 1\n        defined_in: m\n"
            "      - v: 2\n        defined_in: m\n",
            {"models/m.sql": "select 1 as id\n"},
            "version_file_collision",
        ),
        (
            "version: 2\nmodels:\n  - name: m\n    versions:\n      - v: 2\n"
            "  - name: m_v2\n",
            {"models/m_v2.sql": "select 1 as id\n"},
            "version_file_collision",
        ),
        (
            "version: 2\nmodels:\n  - name: m\n    versions:\n      - v: 1\n"
            "      - v: 2\n",
            {
                "models/m_v1.sql": "select 1 as id\n",
                "models/m_v2.sql": "select 1 as id\n",
                "models/m.sql": "select 1 as id\n",
            },
            "version_file_collision",
        ),
        (
            "version: 2\nmodels:\n  - name: m\n    versions:\n"
            "      - v: 1\n        defined_in: models/m.sql\n",
            {"models/m.sql": "select 1 as id\n"},
            "defined_in_not_a_stem",
        ),
        (
            "version: 2\nmodels:\n  - name: m\n    versions:\n      - v: 1\n"
            "      - v: 1\n",
            {"models/m_v1.sql": "select 1 as id\n"},
            "version_duplicate",
        ),
    ],
)
def test_what_dbt_would_build_wrong_is_named_by_code(yml, extra, code):
    files = {"models/s.yml": yml, **extra}
    assert code in {p.code for p in mv.structural_problems(files, ["models"])}


def test_a_sound_versioned_model_has_no_problem():
    assert mv.structural_problems(_files(), ["models"]) == []


def test_the_cached_map_reads_a_rewritten_file_afresh():
    files = _files()
    assert mv.cached_version_map(files, ["models"])["dim_teams"].default == "1"
    files["models/schema.yml"] = TEAMS_YML.replace(
        "latest_version: 1", "latest_version: 2"
    )
    assert mv.cached_version_map(files, ["models"])["dim_teams"].default == "2"


# --- parity with dbt's own parser ---------------------------------------------------


def _dbt() -> str:
    pytest.importorskip("dbt.cli.main")
    pytest.importorskip("dbt.adapters.duckdb")
    sibling = Path(sys.executable).with_name("dbt")
    return str(sibling) if sibling.exists() else (shutil.which("dbt") or "dbt")


def test_effective_entries_match_the_manifest_dbt_writes(tmp_path: Path):
    dbt = _dbt()
    (tmp_path / "models").mkdir()
    (tmp_path / "dbt_project.yml").write_text('name: p\nversion: "1.0.0"\nprofile: p\n')
    (tmp_path / "profiles.yml").write_text(
        f"p:\n  target: dev\n  outputs:\n    dev: {{type: duckdb, path: "
        f"{tmp_path / 'dev.duckdb'}}}\n"
    )
    for path, content in _files().items():
        (tmp_path / path).write_text(content)
    completed = subprocess.run(  # noqa: S603 - the dbt beside this interpreter
        [dbt, "parse", "--project-dir", str(tmp_path), "--profiles-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]
    manifest = json.loads((tmp_path / "target" / "manifest.json").read_text())
    vmap = mv.version_map(_files(), ["models"])
    variants = dict(mv.entry_variants(vmap["dim_teams"].entry))
    for item in vmap["dim_teams"].versions:
        node = manifest["nodes"][f"model.p.dim_teams.v{item.v}"]
        effective = variants[item.label]
        assert node["original_file_path"] == item.file
        assert str(node["latest_version"]) == vmap["dim_teams"].default
        assert list(node["columns"]) == [c["name"] for c in effective["columns"]]
        assert node["description"] == effective["description"]
        assert node["config"]["meta"] == effective["config"]["meta"]
