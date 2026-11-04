"""`transform test --scaffold` on an incremental model (#1111).

An incremental model is two programs: the full build and the next incremental
run. A full build, and so a ``--full-refresh``, only ever runs the first. These
tests pin the scaffold that gives the second program a test of its own: a
``{% if is_incremental() %}`` branch is read, a test with dbt's
``is_incremental`` override and a ``this`` fixture is planned beside the full
one, and where the branch compares a column with a subquery over ``{{ this }}``
the fixture rows sit before, on and after that watermark. The end-to-end test
fills in the expectation and shows dbt catching a strict ``>`` watermark that a
``--full-refresh`` build passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlglot
import yaml

from exmergo_dex_core.cli import main
from exmergo_dex_core.transform.test_scaffold import (
    THIS_PLACEHOLDER,
    _incremental_variants,
    _watermark,
)

_STRICT = (
    "{{ config(materialized='incremental') }}\n"
    "select scan_id, hub, scanned_at\n"
    "from {{ source('wh', 'scans') }}\n"
    "{%- if is_incremental() %}\n"
    "where scanned_at > (select max(scanned_at) from {{ this }})\n"
    "{%- endif %}\n"
)


# --- reading the two programs ---------------------------------------------------


def test_the_full_build_drops_the_branch_and_the_incremental_run_keeps_it():
    variants = _incremental_variants(_STRICT)

    assert variants is not None
    assert "where" not in variants.full
    bound = f"(select max(scanned_at) from {THIS_PLACEHOLDER})"  # noqa: S608
    assert bound in variants.incremental
    assert "{{ source('wh', 'scans') }}" in variants.incremental


def test_an_else_branch_belongs_to_the_full_build_only():
    sql = (
        "select id, d from {{ ref('events') }}\n"
        "{% if is_incremental() %}where d >= (select max(d) from {{ this }})\n"
        "{% else %}where d >= '2020-01-01'\n"
        "{% endif %}"
    )

    variants = _incremental_variants(sql)

    assert variants is not None
    assert "'2020-01-01'" in variants.full and THIS_PLACEHOLDER not in variants.full
    assert "'2020-01-01'" not in variants.incremental


@pytest.mark.parametrize(
    "sql",
    [
        # any other control flow is left to the render's own refusal
        "select 1 as x from {{ ref('a') }} {% if var('flag') %}where x = 1{% endif %}",
        "select 1 as x from {{ ref('a') }} {% for c in [1] %}{% endfor %}",
        # nesting inside the branch
        "select 1 as x from {{ ref('a') }} {% if is_incremental() %}"
        "{% if var('f') %}where x = 1{% endif %}{% endif %}",
        # {{ this }} in the full build has nothing to fixture
        "select 1 as x from {{ this }} {% if is_incremental() %}where 1=1{% endif %}",
        # an unclosed branch
        "select 1 as x from {{ ref('a') }} {% if is_incremental() %}where x = 1",
        # no jinja statement at all: the existing single-test path
        "select 1 as x from {{ ref('a') }}",
    ],
)
def test_only_a_flat_is_incremental_branch_is_read(sql: str):
    assert _incremental_variants(sql) is None


@pytest.mark.parametrize(
    ("predicate", "operator"),
    [
        ("d > (select max(d) from __dex_this)", ">"),
        ("d >= (select max(d) from __dex_this)", ">="),
        ("(select max(d) from __dex_this) < d", ">"),
        ("d >= coalesce((select max(d) from __dex_this), '1900-01-01')", ">="),
    ],
)
def test_the_watermark_comparison_is_read_with_the_column_on_the_left(
    predicate: str, operator: str
):
    parsed = sqlglot.parse_one(f"select id, d from events where {predicate}")  # noqa: S608

    watermark = _watermark(parsed, {"events": {"id", "d"}})

    assert watermark is not None
    assert (watermark.column, watermark.operator) == ("d", operator)
    assert watermark.input == "events"
    assert watermark.this_column == "d"


def test_a_branch_with_no_subquery_over_this_has_no_watermark():
    parsed = sqlglot.parse_one(
        "select id, d from events where d > current_date - interval 3 day"
    )

    assert _watermark(parsed, {"events": {"id", "d"}}) is None


def test_two_different_watermarks_are_not_one_boundary():
    parsed = sqlglot.parse_one(
        "select id, d, u from events "
        "where d > (select max(d) from __dex_this) "
        "and u > (select max(u) from __dex_this)"
    )

    assert _watermark(parsed, {"events": {"id", "d", "u"}}) is None


# --- the command, end to end on DuckDB ------------------------------------------


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    rc = main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


@pytest.fixture
def scans_repo(tmp_path: Path) -> Path:
    """A repository whose one dbt model is an incremental over a raw table.

    The dev target is the warehouse file itself, which is what Dex's dev
    preflight asks of a DuckDB project.
    """

    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(tmp_path / "wh.duckdb"))
    conn.execute(
        "create table scans (scan_id integer, hub varchar, scanned_at timestamp)"
    )
    conn.execute(
        "insert into scans values (1, 'a', '2024-01-01 10:00'), "
        "(2, 'b', '2024-01-02 10:00'), (3, 'c', null)"
    )
    conn.close()
    project = tmp_path / "proj"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text(
        'name: scans\nversion: "1.0.0"\nprofile: scans\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "scans:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'wh.duckdb'}\n",
        encoding="utf-8",
    )
    (project / "models" / "_sources.yml").write_text(
        "version: 2\nsources:\n  - name: wh\n    schema: main\n"
        "    tables:\n      - name: scans\n",
        encoding="utf-8",
    )
    (project / "models" / "fct_scans.sql").write_text(_STRICT, encoding="utf-8")
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'wh.duckdb'}\n",
        encoding="utf-8",
    )
    return tmp_path


def _scaffold(repo: Path, capsys) -> dict:
    rc, envelope = _run(
        ["--repo-root", str(repo), "transform", "test", "--scaffold", "fct_scans"],
        capsys,
    )
    assert rc == 0, envelope
    return envelope


@pytest.fixture
def mapped(scans_repo: Path, capsys) -> Path:
    rc, envelope = _run(["--repo-root", str(scans_repo), "explore", "map"], capsys)
    assert rc == 0, envelope
    return scans_repo


def test_an_incremental_model_scaffolds_a_test_per_program(mapped: Path, capsys):
    pytest.importorskip("dbt.cli.main")
    envelope = _scaffold(mapped, capsys)

    data = envelope["data"]
    assert data["incremental"] == {
        "tests": ["test_fct_scans", "test_fct_scans_incremental"],
        "watermark": {
            "column": "scanned_at",
            "operator": ">",
            "input": "scans",
            "this_column": "scanned_at",
        },
    }
    assert any(
        "--full-refresh run only its full program" in w for w in envelope["warnings"]
    )

    plan = json.loads(
        (mapped / ".dex" / "plans" / f"{data['plan_id']}.json").read_text()
    )
    content = next(e for e in plan["edits"] if e["path"].endswith(".yml"))
    tests = yaml.safe_load(content["new_content"])["unit_tests"]
    full, incremental = tests
    # dbt refuses a unit test on an incremental model with no override, so the
    # full-build test states `false`
    assert full["overrides"] == {"macros": {"is_incremental": False}}
    assert incremental["overrides"] == {"macros": {"is_incremental": True}}

    source, this = incremental["given"]
    assert [row["scanned_at"] for row in source["rows"]] == [
        "2024-01-01 00:00:00",
        "2024-01-02 00:00:00",
        "2024-01-03 00:00:00",
        None,
    ]
    # distinct keys, so a dedup on the key cannot collapse the boundary rows
    assert len({row["scan_id"] for row in source["rows"]}) == 4
    # `this` is typed SQL: dbt would otherwise read its columns from a relation
    # a cold dev target does not have
    assert this["input"] == "this" and this["format"] == "sql"
    assert "cast('2024-01-02 00:00:00' as TIMESTAMP) as scanned_at" in this["rows"]
    assert "exactly on the current watermark" in content["new_content"]


def test_a_this_column_no_input_passes_through_is_typed_from_the_cache_or_refused(
    mapped: Path, capsys
):
    pytest.importorskip("dbt.cli.main")
    model = mapped / "proj" / "models" / "fct_scans.sql"
    model.write_text(
        _STRICT.replace(
            "select max(scanned_at) from {{ this }}",
            "select max(loaded_at) from {{ this }}",
        ),
        encoding="utf-8",
    )

    rc, envelope = _run(
        ["--repo-root", str(mapped), "transform", "test", "--scaffold", "fct_scans"],
        capsys,
    )

    assert rc != 0
    assert envelope["status"] == "error"
    message = envelope["errors"][0]
    assert "loaded_at" in message
    assert "explore profile <dev namespace>.fct_scans --target dev" in message


def test_the_incremental_test_catches_a_strict_watermark_a_full_refresh_passes(
    mapped: Path, capsys
):
    """The two-program proof: with its expectation filled in, the incremental
    test fails on a strict ``>`` (the tie at the watermark is skipped) while the
    full-build test passes, and both pass once the comparison is ``>=``."""

    pytest.importorskip("dbt.cli.main")
    envelope = _scaffold(mapped, capsys)
    rc, applied = _run(
        ["--repo-root", str(mapped), "transform", "apply", envelope["data"]["plan_id"]],
        capsys,
    )
    assert rc == 0, applied

    path = mapped / "proj" / "models" / "test_fct_scans.yml"
    full_stub, incremental_stub, tail = path.read_text().split("        - {}\n")
    path.write_text(
        full_stub
        + '        - {hub: "example", scan_id: 1, scanned_at: "2024-01-01 00:00:00"}\n'
        + incremental_stub
        + "        - {scan_id: 2}\n        - {scan_id: 3}\n"
        + tail,
        encoding="utf-8",
    )

    last: dict = {}

    def build() -> dict[str, str]:
        _rc, built = _run(
            [
                "--repo-root",
                str(mapped),
                "transform",
                "build",
                "--target",
                "dev",
                "--select",
                "fct_scans",
                "--full-refresh",
            ],
            capsys,
        )
        last.clear()
        last.update(built)
        return {n["name"]: n["status"] for n in built["data"]["nodes"]}

    strict = build()
    # the full-build test passes, or is skipped once its sibling has failed
    assert strict["test_fct_scans"] in ("pass", "skipped")
    assert strict["test_fct_scans_incremental"] == "fail"
    assert strict["fct_scans"] == "skipped"
    # the failure names itself: a unit test's message opens with blank lines
    # and dbt colors its diff, which left `errors[0]` reading "dbt build failed: "
    assert "actual differs from expected" in last["errors"][0]
    blocker = last["data"]["completion"]["blockers"][0]
    assert blocker["message"].startswith("actual differs from expected")
    assert "\x1b" not in json.dumps(last["data"])

    model = mapped / "proj" / "models" / "fct_scans.sql"
    model.write_text(_STRICT.replace("scanned_at >", "scanned_at >="), encoding="utf-8")
    assert build() == {
        "test_fct_scans": "pass",
        "test_fct_scans_incremental": "pass",
        "fct_scans": "success",
    }


def test_incremental_model_without_branch_scaffolds_false_override(
    mapped: Path, capsys
):
    """An incremental model without an is_incremental() branch scaffolds ONE
    test with an is_incremental: false override (#1131).

    dbt requires `overrides: macros: is_incremental: false` for unit tests on
    any model materialized as incremental, even when the model body does not
    contain an is_incremental() branch.
    """
    pytest.importorskip("dbt.cli.main")
    model = mapped / "proj" / "models" / "fct_scans.sql"
    model.write_text(
        "{{ config(materialized='incremental') }}\n"
        "select scan_id, hub from {{ source('wh', 'scans') }}\n",
        encoding="utf-8",
    )

    envelope = _scaffold(mapped, capsys)

    data = envelope["data"]
    assert data.get("incremental") is None

    plan = json.loads(
        (mapped / ".dex" / "plans" / f"{data['plan_id']}.json").read_text()
    )
    content = next(e for e in plan["edits"] if e["path"].endswith(".yml"))
    tests = yaml.safe_load(content["new_content"])["unit_tests"]
    assert len(tests) == 1
    (test,) = tests
    assert test["name"] == "test_fct_scans"
    assert test.get("overrides") == {"macros": {"is_incremental": False}}


def test_project_level_incremental_model_without_branch_scaffolds_false_override(
    mapped: Path, capsys
):
    """A model materialized as incremental via dbt_project.yml without an
    is_incremental() branch scaffolds ONE test with is_incremental: false (#1131).
    """
    pytest.importorskip("dbt.cli.main")
    dbt_project = mapped / "proj" / "dbt_project.yml"
    dbt_project.write_text(
        'name: scans\nversion: "1.0.0"\nprofile: scans\nmodel-paths: ["models"]\n'
        "models:\n"
        "  scans:\n"
        "    +materialized: incremental\n",
        encoding="utf-8",
    )
    model = mapped / "proj" / "models" / "fct_scans.sql"
    model.write_text(
        "select scan_id, hub from {{ source('wh', 'scans') }}\n",
        encoding="utf-8",
    )

    envelope = _scaffold(mapped, capsys)

    data = envelope["data"]
    assert data.get("incremental") is None

    plan = json.loads(
        (mapped / ".dex" / "plans" / f"{data['plan_id']}.json").read_text()
    )
    content = next(e for e in plan["edits"] if e["path"].endswith(".yml"))
    tests = yaml.safe_load(content["new_content"])["unit_tests"]
    assert len(tests) == 1
    (test,) = tests
    assert test["name"] == "test_fct_scans"
    assert test.get("overrides") == {"macros": {"is_incremental": False}}


@pytest.mark.parametrize(
    "config_line",
    [
        "{{ config(materialized='table') }}\n",
        "{{ config(materialized='view') }}\n",
        "",
    ],
)
def test_table_and_view_models_do_not_get_incremental_override(
    mapped: Path, capsys, config_line: str
):
    """Table and view models do not get an is_incremental override (#1131)."""
    pytest.importorskip("dbt.cli.main")
    model = mapped / "proj" / "models" / "fct_scans.sql"
    model.write_text(
        config_line + "select scan_id, hub from {{ source('wh', 'scans') }}\n",  # noqa: S608
        encoding="utf-8",
    )

    envelope = _scaffold(mapped, capsys)

    plan = json.loads(
        (mapped / ".dex" / "plans" / f"{envelope['data']['plan_id']}.json").read_text()
    )
    content = next(e for e in plan["edits"] if e["path"].endswith(".yml"))
    tests = yaml.safe_load(content["new_content"])["unit_tests"]
    assert len(tests) == 1
    (test,) = tests
    assert "overrides" not in test

