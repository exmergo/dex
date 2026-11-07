"""The dbt project reader/writer: load, target resolution, hash-checked writes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from conftest import write_manifest, write_semantic_manifest
from maintain.conftest import SEMANTIC_YAML

from exmergo_dex_core import dbt_project
from exmergo_dex_core.dbt_project import (
    DbtProjectError,
    DbtProjectView,
    Edit,
    EditOp,
    SourceFile,
    content_hash,
    definitions,
    find_project,
    jinja_calls,
    jinja_regions,
    load,
    model_file,
    resolve_target,
    unresolvable_source_relations,
    write_edits,
)
from exmergo_dex_core.errors import RequestError


def _sources_view(tmp_path: Path, yml: str) -> DbtProjectView:
    return DbtProjectView(
        root=str(tmp_path),
        project_name="p",
        profile_name="p",
        files={
            "models/sources.yml": SourceFile(
                path="models/sources.yml", content=yml, sha256="x"
            )
        },
    )


def test_a_scalar_sources_value_declares_nothing(tmp_path):
    """``sources: 5`` is a broken declaration; walking it must not crash row
    attribution with a TypeError, and a truthy scalar no longer iterates."""

    view = _sources_view(Path(tmp_path), "version: 2\nsources: 5\n")
    assert dbt_project.declared_source_relations(view) == {}
    assert unresolvable_source_relations(view) == set()


def test_a_scalar_tables_value_declares_nothing(tmp_path):
    view = _sources_view(
        Path(tmp_path),
        "version: 2\nsources:\n  - name: feed\n    tables: 7\n",
    )
    assert dbt_project.declared_source_relations(view) == {}
    assert unresolvable_source_relations(view) == set()


def test_a_templated_schema_is_reported_unresolvable_and_omitted(tmp_path):
    """``schema`` built with jinja: the relation cannot be known, so the pair
    is reported for refusal instead of sitting in the map as template text
    that a bare-name fallback would happily match."""

    view = _sources_view(
        Path(tmp_path),
        "version: 2\nsources:\n"
        "  - name: feed\n    schema: \"{{ var('env') }}_feed\"\n"
        "    tables:\n      - name: events\n        identifier: raw_events\n",
    )
    assert dbt_project.declared_source_relations(view) == {}
    assert unresolvable_source_relations(view) == {("feed", "events")}


def test_a_plain_declaration_still_resolves(tmp_path):
    view = _sources_view(
        Path(tmp_path),
        "version: 2\nsources:\n  - name: feed\n    schema: feed\n"
        "    tables:\n      - name: events\n        identifier: raw_events\n",
    )
    assert dbt_project.declared_source_relations(view) == {
        ("feed", "events"): "feed.raw_events"
    }
    assert unresolvable_source_relations(view) == set()


def test_authored_qualifications_separate_authored_schema_from_dbt_s_default(tmp_path):
    """Only a schema the declaration writes may override a cache match: dbt's
    own default (schema = source name) is a guess, so it maps to '' and the
    bare-identifier resolution stays available for projects that never
    declare a schema."""

    view = _sources_view(
        Path(tmp_path),
        "version: 2\nsources:\n"
        "  - name: feed\n    schema: feed\n"
        "    tables:\n      - name: events\n        identifier: raw_events\n"
        "  - name: other\n    tables:\n      - name: things\n",
    )
    assert dbt_project.authored_source_qualifications(view) == {
        "feed.raw_events": "feed",
        "other.things": "",
    }


def test_load_parses_project_and_files(dbt_project_dir: Path):
    view = load(dbt_project_dir)
    assert view.project_name == "dex_test"
    assert view.profile_name == "dex_test"
    assert view.model_paths == ["models"]
    assert "models/staging/stg_customers.sql" in view.files
    assert "models/staging/schema.yml" in view.files
    sql = view.files["models/staging/stg_customers.sql"]
    assert sql.sha256 == content_hash(sql.content)


def test_load_defaults_every_path_family_the_way_dbt_does(dbt_project_dir: Path):
    # The fixture's dbt_project.yml declares only `model-paths`, which is the
    # common case: a project that never wrote the other keys still gets dbt's
    # own defaults, so a first authored test or analysis lands where dbt looks.
    view = load(dbt_project_dir)
    assert view.macro_paths == ["macros"]
    assert view.snapshot_paths == ["snapshots"]
    assert view.seed_paths == ["seeds"]
    assert view.test_paths == ["tests"]
    assert view.analysis_paths == ["analyses"]
    # And the order they are matched in, with models last as the catch-all.
    assert [name for name, _paths in view.path_families()] == [
        "macro",
        "snapshot",
        "seed",
        "test",
        "analysis",
        "model",
    ]


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


@pytest.mark.skipif(os.name != "posix", reason="mode bits are synthetic on Windows")
def test_write_edits_leaves_profiles_owner_only_and_other_files_alone(
    dbt_project_dir: Path,
):
    """profiles.yml is the file people hand-edit a password into, so dex writes it
    0600 whatever the umask, and tightens an existing one it rewrites. Everything
    else keeps the umask's mode: a model is not a credential, and a 0600 model
    would be a surprise in a shared checkout."""

    profiles = dbt_project_dir / "profiles.yml"
    profiles.chmod(0o644)
    previous = os.umask(0o022)
    try:
        result = write_edits(
            [
                Edit(
                    path="profiles.yml",
                    new_content=profiles.read_text(encoding="utf-8") + "# touched\n",
                    old_content_hash=content_hash(profiles.read_text(encoding="utf-8")),
                ),
                Edit(
                    path="models/marts/fct_orders.sql",
                    new_content="select 2 as id\n",
                    old_content_hash=None,
                ),
            ],
            dbt_project_dir,
        )
    finally:
        os.umask(previous)
    assert sorted(result.written) == ["models/marts/fct_orders.sql", "profiles.yml"]
    assert profiles.stat().st_mode & 0o777 == 0o600
    model = dbt_project_dir / "models/marts/fct_orders.sql"
    assert model.stat().st_mode & 0o777 == 0o644


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


def test_edit_op_content_invariant():
    # An upsert must carry content; a delete must not. The model refuses both
    # mistakes so a malformed edit never reaches the write path.
    with pytest.raises(ValueError):
        Edit(path="models/x.sql", op=EditOp.UPSERT, new_content=None)
    with pytest.raises(ValueError):
        Edit(path="models/x.sql", op=EditOp.DELETE, new_content="select 1\n")
    # The defaults still describe an upsert, so pre-op plans deserialize unchanged.
    assert Edit(path="models/x.sql", new_content="select 1\n").op is EditOp.UPSERT


def test_write_edits_deletes_a_file(dbt_project_dir: Path):
    view = load(dbt_project_dir)
    target = dbt_project_dir / "models/staging/stg_customers.sql"
    edit = Edit(
        path="models/staging/stg_customers.sql",
        op=EditOp.DELETE,
        old_content_hash=view.files["models/staging/stg_customers.sql"].sha256,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == ["models/staging/stg_customers.sql"]
    assert not result.conflicts
    assert result.diffs[0]["op"] == "delete"
    assert not target.exists()


def test_write_edits_delete_of_a_changed_file_is_a_conflict(dbt_project_dir: Path):
    path = dbt_project_dir / "models/staging/stg_customers.sql"
    stale_hash = content_hash(path.read_text(encoding="utf-8"))
    # A human edits the file after the delete was planned.
    path.write_text("select 42 as id -- keep me\n", encoding="utf-8")

    edit = Edit(
        path="models/staging/stg_customers.sql",
        op=EditOp.DELETE,
        old_content_hash=stale_hash,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == []
    assert len(result.conflicts) == 1
    # The divergence is surfaced, and the file is emphatically still there.
    assert path.read_text(encoding="utf-8") == "select 42 as id -- keep me\n"


def test_write_edits_delete_of_a_missing_file_is_a_noop(dbt_project_dir: Path):
    edit = Edit(
        path="models/staging/never_existed.sql",
        op=EditOp.DELETE,
        old_content_hash=None,
    )
    result = write_edits([edit], dbt_project_dir)
    assert result.written == []
    assert not result.conflicts
    assert not result.diffs


def test_write_edits_all_or_nothing_across_a_delete_and_an_upsert(
    dbt_project_dir: Path,
):
    view = load(dbt_project_dir)
    delete = Edit(
        path="models/staging/stg_customers.sql",
        op=EditOp.DELETE,
        old_content_hash=view.files["models/staging/stg_customers.sql"].sha256,
    )
    # A conflicting upsert in the same plan must veto the delete too.
    stale_upsert = Edit(
        path="models/staging/schema.yml",
        new_content="version: 2\n",
        old_content_hash="stale-and-wrong",
    )
    result = write_edits([delete, stale_upsert], dbt_project_dir)
    assert result.written == []
    assert result.conflicts
    # The delete did not happen: nothing was written, nothing was removed.
    assert (dbt_project_dir / "models/staging/stg_customers.sql").exists()


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.sql",
        "models/../../escape.sql",
        # Inside the project, outside every authored family: the surface is the
        # path families plus the root manifests, not the whole repo. `analyses/`
        # used to sit here and no longer does, which is the one behavior change
        # a project with a stray analyses directory sees.
        "docs/scratch.sql",
        "target/compiled.sql",
    ],
)
def test_write_edits_refuses_paths_outside_the_authored_families(
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


def test_definitions_manifest_composite_unique_key(dbt_project_dir: Path):
    """#169: a model-level unique_combination_of_columns test has no
    column_name at all, so it must be read before (never gated behind) the
    column_name check that every other test type shares."""

    write_manifest(
        dbt_project_dir,
        models={"stg_customers": '"dev"."main"."stg_customers"'},
        composite_unique_tests=[("stg_customers", ["id", "email"])],
    )
    defs = definitions(dbt_project_dir)
    (composite,) = defs.declared_composite_keys
    assert composite.model == "stg_customers"
    assert composite.columns == ["id", "email"]
    assert composite.relation == "dev.main.stg_customers"
    assert composite.source == "manifest"
    # No column_name means it must never leak into the single-column list.
    assert defs.declared_keys == []


def test_definitions_yaml_composite_unique_key(dbt_project_dir: Path):
    """The model-level tests:/data_tests: block, sibling to columns:, was
    never read at all before #169 -- a pure blind spot, not a parse failure."""

    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    tests:\n"
        "      - unique_combination_of_columns:\n"
        "          combination_of_columns: [id, email]\n"
        "    columns:\n"
        "      - name: id\n"
        "        tests: [not_null]\n",
        encoding="utf-8",
    )
    defs = definitions(dbt_project_dir)
    assert defs.relationship_source == "yaml"
    (composite,) = defs.declared_composite_keys
    assert composite.model == "stg_customers"
    assert composite.columns == ["id", "email"]
    assert composite.source == "yaml"
    # The column-level not_null test alongside it is unaffected.
    (key,) = defs.declared_keys
    assert (key.model, key.column) == ("stg_customers", "id")


def test_definitions_yaml_namespaced_composite_unique_key(dbt_project_dir: Path):
    """dbt_utils.unique_combination_of_columns (the pre-dbt-core-1.9 macro
    spelling) is recognized the same as the bare built-in test name."""

    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    tests:\n"
        "      - dbt_utils.unique_combination_of_columns:\n"
        "          combination_of_columns: [id, email]\n",
        encoding="utf-8",
    )
    defs = definitions(dbt_project_dir)
    (composite,) = defs.declared_composite_keys
    assert composite.columns == ["id", "email"]


def test_definitions_yaml_declared_grain(dbt_project_dir: Path):
    """#496: a declaration's grain (`config.meta.dex.grain`) is read as its own
    source, beside the tests, so `maintain verify` can rank it first."""

    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id, email]\n",
        encoding="utf-8",
    )
    defs = definitions(dbt_project_dir)
    assert defs.declared_grains == {"stg_customers": ["id", "email"]}
    assert defs.declared_composite_keys == []


def test_definitions_manifest_declared_grain(dbt_project_dir: Path):
    path = write_manifest(
        dbt_project_dir, models={"stg_customers": '"dev"."main"."stg_customers"'}
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    node = manifest["nodes"]["model.dex_test.stg_customers"]
    node["config"]["meta"] = {"dex": {"grain": ["id"]}}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    defs = definitions(dbt_project_dir)
    assert defs.declared_grains == {"stg_customers": ["id"]}


_GRAIN = {"combination_of_columns": ["order_id", "line_number"]}
_JOIN = {"to": "ref('stg_customers')", "field": "id"}


@pytest.mark.parametrize(
    ("grain_test", "join_test"),
    [
        pytest.param(
            {"dbt_utils.unique_combination_of_columns": _GRAIN},
            {"relationships": _JOIN},
            id="inputs-beside-the-name",
        ),
        pytest.param(
            {"dbt_utils.unique_combination_of_columns": {"arguments": _GRAIN}},
            {"relationships": {"arguments": _JOIN}},
            id="inputs-under-arguments",
        ),
        pytest.param(
            {
                "name": "stg_orders_grain",
                "test_name": "dbt_utils.unique_combination_of_columns",
                "arguments": _GRAIN,
            },
            {
                "name": "stg_orders_customer",
                "test_name": "relationships",
                "arguments": _JOIN,
            },
            id="test-name-with-arguments",
        ),
        pytest.param(
            {"test_name": "dbt_utils.unique_combination_of_columns", **_GRAIN},
            {"test_name": "relationships", **_JOIN},
            id="test-name-with-inputs-beside-it",
        ),
    ],
)
def test_definitions_yaml_reads_every_spelling_dbt_reads(
    dbt_project_dir: Path, grain_test: dict, join_test: dict
):
    """#565: dbt has asked for a generic test's inputs under ``arguments:``
    since 1.10.8, and the compiled read always saw them because dbt merges
    that block into the manifest's kwargs. The YAML read now merges it the
    same way, and reads the ``test_name:`` spelling, so every form dbt
    accepts yields the same composite key and the same foreign key."""

    (dbt_project_dir / "models" / "staging" / "stg_orders.sql").write_text(
        "select 1 as order_id, 1 as line_number, 1 as customer_id\n",
        encoding="utf-8",
    )
    schema = {
        "version": 2,
        "models": [
            {
                "name": "stg_orders",
                "data_tests": [grain_test],
                "columns": [{"name": "customer_id", "data_tests": [join_test]}],
            }
        ],
    }
    (dbt_project_dir / "models" / "staging" / "orders_schema.yml").write_text(
        yaml.safe_dump(schema, sort_keys=False), encoding="utf-8"
    )
    defs = definitions(dbt_project_dir)
    assert defs.relationship_source == "yaml"
    (composite,) = defs.declared_composite_keys
    assert (composite.model, composite.columns) == (
        "stg_orders",
        ["order_id", "line_number"],
    )
    (fk,) = defs.foreign_keys
    assert (fk.model, fk.column, fk.to_model, fk.to_column) == (
        "stg_orders",
        "customer_id",
        "stg_customers",
        "id",
    )


def test_definitions_yaml_arguments_win_over_the_keys_beside_them(
    dbt_project_dir: Path,
):
    """dbt merges ``arguments:`` over its siblings, so where a project writes
    an input in both places the one under ``arguments:`` is the one dbt
    tests, and the one dex must report."""

    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    data_tests:\n"
        "      - unique_combination_of_columns:\n"
        "          combination_of_columns: [id, name]\n"
        "          arguments:\n"
        "            combination_of_columns: [id, email]\n",
        encoding="utf-8",
    )
    (composite,) = definitions(dbt_project_dir).declared_composite_keys
    assert composite.columns == ["id", "email"]


def test_definitions_yaml_reads_unique_and_not_null_by_test_name(
    dbt_project_dir: Path,
):
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    columns:\n"
        "      - name: id\n"
        "        data_tests:\n"
        "          - name: stg_customers_id_unique\n"
        "            test_name: unique\n"
        "          - test_name: not_null\n"
        "            config:\n"
        "              severity: warn\n",
        encoding="utf-8",
    )
    (key,) = definitions(dbt_project_dir).declared_keys
    assert (key.model, key.column) == ("stg_customers", "id")
    assert key.unique is True and key.not_null is True


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


def test_definitions_degrades_on_an_unparseable_project_file(tmp_path: Path):
    """A dbt_project.yml that is not valid YAML degrades rather than raising.

    `load` parses it with `yaml.safe_load`, so this case arrives as `yaml.YAMLError`
    rather than `DbtProjectError`, and catching only the latter let it escape the
    promise the docstring makes two lines up from the `try`. The three profile
    readers in this module already pair the two exceptions; this is the same pairing
    on the read path that explore depends on.
    """

    project = tmp_path / "analytics"
    project.mkdir()
    (project / "dbt_project.yml").write_text("name: [unclosed\n", encoding="utf-8")

    defs = definitions(tmp_path, project)

    assert defs.present is False
    assert defs.declared_keys == [] and defs.foreign_keys == []
    assert any("could not be read" in note for note in defs.notes)


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


# --- the jinja scanner -----------------------------------------------------------
#
# The shared reader behind both the row-attribution renderer and the reference
# index. Its whole job is telling a resolved argument from one dex only guessed
# at, so most of these are about the difference.


def test_jinja_reads_a_call_and_its_literal_arguments():
    (call,) = jinja_calls("select * from {{ ref('stg_orders') }}")
    assert (call.callee, call.args, call.line) == ("ref", ("stg_orders",), 1)
    assert call.spans_region is True


def test_jinja_reports_a_non_literal_argument_as_unread_rather_than_guessing():
    calls = jinja_calls("{{ ref(var('which')) }}")
    # Both calls, not one. Reporting only the outer would hide the var; reporting
    # only the inner would invent a ref nobody wrote.
    assert [(c.callee, c.args) for c in calls] == [
        ("ref", (None,)),
        ("var", ("which",)),
    ]
    assert calls[0].spans_region is True
    assert calls[1].spans_region is False


def test_jinja_reads_the_two_argument_ref_and_source_forms():
    assert jinja_calls("{{ ref('pkg', 'stg_orders') }}")[0].args == (
        "pkg",
        "stg_orders",
    )
    assert jinja_calls("{{ source('raw', 'orders') }}")[0].args == ("raw", "orders")


def test_jinja_ignores_commas_and_parens_inside_a_string_argument():
    (call,) = jinja_calls("{{ label('a, b (c)') }}")
    assert call.args == ("a, b (c)",)


def test_jinja_masks_comments_but_keeps_the_line_numbers():
    calls = jinja_calls("{# ref('ghost') #}\nselect {{ ref('real') }}")
    assert [(c.callee, c.args, c.line) for c in calls] == [("ref", ("real",), 2)]


def test_jinja_reads_calls_inside_statement_blocks():
    calls = jinja_calls("{% if var('flag') %}\nselect 1\n{% endif %}")
    assert [(c.callee, c.args, c.region_kind) for c in calls] == [
        ("var", ("flag",), "statement")
    ]


def test_jinja_skips_an_unbalanced_call_without_losing_the_rest():
    # One broken template must not hide every other reference in the file.
    calls = jinja_calls("{{ ref('unclosed' }}\n{{ ref('fine') }}")
    assert [c.args for c in calls] == [("fine",)]


def test_jinja_reads_a_keyword_argument_as_unread():
    # A keyword argument is left out of args.
    (call,) = jinja_calls("{{ config(materialized='view') }}")
    assert call.args == ()


@pytest.mark.parametrize(
    "template,args",
    [
        ("{{ ref('dim_orders', v=1) }}", ("dim_orders",)),
        ("{{ ref('dim_orders', version=1) }}", ("dim_orders",)),
        ("{{ ref('dim_orders', v = 1) }}", ("dim_orders",)),
        ("{{ ref('dim_orders', version = 1) }}", ("dim_orders",)),
        ("{{ ref('pkg', 'dim_orders', version=2) }}", ("pkg", "dim_orders")),
        ("{{ ref(var('x')) }}", (None,)),
    ],
)
def test_jinja_drops_a_keyword_argument_and_keeps_the_positional_name(template, args):
    assert jinja_calls(template)[0].args == args


def test_jinja_regions_returns_a_region_that_calls_nothing():
    regions, _masked = jinja_regions("select {{ some_var }}")
    assert [(r.kind, r.calls) for r in regions] == [("expression", [])]


# --- versioned models ----------------------------------------------------------


def test_a_manifest_node_is_labelled_the_way_dbt_selects_it():
    label = dbt_project.manifest_node_label
    versioned = {"resource_type": "model", "name": "dim_customers", "version": 2}
    assert label("model.shop.dim_customers.v2", versioned) == "dim_customers.v2"
    assert (
        label("model.shop.fct_orders", {"resource_type": "model", "name": "fct_orders"})
        == "fct_orders"
    )
    # Without the node, the id alone settles it.
    assert label("model.shop.dim_customers.v2") == "dim_customers.v2"
    assert label("model.shop.fct_orders") == "fct_orders"
    assert label("test.shop.not_null_orders_id.3249b83c15") == "not_null_orders_id"
    assert label("test.shop.assert_positive") == "assert_positive"


def _versioned_manifest(project: Path, *, relationship_to: str, depends_on: list[str]):
    """Two versions of `dim_customers`, an unversioned `fct_orders` with a
    relationships test to it, and one source, spelled the way dbt writes them."""

    def model(name: str, relation: str, **extra) -> dict:
        return {
            "resource_type": "model",
            "name": name,
            "relation_name": relation,
            "config": {"enabled": True},
            **extra,
        }

    nodes = {
        "model.dex_test.fct_orders": model("fct_orders", '"dev"."main"."fct_orders"'),
        **{
            f"model.dex_test.dim_customers.v{v}": model(
                "dim_customers",
                f'"dev"."main"."dim_customers_v{v}"',
                version=v,
                latest_version=2,
            )
            for v in (1, 2)
        },
        "test.dex_test.unique_dim_customers_v1_id.aaa": {
            "resource_type": "test",
            "name": "unique_dim_customers_v1_id",
            "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}},
            "attached_node": "model.dex_test.dim_customers.v1",
            "depends_on": {"nodes": ["model.dex_test.dim_customers.v1"]},
        },
        "test.dex_test.relationships_fct_orders.bbb": {
            "resource_type": "test",
            "name": "relationships_fct_orders_customer_id",
            "test_metadata": {
                "name": "relationships",
                "kwargs": {
                    "column_name": "customer_id",
                    "to": relationship_to,
                    "field": "id",
                },
            },
            "attached_node": "model.dex_test.fct_orders",
            "depends_on": {"nodes": depends_on},
        },
    }
    sources = {
        "source.dex_test.raw.customers": {
            "resource_type": "source",
            "source_name": "raw",
            "name": "customers",
            "relation_name": '"dev"."raw"."customers"',
        }
    }
    target = project / "target"
    target.mkdir(exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps({"nodes": nodes, "sources": sources}), encoding="utf-8"
    )


def test_each_version_keeps_its_own_relation_and_sources_are_apart(
    dbt_project_dir: Path,
):
    """One `name` across versions once collapsed them onto one entry, the last
    one read, so a missing v1 relation was checked against v2's."""

    _versioned_manifest(
        dbt_project_dir,
        relationship_to="ref('dim_customers')",
        depends_on=["model.dex_test.dim_customers.v2", "model.dex_test.fct_orders"],
    )
    defs = definitions(dbt_project_dir)
    assert defs.model_relations == {
        "fct_orders": "dev.main.fct_orders",
        "dim_customers.v1": "dev.main.dim_customers_v1",
        "dim_customers.v2": "dev.main.dim_customers_v2",
    }
    assert defs.source_relations == {"raw.customers": "dev.raw.customers"}
    (key,) = defs.declared_keys
    assert (key.model, key.relation) == (
        "dim_customers.v1",
        "dev.main.dim_customers_v1",
    )


@pytest.mark.parametrize(
    "to,depends_on,model,relation",
    [
        # A bare ref is the latest version, which dbt has already resolved.
        (
            "ref('dim_customers')",
            ["model.dex_test.dim_customers.v2", "model.dex_test.fct_orders"],
            "dim_customers.v2",
            "dev.main.dim_customers_v2",
        ),
        (
            "ref('dim_customers', v=1)",
            ["model.dex_test.dim_customers.v1", "model.dex_test.fct_orders"],
            "dim_customers.v1",
            "dev.main.dim_customers_v1",
        ),
        # No usable dependency: the written name stands, and resolves nothing.
        ("ref('dim_customers')", [], "dim_customers", None),
    ],
)
def test_a_relationships_target_is_the_node_dbt_resolved(
    dbt_project_dir: Path, to, depends_on, model, relation
):
    _versioned_manifest(dbt_project_dir, relationship_to=to, depends_on=depends_on)
    (fk,) = definitions(dbt_project_dir).foreign_keys
    assert (fk.model, fk.to_model, fk.to_relation) == ("fct_orders", model, relation)


def test_a_semantic_model_s_grain_lands_on_the_version_it_reads(dbt_project_dir: Path):
    """The semantic manifest names the relation by alias, `dim_customers_v2`,
    which is not a model's label; filing the grain there made one relation two
    models to every consumer."""

    _versioned_manifest(
        dbt_project_dir, relationship_to="ref('dim_customers')", depends_on=[]
    )
    write_semantic_manifest(
        dbt_project_dir,
        semantic_models=[
            {
                "name": "customers",
                "model": "ref('dim_customers')",
                "node_relation": {
                    "alias": "dim_customers_v2",
                    "relation_name": '"dev"."main"."dim_customers_v2"',
                },
                "entities": [{"name": "customer", "type": "primary", "expr": "id"}],
            }
        ],
        metrics=[],
    )
    defs = definitions(dbt_project_dir)
    assert "dim_customers_v2" not in defs.model_relations
    assert defs.primary_entities == {"dim_customers.v2": "id"}


# --- which file builds a model ------------------------------------------------


def _layout(root: Path, files: list[str], **paths: list[str]):
    """A project on disk with the given path keys, read back through `load()`, so
    the keys under test are the ones the engine itself builds."""

    lines = ["name: p", "profile: p"]
    lines += [
        f"{key.replace('_', '-')}: {json.dumps(value)}" for key, value in paths.items()
    ]
    (root / "dbt_project.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("select 1 as id\n", encoding="utf-8")
    return load(root)


@pytest.mark.parametrize(
    "model_paths,path",
    [
        (["models"], "models/staging/stg_one.sql"),
        # Two segments in one entry: stringified with the platform separator it
        # was `src\models` on Windows, which no POSIX key starts with.
        (["src/models"], "src/models/staging/stg_one.sql"),
        # dbt reads both as `models`, and `load()` keys never carry the `./` or
        # the trailing slash.
        (["./models"], "models/staging/stg_one.sql"),
        (["models/"], "models/staging/stg_one.sql"),
        (["lib", "models"], "models/stg_one.sql"),
    ],
)
def test_model_file_finds_the_model_however_its_model_path_is_spelled(
    tmp_path: Path, model_paths: list[str], path: str
):
    view = _layout(tmp_path, [path], model_paths=model_paths)
    assert model_file(view, "stg_one") == path


def test_model_file_does_not_take_a_sibling_directory_for_the_model_path(
    tmp_path: Path,
):
    """`models_analysis` begins with the characters `models` and is not under it."""

    view = _layout(
        tmp_path,
        ["models/stg_one.sql", "models_analysis/stg_ghost.sql"],
        model_paths=["models"],
        analysis_paths=["models_analysis"],
    )
    assert "models_analysis/stg_ghost.sql" in view.files
    with pytest.raises(
        RequestError, match="no model named 'stg_ghost' found under models"
    ):
        model_file(view, "stg_ghost")


def test_model_file_takes_the_model_over_an_analysis_of_the_same_name(tmp_path: Path):
    view = _layout(
        tmp_path,
        ["models/stg_one.sql", "models_analysis/stg_one.sql"],
        model_paths=["models"],
        analysis_paths=["models_analysis"],
    )
    assert model_file(view, "stg_one") == "models/stg_one.sql"


def test_model_file_refuses_a_name_two_model_files_share(tmp_path: Path):
    """dbt refuses two models of one name at parse, so naming both is the answer
    rather than picking whichever the scan reached first."""

    view = _layout(
        tmp_path,
        ["lib/stg_one.sql", "models/staging/stg_one.sql"],
        model_paths=["lib", "models"],
    )
    with pytest.raises(RequestError, match="ambiguous") as caught:
        model_file(view, "stg_one")
    assert "lib/stg_one.sql" in str(caught.value)
    assert "models/staging/stg_one.sql" in str(caught.value)


@pytest.mark.parametrize(
    "path", ["snapshots/stg_one.sql", "models/stg_one.yml", "macros/stg_one.sql"]
)
def test_model_file_ignores_a_file_of_that_name_that_is_not_a_model(
    tmp_path: Path, path: str
):
    view = _layout(tmp_path, [path], model_paths=["models"])
    assert path in view.files
    with pytest.raises(RequestError, match="no model named 'stg_one'"):
        model_file(view, "stg_one")


def test_model_file_matches_the_name_with_its_case(tmp_path: Path):
    """`ref()` matches case, so a model is found by the name its file spells."""

    view = _layout(tmp_path, ["models/Stg_One.sql"], model_paths=["models"])
    assert model_file(view, "Stg_One") == "models/Stg_One.sql"
    with pytest.raises(RequestError):
        model_file(view, "stg_one")


def test_write_edits_case_only_rename_keeps_the_model(
    dbt_project_dir: Path, monkeypatch
):
    """On a case-insensitive filesystem, create+delete of the same path spelling
    must not delete the model (#981). Simulated on Linux by casefolding path ops
    under the project root.
    """

    staging = dbt_project_dir / "models" / "staging"
    old_path = staging / "stg_ORDERS.sql"
    content = "select 1 as id\n"
    old_path.write_text(content, encoding="utf-8")

    real_is_file = Path.is_file
    real_read_text = Path.read_text
    real_unlink = Path.unlink
    real_replace = Path.replace
    real_exists = Path.exists

    root = dbt_project_dir.resolve()

    def _ci_resolve(path: Path) -> Path:
        # Use unpatched helpers so the monkeypatch cannot recurse into itself.
        resolved = path if path.is_absolute() else (dbt_project_dir / path)
        try:
            resolved = resolved.resolve()
        except OSError:
            return path
        try:
            resolved.relative_to(root)
        except ValueError:
            return path
        if real_exists(resolved):
            return resolved
        parent = resolved.parent
        if not real_exists(parent):
            return path
        wanted = resolved.name.casefold()
        try:
            children = list(parent.iterdir())
        except OSError:
            return path
        for child in children:
            if child.name.casefold() == wanted and real_exists(child):
                return child
        return path

    def is_file(self):  # type: ignore[no-untyped-def]
        return real_is_file(_ci_resolve(self))

    def exists(self):  # type: ignore[no-untyped-def]
        return real_exists(_ci_resolve(self))

    def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return real_read_text(_ci_resolve(self), *args, **kwargs)

    def unlink(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return real_unlink(_ci_resolve(self), *args, **kwargs)

    def replace(self, target):  # type: ignore[no-untyped-def]
        return real_replace(_ci_resolve(self), target)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "replace", replace)

    result = write_edits(
        [
            Edit(
                path="models/staging/stg_ORDERS.sql",
                new_content=None,
                old_content_hash=content_hash(content),
                op=EditOp.DELETE,
            ),
            Edit(
                path="models/staging/stg_orders.sql",
                new_content=content,
                old_content_hash=None,
            ),
        ],
        dbt_project_dir,
    )

    assert result.conflicts == []
    assert "models/staging/stg_orders.sql" in result.written
    # Exactly one model file remains, with the create's spelling when the FS
    # is case-sensitive under the simulation's final replace.
    remaining = sorted(p.name for p in staging.glob("stg_*rders.sql"))
    assert remaining == ["stg_orders.sql"]
    assert (staging / "stg_orders.sql").read_text(encoding="utf-8") == content
