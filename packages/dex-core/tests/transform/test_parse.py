"""Shadow `dbt parse` at semantic plan time: a plan that cannot parse is never
stored, and nothing dbt does during the parse touches the real project."""

from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

_SEMANTIC_YAML = """\
semantic_models:
  - name: customers
    description: Customer grain over stg_customers.
    model: ref('stg_customers')
    defaults:
      agg_time_dimension: signup_date
    entities:
      - name: customer
        type: primary
        expr: id
    dimensions:
      - name: signup_date
        type: time
        expr: cast('2020-01-01' as date)
        type_params:
          time_granularity: day
    measures:
      - name: customer_count
        agg: count
        expr: id

metrics:
  - name: customer_count
    label: Customer count
    type: simple
    type_params:
      measure: customer_count
"""


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    return rc, json.loads(out)


def _payload_file(tmp_path: Path, content: str) -> Path:
    payload = tmp_path / "semantic.json"
    payload.write_text(
        json.dumps(
            {"edits": [{"path": "models/semantic/customers.yml", "content": content}]}
        ),
        encoding="utf-8",
    )
    return payload


def _add_time_spine(project: Path) -> None:
    (project / "models" / "metricflow_time_spine.sql").write_text(
        "select cast(range as date) as date_day\n"
        "from range(date '2020-01-01', date '2030-01-01', interval 1 day)\n",
        encoding="utf-8",
    )
    (project / "models" / "metricflow_time_spine.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: metricflow_time_spine\n"
        "    time_spine:\n"
        "      standard_granularity_column: date_day\n"
        "    columns:\n"
        "      - name: date_day\n"
        "        granularity: day\n",
        encoding="utf-8",
    )


def _tree_snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _define(tmp_path: Path, payload: Path, capsys, *extra: str) -> tuple[int, dict]:
    return _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "define",
            "customer semantics",
            "--edits-file",
            str(payload),
            *extra,
        ],
        capsys,
    )


def _plans_on_disk(tmp_path: Path) -> list[Path]:
    plans_dir = tmp_path / ".dex" / "plans"
    return sorted(plans_dir.glob("*.json")) if plans_dir.is_dir() else []


def test_parse_failure_blocks_the_plan_and_stores_nothing(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    line = json.dumps(
        {
            "info": {
                "level": "error",
                "msg": "Parsing Error: The metric 'dnf_count' does not exist",
            }
        }
    )

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=line, stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys)
    assert rc == 1
    assert envelope["errors"][0] == (
        "dbt parse failed: Parsing Error: The metric 'dnf_count' does not exist"
    )
    assert _plans_on_disk(tmp_path) == []


def test_shadow_parse_never_touches_the_real_project(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    before = _tree_snapshot(dbt_project_dir)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    calls: list[dict] = []

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            calls.append({"argv": argv, "cwd": Path(cwd)})
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys)
    assert rc == 0, envelope
    assert len(calls) == 1
    argv = calls[0]["argv"]
    shadow_dir = Path(argv[argv.index("--project-dir") + 1])
    assert shadow_dir != dbt_project_dir
    assert dbt_project_dir not in [shadow_dir, *shadow_dir.parents]
    assert calls[0]["cwd"] == shadow_dir
    assert _tree_snapshot(dbt_project_dir) == before


def test_parse_skipped_with_warning_when_dbt_is_missing(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def missing():
        raise build_module.DbtRunError("dbt executable not found")

    monkeypatch.setattr(build_module, "_dbt_executable", missing)
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert any("dbt is not installed" in w for w in envelope["warnings"])
    assert len(_plans_on_disk(tmp_path)) == 1


def test_no_parse_flag_skips_the_shadow_parse(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _add_time_spine(dbt_project_dir)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def exploded(timeout: float, cwd):
        def run(argv: list[str]):
            raise AssertionError(f"--no-parse must not spawn dbt: {argv}")

        return run

    monkeypatch.setattr(build_module, "_default_runner", exploded)
    rc, envelope = _define(
        tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys, "--no-parse"
    )
    assert rc == 0, envelope
    assert envelope["status"] == "ok"


def test_parse_skipped_while_the_project_lacks_a_time_spine(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    # dbt would refuse to parse for a reason the plan already warns about
    # loudly, so the gate defers instead of hard-failing every first define.
    build_module = importlib.import_module("exmergo_dex_core.transform.build")

    def exploded(timeout: float, cwd):
        def run(argv: list[str]):
            raise AssertionError("parse must be skipped without a time spine")

        return run

    monkeypatch.setattr(build_module, "_default_runner", exploded)
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys)
    assert rc == 0, envelope
    assert any("time spine" in w for w in envelope["warnings"])
    assert any("dbt parse skipped" in w for w in envelope["warnings"])


def test_parse_success_still_surfaces_deprecation_warnings(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    """Regression for #55: a clean parse (returncode 0) with a deprecation
    notice on stdout must not report warnings: [] just because nothing failed
    -- the same YAML would go on to warn at `transform build` (#50)."""

    _add_time_spine(dbt_project_dir)
    build_module = importlib.import_module("exmergo_dex_core.transform.build")
    line = json.dumps(
        {
            "info": {
                "level": "warn",
                "name": "PropertyMovedToConfigDeprecation",
                "msg": "[WARNING][PropertyMovedToConfigDeprecation]: Deprecated "
                "functionality",
            }
        }
    )

    def fake(timeout: float, cwd, env=None):
        def run(argv: list[str]):
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=line, stderr=""
            )

        return run

    monkeypatch.setattr(build_module, "_default_runner", fake)
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert len(_plans_on_disk(tmp_path)) == 1
    assert any("PropertyMovedToConfigDeprecation" in w for w in envelope["warnings"])


def test_real_dbt_parse_catches_a_dangling_model_ref(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("dbt.cli.main")
    _add_time_spine(dbt_project_dir)
    dangling = _SEMANTIC_YAML.replace(
        "ref('stg_customers')", "ref('nonexistent_model')"
    )
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, dangling), capsys)
    assert rc == 1
    assert envelope["errors"][0].startswith("dbt parse failed: ")
    assert _plans_on_disk(tmp_path) == []


def test_real_dbt_parse_accepts_a_valid_semantic_plan(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    pytest.importorskip("dbt.cli.main")
    _add_time_spine(dbt_project_dir)
    rc, envelope = _define(tmp_path, _payload_file(tmp_path, _SEMANTIC_YAML), capsys)
    assert rc == 0, envelope
    assert envelope["status"] == "ok"
    assert len(_plans_on_disk(tmp_path)) == 1
    # The shadow parse left the project exactly as it was: the plan is the only
    # artifact.
    assert not (dbt_project_dir / "models" / "semantic").exists()
