"""`maintain coverage`'s declaration section, over hand-built manifests (#1283)."""

from __future__ import annotations

import json
from typing import Any

from exmergo_dex_core.maintain.coverage_declarations import FACT_CAP, declarations

PROJECT = "shop"


def _uid(name: str) -> str:
    return f"model.{PROJECT}.{name}"


def _model(name: str, *, dex=None, columns=None, reads=()) -> dict:
    cols: dict[str, Any] = {}
    for column, col_dex in (columns or {}).items():
        entry: dict[str, Any] = {"name": column}
        if col_dex is not None:
            entry["config"] = {"meta": {"dex": col_dex}}
        cols[column] = entry
    config: dict[str, Any] = {"materialized": "table", "enabled": True}
    if dex is not None:
        config["meta"] = {"dex": dex}
    return {
        "resource_type": "model",
        "name": name,
        "package_name": PROJECT,
        "original_file_path": f"models/{name}.sql",
        "config": config,
        "columns": cols,
        "depends_on": {"nodes": [_uid(r) for r in reads]},
    }


def _test(
    test: str,
    on: str,
    column: str | None = None,
    *,
    kwargs=None,
    reads=None,
    enabled=True,
    namespace=None,
) -> dict:
    return {
        "resource_type": "test",
        "name": f"{test}_{on}_{column}",
        "package_name": PROJECT,
        "attached_node": _uid(on),
        "column_name": column,
        "test_metadata": {
            "name": test,
            "namespace": namespace,
            "kwargs": kwargs or {},
        },
        "config": {"severity": "ERROR", "enabled": enabled},
        "depends_on": {"nodes": [_uid(r) for r in (reads or [on])]},
    }


def _manifest(models: list[dict], tests: list[dict] = ()) -> dict:
    nodes = {_uid(m["name"]): m for m in models}
    for i, test in enumerate(tests):
        nodes[f"test.{PROJECT}.t{i}"] = test
    return {"metadata": {"project_name": PROJECT}, "nodes": nodes}


def _run(manifest: dict, **kw) -> dict:
    selected = {
        uid for uid, n in manifest["nodes"].items() if n["resource_type"] == "model"
    }
    return declarations(manifest, selected=selected, **kw)


def _fact(out: dict, kind: str, column: str | None = None, model: str | None = None):
    hits = [
        f
        for f in out["facts"]
        if f["fact"] == kind
        and (column is None or f.get("column") == column)
        and (model is None or f["model"] == model)
    ]
    assert len(hits) == 1, (kind, column, out["facts"])
    return hits[0]


def test_composite_grain_with_and_without_combination_test() -> None:
    manifest = _manifest(
        [
            _model("a", dex={"grain": ["x", "y"]}),
            _model("b", dex={"grain": ["x", "y"]}),
        ],
        [
            _test(
                "unique_combination_of_columns",
                "a",
                kwargs={"combination_of_columns": ["y", "x"]},
                namespace="dbt_utils",
            )
        ],
    )
    out = _run(manifest)
    a = _fact(out, "grain", model="a")
    assert a["checked_by"] == "both"
    assert a["tests"] == ["dbt_utils.unique_combination_of_columns"]
    assert a["verify"] == ["grain_broken"]
    assert _fact(out, "grain", model="b")["checked_by"] == "dex_verify"


def test_single_grain_with_unique_and_key_role() -> None:
    manifest = _manifest(
        [
            _model(
                "orders",
                dex={"grain": ["order_id"]},
                columns={"order_id": {"role": "key"}, "alt_id": {"role": "key"}},
            )
        ],
        [
            _test("unique", "orders", "order_id"),
            _test("not_null", "orders", "order_id"),
        ],
    )
    out = _run(manifest)
    assert _fact(out, "grain")["checked_by"] == "both"
    assert _fact(out, "key", "order_id")["checked_by"] == "both"
    assert _fact(out, "key", "alt_id")["checked_by"] == "none"


def test_null_rule_never_and_disabled_test_does_not_count() -> None:
    manifest = _manifest(
        [
            _model(
                "m",
                columns={
                    "a": {"null_rule": "never"},
                    "b": {"null_rule": "never"},
                    "c": {"null_rule": "zero when no children"},
                },
            )
        ],
        [_test("not_null", "m", "a"), _test("not_null", "m", "b", enabled=False)],
    )
    out = _run(manifest)
    assert _fact(out, "null_rule", "a")["checked_by"] == "dbt_test"
    assert _fact(out, "null_rule", "a")["tests"] == ["not_null"]
    assert _fact(out, "null_rule", "b")["checked_by"] == "none"
    assert _fact(out, "null_rule", "c")["checked_by"] == "prose"


def test_references_with_and_without_relationships_test() -> None:
    manifest = _manifest(
        [
            _model("customers"),
            _model(
                "orders",
                columns={
                    "customer_id": {
                        "role": "foreign_key",
                        "references": "customers.customer_id",
                    },
                    "store_id": {"references": "stores.store_id"},
                    "loose_fk": {"role": "foreign_key"},
                },
                reads=["customers"],
            ),
            _model("stores"),
        ],
        [
            _test(
                "relationships",
                "orders",
                "customer_id",
                reads=["orders", "customers"],
            )
        ],
    )
    out = _run(manifest)
    joined = _fact(out, "references", "customer_id")
    assert joined["checked_by"] == "both"
    assert joined["verify"] == ["join_no_match", "join_orphans"]
    unjoined = _fact(out, "references", "store_id")
    assert unjoined["checked_by"] == "none"
    assert _fact(out, "foreign_key", "loose_fk")["checked_by"] == "none"


def test_population_filters_parent_rule_and_history() -> None:
    manifest = _manifest(
        [
            _model("subs"),
            _model(
                "prose_only",
                dex={"population": {"rule": "SECRET_RULE_TEXT only"}},
            ),
            _model(
                "filtered",
                dex={
                    "population": {
                        "rule": "SECRET_RULE_TEXT",
                        "filters": [
                            {"column": "status", "excludes": ["SENTINEL_VALUE_1"]},
                            {"column": "region", "includes": ["SENTINEL_VALUE_2"]},
                        ],
                        "parent": {
                            "model": "subs",
                            "key": {"subscription_id": "subscription_id"},
                        },
                    },
                    "history": {"snapshot": "snap_subs", "entity": "id"},
                },
            ),
        ]
    )
    out = _run(manifest)
    assert _fact(out, "population_rule", model="prose_only")["checked_by"] == "prose"
    assert _fact(out, "population_filter", "status")["verify"] == [
        "population_contradicted"
    ]
    parent = _fact(out, "population_parent", model="filtered")
    assert parent["checked_by"] == "dex_verify"
    assert parent["column"] == "subscription_id"
    assert _fact(out, "history")["checked_by"] == "dex_verify"
    # A rule beside a checkable form is not a separate prose fact.
    assert not [
        f
        for f in out["facts"]
        if f["model"] == "filtered" and f["fact"] == "population_rule"
    ]
    for mode in (False, True):
        dumped = json.dumps(_run(manifest, values_free=mode))
        assert "SENTINEL_VALUE" not in dumped
        assert "SECRET_RULE_TEXT" not in dumped


def test_units_and_additivity() -> None:
    manifest = _manifest(
        [
            _model(
                "m",
                columns={
                    "rate": {"unit": "fraction", "additivity": "non_additive"},
                    "amount": {"unit": "currency:USD", "additivity": "additive"},
                },
            )
        ]
    )
    out = _run(manifest)
    assert _fact(out, "unit", "rate")["verify"] == ["unit_out_of_range"]
    assert _fact(out, "unit", "amount")["checked_by"] == "none"
    assert _fact(out, "additivity", "amount")["checked_by"] == "dex_verify"
    assert _fact(out, "additivity", "rate")["checked_by"] == "none"


def test_undeclared_model_is_counted_apart() -> None:
    manifest = _manifest(
        [
            _model("declared", dex={"grain": ["id"]}),
            _model("plain", columns={"id": None}),
        ]
    )
    summary = _run(manifest)["summary"]
    assert summary["models_declared"] == {
        "numerator": 1,
        "denominator": 2,
        "of": "selected enabled project models",
    }
    assert summary["models_undeclared"] == 1
    assert summary["facts"]["count"] == 1
    assert summary["facts"]["checked"]["denominator"] == 1


def test_summary_and_selection() -> None:
    manifest = _manifest(
        [
            _model("a", columns={"x": {"null_rule": "never"}}),
            _model("b", columns={"y": {"null_rule": "never"}}),
        ],
        [_test("not_null", "a", "x")],
    )
    out = declarations(manifest, selected={_uid("a")})
    assert [f["model"] for f in out["facts"]] == ["a"]
    facts = out["summary"]["facts"]
    assert facts["by_kind"]["null_rule"]["dbt_test"] == 1
    assert facts["by_checker"]["dbt_test"] == 1
    assert facts["checked"]["numerator"] == 1


def test_cap_lists_unchecked_first_and_counts_the_cut() -> None:
    columns = {f"c{i}": {"unit": "currency:USD"} for i in range(FACT_CAP + 5)}
    columns["r"] = {"unit": "fraction"}
    manifest = _manifest([_model("wide", columns=columns)])
    out = _run(manifest)
    assert len(out["facts"]) == FACT_CAP
    assert out["elided"] == 6
    assert all(f["checked_by"] == "none" for f in out["facts"])
    assert any("not listed" in note for note in out["notes"])
    assert out["summary"]["facts"]["count"] == FACT_CAP + 6
    full = _run(manifest, full=True)
    assert len(full["facts"]) == FACT_CAP + 6
    assert full["elided"] == 0


def test_a_relationships_test_on_another_field_does_not_check_the_reference() -> None:
    """Review fix: membership in ``customers.legacy_id`` says nothing about the
    declared ``customers.customer_id``."""

    manifest = _manifest(
        [
            _model("customers"),
            _model(
                "orders",
                columns={"customer_id": {"references": "customers.customer_id"}},
            ),
        ],
        [
            _test(
                "relationships",
                "orders",
                "customer_id",
                reads=["orders", "customers"],
                kwargs={"to": "ref('customers')", "field": "legacy_id"},
            )
        ],
    )
    assert _fact(_run(manifest), "references", "customer_id")["checked_by"] == "none"
    manifest["nodes"][next(k for k in manifest["nodes"] if k.startswith("test."))][
        "test_metadata"
    ]["kwargs"]["field"] = "customer_id"
    assert _fact(_run(manifest), "references", "customer_id")["checked_by"] == (
        "dbt_test"
    )


def test_a_self_reference_counts_its_relationships_test() -> None:
    manifest = _manifest(
        [
            _model(
                "employees",
                columns={"manager_id": {"references": "employees.employee_id"}},
            ),
        ],
        [
            _test(
                "relationships",
                "employees",
                "manager_id",
                reads=["employees"],
                kwargs={"to": "ref('employees')", "field": "employee_id"},
            )
        ],
    )
    fact = _fact(_run(manifest), "references", "manager_id")
    assert fact["checked_by"] in {"dbt_test", "both"}
    assert fact["tests"] == ["relationships"]
