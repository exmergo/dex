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
    monkeypatch, *, returncode: int, stdout: str = "", stderr: str = ""
):
    """Replace _default_runner with a recorder returning a canned dbt result."""

    import subprocess

    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[dict] = []

    def fake(timeout: float, cwd):
        def run(argv: list[str]):
            calls.append({"argv": argv, "cwd": cwd})
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
