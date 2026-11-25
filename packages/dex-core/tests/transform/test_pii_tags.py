"""Elementary's PII tag on the columns Dex marks as personal data (#1487).

With ``monitors.vendor: elementary`` and the package installed, a plan tags each
personal-data column of the models it writes in ``config.meta.tags``, the model
in ``config.tags``, and turns on ``disable_samples_on_pii_tags`` in
``dbt_project.yml``, so the project's own tests keep no sample of those columns.

The package here is the same stand-in ``test_monitors.py`` uses: an
``elementary`` project under ``dbt_packages`` that dbt parses without the hub.
Whether the real package then keeps no sample is the Elementary fixture's
(``test_elementary_fixture.py``, ``DEX_TEST_ELEMENTARY=1``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.storage import FilesystemStore
from exmergo_dex_core.transform import pii_tags

SCHEMA = """version: 2
models:
  - name: stg_people
    description: One row per person.
    columns:
      - name: person_id
        data_tests: [unique]
      - name: email
        config:
          meta:
            contains_pii: true
            pii_category: email
      - name: segment
"""


def _entry(text: str, model: str = "stg_people") -> dict:
    return next(e for e in yaml.safe_load(text)["models"] if e["name"] == model)


def _column(entry: dict, name: str) -> dict:
    return next(c for c in entry["columns"] if c["name"] == name)


# --- one entry ---------------------------------------------------------------------


def test_a_marked_column_is_tagged_and_its_model_too():
    text, row = pii_tags.tag_entry(SCHEMA, "stg_people", "pii")

    entry = _entry(text)
    assert _column(entry, "email")["config"]["meta"] == {
        "contains_pii": True,
        "pii_category": "email",
        "tags": ["pii"],
    }
    assert entry["config"] == {"tags": ["pii"]}
    assert row == {
        "model_tag": pii_tags.ADDED,
        "columns": [{"column": "email", "status": pii_tags.ADDED}],
    }


def test_a_column_nothing_marks_is_left_exactly_as_written():
    text, _ = pii_tags.tag_entry(SCHEMA, "stg_people", "pii")

    entry = _entry(text)
    assert _column(entry, "segment") == {"name": "segment"}
    assert _column(entry, "person_id") == {
        "name": "person_id",
        "data_tests": ["unique"],
    }


def test_a_model_with_no_marked_column_is_not_tagged():
    unmarked = SCHEMA.replace("contains_pii: true", "contains_pii: false")

    text, row = pii_tags.tag_entry(unmarked, "stg_people", "pii")

    assert (text, row) == (unmarked, None)


@pytest.mark.parametrize("flag", ["null", "''"])
def test_a_null_flag_is_no_answer_and_the_category_still_marks(flag: str):
    marked = SCHEMA.replace("contains_pii: true", f"contains_pii: {flag}")

    _, row = pii_tags.tag_entry(marked, "stg_people", "pii")

    assert row is not None
    assert row["columns"] == [{"column": "email", "status": pii_tags.ADDED}]


@pytest.mark.parametrize("flag", ["false", "no", "'false'"])
def test_every_spelling_of_a_reviewer_s_no_wins(flag: str):
    unmarked = SCHEMA.replace("contains_pii: true", f"contains_pii: {flag}")

    assert pii_tags.tag_entry(unmarked, "stg_people", "pii") == (unmarked, None)


@pytest.mark.parametrize("key", ["pii", "is_pii", "pii_category"])
def test_every_key_a_reader_accepts_as_a_mark_is_tagged(key: str):
    marked = SCHEMA.replace(
        "            contains_pii: true\n            pii_category: email\n",
        f"            {key}: {'email' if key == 'pii_category' else 'true'}\n",
    )

    _, row = pii_tags.tag_entry(marked, "stg_people", "pii")

    assert row["columns"] == [{"column": "email", "status": pii_tags.ADDED}]


def test_tags_already_there_are_kept_and_the_tag_is_appended():
    tagged = SCHEMA.replace(
        "            pii_category: email\n",
        "            pii_category: email\n            tags: [contact]\n",
    ).replace(
        "    description: One row per person.\n",
        "    description: One row per person.\n    config:\n      tags: daily\n",
    )

    text, _ = pii_tags.tag_entry(tagged, "stg_people", "pii")

    entry = _entry(text)
    assert _column(entry, "email")["config"]["meta"]["tags"] == ["contact", "pii"]
    assert entry["config"]["tags"] == ["daily", "pii"]


_HEAD = "version: 2\nmodels:\n  - name: stg_people\n    config:\n      tags: [PII]\n"


@pytest.mark.parametrize(
    "column",
    [
        # `tags`, upper case: the package lowers every tag it reads.
        "      - name: email\n        tags: [PII]\n"
        "        config:\n          meta:\n            contains_pii: true\n",
        "      - name: email\n        config:\n          tags: [pii]\n"
        "          meta:\n            contains_pii: true\n",
        "      - name: email\n        meta:\n          contains_pii: true\n"
        "          tags: [pii]\n",
    ],
)
def test_a_tag_in_any_place_the_package_reads_counts_as_already_tagged(column: str):
    text = _HEAD + "    columns:\n" + column

    out, row = pii_tags.tag_entry(text, "stg_people", "pii")

    assert out == text
    assert row == {
        "model_tag": pii_tags.ALREADY_TAGGED,
        "columns": [{"column": "email", "status": pii_tags.ALREADY_TAGGED}],
    }


def test_a_column_marked_under_a_top_level_meta_is_left_for_a_person():
    text = SCHEMA.replace(
        "        config:\n          meta:\n            contains_pii: true\n"
        "            pii_category: email\n",
        "        meta:\n          contains_pii: true\n",
    )

    out, row = pii_tags.tag_entry(text, "stg_people", "pii")

    assert row["columns"] == [
        {"column": "email", "status": pii_tags.NOT_WRITTEN, "reason": "top_level_meta"}
    ]
    assert "meta" not in _column(_entry(out), "email").get("config", {})


def test_a_version_s_marked_column_is_tagged_on_the_version():
    text = (
        "version: 2\nmodels:\n  - name: dim_people\n    latest_version: 1\n"
        "    versions:\n      - v: 1\n        columns:\n          - name: email\n"
        "            config:\n              meta:\n                contains_pii: true\n"
    )

    out, row = pii_tags.tag_entry(text, "dim_people", "pii")

    [version] = _entry(out, "dim_people")["versions"]
    assert version["columns"][0]["config"]["meta"]["tags"] == ["pii"]
    assert row["columns"] == [
        {"column": "email", "version": "1", "status": pii_tags.ADDED}
    ]


# --- the project's settings ---------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ("name: shop\n", ("pii", "default")),
        (
            "name: shop\nvars:\n  pii_tags: [sensitive, pii]\n",
            ("sensitive", "pii_tags"),
        ),
        ("name: shop\nvars:\n  pii_tags: restricted\n", ("restricted", "pii_tags")),
        # The project's own tests read the block keyed by the root project's
        # name; a block keyed `elementary` reaches only the package's own nodes.
        (
            "name: shop\nvars:\n  pii_tags: [root]\n  shop:\n    pii_tags: [scoped]\n",
            ("scoped", "pii_tags"),
        ),
        (
            "name: shop\nvars:\n  pii_tags: [root]\n"
            "  elementary:\n    pii_tags: [package]\n",
            ("root", "pii_tags"),
        ),
        ("name: shop\nvars:\n  pii_tags: \"{{ var('x') }}\"\n", ("pii", "default")),
        ("name: shop\nvars:\n  pii_tags: []\n", ("pii", "default")),
    ],
)
def test_the_tag_is_the_first_of_the_project_s_pii_tags_or_pii(project, expected):
    assert pii_tags.project_tag(project) == expected


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ("name: shop\n", pii_tags.SWITCH_ABSENT),
        (None, pii_tags.SWITCH_ABSENT),
        (
            "name: shop\nvars:\n  disable_samples_on_pii_tags: true\n",
            pii_tags.SWITCH_ON,
        ),
        (
            "name: shop\nvars:\n  disable_samples_on_pii_tags: false\n",
            pii_tags.SWITCH_OFF,
        ),
        (
            "name: shop\nvars:\n  disable_samples_on_pii_tags: true\n"
            "  shop:\n    disable_samples_on_pii_tags: false\n",
            pii_tags.SWITCH_OFF,
        ),
        (
            "name: shop\nvars:\n  disable_samples_on_pii_tags: true\n"
            "  elementary:\n    disable_samples_on_pii_tags: false\n",
            pii_tags.SWITCH_ON,
        ),
        (
            "name: shop\nvars:\n  elementary:\n    disable_samples_on_pii_tags: true\n",
            pii_tags.SWITCH_ABSENT,
        ),
        (
            'name: shop\nvars:\n  disable_samples_on_pii_tags: "{{ target.name }}"\n',
            pii_tags.SWITCH_UNREAD,
        ),
    ],
)
def test_the_switch_is_read_as_the_package_reads_it(project, expected):
    assert pii_tags.switch_status(project) == expected


@pytest.mark.parametrize(
    ("release", "rule"),
    [
        ("0.15.2", pii_tags.RULE_NONE),
        ("0.16.4", pii_tags.RULE_NONE),
        ("0.19.1", pii_tags.RULE_NONE),
        ("0.19.2", pii_tags.RULE_MODEL_ONLY),
        ("0.19.4", pii_tags.RULE_MODEL_ONLY),
        ("0.20.0", pii_tags.RULE_FULL),
        ("0.23.0", pii_tags.RULE_FULL),
        ("0.26.1", pii_tags.RULE_FULL),
        (None, pii_tags.RULE_UNKNOWN),
    ],
)
def test_what_each_release_does_with_the_tags(release, rule):
    assert pii_tags.release_rule(release) == rule


# --- the plan -------------------------------------------------------------------

#: The rows the cache holds for ``stg_people``: no value, only the flags, one of
#: them at a confidence below the blocking threshold.
PEOPLE = "select 1 as person_id, 'x' as email, 'x' as notes, 'x' as segment\n"
MART = "select person_id, email, notes, segment from {{ ref('stg_people') }}\n"


def _install(project: Path, release: str | None = "0.26.1") -> None:
    """The stand-in package; ``None`` installs one whose release cannot be read."""

    package = project / "dbt_packages" / "elementary"
    (package / "macros").mkdir(parents=True)
    version = "" if release is None else f'version: "{release}"\n'
    (package / "dbt_project.yml").write_text(
        f"name: elementary\n{version}config-version: 2\n", encoding="utf-8"
    )
    (package / "macros" / "noop.sql").write_text(
        "{% macro noop() %}{% endmacro %}\n", encoding="utf-8"
    )
    (project / "packages.yml").write_text(
        "packages:\n  - package: elementary-data/elementary\n"
        f"    version: {release or '0.26.1'}\n",
        encoding="utf-8",
    )


def _repo(tmp_path: Path, vendor: str | None = "elementary") -> None:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    text = (
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        "dbt_project_dir: analytics\ndbt_prod_target: prod\n"
    )
    if vendor is not None:
        text += f"monitors:\n  vendor: {vendor}\n"
    (tmp_path / ".dex" / "config.yml").write_text(text, encoding="utf-8")


@pytest.fixture
def people(dbt_project_dir: Path, tmp_path: Path) -> Path:
    """``stg_people`` built and profiled: ``email`` flagged high, ``notes`` low."""

    (dbt_project_dir / "models" / "staging" / "stg_people.sql").write_text(
        PEOPLE, encoding="utf-8"
    )

    def flag(category: str, confidence: float) -> PIIFlag:
        return PIIFlag(category=PIICategory(category), confidence=confidence)

    FilesystemStore(tmp_path).save_cache(
        DexCache(
            connector="duckdb",
            datasets=[
                Dataset(
                    identifier="dev.main.stg_people",
                    columns=[
                        ColumnProfile(name="person_id", data_type="INTEGER"),
                        ColumnProfile(
                            name="email", data_type="VARCHAR", pii=flag("email", 0.9)
                        ),
                        ColumnProfile(
                            name="notes",
                            data_type="VARCHAR",
                            pii=flag("free_text", 0.3),
                        ),
                        ColumnProfile(name="segment", data_type="VARCHAR"),
                    ],
                )
            ],
        )
    )
    return dbt_project_dir


def _dex(argv: list[str], capsys) -> dict:
    main(argv)
    return json.loads(capsys.readouterr().out)


def _plan(tmp_path: Path, capsys, body: dict | None = None) -> dict:
    payload = tmp_path / "edits.json"
    body = body or {
        "edits": [
            {
                "path": "models/marts/rpt_people.sql",
                "kind": "model_sql",
                "content": MART,
            }
        ]
    }
    payload.write_text(json.dumps(body), encoding="utf-8")
    planned = _dex(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "people mart",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert planned["status"] == "ok", planned["errors"]
    return planned


def _apply(tmp_path: Path, capsys) -> None:
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]


def test_a_plan_tags_every_column_it_stamps_at_any_confidence_and_turns_the_rule_on(
    people: Path, tmp_path: Path, capsys
):
    _install(people)
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    report = planned["data"]["pii_tags"]
    assert (report["tag"], report["tag_source"]) == ("pii", "default")
    assert report["switch"]["status"] == pii_tags.SWITCH_ADDED
    assert report["rule"] == pii_tags.RULE_FULL
    [model] = report["models"]
    assert model["model"] == "rpt_people"
    assert model["model_tag"] == pii_tags.ADDED
    assert model["columns"] == [
        {"column": "email", "status": pii_tags.ADDED},
        {"column": "notes", "status": pii_tags.ADDED},
    ]
    assert any(
        "the project's own included" in w and "rpt_people" in w
        for w in planned["warnings"]
    )

    _apply(tmp_path, capsys)
    entry = _entry(
        (people / "models" / "marts" / "rpt_people.yml").read_text("utf-8"),
        "rpt_people",
    )
    assert entry["config"]["tags"] == ["pii"]
    assert _column(entry, "email")["config"]["meta"]["tags"] == ["pii"]
    assert _column(entry, "notes")["config"]["meta"]["tags"] == ["pii"]
    assert not any(c["name"] == "segment" for c in entry["columns"])
    project = yaml.safe_load((people / "dbt_project.yml").read_text("utf-8"))
    assert project["vars"] == {"disable_samples_on_pii_tags": True}


def test_with_monitors_vendor_unset_nothing_is_tagged(
    people: Path, tmp_path: Path, capsys
):
    _install(people)
    _repo(tmp_path, vendor=None)

    planned = _plan(tmp_path, capsys)

    assert "pii_tags" not in planned["data"]
    assert "disable_samples_on_pii_tags" not in json.dumps(planned["diffs"])
    assert planned["data"]["pii_metadata"]  # the stamps themselves still land


def test_without_the_package_nothing_is_tagged(people: Path, tmp_path: Path, capsys):
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert "pii_tags" not in planned["data"]
    assert "disable_samples_on_pii_tags" not in json.dumps(planned["diffs"])


def test_the_project_s_own_pii_tags_name_the_tag(people: Path, tmp_path: Path, capsys):
    _install(people)
    project_yml = people / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text("utf-8") + "vars:\n  pii_tags: [restricted, pii]\n",
        encoding="utf-8",
    )
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    report = planned["data"]["pii_tags"]
    assert (report["tag"], report["tag_source"]) == ("restricted", "pii_tags")
    _apply(tmp_path, capsys)
    entry = _entry(
        (people / "models" / "marts" / "rpt_people.yml").read_text("utf-8"),
        "rpt_people",
    )
    assert entry["config"]["tags"] == ["restricted"]
    project = yaml.safe_load(project_yml.read_text("utf-8"))
    assert project["vars"] == {
        "pii_tags": ["restricted", "pii"],
        "disable_samples_on_pii_tags": True,
    }


def test_a_switch_the_project_turned_off_is_kept_and_said(
    people: Path, tmp_path: Path, capsys
):
    _install(people)
    project_yml = people / "dbt_project.yml"
    before = (
        project_yml.read_text("utf-8") + "vars:\n  disable_samples_on_pii_tags: false\n"
    )
    project_yml.write_text(before, encoding="utf-8")
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert planned["data"]["pii_tags"]["switch"]["status"] == pii_tags.SWITCH_OFF
    assert any("is kept as written" in w for w in planned["warnings"])
    _apply(tmp_path, capsys)
    assert project_yml.read_text("utf-8") == before


def test_a_release_before_the_rule_is_said_to_do_nothing_with_the_tags(
    people: Path, tmp_path: Path, capsys
):
    _install(people, release="0.16.4")
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert planned["data"]["pii_tags"]["rule"] == pii_tags.RULE_NONE
    assert any(
        "0.16.4 has no PII sampling rule" in w and "0.19.2" in w
        for w in planned["warnings"]
    )


def test_transform_declare_tags_a_model_whose_marks_predate_the_tag(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "stg_people.sql").write_text(
        "select 1 as person_id, 'x' as email, 'x' as segment\n", encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_people\n"
        "    description: One row per person.\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [person_id]\n"
        "    columns:\n"
        "      - name: person_id\n"
        "        data_tests: [unique, not_null]\n"
        "        config:\n"
        "          meta:\n"
        "            dex:\n"
        "              role: key\n"
        "      - name: email\n"
        "        config:\n"
        "          meta:\n"
        "            contains_pii: true\n"
        "            pii_category: email\n"
        "      - name: segment\n",
        encoding="utf-8",
    )

    def declare() -> dict:
        return _dex(
            ["--repo-root", str(tmp_path), "transform", "declare", "stg_people"],
            capsys,
        )

    _repo(tmp_path, vendor=None)
    assert declare()["data"]["plan_id"] is None

    _repo(tmp_path)
    proposed = declare()
    assert proposed["status"] == "ok", proposed["errors"]
    [proposal] = proposed["data"]["proposed"]
    assert proposal["fields"]["pii_tags"]["value"] == {
        "tag": "pii",
        "columns": ["email"],
        "switch": True,
    }
    # The plan underneath reports what it wrote, and declare passes it on.
    report = proposed["data"]["pii_tags"]
    assert report["switch"]["status"] == pii_tags.SWITCH_ADDED
    assert [m["model"] for m in report["models"]] == ["stg_people"]
    _apply(tmp_path, capsys)
    entry = _entry(
        (dbt_project_dir / "models" / "staging" / "schema.yml").read_text("utf-8")
    )
    assert _column(entry, "email")["config"]["meta"]["tags"] == ["pii"]
    assert entry["config"]["tags"] == ["pii"]
    assert _column(entry, "segment") == {"name": "segment"}
    assert declare()["data"]["plan_id"] is None


def test_a_sql_rewrite_tags_the_yaml_and_withholds_the_switch(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        SCHEMA, encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "stg_people.sql").write_text(
        PEOPLE, encoding="utf-8"
    )
    _repo(tmp_path)

    planned = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                {
                    "path": "models/staging/stg_people.sql",
                    "kind": "model_sql",
                    "content": PEOPLE.replace("'x' as segment", "'y' as segment"),
                }
            ]
        },
    )

    [model] = planned["data"]["pii_tags"]["models"]
    assert (model["model"], model["path"]) == (
        "stg_people",
        "models/staging/schema.yml",
    )
    assert model["columns"] == [{"column": "email", "status": pii_tags.ADDED}]
    # A dbt_project.yml edit would stop the rewrite's comparison and impact
    # check, so the switch waits for a plan of its own.
    assert planned["data"]["pii_tags"]["switch"]["status"] == pii_tags.SWITCH_WITHHELD
    assert "disable_samples_on_pii_tags" not in json.dumps(planned["diffs"])
    assert any(
        "left out of this plan" in w and "transform declare stg_people" in w
        for w in planned["warnings"]
    )

    _apply(tmp_path, capsys)
    declared = _dex(
        ["--repo-root", str(tmp_path), "transform", "declare", "stg_people"], capsys
    )
    assert declared["status"] == "ok", declared["errors"]
    [proposal] = declared["data"]["proposed"]
    assert proposal["fields"]["pii_tags"]["value"]["switch"] is True
    _apply(tmp_path, capsys)
    project = yaml.safe_load((dbt_project_dir / "dbt_project.yml").read_text("utf-8"))
    assert project["vars"] == {"disable_samples_on_pii_tags": True}


def test_a_sql_rewrite_resending_dbt_project_yml_unchanged_still_withholds_the_switch(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """A payload that resends ``dbt_project.yml`` as it stands changes nothing
    there, so merging the switch into that edit would be what made the plan
    global; the switch is withheld as for any other rewrite."""

    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        SCHEMA, encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "stg_people.sql").write_text(
        PEOPLE, encoding="utf-8"
    )
    _repo(tmp_path)
    project_yml = (dbt_project_dir / "dbt_project.yml").read_text("utf-8")

    planned = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                {
                    "path": "models/staging/stg_people.sql",
                    "kind": "model_sql",
                    "content": PEOPLE.replace("'x' as segment", "'y' as segment"),
                },
                {
                    "path": "dbt_project.yml",
                    "kind": "project_yml",
                    "content": project_yml,
                },
            ]
        },
    )

    assert planned["data"]["pii_tags"]["switch"]["status"] == pii_tags.SWITCH_WITHHELD
    assert "dbt_project.yml" not in planned["data"]["paths"]


@pytest.mark.parametrize(
    ("path", "kind", "anchor", "added", "status"),
    [
        # Already global: the rewrite gets no comparison or impact check either
        # way, so the switch joins the project edit the plan carries.
        (
            "dbt_project.yml",
            "project_yml",
            'model-paths: ["models"]\n',
            'vars:\n  orders_since: "2026-01-01"\n',
            pii_tags.SWITCH_ADDED,
        ),
        # Global to prerequisites and attribution, not to the comparison or the
        # impact check, so the switch still waits for a plan of its own.
        (
            "profiles.yml",
            "profiles_yml",
            "      type: duckdb\n",
            "      threads: 2\n",
            pii_tags.SWITCH_WITHHELD,
        ),
    ],
    ids=["project_yml", "profiles_yml"],
)
def test_a_sql_rewrite_beside_a_changed_file_adds_the_switch_only_on_the_core(
    dbt_project_dir: Path,
    tmp_path: Path,
    capsys,
    path: str,
    kind: str,
    anchor: str,
    added: str,
    status: str,
):
    _install(dbt_project_dir)
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        SCHEMA, encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "stg_people.sql").write_text(
        PEOPLE, encoding="utf-8"
    )
    _repo(tmp_path)
    before = (dbt_project_dir / path).read_text("utf-8")
    assert before.count(anchor) >= 1
    changed = before.replace(anchor, anchor + added, 1)

    planned = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                {
                    "path": "models/staging/stg_people.sql",
                    "kind": "model_sql",
                    "content": PEOPLE.replace("'x' as segment", "'y' as segment"),
                },
                {"path": path, "kind": kind, "content": changed},
            ]
        },
    )

    assert planned["data"]["pii_tags"]["switch"]["status"] == status
    assert path in planned["data"]["paths"]
    switched = "disable_samples_on_pii_tags" in json.dumps(planned["diffs"])
    assert switched is (status == pii_tags.SWITCH_ADDED)


def test_a_single_string_pii_tags_is_written_and_warned(
    people: Path, tmp_path: Path, capsys
):
    _install(people)
    project_yml = people / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text("utf-8") + "vars:\n  pii_tags: restricted\n",
        encoding="utf-8",
    )
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert planned["data"]["pii_tags"]["tag"] == "restricted"
    assert any(
        "as one string" in w and "[restricted]" in w for w in planned["warnings"]
    )


def test_an_unreadable_release_is_said_to_be_unchecked(
    people: Path, tmp_path: Path, capsys
):
    _install(people, release=None)
    _repo(tmp_path)

    planned = _plan(tmp_path, capsys)

    assert planned["data"]["pii_tags"]["rule"] == pii_tags.RULE_UNKNOWN
    assert any(
        "could not read the installed Elementary release" in w
        for w in planned["warnings"]
    )


def test_a_version_label_tags_the_model_s_one_entry():
    text = (
        "version: 2\nmodels:\n  - name: dim_people\n    latest_version: 1\n"
        "    versions:\n      - v: 1\n        columns:\n          - name: email\n"
        "            config:\n              meta:\n                contains_pii: true\n"
    )

    out, row = pii_tags.tag_entry(text, "dim_people.v1", "pii")

    assert row is not None and row["model_tag"] == pii_tags.ADDED
    assert _entry(out, "dim_people")["config"]["tags"] == ["pii"]


def test_a_model_tag_held_under_meta_counts_as_already_tagged():
    text = SCHEMA.replace(
        "    description: One row per person.\n",
        "    description: One row per person.\n    config:\n      meta:\n"
        "        tags: [pii]\n",
    )

    _, row = pii_tags.tag_entry(text, "stg_people", "pii")

    assert row["model_tag"] == pii_tags.ALREADY_TAGGED


def test_a_plan_that_writes_nothing_personal_carries_no_report(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _install(dbt_project_dir)
    _repo(tmp_path)

    planned = _plan(
        tmp_path,
        capsys,
        {
            "edits": [
                {
                    "path": "models/marts/rpt_counts.sql",
                    "kind": "model_sql",
                    "content": "select 1 as n\n",
                }
            ]
        },
    )

    assert "pii_tags" not in planned["data"]
    assert "disable_samples_on_pii_tags" not in json.dumps(planned["diffs"])


def test_dbt_carries_both_tags_where_the_package_reads_them(
    people: Path, tmp_path: Path, capsys
):
    """Elementary reads a test's ``model_tags`` from the tested model node's
    ``tags``, and a column's tag from its ``meta`` or ``config.meta``: dbt's own
    parse of the applied plan puts both there."""

    from .elementary_fixture import dbt

    _install(people)
    _repo(tmp_path)
    _plan(tmp_path, capsys)
    _apply(tmp_path, capsys)

    dbt(
        "parse", "--project-dir", str(people), "--profiles-dir", str(people), cwd=people
    )
    manifest = json.loads((people / "target" / "manifest.json").read_text("utf-8"))
    [node] = [
        n for n in manifest["nodes"].values() if n["unique_id"].endswith(".rpt_people")
    ]
    assert node["tags"] == ["pii"]
    email = node["columns"]["email"]
    read = (email.get("meta") or {}).get("tags") or (
        (email.get("config") or {}).get("meta") or {}
    ).get("tags")
    assert read == ["pii"]
    assert "segment" not in node["columns"]


def test_tagging_twice_adds_nothing():
    once, _ = pii_tags.tag_entry(SCHEMA, "stg_people", "pii")

    twice, row = pii_tags.tag_entry(once, "stg_people", "pii")

    assert twice == once
    assert row == {
        "model_tag": pii_tags.ALREADY_TAGGED,
        "columns": [{"column": "email", "status": pii_tags.ALREADY_TAGGED}],
    }


def test_planning_again_after_apply_adds_nothing(people: Path, tmp_path: Path, capsys):
    _install(people)
    _repo(tmp_path)
    _plan(tmp_path, capsys)
    _apply(tmp_path, capsys)
    written = {
        path: (people / path).read_text("utf-8")
        for path in ("dbt_project.yml", "models/marts/rpt_people.yml")
    }

    again = _plan(tmp_path, capsys)

    assert again["data"]["plan_id"] is None
    assert not again["diffs"]
    for path, text in written.items():
        assert (people / path).read_text("utf-8") == text
