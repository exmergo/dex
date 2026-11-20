"""Live transform against Trino: `transform init` renders a profile dbt-trino can
use, the dev-target preflight refuses a catalog dbt could never write, a real
`dbt build` lands in the dev schema and nowhere else, the build cap reaches the
statements dbt issues, and an incremental model merges on Iceberg.

The target is the seeded pair from scripts/setup_trino_dev.sh. dex reads as
dex_ro through TRINO_URL; dbt writes as dbt_dev through TRINO_USER, which the
rendered profile reads. That is the identity split references/trino.md
documents, and the access-control rules make it real: dex_ro may write nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from .conftest import TRINO_MAX_SECONDS, assert_generated_test_parity, trino_client
from .test_trino_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.trino]

# Small fixed multiple of the per-statement cap: a build issues several
# statements, and the ceiling has to cover them together.
BUILD_BUDGET = TRINO_MAX_SECONDS * 5


@pytest.fixture
def dev_env(trino_url, trino_dev_user, monkeypatch):
    monkeypatch.setenv("TRINO_URL", trino_url)
    monkeypatch.setenv("TRINO_USER", trino_dev_user)
    for name in ("TRINO_HOST", "TRINO_PASSWORD", "TRINO_JWT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _init(tmp_path: Path, capsys) -> None:
    rc, envelope = run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "init",
            "trshop",
            "--connector",
            "trino",
        ],
        capsys,
    )
    assert rc == 0, envelope


def _profile(tmp_path: Path) -> dict:
    rendered = (tmp_path / "trshop" / "profiles.yml").read_text(encoding="utf-8")
    return yaml.safe_load(rendered)["trshop"]["outputs"]["dev"]


def _build(tmp_path: Path, capsys, *extra: str, budget: float = BUILD_BUDGET):
    return run_cli(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            *extra,
            "--confirm",
            "--budget",
            str(budget),
        ],
        capsys,
    )


def _model(tmp_path: Path, name: str, sql: str) -> None:
    models = tmp_path / "trshop" / "models" / "staging"
    models.mkdir(parents=True, exist_ok=True)
    (models / f"{name}.sql").write_text(sql, encoding="utf-8")


def _drop_dev_schema(trino_url: str, catalog: str) -> None:
    cursor = trino_client(trino_url, user="admin").cursor()
    cursor.execute(f"DROP SCHEMA IF EXISTS {catalog}.dbt_dev CASCADE")
    cursor.fetchall()


def test_init_renders_a_profile_dbt_can_use(tmp_path: Path, capsys, dev_env):
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    output = _profile(tmp_path)
    assert output["type"] == "trino"
    assert output["method"] == "none"
    assert (output["database"], output["schema"]) == ("memory", "dbt_dev")
    assert output["user"] == "{{ env_var('TRINO_USER', 'dex_ro') }}"
    for value in output["session_properties"].values():
        assert value.startswith("{{ env_var(") and value.endswith(") }}")


def test_a_read_only_dev_catalog_is_refused_before_dbt_runs(
    tmp_path: Path, capsys, dev_env
):
    seed_repo(tmp_path, catalogs=["memory.app"], dev_catalog="tpch")
    _init(tmp_path, capsys)
    rc, envelope = _build(tmp_path, capsys)
    assert rc == 1
    assert "tpch catalog, which is read-only" in envelope["errors"][0]


def test_the_read_only_identity_cannot_build(
    tmp_path: Path, capsys, dev_env, monkeypatch, trino_url
):
    """Trino exposes no grant table, so dex cannot preflight the dbt identity's
    rights; the cluster's access control refuses the write instead, and the
    build reports it as a failure rather than a success."""

    _drop_dev_schema(trino_url, "memory")
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    monkeypatch.setenv("TRINO_USER", "dex_ro")
    _model(tmp_path, "stg_probe", "select 1 as id\n")
    rc, envelope = _build(tmp_path, capsys)
    assert rc == 1
    assert "Access Denied" in " ".join(envelope["errors"])


def test_a_build_writes_only_the_dev_schema_and_says_it_was_capped(
    tmp_path: Path, capsys, dev_env, trino_url
):
    _drop_dev_schema(trino_url, "memory")
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    _model(
        tmp_path,
        "stg_orders",
        "select id, customer_id, status from memory.app.orders\n",
    )
    rc, envelope = _build(tmp_path, capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["success"] is True
    assert data["spend"]["seconds_billed"] > 0

    warnings = " ".join(envelope["warnings"])
    assert "query_max_run_time" in warnings
    assert "this build ran without a dex-injected server-side cap" not in warnings

    cursor = trino_client(trino_url, user="admin").cursor()
    cursor.execute(
        "SELECT table_name FROM memory.information_schema.tables "
        "WHERE table_schema = 'dbt_dev'"
    )
    assert ["stg_orders"] in cursor.fetchall()
    cursor.execute(
        "SELECT count(*) FROM memory.information_schema.tables "
        "WHERE table_schema = 'app' AND table_name LIKE 'stg_%'"
    )
    assert cursor.fetchall()[0][0] == 0


def test_the_build_cap_reaches_the_statements_dbt_issues(
    tmp_path: Path, capsys, dev_env, trino_url
):
    """The ceiling becomes query_max_run_time on dbt's own CTAS, through the
    profile's session_properties. A model the estimate underprices runs into it
    and is killed there, which is the whole point of a server-side cap."""

    _drop_dev_schema(trino_url, "memory")
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    _model(
        tmp_path,
        "slow_model",
        # A table, because dbt's default view would be created without the
        # query ever running.
        "{{ config(materialized='table') }}\n"
        "select count(*) as n from tpch.tiny.lineitem a\n"
        "cross join tpch.tiny.lineitem b where a.comment < b.comment\n",
    )
    rc, envelope = _build(tmp_path, capsys, budget=4.0)
    assert rc == 1
    assert "EXCEEDED_TIME_LIMIT" in " ".join(envelope["errors"])


def test_an_incremental_model_merges_on_iceberg(
    tmp_path: Path, capsys, dev_env, trino_url
):
    _drop_dev_schema(trino_url, "iceberg")
    seed_repo(
        tmp_path, catalogs=["iceberg.app"], dev_catalog="iceberg", budget=BUILD_BUDGET
    )
    _init(tmp_path, capsys)
    _model(
        tmp_path,
        "orders_incremental",
        "{{ config(materialized='incremental', unique_key='orderkey', "
        "incremental_strategy='merge') }}\n"
        "select orderkey, totalprice from iceberg.app.orders\n"
        "{% if is_incremental() %}\n"
        "where orderkey > (select max(orderkey) - 100 from {{ this }})\n"
        "{% endif %}\n",
    )
    for _ in range(2):
        rc, envelope = _build(tmp_path, capsys)
        assert rc == 0, envelope
    cursor = trino_client(trino_url, user="admin").cursor()
    cursor.execute("SELECT count(*) FROM iceberg.dbt_dev.orders_incremental")
    assert cursor.fetchall()[0][0] == 1_500_000


def test_the_unpivot_macro_builds_live(tmp_path: Path, capsys, dev_env, trino_url):
    """Trino unnests maps rather than JSON, so the shipped macro casts the object
    to a map of its top-level keys. It has to compile and run, not merely
    render."""

    _drop_dev_schema(trino_url, "memory")
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    rc, envelope = run_cli(
        ["--repo-root", str(tmp_path), "transform", "macro", "unpivot_json_object"],
        capsys,
    )
    assert rc == 0, envelope
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert rc == 0, envelope
    _model(
        tmp_path,
        "product_attrs",
        "select k as attr_key, v as attr_value, id\n"
        "from (\n"
        "  {{ unpivot_json_object(\n"
        "       relation='memory.app.products',\n"
        "       json_column='json_parse(base.attrs)',\n"
        "       key_alias='k',\n"
        "       value_alias='v',\n"
        "       passthrough=['id']) }}\n"
        ")\n",
    )
    rc, envelope = _build(tmp_path, capsys, "--select", "product_attrs")
    assert rc == 0, envelope
    cursor = trino_client(trino_url, user="admin").cursor()
    cursor.execute(
        "SELECT count(*), count(DISTINCT attr_key) FROM memory.dbt_dev.product_attrs"
    )
    rows, keys = cursor.fetchall()[0]
    assert rows == 160
    assert keys == 2


def test_generated_tests_agree_with_maintain_verify_live(
    tmp_path: Path, capsys, dev_env, trino_url
):
    """The generated-test parity harness (#1450, #1451) on Trino: each generated
    test and `maintain verify` agree on a violating and a clean fixture."""
    _drop_dev_schema(trino_url, "memory")
    seed_repo(tmp_path, catalogs=["memory.app"], budget=BUILD_BUDGET)
    _init(tmp_path, capsys)
    assert_generated_test_parity(
        run_cli, capsys, tmp_path, connector="trino", budget=BUILD_BUDGET
    )
    _drop_dev_schema(trino_url, "memory")
