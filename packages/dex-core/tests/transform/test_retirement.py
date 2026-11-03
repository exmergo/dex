"""The retirement contract without dbt: parsing, refusals, the no-op (#858)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.transform.native_semantic import (
    edits_from_payload,
    payload_from_file,
    removal_payload,
)
from exmergo_dex_core.transform.plans import EditKind, PlanEdit
from exmergo_dex_core.transform.propagate import PropagationRefusedError, propagate
from exmergo_dex_core.transform.retirement import (
    decisions,
    is_already_complete,
    metric_records,
    package_scope,
    package_warnings,
    parse_contract,
    scoped_definitions,
    stored_form,
    yaml_reads,
)

FLAG = "use_region_enrichment"
GUARDED = (
    "select o.order_id{% if var('use_region_enrichment') %}, 'x' as region_label"
    "{% endif %} from {{ ref('stg_orders') }} o\n"
)
PLAIN = "select o.order_id from {{ ref('stg_orders') }} o\n"
CONTRACT = {
    "behavior": "retire",
    "retained_value": False,
    "outputs": {"rpt": {"expected": ["order_id"], "removed": ["region_label"]}},
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    _write(
        root / "dbt_project.yml",
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n'
        f"vars:\n  {FLAG}: true\n",
    )
    _write(root / "models/stg_orders.sql", "select 1 as order_id\n")
    _write(root / "models/rpt.sql", GUARDED)
    return root


def _read_edit(content: str = PLAIN) -> PlanEdit:
    return PlanEdit(path="models/rpt.sql", new_content=content, kind=EditKind.MODEL_SQL)


def _propagate(root: Path, *edits: PlanEdit, retirement: bool = True):
    return propagate(
        load_project(root),
        root,
        "var",
        FLAG,
        extra_edits=list(edits),
        retirement=retirement,
    )


# --- the contract -------------------------------------------------------------


def test_retained_value_is_required_and_never_defaulted():
    raw = {k: v for k, v in CONTRACT.items() if k != "retained_value"}
    with pytest.raises(ValueError, match=r"retained_value"):
        parse_contract(raw)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"behavior": "keep"}, "behavior"),
        ({"packages": "in_scope"}, "packages"),
        ({"outputs": {}}, "outputs"),
        ({"outputs": {"rpt": {"expected": []}}}, "expected"),
        ({"outputs": {"rpt": {"removed": ["a"]}}}, "expected"),
        ({"outputs": {"rpt": {"expected": ["A"], "removed": ["a"]}}}, "both"),
        ({"retained_value": [True]}, "scalar"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_a_malformed_contract_is_refused_by_name(change, fragment):
    with pytest.raises(ValueError, match=fragment):
        parse_contract({**CONTRACT, **change})


def test_a_null_retained_value_is_a_statement_not_an_absence():
    assert parse_contract({**CONTRACT, "retained_value": None}).retained_value is None


def test_assumptions_become_decisions():
    contract = parse_contract(
        {
            **CONTRACT,
            "assumptions": [
                {
                    "decision": "Which branch",
                    "chosen": "disabled",
                    "evidence": "request",
                    "column": "region_label",
                }
            ],
        }
    )
    assert decisions(stored_form(FLAG, contract)) == [
        {
            "retirement": FLAG,
            "decision": "Which branch",
            "chosen": "disabled",
            "evidence": "request",
            "column": "region_label",
        }
    ]


# --- the payload --------------------------------------------------------------


def test_only_the_removal_reads_the_contract(tmp_path: Path):
    payload = tmp_path / "edits.json"
    payload.write_text('{"retirement": {"behavior": "retire"}}', encoding="utf-8")
    assert removal_payload(str(payload)) == ([], {"behavior": "retire"})
    with pytest.raises(ValueError, match=r"retirement contract"):
        payload_from_file(str(payload))
    with pytest.raises(ValueError, match=r"retirement contract"):
        edits_from_payload(str(payload))


# --- the removal --------------------------------------------------------------


def test_without_a_contract_the_refusals_are_unchanged(root: Path):
    _write(root / "models/rpt.sql", PLAIN)
    (root / "dbt_project.yml").write_text(
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n',
        encoding="utf-8",
    )
    with pytest.raises(PropagationRefusedError, match="nothing to change"):
        _propagate(root, retirement=False)


def test_a_read_only_flag_needs_no_definition(root: Path):
    (root / "dbt_project.yml").write_text(
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n',
        encoding="utf-8",
    )
    _write(root / "models/rpt.sql", GUARDED.replace(f"'{FLAG}'", f"'{FLAG}', true"))
    outcome = _propagate(root, _read_edit())
    assert [edit.path for edit in outcome.edits] == ["models/rpt.sql"]


def test_a_surviving_read_is_still_refused_by_file_and_line(root: Path):
    with pytest.raises(PropagationRefusedError, match=r"models/rpt\.sql:1"):
        _propagate(root)


def test_a_local_dynamic_read_is_refused_then_cleared_in_the_same_payload(root: Path):
    _write(root / "models/dyn.sql", "select {{ var(flag_name) }} as x\n")
    with pytest.raises(PropagationRefusedError, match=r"models/dyn.sql:1.*re-run"):
        _propagate(root, _read_edit())
    repaired = PlanEdit(
        path="models/dyn.sql", new_content="select 1 as x\n", kind=EditKind.MODEL_SQL
    )
    assert _propagate(root, _read_edit(), repaired).edits


def test_a_read_in_yaml_is_refused(root: Path):
    _write(
        root / "models/rpt.yml",
        "version: 2\nmodels:\n  - name: rpt\n    config:\n"
        f"      enabled: \"{{{{ var('{FLAG}') }}}}\"\n",
    )
    with pytest.raises(PropagationRefusedError, match=r"models/rpt.yml:\d"):
        _propagate(root, _read_edit())


def test_yaml_reads_ignore_other_variables():
    files = {
        "a.yml": "x: \"{{ var('other') }}\"\n",
        "b.sql": f"{{{{ var('{FLAG}') }}}}",
    }
    assert yaml_reads(files, FLAG) == []


# --- packages -----------------------------------------------------------------


def _package(root: Path, sql: str, *, vars_block: str = "") -> None:
    _write(root / "packages.yml", "packages:\n  - local: pkgs/tiny\n")
    _write(root / "package-lock.yml", "packages:\n  - local: pkgs/tiny\n")
    _write(
        root / "dbt_packages/tiny/dbt_project.yml",
        f'name: tiny\nversion: "1.0"\nprofile: shop\n{vars_block}',
    )
    _write(root / "dbt_packages/tiny/models/pkg_reader.sql", sql)


def test_a_package_reader_is_reported_and_never_edited(root: Path):
    _package(root, f"select {{{{ var('{FLAG}', false) }}}} as flag\n")
    view = load_project(root)
    outcome = _propagate(root, _read_edit())
    assert not [e for e in outcome.edits if e.path.startswith("dbt_packages")]
    scope = package_scope(view, root, FLAG)
    assert scope["readers"] == [
        {
            "package": "tiny",
            "path": "dbt_packages/tiny/models/pkg_reader.sql",
            "line": 1,
            "has_default": True,
        }
    ]
    assert not any("stops compiling" in w for w in package_warnings(FLAG, scope))


def test_a_package_read_with_no_default_warns_it_stops_compiling(root: Path):
    _package(root, f"select {{{{ var('{FLAG}') }}}} as flag\n")
    scope = package_scope(load_project(root), root, FLAG)
    assert scope["readers"][0]["has_default"] is False
    assert any("stops compiling" in w for w in package_warnings(FLAG, scope))


def test_a_package_dynamic_reader_is_a_bounded_report_not_a_refusal(root: Path):
    _package(root, "select {{ var(some_name) }} as flag\n")
    assert _propagate(root, _read_edit()).edits
    scope = package_scope(load_project(root), root, FLAG)
    assert scope["dynamic_readers"][0]["package"] == "tiny"
    assert any("pin the value" in w for w in package_warnings(FLAG, scope))


def test_a_package_defining_the_same_name_is_reported_not_refused(root: Path):
    _package(root, "select 1 as flag\n", vars_block=f"vars:\n  {FLAG}: false\n")
    assert _propagate(root, _read_edit()).edits
    assert package_scope(load_project(root), root, FLAG)["defined_by"] == ["tiny"]


def test_a_package_scoped_entry_in_the_project_file_is_read(root: Path):
    (root / "dbt_project.yml").write_text(
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n'
        f"vars:\n  tiny:\n    {FLAG}: true\n",
        encoding="utf-8",
    )
    view = load_project(root)
    assert scoped_definitions(view, FLAG) == ["tiny"]
    assert package_scope(view, root, FLAG)["scoped_definitions"] == ["tiny"]


# --- the no-op ----------------------------------------------------------------


def _retired(root: Path) -> None:
    _write(root / "models/rpt.sql", PLAIN)
    (root / "dbt_project.yml").write_text(
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n',
        encoding="utf-8",
    )


def test_a_repeated_payload_on_a_finished_retirement_is_a_no_op(root: Path):
    _retired(root)
    assert is_already_complete(load_project(root), FLAG, [_read_edit()])
    gone = PlanEdit(path="models/gone.sql", op="delete", kind=EditKind.MODEL_SQL)
    assert is_already_complete(load_project(root), FLAG, [gone])


def test_a_payload_that_changes_a_file_is_not_a_no_op(root: Path):
    _retired(root)
    assert not is_already_complete(
        load_project(root), FLAG, [_read_edit("select 2 as order_id\n")]
    )


def test_a_project_that_still_reads_the_flag_is_not_a_no_op(root: Path):
    assert not is_already_complete(load_project(root), FLAG, [])


def test_a_project_scoped_definition_left_behind_is_not_a_no_op(root: Path):
    _retired(root)
    (root / "dbt_project.yml").write_text(
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n'
        f"vars:\n  shop:\n    {FLAG}: true\n",
        encoding="utf-8",
    )
    assert not is_already_complete(load_project(root), FLAG, [])


# --- metrics ------------------------------------------------------------------


class _Index:
    def __init__(self, defined: set[str]) -> None:
        self.defined = defined

    def definitions_of(self, name: str, kind: str) -> list[str]:
        return [name] if name in self.defined else []


def test_metrics_are_held_to_the_definitions_in_both_directions():
    contract = parse_contract(
        {**CONTRACT, "metrics": {"retained": ["revenue", "lost"], "removed": ["left"]}}
    )
    records = metric_records(_Index({"revenue", "left"}), contract)
    assert {r["model"]: (r["status"], r.get("finding")) for r in records} == {
        "revenue": ("satisfied", None),
        "lost": ("contradicted", "retirement_metric_missing"),
        "left": ("contradicted", "retirement_metric_present"),
    }
