"""`maintain coverage` through a real dbt parse on DuckDB (#1280).

One fixture project carries every case the issue names: generic, singular and
unit tests, a test on an ephemeral model, a disabled test, `severity: warn`, a
package model with tests, a disabled model, incremental models with and without
an `is_incremental` override, source freshness, and tests on a snapshot and a
seed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.engine import DexEngine

pytest.importorskip("dbt.cli.main")
pytest.importorskip("dbt.adapters.duckdb")

_FILES = {
    "dbt_project.yml": "name: cov\nversion: '1.0'\nprofile: cov\n",
    "packages.yml": "packages:\n  - local: helpers\n",
    "helpers/dbt_project.yml": "name: helpers\nversion: '1.0'\n",
    "helpers/models/helper.sql": "select 1 as x\n",
    "helpers/models/helper.yml": (
        "models:\n  - name: helper\n    columns:\n"
        "      - name: x\n        data_tests: [not_null]\n"
    ),
    "seeds/countries.csv": "code,name\nIT,Italy\n",
    "seeds/seeds.yml": (
        "seeds:\n  - name: countries\n    columns:\n"
        "      - name: code\n        data_tests: [unique]\n"
    ),
    "models/staging/sources.yml": """\
sources:
  - name: raw
    schema: main
    loaded_at_field: loaded_at
    freshness:
      warn_after: {count: 12, period: hour}
    tables:
      - name: orders
        columns:
          - name: id
            data_tests: [not_null]
      - name: customers
        freshness: null
""",
    "models/staging/stg_orders.sql": (
        "{{ config(materialized='ephemeral') }}\n"
        "select id as order_id, customer_id, status, amount, loaded_at "
        "from {{ source('raw', 'orders') }}\n"
    ),
    "models/staging/stg_customers.sql": (
        "select id as customer_id from {{ source('raw', 'customers') }}\n"
    ),
    "models/staging/old_model.sql": "{{ config(enabled=false) }}\nselect 1 as x\n",
    "models/marts/fct_orders.sql": """\
{{ config(materialized='incremental', unique_key='order_id') }}
select o.* from {{ ref('stg_orders') }} o
join {{ ref('stg_customers') }} c using (customer_id)
{% if is_incremental() %}
where loaded_at > (select max(loaded_at) from {{ this }})
{% endif %}
""",
    "models/marts/fct_daily.sql": (
        "{{ config(materialized='incremental') }}\n"
        "select loaded_at::date as day, count(*) as n "
        "from {{ ref('fct_orders') }} group by 1\n"
    ),
    "models/marts/schema.yml": """\
models:
  - name: fct_orders
    columns:
      - name: order_id
        data_tests:
          - unique
          - not_null:
              config: {severity: warn}
      - name: status
        data_tests:
          - accepted_values:
              arguments: {values: ['placed', 'shipped']}
      - name: amount
      - name: customer_id
        data_tests:
          - not_null:
              config: {enabled: false}
  - name: fct_daily
    columns:
      - name: day
  - name: stg_orders
    columns:
      - name: order_id
        data_tests: [not_null]
unit_tests:
  - name: ut_orders_full
    model: fct_orders
    overrides: {macros: {is_incremental: false}}
    given:
      - input: ref('stg_orders')
        rows: [{order_id: 1, customer_id: 1}]
      - input: ref('stg_customers')
        rows: [{customer_id: 1}]
    expect: {rows: [{order_id: 1}]}
  - name: ut_orders_incr
    model: fct_orders
    overrides: {macros: {is_incremental: true}}
    given:
      - input: ref('stg_orders')
        rows: [{order_id: 1, customer_id: 1}]
      - input: ref('stg_customers')
        rows: [{customer_id: 1}]
      - input: this
        rows: [{order_id: 0}]
    expect: {rows: [{order_id: 1}]}
  - name: ut_daily_full
    model: fct_daily
    overrides: {macros: {is_incremental: false}}
    given:
      - input: ref('fct_orders')
        rows: [{loaded_at: '2024-01-01'}]
    expect: {rows: [{day: '2024-01-01', n: 1}]}
""",
    "tests/assert_positive_amount.sql": (
        "select * from {{ ref('fct_orders') }} where amount < 0\n"
    ),
    "snapshots/snap_customers.yml": """\
snapshots:
  - name: snap_customers
    relation: ref('stg_customers')
    config: {unique_key: customer_id, strategy: check, check_cols: all}
    columns:
      - name: customer_id
        data_tests: [not_null]
""",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "analytics"
    for path, content in _FILES.items():
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (project / "profiles.yml").write_text(
        "cov:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'dev.duckdb'}\n",
        encoding="utf-8",
    )
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"duckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        "dbt_project_dir: analytics\n",
        encoding="utf-8",
    )
    from dbt.cli.main import dbtRunner

    installed = dbtRunner().invoke(
        ["deps", "--project-dir", str(project), "--profiles-dir", str(project)]
    )
    assert installed.success
    return tmp_path


def _run(repo: Path, capsys, *argv: str) -> dict:
    capsys.readouterr()  # whatever an in-process dbt printed first
    main(["--repo-root", str(repo), "maintain", "coverage", *argv])
    return json.loads(capsys.readouterr().out)


def _named(data: dict, section: str, name: str) -> dict:
    return next(r for r in data[section] if r["name"] == name)


def test_the_fixture_project_inventories_every_case(repo: Path, capsys):
    envelope = _run(repo, capsys)
    assert envelope["status"] == "ok", envelope
    data = envelope["data"]
    assert next(iter(data)) == "summary"
    assert data["manifest"] == {"source": "parsed"}
    assert data["suppressed"] == {}

    summary = data["summary"]
    assert summary["models"]["count"] == 4
    assert summary["models"]["ephemeral"] == 1
    assert summary["models"]["incremental_branch_tested"] == {
        "numerator": 1,
        "denominator": 2,
        "of": "enabled incremental project models",
    }
    assert summary["sources"]["with_freshness"]["numerator"] == 1
    assert summary["tests"]["by_kind"] == {
        "generic": 7,
        "singular": 1,
        "unit": 3,
        "freshness": 1,
    }
    assert summary["tests"]["by_severity"] == {"error": 7, "warn": 1}

    orders = _named(data, "models", "fct_orders")
    assert orders["own_tests"] == {"generic": 3, "singular": 1, "unit": 2}
    assert orders["severity"] == {"error": 3, "warn": 1}
    assert orders["incremental"]["incremental_branch_tests"] == ["ut_orders_incr"]
    # The disabled not_null on customer_id asserts nothing.
    assert orders["columns"]["unasserted"] == ["amount", "customer_id"]
    daily = _named(data, "models", "fct_daily")
    assert daily["incremental"]["incremental_branch_tested"] is False
    assert _named(data, "models", "stg_orders")["ephemeral"] is True
    customers = _named(data, "models", "stg_customers")
    assert customers["tested"] is False
    assert customers["downstream_tests"]["count"] > 0
    assert _named(data, "sources", "raw.orders")["own_tests"]["freshness"] == 1
    assert _named(data, "snapshots", "snap_customers")["tested"] is True
    assert _named(data, "seeds", "countries")["tested"] is True

    excluded = {(e["name"], e["reason"]) for e in data["excluded"]["resources"]}
    assert ("helper", "package") in excluded
    assert ("old_model", "disabled") in excluded
    assert data["excluded"]["tests"]["disabled"] == 1
    assert data["excluded"]["tests"]["package"] == 1

    # No accepted_values argument reaches the payload.
    assert "placed" not in json.dumps(envelope)
    # Nothing was written into the project.
    assert not (repo / "analytics" / "target").exists()


def test_the_engine_and_the_cli_return_the_same_payload(repo: Path, capsys):
    cli = _run(repo, capsys, "fct_orders,raw.orders")["data"]
    with DexEngine.from_repo(str(repo)) as engine:
        result = engine.coverage(["fct_orders", "raw.orders"])
    assert result.data() == {k: v for k, v in cli.items() if k != "notes"}
    assert cli["scope"] == {"selected": ["fct_orders", "raw.orders"]}


def test_values_free_mode_runs_the_command(repo: Path, capsys):
    envelope = _run(repo, capsys, "--values-free")
    assert envelope["status"] == "ok", envelope
    assert envelope["data"]["result_mode"] == "values_free"
    assert "placed" not in json.dumps(envelope)


def test_a_project_that_does_not_compile_is_reported_first(repo: Path, capsys):
    (repo / "analytics" / "models" / "marts" / "broken.sql").write_text(
        "select * from {{ ref('no_such_model') }}\n", encoding="utf-8"
    )
    envelope = _run(repo, capsys)
    data = envelope["data"]
    assert [f["code"] for f in data["findings"]] == ["project_does_not_compile"]
    assert data["suppressed"] == {"inventory": "the project does not compile"}
    assert "summary" not in data
    assert "models" not in data


def test_without_a_parse_the_newest_manifest_on_disk_is_read_and_dated(
    repo: Path, capsys, monkeypatch
):
    from dbt.cli.main import dbtRunner

    project = repo / "analytics"
    assert (
        dbtRunner()
        .invoke(
            ["parse", "--project-dir", str(project), "--profiles-dir", str(project)]
        )
        .success
    )
    import importlib

    # The package exports a `build` function under the module's name.
    build_mod = importlib.import_module("exmergo_dex_core.transform.build")

    monkeypatch.setattr(
        build_mod,
        "shadow_parse",
        lambda *a, **k: {
            "available": False,
            "reason": "dbt is not installed",
            "success": None,
            "messages": [],
        },
    )
    envelope = _run(repo, capsys)
    data = envelope["data"]
    assert data["manifest"]["source"] == "artifact"
    assert data["manifest"]["generated_at"]
    assert any("not a fresh parse" in w for w in envelope["warnings"])
    assert data["summary"]["models"]["count"] == 4

    (project / "target" / "manifest.json").unlink()
    envelope = _run(repo, capsys)
    assert "inventory" in envelope["data"]["suppressed"]
    assert "summary" not in envelope["data"]


def test_an_uninstalled_package_skips_the_parse_and_names_transform_deps(
    repo: Path, capsys
):
    import shutil

    shutil.rmtree(repo / "analytics" / "dbt_packages")
    envelope = _run(repo, capsys)
    assert envelope["status"] == "ok"
    assert "transform deps" in envelope["data"]["suppressed"]["inventory"]


def test_values_free_mode_withholds_the_parse_error(repo: Path, capsys):
    (repo / "analytics" / "models" / "marts" / "schema.yml").write_text(
        "models:\n  - name: fct_orders\n    columns: [zq7x_value\n", encoding="utf-8"
    )
    envelope = _run(repo, capsys, "--values-free")
    data = envelope["data"]
    assert [f["code"] for f in data["findings"]] == ["project_does_not_compile"]
    assert data["findings"][0]["data"] == {}
    assert "zq7x" not in json.dumps(envelope)


def test_a_values_free_engine_stamps_its_mode_on_the_result(repo: Path):
    from exmergo_dex_core.results import to_envelope

    with DexEngine.from_repo(str(repo), result_mode="values_free") as engine:
        result = engine.coverage()
    assert to_envelope(result).data["result_mode"] == "values_free"


def test_a_missing_git_package_is_named_without_its_credentials(repo: Path, capsys):
    project = repo / "analytics"
    (project / "packages.yml").write_text(
        "packages:\n  - local: helpers\n"
        "  - git: https://s3cr3t-token@github.com/org/private-pkg.git\n"
        "    revision: main\n",
        encoding="utf-8",
    )
    # With nothing installed, every declaration is missing, the git one too
    # (dex cannot tell a git package's install name otherwise).
    import shutil

    shutil.rmtree(project / "dbt_packages")
    envelope = _run(repo, capsys)
    assert "github.com/org/private-pkg" in json.dumps(envelope)
    assert "s3cr3t" not in json.dumps(envelope)
    assert "transform deps" in json.dumps(envelope)
