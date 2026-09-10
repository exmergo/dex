"""Live transform against BigQuery: init a project wired to the scratch dev
dataset, plan and apply a trivial model, then a confirmed dev build via
dbt-bigquery. Writes land only in the scratch dataset (IAM enforces it: the CI
principal holds dataEditor there and nowhere else); its table TTL cleans up
anything teardown misses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from .conftest import (
    MAX_BYTES,
    assert_ok,
    assert_unpivot_build,
    integration_budget,
    unpivot_fixture_edits,
)
from .test_bigquery_connect import run_cli, seed_repo

pytestmark = [pytest.mark.integration, pytest.mark.bigquery]

MODEL_NAME = "dex_probe"


def _seed_transform_repo(root: Path, project: str, dataset: str) -> None:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "bigquery",
                "budget": integration_budget(),
                "bigquery": {"project": project, "dev_dataset": dataset},
            }
        ),
        encoding="utf-8",
    )


def test_init_plan_apply_build_into_the_scratch_dataset(
    tmp_path: Path, capsys, bq_project: str, bq_scratch_dataset: str
):
    pytest.importorskip("dbt.adapters.bigquery")
    root = str(tmp_path)
    _seed_transform_repo(tmp_path, bq_project, bq_scratch_dataset)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert_ok(rc, envelope)
    profiles = yaml.safe_load(
        (tmp_path / "analytics" / "profiles.yml").read_text(encoding="utf-8")
    )
    dev = profiles["analytics"]["outputs"]["dev"]
    assert dev == {
        "type": "bigquery",
        "method": "oauth",
        "project": bq_project,
        "dataset": bq_scratch_dataset,
        "threads": 4,
        "priority": "interactive",
    }

    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": f"models/staging/{MODEL_NAME}.sql",
                        "kind": "model_sql",
                        "content": "select 1 as id\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "probe model",
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned
    rc, applied = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, applied

    rc, built = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert rc == 0, built
    assert built["status"] == "ok"
    # The build is priced upfront now: the confirmed envelope carries the summed
    # dry-run byte estimate (floored to BigQuery's per-query minimum), not a null.
    assert built["cost"]["estimate"] is not None
    assert built["cost"]["estimate"] > 0
    statuses = {n["name"]: n["status"] for n in built["data"]["nodes"]}
    assert statuses.get(MODEL_NAME) == "success"

    # The relation exists exactly where the dev target points (a free metadata
    # GET), then best-effort cleanup; the dataset TTL is the backstop.
    from google.cloud import bigquery

    client = bigquery.Client(project=bq_project)
    try:
        table = client.get_table(f"{bq_project}.{bq_scratch_dataset}.{MODEL_NAME}")
        assert table.table_id == MODEL_NAME
        client.delete_table(table, not_found_ok=True)
    finally:
        client.close()


def test_unpivot_json_object_macro_builds_live(
    tmp_path: Path, capsys, bq_project: str, bq_scratch_dataset: str
):
    """The macro's two BigQuery fixes proven on BigQuery itself: the subscript
    read of a computed key compiles (a dynamic path string would not), and the
    depth-limited json_keys keeps nested field names out of the key rows (the
    accepted_values test fails if either regresses)."""

    pytest.importorskip("dbt.adapters.bigquery")
    root = str(tmp_path)
    _seed_transform_repo(tmp_path, bq_project, bq_scratch_dataset)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert_ok(rc, envelope)
    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "macro", "unpivot_json_object"], capsys
    )
    assert_ok(rc, envelope)
    rc, envelope = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert_ok(rc, envelope)

    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            unpivot_fixture_edits(lambda d: f"json '{d}'", "cast(null as json)")
        ),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "unpivot fixture",
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned
    rc, applied = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, applied

    rc, built = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            str(MAX_BYTES),
        ],
        capsys,
    )
    assert rc == 0, built
    assert_unpivot_build(built)

    from google.cloud import bigquery

    client = bigquery.Client(project=bq_project)
    try:
        for name in ("entity_relations", "stg_entities"):
            client.delete_table(
                f"{bq_project}.{bq_scratch_dataset}.{name}", not_found_ok=True
            )
    finally:
        client.close()


def test_a_missing_dev_dataset_warns_rather_than_refusing(
    tmp_path: Path, capsys, bq_project
):
    """BigQuery is the connector where the missing dev namespace is not fatal:
    dbt-bigquery's create_schema issues CREATE SCHEMA IF NOT EXISTS, which creates
    the dataset. Refusing would block a first build that would have succeeded, so
    the preflight warns and names the permission that build needs."""

    pytest.importorskip("dbt.adapters.bigquery")
    root = str(tmp_path)
    seed_repo(tmp_path, bq_project)
    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["bigquery"]["dev_dataset"] = "dex_absent_dev_dataset"
    config["bigquery"]["location"] = "US"
    config["budget"] = integration_budget(100 * 1024 * 1024)
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert_ok(rc, envelope)

    # No model is authored, so the build creates nothing: the preflight's warning
    # is what is under test, not dbt's dataset creation.
    rc, built = run_cli(
        ["--repo-root", root, "transform", "build", "--target", "dev", "--confirm"],
        capsys,
    )
    assert rc == 0, built
    warnings = [w for w in built["warnings"] if "dev_dataset" in w]
    assert len(warnings) == 1
    assert "dex_absent_dev_dataset does not exist" in warnings[0]
    assert "bigquery.datasets.create" in warnings[0]
    assert "bq mk --dataset --location=US" in warnings[0]


def test_mutation_coverage_prices_the_batch_then_measures_the_tests(
    tmp_path: Path, capsys, bq_project: str, bq_scratch_dataset: str
):
    """Mutation coverage against a metered connector, end to end.

    Two things only a live run can establish. The batch is priced and confirmed
    as one number before any mutant executes, which is the cost contract for the
    most expensive command dex has. And nothing the run does materializes:
    mutants build as ephemeral models, so the dev dataset holds exactly the
    relations it held before, which a fake dbt cannot demonstrate.
    """

    pytest.importorskip("dbt.adapters.bigquery")
    root = str(tmp_path)
    _seed_transform_repo(tmp_path, bq_project, bq_scratch_dataset)

    rc, envelope = run_cli(
        ["--repo-root", root, "transform", "init", "analytics"], capsys
    )
    assert_ok(rc, envelope)

    model = "dex_mutation_probe"
    edits_file = tmp_path / "edits.json"
    edits_file.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": f"models/staging/{model}.sql",
                        "kind": "model_sql",
                        "content": (
                            "select id, amount from unnest([\n"
                            "  struct(1 as id, 10 as amount),\n"
                            "  struct(2 as id, 200 as amount)\n"
                            "]) where amount > 5\n"
                        ),
                    },
                    {
                        "path": "models/staging/schema.yml",
                        "kind": "schema_yml",
                        "content": (
                            "version: 2\n"
                            "models:\n"
                            f"  - name: {model}\n"
                            "    columns:\n"
                            "      - name: id\n"
                            "        data_tests: [not_null, unique]\n"
                        ),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, planned = run_cli(
        [
            "--repo-root",
            root,
            "transform",
            "plan",
            "mutation probe",
            "--edits-file",
            str(edits_file),
        ],
        capsys,
    )
    assert rc == 0, planned
    rc, applied = run_cli(["--repo-root", root, "transform", "apply"], capsys)
    assert rc == 0, applied

    from google.cloud import bigquery

    client = bigquery.Client(project=bq_project)
    try:
        before = {
            t.table_id for t in client.list_tables(f"{bq_project}.{bq_scratch_dataset}")
        }

        # Unconfirmed: one estimate for the whole batch, and nothing has run.
        rc, unconfirmed = run_cli(
            ["--repo-root", root, "transform", "test", "--mutate", model], capsys
        )
        assert unconfirmed["status"] == "needs_confirmation", unconfirmed
        assert unconfirmed["cost"]["estimate"] is not None
        per_mutant = unconfirmed["data"].get("per_table_bytes") or {}
        assert "(baseline)" in per_mutant, per_mutant

        rc, measured = run_cli(
            [
                "--repo-root",
                root,
                "transform",
                "test",
                "--mutate",
                model,
                "--max-mutants",
                "2",
                "--confirm",
                "--budget",
                str(MAX_BYTES),
            ],
            capsys,
        )
        assert rc == 0, measured
        assert measured["status"] == "ok"
        data = measured["data"]
        assert data["counts"]["generated"] == 2
        assert data["runs"] == 3, data["runs"]
        assert data["baseline"]["tests"], data["baseline"]
        assert data["spend"]["bytes_billed"] is not None

        # Ephemeral mutants materialize nothing, so the dataset is unchanged.
        after = {
            t.table_id for t in client.list_tables(f"{bq_project}.{bq_scratch_dataset}")
        }
        assert after == before

        client.delete_table(
            f"{bq_project}.{bq_scratch_dataset}.{model}", not_found_ok=True
        )
    finally:
        client.close()
