"""The dbt project reader/writer: load, target resolution, hash-checked writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_manifest, write_semantic_manifest
from maintain.conftest import SEMANTIC_YAML

from exmergo_dex_core import dbt_project
from exmergo_dex_core.dbt_project import (
    DbtProjectError,
    Edit,
    content_hash,
    definitions,
    find_project,
    load,
    resolve_target,
    write_edits,
)


def test_load_parses_project_and_files(dbt_project_dir: Path):
    view = load(dbt_project_dir)
    assert view.project_name == "dex_test"
    assert view.profile_name == "dex_test"
    assert view.model_paths == ["models"]
    assert "models/staging/stg_customers.sql" in view.files
    assert "models/staging/schema.yml" in view.files
    sql = view.files["models/staging/stg_customers.sql"]
    assert sql.sha256 == content_hash(sql.content)


def test_load_without_manifest_is_graceful(dbt_project_dir: Path):
    assert load(dbt_project_dir).manifest is None


def test_load_reads_manifest_when_compiled(dbt_project_dir: Path):
    target = dbt_project_dir / "target"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps({"metadata": {"project_name": "dex_test"}, "nodes": {}}),
        encoding="utf-8",
    )
    view = load(dbt_project_dir)
    assert view.manifest is not None
    assert view.manifest["metadata"]["project_name"] == "dex_test"


def test_find_project_at_root_and_in_child(dbt_project_dir: Path, tmp_path: Path):
    assert find_project(dbt_project_dir) == dbt_project_dir
    # tmp_path contains the project as its only child directory with a dbt_project.yml.
    assert find_project(tmp_path) == dbt_project_dir


def test_find_project_missing_is_an_error(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DbtProjectError):
        find_project(empty)


def test_resolve_target_returns_only_name_and_type(dbt_project_dir: Path):
    info = resolve_target(dbt_project_dir, None)
    assert info.name == "dev"
    assert info.type == "duckdb"
    assert info.is_default is True
    # Only the safe fields exist on the model: connection details (paths, hosts,
    # credentials) must never leave dbt_project.py.
    assert set(type(info).model_fields) == {"name", "type", "is_default"}

    prod = resolve_target(dbt_project_dir, "prod")
    assert prod.name == "prod" and prod.is_default is False


def test_resolve_target_unknown_is_an_error(dbt_project_dir: Path):
    with pytest.raises(DbtProjectError):
        resolve_target(dbt_project_dir, "staging")


def test_write_edits_clean_apply(dbt_project_dir: Path):
    view = load(dbt_project_dir)
    old = view.files["models/staging/stg_customers.sql"]
    edit = Edit(
        path="models/staging/stg_customers.sql",
        new_content="select 1 as id\n",
        old_content_hash=old.sha256,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == ["models/staging/stg_customers.sql"]
    assert not result.conflicts
    assert result.diffs and result.diffs[0]["op"] == "update"
    on_disk = (dbt_project_dir / "models/staging/stg_customers.sql").read_text()
    assert on_disk == "select 1 as id\n"


def test_write_edits_create_new_file(dbt_project_dir: Path):
    edit = Edit(
        path="models/marts/fct_orders.sql",
        new_content="select 2 as id\n",
        old_content_hash=None,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == ["models/marts/fct_orders.sql"]
    assert result.diffs[0]["op"] == "create"
    assert (dbt_project_dir / "models/marts/fct_orders.sql").is_file()


def test_write_edits_human_edit_is_a_conflict(dbt_project_dir: Path):
    path = dbt_project_dir / "models/staging/stg_customers.sql"
    stale_hash = content_hash(path.read_text(encoding="utf-8"))
    # A human edits the file after the plan was made.
    path.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    edit = Edit(
        path="models/staging/stg_customers.sql",
        new_content="select 1 as id\n",
        old_content_hash=stale_hash,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == []
    assert len(result.conflicts) == 1
    assert result.conflicts[0].path == "models/staging/stg_customers.sql"
    # Nothing was overwritten; the divergence is surfaced as a diff instead.
    assert path.read_text(encoding="utf-8") == "select 99 as id -- hand-tuned\n"
    assert result.diffs


def test_write_edits_conflict_override_with_confirmed(dbt_project_dir: Path):
    path = dbt_project_dir / "models/staging/stg_customers.sql"
    stale_hash = content_hash(path.read_text(encoding="utf-8"))
    path.write_text("select 99 as id -- hand-tuned\n", encoding="utf-8")

    edit = Edit(
        path="models/staging/stg_customers.sql",
        new_content="select 1 as id\n",
        old_content_hash=stale_hash,
    )
    result = write_edits([edit], dbt_project_dir, confirmed=True)
    assert result.written == ["models/staging/stg_customers.sql"]
    assert result.conflicts  # still reported, just overridden explicitly
    assert path.read_text(encoding="utf-8") == "select 1 as id\n"


def test_write_edits_already_applied_is_a_noop(dbt_project_dir: Path):
    view = load(dbt_project_dir)
    current = view.files["models/staging/stg_customers.sql"]
    edit = Edit(
        path="models/staging/stg_customers.sql",
        new_content=current.content,
        old_content_hash="stale-and-wrong",
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == []
    assert not result.conflicts
    assert not result.diffs


def test_write_edits_all_or_nothing(dbt_project_dir: Path):
    view = load(dbt_project_dir)
    good = Edit(
        path="models/marts/fct_new.sql", new_content="select 1\n", old_content_hash=None
    )
    stale = Edit(
        path="models/staging/stg_customers.sql",
        new_content="select 1 as id\n",
        old_content_hash="stale-and-wrong",
    )
    result = write_edits([good, stale], dbt_project_dir)
    assert result.written == []
    assert result.conflicts
    assert not (dbt_project_dir / "models/marts/fct_new.sql").exists()
    assert view.files  # project untouched


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.sql",
        "models/../../escape.sql",
        "seeds/data.yml",
    ],
)
def test_write_edits_refuses_paths_outside_model_paths(
    dbt_project_dir: Path, bad_path: str
):
    edit = Edit(path=bad_path, new_content="x\n", old_content_hash=None)
    with pytest.raises(DbtProjectError):
        write_edits([edit], dbt_project_dir)


def test_write_edits_refuses_absolute_paths(dbt_project_dir: Path, tmp_path: Path):
    edit = Edit(
        path=str(tmp_path / "abs.sql"), new_content="x\n", old_content_hash=None
    )
    with pytest.raises(DbtProjectError):
        write_edits([edit], dbt_project_dir)


@pytest.mark.parametrize(
    "root_file", ["packages.yml", "dependencies.yml", "dbt_project.yml", "profiles.yml"]
)
def test_write_edits_allows_root_config_files(dbt_project_dir: Path, root_file: str):
    # Containment now admits the project config and profiles alongside the
    # package manifests. Pin the current hash so an existing fixture file is a
    # clean apply, not a conflict, and a not-yet-present one is a create.
    existing = dbt_project_dir / root_file
    old_hash = content_hash(existing.read_text()) if existing.is_file() else None
    edit = Edit(
        path=root_file,
        new_content="name: dex_test\nprofile: dex_test\n# authored\n",
        old_content_hash=old_hash,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == [root_file]
    assert existing.is_file()


@pytest.mark.parametrize("root_file", ["random.yml", "Makefile", "secrets.env"])
def test_write_edits_still_refuses_other_root_files(
    dbt_project_dir: Path, root_file: str
):
    # The carve-out is exactly the known dbt files (package manifests, project
    # config, profiles), not project-root files in general.
    edit = Edit(path=root_file, new_content="x: 1\n", old_content_hash=None)
    with pytest.raises(DbtProjectError):
        write_edits([edit], dbt_project_dir)


def test_profiles_dir_env_override(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    override = tmp_path / "profiles-elsewhere"
    override.mkdir()
    (override / "profiles.yml").write_text(
        (dbt_project_dir / "profiles.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("DBT_PROFILES_DIR", str(override))
    assert dbt_project.profiles_dir(dbt_project_dir) == override


# --- definitions(): the read view of declared and semantic definitions --------


def test_definitions_reads_yaml_tests_without_manifest(dbt_project_dir: Path):
    defs = definitions(dbt_project_dir)
    assert defs.present is True
    assert defs.relationship_source == "yaml"
    assert defs.manifest_loaded is False
    assert defs.foreign_keys == []
    (key,) = defs.declared_keys
    assert (key.model, key.column) == ("stg_customers", "id")
    assert key.not_null is True and key.unique is False
    assert key.source == "yaml"
    # No semantic YAML in this project: the semantic half stays empty.
    assert defs.semantic_source is None
    assert defs.primary_entities == {}


def test_definitions_yaml_relationships_test(dbt_project_dir: Path):
    (dbt_project_dir / "models" / "staging" / "stg_orders.sql").write_text(
        "select 1 as id, 1 as customer_id\n", encoding="utf-8"
    )
    (dbt_project_dir / "models" / "staging" / "orders_schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_orders\n"
        "    columns:\n"
        "      - name: customer_id\n"
        "        tests:\n"
        "          - relationships:\n"
        "              to: ref('stg_customers')\n"
        "              field: id\n",
        encoding="utf-8",
    )
    defs = definitions(dbt_project_dir)
    (fk,) = defs.foreign_keys
    assert (fk.model, fk.column, fk.to_model, fk.to_column) == (
        "stg_orders",
        "customer_id",
        "stg_customers",
        "id",
    )
    assert fk.relation is None and fk.to_relation is None
    assert fk.source == "yaml"
    assert any("name-based" in note for note in defs.notes)


def test_definitions_manifest_foreign_keys_and_key_merge(dbt_project_dir: Path):
    write_manifest(
        dbt_project_dir,
        models={
            "stg_customers": '"dev"."main"."stg_customers"',
            "stg_orders": '"dev"."main"."stg_orders"',
        },
        relationship_tests=[
            ("stg_orders", "customer_id", "ref('stg_customers')", "id")
        ],
        unique_tests=[("stg_customers", "id")],
        not_null_tests=[("stg_customers", "id")],
    )
    defs = definitions(dbt_project_dir)
    assert defs.relationship_source == "manifest"
    assert defs.manifest_loaded is True
    (fk,) = defs.foreign_keys
    assert fk.relation == "dev.main.stg_orders"
    assert fk.to_relation == "dev.main.stg_customers"
    assert fk.source == "manifest"
    # unique + not_null on the same column merge into one declared key. The
    # YAML not_null on stg_customers.id is superseded by the manifest read.
    (key,) = defs.declared_keys
    assert (key.model, key.column) == ("stg_customers", "id")
    assert key.unique is True and key.not_null is True
    assert defs.model_relations["stg_orders"] == "dev.main.stg_orders"


def test_definitions_manifest_source_parent_and_backtick_quoting(
    dbt_project_dir: Path,
):
    write_manifest(
        dbt_project_dir,
        models={"stg_orders": "`proj.main.stg_orders`"},
        sources={"raw.customers": '"dev"."raw"."customers"'},
        relationship_tests=[
            ("stg_orders", "customer_id", "source('raw', 'customers')", "id")
        ],
    )
    defs = definitions(dbt_project_dir)
    (fk,) = defs.foreign_keys
    assert fk.relation == "proj.main.stg_orders"
    assert fk.to_model == "raw.customers"
    assert fk.to_relation == "dev.raw.customers"


def test_definitions_ephemeral_model_has_no_relation(dbt_project_dir: Path):
    write_manifest(
        dbt_project_dir,
        models={"stg_orders": '"dev"."main"."stg_orders"', "int_helper": None},
        relationship_tests=[("stg_orders", "helper_id", "ref('int_helper')", "id")],
    )
    defs = definitions(dbt_project_dir)
    assert "int_helper" not in defs.model_relations
    (fk,) = defs.foreign_keys
    assert fk.to_relation is None


def test_definitions_stub_manifest_falls_back_to_yaml(dbt_project_dir: Path):
    # The 2-key stub other tests use: metadata plus empty nodes must not be
    # mistaken for a compiled project.
    target = dbt_project_dir / "target"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps({"metadata": {"project_name": "dex_test"}, "nodes": {}}),
        encoding="utf-8",
    )
    defs = definitions(dbt_project_dir)
    assert defs.relationship_source == "yaml"
    assert defs.manifest_loaded is False
    assert len(defs.declared_keys) == 1


def test_definitions_flags_stale_manifest(dbt_project_dir: Path):
    write_manifest(
        dbt_project_dir,
        models={"stg_customers": '"dev"."main"."stg_customers"'},
        generated_at="2020-01-01T00:00:00Z",
    )
    defs = definitions(dbt_project_dir)
    assert defs.manifest_stale is True
    assert any("older than the model sources" in note for note in defs.notes)


def test_definitions_fresh_manifest_is_not_stale(dbt_project_dir: Path):
    write_manifest(
        dbt_project_dir,
        models={"stg_customers": '"dev"."main"."stg_customers"'},
        generated_at="2099-01-01T00:00:00Z",
    )
    defs = definitions(dbt_project_dir)
    assert defs.manifest_stale is False


def test_definitions_degrades_without_a_project(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    defs = definitions(empty)
    assert defs.present is False
    assert defs.foreign_keys == [] and defs.declared_keys == []
    assert defs.notes == []


def test_definitions_degrades_on_multiple_projects(tmp_path: Path):
    for name in ("one", "two"):
        child = tmp_path / name
        child.mkdir()
        (child / "dbt_project.yml").write_text(f"name: {name}\n", encoding="utf-8")
    defs = definitions(tmp_path)
    assert defs.present is False
    assert any("dbt_project_dir" in note for note in defs.notes)


def test_definitions_honors_project_dir_pin(dbt_project_dir: Path, tmp_path: Path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "dbt_project.yml").write_text("name: decoy\n", encoding="utf-8")
    defs = definitions(tmp_path, project_dir=dbt_project_dir)
    assert defs.present is True
    assert defs.project_dir == str(dbt_project_dir)
    assert len(defs.declared_keys) == 1


def test_definitions_degrades_on_corrupt_manifest(dbt_project_dir: Path):
    target = dbt_project_dir / "target"
    target.mkdir()
    (target / "manifest.json").write_text("{not json", encoding="utf-8")
    defs = definitions(dbt_project_dir)
    assert defs.present is False
    assert any("could not be read" in note for note in defs.notes)


def test_definitions_semantic_from_yaml(dbt_project_dir: Path):
    (dbt_project_dir / "models" / "staging" / "semantic.yml").write_text(
        SEMANTIC_YAML, encoding="utf-8"
    )
    defs = definitions(dbt_project_dir)
    assert defs.semantic_source == "yaml"
    assert defs.primary_entities == {"stg_orders": "order_id"}
    assert defs.metric_models == ["stg_orders"]


def test_definitions_prefers_semantic_manifest(dbt_project_dir: Path):
    # YAML on disk says stg_orders; the compiled artifact says the model landed
    # as a different alias/relation and must win.
    (dbt_project_dir / "models" / "staging" / "semantic.yml").write_text(
        SEMANTIC_YAML, encoding="utf-8"
    )
    write_semantic_manifest(
        dbt_project_dir,
        semantic_models=[
            {
                "name": "orders",
                "node_relation": {
                    "alias": "orders_mart",
                    "relation_name": '"dev"."main"."orders_mart"',
                },
                "entities": [{"name": "order_id", "type": "primary"}],
                "measures": [{"name": "order_amount"}],
            }
        ],
        metrics=[
            {
                "name": "revenue",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "order_amount"}]},
            }
        ],
    )
    defs = definitions(dbt_project_dir)
    assert defs.semantic_source == "manifest"
    assert defs.primary_entities == {"orders_mart": "order_id"}
    assert defs.metric_models == ["orders_mart"]
    assert defs.model_relations["orders_mart"] == "dev.main.orders_mart"


def test_definitions_yaml_derived_metric_lineage(dbt_project_dir: Path):
    # A derived metric grounds through its input metrics down to measures.
    (dbt_project_dir / "models" / "staging" / "semantic.yml").write_text(
        SEMANTIC_YAML
        + (
            "  - name: revenue_growth\n"
            "    label: Revenue growth\n"
            "    type: derived\n"
            "    type_params:\n"
            "      expr: revenue - revenue\n"
            "      metrics:\n"
            "        - revenue\n"
        ),
        encoding="utf-8",
    )
    defs = definitions(dbt_project_dir)
    assert defs.metric_models == ["stg_orders"]
