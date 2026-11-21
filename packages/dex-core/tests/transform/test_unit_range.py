"""``dex_unit_range``: the generated test for a column declared a fraction (#1455).

A column declared ``unit: fraction`` renders this column test, with
``declarations.render_tests`` on: it fails with the count of non-NULL values
below 0 or above 1. A column declared ``unit: percent`` renders nothing and is
listed ``heuristic_only``: ``maintain verify``'s percent check flags a column
whose every value sits within 0 to 1, which correct small percentages do too,
so it stays a question rather than a test that fails a run. Whether the test
agrees with ``maintain verify``'s ``unit_out_of_range`` is the parity harness's
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
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.UNIT_RANGE


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


def _declared(**columns: str) -> dict:
    return {
        "model": "fct_orders",
        "columns": {name: {"unit": unit} for name, unit in columns.items()},
    }


FRACTION = _declared(discount_rate="fraction")
PERCENT = _declared(margin_pct="percent")


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_an_every_run_test_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.EVERY_RUN
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


def test_a_fraction_renders_one_count_only_test_under_its_column():
    content = _render(FRACTION, True)

    [test] = _column_tests(content, "discount_rate")
    assert test == {
        NAME: {
            "config": {
                "tags": [gt.EVERY_RUN],
                "meta": {
                    "disable_test_samples": True,
                    "dex": {
                        "claim": claim_id(
                            "fct_orders",
                            "unit",
                            ["discount_rate"],
                            {"check": NAME, "unit": "fraction"},
                        )
                    },
                },
            },
        }
    }


def test_the_unit_is_read_as_maintain_verify_reads_it():
    content = _render(_declared(discount_rate=" Fraction "), True)
    assert len(_column_tests(content, "discount_rate")) == 1


@pytest.mark.parametrize("unit", ["count", "currency:USD", "days"])
def test_a_unit_with_no_range_renders_nothing(unit):
    generated: list = []
    content = _render(_declared(amount=unit), True, generated=generated)
    assert NAME not in content
    assert generated == []


def test_a_percent_renders_nothing_and_says_why():
    generated: list = []
    content = _render(PERCENT, True, generated=generated)

    assert NAME not in content
    assert [
        (g["test"], g["column"], g["variant"], g["status"], g["reason"])
        for g in generated
    ] == [(NAME, "margin_pct", "percent", "not_rendered", gt.HEURISTIC_ONLY)]


def test_each_fraction_is_its_own_test_and_claim_and_a_percent_beside_it_is_not():
    generated: list = []
    content = _render(
        _declared(discount_rate="fraction", share="fraction", margin_pct="percent"),
        True,
        generated=generated,
    )

    claims = {
        t[NAME]["config"]["meta"]["dex"]["claim"]
        for column in ("discount_rate", "share")
        for t in _column_tests(content, column)
    }
    assert len(claims) == 2
    assert sorted((g["column"], g["status"]) for g in generated) == [
        ("discount_rate", "added"),
        ("margin_pct", "not_rendered"),
        ("share", "added"),
    ]


def test_rendering_again_adds_nothing():
    once = _render(FRACTION, True)
    generated: list = []
    twice = _render(FRACTION, True, content=once, generated=generated)

    assert twice == once
    assert [g["status"] for g in generated] == ["already_listed"]


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(FRACTION, False, generated=generated)

    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "render_tests_off")
    ]


@pytest.mark.parametrize("setting", [[NAME], [gt.EVERY_RUN]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert NAME in _render(FRACTION, setting)


def test_a_list_naming_another_kind_leaves_it_off():
    generated: list = []
    content = _render(FRACTION, [gt.UNIQUE_COMBINATION], generated=generated)

    assert NAME not in content
    assert [g["reason"] for g in generated] == ["kind_not_enabled"]


def test_a_column_the_model_provably_does_not_output_renders_nothing():
    generated: list = []
    content = _render(
        FRACTION, True, generated=generated, outputs=frozenset({"order_id"})
    )

    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", gt.COLUMN_NOT_OUTPUT)
    ]


# --- the macro file ---------------------------------------------------------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


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


def _plan(tmp_path: Path, capsys, sql: str, declaration: dict) -> dict:
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


ORDERS_SQL = "select 1 as order_id, 0.25 as discount_rate, 12.5 as margin_pct\n"


def test_the_plan_that_renders_it_writes_its_macro_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, ORDERS_SQL, FRACTION)

    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    assert planned["data"]["generated_tests"]["counts"] == {"added": 1}


def test_a_plan_declaring_only_a_percent_writes_no_macro_and_says_why(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, ORDERS_SQL, PERCENT)

    assert planned["status"] == "ok", planned["errors"]
    assert not any(p.startswith("macros/") for p in planned["data"]["paths"])
    [row] = planned["data"]["generated_tests"]["tests"]
    assert (row["status"], row["reason"]) == ("not_rendered", gt.HEURISTIC_ONLY)


def test_transform_macro_lists_it(dbt_project_dir: Path, tmp_path: Path, capsys):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- what it compiles to, against DuckDB --------------------------------------------


def _failures(tmp_path: Path, select: str) -> tuple[str, int, str]:
    """Build ``select`` as a view, run the shipped test on its ``share`` column,
    and return dbt's status, failure count and compiled SQL."""

    import subprocess
    import sys

    pytest.importorskip("dbt.adapters.duckdb")
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
                        "columns": [{"name": "share", "data_tests": [NAME]}],
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


def _shares(*values: str) -> str:
    first, *rest = values
    return " ".join(
        [f"select {first} as share", *(f"union all select {v}" for v in rest)]
    )


def test_values_outside_0_to_1_fail_with_their_count_and_never_a_null(
    tmp_path: Path,
):
    status, failures, sql = _failures(
        tmp_path, _shares("0.5", "1.5", "-0.2", "cast(null as double)", "1")
    )
    assert (status, failures) == ("fail", 2)
    assert "share" not in sql.split("from", 1)[0]  # a count, never a value


def test_values_within_0_to_1_pass_bounds_included(tmp_path: Path):
    assert _failures(tmp_path, _shares("0", "0.5", "1", "cast(null as double)"))[
        :2
    ] == ("pass", 0)


def test_a_column_holding_only_nulls_passes(tmp_path: Path):
    assert _failures(tmp_path, _shares("cast(null as double)"))[:2] == ("pass", 0)


def test_a_text_column_is_not_checked_as_maintain_verify_does_not(tmp_path: Path):
    status, failures, sql = _failures(tmp_path, _shares("'12'", "'x'"))
    assert (status, failures) == ("pass", 0)
    assert "1 = 0" in sql


def test_a_decimal_with_a_precision_is_checked(tmp_path: Path):
    # dbt's Column.is_number() reads DECIMAL(2,1) as no number; the test must not.
    status, failures, sql = _failures(tmp_path, _shares("0.5", "1.5", "-0.2"))
    assert (status, failures) == ("fail", 2)
    assert "1 = 0" not in sql


def test_a_date_column_is_not_checked(tmp_path: Path):
    assert _failures(tmp_path, _shares("date '2026-01-01'"))[:2] == ("pass", 0)


def test_a_uuid_column_is_not_compared(tmp_path: Path):
    # Not a number as maintain verify reads one, so neither side checks it.
    status, failures, sql = _failures(tmp_path, _shares("uuid()"))
    assert (status, failures) == ("pass", 0)
    assert "1 = 0" in sql
