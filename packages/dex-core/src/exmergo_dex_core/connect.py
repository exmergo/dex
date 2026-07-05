"""Connection handling and credential discovery.

All connection handling lives here so credentials and raw rows stay inside the
engine process and never reach the agent. DuckDB is a local file path, opened
read-only, no credentials. BigQuery discovers credentials ("discover, don't
ask"): Application Default Credentials only, never a prompted or pasted key,
with the project resolved from explicit config, the environment, the ADC
default, or a dbt profile, in that order. Every discovery failure names the
fix; nothing is ever prompted for.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .adapters import get_adapter
from .cache import DexStore
from .config import BigQueryTarget, DexConfig, load_config
from .envelope import Paradigm
from .guards.cost_guard import CostGate


class CredentialDiscoveryError(Exception):
    """Raised when a cloud connection cannot be discovered. The message always
    names the command or config that fixes it, never a credential value."""


def open_adapter(
    *,
    connector: str | None = None,
    path: str | None = None,
    project: str | None = None,
    datasets: list[str] | None = None,
    repo_root: str | Path = ".",
    budget: float | None = None,
    confirmed: bool = False,
    command: str | None = None,
):
    """Resolve the connection target and return an open, read-only adapter.

    Resolution order: explicit arguments win, then ``.dex/config.yml``. For
    DuckDB the only input is a file path; for BigQuery ``project``/``datasets``
    are convenience overrides of the config target (never written back).
    ``budget``/``confirmed`` feed the cost gate on billed connectors and are
    ignored by free ones; ``command`` labels ledger entries.
    """

    config = load_config(repo_root) or DexConfig()
    connector = connector or config.connector

    if connector == "duckdb":
        resolved = path or (config.duckdb.path if config.duckdb else None)
        if not resolved:
            raise ValueError(
                "no DuckDB path: pass --path or set duckdb.path in .dex/config.yml"
            )
        return get_adapter("duckdb", path=resolved)

    if connector == "bigquery":
        return _open_bigquery(
            config,
            repo_root,
            budget=budget,
            confirmed=confirmed,
            command=command,
            project_override=project,
            dataset_override=datasets,
        )

    # The remaining cloud connectors are not yet implemented.
    return get_adapter(connector)


def _open_bigquery(
    config: DexConfig,
    repo_root: str | Path,
    *,
    budget: float | None,
    confirmed: bool,
    command: str | None,
    project_override: str | None = None,
    dataset_override: list[str] | None = None,
):
    target = config.bigquery or BigQueryTarget()
    if project_override or dataset_override:
        # A command-line override of the committed target, applied in memory
        # only: a smoke test should not silently rewrite .dex/config.yml.
        updates: dict = {}
        if project_override:
            updates["project"] = project_override
        if dataset_override:
            updates["datasets"] = dataset_override
        target = target.model_copy(update=updates)
    credentials, adc_project, principal_type = _default_credentials()
    project = resolve_bigquery_project(
        target, os.environ, adc_project, repo_root=repo_root
    )
    if not project:
        raise CredentialDiscoveryError(
            "no GCP project resolved; set bigquery.project in .dex/config.yml "
            "or run `gcloud config set project <id>` (then refresh ADC with "
            "`gcloud auth application-default login`)"
        )

    store = DexStore(repo_root)
    utc_midnight = (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=budget if budget is not None else config.budget.ceiling,
        session_ceiling=config.budget.session_ceiling,
        session_spent=store.spend_since(utc_midnight),
        confirmed=confirmed,
        connector="bigquery",
        command=command,
        record=store.append_spend_log,
    )
    return get_adapter(
        "bigquery",
        project=project,
        target=target,
        cost_gate=gate,
        credentials=credentials,
        principal_type=principal_type,
    )


def _default_credentials():
    """Discover Application Default Credentials, never prompting.

    Returns ``(credentials, adc_project, principal_type)``. ``principal_type``
    is a coarse class of principal (user / service_account /
    impersonated_service_account / external_account / metadata), safe to
    surface; the principal's identity never is.
    """

    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as exc:
        raise CredentialDiscoveryError(
            "the BigQuery client is not installed; install the connector "
            "extra: exmergo-dex-core[bigquery]"
        ) from exc

    try:
        credentials, adc_project = google.auth.default()
    except DefaultCredentialsError as exc:
        raise CredentialDiscoveryError(
            "no Google Application Default Credentials found; run "
            "`gcloud auth application-default login` (or point "
            "GOOGLE_APPLICATION_CREDENTIALS at a service-account file) and retry"
        ) from exc

    module = type(credentials).__module__
    if "impersonated" in module:
        principal_type = "impersonated_service_account"
    elif "external_account" in module:
        principal_type = "external_account"
    elif "service_account" in module:
        principal_type = "service_account"
    elif "compute_engine" in module:
        principal_type = "metadata"
    else:
        principal_type = "user"
    return credentials, adc_project, principal_type


def resolve_bigquery_project(
    target: BigQueryTarget,
    env: dict | os._Environ,
    adc_project: str | None,
    *,
    repo_root: str | Path = ".",
) -> str | None:
    """The project resolution chain: explicit config, then the environment,
    then the ADC default, then a dbt profile's bigquery target. Pure given its
    inputs (the environment is injected), so the order is directly testable."""

    return (
        target.project
        or env.get("GOOGLE_CLOUD_PROJECT")
        or env.get("GCLOUD_PROJECT")
        or adc_project
        or _project_from_dbt_profiles(repo_root)
    )


def _project_from_dbt_profiles(repo_root: str | Path) -> str | None:
    """Best-effort: the ``project`` of a ``type: bigquery`` output in the
    discovered dbt project's profiles. Any failure means "not found"."""

    try:
        from .dbt_project import PROFILES_FILE, find_project, profiles_dir

        project_dir = find_project(repo_root)
        profiles_path = profiles_dir(project_dir) / PROFILES_FILE
        profiles = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        for output in (profile.get("outputs") or {}).values():
            if (
                isinstance(output, dict)
                and output.get("type") == "bigquery"
                and output.get("project")
            ):
                return str(output["project"])
    return None
