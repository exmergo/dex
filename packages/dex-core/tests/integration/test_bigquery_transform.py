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

from .conftest import MAX_BYTES
from .test_bigquery_connect import run_cli

pytestmark = [pytest.mark.integration, pytest.mark.bigquery]

MODEL_NAME = "dex_probe"


def _seed_transform_repo(root: Path, project: str, dataset: str) -> None:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "bigquery",
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
    assert rc == 0, envelope
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
