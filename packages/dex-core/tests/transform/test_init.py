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


@pytest.fixture(autouse=True)
def no_warehouse(request, monkeypatch):
    """No connection reachable by default: init's content preflight must degrade,
    not wander onto a real account through ambient credentials. Tests that
    exercise the preflight replace this with a fake adapter; the composed-flow
    tests that genuinely open local duckdb opt out via the live_open_adapter
    marker."""

    if "live_open_adapter" in request.keywords:
        return

    def unreachable(**_kwargs):
        raise RuntimeError("no connection discovered")

    _patch_open_adapter(monkeypatch, unreachable)


def _patch_open_adapter(monkeypatch, replacement):
    # Both the source attribute and command_args' import-time binding: patching
    # only the connect module would leak the replacement into command_args if
    # its first import happens inside the patched window. Import order matters
    # for the same reason: command_args must bind the real function before
    # connect is patched, or the "original" monkeypatch restores is the fake.
    import exmergo_dex_core.command_args as command_args_mod
    import exmergo_dex_core.connect as connect_mod

    monkeypatch.setattr(connect_mod, "open_adapter", replacement)
    monkeypatch.setattr(command_args_mod, "open_adapter", replacement)


def _fake_open(monkeypatch, adapter):
    _patch_open_adapter(monkeypatch, lambda **_kwargs: adapter)


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


@pytest.mark.live_open_adapter
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


def _seed_databricks_config(repo: Path, **target) -> None:
    (repo / ".dex").mkdir(parents=True, exist_ok=True)
    (repo / ".dex" / "config.yml").write_text(
        yaml.safe_dump({"databricks": target}), encoding="utf-8"
    )


def _patch_databricks_discovery(monkeypatch, method: str, **config_attrs):
    # The renderer resolves the connection at call time from connect.py, so
    # patching there keeps the real rendering logic under test.
    from types import SimpleNamespace

    import exmergo_dex_core.connect as connect_mod

    cfg = SimpleNamespace(
        host="https://test.cloud.databricks.com", client_id=None, **config_attrs
    )
    monkeypatch.setattr(
        connect_mod,
        "resolve_databricks_connection",
        lambda target, env, root: (cfg, method),
    )


def test_init_databricks_bootstraps_a_project(tmp_path: Path, capsys, monkeypatch):
    _seed_databricks_config(
        tmp_path,
        warehouse="abc123",
        dev_catalog="scratch",
        catalogs=["samples.tpch"],
    )
    _patch_databricks_discovery(monkeypatch, "default_profile:oauth_user")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "databricks"), capsys)
    assert rc == 0, envelope
    assert envelope["data"]["connector"] == "databricks"
    profile = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    output = profile["analytics"]["outputs"]["dev"]
    assert output["type"] == "databricks"
    assert output["host"] == "test.cloud.databricks.com"
    assert output["http_path"] == "/sql/1.0/warehouses/abc123"
    assert output["catalog"] == "scratch"
    assert output["schema"] == "dbt_dev"
    assert output["threads"] == 1
    # A user OAuth connection renders dbt's own browser flow, never a token.
    assert output["auth_type"] == "oauth"
    assert "token" not in output


def test_init_databricks_token_auth_renders_an_env_reference(
    tmp_path: Path, capsys, monkeypatch
):
    _seed_databricks_config(tmp_path, warehouse="abc123", dev_catalog="scratch")
    _patch_databricks_discovery(monkeypatch, "environment:token")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "databricks"), capsys)
    assert rc == 0, envelope
    profile = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    output = profile["analytics"]["outputs"]["dev"]
    # The token is a Jinja env reference, never a value.
    assert output["token"] == "{{ env_var('DATABRICKS_TOKEN') }}"  # noqa: S105


def test_init_databricks_refuses_unpinned_warehouse_and_source_collision(
    tmp_path: Path, capsys, monkeypatch
):
    _patch_databricks_discovery(monkeypatch, "default_profile:oauth_user")
    _seed_databricks_config(tmp_path, dev_catalog="scratch")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "databricks"), capsys)
    assert rc == 1
    assert "databricks.warehouse" in envelope["errors"][0]

    _seed_databricks_config(
        tmp_path,
        warehouse="abc123",
        dev_catalog="samples",
        dev_schema="tpch",
        catalogs=["samples.tpch"],
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "databricks"), capsys)
    assert rc == 1
    assert "source" in envelope["errors"][0]


def test_init_snowflake_refuses_workload_identity_with_the_fix(
    tmp_path: Path, capsys, monkeypatch
):
    # dbt-snowflake cannot authenticate via workload identity yet, so a
    # rendered profile would fail every build; init must refuse actionably.
    _patch_snowflake_discovery(
        monkeypatch,
        {
            "account": "A",
            "user": "U",
            "authenticator": "WORKLOAD_IDENTITY",
            "token": "not-a-real-token",
        },
    )
    _seed_snowflake_config(tmp_path, warehouse="DEX_WH", dev_database="SCRATCH")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "snowflake"), capsys)
    assert rc == 1
    message = envelope["errors"][0]
    assert "workload identity" in message
    assert "connection_name" in message
    assert not (tmp_path / "analytics").exists()


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


def _seed_postgres_config(repo: Path, **target) -> None:
    (repo / ".dex").mkdir(parents=True, exist_ok=True)
    (repo / ".dex" / "config.yml").write_text(
        yaml.safe_dump({"postgres": target}), encoding="utf-8"
    )


def test_init_postgres_bootstraps_a_project(tmp_path: Path, capsys, monkeypatch):
    # Postgres discovery is environment-based and pure, so the real chain runs
    # under test: no patching, just a DATABASE_URL.
    pytest.importorskip("psycopg")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://dbt:hunter2-never-rendered@db.example.com:5439/shop",
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "postgres"), capsys)
    assert rc == 0, envelope
    assert envelope["data"]["connector"] == "postgres"
    rendered = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    profile = yaml.safe_load(rendered)
    output = profile["analytics"]["outputs"]["dev"]
    assert output["type"] == "postgres"
    assert output["host"] == "db.example.com"
    assert output["port"] == 5439
    assert output["user"] == "dbt"
    assert output["dbname"] == "shop"
    assert output["schema"] == "dbt_dev"
    assert output["threads"] == 1
    # Never a password value: an env_var reference with an empty default so
    # ~/.pgpass and peer auth still work at dbt runtime.
    assert "hunter2" not in rendered
    assert "env_var" in output["password"] and "PGPASSWORD" in output["password"]
    # The dev schema choice is persisted for cmd_map's replica folding.
    config = yaml.safe_load(
        (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    )
    assert config["postgres"]["dev_schema"] == "dbt_dev"


def test_init_postgres_refuses_dev_schema_source_collision(
    tmp_path: Path, capsys, monkeypatch
):
    pytest.importorskip("psycopg")
    monkeypatch.setenv("DATABASE_URL", "postgresql://dbt:x@db.example.com/shop")
    _seed_postgres_config(tmp_path, schemas=["app", "dbt_dev"], dev_schema="dbt_dev")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "postgres"), capsys)
    assert rc == 1
    assert "source schema" in envelope["errors"][0]
    assert not (tmp_path / "analytics").exists()


def test_init_postgres_refuses_incomplete_connection(
    tmp_path: Path, capsys, monkeypatch
):
    pytest.importorskip("psycopg")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.example.com/")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "postgres"), capsys)
    assert rc == 1
    message = envelope["errors"][0]
    assert "dbt-postgres requires" in message
    assert not (tmp_path / "analytics").exists()


def test_init_postgres_without_a_connection_names_the_fixes(
    tmp_path: Path, capsys, monkeypatch
):
    for var in ("DATABASE_URL", "PGHOST", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(var, raising=False)
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "postgres"), capsys)
    assert rc == 1
    assert "DATABASE_URL" in envelope["errors"][0]


def _seed_redshift_config(repo: Path, **target) -> None:
    (repo / ".dex").mkdir(parents=True, exist_ok=True)
    (repo / ".dex" / "config.yml").write_text(
        yaml.safe_dump({"redshift": target}), encoding="utf-8"
    )


def test_init_redshift_bootstraps_a_password_profile(
    tmp_path: Path, capsys, monkeypatch
):
    # The committed non-secret config target wins discovery when no workgroup
    # is pinned; the password stays an env_var reference at dbt runtime.
    pytest.importorskip("redshift_connector")
    monkeypatch.setenv("REDSHIFT_PASSWORD", "hunter2-never-rendered")
    _seed_redshift_config(
        tmp_path,
        host="wg.example.redshift-serverless.amazonaws.com",
        port=5439,
        dbname="shop",
        user="dbt",
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "redshift"), capsys)
    assert rc == 0, envelope
    assert envelope["data"]["connector"] == "redshift"
    rendered = (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    profile = yaml.safe_load(rendered)
    output = profile["analytics"]["outputs"]["dev"]
    assert output["type"] == "redshift"
    assert output["host"] == "wg.example.redshift-serverless.amazonaws.com"
    assert output["port"] == 5439
    assert output["user"] == "dbt"
    assert output["dbname"] == "shop"
    assert output["schema"] == "dbt_dev"
    assert output["threads"] == 1
    # Never a password value: an env_var reference resolved at dbt runtime.
    assert "hunter2" not in rendered
    assert "env_var" in output["password"] and "REDSHIFT_PASSWORD" in output["password"]
    # The dev schema choice is persisted for cmd_map's replica folding.
    config = yaml.safe_load(
        (tmp_path / ".dex" / "config.yml").read_text(encoding="utf-8")
    )
    assert config["redshift"]["dev_schema"] == "dbt_dev"


def test_init_redshift_refuses_dev_schema_source_collision(
    tmp_path: Path, capsys, monkeypatch
):
    pytest.importorskip("redshift_connector")
    _seed_redshift_config(
        tmp_path,
        host="h.example.com",
        dbname="shop",
        user="dbt",
        schemas=["app", "dbt_dev"],
        dev_schema="dbt_dev",
    )
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "redshift"), capsys)
    assert rc == 1
    assert "source schema" in envelope["errors"][0]
    assert not (tmp_path / "analytics").exists()


def test_init_redshift_without_a_connection_names_the_fixes(
    tmp_path: Path, capsys, monkeypatch
):
    pytest.importorskip("redshift_connector")
    for var in ("REDSHIFT_HOST", "REDSHIFT_DATABASE", "REDSHIFT_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "redshift"), capsys)
    assert rc == 1
    assert "redshift.workgroup" in envelope["errors"][0]


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


# --- layered schemas: per-layer routing is scaffolded, never hand-derived -----------


def test_init_layered_schemas_scaffolds_schema_routing(
    duckdb_file: Path, tmp_path: Path, capsys
):
    rc, envelope = _run(
        _init_argv(
            tmp_path,
            "--connector",
            "duckdb",
            "--path",
            str(duckdb_file),
            "--layered-schemas",
        ),
        capsys,
    )
    assert rc == 0, envelope

    project = tmp_path / "analytics"
    macro = (project / "macros" / "generate_schema_name.sql").read_text(
        encoding="utf-8"
    )
    assert "{{ custom_schema_name | trim }}_{{ target.name }}" in macro
    assert "{{ target.schema }}" in macro
    assert (project / "models" / "intermediate" / ".gitkeep").is_file()

    dbt_project = yaml.safe_load(
        (project / "dbt_project.yml").read_text(encoding="utf-8")
    )
    for layer in ("staging", "intermediate", "marts"):
        assert dbt_project["models"]["analytics"][layer]["+schema"] == layer

    ops = {d["path"]: d["op"] for d in envelope["diffs"]}
    assert ops["analytics/macros/generate_schema_name.sql"] == "create"
    assert ops["analytics/models/intermediate/.gitkeep"] == "create"


def test_default_init_has_no_layer_routing(duckdb_file: Path, tmp_path: Path, capsys):
    rc, envelope = _run(
        _init_argv(tmp_path, "--connector", "duckdb", "--path", str(duckdb_file)),
        capsys,
    )
    assert rc == 0, envelope
    project = tmp_path / "analytics"
    assert not (project / "macros").exists()
    assert not (project / "models" / "intermediate").exists()
    dbt_project = yaml.safe_load(
        (project / "dbt_project.yml").read_text(encoding="utf-8")
    )
    assert "models" not in dbt_project


def test_layered_init_refuses_an_existing_macro_file(
    duckdb_file: Path, tmp_path: Path, capsys
):
    existing = tmp_path / "analytics" / "macros" / "generate_schema_name.sql"
    existing.parent.mkdir(parents=True)
    existing.write_text("{% macro generate_schema_name(c, n) %}{% endmacro %}\n")
    rc, envelope = _run(
        _init_argv(
            tmp_path,
            "--connector",
            "duckdb",
            "--path",
            str(duckdb_file),
            "--layered-schemas",
        ),
        capsys,
    )
    assert rc == 1
    assert "refusing to overwrite" in envelope["errors"][0]
    assert not (tmp_path / "analytics" / "dbt_project.yml").exists()


# --- the content preflight: a populated dev namespace warns at init time ------------


def _bigquery_fake_adapter(tables=None, empty_datasets=None):
    pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.config import BigQueryTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    client = FakeBigQueryClient(
        project="test-proj", tables=tables or [], empty_datasets=empty_datasets or []
    )
    adapter = BigQueryAdapter(
        project="test-proj",
        cost_gate=CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="bigquery",
        ),
        target=BigQueryTarget(),
        client=client,
    )
    return client, adapter


def _bigquery_fake_tables(dataset: str, names: list[str]):
    from fakes.bigquery import FakeTable
    from google.cloud import bigquery

    return [
        FakeTable(
            project="test-proj",
            dataset_id=dataset,
            table_id=name,
            schema=[bigquery.SchemaField("id", "INTEGER")],
        )
        for name in names
    ]


def test_init_warns_when_the_dev_dataset_already_holds_objects(
    tmp_path: Path, capsys, monkeypatch
):
    """The field report behind the check: a leftover tutorial project already
    sitting at the default dev_dataset name, discovered only when a later build
    collided with it. Now init names it, with samples capped and counted."""

    names = ["events", "leads", "orders", "refunds", "sessions", "users", "visits"]
    client, adapter = _bigquery_fake_adapter(
        tables=_bigquery_fake_tables("dbt_dev", names)
    )
    _fake_open(monkeypatch, adapter)
    _seed_bigquery_config(tmp_path, project="test-proj")

    rc, envelope = _run(_init_argv(tmp_path, "--connector", "bigquery"), capsys)
    assert rc == 0, envelope
    assert (tmp_path / "analytics" / "dbt_project.yml").is_file()

    content = [w for w in envelope["warnings"] if "already contains" in w]
    assert len(content) == 1
    assert "test-proj.dbt_dev" in content[0]
    assert "7 objects" in content[0]
    assert "events, leads, orders, refunds, sessions, and 2 more" in content[0]
    assert "users" not in content[0]
    # Free: the probe is metadata-only, so nothing was ever queried.
    assert client.query_calls == []


def test_empty_or_absent_dev_namespaces_do_not_warn(
    tmp_path: Path, capsys, monkeypatch
):
    """An empty leftover dataset is harmless (dbt would build into it anyway),
    and an absent one is the normal first-build state; neither is worth a line."""

    _client, adapter = _bigquery_fake_adapter(empty_datasets=["dbt_dev"])
    _fake_open(monkeypatch, adapter)
    _seed_bigquery_config(tmp_path, project="test-proj")

    rc, envelope = _run(_init_argv(tmp_path, "--connector", "bigquery"), capsys)
    assert rc == 0, envelope
    assert [w for w in envelope["warnings"] if "already contains" in w] == []


def test_layered_init_warns_per_populated_layer_namespace(
    tmp_path: Path, capsys, monkeypatch
):
    """With --layered-schemas the builds land in the derived layer schemas, so
    those are what the preflight checks: one warning per populated namespace,
    silence for the empty ones."""

    pytest.importorskip("snowflake.connector")
    from fakes.snowflake import (
        FakeSnowflakeConnection,
        FakeSnowflakeTable,
        FakeWarehouse,
    )

    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter
    from exmergo_dex_core.config import SnowflakeTarget
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate

    connection = FakeSnowflakeConnection(
        tables=[
            FakeSnowflakeTable(
                database="SCRATCH",
                schema="STAGING_DEV",
                name="STG_CUSTOMERS",
                columns=[("ID", "FIXED", False)],
            ),
            # A view is content too: a leftover reporting view collides with a
            # built model exactly the way a table does.
            FakeSnowflakeTable(
                database="SCRATCH",
                schema="MARTS_DEV",
                name="PLACEHOLDER",
                columns=[("ID", "FIXED", False)],
                kind="view",
            ),
        ],
        warehouses=[FakeWarehouse(name="DEX_WH")],
    )
    adapter = SnowflakeAdapter(
        connection=connection,
        cost_gate=CostGate(
            paradigm=Paradigm.COMPUTE_TIME,
            ceiling=None,
            session_ceiling=None,
            session_spent=0.0,
            confirmed=False,
            connector="snowflake",
        ),
        target=SnowflakeTarget(warehouse="DEX_WH"),
        clock=connection.clock,
    )
    _fake_open(monkeypatch, adapter)
    _seed_snowflake_config(
        tmp_path, warehouse="DEX_WH", dev_database="SCRATCH", databases=["SHOP"]
    )
    _patch_snowflake_discovery(
        monkeypatch,
        {
            "account": "TESTORG-TESTACCT",
            "user": "DEX_DEV",
            "private_key_file": "/keys/k.p8",
        },
    )

    rc, envelope = _run(
        _init_argv(tmp_path, "--connector", "snowflake", "--layered-schemas"), capsys
    )
    assert rc == 0, envelope
    content = [w for w in envelope["warnings"] if "already contains" in w]
    assert len(content) == 2
    staging = next(w for w in content if "SCRATCH.STAGING_DEV" in w)
    assert "1 object (STG_CUSTOMERS)" in staging
    marts = next(w for w in content if "SCRATCH.MARTS_DEV" in w)
    assert "PLACEHOLDER" in marts
    # Free: SHOW only, nothing ever crossed the data door.
    assert connection.data_statements == []


def test_init_without_credentials_degrades_to_a_single_note(tmp_path: Path, capsys):
    """Init is credential-optional and must stay that way: no connection means
    one note, a written project, and rc 0."""

    _seed_bigquery_config(tmp_path, project="test-proj")
    rc, envelope = _run(_init_argv(tmp_path, "--connector", "bigquery"), capsys)
    assert rc == 0, envelope
    assert (tmp_path / "analytics" / "dbt_project.yml").is_file()
    notes = [w for w in envelope["warnings"] if "could not check" in w]
    assert len(notes) == 1
    assert "RuntimeError" in notes[0]
    assert [w for w in envelope["warnings"] if "already contains" in w] == []


def test_duckdb_base_namespace_is_never_content_checked(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """The duckdb dev target is the same file as the source warehouse, so its
    content is every working setup's normal state. The autouse raiser is live
    here: a probe attempt would surface as a could-not-check note, and none
    does, proving no adapter was ever opened."""

    rc, envelope = _run(
        _init_argv(tmp_path, "--connector", "duckdb", "--path", str(duckdb_file)),
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["warnings"] == []


@pytest.mark.live_open_adapter
def test_layered_duckdb_init_checks_only_the_layer_schemas(tmp_path: Path, capsys):
    """Layered schemas inside the duckdb file are genuinely dbt-owned, so a
    populated one warns even though the base namespace never does."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("CREATE SCHEMA staging_dev")
    conn.execute("CREATE TABLE staging_dev.stg_leftover (id INTEGER)")
    conn.close()

    rc, envelope = _run(
        _init_argv(
            tmp_path, "--connector", "duckdb", "--path", str(path), "--layered-schemas"
        ),
        capsys,
    )
    assert rc == 0, envelope
    content = [w for w in envelope["warnings"] if "already contains" in w]
    assert len(content) == 1
    assert "staging_dev" in content[0]
    assert "stg_leftover" in content[0]
    # The base namespace (main, holding customers) stayed unmentioned.
    assert all("customers" not in w for w in envelope["warnings"])


@pytest.mark.live_open_adapter
def test_layered_init_build_lands_in_layer_schemas(
    duckdb_file: Path, tmp_path: Path, capsys
):
    # The functional proof of the composition order: a staging model built on
    # the dev target lands in staging_dev, not in the shared base namespace.
    pytest.importorskip("dbt.cli.main")
    duckdb = pytest.importorskip("duckdb")
    repo = ["--repo-root", str(tmp_path)]

    rc, _ = _run(
        _init_argv(
            tmp_path,
            "--connector",
            "duckdb",
            "--path",
            str(duckdb_file),
            "--layered-schemas",
        ),
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

    rc, envelope = _run(
        [*repo, "transform", "build", "--target", "dev", "--confirm"], capsys
    )
    assert rc == 0, envelope
    assert envelope["data"]["success"] is True

    conn = duckdb.connect(str(duckdb_file), read_only=True)
    rows = conn.execute(
        "SELECT schema_name FROM duckdb_tables() WHERE table_name = 'stg_customers'"
        " UNION ALL "
        "SELECT schema_name FROM duckdb_views() WHERE view_name = 'stg_customers'"
    ).fetchall()
    conn.close()
    assert rows and all(schema == "staging_dev" for (schema,) in rows)


@pytest.mark.live_open_adapter
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
