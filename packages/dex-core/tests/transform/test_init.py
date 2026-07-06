"""`transform init`: engine-owned dbt bootstrap. Strictly additive, and the
connector never falls through to a default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

CONNECTORS = ("duckdb", "snowflake", "bigquery", "databricks", "postgres")


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _init_argv(repo: Path, *extra: str, name: str = "analytics") -> list[str]:
    return ["--repo-root", str(repo), "transform", "init", name, *extra]


def test_init_bootstraps_a_project_end_to_end(
    duckdb_file: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        _init_argv(tmp_path, "--connector", "duckdb", "--path", str(duckdb_file)),
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["project_name"] == "analytics"
    assert envelope["data"]["project_dir"] == "analytics"
    assert envelope["data"]["connector"] == "duckdb"
    assert envelope["data"]["connector_source"] == "flag"

    project = tmp_path / "analytics"
    assert (project / "dbt_project.yml").is_file()
    assert (project / "profiles.yml").is_file()
    assert (project / "models" / "staging").is_dir()
    assert (project / "models" / "marts").is_dir()

    ops = {d["path"]: d["op"] for d in envelope["diffs"]}
    assert ops["analytics/dbt_project.yml"] == "create"
    assert ops["analytics/profiles.yml"] == "create"
    assert ops[".dex/config.yml"] == "create"

    config = yaml.safe_load(
        (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    )
    assert config["connector"] == "duckdb"
    assert config["dbt_project_dir"] == "analytics"
    assert config["dbt_target"] == "dev"
    assert config["duckdb"]["path"] == str(duckdb_file)

    profiles = yaml.safe_load((project / "profiles.yml").read_text(encoding="utf-8"))
    assert profiles["analytics"]["target"] == "dev"
    assert set(profiles["analytics"]["outputs"]) == {"dev"}
    assert profiles["analytics"]["outputs"]["dev"] == {
        "type": "duckdb",
        "path": str(duckdb_file),
    }


def test_init_makes_the_choices_ambient_for_the_composed_flow(
    duckdb_file: Path, tmp_path: Path, capsys
):
    # After init, explore map and transform plan need no flags: the warehouse
    # path and the project dir both come from the config init wrote back.
    rc, _ = _run(
        _init_argv(tmp_path, "--connector", "duckdb", "--path", str(duckdb_file)),
        capsys,
    )
    assert rc == 0

    rc, envelope = _run(["--repo-root", str(tmp_path), "explore", "map"], capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok"

    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "stage customers",
            "--scaffold",
            "customers",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert "models/staging/stg_customers.sql" in envelope["data"]["paths"]


def test_bare_init_without_a_connector_is_refused(tmp_path: Path, capsys):
    rc, envelope = _run(_init_argv(tmp_path), capsys)
    assert rc == 1
    assert envelope["status"] == "error"
    for connector in CONNECTORS:
        assert connector in envelope["errors"][0]
    assert not (tmp_path / "analytics").exists()


def test_config_declared_connector_is_accepted_and_attributed(
    duckdb_file: Path, tmp_path: Path, capsys
):
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {duckdb_file}\n", encoding="utf-8"
    )
    rc, envelope = _run(_init_argv(tmp_path), capsys)
    assert rc == 0, envelope
    assert envelope["data"]["connector"] == "duckdb"
    assert envelope["data"]["connector_source"] == "config"
    ops = {d["path"]: d["op"] for d in envelope["diffs"]}
    assert ops[".dex/config.yml"] == "update"


def test_init_refuses_when_a_project_exists_at_the_repo_root(
    duckdb_file: Path, tmp_path: Path, capsys
):
    (tmp_path / "dbt_project.yml").write_text("name: existing\n", encoding="utf-8")
    rc, envelope = _run(
        _init_argv(tmp_path, "--connector", "duckdb", "--path", str(duckdb_file)),
        capsys,
    )
    assert rc == 1
    assert "already exists" in envelope["errors"][0]


def test_init_refuses_when_a_project_exists_in_a_child_dir(
    dbt_project_dir: Path, duckdb_file: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        _init_argv(
            tmp_path,
            "--connector",
            "duckdb",
            "--path",
            str(duckdb_file),
            name="fresh",
        ),
        capsys,
    )
    assert rc == 1
    assert "already exists" in envelope["errors"][0]
    assert str(dbt_project_dir) in envelope["errors"][0]
    assert not (tmp_path / "fresh").exists()


@pytest.mark.parametrize("connector", ["databricks", "postgres"])
def test_cloud_connectors_error_actionably(tmp_path: Path, capsys, connector: str):
    rc, envelope = _run(_init_argv(tmp_path, "--connector", connector), capsys)
    assert rc == 1
    message = envelope["errors"][0]
    assert connector in message
    assert "not yet supported" in message
    assert "duckdb" in message.lower()


def _seed_snowflake_config(repo: Path, **target) -> None:
    (repo / ".dex").mkdir(parents=True, exist_ok=True)
    (repo / ".dex" / "config.yml").write_text(
        yaml.safe_dump({"snowflake": target}), encoding="utf-8"
    )


def _patch_snowflake_discovery(monkeypatch, params: dict):
    # The renderer resolves the connection at call time from connect.py, so
    # patching there keeps the real rendering logic under test.
    import exmergo_dex_core.connect as connect_mod

    monkeypatch.setattr(
        connect_mod,
        "resolve_snowflake_connection",
        lambda target, env, root: (params, "named_connection:key_pair"),
    )


def test_init_snowflake_bootstraps_a_project(tmp_path: Path, capsys, monkeypatch):
    _seed_snowflake_config(
        tmp_path,
        warehouse="DEX_WH",
        dev_database="SCRATCH",
        databases=["SHOP"],
    )
    _patch_snowflake_discovery(
        monkeypatch,
        {
            "account": "TESTORG-TESTACCT",
            "user": "DEX_DEV",
            "private_key_file": "/keys/k.p8",
            "role": "DEX_ROLE",
        },
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "snowflake"), capsys)
    assert rc == 0, envelope
    assert envelope["data"]["connector"] == "snowflake"
    profile = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    output = profile["analytics"]["outputs"]["dev"]
    assert output["type"] == "snowflake"
    assert output["account"] == "TESTORG-TESTACCT"
    assert output["warehouse"] == "DEX_WH"
    assert output["database"] == "SCRATCH"
    assert output["schema"] == "DBT_DEV"
    assert output["threads"] == 1
    assert output["query_tag"] == "dex"
    # Key-pair auth renders as a path, never a key or password value.
    assert output["private_key_path"] == "/keys/k.p8"
    assert "password" not in output


def test_init_snowflake_refuses_unpinned_warehouse_and_source_collision(
    tmp_path: Path, capsys, monkeypatch
):
    _patch_snowflake_discovery(
        monkeypatch, {"account": "A", "user": "U", "password": "x"}
    )
    _seed_snowflake_config(tmp_path, dev_database="SCRATCH")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "snowflake"), capsys)
    assert rc == 1
    assert "snowflake.warehouse" in envelope["errors"][0]

    _seed_snowflake_config(
        tmp_path,
        warehouse="DEX_WH",
        dev_database="SHOP",
        dev_schema="PUBLIC",
        databases=["SHOP.PUBLIC"],
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "snowflake"), capsys)
    assert rc == 1
    assert "source" in envelope["errors"][0]


def test_init_snowflake_never_persists_a_password(tmp_path: Path, capsys, monkeypatch):
    _patch_snowflake_discovery(
        monkeypatch,
        {"account": "A", "user": "U", "password": "hunter2-never-rendered"},
    )
    _seed_snowflake_config(tmp_path, warehouse="DEX_WH", dev_database="SCRATCH")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "snowflake"), capsys)
    assert rc == 0, envelope
    rendered = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    assert "hunter2" not in rendered
    assert "env_var" in rendered and "SNOWFLAKE_PASSWORD" in rendered


def _seed_bigquery_config(repo: Path, **target) -> None:
    (repo / ".dex").mkdir(parents=True, exist_ok=True)
    (repo / ".dex" / "config.yml").write_text(
        yaml.safe_dump({"bigquery": target}), encoding="utf-8"
    )


def test_init_bigquery_bootstraps_a_project(tmp_path: Path, capsys):
    _seed_bigquery_config(tmp_path, project="test-proj")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "bigquery"), capsys)
    assert rc == 0, envelope
    assert envelope["data"]["connector"] == "bigquery"

    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    profile = profiles["analytics"]
    assert profile["target"] == "dev"
    assert set(profile["outputs"]) == {"dev"}
    dev = profile["outputs"]["dev"]
    assert dev["type"] == "bigquery"
    assert dev["method"] == "oauth"  # ADC: no secret is ever rendered
    assert dev["project"] == "test-proj"
    assert dev["dataset"] == "dbt_dev"

    config = yaml.safe_load(
        (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    )
    assert config["connector"] == "bigquery"
    assert config["bigquery"]["project"] == "test-proj"
    assert config["bigquery"]["dev_dataset"] == "dbt_dev"


def test_init_bigquery_without_a_project_errors_actionably(
    tmp_path: Path, capsys, monkeypatch
):
    from exmergo_dex_core import connect

    # No config project, no env, no ADC: deterministic regardless of the
    # developer machine's gcloud state.
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GCLOUD_PROJECT", raising=False)

    def no_adc():
        raise connect.CredentialDiscoveryError("no ADC")

    monkeypatch.setattr(connect, "_default_credentials", no_adc)
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "bigquery"), capsys)
    assert rc == 1
    message = envelope["errors"][0]
    assert "bigquery.project" in message
    assert "gcloud" in message
    assert not (tmp_path / "analytics").exists()


def test_init_bigquery_refuses_a_dev_dataset_that_is_a_source(tmp_path: Path, capsys):
    _seed_bigquery_config(
        tmp_path, project="test-proj", datasets=["shop"], dev_dataset="shop"
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "bigquery"), capsys)
    assert rc == 1
    assert "dev_dataset" in envelope["errors"][0]
    assert not (tmp_path / "analytics").exists()


def test_unknown_connector_lists_the_valid_ones(tmp_path: Path, capsys):
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "sqlite"), capsys)
    assert rc == 1
    for connector in CONNECTORS:
        assert connector in envelope["errors"][0]


def test_project_name_is_sanitized(duckdb_file: Path, tmp_path: Path, capsys):
    rc, envelope = _run(
        _init_argv(
            tmp_path,
            "--connector",
            "duckdb",
            "--path",
            str(duckdb_file),
            name="My Analytics!",
        ),
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["project_name"] == "my_analytics"
    assert (tmp_path / "my_analytics" / "dbt_project.yml").is_file()


@pytest.mark.parametrize("name", ["", "!!!"])
def test_unusable_project_name_is_refused(tmp_path: Path, capsys, name: str):
    rc, envelope = _run(
        _init_argv(tmp_path, "--connector", "duckdb", name=name), capsys
    )
    assert rc == 1
    assert "name" in envelope["errors"][0]


def test_missing_warehouse_path_errors_actionably(tmp_path: Path, capsys):
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "duckdb"), capsys)
    assert rc == 1
    assert "--path" in envelope["errors"][0]
    assert not (tmp_path / "analytics").exists()


def test_init_scaffold_apply_build_round_trips(
    duckdb_file: Path, tmp_path: Path, capsys
):
    # The composed flow on a bare repo: init, map, scaffold, apply, then a real
    # gated dev build against the generated profiles.yml.
    pytest.importorskip("dbt.cli.main")
    repo = ["--repo-root", str(tmp_path)]

    rc, _ = _run(
        _init_argv(tmp_path, "--connector", "duckdb", "--path", str(duckdb_file)),
        capsys,
    )
    assert rc == 0
    rc, _ = _run([*repo, "explore", "map"], capsys)
    assert rc == 0
    rc, envelope = _run(
        [*repo, "transform", "plan", "stage customers", "--scaffold", "customers"],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = _run(
        [*repo, "transform", "apply", envelope["data"]["plan_id"]], capsys
    )
    assert rc == 0, envelope
    assert envelope["data"]["written"]

    rc, envelope = _run(
        [*repo, "transform", "build", "--target", "dev", "--confirm"], capsys
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["success"] is True
    assert "stg_customers" in {n["name"] for n in envelope["data"]["nodes"]}
