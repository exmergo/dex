"""`transform build` gating: confirm handshake, prod refusal, sanitized summary."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


@pytest.fixture
def bigquery_project_dir(dbt_project_dir: Path) -> Path:
    """The shared dbt project, retyped to a BigQuery dev target.

    The billed-paradigm tests drive `--connector bigquery` for its cost gate, and
    the dev-target preflight now (correctly) refuses a build whose profile names a
    different adapter than the connector governing it. So the profile has to say
    what the test claims it is.
    """

    (dbt_project_dir / "profiles.yml").write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        "      project: dex-test\n"
        "      dataset: dbt_dev\n"
        "    prod:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        "      project: dex-test\n"
        "      dataset: prod\n",
        encoding="utf-8",
    )
    return dbt_project_dir


@pytest.fixture
def forbid_dbt(monkeypatch: pytest.MonkeyPatch):
    """Fail the test if the gate lets a dbt subprocess launch."""

    # importlib rather than attribute access: the transform package re-exports
    # the build *function* under the same name as the module.
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def exploded(timeout: float, cwd):
        def run(argv: list[str]):
            raise AssertionError(f"dbt was invoked through the gate: {argv}")

        return run

    monkeypatch.setattr(build_module, "_default_runner", exploded)


def test_unconfirmed_build_needs_confirmation(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt
):
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--target", "dev"], capsys
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    # DuckDB is free, but the gate still runs: the cost is surfaced before spend.
    assert envelope["cost"]["paradigm"] == "free_local"
    assert envelope["cost"]["estimate"] == 0.0


@pytest.mark.parametrize("target", ["prod", "production", "PRD", "live"])
def test_prod_target_is_refused_even_confirmed(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt, target: str
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            target,
            "--confirm",
            "--budget",
            "1",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "prod" in envelope["errors"][0].lower() or "dev" in envelope["errors"][0]


def test_configured_prod_target_is_still_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt
):
    dex_dir = tmp_path / ".dex"
    dex_dir.mkdir()
    (dex_dir / "config.yml").write_text("dbt_target: prod\n", encoding="utf-8")
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--confirm"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"


def test_non_dev_target_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt
):
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "staging",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"


def _fake_runner_factory(
    monkeypatch,
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    run_results_json: tuple[Path, str] | None = None,
):
    """Replace _default_runner with a recorder returning a canned dbt result.

    ``run_results_json``, when given, is written only once the fake ``run()``
    is actually invoked -- matching real dbt, which writes the artifact as
    part of running rather than beforehand (`build()` clears any stale one
    right before invocation, so pre-seeding it ahead of the call would just
    have it deleted unread).
    """

    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[dict] = []

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            calls.append({"argv": argv, "cwd": cwd, "env": env})
            if run_results_json is not None:
                path, content = run_results_json
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return subprocess.CompletedProcess(
                args=argv, returncode=returncode, stdout=stdout, stderr=stderr
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    return calls


def test_build_pins_cwd_to_the_project_dir(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    calls = _fake_runner_factory(monkeypatch, returncode=0)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert len(calls) == 1
    assert Path(calls[0]["cwd"]) == dbt_project_dir


def test_build_failure_error_names_the_first_dbt_message(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    first = "Compilation Error in model kpi_x: something specific went wrong"
    huge = "Traceback (most recent call last):\n" + ("  frame line\n" * 400)
    lines = [
        json.dumps({"info": {"level": "error", "msg": first}}),
        json.dumps({"info": {"level": "error", "msg": first}}),  # duplicate
        json.dumps({"info": {"level": "error", "msg": huge}}),
    ]
    _fake_runner_factory(monkeypatch, returncode=1, stdout="\n".join(lines))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["errors"][0] == f"dbt build failed: {first}"
    # The duplicate is gone: the first message rides in errors and appears
    # nowhere in warnings.
    assert all(first not in w for w in envelope["warnings"])
    # The traceback collapsed to its first line, capped.
    assert all(len(w) <= 450 for w in envelope["warnings"])
    assert all("frame line" not in w for w in envelope["warnings"])
    # Trimming happened, so the full-log pointer is present.
    assert any("logs" in w and "dbt.log" in w for w in envelope["warnings"])


def test_build_failure_error_skips_deprecation_warnings_for_the_real_cause(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Regression for #50: a dbt 1.11 deprecation notice logs before the real
    failure on every normally-authored project, and must not win errors[0]."""

    real_error = (
        'Database error while listing schemas in database "NOPE_MISSING_DB"\n'
        "  Database Error\n"
        "    002043 (02000): SQL compilation error:\n"
        "    Object does not exist, or operation cannot be performed."
    )
    lines = [
        json.dumps(
            {
                "info": {
                    "level": "warn",
                    "name": "PropertyMovedToConfigDeprecation",
                    "msg": "[WARNING][PropertyMovedToConfigDeprecation]: "
                    "Deprecated functionality",
                }
            }
        ),
        json.dumps(
            {
                "info": {
                    "level": "error",
                    "name": "MainEncounteredError",
                    "msg": real_error,
                }
            }
        ),
    ]
    _fake_runner_factory(monkeypatch, returncode=2, stdout="\n".join(lines))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["errors"][0] == (
        "dbt build failed: Database error while listing schemas in database "
        '"NOPE_MISSING_DB"'
    )
    assert any("PropertyMovedToConfigDeprecation" in w for w in envelope["warnings"])


def test_build_failure_names_the_cause_behind_a_per_node_database_error(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Regression for #76: a per-node `Database/Runtime/Compilation Error in
    <node> (<path>)` header (dbt_common's DbtRuntimeError.__str__ shape, the
    same on every adapter) carries no cause of its own -- the real one is the
    next line. Shape confirmed against a real `dbt build --log-format json`
    failure, not hand-guessed."""

    real_error = (
        "Runtime Error in model my_model (models/staging/my_model.sql)\n"
        "  Argument 2 to JSON_VALUE must be a constant expression\n"
        "  compiled code at target/run/dex_test/models/staging/my_model.sql"
    )
    lines = [
        json.dumps(
            {
                "info": {
                    "level": "error",
                    "name": "LogModelResult",
                    "msg": "1 of 1 ERROR creating sql view model my_model ... "
                    "[ERROR in 0.12s]",
                }
            }
        ),
        json.dumps(
            {
                "info": {
                    "level": "error",
                    "name": "RunResultFailure",
                    "msg": "Failure in model my_model (models/staging/my_model.sql)",
                }
            }
        ),
        json.dumps(
            {"info": {"level": "error", "name": "RunResultError", "msg": real_error}}
        ),
    ]
    _fake_runner_factory(monkeypatch, returncode=1, stdout="\n".join(lines))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["errors"][0] == (
        "dbt build failed: Runtime Error in model my_model "
        "(models/staging/my_model.sql): Argument 2 to JSON_VALUE must be a "
        "constant expression"
    )
    # The uninformative progress line and bare failure header are demoted to
    # warnings, not lost, and not mistaken for the cause.
    assert any("Failure in model my_model" in w for w in envelope["warnings"])
    assert any("ERROR creating" in w for w in envelope["warnings"])


def test_build_failure_names_the_cause_behind_a_whole_run_fatal(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """A whole-invocation fatal (no node ever ran) is wrapped by dbt's own
    top-level handler in "Encountered an error:", then by each nested
    exception level in a bare "<Type> Error" -- confirmed against a real `dbt
    build` failure (a division-by-zero in a profile Jinja expression)."""

    real_error = (
        "Encountered an error:\n"
        "Runtime Error\n"
        "  Compilation Error\n"
        "    Could not render {{ 1/0 }}: division by zero"
    )
    lines = [
        json.dumps(
            {
                "info": {
                    "level": "error",
                    "name": "MainEncounteredError",
                    "msg": real_error,
                }
            }
        ),
    ]
    _fake_runner_factory(monkeypatch, returncode=2, stdout="\n".join(lines))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["errors"][0] == (
        "dbt build failed: Encountered an error: Could not render {{ 1/0 }}: "
        "division by zero"
    )


def test_build_failure_message_strips_ansi_color_codes(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """dbt colors console messages even under --log-format json; the escapes
    are noise once that text crosses into the JSON envelope."""

    lines = [
        json.dumps(
            {
                "info": {
                    "level": "error",
                    "name": "RunResultFailure",
                    "msg": "\x1b[31mFailure in model x (models/x.sql)\x1b[0m",
                }
            }
        ),
    ]
    _fake_runner_factory(monkeypatch, returncode=1, stdout="\n".join(lines))
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["errors"][0] == (
        "dbt build failed: Failure in model x (models/x.sql)"
    )
    assert "\x1b" not in envelope["errors"][0]


def test_build_ignores_a_stale_run_results_json_from_a_prior_invocation(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Regression for #76: a whole-invocation fatal (e.g. a Jinja context
    error) dies before dbt ever reaches node execution, so it never rewrites
    target/run_results.json. A stale one left over from a prior successful
    build must not be reported as this invocation's node results -- verified
    against real dbt behavior (its mtime is untouched by such a failure)."""

    target_dir = dbt_project_dir / "target"
    target_dir.mkdir(exist_ok=True)
    (target_dir / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "unique_id": "model.dex_test.stg_customers",
                        "status": "success",
                        "execution_time": 1.23,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # A fatal that never touches run_results.json: empty stdout, no node ever
    # ran, matching a real whole-invocation compile/parse-time crash.
    _fake_runner_factory(monkeypatch, returncode=2, stdout="")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["data"]["nodes"] == []
    assert envelope["data"]["counts"] == {}


def test_missing_dev_db_with_sources_is_an_actionable_error(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt
):
    (dbt_project_dir / "models" / "staging" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n",
        encoding="utf-8",
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert "seed" in envelope["errors"][0]
    assert "dev.duckdb" in envelope["errors"][0]


def test_missing_dev_db_without_sources_only_warns(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _fake_runner_factory(monkeypatch, returncode=0)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert any("does not exist" in w for w in envelope["warnings"])


def test_confirmed_dev_build_runs_dbt_for_real(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("dbt.cli.main")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["success"] is True
    assert envelope["data"]["target"] == "dev"
    node_names = {n["name"] for n in envelope["data"]["nodes"]}
    assert "stg_customers" in node_names
    assert (
        envelope["data"]["counts"].get("success", 0)
        + envelope["data"]["counts"].get("pass", 0)
        >= 2
    )  # the model and its not_null test
    # No raw dbt log text in data: only the structured summary keys.
    assert set(envelope["data"]) == {
        "target",
        "success",
        "returncode",
        "nodes",
        "counts",
    }


def test_relative_profile_path_resolves_against_project(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """A relative duckdb path in profiles.yml must land in the project dir, not
    wherever the caller's shell happened to be (the stray-database defect)."""

    pytest.importorskip("dbt.cli.main")
    (dbt_project_dir / "profiles.yml").write_text(
        "dex_test:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        "      path: dev-rel.duckdb\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert (dbt_project_dir / "dev-rel.duckdb").exists()
    assert not (elsewhere / "dev-rel.duckdb").exists()


def test_build_paths_are_absolute_from_a_relative_project_dir(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """A relative project dir must not double against the cwd we pin. dbt resolves
    --project-dir against the process cwd, and the runner pins that cwd to the
    project; a relative --project-dir would resolve to project/project and fail."""

    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    captured: dict = {}

    def runner(argv: list[str]):
        captured["argv"] = argv
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    monkeypatch.chdir(tmp_path)
    summary, _cost = build_module.build(
        "analytics",  # relative to the pinned cwd (tmp_path)
        target="dev",
        confirmed=True,
        runner=runner,
    )
    assert summary["success"] is True

    argv = captured["argv"]
    project_arg = Path(argv[argv.index("--project-dir") + 1])
    profiles_arg = Path(argv[argv.index("--profiles-dir") + 1])
    assert project_arg.is_absolute()
    assert profiles_arg.is_absolute()
    assert project_arg == dbt_project_dir.resolve()
    # The doubling bug would have produced .../analytics/analytics.
    assert project_arg.parent.name != "analytics"


def test_shadow_parse_profiles_dir_is_absolute_from_a_relative_project(
    dbt_project_dir: Path, tmp_path: Path, monkeypatch
):
    """The define-time parse gate had the same doubling on --profiles-dir: its cwd
    is an absolute shadow tempdir, so a relative --profiles-dir pointing at the
    real project would resolve against the shadow and fail."""

    pytest.importorskip("dbt.cli.main")
    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    captured: dict = {}

    def runner(argv: list[str]):
        captured["argv"] = argv
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    monkeypatch.chdir(tmp_path)
    result = build_module.shadow_parse("analytics", [], target="dev", runner=runner)
    assert result["available"] is True

    argv = captured["argv"]
    assert Path(argv[argv.index("--project-dir") + 1]).is_absolute()
    assert Path(argv[argv.index("--profiles-dir") + 1]).is_absolute()


def test_relative_project_dir_builds_without_path_doubling(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """End-to-end proof of the blocking defect: a config-pinned relative
    dbt_project_dir and a relative repo-root build green, no 'Path ... does not
    exist' from a doubled --project-dir."""

    pytest.importorskip("dbt.cli.main")
    dex_dir = tmp_path / ".dex"
    dex_dir.mkdir()
    (dex_dir / "config.yml").write_text(
        "dbt_project_dir: analytics\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    rc, envelope = _run(
        ["--repo-root", ".", "transform", "build", "--target", "dev", "--confirm"],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["data"]["success"] is True


# --- billed connectors (BigQuery): the ceiling binds, spend is ledgered --------


def _install_fake_pricing(
    monkeypatch,
    *,
    connector: str,
    paradigm,
    estimate: float | None,
    per_node: dict,
    confirmed: bool,
    ceiling: float | None,
    describe=None,
    notes: list[str] = (),
):
    """Make ``cmd_build`` price a billed build without a real warehouse.

    Replaces the adapter open with a fake carrying a real ``CostGate``, stubs
    ``compile_estimate`` so no dbt compile or dry-run runs, and neutralizes the
    dev-target check (which would otherwise open its own connection). Returns the
    fake adapter, whose ``.closed`` records that ``cmd_build`` closed it.
    """

    from exmergo_dex_core import command_args
    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.transform import dev_target

    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    class FakeAdapter:
        def __init__(self):
            self.paradigm = paradigm
            self.name = connector
            self.cost_gate = CostGate(
                paradigm=paradigm,
                ceiling=ceiling,
                session_ceiling=None,
                session_spent=0.0,
                confirmed=confirmed,
                connector=connector,
                command="transform build",
            )
            self.closed = False

        def query_estimate(self, sql):  # not exercised: compile_estimate is stubbed
            return 0.0

        def close(self):
            self.closed = True

    adapter = FakeAdapter()
    if describe is not None:
        adapter.describe_estimate = describe
    monkeypatch.setattr(command_args, "open_from_args", lambda args: adapter)
    monkeypatch.setattr(
        build_module,
        "compile_estimate",
        lambda project, adp, *, target, select=None, **kw: (
            estimate,
            dict(per_node),
            list(notes),
        ),
    )
    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])
    return adapter


def test_billed_build_unconfirmed_needs_confirmation_with_estimate(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The build is priced upfront (a free `dbt compile` dry-run of each node),
    so the confirm handshake carries a real byte estimate, not a null."""

    from exmergo_dex_core.envelope import Paradigm

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=5_000_000.0,
        per_node={"stg_customers": 5_000_000.0},
        confirmed=False,
        ceiling=100_000_000,
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--target",
            "dev",
            "--budget",
            "100000000",
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert envelope["cost"]["paradigm"] == "bytes_scanned"
    assert envelope["cost"]["estimate"] == 5_000_000.0
    assert envelope["cost"]["ceiling"] == 100_000_000
    # BigQuery has no describe_estimate, so the default bytes payload is used.
    assert envelope["data"]["estimated_bytes"] == 5_000_000.0
    assert envelope["data"]["per_table_bytes"]["stg_customers"] == 5_000_000.0


def test_billed_build_surfaces_estimate_quality_on_compute_time(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Compute-time connectors price via a heuristic; the handshake carries the
    honest `estimate_quality` label the connector's describe_estimate returns."""

    from exmergo_dex_core.envelope import Paradigm

    def describe(estimate, per_table=None):
        return {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "per_table_seconds": per_table or {},
        }

    _install_fake_pricing(
        monkeypatch,
        connector="snowflake",
        paradigm=Paradigm.COMPUTE_TIME,
        estimate=42.0,
        per_node={"stg_customers": 42.0},
        confirmed=False,
        ceiling=600,
        describe=describe,
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "snowflake",
            "transform",
            "build",
            "--target",
            "dev",
            "--budget",
            "600",
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert envelope["cost"]["paradigm"] == "compute_time"
    assert envelope["cost"]["estimate"] == 42.0
    assert envelope["data"]["estimated_seconds"] == 42.0
    assert envelope["data"]["estimate_quality"] == "heuristic"


def test_billed_build_degrades_to_no_estimate_when_connection_unavailable(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch, forbid_dbt
):
    """dex discovers its own connection separately from dbt's profile, so a
    connection dex cannot open must not break a build dbt could run: pricing
    degrades to no estimate with a note, and the gate still binds."""

    from exmergo_dex_core import command_args
    from exmergo_dex_core.transform import dev_target

    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])

    def boom(args):
        raise RuntimeError("no application default credentials")

    monkeypatch.setattr(command_args, "open_from_args", boom)
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--target",
            "dev",
            "--budget",
            "100000000",
        ],
        capsys,
    )
    assert rc == 0
    assert envelope["status"] == "needs_confirmation"
    assert envelope["cost"]["estimate"] is None
    assert any("could not price" in w for w in envelope["warnings"])


def test_billed_build_without_a_budget_is_refused(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    from exmergo_dex_core.envelope import Paradigm

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=5_000_000.0,
        per_node={"stg_customers": 5_000_000.0},
        confirmed=True,
        ceiling=None,
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "--budget" in envelope["errors"][0]


def test_billed_build_sums_bytes_billed_into_the_ledger(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    import json as json_mod

    from exmergo_dex_core.envelope import Paradigm

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=5_000_000.0,
        per_node={"stg_customers": 5_000_000.0},
        confirmed=True,
        ceiling=100_000_000,
    )
    target_dir = bigquery_project_dir / "target"
    run_results = json_mod.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 1000},
                },
                {
                    "unique_id": "model.dex_test.mart_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 2000},
                },
            ]
        }
    )
    # compile_estimate is stubbed, so the fake runner only serves the dbt build.
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(target_dir / "run_results.json", run_results),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            "100000000",
        ],
        capsys,
    )
    assert rc == 0, envelope
    # The confirmed run's envelope carries the preflight estimate and the actual.
    assert envelope["cost"]["estimate"] == 5_000_000.0
    assert envelope["data"]["bytes_billed"] == 3000
    assert any("maximum_bytes_billed" in w for w in envelope["warnings"])
    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json_mod.loads(ledger[-1])
    assert entry["command"] == "transform build"
    assert entry["billed_bytes"] == 3000


def test_billed_build_failure_names_the_real_error_in_errors(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The failure-path envelope on the billed connector: the real dbt message
    rides in errors, not buried in warnings (guards the sanitized-failure fix on
    the bytes_scanned paradigm)."""

    from exmergo_dex_core.envelope import Paradigm

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=5_000_000.0,
        per_node={"stg_customers": 5_000_000.0},
        confirmed=True,
        ceiling=100_000_000,
    )
    msg = "Database Error in model x: Access Denied on dataset dbt_dev"
    _fake_runner_factory(
        monkeypatch,
        returncode=1,
        stdout=json.dumps({"info": {"level": "error", "msg": msg}}),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--target",
            "dev",
            "--confirm",
            "--budget",
            "100000000",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert envelope["errors"][0] == f"dbt build failed: {msg}"


def test_build_env_caps_postgres_statements_via_pgoptions(monkeypatch):
    """On db-load gating the ceiling becomes a server-side statement_timeout
    injected through PGOPTIONS (the maximum_bytes_billed analogue: dbt has no
    dry-run, so the per-statement cap is the binding cost control)."""

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform.build import _build_env

    monkeypatch.delenv("PGOPTIONS", raising=False)
    env = _build_env(Paradigm.DB_LOAD, 120.0)
    assert env is not None
    assert env["PGOPTIONS"] == "-c statement_timeout=120s"

    monkeypatch.setenv("PGOPTIONS", "-c search_path=app")
    env = _build_env(Paradigm.DB_LOAD, 120.0)
    assert env["PGOPTIONS"] == "-c search_path=app -c statement_timeout=120s"

    assert _build_env(Paradigm.DB_LOAD, None) is None
    assert _build_env(Paradigm.FREE_LOCAL, 120.0) is None
    assert _build_env(Paradigm.BYTES_SCANNED, 120.0) is None


def test_dev_target_check_runs_before_the_cost_gate(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt
):
    """A dev target that cannot work is refused before anyone is asked to weigh a
    budget. The preflight is free, so surfacing `needs_confirmation` for a build
    that is already doomed would be the wrong order.
    """

    (dbt_project_dir / "models" / "staging" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n",
        encoding="utf-8",
    )
    # No --confirm: the old ordering would have returned needs_confirmation here.
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--target", "dev"], capsys
    )
    assert rc == 1
    assert envelope["status"] == "error"
    assert "seed" in envelope["errors"][0]


def test_prod_refusal_still_beats_the_dev_target_check(
    dbt_project_dir: Path, tmp_path: Path, capsys, forbid_dbt
):
    """Ordering, continued: a prod target is refused outright, before dex goes
    looking at whether that target happens to exist."""

    (dbt_project_dir / "models" / "staging" / "sources.yml").write_text(
        "version: 2\nsources:\n  - name: raw\n    tables:\n      - name: customers\n",
        encoding="utf-8",
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--target", "prod"], capsys
    )
    assert rc == 1
    assert "prod" in envelope["errors"][0].lower()
    assert "seed" not in envelope["errors"][0]


# --- compile_estimate: pricing a build from a free dbt compile dry-run --------


def _compile_runner(
    monkeypatch, project: Path, run_results: dict, *, returncode: int = 0
):
    """Fake ``_default_runner`` for compile: writes the given run_results.json on
    invocation (mirroring real dbt) and returns the requested code."""

    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            (project / "target").mkdir(parents=True, exist_ok=True)
            (project / "target" / "run_results.json").write_text(
                json.dumps(run_results), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, returncode, "", "")

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)


class _EstimatingAdapter:
    """Prices each SQL at a fixed cost, raising for any SQL containing ``fail``
    (standing in for a node whose dev-target input does not exist yet)."""

    def __init__(self, per_sql: float = 10.0):
        self._per_sql = per_sql

    def query_estimate(self, sql: str) -> float:
        if "fail" in sql:
            raise RuntimeError("relation not found in the dev target")
        return self._per_sql


def _write_manifest(project: Path, nodes: dict) -> None:
    (project / "target").mkdir(parents=True, exist_ok=True)
    (project / "target" / "manifest.json").write_text(
        json.dumps({"nodes": nodes}), encoding="utf-8"
    )


def test_compile_estimate_sums_priced_nodes_and_skips_seeds_and_ephemeral(
    dbt_project_dir: Path, monkeypatch
):
    build_mod = importlib.import_module("exmergo_dex_core.transform.build")

    nodes = {
        "model.p.stg_a": {
            "resource_type": "model",
            "name": "stg_a",
            "compiled_code": "select 1",
            "config": {"materialized": "view"},
        },
        "model.p.eph": {
            "resource_type": "model",
            "name": "eph",
            "compiled_code": "select 2",
            "config": {"materialized": "ephemeral"},
        },
        "snapshot.p.snap": {
            "resource_type": "snapshot",
            "name": "snap",
            "compiled_code": "select 3",
            "config": {},
        },
        "test.p.nn": {
            "resource_type": "test",
            "name": "not_null_stg_a",
            "compiled_code": "select 4",
            "config": {},
        },
        "seed.p.s": {
            "resource_type": "seed",
            "name": "s",
            "compiled_code": "",
            "config": {},
        },
    }
    _write_manifest(dbt_project_dir, nodes)
    _compile_runner(
        monkeypatch,
        dbt_project_dir,
        {"results": [{"unique_id": uid} for uid in nodes]},
    )
    total, per_node, notes = build_mod.compile_estimate(
        dbt_project_dir, _EstimatingAdapter(), target="dev"
    )
    # model(view) + snapshot + test priced at 10 each; ephemeral and seed skipped.
    assert total == 30.0
    assert set(per_node) == {"stg_a", "snap", "not_null_stg_a"}
    assert notes == []


def test_compile_estimate_skips_and_notes_unpriceable_nodes(
    dbt_project_dir: Path, monkeypatch
):
    build_mod = importlib.import_module("exmergo_dex_core.transform.build")

    nodes = {
        "model.p.stg_a": {
            "resource_type": "model",
            "name": "stg_a",
            "compiled_code": "select 1 from raw",
            "config": {"materialized": "view"},
        },
        "model.p.mart_b": {
            "resource_type": "model",
            "name": "mart_b",
            "compiled_code": "select * from fail_stg_a",  # its input is not built yet
            "config": {"materialized": "table"},
        },
    }
    _write_manifest(dbt_project_dir, nodes)
    _compile_runner(
        monkeypatch,
        dbt_project_dir,
        {"results": [{"unique_id": uid} for uid in nodes]},
    )
    total, per_node, notes = build_mod.compile_estimate(
        dbt_project_dir, _EstimatingAdapter(), target="dev"
    )
    assert total == 10.0
    assert set(per_node) == {"stg_a"}
    assert any("could not be priced" in n and "mart_b" in n for n in notes)


def test_compile_estimate_raises_when_compile_fails(dbt_project_dir: Path, monkeypatch):
    build_mod = importlib.import_module("exmergo_dex_core.transform.build")

    _compile_runner(monkeypatch, dbt_project_dir, {"results": []}, returncode=1)
    with pytest.raises(build_mod.DbtRunError):
        build_mod.compile_estimate(dbt_project_dir, _EstimatingAdapter(), target="dev")


def test_compile_estimate_without_an_estimator_is_a_zero_with_a_note(
    dbt_project_dir: Path,
):
    build_mod = importlib.import_module("exmergo_dex_core.transform.build")

    total, per_node, notes = build_mod.compile_estimate(
        dbt_project_dir, object(), target="dev"
    )
    assert total == 0.0
    assert per_node == {}
    assert any("no estimator" in n for n in notes)
