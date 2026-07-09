"""Live `connect test` against Databricks: connection discovery, capabilities
envelope, and the sanitizer, end to end. Free: capabilities reads Unity
Catalog REST metadata and warehouse facts only; no SQL session is opened, so
the warehouse is never touched, let alone woken."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core.cli import main

pytestmark = [pytest.mark.integration, pytest.mark.databricks]

SAMPLE_SCOPE = "samples.tpch"


def seed_repo(
    root: Path,
    warehouse: str,
    scratch_catalog: str | None,
    *,
    catalogs: list[str] | None = None,
    budget: float | None = None,
) -> None:
    databricks: dict = {
        "warehouse": warehouse,
        "catalogs": catalogs if catalogs is not None else [SAMPLE_SCOPE],
    }
    if scratch_catalog:
        databricks["dev_catalog"] = scratch_catalog
    config: dict = {"connector": "databricks", "databricks": databricks}
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
    tmp_path: Path, capsys, dbx_warehouse, dbx_scratch_catalog
):
    seed_repo(tmp_path, dbx_warehouse, dbx_scratch_catalog)
    rc, envelope = run_cli(["--repo-root", str(tmp_path), "connect", "test"], capsys)
    assert rc == 0, envelope
    data = envelope["data"]
    assert data["connector"] == "databricks"
    assert data["dialect"] == "databricks"
    assert data["read_only"] is True
    assert data["paradigm"] == "compute_time"
    warehouse = data["budget"]["warehouse"]
    assert warehouse["name"]
    assert warehouse["state"]
    assert isinstance(warehouse["serverless"], bool)
    assert envelope["cost"]["paradigm"] == "compute_time"
    # The auth method is coarse; no identity or credential value crosses.
    assert data["auth_method"].split(":")[0] in {
        "named_profile",
        "environment",
        "default_profile",
        "dbt_profile",
    }
    assert not _identity_keys(data)
    payload = json.dumps(envelope)
    secret = os.environ.get("DATABRICKS_TOKEN")
    assert not secret or secret not in payload


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
