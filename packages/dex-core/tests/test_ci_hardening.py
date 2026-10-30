"""Where the integration CI's credentials and containers are allowed to reach.

None of this is engine code, and none of it fails loudly on its own: a credential
that rides in a job's environment instead of one step's still lets every test
pass, and a dogfood container started without its hardening flags still serves
queries. So the properties are held here, hermetically, against the scripts and
the workflow themselves:

- The ClickHouse Cloud API key reaches clickhousectl and nothing else
  preflight.sh starts, and preflight refuses (exit 2, naming the path) when its
  files are missing, empty, or in a directory other accounts can open.
- scripts/check_container_hardening.sh refuses a container missing any one of
  its invariants, whichever flag went missing, and
  scripts/check_loopback_ports.sh refuses one published beyond loopback.
- The dogfood setup scripts publish on 127.0.0.1, commit no superuser password,
  and print none.
- integration.yml puts no secret in any job's environment, hands the Cloud API
  key to exactly one step, gives the Snowflake transform suite a key path
  rather than the key, and runs no third-party action after a step that puts a
  credential where later steps can read it.

The scripts run against fakes put first on PATH (a clickhousectl that answers
from canned JSON, a docker that answers one inspect line), so nothing here
needs a network, a daemon, or a credential.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "scripts" / "clickhouse_cloud" / "preflight.sh"
CHECKER = ROOT / "scripts" / "check_container_hardening.sh"
LOOPBACK = ROOT / "scripts" / "check_loopback_ports.sh"
DOGFOOD_SETUPS = (
    ROOT / "scripts" / "setup_postgres_dev.sh",
    ROOT / "scripts" / "setup_clickhouse_dev.sh",
    ROOT / "scripts" / "setup_cube_dev.sh",
)
WORKFLOW = ROOT / ".github" / "workflows" / "integration.yml"

BASH = shutil.which("bash")
JQ = shutil.which("jq")
needs_bash = pytest.mark.skipif(
    BASH is None or os.name != "posix", reason="the scripts are bash"
)

ORG = "00000000-0000-0000-0000-00000000000a"
SERVICE = "00000000-0000-0000-0000-00000000000b"
SERVICE_JSON = json.dumps({"id": SERVICE, "state": "idle"})
USAGE_JSON = json.dumps(
    {
        "costs": [
            {"serviceId": SERVICE, "metrics": {"computeCHC": 0.25}, "locked": False}
        ]
    }
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# --- the Cloud API key reaches clickhousectl alone --------------------------------


@pytest.fixture
def preflight_env(tmp_path: Path) -> dict[str, object]:
    """A PATH whose clickhousectl is a fake that records the key it was handed
    and whose jq records its environment before running the real one, plus a
    secrets directory holding the scoped key the way CI writes it.

    The environment also carries a stale key under every name a shell might
    still export. preflight must neither pass those along nor use them in place
    of the files."""

    if JQ is None:
        pytest.skip("preflight.sh needs jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_executable(
        bin_dir / "clickhousectl",
        f"""#!/usr/bin/env bash
printf 'key=%s secret=%s\\n' "${{CLICKHOUSE_CLOUD_API_KEY:-}}" \\
    "${{CLICKHOUSE_CLOUD_API_SECRET:-}}" >>"{logs}/clickhousectl"
case "$*" in
  "cloud service get"*) echo '{SERVICE_JSON}' ;;
  "cloud org usage"*) echo '{USAGE_JSON}' ;;
  *) echo "unexpected clickhousectl call: $*" >&2; exit 9 ;;
esac
""",
    )
    _write_executable(
        bin_dir / "jq",
        f"""#!/usr/bin/env bash
env | cut -d= -f1 >>"{logs}/jq"
exec "{JQ}" "$@"
""",
    )
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "clickhouse_api_key").write_text("key-from-file", encoding="utf-8")
    (secrets / "clickhouse_api_secret").write_text("secret-from-file", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "DEX_SECRETS_DIR": str(secrets),
        "DEX_TEST_CH_CLOUD_ORG_ID": ORG,
        "DEX_TEST_CH_CLOUD_SERVICE_ID": SERVICE,
        "DEX_TEST_CH_CLOUD_MAX_DAILY_CHC": "2",
        "CLICKHOUSE_CLOUD_API_KEY": "stale-exported-key",
        "CLICKHOUSE_CLOUD_API_SECRET": "stale-exported-secret",
        "DEX_TEST_CH_CLOUD_API_KEY": "stale-ci-key",
        "DEX_TEST_CH_CLOUD_API_SECRET": "stale-ci-secret",
    }
    return {"env": env, "secrets": secrets, "logs": logs}


def _preflight(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [BASH, str(PREFLIGHT)], env=env, capture_output=True, text=True, check=False
    )


@needs_bash
def test_preflight_hands_the_key_file_to_clickhousectl_and_to_nothing_else(
    preflight_env,
):
    result = _preflight(preflight_env["env"])

    assert result.returncode == 0, result.stderr
    assert '"compute_chc": 0.25' in result.stdout
    logs: Path = preflight_env["logs"]
    calls = (logs / "clickhousectl").read_text(encoding="utf-8").splitlines()
    # Both control-plane calls, each with the key from the files and never the
    # one the environment still exported.
    assert calls == ["key=key-from-file secret=secret-from-file"] * 2
    seen_by_jq = set((logs / "jq").read_text(encoding="utf-8").split())
    leaked = {
        "CLICKHOUSE_CLOUD_API_KEY",
        "CLICKHOUSE_CLOUD_API_SECRET",
        "DEX_TEST_CH_CLOUD_API_KEY",
        "DEX_TEST_CH_CLOUD_API_SECRET",
    } & seen_by_jq
    assert not leaked, f"jq inherited {sorted(leaked)}"


@needs_bash
@pytest.mark.parametrize("problem", ["missing", "empty"])
def test_preflight_refuses_without_its_key_files_and_names_the_path(
    preflight_env, problem: str
):
    secrets: Path = preflight_env["secrets"]
    if problem == "missing":
        (secrets / "clickhouse_api_key").unlink()
    else:
        (secrets / "clickhouse_api_key").write_text("", encoding="utf-8")

    result = _preflight(preflight_env["env"])

    assert result.returncode == 2
    assert str(secrets / "clickhouse_api_key") in result.stderr
    assert not (preflight_env["logs"] / "clickhousectl").exists()


@needs_bash
def test_preflight_refuses_a_secrets_directory_other_accounts_can_open(preflight_env):
    secrets: Path = preflight_env["secrets"]
    secrets.chmod(0o755)

    result = _preflight(preflight_env["env"])

    assert result.returncode == 2
    assert "lets other accounts in" in result.stderr
    assert not (preflight_env["logs"] / "clickhousectl").exists()


@needs_bash
def test_preflight_never_takes_the_admin_key_for_the_scoped_one(preflight_env):
    """The admin files live under different names, so an operator's admin key in
    the same directory cannot pass a check that is meant to prove the CI key."""

    secrets: Path = preflight_env["secrets"]
    for name in ("clickhouse_api_key", "clickhouse_api_secret"):
        (secrets / name).rename(
            secrets / name.replace("clickhouse_", "clickhouse_admin_")
        )

    result = _preflight(preflight_env["env"])

    assert result.returncode == 2
    assert str(secrets / "clickhouse_api_key") in result.stderr


# --- the container hardening checker -----------------------------------------------

DIGEST = "sha256:" + "ab" * 32
HARDENED = {
    "image": f"postgres@{DIGEST}",
    "cap_drop": '["ALL"]',
    "cap_add": "null",
    "security_opt": '["no-new-privileges:true"]',
    "pids_limit": "256",
    "memory": "1073741824",
    "privileged": "false",
}


def _check(tmp_path: Path, fields: dict[str, str], *caps: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    line = "|".join(
        fields[key]
        for key in (
            "image",
            "cap_drop",
            "cap_add",
            "security_opt",
            "pids_limit",
            "memory",
            "privileged",
        )
    )
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
[[ "$1" == inspect ]] || exit 9
printf '%s\\n' '{line}'
""",
    )
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [BASH, str(CHECKER), "dex-pg", DIGEST, *caps],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_the_checker_accepts_a_hardened_container(tmp_path: Path):
    result = _check(tmp_path, HARDENED)
    assert result.returncode == 0, result.stderr


@needs_bash
def test_the_checker_accepts_documented_cap_adds_however_docker_spells_them(
    tmp_path: Path,
):
    fields = {**HARDENED, "cap_add": '["CAP_SETGID","CAP_SETUID"]'}
    result = _check(tmp_path, fields, "SETUID", "setgid")
    assert result.returncode == 0, result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("field", "value", "said"),
    [
        ("image", "postgres:16", "not pinned"),
        ("image", "postgres@sha256:" + "cd" * 32, "not pinned"),
        ("cap_drop", "null", "capabilities dropped"),
        ("cap_drop", '["NET_RAW"]', "capabilities dropped"),
        ("cap_add", '["CAP_SYS_ADMIN"]', "capabilities added back"),
        ("security_opt", "null", "no-new-privileges"),
        ("security_opt", '["no-new-privileges:false"]', "no-new-privileges"),
        ("pids_limit", "<nil>", "pids limit"),
        ("pids_limit", "0", "pids limit"),
        ("pids_limit", "-1", "pids limit"),
        ("memory", "0", "memory limit"),
        ("privileged", "true", "privileged"),
    ],
)
def test_the_checker_refuses_a_container_missing_any_invariant(
    tmp_path: Path, field: str, value: str, said: str
):
    result = _check(tmp_path, {**HARDENED, field: value})
    assert result.returncode == 1
    assert said in result.stderr
    assert "refusing dex-pg" in result.stderr


@needs_bash
def test_the_checker_refuses_a_cap_add_the_caller_did_not_name(tmp_path: Path):
    fields = {**HARDENED, "cap_add": '["CAP_SETGID","CAP_SETUID"]'}
    result = _check(tmp_path, fields, "SETUID")
    assert result.returncode == 1
    assert "capabilities added back" in result.stderr


# --- the dogfood databases listen on loopback only ---------------------------------


def _check_ports(tmp_path: Path, bindings: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
[[ "$1" == inspect ]] || exit 9
printf '%s\\n' '{bindings}'
""",
    )
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [BASH, str(LOOPBACK), "dex-ch"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@needs_bash
def test_the_loopback_check_accepts_ports_published_on_127_0_0_1(tmp_path: Path):
    result = _check_ports(tmp_path, "8123/tcp=127.0.0.1 9000/tcp=127.0.0.1 ")
    assert result.returncode == 0, result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("bindings", "said"),
    [
        ("8123/tcp= 9000/tcp=127.0.0.1 ", "8123/tcp is published on 'all interfaces'"),
        ("5432/tcp=0.0.0.0 ", "5432/tcp is published on '0.0.0.0'"),
    ],
)
def test_the_loopback_check_refuses_a_port_beyond_loopback(
    tmp_path: Path, bindings: str, said: str
):
    result = _check_ports(tmp_path, bindings)
    assert result.returncode == 1
    assert said in result.stderr


@pytest.mark.parametrize("script", DOGFOOD_SETUPS, ids=lambda p: p.name)
def test_a_dogfood_setup_publishes_on_loopback_and_prints_no_password(script: Path):
    body = script.read_text(encoding="utf-8")
    published = [line.strip() for line in body.splitlines() if '-p "' in line]
    assert published, "the setup script publishes no port"
    assert all(line.startswith('-p "127.0.0.1:') for line in published), published
    assert "check_loopback_ports.sh" in body
    assert "SUPERPASS" not in body
    assert "POSTGRES_PASSWORD=postgres" not in body
    echoed = [line for line in body.splitlines() if line.strip().startswith("echo")]
    assert not [line for line in echoed if "PASSWORD" in line or ":dex_ro@" in line]


# --- the workflow's credential channels ---------------------------------------------


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _secret_refs(mapping: dict | None) -> dict[str, str]:
    return {
        name: str(value)
        for name, value in (mapping or {}).items()
        if "secrets." in str(value)
    }


def test_no_job_carries_a_secret_in_its_job_level_env(jobs):
    """A job-level secret is inherited by every step and every process those
    steps start. Each secret belongs on the one step that reads it."""

    offenders = {
        name: sorted(_secret_refs(job.get("env")))
        for name, job in jobs.items()
        if _secret_refs(job.get("env"))
    }
    assert offenders == {}


@pytest.mark.parametrize("consumer", ["engine", "dbt"])
def test_databricks_ci_refreshes_auth_instead_of_reusing_the_bootstrap_token(
    jobs, monkeypatch, tmp_path, consumer
):
    """Both clients must renew OIDC auth after the exported token expires.

    Keep the real SDK credential selection and dbt credential manager; only
    the identity provider and token exchange are fake. In particular, dbt's
    token branch must still honor the workflow's explicit SDK auth type.
    """

    sdk = pytest.importorskip("databricks.sdk.core")
    providers = pytest.importorskip("databricks.sdk.credentials_provider")
    from types import SimpleNamespace

    from exmergo_dex_core.config import DatabricksTarget
    from exmergo_dex_core.connect import resolve_databricks_connection

    host = "https://test.cloud.databricks.com"
    variables = {
        "${{ vars.DEX_TEST_DATABRICKS_HOST }}": host,
        "${{ vars.DEX_TEST_DATABRICKS_CLIENT_ID }}": "ci-principal",
    }
    for name in list(os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name)
    for name, value in jobs["databricks"]["env"].items():
        if name.startswith("DATABRICKS_"):
            monkeypatch.setenv(name, variables.get(value, value))
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "no-profile"))
    monkeypatch.setenv("DATABRICKS_TOKEN", "expired-bootstrap")

    # Discovery is metadata only. No real workspace, token or SQL connection.
    monkeypatch.setattr(sdk.Config, "_resolve_host_metadata", lambda self: None)
    monkeypatch.setattr(
        sdk.Config,
        "databricks_oidc_endpoints",
        property(lambda self: SimpleNamespace(token_endpoint=f"{host}/oidc/v1/token")),
    )
    generation = [1]

    def github_token(self, audience):
        assert audience == host  # matches the provisioned federation policy
        return f"github-jwt-{generation[0]}"

    def exchange(**kwargs):
        assert kwargs["client_id"] == "ci-principal"
        assert kwargs["endpoint_params"]["subject_token"] == (
            f"github-jwt-{generation[0]}"
        )
        token = SimpleNamespace(
            token_type="Bearer",  # noqa: S106 (OAuth scheme, not a credential)
            access_token=f"fresh-{generation[0]}",
        )
        return SimpleNamespace(token=lambda: token)

    monkeypatch.setattr(
        providers.oidc_token_supplier.GitHubOIDCTokenSupplier,
        "get_oidc_token",
        github_token,
    )
    monkeypatch.setattr(providers.oauth, "ClientCredentials", exchange)

    if consumer == "engine":
        config, method = resolve_databricks_connection(
            DatabricksTarget(), os.environ, tmp_path
        )
        authenticate = config.authenticate
        assert method == "environment:workload_identity"
    else:
        credentials = pytest.importorskip("dbt.adapters.databricks.credentials")
        manager = credentials.DatabricksCredentialManager.create_from(
            credentials.DatabricksCredentials(
                host=host.removeprefix("https://"),
                http_path="/sql/1.0/warehouses/test",
                database="scratch",
                schema="dbt_dev",
                token=os.environ["DATABRICKS_TOKEN"],
            )
        )
        authenticate = manager.credentials_provider()

    assert authenticate() == {"Authorization": "Bearer fresh-1"}
    generation[0] = 2  # same client, a later request after the old token expired
    assert authenticate() == {"Authorization": "Bearer fresh-2"}


def test_the_cloud_api_key_reaches_exactly_one_step(jobs):
    holders = [
        step.get("name")
        for step in jobs["clickhouse_cloud"]["steps"]
        if any(
            "DEX_TEST_CH_CLOUD_API_" in ref
            for ref in _secret_refs(step.get("env")).values()
        )
    ]
    assert holders == ["Materialize the Cloud API key for the commands that read it"]


def test_the_snowflake_transform_suite_gets_a_key_path_not_the_key(jobs):
    """The suite runs dbt and the engine; neither needs the PEM, only its path.
    Unsetting the PEM for the pytest child alone would not do: a child can read
    its parent shell's initial environment."""

    suite = next(
        step
        for step in jobs["snowflake"]["steps"]
        if "test_snowflake_transform.py" in str(step.get("run", ""))
    )
    env = suite.get("env") or {}
    assert "SNOWFLAKE_PRIVATE_KEY_FILE" in env
    assert "SNOWFLAKE_PRIVATE_KEY" not in env
    assert _secret_refs(env) == {}


def test_the_redshift_dev_password_rides_only_on_the_transform_step(jobs):
    holders = [
        str(step.get("run", ""))
        for step in jobs["redshift"]["steps"]
        if "DEX_TEST_REDSHIFT_DEV_PASSWORD" in _secret_refs(step.get("env"))
    ]
    assert len(holders) == 1
    assert "test_redshift_transform.py" in holders[0]


def test_the_ci_postgres_service_runs_the_dogfood_script_s_image(jobs):
    """One digest, written twice; bumping one and not the other would leave CI
    and local dogfood on different servers without anything noticing."""

    script = (ROOT / "scripts" / "setup_postgres_dev.sh").read_text(encoding="utf-8")
    digest = next(
        line.split("=", 1)[1]
        for line in script.splitlines()
        if line.startswith("DIGEST=")
    )
    assert jobs["postgres"]["services"]["postgres"]["image"] == f"postgres@{digest}"


def test_the_trino_catalog_sidecar_runs_the_postgres_dogfood_image():
    """The Iceberg catalog's Postgres is the image the Postgres connector is
    tested against, so one digest bump moves both; a sidecar left on an old
    digest would be a second, unreviewed server image in CI."""

    def assignment(script: str, name: str) -> str:
        text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        return next(
            line.split("=", 1)[1]
            for line in text.splitlines()
            if line.startswith(f"{name}=")
        )

    assert assignment("setup_trino_dev.sh", "CATALOG_DIGEST") == assignment(
        "setup_postgres_dev.sh", "DIGEST"
    )


def test_the_trino_job_needs_no_secret_and_seeds_through_the_dogfood_script(jobs):
    """Keyless and fork-runnable by construction: no environment, no secret,
    and the one seeding path the dogfood uses."""

    job = jobs["trino"]
    assert "environment" not in job
    assert "if" not in job
    steps = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "scripts/setup_trino_dev.sh" in steps
    assert "secrets." not in str(job)


# --- third-party actions never run beside a minted credential ----------------------

#: Steps that put a credential where every later step in the job can read it:
#: into GITHUB_ENV, onto the runner's disk, or into the default AWS/GCP chain.
_CREDENTIAL_ACTIONS = (
    "google-github-actions/auth",
    "actions/github-script",
    "aws-actions/configure-aws-credentials",
)


def _writes_a_credential(step: dict) -> bool:
    uses = str(step.get("uses", ""))
    if uses.split("@")[0] in _CREDENTIAL_ACTIONS:
        return True
    return "secrets." in json.dumps(step.get("env") or {}) and "DEX_SECRETS_DIR" in str(
        step.get("run", "")
    )


def test_no_third_party_action_runs_after_a_credential_reaches_the_job(jobs):
    """A SHA pin limits which code an action runs, not what that code can read.
    setup-uv and `uv sync` need no cloud credential, so they run first, and the
    only steps after a credential is minted are dex's own `run:` steps."""

    offenders = {}
    for name, job in jobs.items():
        minted = False
        for step in job.get("steps", []):
            uses = str(step.get("uses", ""))
            if minted and uses and uses.split("@")[0] not in _CREDENTIAL_ACTIONS:
                offenders.setdefault(name, []).append(uses.split("@")[0])
            minted = minted or _writes_a_credential(step)
    assert offenders == {}
