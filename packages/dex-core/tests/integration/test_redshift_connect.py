"""Live `connect test` against Redshift: connection discovery (the AWS chain
with a pinned Serverless workgroup, or the REDSHIFT_* environment),
capabilities envelope, and the sanitizer, end to end. Cheap: capabilities is
one catalog round-trip, no table scan (on an idle Serverless workgroup it can
still incur the 60-second wake minimum, which is exactly what the envelope's
warning says). The target is the seeded workgroup from
scripts/setup_redshift_ci.sh, read as the dex_ro user or the IAM identity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.redshift]


def seed_repo(
    root: Path,
    *,
    schemas: list[str] | None = None,
    budget: float | None = None,
) -> None:
    """Commit a redshift config block wired to whichever live target the
    environment provides: the workgroup pin (IAM) when
    DEX_TEST_REDSHIFT_WORKGROUP is set, else nothing beyond the dev schema
    (discovery then reads the REDSHIFT_* environment)."""

    redshift: dict = {"dev_schema": "dbt_dev"}
    if os.environ.get("DEX_TEST_REDSHIFT_WORKGROUP"):
        redshift["workgroup"] = os.environ["DEX_TEST_REDSHIFT_WORKGROUP"]
        if os.environ.get("DEX_TEST_REDSHIFT_DATABASE"):
            redshift["dbname"] = os.environ["DEX_TEST_REDSHIFT_DATABASE"]
    if schemas is not None:
        redshift["schemas"] = schemas
    config: dict = {"connector": "redshift", "redshift": redshift}
    if budget is not None:
        config["budget"] = {"ceiling": budget}
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")


def run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line on stdout"
    return rc, json.loads(out)


def test_connect_test_discovers_connection_and_reports_read_only(
    tmp_path: Path, capsys
):
    seed_repo(tmp_path)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "redshift"
    assert data["dialect"] == "redshift"
    assert data["read_only"] is True
    # Whether Redshift honors the session read-only mode is probed, not
    # assumed; the envelope reports the truth either way.
    assert isinstance(data["session_read_only"], bool)
    assert data["paradigm"] == "compute_time"
    assert data["schema_count"] >= 1
    assert data["compute"]["kind"] in {"serverless", "provisioned", "unknown"}
    assert envelope["cost"]["paradigm"] == "compute_time"
    # The auth method is coarse; no identity, key, or password crosses.
    assert data["auth_method"].split(":")[0] in {
        "iam_serverless",
        "iam_cluster",
        "environment",
        "config_target",
        "dbt_profile",
    }
    payload = json.dumps(envelope)
    assert "AKIA" not in payload  # no access key id
    assert not _identity_keys(data)


def _identity_keys(value, path="data") -> list[str]:
    """Every key in the payload that names an identity or credential."""

    hits: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            if str(key).lower() in {"user", "username", "login", "login_name"}:
                hits.append(f"{path}.{key}")
            hits.extend(_identity_keys(sub, f"{path}.{key}"))
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            hits.extend(_identity_keys(sub, f"{path}[{i}]"))
    return hits
