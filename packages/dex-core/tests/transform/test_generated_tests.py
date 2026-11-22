"""The opt-in switch and the cadence tag on every generated test (#1449).

A generated test is one Dex renders from a declaration beyond the three
built-ins (``unique``, ``not_null``, ``relationships``). ``declarations.render_tests``
turns them on, off by default, and each carries a fixed cadence tag,
``dex_every_run`` or ``dex_scheduled``, as a dbt tag. These tests set the
shipped kinds aside (#1452 onward add them; see ``test_unique_combination.py``)
and register fixture kinds, so they test the switch and not any one kind.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from exmergo_dex_core import generated_tests as gt
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.config_document import ConfigDocument
from exmergo_dex_core.transform.claims import claim_id, claim_of
from exmergo_dex_core.transform.declarations import parse_declarations, render

COMBO = "dex_fixture_combination"
FILTER = "dex_fixture_filter"


def _combination(declaration) -> list[gt.Instance]:
    grain = list(declaration.grain or [])
    if len(grain) < 2:
        return []
    return [
        gt.Instance(
            column=None,
            field="grain",
            columns=tuple(grain),
            params={"check": COMBO},
            arguments={"columns": grain},
        )
    ]


def _filters(declaration) -> list[gt.Instance]:
    population = declaration.population
    if population is None:
        return []
    out = []
    for item in population.filters or []:
        excluded = list(item.excludes or [])
        out.append(
            gt.Instance(
                column=item.column,
                field="population",
                columns=(item.column,),
                params={"excludes": excluded},
                arguments={"excludes": excluded},
            )
        )
    return out


@pytest.fixture(autouse=True)
def without_shipped_kinds() -> Iterator[None]:
    """The shipped catalog set aside for each test, and put back after it."""

    shipped = gt.kinds()
    for kind in shipped:
        gt.unregister(kind.name)
    try:
        yield
    finally:
        for kind in shipped:
            gt.unregister(kind.name)
            gt.register(kind)


@pytest.fixture
def kinds() -> Iterator[None]:
    gt.register(gt.GeneratedTestKind(COMBO, gt.EVERY_RUN, _combination))
    gt.register(gt.GeneratedTestKind(FILTER, gt.SCHEDULED, _filters))
    try:
        yield
    finally:
        gt.unregister(COMBO)
        gt.unregister(FILTER)


DECLARATION = {
    "model": "fct_lines",
    "grain": ["order_id", "line_number"],
    "population": {
        "rule": "Placed lines only.",
        "filters": [
            {"column": "status", "excludes": ["cancelled"]},
            {"column": "channel", "excludes": ["test"]},
        ],
    },
    "columns": {"customer_id": {"null_rule": "never"}},
}


def _render(
    content: str | None,
    render_tests: bool | list[str],
    payload: dict = DECLARATION,
    generated: list | None = None,
) -> str:
    [declaration] = parse_declarations([payload])
    return render(content, declaration, render_tests=render_tests, generated=generated)


def _entry(content: str, model: str = "fct_lines") -> dict:
    return next(e for e in yaml.safe_load(content)["models"] if e["name"] == model)


def _column_tests(content: str, column: str) -> list:
    found = next(c for c in _entry(content)["columns"] if c["name"] == column)
    return found.get("data_tests") or found.get("tests") or []


def _model_tests(content: str) -> list:
    entry = _entry(content)
    return entry.get("data_tests") or entry.get("tests") or []


def _names(tests: list) -> list[str]:
    return [t if isinstance(t, str) else next(iter(t)) for t in tests]


# --- the catalog --------------------------------------------------------------


def test_with_the_shipped_kinds_set_aside_the_catalog_is_empty():
    # The fixture kinds below are the only ones these tests see.
    assert gt.kinds() == ()


def test_a_kind_is_refused_a_cadence_that_is_not_one_of_the_two(kinds):
    with pytest.raises(ValueError, match="cadence"):
        gt.register(gt.GeneratedTestKind("dex_x", "dex_hourly", _filters))
    with pytest.raises(ValueError, match="already registered"):
        gt.register(gt.GeneratedTestKind(COMBO, gt.EVERY_RUN, _filters))
    for name in (*gt.CADENCES, "unique", "not_null", "relationships"):
        with pytest.raises(ValueError, match="cannot name"):
            gt.register(gt.GeneratedTestKind(name, gt.EVERY_RUN, _filters))


def test_the_switch_turns_kinds_on_by_name_or_by_cadence(kinds):
    combo, filt = gt.kinds()

    assert not gt.enabled(combo, False)
    assert gt.enabled(combo, True) and gt.enabled(filt, True)
    assert gt.enabled(combo, [COMBO]) and not gt.enabled(filt, [COMBO])
    assert gt.enabled(combo, [gt.EVERY_RUN]) and not gt.enabled(filt, [gt.EVERY_RUN])
    assert gt.enabled(filt, [gt.SCHEDULED]) and not gt.enabled(combo, [gt.SCHEDULED])
    assert not gt.enabled(combo, [])
    assert gt.off_reason(False) == "render_tests_off"
    assert gt.off_reason([gt.SCHEDULED]) == "kind_not_enabled"


def test_unknown_entries_are_neither_a_kind_nor_a_cadence(kinds):
    assert gt.unknown_entries(True) == []
    assert gt.unknown_entries([COMBO, gt.SCHEDULED, "dex_freshness"]) == [
        "dex_freshness"
    ]


def test_a_generated_test_is_told_from_a_built_in_by_its_cadence_tag():
    assert gt.is_generated({"x": {"config": {"tags": [gt.SCHEDULED]}}})
    assert gt.is_generated({"x": {"tags": gt.EVERY_RUN}})
    assert not gt.is_generated({"unique": {"config": {"meta": {}}}})
    assert not gt.is_generated({"x": {"config": {"tags": ["nightly"]}}})
    assert not gt.is_generated("not_null")


# --- the setting --------------------------------------------------------------


def test_the_switch_is_off_by_default():
    assert DexConfig().declarations.render_tests is False


def test_the_switch_takes_a_boolean_or_a_list():
    assert DexConfig.model_validate(
        {"declarations": {"render_tests": True}}
    ).declarations.render_tests
    assert DexConfig.model_validate(
        {"declarations": {"render_tests": ["dex_every_run"]}}
    ).declarations.render_tests == ["dex_every_run"]


def _document(tmp_path: Path, text: str) -> ConfigDocument:
    path = tmp_path / ".dex" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ConfigDocument.read(path)


@pytest.mark.parametrize(
    ("written", "message"),
    [
        ("[dex_every_run, dex_every_run]", "more than once"),
        ('[" "]', "empty name"),
        ("nightly", "true, false, or a list"),
    ],
)
def test_a_malformed_switch_stops_every_command(
    tmp_path: Path, written: str, message: str
):
    document = _document(tmp_path, f"declarations:\n  render_tests: {written}\n")

    [problem] = [p for p in document.problems() if p.fatal]
    assert problem.key == "declarations.render_tests" and problem.line == 2
    assert message in problem.message


def test_a_name_this_release_does_not_render_is_reported_and_loads(
    tmp_path: Path, kinds
):
    document = _document(
        tmp_path,
        "declarations:\n  render_tests: [dex_every_run, dex_freshness]\n",
    )

    [problem] = document.problems()
    assert problem.kind == "unknown_test_kind" and not problem.fatal
    assert problem.key == "declarations.render_tests" and problem.line == 2
    assert "'dex_freshness'" in problem.message and COMBO in problem.message
    assert document.config().declarations.render_tests == [
        "dex_every_run",
        "dex_freshness",
    ]
    assert any("dex_freshness" in w for w in document.warnings())


def test_config_set_get_and_unset_cover_the_switch(tmp_path: Path, capsys):
    _document(tmp_path, "connector: duckdb  # the local on-ramp\n")

    def dex(*argv: str) -> dict:
        main(["--repo-root", str(tmp_path), "config", *argv])
        return json.loads(capsys.readouterr().out)

    [row] = dex("get", "declarations")["data"]["settings"]
    assert row == {**row, "key": "declarations.render_tests", "value": False}
    assert row["source"] == "default"

    asked = dex("set", "declarations.render_tests", "[dex_every_run]")
    assert asked["status"] == "needs_confirmation"
    assert (
        dex("set", "declarations.render_tests", "[dex_every_run]", "--confirm")[
            "status"
        ]
        == "ok"
    )
    written = (tmp_path / ".dex" / "config.yml").read_text()
    assert "# the local on-ramp" in written
    [row] = dex("get", "declarations.render_tests")["data"]["settings"]
    assert row["value"] == ["dex_every_run"] and row["source"] == "file"
    assert dex("validate")["status"] == "ok"

    assert dex("unset", "declarations.render_tests", "--confirm")["status"] == "ok"
    assert "declarations" not in (tmp_path / ".dex" / "config.yml").read_text()


# --- rendering ----------------------------------------------------------------


def test_off_a_declaration_renders_only_the_built_ins(kinds):
    generated: list = []
    out = _render(None, False, generated=generated)

    assert COMBO not in out and FILTER not in out
    assert _names(_column_tests(out, "customer_id")) == ["not_null"]
    assert [(g["test"], g["status"], g["reason"]) for g in generated] == [
        (COMBO, "not_rendered", "render_tests_off"),
        (FILTER, "not_rendered", "render_tests_off"),
        (FILTER, "not_rendered", "render_tests_off"),
    ]


def test_on_each_generated_test_carries_its_cadence_tag_and_claim(kinds):
    generated: list = []
    out = _render(None, True, generated=generated)

    [combo] = _model_tests(out)
    claim = claim_id(
        "fct_lines", "grain", ["order_id", "line_number"], {"check": COMBO}
    )
    assert combo == {
        COMBO: {
            "arguments": {"columns": ["order_id", "line_number"]},
            "config": {
                "tags": [gt.EVERY_RUN],
                "meta": {"disable_test_samples": True, "dex": {"claim": claim}},
            },
        }
    }
    [status] = _column_tests(out, "status")
    assert status[FILTER]["config"]["tags"] == [gt.SCHEDULED]
    assert status[FILTER]["arguments"] == {"excludes": ["cancelled"]}
    [channel] = _column_tests(out, "channel")
    assert claim_of(channel) != claim_of(status)
    assert {g["status"] for g in generated} == {"added"}
    assert [(g["column"], g["cadence"]) for g in generated] == [
        (None, gt.EVERY_RUN),
        ("status", gt.SCHEDULED),
        ("channel", gt.SCHEDULED),
    ]
    assert generated[0]["claim"] == claim


def test_the_built_ins_are_the_same_with_the_switch_on_or_off(kinds):
    on = _render(None, True)
    off = _render(None, False)

    assert _column_tests(on, "customer_id") == _column_tests(off, "customer_id")
    [not_null] = _column_tests(on, "customer_id")
    assert "tags" not in not_null["not_null"]["config"]


def test_a_list_turns_on_only_the_kinds_it_names(kinds):
    generated: list = []
    out = _render(None, [gt.EVERY_RUN], generated=generated)

    assert _names(_model_tests(out)) == [COMBO]
    assert FILTER not in out
    assert [(g["test"], g["status"], g.get("reason")) for g in generated] == [
        (COMBO, "added", None),
        (FILTER, "not_rendered", "kind_not_enabled"),
        (FILTER, "not_rendered", "kind_not_enabled"),
    ]
    assert FILTER in _render(None, [FILTER])


def test_re_rendering_changes_no_byte_and_reports_the_test_already_listed(kinds):
    first = _render(None, True)
    generated: list = []

    assert _render(first, True, generated=generated) == first
    assert {g["status"] for g in generated} == {"already_listed"}


def test_a_renamed_generated_test_is_not_rendered_again(kinds):
    first = _render(None, True)
    renamed = first.replace(
        f"- {COMBO}:\n", f"- {COMBO}:\n          name: lines_unique\n"
    )
    assert "lines_unique" in renamed

    again = _render(renamed, True)

    assert again == renamed
    assert _names(_model_tests(again)) == [COMBO]


def test_a_new_filter_adds_a_second_test_of_the_same_kind(kinds):
    first = _render(None, True)
    payload = json.loads(json.dumps(DECLARATION))
    payload["population"]["filters"][0]["excludes"] = ["cancelled", "void"]

    out = _render(first, True, payload)

    # A changed parameter is a new claim; the test already listed stays.
    assert [t[FILTER]["arguments"] for t in _column_tests(out, "status")] == [
        {"excludes": ["cancelled"]},
        {"excludes": ["cancelled", "void"]},
    ]


def test_a_literal_value_keeps_its_case_in_the_claim(kinds):
    first = _render(None, True)
    payload = json.loads(json.dumps(DECLARATION))
    payload["population"]["filters"][0]["excludes"] = ["CANCELLED"]

    out = _render(first, True, payload)

    # A value from the data is part of the fact, so its case makes a new claim.
    assert [t[FILTER]["arguments"] for t in _column_tests(out, "status")] == [
        {"excludes": ["cancelled"]},
        {"excludes": ["CANCELLED"]},
    ]


def test_off_a_hand_written_equivalent_reads_listed_while_off(kinds):
    hand = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_lines\n"
        "    data_tests:\n"
        f"      - {COMBO}:\n"
        "          arguments:\n"
        "            columns: [order_id, line_number]\n"
    )
    generated: list = []

    _render(hand, False, generated=generated)

    assert generated[0]["status"] == "listed_while_off"


def test_a_renamed_model_s_tests_do_not_count_for_a_recreated_one(kinds):
    payload = {"model": "fct_lines", "grain": ["order_id", "line_number"]}
    first = _render(None, True, payload)
    # The model renamed by hand, its claimed test kept as written, and a new
    # model taking the old name in the same file.
    renamed = first.replace("name: fct_lines", "name: fct_order_lines")
    generated: list = []

    out = _render(renamed, True, payload, generated=generated)

    assert _names(_model_tests(out)) == [COMBO]
    assert generated[0]["status"] == "added"


def test_turning_the_switch_off_leaves_rendered_tests_in_place(kinds):
    first = _render(None, True)
    generated: list = []

    out = _render(first, False, generated=generated)

    assert out == first
    assert {g["status"] for g in generated} == {"listed_while_off"}


def test_a_hand_written_test_of_the_same_name_and_arguments_is_kept(kinds):
    hand = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_lines\n"
        "    data_tests:\n"
        f"      - {COMBO}:\n"
        "          arguments:\n"
        "            columns: [order_id, line_number]\n"
    )
    generated: list = []

    out = _render(hand, True, generated=generated)

    # dbt would build the two into one node; the person's test stays as written.
    assert _model_tests(out) == [
        {COMBO: {"arguments": {"columns": ["order_id", "line_number"]}}}
    ]
    assert generated[0]["status"] == "already_listed"


def test_a_versioned_model_takes_its_generated_test_on_the_version(kinds):
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_lines\n"
        "    latest_version: 1\n"
        "    versions:\n"
        "      - v: 1\n"
        "      - v: 2\n"
    )
    payload = {"model": "fct_lines.v2", "grain": ["order_id", "line_number"]}

    out = _render(content, True, payload)

    [entry] = yaml.safe_load(out)["models"]
    v1, v2 = entry["versions"]
    assert "data_tests" not in v1 and "data_tests" not in entry
    [test] = v2["data_tests"]
    assert claim_of(test) == claim_id(
        "fct_lines.v2", "grain", ["order_id", "line_number"], {"check": COMBO}
    )


def test_a_fact_declared_earlier_gets_its_generated_test_when_declared_again(kinds):
    # Declared with the switch off, then a description alone with it on: the
    # composite grain the entry already states gets its test.
    first = _render(None, False, {"model": "fct_lines", "grain": ["a", "b"]})
    assert COMBO not in first
    generated: list = []

    out = _render(
        first, True, {"model": "fct_lines", "description": "x"}, generated=generated
    )

    assert _names(_model_tests(out)) == [COMBO]
    assert [(g["test"], g["status"]) for g in generated] == [(COMBO, "added")]


def test_off_a_version_s_inherited_equivalent_reads_listed_while_off(kinds):
    content = (
        "version: 2\n"
        "models:\n"
        "  - name: fct_lines\n"
        "    latest_version: 1\n"
        "    columns:\n"
        "      - name: status\n"
        "        data_tests:\n"
        f"          - {FILTER}:\n"
        "              arguments:\n"
        "                excludes: [cancelled]\n"
        "    versions:\n"
        "      - v: 1\n"
    )
    payload = {
        "model": "fct_lines.v1",
        "population": {
            "rule": "x",
            "filters": [{"column": "status", "excludes": ["cancelled"]}],
        },
    }
    generated: list = []

    _render(content, False, payload, generated=generated)

    assert [g["status"] for g in generated] == ["listed_while_off"]


# --- through the plan ---------------------------------------------------------

MACROS = (
    "{% test dex_fixture_combination(model, columns) %}\n"
    "select count(*) as violations from (\n"
    "  select {{ columns | join(', ') }} from {{ model }}\n"
    "  group by {{ columns | join(', ') }} having count(*) > 1\n"
    ") as dupes having count(*) > 0\n"
    "{% endtest %}\n"
    "\n"
    "{% test dex_fixture_filter(model, column_name, excludes) %}\n"
    "select count(*) as violations from {{ model }}\n"
    "where {{ column_name }} in ('{{ excludes | join(\"', '\") }}')\n"
    "having count(*) > 0\n"
    "{% endtest %}\n"
)

MODEL = (
    "select 1 as order_id, 1 as line_number, 'placed' as status, "
    "'web' as channel, 7 as customer_id\n"
    "union all select 1, 2, 'placed', 'web', 7\n"
)


def _dex(argv: list[str], capsys) -> dict:
    main(argv)
    return json.loads(capsys.readouterr().out)


def _plan(tmp_path: Path, capsys) -> dict:
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {
                "edits": [
                    {
                        "path": "tests/generic/dex_fixture_tests.sql",
                        "kind": "test_sql",
                        "content": MACROS,
                    },
                    {
                        "path": "models/marts/fct_lines.sql",
                        "kind": "model_sql",
                        "content": MODEL,
                    },
                ],
                "declarations": [DECLARATION],
            }
        )
    )
    return _dex(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "declare fct_lines",
            "--edits-file",
            str(payload),
        ],
        capsys,
    )


def _switch(tmp_path: Path, value: str) -> None:
    config = tmp_path / ".dex" / "config.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"declarations:\n  render_tests: {value}\n", encoding="utf-8")


def test_the_plan_lists_each_generated_test_with_its_tag(
    dbt_project_dir: Path, tmp_path: Path, capsys, kinds
):
    _switch(tmp_path, "[dex_every_run]")

    planned = _plan(tmp_path, capsys)

    assert planned["status"] == "ok", planned["errors"]
    report = planned["data"]["generated_tests"]
    assert report["render_tests"] == ["dex_every_run"]
    assert report["counts"] == {"added": 1, "not_rendered": 2}
    [added] = [t for t in report["tests"] if t["status"] == "added"]
    # The cost a test runs at in production (#1462): unknown here, since the
    # config names no `prod` namespace.
    assert added.pop("cost")["reason"] == "prod_unset"
    assert added == {
        "model": "fct_lines",
        "column": None,
        "test": COMBO,
        "cadence": gt.EVERY_RUN,
        "claim": claim_id(
            "fct_lines", "grain", ["order_id", "line_number"], {"check": COMBO}
        ),
        "status": "added",
    }


def test_a_plan_whose_declarations_give_no_generated_test_carries_no_key(
    dbt_project_dir: Path, tmp_path: Path, capsys
):
    _switch(tmp_path, "true")

    planned = _plan(tmp_path, capsys)

    assert planned["status"] == "ok", planned["errors"]
    assert "generated_tests" not in planned["data"]


def test_transform_declare_renders_the_tests_earlier_declarations_owe(
    dbt_project_dir: Path, tmp_path: Path, capsys, kinds
):
    sql = dbt_project_dir / "models" / "staging" / "stg_customers.sql"
    sql.write_text("select 1 as id, 2 as region_id\n", encoding="utf-8")
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id, region_id]\n",
        encoding="utf-8",
    )

    def declare() -> dict:
        return _dex(
            ["--repo-root", str(tmp_path), "transform", "declare", "stg_customers"],
            capsys,
        )

    _switch(tmp_path, "false")
    assert declare()["data"]["plan_id"] is None

    _switch(tmp_path, "true")
    proposed = declare()

    assert proposed["status"] == "ok", proposed["errors"]
    [proposal] = proposed["data"]["proposed"]
    [owed] = proposal["fields"]["generated_tests"]["value"]
    assert owed["test"] == COMBO and owed["cadence"] == gt.EVERY_RUN
    assert proposed["data"]["generated_tests"]["counts"] == {"added": 1}
    applied = _dex(["--repo-root", str(tmp_path), "transform", "apply"], capsys)
    assert applied["status"] == "ok", applied["errors"]
    # Once listed, there is nothing left to propose.
    assert declare()["data"]["plan_id"] is None


# --- dbt selects them by tag --------------------------------------------------


def _dbt() -> str:
    pytest.importorskip("dbt.cli.main")
    pytest.importorskip("dbt.adapters.duckdb")
    sibling = Path(sys.executable).with_name("dbt")
    return str(sibling) if sibling.exists() else (shutil.which("dbt") or "dbt")


def _build(project: Path, *selection: str) -> dict[str, str]:
    completed = subprocess.run(  # noqa: S603 - the dbt beside this interpreter
        [
            _dbt(),
            "build",
            "--project-dir",
            str(project),
            "--profiles-dir",
            str(project),
            *selection,
        ],
        capture_output=True,
        text=True,
        cwd=project,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-3000:]
    results = json.loads((project / "target" / "run_results.json").read_text())
    return {r["unique_id"]: r["status"] for r in results["results"]}


def test_dbt_build_selects_exactly_the_tests_of_one_cadence(tmp_path: Path, kinds):
    _dbt()
    project = tmp_path / "p"
    (project / "models").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "dbt_project.yml").write_text('name: p\nversion: "1.0.0"\nprofile: p\n')
    (project / "profiles.yml").write_text(
        f"p:\n  target: dev\n  outputs:\n    dev: {{type: duckdb, path: "
        f"{tmp_path / 'dev.duckdb'}}}\n"
    )
    (project / "macros" / "dex_fixture_tests.sql").write_text(MACROS)
    (project / "models" / "fct_lines.sql").write_text(MODEL)
    (project / "models" / "schema.yml").write_text(_render(None, True))
    _build(project, "--select", "fct_lines", "--exclude", "resource_type:test")

    scheduled = _build(project, "--select", "tag:dex_scheduled")
    every_run = _build(project, "--select", "tag:dex_every_run")
    rest = _build(project, "--exclude", "tag:dex_scheduled")

    assert sorted(scheduled) == sorted(
        uid for uid in scheduled if uid.startswith(f"test.p.{FILTER}")
    )
    assert len(scheduled) == 2 and set(scheduled.values()) == {"pass"}
    [only] = every_run
    assert only.startswith(f"test.p.{COMBO}")
    assert not any(FILTER in uid for uid in rest)
    assert any(uid.startswith(f"test.p.{COMBO}") for uid in rest)
    # The built-in not_null carries no cadence tag and runs with the rest.
    assert any(uid.startswith("test.p.not_null_fct_lines_customer_id") for uid in rest)
