"""``dex_population_filter``: the generated test for a population filter (#1453).

A declaration's ``population.filters`` names a column and the values it
``includes`` or ``excludes``. With ``declarations.render_tests`` on, each list
renders this column test, whose macro ships with the engine and is written into
the project's macro paths by the plan that first renders it. Whether it agrees
with ``maintain verify``'s ``population_contradicted`` is the parity harness's
question (``test_generated_parity.py``); these are the rendering, the gating,
the file it writes and the SQL it compiles to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.claims import claim_id
from exmergo_dex_core.transform.declarations import (
    model_outputs,
    parse_declarations,
    render,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.POPULATION_FILTER


def _render(
    payload: dict, render_tests, *, generated=None, outputs=None, content=None
) -> str:
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        render_tests=render_tests,
        generated=generated,
        outputs=outputs,
    )


def _column_tests(content: str, column: str, model: str = "fct_orders") -> list:
    entry = next(e for e in yaml.safe_load(content)["models"] if e["name"] == model)
    found = next(c for c in entry.get("columns") or [] if c["name"] == column)
    return [
        t for t in found.get("data_tests") or [] if isinstance(t, dict) and NAME in t
    ]


def _declared(*filters: dict) -> dict:
    return {"model": "fct_orders", "population": {"filters": list(filters)}}


EXCLUDES = _declared({"column": "status", "excludes": ["cancelled"]})


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_an_every_run_test_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.EVERY_RUN
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


def test_a_filter_renders_one_count_only_test_under_its_column():
    content = _render(EXCLUDES, True)

    [test] = _column_tests(content, "status")
    assert test == {
        NAME: {
            "arguments": {"excludes": ["cancelled"]},
            "config": {
                "tags": [gt.EVERY_RUN],
                "meta": {
                    "disable_test_samples": True,
                    "dex": {
                        "claim": claim_id(
                            "fct_orders",
                            "population",
                            ["status"],
                            {"check": NAME, "excludes": ["cancelled"]},
                        )
                    },
                },
            },
        }
    }


def test_each_filter_and_each_list_is_its_own_test_and_claim():
    content = _render(
        _declared(
            {"column": "status", "includes": ["paid", "shipped"], "excludes": ["x"]},
            {"column": "region", "includes": ["emea"]},
        ),
        True,
    )

    status = _column_tests(content, "status")
    region = _column_tests(content, "region")
    assert [t[NAME]["arguments"] for t in status] == [
        {"includes": ["paid", "shipped"]},
        {"excludes": ["x"]},
    ]
    assert [t[NAME]["arguments"] for t in region] == [{"includes": ["emea"]}]
    claims = {t[NAME]["config"]["meta"]["dex"]["claim"] for t in status + region}
    assert len(claims) == 3


def test_the_values_keep_their_case_in_the_claim():
    upper = _render(_declared({"column": "status", "excludes": ["Paid"]}), True)
    lower = _render(_declared({"column": "status", "excludes": ["paid"]}), True)

    def claim(content: str) -> str:
        [test] = _column_tests(content, "status")
        return test[NAME]["config"]["meta"]["dex"]["claim"]

    assert claim(upper) != claim(lower)


def test_rendering_again_adds_nothing():
    once = _render(EXCLUDES, True)
    generated: list = []
    twice = _render(EXCLUDES, True, content=once, generated=generated)

    assert twice == once
    assert [g["status"] for g in generated] == ["already_listed"]


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(EXCLUDES, False, generated=generated)

    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "render_tests_off")
    ]


@pytest.mark.parametrize("setting", [[NAME], [gt.EVERY_RUN]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert NAME in _render(EXCLUDES, setting)


def test_a_list_naming_another_kind_leaves_it_off():
    generated: list = []
    content = _render(EXCLUDES, [gt.UNIQUE_COMBINATION], generated=generated)

    assert NAME not in content
    assert [g["reason"] for g in generated] == ["kind_not_enabled"]


# --- a column the model does not output ---------------------------------------------


def test_a_column_the_model_provably_does_not_output_renders_nothing():
    generated: list = []
    content = _render(
        EXCLUDES, True, generated=generated, outputs=frozenset({"order_id"})
    )

    assert NAME not in content
    # No column entry either: one for a column the relation lacks would be a
    # `column_missing` in maintain verify.
    [entry] = yaml.safe_load(content)["models"]
    assert "status" not in {c["name"] for c in entry.get("columns") or []}
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", gt.COLUMN_NOT_OUTPUT)
    ]


def test_an_output_column_matches_whatever_its_case():
    assert NAME in _render(EXCLUDES, True, outputs=frozenset({"status"}))
    upper = _declared({"column": "STATUS", "excludes": ["x"]})
    assert NAME in _render(upper, True, outputs=frozenset({"status"}))


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("select order_id, status from {{ ref('stg') }}", {"order_id", "status"}),
        (
            "{{ config(materialized='table') }}\n"
            "with s as (select order_id, status from {{ ref('stg') }})\n"
            "select * from s",
            {"order_id", "status"},
        ),
        (
            "select region, count(*) as n from {{ ref('stg') }} group by 1",
            {"region", "n"},
        ),
        ("select * from {{ ref('stg') }}", None),
        ("select {{ dbt_utils.star(ref('stg')) }} from {{ ref('stg') }}", None),
        (
            "select order_id{% for c in ['status'] %}, {{ c }}{% endfor %} "
            "from {{ ref('stg') }}",
            None,
        ),
        ("select a from x union all select a from y", None),
    ],
)
def test_the_outputs_are_known_only_where_the_sql_proves_them(sql, expected):
    files = {"models/marts/m.sql": sql}
    outputs = model_outputs(files, ["models"], "m")
    assert outputs == (frozenset(expected) if expected is not None else None)


# --- the macro file ---------------------------------------------------------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


def test_clickhouse_casts_with_tostring_which_keeps_a_null():
    # ClickHouse refuses CAST(NULL AS String): its non-Nullable String cannot
    # hold the NULL an includes or excludes column may carry.
    asset = _asset()
    assert "{% macro default__dex_population_filter_text(column_name) %}" in asset
    assert "clickhouse__dex_population_filter_text(column_name) %}toString(" in asset


def test_the_shipped_macro_passes_the_macro_file_check():
    validate_edit(
        PlanEdit(
            path=f"macros/{NAME}.sql",
            kind=EditKind.MACRO_SQL,
            new_content=_asset(),
        )
    )


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


def _plan(tmp_path: Path, capsys, sql: str, declaration: dict = EXCLUDES) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/fct_orders.sql",
                        "kind": "model_sql",
                        "content": sql,
                    }
                ],
                "declarations": [declaration],
            }
        ),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


ORDERS_SQL = "select 1 as order_id, 'paid' as status\n"


def test_the_plan_that_renders_it_writes_its_macro_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, ORDERS_SQL)

    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    assert planned["data"]["generated_tests"]["counts"] == {"added": 1}


def test_a_plan_on_a_rollup_without_the_column_says_why_and_writes_no_macro(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(
        _repo(tmp_path, "true"),
        capsys,
        "select 'emea' as region, 3 as orders\n",
    )

    assert planned["status"] == "ok", planned["errors"]
    assert not any(p.startswith("macros/") for p in planned["data"]["paths"])
    [row] = planned["data"]["generated_tests"]["tests"]
    assert (row["status"], row["reason"]) == ("not_rendered", gt.COLUMN_NOT_OUTPUT)


def test_a_plan_with_the_switch_off_writes_no_macro(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "false"), capsys, ORDERS_SQL)

    assert planned["status"] == "ok", planned["errors"]
    assert not any(p.startswith("macros/") for p in planned["data"]["paths"])


def test_transform_macro_lists_it(dbt_project_dir: Path, tmp_path: Path, capsys):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- what it compiles to, against DuckDB --------------------------------------------


duckdb = pytest.importorskip("duckdb")


def _failures(tmp_path: Path, select: str, arguments: dict) -> tuple[str, int, str]:
    """Build ``select`` as a view, run the shipped test on its ``status`` with
    ``arguments``, and return dbt's status, failure count and compiled SQL."""

    import subprocess
    import sys

    project = tmp_path / "proj"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\nconfig-version: 2\n", encoding="utf-8"
    )
    (project / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'w.duckdb'}\n      threads: 1\n",
        encoding="utf-8",
    )
    (project / "macros" / f"{NAME}.sql").write_text(_asset(), encoding="utf-8")
    (project / "models" / "m.sql").write_text(select, encoding="utf-8")
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "m",
                        "columns": [
                            {
                                "name": "status",
                                "data_tests": [{NAME: {"arguments": arguments}}],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    dbt = Path(sys.executable).parent / "dbt"
    if not dbt.is_file():
        pytest.skip("needs dbt beside the interpreter")
    args = ["--project-dir", str(project), "--profiles-dir", str(project)]
    subprocess.run(  # noqa: S603 - the venv's own dbt, on a fixture project
        [str(dbt), "build", *args], cwd=project, capture_output=True, check=False
    )
    results = json.loads((project / "target" / "run_results.json").read_text())
    [result] = [r for r in results["results"] if r["unique_id"].startswith("test.")]
    compiled = next((project / "target" / "compiled").rglob(f"{NAME}*.sql"))
    return result["status"], int(result["failures"] or 0), compiled.read_text()


_STATUSES = (
    "select 'paid' as status union all select 'cancelled' "
    "union all select 'cancelled' union all select null"
)


def test_excludes_counts_the_excluded_rows_and_never_a_null(tmp_path: Path):
    status, failures, sql = _failures(tmp_path, _STATUSES, {"excludes": ["cancelled"]})
    assert (status, failures) == ("fail", 2)
    assert "cancelled" not in sql.split("from", 1)[0]  # a count, never a value


def test_includes_counts_the_other_values_and_the_nulls(tmp_path: Path):
    assert _failures(tmp_path, _STATUSES, {"includes": ["paid"]})[:2] == ("fail", 3)


def test_a_filter_that_holds_passes(tmp_path: Path):
    assert _failures(tmp_path, _STATUSES, {"excludes": ["refunded"]})[:2] == (
        "pass",
        0,
    )


def test_text_values_on_a_number_column_are_compared_as_text(tmp_path: Path):
    select = "select 7 as status union all select 8"
    status, failures, sql = _failures(tmp_path, select, {"excludes": ["7"]})
    assert (status, failures) == ("fail", 1)
    assert "cast(status as" in sql


def test_number_values_on_a_text_column_are_compared_as_text(tmp_path: Path):
    # The relation's metadata says the column is text, so the numbers are
    # quoted, as maintain verify quotes them.
    select = "select '7' as status union all select '8'"
    status, failures, sql = _failures(tmp_path, select, {"excludes": [7]})
    assert (status, failures) == ("fail", 1)
    assert "'7'" in sql


def test_number_values_on_a_number_column_are_compared_as_numbers(tmp_path: Path):
    select = "select 1.0 as status union all select 2.5"
    status, failures, sql = _failures(tmp_path, select, {"includes": [1]})
    assert (status, failures) == ("fail", 1)
    assert "cast(" not in sql


def test_a_boolean_value_is_a_boolean_literal(tmp_path: Path):
    select = "select true as status union all select false"
    assert _failures(tmp_path, select, {"excludes": [False]})[:2] == ("fail", 1)


def test_a_quote_in_a_value_is_escaped(tmp_path: Path):
    select = "select 'o''brien' as status union all select 'x'"
    assert _failures(tmp_path, select, {"excludes": ["o'brien"]})[:2] == ("fail", 1)
