"""``dex_join_coverage``: the generated test for a join a declaration requires (#1456).

A column carrying a model's join key that declares a ``references`` naming the
joined model makes the join required, and ``maintain verify`` holds it to full
coverage (``join_policy``). With ``declarations.render_tests`` on, an INNER join
so required renders this model-level test: it reads the join's two inputs, as
the plan read them from the model's SQL, and fails with the eligible driving
rows that find no parent row. A required LEFT join is listed
``covered_by_builtin`` (``relationships`` counts the same rows on the output), a
shape this release does not render ``join_shape_unsupported``, and SQL the plan
cannot read ``join_unread``. Whether the test agrees with ``maintain verify`` is
the parity harness's question (``test_generated_parity.py``); these are the
reading, the rendering, the gating, the file it writes and the SQL it compiles
to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.join_reading import (
    UNREAD_JINJA,
    UNREAD_NO_SQL,
    read_model_joins,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.JOIN_COVERAGE
MODEL = "fct_orders"
PATH = f"models/marts/{MODEL}.sql"

INNER = (
    "select o.order_id, o.customer_id, c.segment\n"
    "from {{ ref('stg_orders') }} o\n"
    "inner join {{ ref('dim_customers') }} c on o.customer_id = c.customer_id\n"
)
LEFT = INNER.replace("inner join", "left join")
FILTERED = (
    "{{ config(materialized='table') }}\n"
    "with orders as (\n"
    "    select * from {{ ref('stg_orders') }} where status <> 'cancelled'\n"
    "),\n"
    "customers as (select * from {{ ref('dim_customers') }} where is_active)\n"
    "select orders.order_id, orders.customer_id, customers.segment\n"
    "from orders\n"
    "join customers on orders.customer_id = customers.customer_id\n"
)
REFERENCES = {
    "model": MODEL,
    "columns": {"customer_id": {"references": "dim_customers.customer_id"}},
}


def _joins(sql: str | None, dialect: str | None = "duckdb"):
    files = {PATH: sql} if sql is not None else {}
    return read_model_joins(files, ["models"], MODEL, dialect)


def _render(
    payload: dict,
    render_tests,
    sql: str | None = INNER,
    *,
    generated=None,
    content=None,
    notes=None,
    dialect="duckdb",
) -> str:
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        render_tests=render_tests,
        generated=generated,
        dialect=dialect,
        notes=notes,
        joins=_joins(sql, dialect),
    )


def _model_tests(content: str) -> list:
    entry = next(e for e in yaml.safe_load(content)["models"] if e["name"] == MODEL)
    return [
        t for t in entry.get("data_tests") or [] if isinstance(t, dict) and NAME in t
    ]


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_a_scheduled_test_on_every_connector_reading_joins():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.SCHEDULED
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert kind.reads_joins
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


# --- the reading ------------------------------------------------------------------


def test_the_reading_maps_each_ref_and_source_to_its_call():
    joins = _joins(INNER + "left join {{ source('raw', 'x') }} x on x.id = o.order_id")
    assert joins.read
    assert sorted(joins.calls.values()) == [
        "ref('dim_customers')",
        "ref('stg_orders')",
        "source('raw', 'x')",
    ]
    assert set(joins.parents) == {"stg_orders", "dim_customers"}


@pytest.mark.parametrize(
    "sql",
    [
        "{% if is_incremental() %}select 1{% endif %}",
        "select * from {{ ref('dim', v=2) }}",
        "select {{ dbt_utils.star(ref('x')) }} from {{ ref('x') }}",
        "select * from {{ ref(var('x')) }}",
    ],
)
def test_jinja_other_than_ref_source_config_is_not_read(sql):
    joins = _joins(sql)
    assert (joins.read, joins.reason) == (False, UNREAD_JINJA)


def test_a_model_with_no_sql_is_not_read():
    joins = _joins(None)
    assert (joins.read, joins.reason) == (False, UNREAD_NO_SQL)


@pytest.mark.parametrize("dialect", ["duckdb", "snowflake", "bigquery", "postgres"])
def test_a_required_inner_join_is_read_through_its_filters(dialect):
    [declaration] = parse_declarations([REFERENCES])
    [join] = _joins(FILTERED, dialect).required(declaration)
    assert join.status == "render"
    assert (join.child, join.child_columns) == ("ref('stg_orders')", ("customer_id",))
    assert (join.parent, join.parent_columns) == (
        "ref('dim_customers')",
        ("customer_id",),
    )
    assert join.child_where and "status" in join.child_where
    assert join.child_where.count('"') == 0  # plain names unquoted
    assert join.parent_where == "is_active"
    assert join.null_keys == "excluded"


def test_a_join_to_a_relation_nothing_references_is_not_required():
    [declaration] = parse_declarations(
        [{"model": MODEL, "columns": {"customer_id": {"role": "dimension"}}}]
    )
    assert _joins(INNER).required(declaration) == []


# --- the rendering ----------------------------------------------------------------


def test_a_required_inner_join_renders_one_count_only_test_on_the_model():
    generated: list = []
    content = _render(REFERENCES, True, generated=generated)

    [test] = _model_tests(content)
    body = test[NAME]
    assert body["arguments"] == {
        "child": "ref('stg_orders')",
        "child_columns": ["customer_id"],
        "parent": "ref('dim_customers')",
        "parent_columns": ["customer_id"],
    }
    assert body["config"]["tags"] == [gt.SCHEDULED]
    assert body["config"]["meta"]["disable_test_samples"] is True
    [row] = [g for g in generated if g["test"] == NAME]
    assert (row["column"], row["status"]) == (None, "added")
    assert row["claim"] == body["config"]["meta"]["dex"]["claim"]


def test_the_filters_ride_along_as_arguments():
    content = _render(REFERENCES, True, FILTERED)
    [test] = _model_tests(content)
    arguments = test[NAME]["arguments"]
    assert arguments["child_where"] == "status <> 'cancelled'"
    assert arguments["parent_where"] == "is_active"


def test_a_driving_key_declared_never_null_counts_null_keys():
    payload = {
        "model": MODEL,
        "columns": {
            "customer_id": {
                "references": "dim_customers.customer_id",
                "null_rule": "never",
            }
        },
    }
    [test] = _model_tests(_render(payload, True))
    assert test[NAME]["arguments"]["null_keys"] == "unmatched"


def test_a_composite_key_renders_its_columns_in_order():
    sql = (
        "select l.order_id, l.line_no, l.sku\n"
        "from {{ ref('stg_lines') }} l\n"
        "join {{ ref('dim_lines') }} d\n"
        "  on l.order_id = d.order_id and l.line_no = d.line_no\n"
    )
    payload = {
        "model": MODEL,
        "columns": {"order_id": {"references": "dim_lines.order_id"}},
    }
    [test] = _model_tests(_render(payload, True, sql))
    arguments = test[NAME]["arguments"]
    assert arguments["child_columns"] == ["order_id", "line_no"]
    assert arguments["parent_columns"] == ["order_id", "line_no"]


def test_a_source_is_read_as_the_driving_input():
    sql = INNER.replace("{{ ref('stg_orders') }}", "{{ source('shop', 'orders') }}")
    [test] = _model_tests(_render(REFERENCES, True, sql))
    assert test[NAME]["arguments"]["child"] == "source('shop', 'orders')"


def test_a_left_join_to_a_filtered_parent_renders_the_test():
    # relationships reads the whole parent, so it misses a child whose only
    # parent row the model's filter drops; maintain verify counts it.
    sql = LEFT.replace(
        "{{ ref('dim_customers') }} c",
        "(select * from {{ ref('dim_customers') }} where is_active) c",
    )
    generated: list = []
    [test] = _model_tests(_render(REFERENCES, True, sql, generated=generated))
    assert test[NAME]["arguments"]["parent_where"] == "is_active"
    assert [g["status"] for g in generated if g["test"] == NAME] == ["added"]


def test_a_key_that_cannot_stand_unquoted_is_quoted_in_the_dialect():
    sql = (
        'select o.order_id, o."customer id", c.segment\n'
        "from {{ ref('stg_orders') }} o\n"
        "join {{ ref('dim_customers') }} c\n"
        '  on o."customer id" = c."customer id"\n'
    )
    payload = {
        "model": MODEL,
        "columns": {"customer id": {"references": "dim_customers.customer id"}},
    }
    for dialect, quoted in (("duckdb", '"customer id"'), ("bigquery", "`customer id`")):
        [declaration] = parse_declarations([payload])
        [join] = _joins(sql, dialect).required(declaration)
        assert join.child_columns == join.parent_columns == (quoted,)


@pytest.mark.parametrize(
    ("written", "rendered"), [('"CustId"', '"CustId"'), ("CustId", "CustId")]
)
def test_a_key_the_sql_quotes_keeps_its_quoting(written, rendered):
    # On Snowflake an unquoted CustId folds to CUSTID, another column than the
    # quoted "CustId" the model joins on; an unquoted one folds as the model's.
    sql = (
        f"select o.order_id, o.{written}, c.segment\n"
        "from {{ ref('stg_orders') }} o\n"
        f"join {{{{ ref('dim_customers') }}}} c on o.{written} = c.{written}\n"
    )
    payload = {
        "model": MODEL,
        "columns": {"CustId": {"references": "dim_customers.CustId"}},
    }
    [declaration] = parse_declarations([payload])
    [join] = _joins(sql, "snowflake").required(declaration)
    assert join.child_columns == join.parent_columns == (rendered,)


def test_counting_null_keys_is_a_different_claim_and_a_different_test():
    once = _render(REFERENCES, True)
    never = {
        "model": MODEL,
        "columns": {
            "customer_id": {
                "references": "dim_customers.customer_id",
                "null_rule": "never",
            }
        },
    }
    generated: list = []
    again = _render(never, True, content=once, generated=generated)
    arguments = [t[NAME]["arguments"].get("null_keys") for t in _model_tests(again)]
    # The new test counts NULL keys; the earlier one is left for a reviewer.
    assert sorted(arguments, key=str) == [None, "unmatched"]
    assert sorted(g["status"] for g in generated if g["test"] == NAME) == [
        "added",
        "stale",
    ]


def test_a_required_left_join_is_listed_covered_by_the_builtin():
    generated: list = []
    content = _render(REFERENCES, True, LEFT, generated=generated)

    assert NAME not in content
    assert "relationships" in content
    [row] = [g for g in generated if g["test"] == NAME]
    assert (row["status"], row["reason"]) == ("not_rendered", gt.COVERED_BY_BUILTIN)


def test_a_key_conversion_is_listed_unsupported():
    sql = INNER.replace(
        "o.customer_id = c.customer_id", "cast(o.customer_id as int) = c.customer_id"
    )
    generated: list = []
    content = _render(REFERENCES, True, sql, generated=generated)
    assert NAME not in content
    [row] = [g for g in generated if g["test"] == NAME]
    assert row["reason"] == gt.JOIN_SHAPE_UNSUPPORTED


def test_unreadable_sql_lists_each_reference_unread():
    generated: list = []
    content = _render(
        REFERENCES,
        True,
        "{% if true %}" + INNER + "{% endif %}",
        generated=generated,
    )
    assert NAME not in content
    [row] = [g for g in generated if g["test"] == NAME]
    assert (row["status"], row["reason"]) == ("not_rendered", gt.JOIN_UNREAD)


def test_no_reading_asked_for_lists_nothing():
    generated: list = []
    [declaration] = parse_declarations([REFERENCES])
    render(None, declaration, render_tests=True, generated=generated)
    assert [g for g in generated if g["test"] == NAME] == []


def test_rendering_again_adds_nothing():
    once = _render(REFERENCES, True)
    generated: list = []
    twice = _render(REFERENCES, True, content=once, generated=generated)
    assert twice == once
    assert [g["status"] for g in generated if g["test"] == NAME] == ["already_listed"]


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(REFERENCES, False, generated=generated)
    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated if g["test"] == NAME] == [
        ("not_rendered", "render_tests_off")
    ]


@pytest.mark.parametrize("setting", [[NAME], [gt.SCHEDULED]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert NAME in _render(REFERENCES, setting)


def test_a_join_the_sql_no_longer_writes_is_reported_stale():
    once = _render(REFERENCES, True)
    generated: list = []
    notes: list = []
    moved = INNER.replace("ref('stg_orders')", "ref('stg_orders_v2')")
    again = _render(
        REFERENCES, True, moved, content=once, generated=generated, notes=notes
    )

    # The old test stays (nothing generated is ever removed), the new one is
    # added beside it, and the old one is named stale for a reviewer.
    assert len(_model_tests(again)) == 2
    statuses = sorted(g["status"] for g in generated if g["test"] == NAME)
    assert statuses == ["added", "stale"]
    assert any("no longer writes" in note for note in notes)


def test_the_claim_changes_with_the_join():
    first = _model_tests(_render(REFERENCES, True))[0]
    moved = INNER.replace("ref('stg_orders')", "ref('stg_orders_v2')")
    second = _model_tests(_render(REFERENCES, True, moved))[0]
    claim = lambda t: t[NAME]["config"]["meta"]["dex"]["claim"]  # noqa: E731
    assert claim(first) != claim(second)


# --- the macro file and the plan --------------------------------------------------


def _asset() -> str:
    return scaffold.macro_edit(NAME, "macros").new_content or ""


def test_the_shipped_macro_passes_the_macro_file_check():
    validate_edit(
        PlanEdit(
            path=f"macros/{NAME}.sql", kind=EditKind.MACRO_SQL, new_content=_asset()
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


def _plan(tmp_path: Path, capsys, sql: str) -> dict:
    payload = tmp_path / "edits.json"
    edits = [
        {
            "path": "models/staging/stg_orders.sql",
            "kind": "model_sql",
            "content": "select 1 as order_id, 1 as customer_id\n",
        },
        {
            "path": "models/staging/dim_customers.sql",
            "kind": "model_sql",
            "content": "select 1 as customer_id, 'retail' as segment\n",
        },
        {"path": PATH, "kind": "model_sql", "content": sql},
    ]
    payload.write_text(
        json.dumps({"edits": edits, "declarations": [REFERENCES]}), encoding="utf-8"
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
    planned = _plan(_repo(tmp_path, "true"), capsys, INNER)

    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    rows = [r for r in planned["data"]["generated_tests"]["tests"] if r["test"] == NAME]
    assert [r["status"] for r in rows] == ["added"]


def test_a_plan_with_only_a_left_join_writes_no_macro_and_says_why(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, LEFT)

    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" not in planned["data"]["paths"]
    [row] = [
        r for r in planned["data"]["generated_tests"]["tests"] if r["test"] == NAME
    ]
    assert (row["status"], row["reason"]) == ("not_rendered", gt.COVERED_BY_BUILTIN)


def test_a_plan_editing_only_the_sql_reports_the_test_it_leaves_stale(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path, "true")
    planned = _plan(repo, capsys, INNER)
    assert planned["status"] == "ok", planned["errors"]
    main(["--repo-root", str(repo), "transform", "apply", planned["data"]["plan_id"]])
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    # The join moves to another driving input; nothing is declared again.
    payload = tmp_path / "sql_only.json"
    moved = INNER.replace("ref('stg_orders')", "ref('dim_customers')")
    payload.write_text(
        json.dumps({"edits": [{"path": PATH, "kind": "model_sql", "content": moved}]}),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "move the join",
            "--edits-file",
            str(payload),
        ]
    )
    again = json.loads(capsys.readouterr().out)

    assert again["status"] == "ok", again["errors"]
    [row] = again["data"]["generated_tests"]["tests"]
    assert (row["model"], row["test"], row["status"], row["reason"]) == (
        MODEL,
        NAME,
        "stale",
        "join_not_in_model",
    )
    assert any(f"transform declare {MODEL}" in w for w in again["warnings"]), again[
        "warnings"
    ]


def test_a_plan_editing_the_sql_but_keeping_the_join_reports_nothing(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    repo = _repo(tmp_path, "true")
    planned = _plan(repo, capsys, INNER)
    main(["--repo-root", str(repo), "transform", "apply", planned["data"]["plan_id"]])
    capsys.readouterr()

    payload = tmp_path / "sql_only.json"
    kept = INNER.replace("c.segment", "c.segment, o.order_id * 2 as doubled")
    payload.write_text(
        json.dumps({"edits": [{"path": PATH, "kind": "model_sql", "content": kept}]}),
        encoding="utf-8",
    )
    main(
        [
            "--repo-root",
            str(repo),
            "transform",
            "plan",
            "add a column",
            "--edits-file",
            str(payload),
        ]
    )
    again = json.loads(capsys.readouterr().out)
    assert again["status"] == "ok", again["errors"]
    assert again["data"].get("generated_tests") is None


# --- what it compiles to, against DuckDB --------------------------------------------


def _failures(
    tmp_path: Path,
    child: str,
    parent: str,
    arguments: dict | None = None,
) -> tuple[str, int, str]:
    """Build ``child`` and ``parent`` as views and a model joining them, run the
    shipped test with ``arguments`` (defaults: one key ``customer_id``), and
    return dbt's status, failure count and compiled SQL."""

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
    (project / "models" / "kids.sql").write_text(child, encoding="utf-8")
    (project / "models" / "parents.sql").write_text(parent, encoding="utf-8")
    (project / "models" / "m.sql").write_text(
        "select k.customer_id from {{ ref('kids') }} k\n"
        "join {{ ref('parents') }} p on k.customer_id = p.customer_id\n",
        encoding="utf-8",
    )
    args = {
        "child": "ref('kids')",
        "child_columns": ["customer_id"],
        "parent": "ref('parents')",
        "parent_columns": ["customer_id"],
        **(arguments or {}),
    }
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [{"name": "m", "data_tests": [{NAME: {"arguments": args}}]}],
            }
        ),
        encoding="utf-8",
    )
    dbt = Path(sys.executable).parent / "dbt"
    if not dbt.is_file():
        pytest.skip("needs dbt beside the interpreter")
    flags = ["--project-dir", str(project), "--profiles-dir", str(project)]
    subprocess.run(  # noqa: S603 - the venv's own dbt, on a fixture project
        [str(dbt), "build", *flags], cwd=project, capture_output=True, check=False
    )
    results = json.loads((project / "target" / "run_results.json").read_text())
    [result] = [r for r in results["results"] if r["unique_id"].startswith("test.")]
    compiled = next((project / "target" / "compiled").rglob(f"{NAME}*.sql"))
    return result["status"], int(result["failures"] or 0), compiled.read_text()


def _ids(column: str, *values: str) -> str:
    first, *rest = values
    lines = [f"select {first} as {column}", *(f"union all select {v}" for v in rest)]
    return "\n".join(lines) + "\n"


NULL = "cast(null as integer)"
PARENTS = _ids("customer_id", "1", "2", "2", "3")


def test_unmatched_driving_rows_fail_with_their_count_and_never_a_key(tmp_path):
    status, failures, sql = _failures(
        tmp_path, _ids("customer_id", "1", "2", "9", "9", NULL), PARENTS
    )
    # Two rows on key 9; the NULL key is a missing reference, not counted; the
    # duplicated parent key 2 multiplies nothing.
    assert (status, failures) == ("fail", 2)
    assert "customer_id" not in sql.split("from", 1)[0]


def test_every_row_matched_passes(tmp_path):
    assert _failures(tmp_path, _ids("customer_id", "1", "2", "2", NULL), PARENTS)[
        :2
    ] == ("pass", 0)


def test_nothing_matching_fails_with_every_eligible_row(tmp_path):
    assert _failures(tmp_path, _ids("customer_id", "7", "8", NULL), PARENTS)[:2] == (
        "fail",
        2,
    )


def test_null_keys_unmatched_counts_them(tmp_path):
    status, failures, _ = _failures(
        tmp_path,
        _ids("customer_id", "1", NULL, NULL),
        PARENTS,
        {"null_keys": "unmatched"},
    )
    assert (status, failures) == ("fail", 2)


CHILD = (
    "select 1 as customer_id, 'ok' as status\n"
    "union all select 9, 'cancelled'\n"
    "union all select 8, 'cancelled'\n"
    "union all select 3, 'ok'\n"
)
PARENT = (
    "select 1 as customer_id, true as is_active\n"
    "union all select 3, false\n"
    "union all select 4, false\n"
)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # No filter: 9 and 8 are orphans; 3 has a parent row.
        ({}, 2),
        # The child filter drops the two cancelled orphans.
        ({"child_where": "status <> 'cancelled'"}, 0),
        # The parent filter drops 3's only parent row: 3 joins 9 and 8.
        ({"parent_where": "is_active"}, 3),
        # Both: only 3, whose parent the filter dropped.
        ({"child_where": "status <> 'cancelled'", "parent_where": "is_active"}, 1),
    ],
)
def test_each_filter_scopes_its_own_side(tmp_path, arguments, expected):
    status, failures, _ = _failures(tmp_path, CHILD, PARENT, arguments)
    assert failures == expected
    assert status == ("fail" if expected else "pass")


def test_a_sql_only_edit_of_a_version_file_is_checked_under_its_label(tmp_path):
    from exmergo_dex_core.dbt_project import load
    from exmergo_dex_core.transform.declarations import stale_join_tests

    marts = tmp_path / "models" / "marts"
    marts.mkdir(parents=True)
    (tmp_path / "dbt_project.yml").write_text(
        "name: p\nversion: '1.0'\nprofile: p\nconfig-version: 2\n", encoding="utf-8"
    )
    joined = (
        "select t.team_id, s.team_label\n"
        "from {{ ref('stg_teams') }} t\n"
        "join {{ ref('dim_leagues') }} s on t.league_id = s.league_id\n"
    )
    (marts / "dim_teams.sql").write_text(joined, encoding="utf-8")
    (marts / "dim_teams_v2.sql").write_text(joined, encoding="utf-8")
    listed = {
        NAME: {
            "arguments": {
                "child": "ref('stg_teams')",
                "child_columns": ["league_id"],
                "parent": "ref('dim_leagues')",
                "parent_columns": ["league_id"],
            },
            "config": {"tags": [gt.SCHEDULED], "meta": {"dex": {"claim": "old"}}},
        }
    }
    (marts / "_marts.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "models": [
                    {
                        "name": "dim_teams",
                        "latest_version": 1,
                        "versions": [
                            {"v": 1, "defined_in": "dim_teams"},
                            {"v": 2, "data_tests": [listed]},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    view = load(tmp_path)
    moved = joined.replace("ref('stg_teams')", "ref('stg_teams_v2')")
    edit = PlanEdit(
        path="models/marts/dim_teams_v2.sql", kind=EditKind.MODEL_SQL, new_content=moved
    )

    rows, notes = stale_join_tests(view, [edit], [], "duckdb")

    assert [(r["model"], r["status"], r["claim"]) for r in rows] == [
        ("dim_teams.v2", "stale", "old")
    ]
    assert len(notes) == 1
