"""Control flow is validated as dbt's SQL, with compile-time SQL governed (#641)."""

from __future__ import annotations

import hashlib
import json
import shutil
from types import SimpleNamespace

import pytest
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.envelope import Paradigm
from exmergo_dex_core.guards.cost_guard import ConfirmationRequiredError, CostGate
from exmergo_dex_core.transform.compile import CompileQueries
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import (
    CompiledModel,
    EditValidationError,
    needs_compilation,
    validate_edit,
)

LOOP = """{% set ids = ['a', 'b'] %}
with t as (select 1 as x)
{% for i in ids %}
select '{{ i }}' as id, count(*) as n from t
{% if not loop.last %}union all{% endif %}
{% endfor %}
"""


def _run(root, capsys, *args):
    rc = main(["--repo-root", str(root), *args])
    output = capsys.readouterr().out
    assert output.count("\n") == 1
    return rc, json.loads(output)


@pytest.fixture
def project(dbt_project_dir, duckdb_file, tmp_path):
    pytest.importorskip("dbt")
    profiles = yaml.safe_load((dbt_project_dir / "profiles.yml").read_text())
    profiles["dex_test"]["outputs"]["dev"]["path"] = str(duckdb_file)
    (dbt_project_dir / "profiles.yml").write_text(yaml.safe_dump(profiles))
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        yaml.safe_dump(
            {
                "connector": "duckdb",
                "duckdb": {"path": str(duckdb_file)},
            }
        )
    )
    return dbt_project_dir


def _plan(root, capsys, sql, *, extra=()):
    payload = root / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {"path": "models/loop.sql", "kind": "model_sql", "content": sql},
                    *extra,
                ]
            }
        )
    )
    return _run(
        root, capsys, "transform", "plan", "control flow", "--edits-file", str(payload)
    )


@pytest.mark.parametrize(
    "sql",
    [
        LOOP,
        "{%- for i in [1, 2] -%}select {{ i }} as id"
        "{% if not loop.last %} union all {% endif %}{%- endfor -%}",
        "{% if var('enabled', true) %}select 1 as id"
        "{% else %}select 2 as id{% endif %}",
        "{% for i in [1, 2] %}{% for j in [1, 2] %}select {{ i + j }} as id"
        "{% if not loop.last %} union all {% endif %}{% endfor %}"
        "{% if not loop.last %} union all {% endif %}{% endfor %}",
    ],
)
def test_dbt_compiles_control_flow_without_touching_project(
    project, tmp_path, capsys, sql
):
    before = {
        p.relative_to(project): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file()
    }
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 0, out
    assert out["status"] == "ok"
    assert {
        p.relative_to(project): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file()
    } == before
    stored = json.loads(next((tmp_path / ".dex" / "plans").glob("*.json")).read_text())
    assert stored["edits"][0]["new_content"] == sql


def test_all_edits_and_ephemeral_dependencies_are_compiled(project, tmp_path, capsys):
    sql = "{% if true %}select * from {{ ref('new_parent') }}{% endif %}"
    rc, out = _plan(
        tmp_path,
        capsys,
        sql,
        extra=[
            {
                "path": "models/new_parent.sql",
                "kind": "model_sql",
                "content": "{{ config(materialized='ephemeral') }}\nselect 7 as id",
            }
        ],
    )
    assert rc == 0, out


@pytest.mark.parametrize(
    "body",
    [
        "delete from customers",
        "drop table customers",
        "select 1; select 2",
        "select from",
        "",
    ],
)
def test_compiled_invalid_sql_stores_no_plan(project, tmp_path, capsys, body):
    rc, out = _plan(tmp_path, capsys, "{% if true %}" + body + "{% endif %}")
    assert rc == 1, out
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


def test_compile_time_query_runs_through_dex(project, tmp_path, capsys):
    _run(tmp_path, capsys, "explore", "map")
    sql = """{% if execute %}
{% set rows = run_query('select count(*) as n from customers') %}
select {{ rows.rows[0][0] | int }} as n
{% else %} select 0 as n {% endif %}
"""
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 0, out
    query_log = "\n".join(p.read_text() for p in (tmp_path / ".dex").rglob("*.jsonl"))
    assert "count(*)" in query_log.lower()


@pytest.mark.parametrize(
    "statement", ["delete from customers", "select email from customers"]
)
def test_compile_time_unsafe_query_is_refused(project, tmp_path, capsys, statement):
    _run(tmp_path, capsys, "explore", "map")
    sql = (
        "{% if execute %}{% do run_query("
        + repr(statement)
        + ") %}{% endif %} select 1 as id"
    )
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 1, out
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


def test_unsupported_adapter_introspection_is_refused(project, tmp_path, capsys):
    sql = (
        "{% if execute %}{% set r = adapter.get_columns_in_relation("
        "api.Relation.create(database='warehouse', schema='main', "
        "identifier='customers')) %}{% endif %} select 1 as id"
    )
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 1, out
    assert out["errors"]
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


def test_low_level_validation_requires_current_compilation():
    edit = PlanEdit(path="models/loop.sql", kind=EditKind.MODEL_SQL, new_content=LOOP)
    with pytest.raises(EditValidationError, match="requires dbt compilation"):
        validate_edit(edit)
    evidence = CompiledModel(
        edit.path,
        hashlib.sha256(LOOP.encode()).hexdigest(),
        "select 1",
        "dev",
        "duckdb",
    )
    assert validate_edit(edit, compiled=evidence) == []
    edit.new_content += "-- changed"
    with pytest.raises(EditValidationError, match="stale"):
        validate_edit(edit, compiled=evidence)


def test_jinja_comments_do_not_require_compilation():
    assert not needs_compilation("{# {% if true %} ignored {% endif %} #} select 1")


def test_compiled_file_reads_keep_the_existing_warning():
    edit = PlanEdit(path="models/loop.sql", kind=EditKind.MODEL_SQL, new_content=LOOP)
    evidence = CompiledModel(
        edit.path,
        hashlib.sha256(LOOP.encode()).hexdigest(),
        "select * from read_csv('data.csv')",
        "dev",
        "duckdb",
    )
    assert "reads a file" in validate_edit(edit, compiled=evidence)[0]


@pytest.mark.parametrize("confirmed", [False, True])
def test_compile_queries_share_a_ceiling_and_reservation(confirmed):
    entries = []
    gate = CostGate(
        paradigm=Paradigm.BYTES_SCANNED,
        ceiling=100,
        session_ceiling=1000,
        session_spent=0,
        confirmed=confirmed,
        connector="bigquery",
        command="transform plan",
        record=entries.append,
    )
    adapter = SimpleNamespace(cost_gate=gate)
    broker = CompileQueries(SimpleNamespace())
    if not confirmed:
        with pytest.raises(ConfirmationRequiredError) as raised:
            broker.handshake("unused", adapter, 60)
        assert "dynamically" in str(raised.value.request)
        assert entries == []
    else:
        broker.handshake("unused", adapter, 60)
        gate.charge(60)
        gate.record_billed(60)
        with pytest.raises(ConfirmationRequiredError) as raised:
            broker.handshake("unused", adapter, 60)
        assert raised.value.cost.estimate == 120
        gate.settle()
        assert gate.spend_summary()["bytes_billed"] == 60


@pytest.mark.parametrize(
    "macro",
    [
        "{% do adapter.execute('delete from customers') %}",
        "{% call statement('bad') %}delete from customers{% endcall %}",
    ],
)
def test_macro_sql_cannot_bypass_governance(project, tmp_path, capsys, macro):
    rc, out = _plan(
        tmp_path, capsys, "{% if true %}" + macro + "{% endif %}select 1 as id"
    )
    assert rc == 1, out
    assert "read-only" in str(out)


def test_compilation_requires_a_dev_target(project, tmp_path, capsys):
    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text())
    config["dbt_target"] = "prod"
    config_path.write_text(yaml.safe_dump(config))
    rc, out = _plan(tmp_path, capsys, LOOP)
    assert rc == 1, out
    assert "prod" in str(out)


def test_compile_time_queries_cannot_use_partial_results(project, tmp_path, capsys):
    _run(tmp_path, capsys, "explore", "map")
    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text())
    config["query"] = {"max_rows": 1}
    config_path.write_text(yaml.safe_dump(config))
    rc, out = _plan(
        tmp_path,
        capsys,
        "{% if execute %}{% do run_query('select id from customers') %}"
        "{% endif %}select 1 as id",
    )
    assert rc == 1, out
    assert "truncated" in str(out)


def test_compilation_resolves_a_relative_environment_profile_path(
    project, tmp_path, capsys, monkeypatch
):
    profile_path = project / "profiles.yml"
    profiles = yaml.safe_load(profile_path.read_text())
    profiles["dex_test"]["outputs"]["dev"]["path"] = "{{ env_var('TEST_DEX_DB_PATH') }}"
    profile_path.write_text(yaml.safe_dump(profiles))
    monkeypatch.setenv("TEST_DEX_DB_PATH", "../warehouse.duckdb")
    rc, out = _plan(tmp_path, capsys, LOOP)
    assert rc == 0, out


def test_missing_dbt_and_missing_artifacts_fail_closed(
    project, tmp_path, capsys, monkeypatch
):
    import exmergo_dex_core.transform.compile as compilation

    monkeypatch.setattr(
        compilation, "_worker", lambda *args: ({"success": True, "nodes": {}}, [])
    )
    rc, out = _plan(tmp_path, capsys, LOOP)
    assert rc == 1, out
    assert "no fresh compiled SQL" in str(out)
    monkeypatch.setattr(compilation.importlib.util, "find_spec", lambda name: None)
    rc, out = _plan(tmp_path, capsys, LOOP)
    assert rc == 1, out
    assert "require dbt" in str(out)
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


def test_macro_tables_preserve_sql_types_and_null_spelled_text(
    project, tmp_path, capsys
):
    from exmergo_dex_core import DexEngine
    from exmergo_dex_core.transform.compile import compile_models

    _run(tmp_path, capsys, "explore", "map")
    sql = """{% if execute %}
{% set data = run_query("select 1 as one, 'null' as label") %}
select {{ data.rows[0][0] }} as n, '{{ data.rows[0][1] }}' as label
{% else %}select 0 as n, '' as label{% endif %}
"""
    edit = PlanEdit(
        path="models/typed_macro.sql", kind=EditKind.MODEL_SQL, new_content=sql
    )
    with DexEngine.from_repo(tmp_path) as engine:
        compiled, broker = compile_models(engine, [edit])
        broker.settle()
    assert "select 1 as n, 'null' as label" in compiled[edit.path].sql


def test_incremental_condition_uses_governed_relation_metadata(
    project, tmp_path, capsys
):
    rc, out = _plan(
        tmp_path,
        capsys,
        "{% if is_incremental() %}select 2 as id{% else %}select 1 as id{% endif %}",
    )
    assert rc == 0, out


def test_explicit_warehouse_path_must_match_the_compilation_profile(
    project, tmp_path, capsys
):
    other = tmp_path / "other.duckdb"
    shutil.copyfile(tmp_path / "warehouse.duckdb", other)
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/loop.sql",
                        "kind": "model_sql",
                        "content": LOOP,
                    }
                ]
            }
        )
    )
    rc, out = _run(
        tmp_path,
        capsys,
        "transform",
        "plan",
        "path mismatch",
        "--edits-file",
        str(payload),
        "--path",
        str(other),
    )
    assert rc == 1, out
    assert "duckdb.path" in str(out)
