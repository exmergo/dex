"""`transform build` gating: confirm handshake, prod refusal, sanitized summary."""

from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.envelope import Paradigm


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


def test_unconfirmed_build_on_free_local_runs_and_warns_instead_of_asking(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Issue #197: a confirm handshake is emitted only where spend is possible.
    DuckDB is free, so an unconfirmed build runs rather than asking the caller
    to confirm spending nothing, and the envelope says so instead of staying
    silent about the skipped ask."""

    _fake_runner_factory(monkeypatch, returncode=0)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--target", "dev"], capsys
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["cost"]["paradigm"] == "free_local"
    assert envelope["cost"]["estimate"] == 0.0
    assert any("no confirm handshake" in w for w in envelope["warnings"])


def test_confirmed_build_on_free_local_carries_no_skipped_handshake_note(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Passing --confirm on a free connector does no harm, and nothing was
    actually skipped that is worth a note about."""

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
    assert not any("no confirm handshake" in w for w in envelope["warnings"])


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


@pytest.mark.parametrize("function_guard", [False, True])
def test_confirmed_dev_build_runs_dbt_for_real(
    dbt_project_dir: Path, tmp_path: Path, capsys, function_guard
):
    pytest.importorskip("dbt.cli.main")
    if function_guard:
        import yaml

        config_path = tmp_path / ".dex/config.yml"
        config_path.parent.mkdir(exist_ok=True)
        config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
        config["guards"] = {"approved_functions": ["approved_udf"]}
        config_path.write_text(yaml.safe_dump(config))
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
    # No raw dbt log text in data: only the structured summary keys, plus the
    # typed evidence and the outcome lifted out of it.
    assert set(envelope["data"]) == {
        "target",
        "success",
        "returncode",
        "nodes",
        "counts",
        "outcome",
        "evidence",
        "verification",
    }
    # `success` is dbt's exit code; `outcome` is whether anything was validated,
    # and this run built real nodes against current artifacts.
    assert envelope["data"]["outcome"] == "validated"
    evidence = envelope["data"]["evidence"]
    assert set(evidence) == {
        "outcome",
        "invocation",
        "selection",
        "digests",
        "generated",
        "errors",
        "nodes",
        "stale_artifacts",
        "plan_drift",
    }
    # No plan was named, so there is no coverage to report and the key is absent
    # rather than empty.
    assert "coverage" not in evidence
    assert evidence["selection"]["empty"] is False
    assert "stg_customers" in evidence["selection"]["matched"]
    # Present and negative rather than absent: a build that did not verify and
    # one that verified and found nothing are different answers.
    assert envelope["data"]["verification"]["ran"] is False
    assert "--verify" in envelope["data"]["verification"]["reason"]


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


@dataclass
class _CountResult:
    """One row of ``dex_rows_<n>`` aliases, as the batched count returns it."""

    columns: list[str]
    cells: list[list[int]]


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
    translate=None,
    notes: list[str] = (),
    store=None,
    session_ceiling: float | None = None,
    objects: list | None = None,
    query_estimate: float = 0.0,
    counts: list[int] = (),
):
    """Make ``cmd_build`` price a billed build without a real warehouse.

    Replaces the adapter open with a fake carrying a real ``CostGate``, stubs
    ``compile_estimate`` so no dbt compile or dry-run runs, and neutralizes the
    dev-target check (which would otherwise open its own connection). Returns the
    fake adapter, whose ``.closed`` records that ``cmd_build`` closed it.

    ``objects``/``query_estimate``/``counts`` are what a ``--verify`` run needs
    on top: the catalog the sweep resolves its relations against, the dry-run
    price of the batched count statement, and what that statement returns. A
    build that does not verify touches none of them.
    """

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.guards.cost_guard import CostGate, ledger_field, utc_day_start
    from exmergo_dex_core.transform import dev_target

    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    class FakeAdapter:
        def __init__(self):
            self.paradigm = paradigm
            self.name = connector
            # With a store, the gate is wired the way `new_cost_gate` wires a
            # real one (live reader, record hook, the store's own lock) so the
            # reservation a build holds while dbt runs is exercised. Without one
            # it stays the plain snapshot gate the pricing tests want.
            self.cost_gate = CostGate(
                paradigm=paradigm,
                ceiling=ceiling,
                session_ceiling=session_ceiling,
                session_spent=(
                    0.0
                    if store is None
                    else (
                        lambda: store.spend_since(
                            utc_day_start(),
                            field=ledger_field(paradigm),
                            connector=connector,
                        )
                    )
                ),
                confirmed=confirmed,
                connector=connector,
                command="transform build",
                record=None if store is None else store.append_spend_log,
                lock=None if store is None else store.spend_lock,
            )
            self.closed = False
            self.queries: list[str] = []
            self.estimated: list[str] = []

        dialect = "bigquery"

        def query_estimate(self, sql):
            # compile_estimate is stubbed, so the only statement priced through
            # here is `--verify`'s batched row count.
            self.estimated.append(sql)
            return query_estimate

        def list_objects(self, *, include_views: bool = True):
            return list(objects or [])

        def run_query(self, sql, max_rows=None, timeout_seconds=None):
            self.queries.append(sql)
            self.cost_gate.charge(query_estimate)
            self.cost_gate.record_billed(query_estimate)
            return _CountResult(
                columns=[f"dex_rows_{i}" for i in range(len(counts))],
                cells=[list(counts)],
            )

        def close(self):
            self.closed = True

    adapter = FakeAdapter()
    if describe is not None:
        adapter.describe_estimate = describe
    if translate is not None:
        adapter.compute_spend_translation = translate
    monkeypatch.setattr(DexEngine, "_adapter", lambda self, command=None, **kw: adapter)
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


def test_clickhouse_cloud_build_uses_compute_time_and_translates_settled_seconds(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    from exmergo_dex_core.envelope import Paradigm

    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: clickhouse\n"
        "clickhouse:\n"
        "  deployment: cloud\n"
        "  compute_unit_price_usd: 0.29846\n",
        encoding="utf-8",
    )
    _install_fake_pricing(
        monkeypatch,
        connector="clickhouse",
        paradigm=Paradigm.COMPUTE_TIME,
        estimate=1.0,
        per_node={"stg_customers": 1.0},
        confirmed=True,
        ceiling=60,
        translate=lambda seconds: {
            "compute_unit_hours_billed": seconds * 2 / 3600,
            "usd_billed": seconds * 2 / 3600 * 0.29846,
        },
    )
    run_results = json.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 18.0,
                    "adapter_response": {},
                }
            ]
        }
    )
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            run_results,
        ),
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
            "--budget",
            "60",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["cost"]["paradigm"] == "compute_time"
    spend = envelope["data"]["spend"]
    assert spend["seconds_billed"] == 18.0
    assert spend["compute_unit_hours_billed"] == pytest.approx(0.01)
    assert spend["usd_billed"] == pytest.approx(0.0029846)


def test_clickhouse_cloud_build_fails_closed_before_dbt_without_live_capacity(
    bigquery_project_dir: Path,
    tmp_path: Path,
    capsys,
    monkeypatch,
    forbid_dbt,
):
    from exmergo_dex_core.envelope import Paradigm

    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: clickhouse\nclickhouse:\n  deployment: cloud\n",
        encoding="utf-8",
    )

    def missing_capacity(_seconds):
        raise RuntimeError("capacity could not be proved")

    _install_fake_pricing(
        monkeypatch,
        connector="clickhouse",
        paradigm=Paradigm.COMPUTE_TIME,
        estimate=1.0,
        per_node={"stg_customers": 1.0},
        confirmed=True,
        ceiling=60,
        translate=missing_capacity,
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
            "--budget",
            "60",
        ],
        capsys,
    )
    assert rc == 1
    assert envelope["cost"]["paradigm"] == "compute_time"
    assert "capacity could not be proved" in envelope["errors"][0]


def test_billed_build_degrades_to_no_estimate_when_connection_unavailable(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch, forbid_dbt
):
    """dex discovers its own connection separately from dbt's profile, so a
    connection dex cannot open must not break a build dbt could run: pricing
    degrades to no estimate with a note, and the gate still binds."""

    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.transform import dev_target

    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])

    def boom(self, command=None, **_kw):
        raise RuntimeError("no application default credentials")

    monkeypatch.setattr(DexEngine, "_adapter", boom)
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
    # Issue #276: spend lives at `data.spend` and nowhere else. `transform build`
    # used to also stamp a top-level `data.bytes_billed` that no other billed
    # command carried, so a caller reading that key saw a build's spend and read
    # `maintain check`'s missing key as zero.
    assert "bytes_billed" not in envelope["data"]
    assert any("maximum_bytes_billed" in w for w in envelope["warnings"])
    ledger = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    entry = json_mod.loads(ledger[-1])
    assert entry["command"] == "transform build"
    assert entry["billed_bytes"] == 3000
    # The envelope and the ledger have to agree. `stamp_spend` has no call site
    # in transform/, and a build settles outside the gate entirely (dbt runs the
    # statements, so `record_billed` never fires), so a consumer summing
    # `data.spend` across commands used to count every build as free.
    assert envelope["data"]["spend"]["bytes_billed"] == entry["billed_bytes"]
    assert envelope["data"]["spend"]["session_spent_today"] == 3000
    # Issue #278: the estimate rides into the ledger beside the settled figure,
    # so a later over-ceiling refusal on this connector can say how far the two
    # have run apart. A build is the largest billed command dex has and settles
    # outside any gate, so without this the command most likely to be refused
    # over a ceiling would have been the one contributing nothing to calibrating
    # that refusal.
    assert entry["entry"] == "settlement"
    assert entry["estimate"] == envelope["cost"]["estimate"] == 5_000_000.0
    # Issue #277: the whole row, not only the keys this test cares about. A build
    # settles outside any gate, and the row it wrote used to say so by omission,
    # carrying no `reservation_id` at all where every gate-written row carries
    # one. A reader joining settlements on that key skipped or mis-joined every
    # build, so the row now declares the absence instead of leaving it to be
    # inferred, and pinning the shape here is what keeps the next writer honest.
    assert set(entry) == {
        "at",
        "connector",
        "command",
        "entry",
        "reservation_id",
        "billed_bytes",
        "estimate",
        "job_id",
        "statement_sha256",
    }
    assert entry["reservation_id"] is None


def test_a_billed_build_that_billed_nothing_still_reports_a_spend_of_zero(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Issue #276, the other half: a key present only sometimes is worse than one
    that never exists, so a billed connector reports `data.spend` whatever the
    build settled at. A build that billed zero and one that does not report spend
    have to be distinguishable, and `if billed:` made them identical."""

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
    # dbt ran the node and BigQuery billed nothing for it: a cache hit, or a
    # no-op incremental. The figure is reported, so it is zero.
    run_results = json.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 0},
                }
            ]
        }
    )
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            run_results,
        ),
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
    assert envelope["data"]["spend"]["bytes_billed"] == 0
    assert envelope["data"]["spend"]["session_spent_today"] == 0
    entry = json.loads((tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()[-1])
    assert entry["command"] == "transform build"
    assert entry["billed_bytes"] == 0


def test_a_build_whose_statements_reported_no_billing_figure_says_so(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Statements ran and none of them reported what they billed, so the spend is
    unknown rather than zero. The key is still there (parity), its value is null
    (honesty), a note explains it, and nothing reaches the ledger: rounding an
    unknown spend down to zero is the under-report issue #276 is about."""

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
    run_results = json.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    # No adapter_response at all: whatever this scanned, dbt is
                    # not saying.
                    "adapter_response": {},
                }
            ]
        }
    )
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            run_results,
        ),
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
    assert envelope["data"]["spend"]["bytes_billed"] is None
    # A build's per-paradigm notes ride in `warnings`, where its cap note does.
    assert any("no billing figure" in w for w in envelope["warnings"])
    assert not (tmp_path / ".dex" / "spend.jsonl").exists()


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
    env = _build_env("postgres", Paradigm.DB_LOAD, 120.0)
    assert env is not None
    assert env["PGOPTIONS"] == "-c statement_timeout=120s"

    monkeypatch.setenv("PGOPTIONS", "-c search_path=app")
    env = _build_env("postgres", Paradigm.DB_LOAD, 120.0)
    assert env["PGOPTIONS"] == "-c search_path=app -c statement_timeout=120s"

    assert _build_env("postgres", Paradigm.DB_LOAD, None) is None
    assert _build_env("postgres", Paradigm.FREE_LOCAL, 120.0) is None
    assert _build_env("postgres", Paradigm.BYTES_SCANNED, 120.0) is None


def test_build_env_caps_clickhouse_statements_via_custom_settings(monkeypatch):
    """The second db-load connector caps by a different mechanism, and the cap
    is two settings rather than one.

    ClickHouse ignores PGOPTIONS entirely, so a paradigm-keyed lookup would hand
    it a libpq variable and leave the build uncapped while the envelope claimed
    otherwise. Both settings are asserted because max_execution_time alone is
    checked at block boundaries: the byte cap is what binds on a fast scan.
    """

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform import init
    from exmergo_dex_core.transform.build import (
        _CH_SCAN_BYTES_PER_SECOND,
        _build_env,
    )

    env = _build_env("clickhouse", Paradigm.DB_LOAD, 120.0)
    assert env is not None
    assert env[init.CH_MAX_EXECUTION_TIME_ENV] == "120"
    assert env[init.CH_MAX_BYTES_TO_READ_ENV] == str(120 * _CH_SCAN_BYTES_PER_SECOND)
    assert "PGOPTIONS" not in _build_env("clickhouse", Paradigm.DB_LOAD, 120.0) or (
        env.get("PGOPTIONS") == os.environ.get("PGOPTIONS")
    )

    assert _build_env("clickhouse", Paradigm.DB_LOAD, None) is None
    assert _build_env("clickhouse", Paradigm.FREE_LOCAL, 120.0) is None


def test_an_uncapped_db_load_connector_gets_no_cap_and_claims_none(monkeypatch):
    """The degradation and the non-degraded path, asserted together.

    A db-load connector with no cap mechanism registered must inherit the plain
    environment rather than another connector's variable, and the build note
    must say the build was uncapped instead of naming a cap that was never
    applied. The Postgres assertion above it is the non-degraded sibling:
    without it, a dispatch table that returned None for everything would make
    this test pass while silently removing every connector's cap.
    """

    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.transform.build import _build_env
    from exmergo_dex_core.transform.commands import (
        _DB_LOAD_CAP_NOTES,
        _UNCAPPED_BUILD_NOTE,
    )

    assert _build_env("some-future-db", Paradigm.DB_LOAD, 120.0) is None
    assert _DB_LOAD_CAP_NOTES.get("some-future-db", _UNCAPPED_BUILD_NOTE) == (
        _UNCAPPED_BUILD_NOTE
    )
    # And every connector that does register a mechanism has a note describing
    # it, so the pair can never drift into claiming a cap it did not inject.
    from exmergo_dex_core.transform.build import _CAP_ENV_BUILDERS

    assert set(_CAP_ENV_BUILDERS) == set(_DB_LOAD_CAP_NOTES)


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


def test_build_hands_the_dev_target_preflight_a_store_for_a_billed_connector(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The dev-target preflight degrades to a note when it cannot open, and a
    billed connector will not open without a store for its cost gate. A caller
    that forgets one therefore loses the refusal that stops a doomed build, and
    loses it quietly, so the handoff is pinned rather than inferred."""

    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: bigquery\nbigquery:\n  project: dex-test\n  dev_dataset: dbt_dev\n",
        encoding="utf-8",
    )

    probed: list[str] = []
    stores: list[object] = []

    class _Probe:
        project = "dex-test"
        name = "bigquery"

        def missing_dev_namespaces(self, dataset):
            probed.append(dataset)
            return []

        def close(self):
            pass

    def opener(*, store=None, **_kwargs):
        # Stands in for connect._require_store, so a missing store fails the
        # test the same way it failed in production.
        if store is None:
            raise ValueError("opening 'bigquery' needs a store")
        stores.append(store)
        return _Probe()

    import exmergo_dex_core.connect as connect_mod

    monkeypatch.setattr(connect_mod, "open_adapter", opener)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    monkeypatch.setattr(build_module, "compile_estimate", lambda *a, **k: (1.0, {}, []))

    _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--target", "dev"], capsys
    )

    assert stores, "the dev-target preflight opened no connection"
    assert probed == ["dbt_dev"]


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


@pytest.mark.parametrize("connector", ["duckdb", "bigquery"])
def test_public_build_refuses_partial_installation_before_pricing(
    dbt_project_dir, monkeypatch, connector
):
    from exmergo_dex_core import DexEngine, MissingPackagesError
    from exmergo_dex_core.transform import commands, dev_target
    from exmergo_dex_core.transform.build import DependencyPolicy

    (dbt_project_dir / "packages.yml").write_text(
        "packages:\n  - package: a/installed\n  - package: a/missing\n"
    )
    (dbt_project_dir / "dbt_packages/installed").mkdir(parents=True)
    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])

    def forbidden(*a, **k):
        pytest.fail("partial installation reached pricing or a subprocess")

    module = importlib.import_module("exmergo_dex_core.transform.build")
    monkeypatch.setattr(module, "_default_runner", forbidden)
    monkeypatch.setattr(commands, "_price_build", forbidden)
    with (
        DexEngine.from_repo(dbt_project_dir.parent, connector=connector) as engine,
        pytest.raises(MissingPackagesError) as caught,
    ):
        engine.build(dependencies=DependencyPolicy.REFUSE)
    assert [p.name for p in caught.value.packages] == ["a/missing"]


# --- compile_estimate: pricing a build from a free dbt compile dry-run --------


@pytest.mark.parametrize("connector", ["duckdb", "bigquery"])
@pytest.mark.parametrize(
    "code,reason",
    [
        ("select forbidden_udf(1)", "unapproved_function"),
        ("CALL forbidden_proc()", "stored_procedure_or_call"),
    ],
)
def test_public_build_enforces_configured_functions_before_execution(
    dbt_project_dir, monkeypatch, connector, code, reason
):
    from types import SimpleNamespace

    from exmergo_dex_core import DexEngine
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.sql_guard import NotSelectOnlyError
    from exmergo_dex_core.transform import dev_target

    calls = _guard_compile_runner(monkeypatch, dbt_project_dir, code)
    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])
    adapter = SimpleNamespace(
        dialect=connector,
        paradigm=Paradigm.BYTES_SCANNED,
        query_estimate=lambda _: pytest.fail("refused SQL reached provider pricing"),
    )
    monkeypatch.setattr(DexEngine, "_adapter", lambda *a, **k: adapter)
    with DexEngine.from_repo(dbt_project_dir.parent, connector=connector) as engine:
        engine.config.guards.approved_functions = ["approved_udf"]
        with pytest.raises(NotSelectOnlyError) as caught:
            engine.build(select="guarded")
    assert caught.value.reason.value == reason
    assert "model.dex_test.guarded" in str(caught.value)
    assert calls == ["compile"]


def _guard_compile_runner(monkeypatch, project, code, *, mode="ok"):
    """Exercise the real guard with dbt artifacts produced by a controlled runner."""
    import subprocess

    module = importlib.import_module("exmergo_dex_core.transform.build")
    calls = []

    def factory(*args, **kwargs):
        def run(argv):
            command = argv[1]
            calls.append(command)
            if command == "compile":
                assert argv[argv.index("--select") + 1] == "guarded"
                if mode == "failed":
                    return subprocess.CompletedProcess(argv, 1, "", "")
                if mode != "absent":
                    target = project / "target"
                    target.mkdir(exist_ok=True)
                    uid = "model.dex_test.guarded"
                    (target / "run_results.json").write_text(
                        json.dumps(
                            {"results": [{"unique_id": uid, "status": "success"}]}
                        )
                    )
                    (target / "manifest.json").write_text(
                        json.dumps(
                            {
                                "nodes": {
                                    uid: {
                                        "name": "guarded",
                                        "resource_type": "model",
                                        "compiled_code": code,
                                        "config": {},
                                    }
                                }
                            }
                        )
                    )
            else:
                assert command == "build"
                assert not (project / "target/run_results.json").exists()
            return subprocess.CompletedProcess(argv, 0, "", "")

        return run

    monkeypatch.setattr(module, "_default_runner", factory)
    return calls


@pytest.mark.parametrize("approved", [[], ["APPROVED_UDF"]])
def test_public_local_build_allows_approved_functions_and_preserves_default(
    dbt_project_dir, monkeypatch, approved
):
    from exmergo_dex_core import DexEngine
    from exmergo_dex_core.transform import dev_target

    calls = _guard_compile_runner(
        monkeypatch, dbt_project_dir, "select approved_udf(abs(-1))"
    )
    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])
    with DexEngine.from_repo(dbt_project_dir.parent, connector="duckdb") as engine:
        engine.config.guards.approved_functions = approved
        result = engine.build(select="guarded")
    assert result.success
    assert calls == (["compile", "build"] if approved else ["build"])


@pytest.mark.parametrize(
    "mode,code",
    [
        ("failed", "select 1"),
        ("absent", "select 1"),
        ("ok", None),
    ],
)
def test_enabled_function_guard_refuses_unavailable_compile_evidence(
    dbt_project_dir, monkeypatch, mode, code
):
    from exmergo_dex_core import DexEngine
    from exmergo_dex_core.transform import dev_target

    module = importlib.import_module("exmergo_dex_core.transform.build")
    calls = _guard_compile_runner(monkeypatch, dbt_project_dir, code, mode=mode)
    monkeypatch.setattr(dev_target, "check", lambda *a, **k: [])
    with DexEngine.from_repo(dbt_project_dir.parent, connector="duckdb") as engine:
        engine.config.guards.approved_functions = ["approved_udf"]
        with pytest.raises(module.DbtRunError):
            engine.build(select="guarded")
    assert calls == ["compile"]


def _compile_runner(
    monkeypatch,
    project: Path,
    run_results: dict,
    *,
    returncode: int = 0,
    expected_env: dict[str, str] | None = None,
):
    """Fake ``_default_runner`` for compile: writes the given run_results.json on
    invocation (mirroring real dbt) and returns the requested code."""

    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def fake(timeout: float, cwd, env=None):
        if expected_env is not None:
            assert env == expected_env

        def run(argv: list[str]):
            (project / "target").mkdir(parents=True, exist_ok=True)
            (project / "target" / "run_results.json").write_text(
                json.dumps(run_results), encoding="utf-8"
            )
            return subprocess.CompletedProcess(argv, returncode, "", "")

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)


def test_compile_estimate_forwards_statement_caps_to_dbt_compile(
    dbt_project_dir: Path, monkeypatch
):
    """The pricing compile opens the dev connection, so it needs the same
    constrained settings as the eventual build."""

    build_mod = importlib.import_module("exmergo_dex_core.transform.build")
    env = {
        "DEX_CLICKHOUSE_MAX_EXECUTION_TIME": "60",
        "DEX_CLICKHOUSE_MAX_BYTES_TO_READ": str(60 * 200 * 1024 * 1024),
    }
    _compile_runner(
        monkeypatch,
        dbt_project_dir,
        {"results": []},
        expected_env=env,
    )
    total, per_node, notes = build_mod.compile_estimate(
        dbt_project_dir, _EstimatingAdapter(), target="dev", env=env
    )
    assert (total, per_node) == (0.0, {})
    assert notes == ["no scanning build nodes to price; the estimate is zero"]


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


def test_compile_estimate_sums_priced_nodes_and_skips_the_unbilled_ones(
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
        # Compiled by `dbt compile` and never built, so it has compiled SQL and
        # still issues no billed statement. Priced at zero on purpose, not by
        # accident of having no code to price.
        "analysis.p.scratch": {
            "resource_type": "analysis",
            "name": "scratch",
            "compiled_code": "select 5",
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
    # model(view) + snapshot + test priced at 10 each; ephemeral, seed and
    # analysis skipped.
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


def test_a_failed_billed_build_still_reports_what_it_burned(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """dbt bills for the statements it ran before it stopped.

    The failure envelope is built directly rather than through `to_envelope`,
    which is what normally lifts `spend` into `data`, so this is the path most
    likely to drop the number a caller needs to size the re-run.
    """

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
    msg = "Database Error in model mart_customers: Access Denied"
    run_results = json.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 7000},
                },
                {
                    "unique_id": "model.dex_test.mart_customers",
                    "status": "error",
                    "execution_time": 0.1,
                    "adapter_response": {},
                },
            ]
        }
    )
    _fake_runner_factory(
        monkeypatch,
        returncode=1,
        stdout=json.dumps({"info": {"level": "error", "msg": msg}}),
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            run_results,
        ),
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
    assert envelope["data"]["spend"]["bytes_billed"] == 7000
    lines = (tmp_path / ".dex" / "spend.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["billed_bytes"] == 7000


def test_a_billed_build_says_when_no_cumulative_cap_is_set(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The two-root field report was a `transform build` loop: seven builds, each
    individually legal because `--budget` was passed on every call, settled
    against a `session_ceiling` that the second root's config never declared."""

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
    _fake_runner_factory(monkeypatch, returncode=0)
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
    assert [w for w in envelope["warnings"] if "budget.session_ceiling" in w]


def test_a_billed_build_under_a_cumulative_cap_stays_quiet(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    from exmergo_dex_core.config import Budget, DexConfig, save_config
    from exmergo_dex_core.envelope import Paradigm

    save_config(
        DexConfig(connector="bigquery", budget=Budget(session_ceiling=1_000_000_000.0)),
        tmp_path,
    )
    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=5_000_000.0,
        per_node={"stg_customers": 5_000_000.0},
        confirmed=True,
        ceiling=100_000_000,
    )
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
            "--budget",
            "100000000",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert [w for w in envelope["warnings"] if "budget.session_ceiling" in w] == []


def test_a_running_build_holds_its_headroom_and_gives_it_back(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The case the reservation exists for, on the command where it matters most.

    A build is the longest billed command dex has, and it settles outside the
    cost gate entirely: dbt executes the statements, so nothing reaches the
    ledger until the run finishes. For as long as it runs, its headroom used to
    look free, and a concurrent `explore profile` was admitted against a daily
    budget the build had already committed.

    The probe fires from inside the fake dbt runner, which is exactly the window
    a real build is open for.
    """

    import json as json_mod

    from exmergo_dex_core.config import Budget, DexConfig
    from exmergo_dex_core.connect import new_cost_gate
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import OverCeilingError, utc_day_start
    from exmergo_dex_core.storage import FilesystemStore

    store = FilesystemStore(tmp_path)
    config = DexConfig(
        connector="bigquery",
        budget=Budget(ceiling=100_000_000.0, session_ceiling=8_000_000.0),
    )
    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=5_000_000.0,
        per_node={"stg_customers": 5_000_000.0},
        confirmed=True,
        ceiling=100_000_000,
        store=store,
        session_ceiling=8_000_000.0,
    )

    refused: list[bool] = []

    def probe_while_dbt_runs():
        # A second command arriving mid-build. 5 MB is held of an 8 MB day, so a
        # 5 MB scan no longer fits, and before reservations it would have.
        concurrent = new_cost_gate(
            "bigquery", config, store, confirmed=True, command="explore profile"
        )
        try:
            concurrent.preflight_command(5_000_000.0)
        except OverCeilingError:
            refused.append(True)
        finally:
            concurrent.settle()

    target_dir = bigquery_project_dir / "target"
    run_results = json_mod.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 3_000_000},
                }
            ]
        }
    )
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    import subprocess as _subprocess

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            probe_while_dbt_runs()
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "run_results.json").write_text(run_results, encoding="utf-8")
            return _subprocess.CompletedProcess(args=argv, returncode=0, stdout="")

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)

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
    assert refused == [True], "a concurrent command was admitted against held headroom"

    # ...and the hold is given back once dbt returns, so the day settles to what
    # the build really cost rather than to what it was quoted.
    assert (
        store.spend_since(utc_day_start(), field="billed_bytes", connector="bigquery")
        == 3_000_000
    )
    assert envelope["data"]["spend"]["session_spent_today"] == 3_000_000


# --- node identity: which test warned (the field report behind --verify) ------


def _node_results(*entries: dict) -> str:
    return json.dumps({"results": list(entries)})


def test_a_warning_test_node_is_named_rather_than_hashed(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """A green build with warnings has to say which nodes warned.

    dbt's unique id for a generic test ends in a content hash, so taking the
    last segment named every test in the envelope something like `3249b83c15`.
    A caller with seventeen warnings and a suspicion that one of them is new
    had no way to tell which seventeen they were.
    """

    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            dbt_project_dir / "target" / "run_results.json",
            _node_results(
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "success",
                    "execution_time": 1.0,
                },
                {
                    "unique_id": ("test.dex_test.not_null_stg_customers_id.3249b83c15"),
                    "status": "warn",
                    "execution_time": 0.1,
                },
            ),
        ),
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--confirm"],
        capsys,
    )
    assert rc == 0, envelope
    by_status = {n["status"]: n for n in envelope["data"]["nodes"]}
    assert by_status["warn"]["name"] == "not_null_stg_customers_id"
    assert (
        by_status["warn"]["unique_id"]
        == "test.dex_test.not_null_stg_customers_id.3249b83c15"
    )
    # A model's id has no trailing hash, so its name is untouched.
    assert by_status["success"]["name"] == "stg_customers"


# --- --verify: the sweep folded onto the build --------------------------------


def test_a_build_does_not_verify_unless_asked(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _fake_runner_factory(monkeypatch, returncode=0)
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--confirm"],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["verification"] == {
        "ran": False,
        "reason": "not requested; re-run with --verify to sweep",
    }


def test_a_verified_build_reports_findings_and_stays_ok(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Findings never turn a build dbt completed into a failure.

    They ride `data.verification`, the status stays `ok`, `errors` stays empty,
    and a pointer line in `warnings` says they are there so a caller scanning
    warnings alone still learns about them.
    """

    target = dbt_project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "test.dex_test.relationships_orders.abc123": {
                        "name": "relationships_orders",
                        "resource_type": "test",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            target / "run_results.json",
            _node_results(
                {
                    "unique_id": "test.dex_test.relationships_orders.abc123",
                    "status": "warn",
                    "message": "got 12 results, configured to warn if != 0",
                    "execution_time": 0.1,
                }
            ),
        ),
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--confirm", "--verify"],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert envelope["errors"] == []
    verification = envelope["data"]["verification"]
    assert verification["ran"] is True
    assert verification["finding_count"] == 1
    finding = verification["findings"][0]
    assert finding["code"] == "node_warned"
    assert finding["identifier"] == "relationships_orders"
    assert any("data.verification.findings" in w for w in envelope["warnings"])


def test_a_failed_verified_build_names_the_failed_and_skipped_nodes(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The error envelope is built by hand rather than through `to_envelope`,
    so the payload has to reach it separately. A half-built project is also
    when "which node failed, and what did it take down with it" is worth most.
    """

    target = dbt_project_dir / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.dex_test.stg_customers": {
                        "name": "stg_customers",
                        "resource_type": "model",
                    },
                    "model.dex_test.mart_customers": {
                        "name": "mart_customers",
                        "resource_type": "model",
                        "depends_on": {"nodes": ["model.dex_test.stg_customers"]},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    _fake_runner_factory(
        monkeypatch,
        returncode=1,
        stdout=json.dumps({"info": {"level": "error", "msg": "Database Error"}}),
        run_results_json=(
            target / "run_results.json",
            _node_results(
                {
                    "unique_id": "model.dex_test.stg_customers",
                    "status": "error",
                    "message": "relation raw.customers does not exist",
                    "execution_time": 0.2,
                },
                {
                    "unique_id": "model.dex_test.mart_customers",
                    "status": "skipped",
                    "execution_time": 0.0,
                },
            ),
        ),
    )
    rc, envelope = _run(
        ["--repo-root", str(tmp_path), "transform", "build", "--confirm", "--verify"],
        capsys,
    )
    assert rc == 1
    assert envelope["status"] == "error"
    verification = envelope["data"]["verification"]
    codes = {f["code"]: f for f in verification["findings"]}
    assert set(codes) == {"node_failed", "node_skipped"}
    assert codes["node_skipped"]["data"]["caused_by"] == "stg_customers"
    # A half-built dev target is a mix of this run's output and the last one's,
    # so its row counts are not judged and the envelope says why.
    assert "did not complete" in verification["suppressed"]["row_population"]


@dataclass
class _Meta:
    """One catalog row, as `list_objects` returns it."""

    identifier: str
    row_count: int | None = None


def _verifiable_manifest(project: Path, *, materialized: str) -> None:
    """A one-model project whose driving parent is a source relation.

    The model loses every row to its join, which is the shape `row_loss` exists
    for; ``materialized`` decides whether the warehouse keeps a row count for it
    and therefore whether the sweep has anything to price.

    Writes the run results a `dbt compile` leaves behind as well as the
    manifest, because that pair is what pricing reads and the selection in it is
    what scopes the sweep. A build that follows overwrites them with its own.
    """

    target = project / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "model.dex_test.fct_orders", "status": "success"}
                ]
            }
        ),
        encoding="utf-8",
    )
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "sources": {
                    "source.dex_test.raw.orders": {
                        "name": "orders",
                        "relation_name": "`dex-test`.`raw`.`orders`",
                    }
                },
                "nodes": {
                    "model.dex_test.fct_orders": {
                        "name": "fct_orders",
                        "resource_type": "model",
                        "relation_name": "`dex-test`.`dbt_dev`.`fct_orders`",
                        "config": {"materialized": materialized},
                        "compiled_code": (
                            "select o.id from `dex-test`.`raw`.`orders` o "
                            "join `dex-test`.`raw`.`events` e on o.id = e.order_id"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _built_run_results() -> str:
    return json.dumps(
        {
            "results": [
                {
                    "unique_id": "model.dex_test.fct_orders",
                    "status": "success",
                    "execution_time": 1.0,
                    "adapter_response": {"bytes_billed": 3000},
                }
            ]
        }
    )


def test_a_metered_build_does_not_verify_or_connect_unless_asked(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The safety property: no scan a caller did not ask for."""

    adapter = _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=1000.0,
        per_node={"fct_orders": 1000.0},
        confirmed=True,
        ceiling=100_000.0,
        query_estimate=50_000.0,
    )
    _verifiable_manifest(bigquery_project_dir, materialized="view")
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            _built_run_results(),
        ),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--confirm",
            "--budget",
            "100000",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["verification"]["ran"] is False
    assert adapter.queries == [], "a build that was not asked to verify scanned"
    assert adapter.estimated == [], "a build that was not asked to verify priced a scan"


def test_the_upfront_estimate_covers_the_build_and_its_verification(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """One estimate, one budget, one handshake.

    The row counts are priced during the build's own pricing pass, off the
    manifest the compile wrote, so an unconfirmed `--verify` returns a single
    number that covers both phases instead of agreeing to a build and then
    being asked again about the counts that judge it.
    """

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=1000.0,
        per_node={"fct_orders": 1000.0},
        confirmed=False,
        ceiling=None,
        query_estimate=50_000.0,
    )
    _verifiable_manifest(bigquery_project_dir, materialized="view")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--verify",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "needs_confirmation"
    assert envelope["data"]["estimated_bytes"] == 51_000.0
    assert envelope["data"]["per_table_bytes"]["(row counts)"] == 50_000.0
    assert envelope["data"]["per_table_bytes"]["fct_orders"] == 1000.0


def test_a_table_materialization_adds_nothing_to_the_estimate(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """A warehouse keeps a row count for a table, so nothing has to be scanned
    to judge it. A view is the case that costs, and it is dbt's default."""

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=1000.0,
        per_node={"fct_orders": 1000.0},
        confirmed=False,
        ceiling=None,
        query_estimate=50_000.0,
    )
    _verifiable_manifest(bigquery_project_dir, materialized="table")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--verify",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["data"]["estimated_bytes"] == 1000.0
    assert "(row counts)" not in envelope["data"]["per_table_bytes"]


def test_a_verified_metered_build_reports_the_count_spend_beside_the_build(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """`data.spend` is what this command billed, and the counts are part of it.

    dbt bills outside the gate and the counts bill through it, so reporting
    dbt's figure alone would under-report the command. Under-reporting is the
    one direction a cost guard must never round.
    """

    adapter = _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=1000.0,
        per_node={"fct_orders": 1000.0},
        confirmed=True,
        ceiling=1_000_000.0,
        query_estimate=50_000.0,
        objects=[
            _Meta("dex-test.raw.orders", 1000),
            _Meta("dex-test.dbt_dev.fct_orders", None),
        ],
        counts=[0],
    )
    _verifiable_manifest(bigquery_project_dir, materialized="view")
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            _built_run_results(),
        ),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--verify",
            "--confirm",
            "--budget",
            "1000000",
        ],
        capsys,
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert len(adapter.queries) == 1, "the counts are one batched statement"
    # 3000 from dbt, 50000 from the count statement dex issued through the gate.
    assert envelope["data"]["spend"]["bytes_billed"] == 53_000.0
    verification = envelope["data"]["verification"]
    assert verification["ran"] is True
    assert verification["scope"] == ["fct_orders"]
    assert [f["code"] for f in verification["findings"]] == ["row_loss"]


def test_a_verified_build_whose_dev_target_is_out_of_scope_says_so(
    bigquery_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """The metered default, before the dev-namespace fold takes effect: dbt
    writes to a dataset that is refused as a source, so a sweep that just
    compared what it could see would report nothing and look clean."""

    _install_fake_pricing(
        monkeypatch,
        connector="bigquery",
        paradigm=Paradigm.BYTES_SCANNED,
        estimate=1000.0,
        per_node={"fct_orders": 1000.0},
        confirmed=True,
        ceiling=1_000_000.0,
        query_estimate=0.0,
        objects=[_Meta("dex-test.raw.orders", 1000)],
    )
    _verifiable_manifest(bigquery_project_dir, materialized="table")
    _fake_runner_factory(
        monkeypatch,
        returncode=0,
        run_results_json=(
            bigquery_project_dir / "target" / "run_results.json",
            _built_run_results(),
        ),
    )
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "--connector",
            "bigquery",
            "transform",
            "build",
            "--verify",
            "--confirm",
            "--budget",
            "1000000",
        ],
        capsys,
    )
    assert rc == 0, envelope
    suppressed = envelope["data"]["verification"]["suppressed"]
    assert "outside dex's read scope" in suppressed["row_population"]
