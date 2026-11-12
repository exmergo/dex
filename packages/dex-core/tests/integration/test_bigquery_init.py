"""Live onboarding: metadata only, no build or analytics jobs."""

from __future__ import annotations

import pytest
import yaml

from .test_bigquery_connect import run_cli

pytestmark = [pytest.mark.integration, pytest.mark.bigquery]


def test_onboarding_verifies_connection_and_preserves_repeat_run(
    tmp_path, capsys, bq_project, bq_scratch_dataset
):
    arguments = [
        "--repo-root",
        str(tmp_path),
        "init",
        "analytics",
        "--connector",
        "bigquery",
        "--project",
        bq_project,
        "--scope",
        f"{bq_project}.{bq_scratch_dataset}",
        "--dev-destination",
        "dbt_init_dev",
    ]
    rc, result = run_cli(arguments, capsys)
    assert rc == 0, result
    assert result["data"]["readiness"]["connection"]["status"] == "passed"
    assert result["data"]["readiness"]["build"]["status"] == "unchecked"
    configuration = tmp_path / ".dex/config.yml"
    before = configuration.read_bytes()
    assert "budget" not in yaml.safe_load(before)
    profile = tmp_path / "analytics/profiles.yml"
    original_profile = profile.read_bytes()
    rc, repeated = run_cli(["--repo-root", str(tmp_path), "init"], capsys)
    assert rc == 0, repeated
    assert repeated["data"]["created"] == []
    assert configuration.read_bytes() == before
    assert profile.read_bytes() == original_profile
    assert not (tmp_path / "analytics/target/manifest.json").exists()
