"""The dbt project reader/writer: load, target resolution, hash-checked writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import dbt_project
from exmergo_dex_core.dbt_project import (
    DbtProjectError,
    Edit,
    content_hash,
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
        "dbt_project.yml",
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
