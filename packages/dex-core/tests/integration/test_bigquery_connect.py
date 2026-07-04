"""Live `connect test` against BigQuery: ADC discovery, capabilities envelope,
and the sanitizer, end to end. Free: capabilities issues no queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.bigquery]

PUBLIC_DATASETS = [
    "bigquery-public-data.samples",
    "bigquery-public-data.austin_bikeshare",
]


def seed_repo(root: Path, project: str, datasets: list[str] | None = None) -> None:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "bigquery",
                "bigquery": {
                    "project": project,
                    "datasets": datasets if datasets is not None else PUBLIC_DATASETS,
                },
            }
        ),
        encoding="utf-8",
    )


def run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line on stdout"
    return rc, json.loads(out)


def test_connect_test_discovers_adc_and_reports_read_only(
    tmp_path: Path, capsys, bq_project: str
):
    seed_repo(tmp_path, bq_project)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "bigquery"
    assert data["dialect"] == "bigquery"
    assert data["read_only"] is True
    assert data["paradigm"] == "bytes_scanned"
    assert data["project"] == bq_project
    assert data["dataset_count"] == len(PUBLIC_DATASETS)
    assert envelope["cost"]["paradigm"] == "bytes_scanned"
    # The principal's identity never crosses the envelope, only its type.
    assert "@" not in json.dumps(envelope)
