"""``dex_parent_population``: the generated test for a declared parent (#1454).

A declaration's ``population.parent`` says the model holds one row per key of a
parent model, seed or snapshot. That is two claims (no eligible parent key is
missing, no output key is unexpected), and with ``declarations.render_tests``
on each renders as its own model-level test, told apart by ``direction``.
Whether each agrees with ``maintain verify``'s ``parent_population_missing``
and ``parent_population_unexpected`` is the parity harness's question
(``test_generated_parity.py``); these are the rendering, the gating, the file
it writes and the SQL it compiles to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.model_versions import ref_call
from exmergo_dex_core.transform import scaffold
from exmergo_dex_core.transform.claims import claim_id
from exmergo_dex_core.transform.declarations import parse_declarations, render
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.validate import validate_edit

NAME = gt.PARENT_POPULATION


def _declared(parent: str = "dim_customers", key=None, filters=None) -> dict:
    population: dict = {
        "parent": {"model": parent, "key": key or {"customer_id": "customer_id"}}
    }
    if filters:
        population["filters"] = filters
    return {"model": "fct_customer_orders", "population": population}


DECLARED = _declared()


def _render(payload: dict, render_tests, *, generated=None, outputs=None, content=None):
    [declaration] = parse_declarations([payload])
    return render(
        content,
        declaration,
        render_tests=render_tests,
        generated=generated,
        outputs=outputs,
    )


def _model_tests(content: str, model: str = "fct_customer_orders") -> list:
    entry = next(e for e in yaml.safe_load(content)["models"] if e["name"] == model)
    return [
        t for t in entry.get("data_tests") or [] if isinstance(t, dict) and NAME in t
    ]


# --- the kind ---------------------------------------------------------------------


def test_it_ships_as_a_scheduled_test_on_every_connector():
    [kind] = [k for k in gt.kinds() if k.name == NAME]
    assert kind.cadence == gt.SCHEDULED
    assert kind.macro == NAME
    assert kind.dialects == gt.ALL_CONNECTORS
    assert NAME in scaffold.MACRO_ASSETS
    assert gt.unknown_entries([NAME]) == []


def test_a_parent_renders_two_model_level_tests_one_per_direction():
    content = _render(DECLARED, True)

    tests = _model_tests(content)
    assert [t[NAME]["arguments"] for t in tests] == [
        {
            "parent": "ref('dim_customers')",
            "key": {"customer_id": "customer_id"},
            "direction": direction,
        }
        for direction in ("missing", "unexpected")
    ]
    for test in tests:
        assert test[NAME]["config"]["tags"] == [gt.SCHEDULED]
        assert test[NAME]["config"]["meta"]["disable_test_samples"] is True


def test_the_two_tests_are_written_without_yaml_anchors():
    # One mapping shared by both tests would dump as `&id001` / `*id001`.
    content = _render(_declared(filters=[{"column": "s", "excludes": ["x"]}]), True)
    assert "&id" not in content and "*id" not in content


def test_each_direction_is_its_own_claim():
    tests = _model_tests(_render(DECLARED, True))
    claims = [t[NAME]["config"]["meta"]["dex"]["claim"] for t in tests]
    assert claims == [
        claim_id(
            "fct_customer_orders",
            "population",
            ["customer_id"],
            {
                "check": NAME,
                "direction": direction,
                "parent": "dim_customers",
                "parent_key": ["customer_id"],
            },
        )
        for direction in ("missing", "unexpected")
    ]
    assert len(set(claims)) == 2


def test_another_parent_or_parent_key_is_another_claim():
    def claims(payload: dict) -> set[str]:
        tests = _model_tests(_render(payload, True))
        return {t[NAME]["config"]["meta"]["dex"]["claim"] for t in tests}

    base = claims(DECLARED)
    assert claims(_declared(parent="dim_accounts")).isdisjoint(base)
    assert claims(_declared(key={"customer_id": "id"})).isdisjoint(base)
    # A respelled name states the same fact.
    assert claims(_declared(key={"customer_id": "CUSTOMER_ID"})) == base


def test_a_composite_key_keeps_its_order():
    payload = _declared(
        parent="dim_lines", key={"order_id": "order_id", "line": "line_number"}
    )
    [missing, _unexpected] = _model_tests(_render(payload, True))
    assert list(missing[NAME]["arguments"]["key"].items()) == [
        ("order_id", "order_id"),
        ("line", "line_number"),
    ]


def test_a_version_label_is_a_versioned_ref():
    payload = _declared(parent="dim_customers.v2")
    [missing, _] = _model_tests(_render(payload, True))
    assert missing[NAME]["arguments"]["parent"] == "ref('dim_customers', v=2)"
    assert ref_call("dim_customers") == "ref('dim_customers')"


def test_the_filters_ride_on_the_missing_direction_only():
    payload = _declared(
        filters=[
            {"column": "status", "excludes": ["churned"]},
            {"column": "region", "includes": ["emea"], "excludes": ["x"]},
        ]
    )
    [missing, unexpected] = _model_tests(_render(payload, True))
    assert missing[NAME]["arguments"]["filters"] == [
        {"column": "status", "excludes": ["churned"]},
        {"column": "region", "includes": ["emea"], "excludes": ["x"]},
    ]
    assert "filters" not in unexpected[NAME]["arguments"]
    # A different filter scopes a different set of eligible parents.
    [plain, _] = _model_tests(_render(DECLARED, True))
    assert (
        missing[NAME]["config"]["meta"]["dex"]["claim"]
        != plain[NAME]["config"]["meta"]["dex"]["claim"]
    )


def test_no_parent_renders_nothing():
    generated: list = []
    payload = {
        "model": "fct_customer_orders",
        "population": {"filters": [{"column": "status", "excludes": ["x"]}]},
    }
    content = _render(payload, [NAME], generated=generated)
    assert NAME not in content
    assert not [g for g in generated if g["test"] == NAME]


def test_the_plan_report_names_each_direction():
    generated: list = []
    _render(DECLARED, True, generated=generated)
    assert [g["variant"] for g in generated] == ["missing", "unexpected"]


def test_rendering_again_adds_nothing():
    once = _render(DECLARED, True)
    generated: list = []
    twice = _render(DECLARED, True, content=once, generated=generated)

    assert twice == once
    assert [g["status"] for g in generated] == ["already_listed", "already_listed"]


def test_the_switch_off_renders_nothing_and_says_why():
    generated: list = []
    content = _render(DECLARED, False, generated=generated)

    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", "render_tests_off")
    ] * 2


@pytest.mark.parametrize("setting", [[NAME], [gt.SCHEDULED]])
def test_a_list_naming_the_kind_or_its_cadence_turns_it_on(setting):
    assert len(_model_tests(_render(DECLARED, setting))) == 2


def test_the_every_run_cadence_leaves_it_off():
    generated: list = []
    content = _render(DECLARED, [gt.EVERY_RUN], generated=generated)

    assert NAME not in content
    assert {g["reason"] for g in generated} == {"kind_not_enabled"}


def test_a_key_column_the_model_provably_does_not_output_renders_nothing():
    generated: list = []
    content = _render(
        DECLARED, True, generated=generated, outputs=frozenset({"orders"})
    )

    assert NAME not in content
    assert [(g["status"], g["reason"]) for g in generated] == [
        ("not_rendered", gt.COLUMN_NOT_OUTPUT)
    ] * 2


def test_a_key_column_the_model_outputs_renders_whatever_its_case():
    assert _model_tests(_render(DECLARED, True, outputs=frozenset({"customer_id"})))
    upper = _declared(key={"CUSTOMER_ID": "customer_id"})
    assert _model_tests(_render(upper, True, outputs=frozenset({"customer_id"})))


def test_a_composite_grain_missing_from_the_outputs_renders_nothing_either():
    # The model-level check reads every column a test reads (#1454), so the
    # composite grain test (#1452) is held to it too.
    generated: list = []
    payload = {"model": "fct_lines", "grain": ["order_id", "line_number"]}
    _render(payload, True, generated=generated, outputs=frozenset({"order_id"}))
    assert [(g["test"], g["reason"]) for g in generated] == [
        (gt.UNIQUE_COMBINATION, gt.COLUMN_NOT_OUTPUT)
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


def test_clickhouse_and_the_backslash_warehouses_get_their_own_helpers():
    asset = _asset()
    assert "clickhouse__dex_parent_population_text(column_name) %}toString(" in asset
    for adapter in ("bigquery", "snowflake", "databricks", "spark", "redshift"):
        assert (
            f"{{% macro {adapter}__dex_parent_population_literal(text) %}}"
            "{{ dex_parent_population_backslashed(text) }}"
        ) in asset


def _repo(tmp_path: Path, render_tests: str) -> Path:
    (tmp_path / ".dex").mkdir(exist_ok=True)
    (tmp_path / ".dex" / "config.yml").write_text(
        f"connector: duckdb\nduckdb:\n  path: {tmp_path / 'dev.duckdb'}\n"
        f"declarations:\n  render_tests: {render_tests}\n",
        encoding="utf-8",
    )
    return tmp_path


def _plan(tmp_path: Path, capsys, edits: list[dict], declaration: dict) -> dict:
    payload = tmp_path / "edits.json"
    payload.write_text(
        json.dumps({"edits": edits, "declarations": [declaration]}), encoding="utf-8"
    )
    main(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "customer orders",
            "--edits-file",
            str(payload),
        ]
    )
    return json.loads(capsys.readouterr().out)


_EDITS = [
    {
        "path": "models/marts/dim_customers.sql",
        "kind": "model_sql",
        "content": "select 1 as customer_id\n",
    },
    {
        "path": "models/marts/fct_customer_orders.sql",
        "kind": "model_sql",
        "content": "select 1 as customer_id, 2 as orders\n",
    },
]


def test_the_plan_that_renders_it_writes_its_macro_once_as_a_diff(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    planned = _plan(_repo(tmp_path, "true"), capsys, _EDITS, DECLARED)

    assert planned["status"] == "ok", planned["errors"]
    assert f"macros/{NAME}.sql" in planned["data"]["paths"]
    mine = [t for t in planned["data"]["generated_tests"]["tests"] if t["test"] == NAME]
    assert [t["status"] for t in mine] == ["added", "added"]
    assert {t["cadence"] for t in mine} == {gt.SCHEDULED}


def test_a_plan_whose_model_drops_the_key_is_refused_before_any_test_renders(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    # The plan already refuses a parent key the SELECT does not produce, so
    # the column_not_output path above is reached by a declaration the plan
    # does not write (``transform declare`` over an existing entry).
    edits = [
        _EDITS[0],
        {
            "path": "models/marts/fct_customer_orders.sql",
            "kind": "model_sql",
            "content": "select count(*) as orders from {{ ref('dim_customers') }}\n",
        },
    ]
    planned = _plan(_repo(tmp_path, f"[{NAME}]"), capsys, edits, DECLARED)

    assert planned["status"] == "error"
    assert "population.parent key column(s) customer_id" in planned["errors"][0]


def test_transform_macro_lists_it(dbt_project_dir: Path, tmp_path: Path, capsys):
    _repo(tmp_path, "false")
    main(["--repo-root", str(tmp_path), "transform", "macro"])
    listed = json.loads(capsys.readouterr().out)
    assert NAME in {m["name"] for m in listed["data"]["macros"]}


# --- what it compiles to, against DuckDB --------------------------------------------


duckdb = pytest.importorskip("duckdb")


def _run(
    tmp_path: Path,
    parent: str,
    model: str,
    *,
    key: dict | None = None,
    filters: list | None = None,
    parent_materialized: str = "table",
) -> dict[str, tuple[str, int, str]]:
    """Build ``parent`` and ``model``, run both directions of the shipped test
    on ``model``, and return each direction's dbt status, failure count and
    compiled SQL."""

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
    (project / "models" / "p.sql").write_text(
        f"{{{{ config(materialized='{parent_materialized}') }}}}\n{parent}",
        encoding="utf-8",
    )
    (project / "models" / "m.sql").write_text(model, encoding="utf-8")
    tests = []
    for direction in gt.PARENT_DIRECTIONS:
        arguments: dict = {
            "parent": "ref('p')",
            "key": key or {"id": "id"},
            "direction": direction,
        }
        if filters and direction == "missing":
            arguments["filters"] = filters
        tests.append({NAME: {"name": f"dex_{direction}", "arguments": arguments}})
    (project / "models" / "schema.yml").write_text(
        yaml.safe_dump({"version": 2, "models": [{"name": "m", "data_tests": tests}]}),
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
    out: dict[str, tuple[str, int, str]] = {}
    for direction in gt.PARENT_DIRECTIONS:
        [result] = [
            r
            for r in results["results"]
            if r["unique_id"].startswith(f"test.p.dex_{direction}")
        ]
        compiled = next(
            (project / "target" / "compiled").rglob(f"dex_{direction}*.sql")
        )
        out[direction] = (
            result["status"],
            int(result["failures"] or 0),
            compiled.read_text(),
        )
    return out


def _ids(*values: str) -> str:
    first, *rest = values
    return " union all ".join([f"select {first} as id", *(f"select {v}" for v in rest)])


def _counts(run: dict) -> dict[str, tuple[str, int]]:
    return {d: run[d][:2] for d in run}


def test_a_missing_parent_key_fails_missing_only(tmp_path: Path):
    run = _run(tmp_path, _ids("1", "2", "3"), _ids("1", "2"))
    assert _counts(run) == {"missing": ("fail", 1), "unexpected": ("pass", 0)}
    assert "select dex_violating_keys" in run["missing"][2]


def test_an_unexpected_key_fails_unexpected_only(tmp_path: Path):
    run = _run(tmp_path, _ids("1", "2"), _ids("1", "2", "4", "5", "5"))
    # Distinct keys: 5 twice is one unexpected key, as maintain verify counts.
    assert _counts(run) == {"missing": ("pass", 0), "unexpected": ("fail", 2)}


def test_the_same_population_passes_both(tmp_path: Path):
    run = _run(tmp_path, _ids("1", "2", "3"), _ids("3", "1", "2", "2"))
    assert _counts(run) == {"missing": ("pass", 0), "unexpected": ("pass", 0)}


def test_a_null_key_is_no_key_on_either_side(tmp_path: Path):
    parent = _ids("1", "2", "cast(null as integer)")
    model = _ids("1", "2", "cast(null as integer)", "cast(null as integer)")
    run = _run(tmp_path, parent, model)
    assert _counts(run) == {"missing": ("pass", 0), "unexpected": ("pass", 0)}


def test_a_composite_key_is_compared_whole(tmp_path: Path):
    parent = (
        "select 1 as order_id, 1 as line_number union all select 1, 2 "
        "union all select 2, 1"
    )
    model = "select 1 as o, 1 as l union all select 1, 2 union all select 2, 2"
    run = _run(tmp_path, parent, model, key={"o": "order_id", "l": "line_number"})
    assert _counts(run) == {"missing": ("fail", 1), "unexpected": ("fail", 1)}


_CUSTOMERS = (
    "select 1 as id, 'active' as status union all select 2, 'active' "
    "union all select 3, 'churned' union all select 4, null"
)


def test_an_excludes_filter_scopes_the_eligible_parents(tmp_path: Path):
    # 3 is churned and excluded; 4 has a NULL status, which no excludes list
    # rules out, so it is eligible and missing.
    run = _run(
        tmp_path,
        _CUSTOMERS,
        _ids("1", "2"),
        filters=[{"column": "status", "excludes": ["churned"]}],
    )
    assert _counts(run) == {"missing": ("fail", 1), "unexpected": ("pass", 0)}
    assert "churned" in run["missing"][2]


def test_an_includes_filter_keeps_only_its_values(tmp_path: Path):
    run = _run(
        tmp_path,
        _CUSTOMERS,
        _ids("1", "2"),
        filters=[{"column": "status", "includes": ["active"]}],
    )
    assert _counts(run) == {"missing": ("pass", 0), "unexpected": ("pass", 0)}


def test_the_unexpected_direction_reads_the_whole_parent(tmp_path: Path):
    # 3 is excluded from the eligible parents, but it is a parent key, so a row
    # for it is not unexpected.
    run = _run(
        tmp_path,
        _CUSTOMERS,
        _ids("1", "2", "3", "4"),
        filters=[{"column": "status", "excludes": ["churned"]}],
    )
    assert _counts(run) == {"missing": ("pass", 0), "unexpected": ("pass", 0)}


def test_a_filter_on_a_column_the_parent_lacks_is_skipped(tmp_path: Path):
    # maintain verify skips it and lowers its finding; the test has no severity
    # to lower, so it counts the parents the filter cannot rule out.
    run = _run(
        tmp_path,
        _ids("1", "2", "3"),
        _ids("1", "2"),
        filters=[{"column": "channel", "excludes": ["web"]}],
    )
    assert _counts(run) == {"missing": ("fail", 1), "unexpected": ("pass", 0)}
    assert "channel" not in run["missing"][2]


def test_text_values_on_a_number_column_compare_as_text(tmp_path: Path):
    parent = "select 1 as id, 7 as tier union all select 2, 8"
    run = _run(
        tmp_path,
        parent,
        _ids("2"),
        filters=[{"column": "tier", "excludes": ["7"]}],
    )
    assert _counts(run)["missing"] == ("pass", 0)
    assert "cast(tier as" in run["missing"][2]


def test_an_ephemeral_parent_applies_every_filter(tmp_path: Path):
    # No metadata to read: every filter applies, as maintain verify applies
    # them when it cannot read the parent's columns.
    run = _run(
        tmp_path,
        _CUSTOMERS,
        _ids("1", "2", "4"),
        filters=[{"column": "status", "excludes": ["churned"]}],
        parent_materialized="ephemeral",
    )
    assert _counts(run) == {"missing": ("pass", 0), "unexpected": ("pass", 0)}
