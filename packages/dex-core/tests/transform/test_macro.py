"""`transform macro` end to end: shipped assets, scaffolding as a plan,
containment of the macros/ surface, and the functional guarantee the macro
exists for (a nested object's keys never surface as top-level rows)."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.dbt_project import DbtProjectError, contained_path
from exmergo_dex_core.transform.plans import EditKind, PlanEdit, PlanError
from exmergo_dex_core.transform.plans import plan as make_plan
from exmergo_dex_core.transform.scaffold import MACRO_ASSETS, macro_edit
from exmergo_dex_core.transform.validate import EditValidationError, validate_edit

ADAPTER_PREFIXES = (
    "default",
    "bigquery",
    "snowflake",
    "databricks",
    "postgres",
    "redshift",
    "duckdb",
)


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


# --- the shipped asset -----------------------------------------------------------


def test_asset_ships_in_the_package_with_every_adapter_block():
    # importlib.resources is how the engine loads it at runtime, so this is the
    # packaging regression guard: a wheel that drops the asset fails here.
    asset = (
        resources.files("exmergo_dex_core.transform")
        / "assets"
        / "macros"
        / "unpivot_json_object.sql"
    )
    content = asset.read_text(encoding="utf-8")
    for prefix in ADAPTER_PREFIXES:
        assert f"{prefix}__unpivot_json_object(" in content
    assert content.count("{% macro ") == content.count("{% endmacro %}")


def test_macro_edit_targets_the_macro_dir():
    edit = macro_edit("unpivot_json_object", "macros")
    assert edit.path == "macros/unpivot_json_object.sql"
    assert edit.kind is EditKind.MACRO_SQL
    assert "unpivot_json_object" in edit.new_content


def test_macro_edit_refuses_an_unknown_name():
    from exmergo_dex_core.transform.scaffold import ScaffoldError

    with pytest.raises(ScaffoldError, match="unpivot_json_object"):
        macro_edit("no_such_macro", "macros")


# --- the command surface ---------------------------------------------------------


def test_macro_without_a_name_lists_the_shipped_macros(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "macro"], capsys)
    assert rc == 0
    names = [m["name"] for m in envelope["data"]["macros"]]
    assert names == sorted(MACRO_ASSETS)


def test_macro_scaffolds_a_plan_and_apply_writes_it(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "ok"
    assert envelope["data"]["paths"] == ["macros/unpivot_json_object.sql"]
    assert envelope["diffs"][0]["op"] == "create"
    # Nothing written yet: propose, don't impose.
    assert not (dbt_project_dir / "macros").exists()

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0
    assert envelope["data"]["written"] == ["macros/unpivot_json_object.sql"]
    assert (dbt_project_dir / "macros" / "unpivot_json_object.sql").is_file()


def test_generate_schema_name_scaffolds_into_an_existing_project(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # The layered-init macro is also reachable on its own, for projects that
    # adopt per-layer schemas after the fact.
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "generate_schema_name"],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["paths"] == ["macros/generate_schema_name.sql"]
    unified = envelope["diffs"][0]["unified"]
    assert "{{ custom_schema_name | trim }}_{{ target.name }}" in unified
    assert "{{ target.schema }}" in unified

    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope
    assert (dbt_project_dir / "macros" / "generate_schema_name.sql").is_file()


def test_macro_refuses_an_unknown_name_naming_the_available(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "nope"], capsys
    )
    assert rc == 1
    assert "unpivot_json_object" in envelope["errors"][0]


def test_rescaffolding_an_up_to_date_copy_is_a_noop(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    assert rc == 0
    assert envelope["data"]["up_to_date"] is True


def test_rescaffolding_a_customized_copy_warns_and_diffs_back(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    macro_file = dbt_project_dir / "macros" / "unpivot_json_object.sql"
    macro_file.write_text(
        macro_file.read_text(encoding="utf-8").replace("v1", "v1-custom"),
        encoding="utf-8",
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    assert rc == 0
    assert any("differs from the shipped version" in w for w in envelope["warnings"])
    assert envelope["diffs"][0]["op"] == "update"


def test_plan_warns_when_a_model_calls_the_missing_macro(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/relations.sql",
                        "kind": "model_sql",
                        "content": (
                            "select key, value from (\n"
                            "  {{ unpivot_json_object(relation=ref('stg_customers'),"
                            " json_column='doc') }}\n"
                            ")\n"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "unpivot",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert any("transform macro unpivot_json_object" in w for w in envelope["warnings"])


# --- containment and validation --------------------------------------------------


def test_contained_path_admits_macros_and_still_refuses_escapes(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    assert contained_path(root, "macros/x.sql", ["models"], ["macros"])
    with pytest.raises(DbtProjectError):
        contained_path(root, "../macros/x.sql", ["models"], ["macros"])
    with pytest.raises(DbtProjectError):
        contained_path(root, str(tmp_path / "macros" / "x.sql"), ["models"], ["macros"])
    with pytest.raises(DbtProjectError):
        contained_path(root, "scripts/x.sql", ["models"], ["macros"])


def test_custom_macro_paths_are_honored(dbt_project_dir: Path, tmp_path: Path, capsys):
    project_yml = dbt_project_dir / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text(encoding="utf-8") + 'macro-paths: ["my_macros"]\n',
        encoding="utf-8",
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    assert rc == 0
    assert envelope["data"]["paths"] == ["my_macros/unpivot_json_object.sql"]


def test_kind_and_surface_must_agree(dbt_project_dir: Path, tmp_path: Path):
    macro_in_models = PlanEdit(
        path="models/staging/x.sql",
        kind=EditKind.MACRO_SQL,
        new_content="{% macro x() %}select 1{% endmacro %}",
    )
    with pytest.raises(PlanError, match="macro paths"):
        make_plan("bad", [macro_in_models], dbt_project_dir, tmp_path)

    model_in_macros = PlanEdit(
        path="macros/x.sql",
        kind=EditKind.MODEL_SQL,
        new_content="select 1",
    )
    with pytest.raises(PlanError, match="macro_sql"):
        make_plan("bad", [model_in_macros], dbt_project_dir, tmp_path)


def test_macro_validation_refuses_non_macro_content():
    for bad in ("select 1", "{% macro x() %}select 1", ""):
        edit = PlanEdit(path="macros/x.sql", kind=EditKind.MACRO_SQL, new_content=bad)
        with pytest.raises(EditValidationError):
            validate_edit(edit)


def test_freehand_macro_edits_are_accepted(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # The agent may repair or customize a macro through the ordinary edits-file
    # flow; the kind exists for exactly that.
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "macros/greet.sql",
                        "kind": "macro_sql",
                        "content": (
                            "{% macro greet(name) %}\n"
                            "select '{{ name }}' as greeting\n"
                            "{% endmacro %}\n"
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "add greeting macro",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["data"]["paths"] == ["macros/greet.sql"]


# --- the functional guarantee: nested keys never surface as top-level -------------


def test_unpivot_json_object_builds_and_pins_the_nested_key_failure_mode(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    """The end-to-end fixture the issue asked for: a JSON object whose values
    are themselves objects. Without a depth limit the nested field names (role,
    since) would surface as extra top-level rows and orphan a downstream join;
    the accepted_values test and the direct row assertions pin both symptoms
    to zero."""

    duckdb = pytest.importorskip("duckdb")

    _run(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)

    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/staging/stg_entities.sql",
                        "kind": "model_sql",
                        "content": (
                            'select 1 as id, \'{"rel_a": {"role": "admin",'
                            ' "since": 2020}, "rel_b": {"role":'
                            ' "viewer"}}\'::json as attributes\n'
                            "union all select 2,"
                            ' \'{"rel_c": {"role": "editor"}}\'::json\n'
                            "union all select 3, null::json\n"
                        ),
                    },
                    {
                        "path": "models/marts/entity_relations.sql",
                        "kind": "model_sql",
                        "content": (
                            "select id, key as related_id, value as attrs\n"
                            "from (\n"
                            "  {{ unpivot_json_object("
                            "relation=ref('stg_entities'),"
                            " json_column='attributes',"
                            " passthrough=['id']) }}\n"
                            ")\n"
                        ),
                    },
                    {
                        "path": "models/marts/entity_relations.yml",
                        "kind": "schema_yml",
                        "content": (
                            "version: 2\n"
                            "models:\n"
                            "  - name: entity_relations\n"
                            "    columns:\n"
                            "      - name: related_id\n"
                            "        data_tests:\n"
                            "          - not_null\n"
                            "          - accepted_values:\n"
                            "              values: ['rel_a', 'rel_b', 'rel_c']\n"
                        ),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "unpivot entity attributes",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    rc, envelope = _run(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    assert envelope["data"]["success"] is True
    statuses = {n["name"]: n["status"] for n in envelope["data"]["nodes"]}
    assert statuses["entity_relations"] == "success"
    assert all(s in {"success", "pass"} for s in statuses.values())

    con = duckdb.connect(str(tmp_path / "dev.duckdb"), read_only=True)
    try:
        rows = con.execute(
            "select id, related_id from entity_relations order by id, related_id"
        ).fetchall()
        assert rows == [(1, "rel_a"), (1, "rel_b"), (2, "rel_c")]
        roles = con.execute(
            "select attrs->>'role' from entity_relations order by related_id"
        ).fetchall()
        assert [r[0] for r in roles] == ["admin", "viewer", "editor"]
        orphans = con.execute(
            "select count(*) from entity_relations "
            "where related_id not in ('rel_a', 'rel_b', 'rel_c')"
        ).fetchone()[0]
        assert orphans == 0
    finally:
        con.close()
