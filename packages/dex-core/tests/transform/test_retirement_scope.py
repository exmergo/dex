"""A retirement scoped to its authored outputs, without dbt (#1086).

The full retirement refuses whenever the reference scan cannot be finished. A
contract that says ``scope: authored`` keeps its own output and metric checks in
that case and reports global discovery as unchecked. These cases hold the
admission rules, the report and the stored form.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.dbt_project import load as load_project
from exmergo_dex_core.transform.plans import EditKind, EditOp, PlanEdit
from exmergo_dex_core.transform.portable import PortablePlan, plan_digest
from exmergo_dex_core.transform.propagate import PropagationRefusedError, propagate
from exmergo_dex_core.transform.retirement import (
    contract_of,
    discovery_record,
    discovery_report,
    parse_contract,
    scope_warnings,
    stored_form,
    validate_scope,
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
SCOPED = {**CONTRACT, "scope": "authored"}
#: Unrelated to the flag: a generic macro the column scan cannot parse.
UNPARSEABLE = (
    "{% macro unioned(names) %}\n"
    "  {%- for name in names %} union all select * from {{ ref(name) }}"
    "{% endfor -%}\n{% endmacro %}\n"
)
#: The same, with a var read whose name is built at run time.
DYNAMIC = (
    "{% macro enabled(names) %}\n"
    "  {%- for name in names -%}\n"
    "    {%- if var('use_' ~ name, true) %} union all select * from "
    "{{ ref(name) }} {% endif -%}\n"
    "  {%- endfor -%}\n{% endmacro %}\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    _write(
        root / "dbt_project.yml",
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n'
        f"macro-paths: [macros]\nvars:\n  {FLAG}: true\n",
    )
    _write(root / "models/stg_orders.sql", "select 1 as order_id\n")
    _write(root / "models/rpt.sql", GUARDED)
    return root


def _edit(content: str = PLAIN, path: str = "models/rpt.sql") -> PlanEdit:
    return PlanEdit(path=path, new_content=content, kind=EditKind.MODEL_SQL)


def _propagate(root: Path, *edits: PlanEdit, scoped: bool = False):
    return propagate(
        load_project(root),
        root,
        "var",
        FLAG,
        extra_edits=list(edits),
        retirement=True,
        scoped=scoped,
    )


def _discovery(root: Path, *edits: PlanEdit) -> dict:
    return discovery_report(load_project(root), root, FLAG, list(edits))


# --- the contract -------------------------------------------------------------


def test_scope_defaults_to_complete_and_takes_only_the_two_values():
    assert parse_contract(CONTRACT).scope == "complete"
    assert parse_contract(SCOPED).scope == "authored"
    with pytest.raises(ValueError, match=r"scope"):
        parse_contract({**CONTRACT, "scope": "everything"})


def test_a_complete_retirement_stores_exactly_what_it_stored_before():
    form = stored_form(FLAG, parse_contract(CONTRACT))
    assert "scope" not in form
    assert "discovery" not in form


def test_a_scoped_retirement_stores_its_scope_and_discovery_and_reads_back():
    discovery = {"scope": "authored", "status": "unchecked"}
    form = stored_form(FLAG, parse_contract(SCOPED), discovery)
    assert form["scope"] == "authored"
    assert form["discovery"] == discovery
    assert contract_of(form).scope == "authored"


def test_the_digest_moves_when_only_the_scope_changes():
    def digest(retirement: dict) -> str:
        return plan_digest(
            PortablePlan(
                plan_id="p1",
                intent="retire",
                created_at="2026-11-05T00:00:00Z",
                project_dir=".",
                retirement=retirement,
            )
        )

    discovery = {"status": "unchecked"}
    full = stored_form(FLAG, parse_contract(CONTRACT), discovery)
    scoped = stored_form(FLAG, parse_contract(SCOPED), discovery)
    assert digest(full) != digest(scoped)


# --- the full path keeps refusing, and names the route ------------------------


def test_the_full_retirement_still_refuses_an_incomplete_scan(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    with pytest.raises(PropagationRefusedError) as refused:
        _propagate(root, _edit())
    message = str(refused.value)
    assert "macros/unioned.sql" in message
    assert "a removal applied to most of a project still compiles" in message
    assert '"scope": "authored"' in message


def test_incomplete_scan_names_the_requested_verb(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    with pytest.raises(PropagationRefusedError) as exc_rename:
        propagate(load_project(root), root, "var", FLAG, "new_flag")
    rename_msg = str(exc_rename.value)
    assert "a rename applied to most of a project still compiles" in rename_msg
    assert '"scope": "authored"' not in rename_msg

    with pytest.raises(PropagationRefusedError) as exc_remove:
        propagate(load_project(root), root, "var", FLAG)
    remove_msg = str(exc_remove.value)
    assert "a removal applied to most of a project still compiles" in remove_msg
    assert '"scope": "authored"' not in remove_msg


def test_a_scoped_retirement_reports_a_dynamic_read_instead_of_refusing(
    root: Path,
):
    _write(root / "macros/enabled.sql", DYNAMIC)
    assert _propagate(root, _edit(), scoped=True).edits


def test_a_dynamic_read_alone_still_refuses_without_scope(root: Path):
    # A macro the column scan reads, so the dynamic read is the only reason.
    _write(
        root / "macros/enabled.sql",
        "{% macro enabled(name) %}{{ var('use_' ~ name, true) }}{% endmacro %}\n",
    )
    with pytest.raises(PropagationRefusedError, match=r"name no literal variable"):
        _propagate(root, _edit())


# --- discovery ----------------------------------------------------------------


def test_a_project_with_nothing_unread_is_complete(root: Path):
    report = _discovery(root, _edit())
    assert report["status"] == "complete"
    assert "reason_code" not in report


def test_an_unparseable_macro_and_a_dynamic_read_are_reported(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(root / "macros/enabled.sql", DYNAMIC)
    report = _discovery(root, _edit())
    assert report["status"] == "unchecked"
    assert report["reason_code"] == "discovery_incomplete"
    assert any(limit.startswith("macros/unioned.sql") for limit in report["limits"])
    assert report["dynamic_reads"] == ["macros/enabled.sql:3"]


def test_a_dynamic_read_the_payload_rewrites_is_not_counted(root: Path):
    _write(root / "macros/enabled.sql", DYNAMIC)
    fixed = PlanEdit(
        path="macros/enabled.sql",
        new_content="{% macro enabled(names) %}{% endmacro %}\n",
        kind=EditKind.MACRO_SQL,
    )
    assert _discovery(root, _edit(), fixed)["dynamic_reads"] == []


def test_package_limits_are_counted_per_package_and_never_listed(root: Path):
    _write(root / "packages.yml", "packages:\n  - local: pkgs/utils\n")
    utils = root / "dbt_packages/utils"
    _write(
        utils / "dbt_project.yml",
        'name: utils\nversion: "1.0"\nprofile: shop\nmacro-paths: [macros]\n',
    )
    for name in ("a", "b", "c"):
        _write(utils / f"macros/{name}.sql", UNPARSEABLE)
    report = _discovery(root, _edit())
    assert report["status"] == "unchecked"
    assert report["package_limits"] == {"utils": 3}
    assert report["limits"] == []


def test_project_limits_are_capped_with_the_rest_counted(root: Path):
    for n in range(13):
        _write(root / f"macros/m{n:02}.sql", UNPARSEABLE)
    report = _discovery(root, _edit())
    assert len(report["limits"]) == 10
    assert report["limits_elided"] == 3


# --- admission ----------------------------------------------------------------


def _validate(root: Path, contract: dict, *edits: PlanEdit) -> None:
    view = load_project(root)
    validate_scope(
        view,
        list(edits),
        parse_contract(contract),
        discovery_report(view, root, FLAG, list(edits)),
    )


def test_a_scoped_contract_is_refused_where_the_full_check_can_run(root: Path):
    with pytest.raises(ValueError, match=r"discovery is complete"):
        _validate(root, SCOPED, _edit())


def test_a_scoped_contract_is_admitted_where_the_scan_cannot_finish(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(root / "macros/enabled.sql", DYNAMIC)
    _validate(root, SCOPED, _edit())
    outcome = _propagate(root, _edit(), scoped=True)
    removed = [e for e in outcome.edits if e.path == "dbt_project.yml"]
    assert removed, "the definition is still removed by Dex"
    assert FLAG not in (removed[0].new_content or "")


def test_a_literal_read_that_survives_is_refused_even_when_scoped(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    with pytest.raises(PropagationRefusedError, match=r"read\(s\) behind"):
        _propagate(root, scoped=True)


def test_a_read_in_yaml_is_refused_even_when_scoped(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(
        root / "models/schema.yml",
        "version: 2\nmodels:\n  - name: rpt\n    config:\n"
        f"      enabled: \"{{{{ var('{FLAG}') }}}}\"\n",
    )
    with pytest.raises(PropagationRefusedError, match=r"read in YAML"):
        _propagate(root, _edit(), scoped=True)


def test_an_output_model_the_project_does_not_define_is_refused(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    contract = {
        **SCOPED,
        "outputs": {
            **SCOPED["outputs"],
            "rpt_typo": {"expected": ["order_id"]},
        },
    }
    with pytest.raises(ValueError, match=r"rpt_typo is not one"):
        _validate(root, contract, _edit())


def test_a_package_model_is_out_of_scope(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(root / "packages.yml", "packages:\n  - local: pkgs/tiny\n")
    tiny = root / "dbt_packages/tiny"
    _write(
        tiny / "dbt_project.yml",
        'name: tiny\nversion: "1.0"\nprofile: shop\nmodel-paths: [models]\n',
    )
    _write(tiny / "models/pkg_reader.sql", f"select {{{{ var('{FLAG}', false) }}}}\n")
    contract = {
        **SCOPED,
        "outputs": {**SCOPED["outputs"], "pkg_reader": {"expected": ["flag"]}},
    }
    with pytest.raises(ValueError, match=r"pkg_reader is not one"):
        _validate(root, contract, _edit())


def test_a_removed_metric_the_project_never_defined_is_refused(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(
        root / "models/metrics.yml",
        "version: 2\nmetrics:\n  - name: revenue_by_region\n    label: r\n"
        "    type: simple\n    type_params:\n      measure: revenue\n",
    )
    contract = {**SCOPED, "metrics": {"removed": ["revenue_by_regoin"]}}
    with pytest.raises(ValueError, match=r"revenue_by_regoin is not one"):
        _validate(root, contract, _edit())
    _validate(root, {**SCOPED, "metrics": {"removed": ["revenue_by_region"]}}, _edit())


def test_an_authored_model_without_outputs_is_refused(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    other = _edit("select 1 as order_id\n", "models/stg_orders.sql")
    with pytest.raises(ValueError, match=r"rewrites stg_orders without"):
        _validate(root, SCOPED, _edit(), other)


def test_a_deleted_model_needs_no_outputs(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(root / "models/rpt_region.sql", "select 1 as region_label\n")
    gone = PlanEdit(
        path="models/rpt_region.sql", kind=EditKind.MODEL_SQL, op=EditOp.DELETE
    )
    _validate(root, SCOPED, _edit(), gone)


# --- what is reported ---------------------------------------------------------


def test_the_warning_bounds_the_claim(root: Path):
    _write(root / "macros/unioned.sql", UNPARSEABLE)
    _write(root / "macros/enabled.sql", DYNAMIC)
    contract = parse_contract(
        {**SCOPED, "metrics": {"retained": ["revenue"], "removed": ["by_region"]}}
    )
    [warning] = scope_warnings(FLAG, contract, _discovery(root, _edit()))
    assert "scoped to the outputs it names (rpt; metrics revenue, by_region)" in (
        warning
    )
    assert "Reference discovery is unchecked" in warning
    assert "macros/enabled.sql:3" in warning
    assert "does not show every reader was found" in warning


def test_the_build_record_for_discovery_is_never_satisfied():
    stored = stored_form(
        FLAG,
        parse_contract(SCOPED),
        {"status": "unchecked", "reason_code": "discovery_incomplete"},
    )
    assert discovery_record(FLAG, stored) == {
        "model": FLAG,
        "check": "reference_discovery",
        "status": "unchecked",
        "reason_code": "discovery_incomplete",
    }


def test_a_finished_retirement_is_recognised_despite_a_dynamic_read(root: Path):
    from exmergo_dex_core.transform.retirement import is_already_complete

    _write(root / "macros/enabled.sql", DYNAMIC)
    _write(root / "models/rpt.sql", PLAIN)
    _write(
        root / "dbt_project.yml",
        'name: shop\nprofile: shop\nversion: "1.0"\nmodel-paths: [models]\n'
        "macro-paths: [macros]\n",
    )
    view = load_project(root)
    assert not is_already_complete(view, FLAG, [_edit()])
    assert is_already_complete(view, FLAG, [_edit()], allow_dynamic=True)


def test_an_unreadable_contract_keeps_every_obligation(tmp_path: Path):
    from exmergo_dex_core.transform.verify import (
        BuildVerification,
        _retirement_contract,
    )

    (tmp_path / "dbt_project.yml").write_text(
        'name: shop\nversion: "1.0"\nprofile: shop\n', encoding="utf-8"
    )

    def broken_adapter():
        raise OSError("driver went away")

    stored = stored_form(
        FLAG,
        parse_contract(
            {**SCOPED, "metrics": {"retained": ["revenue"], "removed": ["by_region"]}}
        ),
        {"status": "unchecked", "reason_code": "discovery_incomplete"},
    )
    result = BuildVerification(ran=True)
    _retirement_contract(
        tmp_path,
        stored,
        {"model.shop.rpt": "rpt"},
        {"model.shop.rpt"},
        broken_adapter,
        result,
    )
    assert "retirement" in result.suppressed
    by_model = {r["model"]: r for r in result.retirement}
    assert set(by_model) == {"rpt", "revenue", "by_region", FLAG}
    assert {r["status"] for r in result.retirement} == {"unchecked"}
    assert by_model["by_region"]["expected"] == "removed"
    assert by_model[FLAG]["check"] == "reference_discovery"
