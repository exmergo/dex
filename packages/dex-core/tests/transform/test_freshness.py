"""``freshness`` on a declared time column, and ``dex_freshness`` (#1460).

A column with role ``time`` may declare how old its newest value may be,
``freshness: {max_lag, basis}``. With ``declarations.render_tests`` on it renders
a count-only column test that fails when the newest value trails the
warehouse's clock by more than ``max_lag``, or when the column holds no value.
Whether the test agrees with ``maintain verify``'s ``freshness_exceeded`` is the
parity harness's question (``test_generated_parity.py``); these are the field,
the rendering, the gating, the file it writes, the macro's expressions against
verify's, and the SQL it compiles to on DuckDB.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.maintain import freshness as freshness_mod
from exmergo_dex_core.semantic_declarations import (
    DeclarationError,
    lag_seconds,
    normalized_lag,
)
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.claims import claim_id
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.FRESHNESS


def _declared(max_lag="6h", basis="capture", role="time", column="loaded_at") -> dict:
    fields: dict = {"freshness": {"max_lag": max_lag, "basis": basis}}
    if role is not None:
        fields["role"] = role
    return {"model": "fct_orders", "columns": {column: fields}}


FRESH = _declared()


def _render(payload: dict, render_tests, *, generated=None, outputs=None, content=None):
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        render_tests=render_tests,
        generated=generated,
        outputs=outputs,
    )


def _column(content: str, column: str = "loaded_at") -> dict:
    entry = next(
        e for e in yaml.safe_load(content)["models"] if e["name"] == "fct_orders"
    )
    return next(c for c in entry.get("columns") or [] if c["name"] == column)


def _tests(content: str, column: str = "loaded_at") -> list:
    return [
        t
        for t in _column(content, column).get("data_tests") or []
        if isinstance(t, dict) and NAME in t
    ]


# --- the field ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "seconds", "normalized"),
    [
        ("30s", 30, "30s"),
        ("90s", 90, "90s"),
        ("120s", 120, "2m"),
        ("360m", 21600, "6h"),
        ("6h", 21600, "6h"),
        (" 6H ", 21600, "6h"),
        ("48h", 172800, "2d"),
        ("14d", 1209600, "2w"),
        ("1w", 604800, "1w"),
    ],
)
def test_a_lag_is_read_in_seconds_and_written_in_its_largest_unit(
    written, seconds, normalized
):
    assert lag_seconds(written) == seconds
    assert normalized_lag(seconds) == normalized
    [declaration] = parse_declarations([_declared(max_lag=written)])
    freshness = declaration.columns["loaded_at"].freshness
    assert freshness is not None
    assert (freshness.max_lag, freshness.max_lag_seconds) == (normalized, seconds)


@pytest.mark.parametrize("bad", ["6", "6 hours", "1.5h", "0h", "-1h", "6y", ""])
def test_a_lag_that_is_no_whole_number_and_unit_is_refused(bad):
    with pytest.raises(DeclarationError, match="max_lag"):
        parse_declarations([_declared(max_lag=bad)])


def test_a_lag_written_as_a_number_is_refused():
    with pytest.raises(DeclarationError, match="max_lag"):
        parse_declarations([_declared(max_lag=6)])


@pytest.mark.parametrize("basis", [None, "loaded", "event"])
def test_a_basis_other_than_business_or_capture_is_refused(basis):
    payload = _declared()
    if basis is None:
        del payload["columns"]["loaded_at"]["freshness"]["basis"]
    else:
        payload["columns"]["loaded_at"]["freshness"]["basis"] = basis
    with pytest.raises(DeclarationError, match="basis"):
        parse_declarations([payload])


def test_an_unknown_freshness_key_is_refused():
    payload = _declared()
    payload["columns"]["loaded_at"]["freshness"]["warn_after"] = "1h"
    with pytest.raises(DeclarationError, match="not a declaration field"):
        parse_declarations([payload])


def test_it_renders_under_the_columns_dex_meta_normalized():
    content = _render(_declared(max_lag="360m"), False)
    assert _column(content)["config"]["meta"]["dex"] == {
        "role": "time",
        "freshness": {"max_lag": "6h", "basis": "capture"},
    }


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_an_every_run_test_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.EVERY_RUN
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


def test_a_freshness_renders_one_count_only_test_under_its_column():
    [test] = _tests(_render(FRESH, True))
    assert test == {
        NAME: {
            "arguments": {"max_lag_seconds": 21600},
            "config": {
                "tags": [gt.EVERY_RUN],
                "meta": {
                    "disable_test_samples": True,
                    "dex": {
                        "claim": claim_id(
                            "fct_orders",
                            "freshness",
                            ["loaded_at"],
                            {
                                "check": NAME,
                                "max_lag_seconds": 21600,
                                "basis": "capture",
                            },
                        )
                    },
                },
            },
        }
    }


def test_one_lag_written_two_ways_is_one_claim_and_a_basis_is_another():
    def claim(payload):
        [test] = _tests(_render(payload, True))
        return test[NAME]["config"]["meta"]["dex"]["claim"]

    assert claim(_declared(max_lag="6h")) == claim(_declared(max_lag="360m"))
    assert claim(_declared(basis="capture")) != claim(_declared(basis="business"))


def test_the_generated_row_names_its_basis():
    generated: list = []
    _render(_declared(basis="business"), True, generated=generated)
    assert [(g["test"], g["column"], g["variant"], g["status"]) for g in generated] == [
        (NAME, "loaded_at", "business", "added")
    ]


def test_rendering_again_adds_nothing():
    once = _render(FRESH, True)
    generated: list = []
    assert _render(FRESH, True, content=once, generated=generated) == once
    assert [g["status"] for g in generated] == ["already_listed"]


def test_a_changed_lag_reports_the_old_test_stale_and_keeps_it():
    once = _render(FRESH, True)
    generated: list = []
    notes: list = []
    [declaration] = parse_declarations([_declared(max_lag="2d")])
    twice = render(
        once, declaration, render_tests=True, generated=generated, notes=notes
    )

    # Nothing generated is ever removed (#1449): the old test stays listed, and
    # the plan says it no longer matches the declaration.
    assert len(_tests(twice)) == 2
    assert sorted((g["status"], g.get("reason")) for g in generated) == [
        ("added", None),
        ("stale", "freshness_changed"),
    ]
    [stale] = [g for g in generated if g["status"] == "stale"]
    assert stale["column"] == "loaded_at"
    assert any("no longer declares" in note for note in notes)


def test_a_basis_only_change_keeps_the_listed_test_and_reports_nothing_stale():
    # The same lag is the same test (the basis is no argument), so the listed
    # test is kept, as rendering keeps it, and is not called stale.
    once = _render(FRESH, True)
    generated: list = []
    twice = _render(
        _declared(basis="business"), True, content=once, generated=generated
    )
    assert len(_tests(twice)) == 1
    assert [g["status"] for g in generated] == ["already_listed"]


def test_an_unchanged_freshness_reports_nothing_stale():
    once = _render(FRESH, True)
    generated: list = []
    _render(_declared(max_lag="360m"), True, content=once, generated=generated)
    assert [g["status"] for g in generated] == ["already_listed"]


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(FRESH, False, generated=generated)
    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "render_tests_off")
    ]


@pytest.mark.parametrize("setting", [[NAME], [gt.EVERY_RUN]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert NAME in _render(FRESH, setting)


def test_a_column_the_model_provably_does_not_output_renders_nothing():
    generated: list = []
    content = _render(FRESH, True, generated=generated, outputs=frozenset({"order_id"}))
    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", gt.COLUMN_NOT_OUTPUT)
    ]


# --- the plan ---------------------------------------------------------------------


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


ORDERS_SQL = "select 1 as order_id, current_timestamp as loaded_at\n"


def _plan(tmp_path: Path, capsys, declaration: dict, sql: str = ORDERS_SQL) -> dict:
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


def test_the_plan_that_renders_it_writes_its_macro_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, FRESH)
    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    assert planned["data"]["generated_tests"]["counts"] == {"added": 1}


@pytest.mark.parametrize("role", ["dimension", None])
def test_a_freshness_off_the_time_column_is_refused_naming_the_fix(
    dbt_project_dir: Path, tmp_path: Path, capsys, role
):
    planned = _plan(_repo(tmp_path, "false"), capsys, _declared(role=role))
    assert planned["status"] == "error"
    message = " ".join(str(e) for e in planned["errors"])
    assert "role `time`" in message
    assert "fct_orders.loaded_at" in message


def test_a_new_role_on_a_column_that_keeps_its_freshness_is_refused(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path, "false")
    planned = _plan(repo, capsys, FRESH)
    assert planned["status"] == "ok", planned["errors"]
    main(["--repo-root", str(repo), "transform", "apply", planned["data"]["plan_id"]])
    capsys.readouterr()

    moved = {"model": "fct_orders", "columns": {"loaded_at": {"role": "dimension"}}}
    refused = _plan(repo, capsys, moved)
    assert refused["status"] == "error"
    assert "already declares freshness" in " ".join(map(str, refused["errors"]))


def test_transform_macro_lists_it(dbt_project_dir: Path, tmp_path: Path, capsys):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- the macro file, and its expressions against maintain verify's ----------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


def test_the_shipped_macro_passes_the_macro_file_check():
    validate_edit(
        PlanEdit(
            path=f"macros/{NAME}.sql", kind=EditKind.MACRO_SQL, new_content=_asset()
        )
    )


class _ReturnedError(Exception):
    def __init__(self, value):
        self.value = value


def _macros(target_type: str):
    """The asset's macros under plain Jinja, with dbt's ``return`` and
    ``target`` stood in, so each dispatch can be rendered without dbt."""

    import jinja2

    def _return(value):
        raise _ReturnedError(value)

    text = re.sub(r"\{% test .*?\{% endtest %\}", "", _asset(), flags=re.DOTALL)
    env = jinja2.Environment(autoescape=False, extensions=["jinja2.ext.do"])  # noqa: S701
    env.globals.update({"return": _return, "target": {"type": target_type}})
    return env.from_string(text).module


def _call(module, name: str, *args):
    try:
        return str(getattr(module, name)(*args))
    except _ReturnedError as returned:
        return returned.value


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


DIALECTS = (
    "duckdb",
    "postgres",
    "redshift",
    "bigquery",
    "snowflake",
    "databricks",
    "clickhouse",
    "trino",
)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("kind", ["date", "ntz", "tz", "unknown"])
def test_each_dialects_epoch_is_the_expression_maintain_verify_sends(dialect, kind):
    module = _macros(dialect)
    prefix = "default" if dialect == "duckdb" else dialect
    expression = 'max("loaded_at")'
    assert _norm(_call(module, f"{prefix}__dex_freshness_epoch", expression, kind)) == (
        _norm(freshness_mod.epoch_sql(dialect, expression, kind))
    )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_each_dialects_clock_is_the_one_maintain_verify_reads(dialect):
    module = _macros(dialect)
    prefix = "default" if dialect == "duckdb" else dialect
    assert _norm(_call(module, f"{prefix}__dex_freshness_now")) == _norm(
        freshness_mod.now_sql(dialect)
    )


@pytest.mark.parametrize(
    ("dialect", "dtype"),
    [
        ("duckdb", "TIMESTAMP"),
        ("duckdb", "TIMESTAMP WITH TIME ZONE"),
        ("postgres", "timestamp without time zone"),
        ("bigquery", "DATETIME"),
        ("bigquery", "TIMESTAMP"),
        ("bigquery", "DATE"),
        ("snowflake", "TIMESTAMP_NTZ"),
        ("snowflake", "TIMESTAMP_LTZ"),
        ("databricks", "timestamp"),
        ("databricks", "timestamp_ntz"),
        ("clickhouse", "Nullable(DateTime64(3))"),
        ("trino", "timestamp(6)"),
        ("trino", "timestamp(3) with time zone"),
        ("duckdb", "VARCHAR"),
        ("duckdb", "TIME"),
        ("duckdb", "INTERVAL"),
    ],
)
def test_the_macro_reads_a_type_as_maintain_verify_does(dialect, dtype):
    assert _call(_macros(dialect), "dex_freshness_kind", dtype) == (
        freshness_mod.temporal_kind(dialect, dtype)
    )


# --- what it compiles to, against DuckDB --------------------------------------------


def _run(tmp_path: Path, select: str, *, view: bool = False, lag: int = 21600):
    """Build ``select`` (a view where asked), run the shipped test on its
    ``loaded_at`` column, and return dbt's status, failures and compiled SQL."""

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
    materialized = "view" if view else "table"
    (project / "models" / "m.sql").write_text(
        f"{{{{ config(materialized='{materialized}') }}}}\n{select}\n", encoding="utf-8"
    )
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "m",
                        "columns": [
                            {
                                "name": "loaded_at",
                                "data_tests": [
                                    {NAME: {"arguments": {"max_lag_seconds": lag}}}
                                ],
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
    compiled = list((project / "target" / "compiled").rglob(f"{NAME}*.sql"))
    return (
        result["status"],
        int(result["failures"] or 0),
        compiled[0].read_text() if compiled else "",
    )


def test_a_stale_column_fails_once_returning_the_lag_and_never_a_value(tmp_path: Path):
    status, failures, sql = _run(
        tmp_path, "select timestamp '2000-01-01 00:00:00' as loaded_at"
    )
    assert (status, failures) == ("fail", 1)
    head = sql.split("from", 1)[0]
    assert "loaded_at" not in head  # the lag and counts, never the value


def test_a_view_exactly_at_its_lag_passes(tmp_path: Path):
    status, failures, _sql = _run(
        tmp_path,
        "select {{ dbt.dateadd('second', -21600, dbt.current_timestamp()) }} "
        "as loaded_at",
        view=True,
    )
    assert (status, failures) == ("pass", 0)


def test_a_view_one_second_past_its_lag_fails(tmp_path: Path):
    status, failures, _sql = _run(
        tmp_path,
        "select {{ dbt.dateadd('second', -21601, dbt.current_timestamp()) }} "
        "as loaded_at",
        view=True,
    )
    assert (status, failures) == ("fail", 1)


def test_a_column_well_inside_its_lag_passes(tmp_path: Path):
    assert _run(tmp_path, "select current_timestamp as loaded_at")[:2] == ("pass", 0)


def test_an_empty_model_fails(tmp_path: Path):
    assert _run(tmp_path, "select current_timestamp as loaded_at where 1 = 0")[:2] == (
        "fail",
        1,
    )


def test_a_column_null_on_every_row_fails(tmp_path: Path):
    assert _run(tmp_path, "select cast(null as timestamp) as loaded_at")[:2] == (
        "fail",
        1,
    )


def test_a_date_column_is_read_as_its_midnight(tmp_path: Path):
    assert _run(tmp_path, "select current_date as loaded_at", lag=2 * 86400)[:2] == (
        "pass",
        0,
    )


def test_a_text_column_errors_rather_than_passing(tmp_path: Path):
    assert _run(tmp_path, "select 'yesterday' as loaded_at")[0] == "error"
