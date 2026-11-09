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


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM (DESCRIBE SELECT 1 AS due_at) AS d",
        "WITH d AS (DESCRIBE SELECT 1 AS due_at) SELECT * FROM d",
    ],
)
def test_compile_time_nested_describe_is_refused_before_connection(
    project, tmp_path, capsys, monkeypatch, statement
):
    # A minimal public planning fixture, separate from the parser-only repro.
    from exmergo_dex_core.engine import DexEngine

    def unexpected_adapter(*args, **kwargs):
        pytest.fail("unsupported compile-time SQL must not open a warehouse")

    monkeypatch.setattr(DexEngine, "_adapter", unexpected_adapter)
    sql = (
        "{% if execute %}{% do run_query("
        + repr(statement)
        + ") %}{% endif %} select 1 as id"
    )
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 1, out
    assert out["reason"] == "guard"
    # The refusal names the brokered adapter method, not a profile that cannot
    # help a macro (#1154).
    errors = " ".join(out["errors"])
    assert "DESCRIBE" in errors
    assert "adapter.get_columns_in_relation" in errors
    assert "explore profile" not in errors
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


@pytest.mark.parametrize(
    "statement",
    [
        "DESCRIBE SELECT 1 AS due_at",
        "DESCRIBE customers",
        "SUMMARIZE customers",
        "SELECT * FROM (SUMMARIZE customers) AS s",
        "SELECT column_name FROM information_schema.columns",
        "SELECT column_name, column_type AS data_type FROM (DESCRIBE customers)",
    ],
)
def test_compile_time_raw_introspection_sql_stays_refused(
    project, tmp_path, capsys, monkeypatch, statement
):
    """Only the adapter method is brokered: every SQL spelling stays refused."""

    from exmergo_dex_core.engine import DexEngine

    def unexpected_adapter(*args, **kwargs):
        pytest.fail("refused compile-time SQL must not open a warehouse")

    monkeypatch.setattr(DexEngine, "_adapter", unexpected_adapter)
    sql = (
        "{% if execute %}{% do run_query("
        + repr(statement)
        + ") %}{% endif %} select 1 as id"
    )
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 1, out
    assert out["reason"] == "guard"
    assert "adapter.get_columns_in_relation" in " ".join(out["errors"])
    assert not list((tmp_path / ".dex" / "plans").glob("*.json"))


# dbt_utils' own `star` and `get_filtered_columns_in_relation`, trimmed to what
# they do with the warehouse: dbt_utils is not installed offline.
STAR_MACROS = """{% macro get_filtered_columns_in_relation(from, except=[]) -%}
  {%- if not execute -%}{{ return('') }}{% endif -%}
  {%- set include_cols = [] -%}
  {%- set cols = adapter.get_columns_in_relation(from) -%}
  {%- set except = except | map("lower") | list -%}
  {%- for col in cols -%}
    {%- if col.column|lower not in except -%}
      {% do include_cols.append(col.column) %}
    {%- endif -%}
  {%- endfor -%}
  {{ return(include_cols) }}
{%- endmacro %}

{% macro star(from, except=[]) -%}
  {%- if not execute -%}{% do return('*') %}{%- endif -%}
  {%- set cols = get_filtered_columns_in_relation(from, except) -%}
  {%- if cols|length <= 0 -%}*{%- else -%}
  {%- for col in cols %}{{ adapter.quote(col) }}
  {%- if not loop.last %}, {% endif %}{% endfor -%}
  {%- endif -%}
{%- endmacro %}
"""


def _compile(tmp_path, sql, *, extra=()):
    from exmergo_dex_core import DexEngine
    from exmergo_dex_core.transform.compile import compile_models

    edits = [
        PlanEdit(path="models/loop.sql", kind=EditKind.MODEL_SQL, new_content=sql),
        *extra,
    ]
    with DexEngine.from_repo(tmp_path) as engine:
        compiled, broker = compile_models(engine, edits)
        broker.settle()
    return compiled["models/loop.sql"].sql, broker


def test_get_columns_in_relation_is_answered_from_metadata(project, tmp_path, capsys):
    sql = (
        "{% if execute %}"
        "{% set cols = adapter.get_columns_in_relation(api.Relation.create("
        "database='warehouse', schema='main', identifier='customers')) %}"
        "select {{ cols | map(attribute='name') | join(', ') }} from customers"
        "{% else %}select 1 as id{% endif %}"
    )
    compiled, broker = _compile(tmp_path, sql)
    assert "select id, email from customers" in compiled
    assert broker.introspected == ["warehouse.main.customers"]
    assert broker.queries == 1
    assert any("listed the columns" in w for w in broker.warnings)

    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 0, out
    assert any("no rows read" in w for w in out["warnings"])


def test_a_dbt_utils_star_model_plans(project, tmp_path, capsys):
    macro = {"path": "macros/star.sql", "kind": "macro_sql", "content": STAR_MACROS}
    sql = (
        "{% if true %}select {{ star(source('raw', 'customers'), except=['email']) }}"
        " from {{ source('raw', 'customers') }}{% endif %}"
    )
    sources = {
        "path": "models/sources.yml",
        "kind": "schema_yml",
        "content": yaml.safe_dump(
            {
                "version": 2,
                "sources": [
                    {
                        "name": "raw",
                        "database": "warehouse",
                        "schema": "main",
                        "tables": [{"name": "customers"}],
                    }
                ],
            }
        ),
    }
    compiled, _broker = _compile(
        tmp_path,
        sql,
        extra=[
            PlanEdit(
                path=macro["path"], kind=EditKind.MACRO_SQL, new_content=STAR_MACROS
            ),
            PlanEdit(
                path=sources["path"],
                kind=EditKind.SCHEMA_YML,
                new_content=sources["content"],
            ),
        ],
    )
    assert 'select "id" from' in compiled

    rc, out = _plan(tmp_path, capsys, sql, extra=[macro, sources])
    assert rc == 0, out
    stored = json.loads(next((tmp_path / ".dex" / "plans").glob("*.json")).read_text())
    # The authored Jinja is the reviewable edit, not the compiled column list.
    assert stored["edits"][0]["new_content"] == sql


def test_column_listing_macro_compiles_and_checks_select_list(
    project, tmp_path, capsys
):
    """#1200: A model listing columns without control flow is compiled and checked."""

    macro = {"path": "macros/star.sql", "kind": "macro_sql", "content": STAR_MACROS}
    # No control flow: just a column-listing macro.
    sql = (
        "select {{ star(source('raw', 'customers'), except=['email']) }}"
        " from {{ source('raw', 'customers') }}"
    )
    sources = {
        "path": "models/sources.yml",
        "kind": "schema_yml",
        "content": yaml.safe_dump(
            {
                "version": 2,
                "sources": [
                    {
                        "name": "raw",
                        "database": "warehouse",
                        "schema": "main",
                        "tables": [{"name": "customers"}],
                    }
                ],
                "models": [
                    {
                        "name": "loop",
                        "columns": [{"name": "id"}, {"name": "email"}],
                    }
                ],
            }
        ),
    }

    rc, out = _plan(tmp_path, capsys, sql, extra=[macro, sources])
    assert rc == 0, out
    expected = (
        "models/loop.sql: schema.yml declares column(s) email for loop "
        "that the SELECT list does not produce"
    )
    assert any(expected in w for w in out["warnings"]), out["warnings"]


def test_columns_of_a_relation_outside_the_inventory_are_none_and_named(
    project, tmp_path
):
    sql = (
        "{% if execute %}"
        "{% set cols = adapter.get_columns_in_relation(api.Relation.create("
        "database='warehouse', schema='main', identifier='not_built')) %}"
        "select {{ cols | length }} as n{% endif %}"
    )
    compiled, broker = _compile(tmp_path, sql)
    assert "select 0 as n" in compiled
    assert broker.introspected == []
    assert any("warehouse.main.not_built" in w for w in broker.warnings)


def test_column_listings_count_toward_max_statements(project, tmp_path, capsys):
    config_path = tmp_path / ".dex" / "config.yml"
    config = yaml.safe_load(config_path.read_text())
    config["query"] = {"max_statements": 1}
    config_path.write_text(yaml.safe_dump(config))
    lookup = (
        "{% do adapter.get_columns_in_relation(api.Relation.create("
        "database='warehouse', schema='main', identifier='customers')) %}"
    )
    rc, out = _plan(
        tmp_path,
        capsys,
        "{% if execute %}" + lookup + lookup + "{% endif %}select 1 as id",
    )
    assert rc == 1, out
    assert "max_statements" in str(out)


def test_columns_broker_unit():
    """The parent side, without dbt: counting, notes, misses and ambiguity."""

    from exmergo_dex_core.adapters.base import ColumnMeta

    listed = [
        SimpleNamespace(identifier="db.main.orders", object_type="TABLE"),
        SimpleNamespace(identifier="db.dev.orders", object_type="VIEW"),
    ]
    reads = []

    def table_metadata(identifier):
        reads.append(identifier)
        return None, [ColumnMeta("id", "INTEGER", False, 0)]

    adapter = SimpleNamespace(
        list_objects=lambda: listed, table_metadata=table_metadata
    )
    engine = SimpleNamespace(
        config=SimpleNamespace(query=SimpleNamespace(max_statements=3)),
        _adapter=lambda *a, **k: adapter,
    )
    broker = CompileQueries(engine)
    request = {"database": "db", "schema": "main", "identifier": "orders"}
    assert broker.columns(request) == {"columns": [["id", "INTEGER"]]}
    assert broker.columns(request) == {"columns": [["id", "INTEGER"]]}
    assert reads == ["db.main.orders", "db.main.orders"]
    # One note per relation, however often dbt asks.
    assert sum("listed the columns" in w for w in broker.warnings) == 1
    assert broker.columns({"identifier": "missing"}) == {"columns": []}
    assert broker.queries == 3
    with pytest.raises(EditValidationError, match="max_statements"):
        broker.columns(request)
    ambiguous = CompileQueries(engine)
    with pytest.raises(EditValidationError, match="ambiguous"):
        ambiguous.columns({"identifier": "orders"})


@pytest.mark.parametrize(
    "call",
    [
        # The dispatched macro, not the adapter method: on DuckDB it issues the
        # DESCRIBE itself, through a statement block.
        "get_columns_in_relation(r)",
        # An adapter method that lists columns from Python, past the broker.
        "adapter.get_missing_columns(r, r)",
    ],
)
def test_unbrokered_column_introspection_is_refused_by_name(
    project, tmp_path, capsys, call
):
    sql = (
        "{% if execute %}{% set r = api.Relation.create(database='warehouse', "
        "schema='main', identifier='customers') %}{% set x = " + call + " %}"
        "{% endif %} select 1 as id"
    )
    rc, out = _plan(tmp_path, capsys, sql)
    assert rc == 1, out
    assert out["reason"] == "guard", out
    errors = " ".join(out["errors"])
    assert "call adapter.get_columns_in_relation instead" in errors
    assert "explore profile" not in errors
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
    """Jinja comments are stripped and do not trigger compilation."""

    assert not needs_compilation("{# {% if true %} ignored {% endif %} #} select 1")


@pytest.mark.parametrize(
    "sql",
    [
        "select {{ dbt_utils.star(ref('orders')) }}",
        "select {{ star(ref('orders')) }}",
        "select {{ dbt_utils.get_filtered_columns_in_relation(ref('orders')) }}",
        "select {{ adapter.get_columns_in_relation(ref('orders')) }}",
        "select {{ adapter.get_relation_columns(ref('orders')) }}",
        "select {{ fivetran_utils.fill_staging_columns(source_columns=[]) }}",
        "{% set cols = adapter.get_columns_in_relation(ref('orders')) %}",
    ],
)
def test_column_listing_macros_require_compilation(sql):
    """#1200: Known column listing macros and adapter calls require compilation."""

    assert needs_compilation(sql)


def test_plain_sql_does_not_require_compilation():
    """#1200: Models without control flow or column listing do not compile."""

    assert not needs_compilation("select 1 as id from {{ ref('orders') }}")
    assert not needs_compilation("select 1 as id from adapter.orders")
    assert not needs_compilation("select {{ adapter.quote('col') }} from tbl")
    assert not needs_compilation("select {{ adapter.dispatch('m', 'p')() }} from tbl")
    assert not needs_compilation("{# {{ dbt_utils.star(ref('x')) }} #} select 1")


def test_column_contract_warnings_when_compilation_not_possible():
    """#1200: When compilation is not possible, warnings say comparison was skipped."""

    from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
    from exmergo_dex_core.transform.plans import column_contract_warnings

    view = DbtProjectView(
        root=".",
        project_name="test",
        profile_name="test",
        files={
            "models/schema.yml": SourceFile(
                path="models/schema.yml",
                content=yaml.safe_dump(
                    {
                        "version": 2,
                        "models": [
                            {
                                "name": "loop",
                                "columns": [{"name": "id"}, {"name": "email"}],
                            }
                        ],
                    }
                ),
                sha256="h",
            )
        },
        model_paths=["models"],
    )
    edits = [
        PlanEdit(
            path="models/loop.sql",
            kind=EditKind.MODEL_SQL,
            new_content=(
                "select {{ dbt_utils.star(ref('stg_orders')) }} as aliased_col "
                "from {{ ref('stg_orders') }}"
            ),
        )
    ]
    warnings = column_contract_warnings(edits, view, compiled_models=None)
    assert any(
        "compilation was not possible" in w and "contract comparison was skipped" in w
        for w in warnings
    ), warnings


def test_column_contract_warnings_upstream_uncompiled_skips_downstream():
    """#1200: Downstream select-star skips contract when upstream uncompiled."""

    from exmergo_dex_core.dbt_project import DbtProjectView, SourceFile
    from exmergo_dex_core.transform.plans import column_contract_warnings

    view = DbtProjectView(
        root=".",
        project_name="test",
        profile_name="test",
        files={
            "models/schema.yml": SourceFile(
                path="models/schema.yml",
                content=yaml.safe_dump(
                    {
                        "version": 2,
                        "models": [
                            {
                                "name": "downstream",
                                "columns": [{"name": "id"}, {"name": "email"}],
                            }
                        ],
                    }
                ),
                sha256="h",
            ),
            "models/upstream.sql": SourceFile(
                path="models/upstream.sql",
                content=(
                    "select {{ dbt_utils.star(ref('raw')) }} as aliased_col "
                    "from {{ ref('raw') }}"
                ),
                sha256="h2",
            ),
        },
        model_paths=["models"],
    )
    edits = [
        PlanEdit(
            path="models/upstream.sql",
            kind=EditKind.MODEL_SQL,
            new_content=(
                "select {{ dbt_utils.star(ref('raw')) }} as aliased_col "
                "from {{ ref('raw') }}"
            ),
        ),
        PlanEdit(
            path="models/downstream.sql",
            kind=EditKind.MODEL_SQL,
            new_content="select * from {{ ref('upstream') }}",
        ),
    ]
    warnings = column_contract_warnings(edits, view, compiled_models=None)
    assert any(
        "compilation was not possible" in w and "contract comparison was skipped" in w
        for w in warnings
    ), warnings


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


def _manager_like_bigquery():
    """A connection manager whose close() reads the handle, as dbt-bigquery's
    does, so closing a never-opened connection would resolve its lazy handle."""

    import threading

    class Lazy:
        def resolve(self, connection):
            raise AssertionError("resolved a handle only to close it")

    class States:
        CLOSED = "closed"

    class Connection:
        def __init__(self, handle):
            self._handle = handle
            self.state = "init"

        @property
        def handle(self):
            if isinstance(self._handle, Lazy):
                self._handle.resolve(self)
            return self._handle

    class Manager:
        def __init__(self, connections):
            self.lock = threading.Lock()
            self.thread_connections = dict(enumerate(connections))

        def get_if_exists(self):
            return next(iter(self.thread_connections.values()), None)

        @classmethod
        def close(cls, connection):
            connection.handle.close()
            connection.state = States.CLOSED

        def release(self):
            conn = self.get_if_exists()
            if conn is not None:
                self.close(conn)

        def cleanup_all(self):
            for connection in self.thread_connections.values():
                self.close(connection)
            self.thread_connections.clear()

    return Manager, Connection, Lazy, States


def test_releasing_a_never_opened_connection_does_not_open_it():
    """dbt-bigquery's close() read the handle and so refused every compile on
    BigQuery before a model compiled (found dogfooding #1015)."""

    from exmergo_dex_core.transform.compile_worker import guard_unopened_connections

    manager, connection, lazy, states = _manager_like_bigquery()
    guard_unopened_connections(manager, lazy, states)

    unopened = connection(lazy())
    manager([unopened]).release()
    assert unopened.state == states.CLOSED

    pool = manager([connection(lazy()), connection(lazy())])
    pool.cleanup_all()
    assert pool.thread_connections == {}


def test_an_opened_connection_is_still_closed_normally():
    from exmergo_dex_core.transform.compile_worker import guard_unopened_connections

    manager, connection, lazy, states = _manager_like_bigquery()
    guard_unopened_connections(manager, lazy, states)

    closed: list[bool] = []
    opened = connection(SimpleNamespace(close=lambda: closed.append(True)))
    manager([opened]).release()
    assert closed == [True]
    assert opened.state == states.CLOSED


@pytest.mark.parametrize(
    ("dialect", "sql", "shape"),
    [
        ("duckdb", "SELECT * FROM (DESCRIBE customers)", "DESCRIBE"),
        ("duckdb", "SUMMARIZE customers", "SUMMARIZE"),
        (
            "duckdb",
            "SELECT 1 FROM information_schema.columns",
            "information_schema read",
        ),
        # BigQuery keeps the dotted catalog view as one table name.
        (
            "bigquery",
            "SELECT column_name FROM information_schema.columns",
            "information_schema read",
        ),
        (
            "bigquery",
            "SELECT * FROM `p.d`.INFORMATION_SCHEMA.COLUMNS",
            "information_schema read",
        ),
        (
            "snowflake",
            "SELECT 1 FROM db.information_schema.columns",
            "information_schema read",
        ),
        ("bigquery", "SELECT word FROM samples.shakespeare", None),
        ("duckdb", "SELECT count(*) FROM customers", None),
        ("duckdb", "select from", None),
    ],
)
def test_introspection_shape_names_catalog_reads(dialect, sql, shape):
    from exmergo_dex_core.transform.compile import introspection_shape

    assert introspection_shape(sql, dialect) == shape


def test_bigquery_columns_are_built_by_dbt_from_the_column_tree():
    """A nested column renders as dbt-bigquery renders it, not as a bare STRUCT."""

    pytest.importorskip("dbt.adapters.bigquery")
    from dbt.adapters.bigquery.column import BigQueryColumn

    from exmergo_dex_core.transform.compile_worker import dbt_columns

    answer = {
        "columns": [["id", "INT64"], ["owner", "STRUCT"], ["tags", "ARRAY<STRING>"]],
        "schema": [
            {"name": "id", "type": "INTEGER", "mode": "REQUIRED", "fields": []},
            {
                "name": "owner",
                "type": "RECORD",
                "mode": "NULLABLE",
                "fields": [
                    {
                        "name": "login",
                        "type": "STRING",
                        "mode": "NULLABLE",
                        "fields": [],
                    }
                ],
            },
            {"name": "tags", "type": "STRING", "mode": "REPEATED", "fields": []},
        ],
    }
    built = dbt_columns(BigQueryColumn, answer)
    assert [c.name for c in built] == ["id", "owner", "tags"]
    assert [c.data_type for c in built] == [
        "INT64",
        "STRUCT<`login` STRING>",
        "ARRAY<STRING>",
    ]


def test_flat_columns_are_built_with_create():
    pytest.importorskip("dbt.adapters.duckdb")
    from dbt.adapters.duckdb.column import DuckDBColumn

    from exmergo_dex_core.transform.compile_worker import dbt_columns

    built = dbt_columns(DuckDBColumn, {"columns": [["total", "DECIMAL(10,2)"]]})
    assert [(c.name, c.data_type) for c in built] == [("total", "DECIMAL(10,2)")]


def test_snowflake_columns_are_built_as_dbt_builds_them_from_describe():
    """A NUMBER(10,2) keeps its precision and scale, as a real build sees it."""

    pytest.importorskip("dbt.adapters.snowflake")
    from dbt.adapters.snowflake import SnowflakeColumn

    from exmergo_dex_core.transform.compile_worker import dbt_columns

    answer = {
        "columns": [["amount", "NUMBER(10,2)"], ["note", "VARCHAR(16777216)"]],
        "described": True,
    }
    amount, note = dbt_columns(SnowflakeColumn, answer)
    assert (amount.dtype, amount.numeric_precision, amount.numeric_scale) == (
        "NUMBER",
        10,
        2,
    )
    assert amount.data_type == "NUMBER(10,2)"
    assert note.char_size == 16777216
    expected = SnowflakeColumn.from_description("note", "VARCHAR(16777216)")
    assert note.data_type == expected.data_type


@pytest.mark.parametrize(
    ("document", "described"),
    [
        ({"type": "FIXED", "precision": 10, "scale": 2}, "NUMBER(10,2)"),
        ({"type": "TEXT", "length": 16777216}, "VARCHAR(16777216)"),
        ({"type": "BINARY", "length": 8388608}, "BINARY(8388608)"),
        ({"type": "REAL"}, "FLOAT"),
        ({"type": "TIMESTAMP_NTZ", "precision": 0, "scale": 9}, "TIMESTAMP_NTZ(9)"),
        ({"type": "TIME", "precision": 0, "scale": 9}, "TIME(9)"),
        ({"type": "FIXED"}, "NUMBER"),
        ({"type": "VARIANT"}, "VARIANT"),
    ],
)
def test_snowflake_types_are_rendered_as_describe_prints_them(document, described):
    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter

    assert SnowflakeAdapter._describe_type(json.dumps(document)) == described


def test_snowflake_dbt_column_types_read_the_show_columns_documents():
    from exmergo_dex_core.adapters.snowflake import SnowflakeAdapter

    adapter = SnowflakeAdapter.__new__(SnowflakeAdapter)
    adapter._objects = {"DB.S.T": {"identifier": "DB.S.T"}}
    adapter._type_documents = {
        "DB.S.T": [("AMOUNT", '{"type":"FIXED","precision":10,"scale":2}')]
    }
    adapter.table_metadata = lambda identifier: (None, [])
    assert adapter.dbt_column_types("DB.S.T") == [["AMOUNT", "NUMBER(10,2)"]]


def test_columns_broker_hands_over_described_types_where_the_adapter_has_them():
    from exmergo_dex_core.adapters.base import ColumnMeta

    adapter = SimpleNamespace(
        list_objects=lambda: [
            SimpleNamespace(identifier="DB.S.T", object_type="TABLE")
        ],
        table_metadata=lambda i: (None, [ColumnMeta("AMOUNT", "FIXED", True, 0)]),
        dbt_column_types=lambda i: [["AMOUNT", "NUMBER(10,2)"]],
    )
    engine = SimpleNamespace(
        config=SimpleNamespace(query=SimpleNamespace(max_statements=5)),
        _adapter=lambda *a, **k: adapter,
    )
    assert CompileQueries(engine).columns({"identifier": "T"}) == {
        "columns": [["AMOUNT", "NUMBER(10,2)"]],
        "described": True,
    }


def test_bigquery_column_schema_keeps_only_what_dbt_reads():
    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter

    login = SimpleNamespace(
        name="login", field_type="STRING", mode=None, fields=(), description="x"
    )
    owner = SimpleNamespace(
        name="owner", field_type="RECORD", mode="NULLABLE", fields=(login,)
    )
    adapter = BigQueryAdapter.__new__(BigQueryAdapter)
    adapter._get_table = lambda identifier: SimpleNamespace(schema=[owner])
    assert adapter.column_schema("p.d.t") == [
        {
            "name": "owner",
            "type": "RECORD",
            "mode": "NULLABLE",
            "fields": [
                {"name": "login", "type": "STRING", "mode": "NULLABLE", "fields": []}
            ],
        }
    ]


def test_columns_broker_hands_over_a_column_tree_where_the_adapter_has_one():
    from exmergo_dex_core.adapters.base import ColumnMeta

    tree = [{"name": "owner", "type": "RECORD", "mode": "NULLABLE", "fields": []}]
    adapter = SimpleNamespace(
        list_objects=lambda: [SimpleNamespace(identifier="p.d.t", object_type="TABLE")],
        table_metadata=lambda i: (None, [ColumnMeta("owner", "STRUCT", True, 0)]),
        column_schema=lambda i: tree,
    )
    engine = SimpleNamespace(
        config=SimpleNamespace(query=SimpleNamespace(max_statements=5)),
        _adapter=lambda *a, **k: adapter,
    )
    answer = CompileQueries(engine).columns({"identifier": "t"})
    assert answer == {"columns": [["owner", "STRUCT"]], "schema": tree}


def test_columns_broker_prefers_a_read_that_counts_no_rows():
    from exmergo_dex_core.adapters.base import ColumnMeta

    def counting(identifier):
        pytest.fail("a column listing must not count the relation's rows")

    adapter = SimpleNamespace(
        list_objects=lambda: [
            SimpleNamespace(identifier="db.main.v", object_type="VIEW")
        ],
        table_metadata=counting,
        table_columns=lambda i: [ColumnMeta("id", "INTEGER", True, 0)],
    )
    engine = SimpleNamespace(
        config=SimpleNamespace(query=SimpleNamespace(max_statements=5)),
        _adapter=lambda *a, **k: adapter,
    )
    assert CompileQueries(engine).columns({"identifier": "v"}) == {
        "columns": [["id", "INTEGER"]]
    }


def test_duckdb_table_columns_reads_the_catalog_only(duckdb_file, monkeypatch):
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    adapter = DuckDBAdapter(str(duckdb_file))
    statements = []
    original = adapter._run_select

    def recording(sql, *args, **kwargs):
        statements.append(sql)
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(adapter, "_run_select", recording)
    try:
        identifier = next(
            o.identifier for o in adapter.list_objects() if o.name == "customers"
        )
        statements.clear()
        columns = adapter.table_columns(identifier)
    finally:
        adapter.close()
    assert [c.name for c in columns] == ["id", "email"]
    assert not any("count(" in sql.lower() for sql in statements)
