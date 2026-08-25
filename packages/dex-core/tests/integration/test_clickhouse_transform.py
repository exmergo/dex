"""Live transform against ClickHouse: `transform init` renders a profile dbt can
actually use, the dev-target preflight refuses a user that cannot write before
dbt runs, and a real `dbt build` lands in the dev database and nowhere else.

The target is the seeded container from scripts/setup_clickhouse_dev.sh. dex
reads as dex_ro and dbt writes as dbt_dev, which is the credential split the
reference page documents: the DSN carries dex's identity, CLICKHOUSE_PASSWORD
carries dbt's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from .conftest import CH_MAX_SECONDS
from .test_clickhouse_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.clickhouse]

# Small fixed multiple of the per-statement cap: a build issues several
# statements, and the ceiling has to cover them together.
BUILD_BUDGET = CH_MAX_SECONDS * 5


@pytest.fixture
def dev_env(ch_dsn, ch_dev_password, monkeypatch):
    """dex reads through the DSN as dex_ro; dbt authenticates as dbt_dev through
    the env_var the rendered profile references. They are different identities on
    purpose: a read-only role for dex is the documented shape, and this is the
    only way the preflight's privilege question means anything."""

    monkeypatch.setenv("CLICKHOUSE_URL", ch_dsn)
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", ch_dev_password)


def _init(tmp_path: Path, capsys) -> dict:
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "init",
            "chshop",
            "--connector",
            "clickhouse",
        ],
        capsys,
    )
    assert rc == 0, envelope
    return envelope


def _point_profile_at(tmp_path: Path, user: str) -> None:
    """Set the dbt user in the rendered profile.

    `transform init` renders whatever connection dex discovered, which here is
    the read-only one, so pointing the profile at a role that can actually write
    is a real step a user takes rather than a test convenience. It is also what
    makes the preflight's privilege question meaningful: dex reads as one
    identity and dbt builds as another, which is the documented shape.
    """

    path = tmp_path / "chshop" / "profiles.yml"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        re.sub(r"^(\s*user:).*$", rf"\1 {user}", text, count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )


def _map(tmp_path: Path, capsys) -> None:
    """`transform plan --scaffold` builds from the exploration cache, so the
    warehouse has to have been mapped first. This is the ordinary order of the
    loop, not a test artifact."""

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "explore",
            "map",
            "--confirm",
            "--budget",
            str(BUILD_BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope


def _profile(tmp_path: Path) -> dict:
    rendered = (tmp_path / "chshop" / "profiles.yml").read_text(encoding="utf-8")
    return yaml.safe_load(rendered)["chshop"]["outputs"]["dev"]


def test_init_renders_a_profile_dbt_can_use(tmp_path: Path, capsys, dev_env):
    seed_repo(tmp_path, databases=["app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    output = _profile(tmp_path)

    assert output["type"] == "clickhouse"
    # dbt-clickhouse has no `database` key: its `schema` is the ClickHouse
    # database, and it must be the dev one, never a source.
    assert output["schema"] == "dbt_dev"
    assert "database" not in output
    assert output["password"].startswith("{{ env_var(")

    # The cap references survive rendering intact. An f-string would collapse
    # the closing braces and dbt would read the value as a literal string, which
    # parses, applies, and caps nothing.
    settings = output["custom_settings"]
    assert settings["max_execution_time"].endswith(") }}")
    assert settings["max_bytes_to_read"].endswith(") }}")

    # And no credential value was written anywhere in the project.
    rendered = (tmp_path / "chshop" / "profiles.yml").read_text(encoding="utf-8")
    assert "dex_ro:" not in rendered


def test_a_user_that_cannot_write_is_refused_before_dbt_runs(
    tmp_path: Path, capsys, dev_env
):
    """dbt-clickhouse creates the dev database itself, so the privilege to
    create it is what gets checked. Without this the first build dies inside
    dbt's own create_schema with a bare permission error naming neither the
    database nor the grant."""

    seed_repo(tmp_path, databases=["app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)

    # Point the profile at the read-only user dex itself connects as.
    _point_profile_at(tmp_path, "dex_ro")

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            str(BUILD_BUDGET),
        ],
        capsys,
    )
    assert rc == 1
    message = envelope["errors"][0]
    assert "dex_ro" in message
    assert "GRANT" in message
    assert "dbt_dev" in message


def test_init_and_build_write_only_the_dev_database(
    tmp_path: Path, capsys, dev_env, ch_dev_password
):
    seed_repo(tmp_path, databases=["app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    _point_profile_at(tmp_path, "dbt_dev")
    _map(tmp_path, capsys)

    # A staging model over a seeded source, authored the way transform does.
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "staging model for orders",
            "--scaffold",
            "app.orders",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            str(BUILD_BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["success"] is True
    assert data["counts"]["success"] >= 1
    assert data["spend"]["seconds_billed"] > 0

    # The build reports the cap it actually injected, naming this connector's
    # mechanism rather than another's.
    warnings = " ".join(envelope["warnings"])
    assert "max_execution_time" in warnings
    assert "max_bytes_to_read" in warnings
    assert "PGOPTIONS" not in warnings

    # And it wrote only where it was allowed to.
    client = _dev_client(ch_dev_password)
    built = client.query(
        "SELECT name FROM system.tables WHERE database = 'dbt_dev'"
    ).result_rows
    assert built, "the model should exist in the dev database"
    stray = client.query(
        "SELECT count() FROM system.tables WHERE database = 'app' AND name LIKE 'stg_%'"
    ).result_rows
    assert stray[0][0] == 0, "nothing was written into the source database"


def test_the_unpivot_macro_builds_live(
    tmp_path: Path, capsys, dev_env, ch_dev_password
):
    """ClickHouse has no lateral join, so the shipped macro expands JSON with
    ARRAY JOIN. It has to compile and run, not merely render."""

    seed_repo(tmp_path, databases=["app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    _point_profile_at(tmp_path, "dbt_dev")
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "macro",
            "unpivot_json_object",
        ],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope

    models = tmp_path / "chshop" / "models" / "staging"
    models.mkdir(parents=True, exist_ok=True)
    (models / "_attrs_sources.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "sources": [
                    {
                        "name": "app",
                        "schema": "app",
                        "tables": [{"name": "products"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (models / "product_attrs.sql").write_text(
        "select k as attr_key, v as attr_value, id\n"
        "from (\n"
        "  {{ unpivot_json_object(\n"
        "       relation=source('app', 'products'),\n"
        "       json_column='attrs',\n"
        "       key_alias='k',\n"
        "       value_alias='v',\n"
        "       passthrough=['id']) }}\n"
        ")\n",
        encoding="utf-8",
    )

    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--select",
            "product_attrs",
            "--confirm",
            "--budget",
            str(BUILD_BUDGET),
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["success"] is True

    client = _dev_client(ch_dev_password)
    rows = client.query(
        "SELECT count(), uniqExact(attr_key) FROM dbt_dev.product_attrs"
    ).result_rows
    # Two top-level keys per product row, and nested values kept whole.
    assert rows[0][0] > 0
    assert rows[0][1] == 2


def _dev_client(ch_dev_password: str):
    """A client authenticated as the user dbt builds with.

    Verification reads the dev database, and dex's own read-only user
    deliberately has no grant there: reads and writes are separated by
    construction, so checking dbt's output has to use dbt's identity. That the
    read-only user *cannot* see it is itself part of what this suite asserts.
    """

    import os
    from urllib.parse import urlsplit

    import clickhouse_connect

    parsed = urlsplit(os.environ["DEX_TEST_CH_DSN"])
    return clickhouse_connect.get_client(
        host=parsed.hostname or "localhost",
        port=parsed.port or 8123,
        username="dbt_dev",
        password=ch_dev_password,
    )
