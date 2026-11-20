"""``dex_unique_combination``, the generated test for a composite grain (#1452).

A one-column ``grain`` already renders dbt's ``unique`` and ``not_null``. A
grain of two or more columns renders this model-level test, when
``declarations.render_tests`` turns it on. Its macro ships with the engine and
is written into the project's macro paths by the plan that first renders it.
Whether it agrees with ``maintain verify``'s ``grain_broken`` is the parity
harness's question (``test_generated_parity.py``); these are the rendering, the
gating and the file it writes.
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
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

from .elementary_fixture import (
    ELEMENTARY_SCHEMA,
    dbt,
    duckdb_census,
    elementary_project,
    footprint,
    requires_elementary,
)
from .elementary_fixture import elementary_installs as elementary_installs
from .elementary_fixture import installed_elementary as installed_elementary

NAME = gt.UNIQUE_COMBINATION


def _render(payload: dict, render_tests, *, dialect=None, generated=None) -> str:
    [declaration] = parse_declarations([payload])
    return render(
        None,
        declaration,
        render_tests=render_tests,
        generated=generated,
        dialect=dialect,
    )


def _model_tests(content: str, model: str = "fct_lines") -> list:
    entry = next(e for e in yaml.safe_load(content)["models"] if e["name"] == model)
    return entry.get("data_tests") or []


COMPOSITE = {"model": "fct_lines", "grain": ["order_id", "line_number"]}


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_an_every_run_test_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.EVERY_RUN
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert NAME in scaffold.MACRO_ASSETS
    # A list naming it turns something on, so `config validate` does not warn.
    assert gt.unknown_entries([NAME]) == []


def test_a_composite_grain_renders_one_count_only_model_level_test():
    content = _render(COMPOSITE, True)

    [test] = _model_tests(content)
    assert test == {
        NAME: {
            "arguments": {"columns": ["order_id", "line_number"]},
            "config": {
                "tags": [gt.EVERY_RUN],
                "meta": {
                    "disable_test_samples": True,
                    "dex": {
                        "claim": claim_id(
                            "fct_lines",
                            "grain",
                            ["order_id", "line_number"],
                            {"check": NAME},
                        )
                    },
                },
            },
        }
    }


def test_a_one_column_grain_keeps_the_built_ins_and_renders_no_combination():
    content = _render({"model": "fct_lines", "grain": ["order_id"]}, True)

    assert _model_tests(content) == []
    assert NAME not in content


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(COMPOSITE, False, generated=generated)

    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "render_tests_off")
    ]


def test_a_dialect_the_kind_does_not_list_renders_nothing(monkeypatch):
    # #1451: a kind proved on some dialects renders nowhere else.
    shipped = next(k for k in gt.kinds() if k.name == NAME)
    gt.unregister(NAME)
    gt.register(
        gt.GeneratedTestKind(
            NAME,
            shipped.cadence,
            shipped.instances,
            macro=shipped.macro,
            dialects=frozenset({"duckdb"}),
        )
    )
    try:
        generated: list = []
        on_duckdb = _render(COMPOSITE, True, dialect="duckdb")
        on_bigquery = _render(COMPOSITE, True, dialect="bigquery", generated=generated)
        unknown = _render(COMPOSITE, True, dialect=None)
    finally:
        gt.unregister(NAME)
        gt.register(shipped)

    assert NAME in on_duckdb and NAME in unknown
    assert NAME not in on_bigquery
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", gt.DIALECT_UNSUPPORTED)
    ]


# --- the macro file ---------------------------------------------------------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


def test_the_shipped_macro_passes_the_macro_file_check():
    # A generic test beside the dispatched macros that implement it, as a
    # package ships one.
    validate_edit(
        PlanEdit(
            path="macros/dex_unique_combination.sql",
            kind=EditKind.MACRO_SQL,
            new_content=_asset(),
        )
    )


@pytest.mark.parametrize(
    "content",
    [
        "{% test t(model) %}select 1{% endtest %}\nloose text\n",
        "{% test t(model) %}select 1\n",
        "{% test t(model) %}select 1{% endtest %}\n{% macro m() %}x\n",
    ],
)
def test_a_macro_file_with_a_test_block_is_still_held_to_its_shape(content):
    from exmergo_dex_core.transform.validate import EditValidationError

    edit = PlanEdit(path="macros/t.sql", kind=EditKind.MACRO_SQL, new_content=content)
    with pytest.raises(EditValidationError):
        validate_edit(edit)


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


def _plan(tmp_path: Path, capsys, extra: list | None = None) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "models/marts/fct_lines.sql",
                        "kind": "model_sql",
                        "content": "select 1 as order_id, 1 as line_number\n",
                    },
                    *(extra or []),
                ],
                "declarations": [COMPOSITE],
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
            "lines",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


def test_the_plan_that_renders_it_writes_its_macro_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert "macros/dex_unique_combination.sql" in planned["data"]["paths"]
    assert planned["data"]["generated_tests"]["counts"] == {"added": 1}


def test_a_plan_with_the_switch_off_writes_no_macro(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "false"), capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert not any(p.startswith("macros/") for p in planned["data"]["paths"])


def test_a_project_that_defines_the_test_keeps_its_own_copy(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    own = dbt_project_dir / "macros" / "my_tests.sql"
    own.parent.mkdir()
    own.write_text(
        "{% test dex_unique_combination(model, columns) %}\n"
        "select 1 as n where 1 = 0\n{% endtest %}\n",
        encoding="utf-8",
    )

    planned = _plan(_repo(tmp_path, "true"), capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert "macros/dex_unique_combination.sql" not in planned["data"]["paths"]
    assert planned["data"]["generated_tests"]["counts"] == {"added": 1}


def test_a_file_already_at_the_macro_path_keeps_what_it_defines(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    override = (
        "{% macro postgres__dex_unique_combination(model, columns) %}\n"
        "select 0 as dex_duplicate_rows where false\n{% endmacro %}\n"
    )
    path = dbt_project_dir / "macros" / "dex_unique_combination.sql"
    path.parent.mkdir()
    path.write_text(override, encoding="utf-8")

    planned = _plan(_repo(tmp_path, "true"), capsys)
    assert planned["status"] == "ok", planned["errors"]
    main(["--repo-root", str(tmp_path), "transform", "apply"])
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "ok", applied

    merged = path.read_text(encoding="utf-8")
    assert merged.startswith(override)
    assert "{% test dex_unique_combination(model, columns) %}" in merged
    assert merged.count("macro default__dex_unique_combination(") == 1


def test_a_default_defined_in_another_file_is_not_written_twice(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    elsewhere = dbt_project_dir / "macros" / "overrides.sql"
    elsewhere.parent.mkdir()
    elsewhere.write_text(
        "{% macro default__dex_unique_combination(model, columns) %}\n"
        "select 0 as dex_duplicate_rows where false\n{% endmacro %}\n",
        encoding="utf-8",
    )

    planned = _plan(_repo(tmp_path, "true"), capsys)

    # dbt parsed the plan, so no name is defined twice.
    assert planned["status"] == "ok", planned["errors"]
    main(["--repo-root", str(tmp_path), "transform", "apply"])
    capsys.readouterr()
    written = (dbt_project_dir / "macros" / "dex_unique_combination.sql").read_text()
    assert "{% test dex_unique_combination(model, columns) %}" in written
    assert "default__dex_unique_combination" not in written


def test_a_macro_path_written_with_a_trailing_slash_finds_its_file(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    project_yml = dbt_project_dir / "dbt_project.yml"
    project_yml.write_text(
        project_yml.read_text(encoding="utf-8") + 'macro-paths: ["macros/"]\n',
        encoding="utf-8",
    )
    path = dbt_project_dir / "macros" / "dex_unique_combination.sql"
    path.parent.mkdir()
    path.write_text(
        "{% macro postgres__dex_unique_combination(model, columns) %}\n"
        "select 0 as dex_duplicate_rows where false\n{% endmacro %}\n",
        encoding="utf-8",
    )

    planned = _plan(_repo(tmp_path, "true"), capsys)
    assert planned["status"] == "ok", planned["errors"]
    assert "macros/dex_unique_combination.sql" in planned["data"]["paths"]
    main(["--repo-root", str(tmp_path), "transform", "apply"])
    applied = json.loads(capsys.readouterr().out)

    assert applied["status"] == "ok", applied
    merged = path.read_text(encoding="utf-8")
    assert merged.startswith("{% macro postgres__dex_unique_combination(")
    assert "{% test dex_unique_combination(model, columns) %}" in merged


def test_a_commented_out_definition_is_no_definition(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    stale = dbt_project_dir / "macros" / "old_tests.sql"
    stale.parent.mkdir()
    stale.write_text(
        "{# {% test dex_unique_combination(model, columns) %}\n"
        "select 1 {% endtest %} #}\n"
        "{% macro keep() %}1{% endmacro %}\n",
        encoding="utf-8",
    )
    # The validator reads the file as the one macro it is.
    validate_edit(
        PlanEdit(
            path="macros/old_tests.sql",
            kind=EditKind.MACRO_SQL,
            new_content=stale.read_text(encoding="utf-8"),
        )
    )

    planned = _plan(_repo(tmp_path, "true"), capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert "macros/dex_unique_combination.sql" in planned["data"]["paths"]


def test_transform_macro_lists_and_scaffolds_it(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- Elementary keeps nothing ------------------------------------------------------


@requires_elementary
def test_a_failing_run_leaves_no_row_in_test_result_rows(
    tmp_path: Path, installed_elementary: Path
):
    """#1452's last acceptance, on variant a, where Elementary's test
    materialization handles the tests and copies failing rows into
    ``test_result_rows``. The test returns a count, never a key, and carries
    ``disable_test_samples``, so a failing run adds no row there."""

    import duckdb

    project = elementary_project(tmp_path, "a", installed_elementary)
    (project / "macros" / f"{NAME}.sql").write_text(_asset(), encoding="utf-8")
    schema = project / "models" / "schema.yml"
    [declaration] = parse_declarations([{"model": "fct", "grain": ["id", "band"]}])
    schema.write_text(
        render(schema.read_text(encoding="utf-8"), declaration, render_tests=True),
        encoding="utf-8",
    )
    warehouse = tmp_path / "dev.duckdb"
    con = duckdb.connect(str(warehouse))
    try:
        con.execute(
            "insert into main.raw_orders select * from main.raw_orders where id = 1"
        )
    finally:
        con.close()
    args = ("--project-dir", str(project), "--profiles-dir", str(project))
    dbt("run", *args, cwd=project)

    rows = (ELEMENTARY_SCHEMA, "test_result_rows", "BASE TABLE")
    run_results = project / "target" / "run_results.json"

    def sampled() -> int:
        before = duckdb_census(warehouse)
        run_results.unlink(missing_ok=True)
        log = dbt(
            "test", *args, "--select", f"test_name:{NAME}", cwd=project, check=False
        )
        [result] = [
            r
            for r in json.loads(run_results.read_text(encoding="utf-8"))["results"]
            if r["unique_id"].startswith("test.")
        ]
        assert (result["status"], result["failures"]) == ("fail", 1), log
        was, now = footprint(before, duckdb_census(warehouse)).get(rows, (0, 0))
        return now - was

    assert sampled() == 0
    # The contrast arm: the flag stripped, Elementary keeps the one row the test
    # returns, which is the duplicate count and not a key, so the census can see
    # a sample when there is one.
    text = schema.read_text(encoding="utf-8")
    schema.write_text(
        text.replace("disable_test_samples: true", "disable_test_samples: false"),
        encoding="utf-8",
    )
    assert sampled() == 1
