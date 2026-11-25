"""`maintain coverage`'s declaration section, over hand-built manifests (#1283)."""

from __future__ import annotations

import json
from typing import Any

from exmergo_dex_core.maintain.coverage_declarations import FACT_CAP, declarations
from exmergo_dex_core.transform.claims import claim_id

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
    claim=None,
) -> dict:
    config: dict[str, Any] = {"severity": "ERROR", "enabled": enabled}
    if claim is not None:
        config["meta"] = {"dex": {"claim": claim}}
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
        "config": config,
        # A name, or a full unique id (a package's or a seed's), as dbt
        # resolves the test's `to` into its dependencies.
        "depends_on": {
            "nodes": [r if r.count(".") >= 2 else _uid(r) for r in (reads or [on])]
        },
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


def test_a_freshness_is_checked_by_verify_and_by_its_generated_test() -> None:
    fresh = {"role": "time", "freshness": {"max_lag": "6h", "basis": "capture"}}
    manifest = _manifest(
        [
            _model("tested", columns={"loaded_at": fresh}),
            _model("untested", columns={"loaded_at": fresh}),
        ],
        [
            _test(
                "dex_freshness",
                "tested",
                "loaded_at",
                kwargs={"max_lag_seconds": 21600},
            ),
            # Left at an older, looser lag: permits what the declaration forbids.
            _test(
                "dex_freshness",
                "untested",
                "loaded_at",
                kwargs={"max_lag_seconds": 86400},
            ),
        ],
    )
    out = _run(manifest)
    tested = _fact(out, "freshness", "loaded_at", model="tested")
    assert (tested["checked_by"], tested["tests"], tested["verify"]) == (
        "both",
        ["dex_freshness"],
        ["freshness_exceeded"],
    )
    assert _fact(out, "freshness", "loaded_at", model="untested")["checked_by"] == (
        "dex_verify"
    )


def test_a_continuity_is_checked_by_its_generated_test_alone() -> None:
    gapless = {"role": "time", "continuity": {"grain": "day", "by": ["region"]}}
    two_keys = {
        "role": "time",
        "continuity": {"grain": "day", "by": ["region", "channel"]},
    }
    manifest = _manifest(
        [
            _model("tested", columns={"ordered_at": gapless}),
            _model("other_grain", columns={"ordered_at": gapless}),
            _model("other_keys", columns={"ordered_at": gapless}),
            _model("untested", columns={"ordered_at": gapless}),
            _model("reordered", columns={"ordered_at": two_keys}),
        ],
        [
            _test(
                "dex_continuity",
                "tested",
                "ordered_at",
                kwargs={"grain": "day", "by": ["Region"]},
            ),
            # Searching other buckets, or the whole table: not the declaration.
            _test(
                "dex_continuity",
                "other_grain",
                "ordered_at",
                kwargs={"grain": "week", "by": ["region"]},
            ),
            _test(
                "dex_continuity", "other_keys", "ordered_at", kwargs={"grain": "day"}
            ),
            # The same two keys in another order: the same partitions.
            _test(
                "dex_continuity",
                "reordered",
                "ordered_at",
                kwargs={"grain": "day", "by": ["channel", "region"]},
            ),
        ],
    )
    out = _run(manifest)
    tested = _fact(out, "continuity", "ordered_at", model="tested")
    assert (tested["checked_by"], tested["tests"], tested["verify"]) == (
        "dbt_test",
        ["dex_continuity"],
        [],
    )
    reordered = _fact(out, "continuity", "ordered_at", model="reordered")
    assert reordered["checked_by"] == "dbt_test"
    for model in ("other_grain", "other_keys", "untested"):
        fact = _fact(out, "continuity", "ordered_at", model=model)
        assert fact["checked_by"] == "none", model


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


def test_a_self_reference_is_not_checked_by_a_test_on_another_model() -> None:
    manifest = _manifest(
        [
            _model("categories"),
            _model(
                "nodes",
                columns={"parent_id": {"references": "nodes.id"}},
            ),
        ],
        [
            _test(
                "relationships",
                "nodes",
                "parent_id",
                reads=["nodes", "categories"],
                kwargs={"to": "ref('categories')", "field": "id"},
            )
        ],
    )
    assert _fact(_run(manifest), "references", "parent_id")["checked_by"] == "none"
    # Without a readable `to`, the other relation it reads is still the target.
    test_id = next(k for k in manifest["nodes"] if k.startswith("test."))
    del manifest["nodes"][test_id]["test_metadata"]["kwargs"]["to"]
    assert _fact(_run(manifest), "references", "parent_id")["checked_by"] == "none"


def test_a_package_qualified_ref_to_a_same_named_model_does_not_check() -> None:
    manifest = _manifest(
        [_model("nodes", columns={"parent_id": {"references": "nodes.id"}})],
        [
            _test(
                "relationships",
                "nodes",
                "parent_id",
                reads=["nodes", "model.other_pkg.nodes"],
                kwargs={"to": "ref('other_pkg', 'nodes')", "field": "id"},
            )
        ],
    )
    other = _model("nodes")
    other["package_name"] = "other_pkg"
    manifest["nodes"]["model.other_pkg.nodes"] = other
    assert _fact(_run(manifest), "references", "parent_id")["checked_by"] == "none"
    # The same test pointed at the model itself reads only the model.
    test_id = next(k for k in manifest["nodes"] if k.startswith("test."))
    manifest["nodes"][test_id]["depends_on"]["nodes"] = [_uid("nodes")]
    assert _fact(_run(manifest), "references", "parent_id")["tests"] == [
        "relationships"
    ]


def test_a_package_only_parent_matches_its_package_qualified_ref() -> None:
    manifest = _manifest(
        [_model("orders", columns={"customer_id": {"references": "customers.id"}})],
        [
            _test(
                "relationships",
                "orders",
                "customer_id",
                reads=["orders", "model.crm.customers"],
                kwargs={"to": "ref('crm', 'customers')", "field": "id"},
            )
        ],
    )
    # The parent lives only in the installed package `crm`.
    package_model = _model("customers")
    package_model["package_name"] = "crm"
    manifest["nodes"]["model.crm.customers"] = package_model
    fact = _fact(_run(manifest), "references", "customer_id")
    assert fact["tests"] == ["relationships"]


def test_a_same_named_package_seed_is_not_the_project_seed() -> None:
    manifest = _manifest(
        [_model("orders", columns={"country": {"references": "countries.code"}})],
        [
            _test(
                "relationships",
                "orders",
                "country",
                reads=["orders", "seed.crm.countries"],
                kwargs={"to": "ref('crm', 'countries')", "field": "code"},
            )
        ],
    )
    for uid, package in (
        ("seed.shop.countries", PROJECT),
        ("seed.crm.countries", "crm"),
    ):
        manifest["nodes"][uid] = {
            "resource_type": "seed",
            "name": "countries",
            "package_name": package,
        }
    assert _fact(_run(manifest), "references", "country")["checked_by"] == "none"
    test_id = next(k for k in manifest["nodes"] if k.startswith("test."))
    manifest["nodes"][test_id]["depends_on"]["nodes"] = [
        _uid("orders"),
        "seed.shop.countries",
    ]
    assert _fact(_run(manifest), "references", "country")["tests"] == ["relationships"]


def test_an_unversioned_reference_is_the_latest_version() -> None:
    manifest = _manifest(
        [_model("orders", columns={"customer_id": {"references": "customers.id"}})],
        [
            _test(
                "relationships",
                "orders",
                "customer_id",
                reads=["orders", "model.shop.customers.v2"],
                kwargs={"to": "ref('customers')", "field": "id"},
            )
        ],
    )
    for version in (1, 2):
        node = _model("customers")
        node.update({"version": version, "latest_version": 2})
        manifest["nodes"][f"model.shop.customers.v{version}"] = node
    assert _fact(_run(manifest), "references", "customer_id")["tests"] == [
        "relationships"
    ]
    # A test on the older version does not check a reference to the latest.
    test_id = next(k for k in manifest["nodes"] if k.startswith("test."))
    manifest["nodes"][test_id]["depends_on"]["nodes"] = [
        _uid("orders"),
        "model.shop.customers.v1",
    ]
    assert _fact(_run(manifest), "references", "customer_id")["checked_by"] == "none"


def test_a_conditional_to_expression_is_not_read_as_its_first_ref() -> None:
    manifest = _manifest(
        [
            _model("customers"),
            _model("vendors"),
            _model(
                "orders",
                columns={"customer_id": {"references": "customers.id"}},
            ),
        ],
        [
            _test(
                "relationships",
                "orders",
                "customer_id",
                reads=["orders", "vendors"],
                kwargs={
                    "to": "{{ ref('customers') if false else ref('vendors') }}",
                    "field": "id",
                },
            )
        ],
    )
    assert _fact(_run(manifest), "references", "customer_id")["checked_by"] == "none"


# --- A native semantic layer's declarations (#1346) --------------------------

from exmergo_dex_core.maintain.snapshot import (  # noqa: E402
    RelationshipDef,
    SemanticLayer,
    SemanticModelDef,
)
from exmergo_dex_core.project_definitions import (  # noqa: E402
    DeclaredColumnMeaning,
    DeclaredPopulation,
    DeclaredPopulationFilter,
    ModelDeclaration,
)


def _layer(
    *, models=(), relationships=(), cardinality=True, captured=True
) -> SemanticLayer:
    return SemanticLayer(
        vendor="ossie",
        relationships_and_keys_captured=captured,
        relationship_cardinality_captured=cardinality,
        semantic_models=list(models),
        relationships=list(relationships),
    )


def _sm(name: str, keys, claims=None, relation=None) -> SemanticModelDef:
    return SemanticModelDef(
        name=name,
        content_sha256="0" * 64,
        relation=relation or f"analytics.{name}",
        keys=[list(k) for k in keys],
        key_claims=claims,
    )


def _rel(name: str, model: str, to: str, pairs, cardinality=None) -> RelationshipDef:
    return RelationshipDef(
        name=name,
        content_sha256="0" * 64,
        model=model,
        to_model=to,
        column_pairs=list(pairs),
        cardinality=cardinality,
    )


def test_native_keys_and_joins_are_what_maintain_grain_judges() -> None:
    layer = _layer(
        models=[
            _sm(
                "orders", [["order_id"], ["store_id", "number"]], ["primary", "unique"]
            ),
            _sm("customers", [["id"]], ["primary"]),
        ],
        relationships=[
            _rel(
                "orders_customers",
                "orders",
                "customers",
                [("customer_id", "id")],
                "many_to_one",
            ),
            _rel(
                "orders_tags", "orders", "customers", [("tag", "tag")], "many_to_many"
            ),
            _rel(
                "orders_stores",
                "orders",
                "customers",
                [("store_id", "id")],
                "one_to_many",
            ),
        ],
    )
    out = declarations(_manifest([]), selected=set(), semantic=layer)
    grain = _fact(out, "grain", "order_id", "orders")
    assert grain["source"] == "ossie"
    assert grain["checked_by"] == "dex_verify"
    assert grain["tests"] == []
    assert grain["verify"] == ["declared_grain_not_unique"]
    key = _fact(out, "key", "store_id, number", "orders")
    assert key["verify"] == ["declared_grain_not_unique"]
    joined = _fact(out, "references", "customer_id", "orders")
    assert joined["verify"] == ["declared_join_orphans", "join_cardinality_exceeded"]
    # A many-to-many join bounds nothing; one-to-many has no arity check.
    assert _fact(out, "references", "tag", "orders")["checked_by"] == "none"
    assert _fact(out, "references", "store_id", "orders")["verify"] == [
        "declared_join_orphans"
    ]
    assert out["summary"]["facts"]["by_source"] == {"ossie": 6}
    assert any("ossie semantic layer" in note for note in out["notes"])


def test_a_layer_that_captured_no_keys_or_joins_adds_none() -> None:
    layer = _layer(models=[_sm("orders", [["order_id"]], ["primary"])], captured=False)
    out = declarations(_manifest([]), selected=set(), semantic=layer)
    assert out["facts"] == []
    assert out["summary"]["facts"]["count"] == 0


def test_native_dex_metadata_is_listed_and_checked_by_nothing_never_a_value() -> None:
    declared = {
        "orders": ModelDeclaration(
            model="orders",
            relation="analytics.orders",
            source="ossie",
            grain=["order_id"],
            columns={
                "margin": DeclaredColumnMeaning(unit="percent", additivity="additive"),
                "customer_id": DeclaredColumnMeaning(references="customers.id"),
            },
            population=DeclaredPopulation(
                rule="Only paid orders, per finance",
                filters=[DeclaredPopulationFilter(column="status", includes=["paid"])],
            ),
        ),
        "refunds": ModelDeclaration(
            model="refunds",
            source="ossie",
            population=DeclaredPopulation(rule="Refunds the support desk approved"),
        ),
    }
    layer = _layer(models=[_sm("orders", [["order_id"]], ["primary"])])
    out = declarations(
        _manifest([]), selected=set(), semantic=layer, native_models=declared
    )
    # The dex grain is the primary key the layer declares: one fact, judged.
    grain = _fact(out, "grain", "order_id", "orders")
    assert grain["checked_by"] == "dex_verify"
    for kind, column in [
        ("unit", "margin"),
        ("additivity", "margin"),
        ("references", "customer_id"),
        ("population_filter", "status"),
    ]:
        fact = _fact(out, kind, column, "orders")
        assert (fact["checked_by"], fact["source"]) == ("none", "ossie"), fact
    assert _fact(out, "population_rule", model="refunds")["checked_by"] == "prose"
    text = json.dumps(out)
    assert "paid" not in text and "finance" not in text and "support desk" not in text


def test_a_fact_dbt_and_the_native_layer_both_declare_is_counted_once() -> None:
    model = _model("orders", dex={"grain": ["order_id"]})
    model["relation_name"] = '"warehouse"."analytics"."orders"'
    manifest = _manifest([model], [_test("unique", "orders", "order_id")])
    layer = _layer(models=[_sm("orders", [["order_id"]], ["primary"])])
    out = _run(manifest, semantic=layer)
    listed = [f for f in out["facts"] if f["fact"] == "grain"]
    assert sorted(f["source"] for f in listed) == ["dbt", "ossie"]
    by_source = {f["source"]: f for f in listed}
    assert by_source["dbt"]["also_declared_by"] == ["ossie"]
    assert by_source["ossie"]["also_declared_by"] == ["dbt"]
    facts = out["summary"]["facts"]
    assert facts["count"] == 1
    assert facts["by_kind"]["grain"]["both"] == 1
    assert facts["checked"] == {
        "numerator": 1,
        "denominator": 1,
        "of": facts["checked"]["of"],
    }


def test_the_same_model_name_on_another_relation_is_not_merged() -> None:
    model = _model("orders", dex={"grain": ["order_id"]})
    model["relation_name"] = '"warehouse"."staging"."orders"'
    layer = _layer(models=[_sm("orders", [["order_id"]], ["primary"])])
    out = _run(_manifest([model]), semantic=layer)
    assert out["summary"]["facts"]["count"] == 2
    assert all("also_declared_by" not in f for f in out["facts"])


def test_a_selector_narrows_the_native_records_by_model_name() -> None:
    layer = _layer(
        models=[
            _sm("orders", [["order_id"]], ["primary"]),
            _sm("customers", [["id"]], ["primary"]),
        ]
    )
    out = declarations(
        _manifest([]), selected=set(), semantic=layer, selector=["Customers"]
    )
    assert [f["model"] for f in out["facts"]] == ["customers"]


def test_dbt_records_name_their_source() -> None:
    out = _run(_manifest([_model("a", dex={"grain": ["x"]})]))
    assert _fact(out, "grain", "x", "a")["source"] == "dbt"
    assert out["summary"]["facts"]["by_source"] == {"dbt": 1}


def test_one_table_name_in_two_databases_is_two_relations() -> None:
    prod = _model("orders", dex={"grain": ["order_id"]})
    prod["relation_name"] = '"prod"."analytics"."orders"'
    layer = _layer(
        models=[
            _sm(
                "orders",
                [["order_id"]],
                ["primary"],
                relation="archive.analytics.orders",
            ),
            _sm(
                "orders_copy",
                [["order_id"]],
                ["primary"],
                relation="prod.analytics.orders_copy",
            ),
        ]
    )
    out = _run(_manifest([prod]), semantic=layer)
    assert out["summary"]["facts"]["count"] == 3
    assert all("also_declared_by" not in f for f in out["facts"])


def test_a_partial_relation_two_databases_end_with_is_not_matched() -> None:
    prod = _model("orders", dex={"grain": ["order_id"]})
    prod["relation_name"] = '"prod"."analytics"."orders"'
    archive = _model("orders_archive", dex={"grain": ["order_id"]})
    archive["relation_name"] = '"archive"."analytics"."orders"'
    layer = _layer(models=[_sm("orders", [["order_id"]], ["primary"])])
    out = _run(_manifest([prod, archive]), semantic=layer)
    assert out["summary"]["facts"]["count"] == 3
    assert all("also_declared_by" not in f for f in out["facts"])


def test_two_joins_on_one_column_stay_two_facts() -> None:
    layer = _layer(
        models=[
            _sm("projects", [["id"]], ["primary"]),
            _sm("users", [["id"]], ["primary"]),
            _sm("teams", [["id"]], ["primary"]),
        ],
        relationships=[
            _rel(
                "owner_user", "projects", "users", [("owner_id", "id")], "many_to_one"
            ),
            _rel(
                "owner_team", "projects", "teams", [("owner_id", "id")], "many_to_many"
            ),
        ],
    )
    declared = {
        "projects": ModelDeclaration(
            model="projects",
            relation="analytics.projects",
            source="ossie",
            columns={"owner_id": DeclaredColumnMeaning(references="users.id")},
        )
    }
    out = declarations(
        _manifest([]), selected=set(), semantic=layer, native_models=declared
    )
    joins = [f for f in out["facts"] if f["fact"] == "references"]
    # The dex reference is the many-to-one join to users, so it merges there.
    assert sorted(f["checked_by"] for f in joins) == ["dex_verify", "none"]
    assert out["summary"]["facts"]["by_kind"]["references"] == {
        "count": 2,
        "dbt_test": 0,
        "dex_verify": 1,
        "both": 0,
        "none": 1,
        "prose": 0,
    }


def test_a_dex_reference_to_another_table_does_not_take_a_join_checker() -> None:
    layer = _layer(
        models=[
            _sm("projects", [["id"]], ["primary"]),
            _sm("users", [["id"]], ["primary"]),
        ],
        relationships=[
            _rel("owner_user", "projects", "users", [("owner_id", "id")], "many_to_one")
        ],
    )
    declared = {
        "projects": ModelDeclaration(
            model="projects",
            relation="analytics.projects",
            source="ossie",
            columns={"owner_id": DeclaredColumnMeaning(references="accounts.id")},
        )
    }
    out = declarations(
        _manifest([]), selected=set(), semantic=layer, native_models=declared
    )
    joins = sorted(f["checked_by"] for f in out["facts"] if f["fact"] == "references")
    assert joins == ["dex_verify", "none"]


def test_a_partial_relation_is_not_matched_when_an_undeclared_model_shares_it() -> None:
    prod = _model("orders", dex={"grain": ["order_id"]})
    prod["relation_name"] = '"prod"."analytics"."orders"'
    archive = _model("orders_archive")  # declares nothing, but is a relation
    archive["relation_name"] = '"archive"."analytics"."orders"'
    layer = _layer(models=[_sm("orders", [["order_id"]], ["primary"])])
    out = _run(_manifest([prod, archive]), semantic=layer)
    assert out["summary"]["facts"]["count"] == 2
    assert all("also_declared_by" not in f for f in out["facts"])


def test_joins_to_one_table_name_in_two_databases_stay_two_facts() -> None:
    layer = _layer(
        models=[
            _sm("orders", [["id"]], ["primary"], relation="prod.analytics.orders"),
            _sm("users", [["id"]], ["primary"], relation="prod.analytics.users"),
            _sm("users_old", [["id"]], ["primary"], relation="archive.analytics.users"),
        ],
        relationships=[
            _rel("a", "orders", "users", [("user_id", "id")], "many_to_one"),
            _rel("b", "orders", "users_old", [("user_id", "id")], "many_to_one"),
        ],
    )
    out = declarations(_manifest([]), selected=set(), semantic=layer)
    assert out["summary"]["facts"]["by_kind"]["references"]["count"] == 2


def test_facts_that_assert_different_things_are_not_merged() -> None:
    model = _model(
        "orders",
        dex={"population": {"parent": {"model": "customers", "key": {"cid": "id"}}}},
        columns={"margin": {"unit": "percent"}, "rate": {"unit": "fraction"}},
    )
    model["relation_name"] = '"prod"."analytics"."orders"'
    declared = {
        "orders": ModelDeclaration(
            model="orders",
            relation="analytics.orders",
            source="ossie",
            columns={
                "margin": DeclaredColumnMeaning(unit="fraction"),
                "rate": DeclaredColumnMeaning(unit="fraction"),
            },
            population=DeclaredPopulation(
                parent={"model": "accounts", "key": {"cid": "account_id"}}
            ),
        )
    }
    out = _run(_manifest([model]), native_models=declared)
    units = out["summary"]["facts"]["by_kind"]["unit"]
    # margin: percent vs fraction, two facts; rate: fraction on both, one.
    assert units["count"] == 3
    assert out["summary"]["facts"]["by_kind"]["population_parent"]["count"] == 2
    shared = [f for f in out["facts"] if "also_declared_by" in f]
    assert [(f["fact"], f["column"]) for f in shared] == [
        ("unit", "rate"),
        ("unit", "rate"),
    ]


def test_a_quoted_identifier_keeps_its_case() -> None:
    model = _model("orders", dex={"grain": ["order_id"]})
    model["relation_name"] = '"DB"."PUBLIC"."orders"'
    layer = _layer(
        models=[_sm("orders", [["order_id"]], ["primary"], relation="DB.PUBLIC.ORDERS")]
    )
    out = _run(_manifest([model]), semantic=layer)
    assert out["summary"]["facts"]["count"] == 2


def test_a_filter_value_keeps_its_type() -> None:
    model = _model(
        "orders",
        dex={"population": {"filters": [{"column": "paid", "includes": [True]}]}},
    )
    model["relation_name"] = '"prod"."analytics"."orders"'
    declared = {
        "orders": ModelDeclaration(
            model="orders",
            relation="analytics.orders",
            source="ossie",
            population=DeclaredPopulation(
                filters=[DeclaredPopulationFilter(column="paid", includes=["True"])]
            ),
        )
    }
    out = _run(_manifest([model]), native_models=declared)
    assert out["summary"]["facts"]["by_kind"]["population_filter"]["count"] == 2
    assert "True" not in json.dumps(out) and "true" not in json.dumps(out)


def test_one_parent_under_two_names_is_one_population_fact() -> None:
    customers = _model("customers")
    customers["relation_name"] = '"prod"."analytics"."customers"'
    orders = _model(
        "orders",
        dex={"population": {"parent": {"model": "customers", "key": {"cid": "id"}}}},
    )
    orders["relation_name"] = '"prod"."analytics"."orders"'
    declared = {
        "orders": ModelDeclaration(
            model="orders",
            relation="analytics.orders",
            source="ossie",
            population=DeclaredPopulation(
                parent={"model": "customers_layer", "key": {"cid": "id"}}
            ),
        ),
        "customers_layer": ModelDeclaration(
            model="customers_layer", relation="analytics.customers", source="ossie"
        ),
    }
    out = _run(_manifest([customers, orders]), native_models=declared)
    assert out["summary"]["facts"]["by_kind"]["population_parent"]["count"] == 1
    shared = [f for f in out["facts"] if f["fact"] == "population_parent"]
    assert sorted(f["also_declared_by"][0] for f in shared) == ["dbt", "ossie"]


def test_one_join_declared_with_two_cardinalities_counts_twice() -> None:
    layer = _layer(
        models=[
            _sm("orders", [["id"]], ["primary"]),
            _sm("users", [["id"]], ["primary"]),
        ],
        relationships=[
            _rel("a", "orders", "users", [("user_id", "id")], "many_to_one"),
            _rel("b", "orders", "users", [("user_id", "id")], "many_to_many"),
        ],
    )
    out = declarations(_manifest([]), selected=set(), semantic=layer)
    joins = out["summary"]["facts"]["by_kind"]["references"]
    assert (joins["count"], joins["dex_verify"], joins["none"]) == (2, 1, 1)


def test_a_quoted_dot_stays_inside_its_identifier() -> None:
    first = _model("a", dex={"grain": ["id"]})
    first["relation_name"] = '"prod"."analytics"."orders.archive"'
    layer = _layer(
        models=[
            _sm(
                "b",
                [["id"]],
                ["primary"],
                relation='"prod"."analytics.orders"."archive"',
            )
        ]
    )
    out = _run(_manifest([first]), semantic=layer)
    assert out["summary"]["facts"]["count"] == 2
    assert all("also_declared_by" not in f for f in out["facts"])
    from exmergo_dex_core.maintain.coverage_declarations import _parts

    assert _parts('"prod"."analytics"."orders.archive"') == (
        "prod",
        "analytics",
        "orders.archive",
    )
    assert _parts('"prod"."analytics.orders"."archive"') == (
        "prod",
        "analytics.orders",
        "archive",
    )
    assert _parts("PROD.Analytics.Orders") == ("prod", "analytics", "orders")


def test_a_filter_entry_with_no_condition_states_no_fact() -> None:
    model = _model(
        "orders",
        dex={
            "population": {
                "filters": [
                    {"column": "status", "includes": ["active"]},
                    {"column": "status"},
                ]
            }
        },
    )
    out = _run(_manifest([model]))
    assert out["summary"]["facts"]["by_kind"]["population_filter"]["count"] == 1


# --- Generated dbt tests credited for the declared facts they check (#1550) ---


def _grain_claim(model: str, columns: list[str]) -> str:
    return claim_id(model, "grain", columns, {"check": "dex_unique_combination"})


def test_a_composite_grain_is_checked_by_its_generated_combination_test() -> None:
    grain = ["order_id", "line_no"]
    manifest = _manifest(
        [
            _model("claimed", dex={"grain": grain}),
            _model("both", dex={"grain": grain}),
            _model("handwritten", dex={"grain": grain}),
            _model("mismatched", dex={"grain": grain}),
            _model("stale", dex={"grain": grain}),
            _model("untested", dex={"grain": grain}),
        ],
        [
            _test(
                "dex_unique_combination",
                "claimed",
                kwargs={"columns": ["line_no", "order_id"]},
                claim=_grain_claim("claimed", grain),
            ),
            _test(
                "dex_unique_combination",
                "both",
                kwargs={"columns": grain},
                claim=_grain_claim("both", grain),
            ),
            _test(
                "unique_combination_of_columns",
                "both",
                kwargs={"combination_of_columns": grain},
                namespace="dbt_utils",
            ),
            _test(
                "dex_unique_combination",
                "handwritten",
                kwargs={"columns": grain},
            ),
            # Another set of columns: not this grain.
            _test(
                "dex_unique_combination",
                "mismatched",
                kwargs={"columns": ["order_id", "other"]},
            ),
            # The columns match, but the claim names a grain this model no
            # longer declares: a stale claim earns no credit.
            _test(
                "dex_unique_combination",
                "stale",
                kwargs={"columns": grain},
                claim=_grain_claim("stale", ["order_id", "legacy"]),
            ),
        ],
    )
    out = _run(manifest)
    claimed = _fact(out, "grain", model="claimed")
    assert (claimed["checked_by"], claimed["tests"], claimed["matched_by"]) == (
        "both",
        ["dex_unique_combination"],
        "claim",
    )
    # The claim wins over a claimless test of the same grain.
    both = _fact(out, "grain", model="both")
    assert (both["checked_by"], both["tests"], both["matched_by"]) == (
        "both",
        ["dex_unique_combination"],
        "claim",
    )
    hand = _fact(out, "grain", model="handwritten")
    assert (hand["checked_by"], hand["tests"], hand["matched_by"]) == (
        "both",
        ["dex_unique_combination"],
        "kind_and_column",
    )
    for model in ("mismatched", "stale", "untested"):
        fact = _fact(out, "grain", model=model)
        assert fact["checked_by"] == "dex_verify", model
        assert fact["tests"] == []
        assert "matched_by" not in fact


def _unit_claim(model: str, column: str, unit: str) -> str:
    return claim_id(model, "unit", [column], {"check": "dex_unit_range", "unit": unit})


def test_a_fraction_is_checked_by_its_unit_range_test_and_a_percent_is_not() -> None:
    manifest = _manifest(
        [
            _model(
                "m",
                columns={
                    "rate": {"unit": "fraction"},
                    "pct": {"unit": "percent"},
                    "claimed_rate": {"unit": "fraction"},
                    "stale_rate": {"unit": "fraction"},
                    "other": {"unit": "fraction"},
                    "amount": {"unit": "currency:USD"},
                },
            )
        ],
        [
            _test("dex_unit_range", "m", "rate"),
            # The test checks 0 to 1; a percent is 0 to 100.
            _test("dex_unit_range", "m", "pct"),
            _test(
                "dex_unit_range",
                "m",
                "claimed_rate",
                claim=_unit_claim("m", "claimed_rate", "fraction"),
            ),
            # The claim names a percent, which this fraction column is not.
            _test(
                "dex_unit_range",
                "m",
                "stale_rate",
                claim=_unit_claim("m", "stale_rate", "percent"),
            ),
            # The claim names another column.
            _test(
                "dex_unit_range",
                "m",
                "other",
                claim=_unit_claim("m", "elsewhere", "fraction"),
            ),
            # A currency has no range fact at all.
            _test("dex_unit_range", "m", "amount"),
        ],
    )
    out = _run(manifest)
    rate = _fact(out, "unit", "rate")
    assert (rate["checked_by"], rate["tests"], rate["matched_by"]) == (
        "both",
        ["dex_unit_range"],
        "kind_and_column",
    )
    claimed = _fact(out, "unit", "claimed_rate")
    assert (claimed["checked_by"], claimed["matched_by"]) == ("both", "claim")
    for column in ("pct", "stale_rate", "other"):
        fact = _fact(out, "unit", column)
        assert fact["checked_by"] == "dex_verify", column
        assert fact["tests"] == []
        assert "matched_by" not in fact
    assert _fact(out, "unit", "amount")["checked_by"] == "none"


def _filter_claim(model: str, column: str, mode: str, values: list) -> str:
    return claim_id(
        model, "population", [column], {"check": "dex_population_filter", mode: values}
    )


def test_a_population_filter_is_checked_only_by_a_test_with_its_values() -> None:
    filters = [
        {"column": "status", "excludes": ["SENTINEL_VALUE_1"]},
        {"column": "region", "includes": ["SENTINEL_VALUE_2"]},
    ]
    manifest = _manifest(
        [
            _model("full", dex={"population": {"filters": filters}}),
            _model("half", dex={"population": {"filters": filters}}),
            _model(
                "wrong_values",
                dex={
                    "population": {
                        "filters": [
                            {"column": "status", "excludes": ["SENTINEL_VALUE_1"]}
                        ]
                    }
                },
            ),
            _model(
                "stale",
                dex={
                    "population": {
                        "filters": [
                            {"column": "status", "excludes": ["SENTINEL_VALUE_1"]}
                        ]
                    }
                },
            ),
            _model(
                "mixed_mode",
                dex={
                    "population": {
                        "filters": [
                            {"column": "status", "includes": ["SENTINEL_VALUE_3"]}
                        ]
                    }
                },
            ),
            _model(
                "other_column",
                dex={
                    "population": {
                        "filters": [
                            {"column": "status", "excludes": ["SENTINEL_VALUE_1"]}
                        ]
                    }
                },
            ),
        ],
        [
            _test(
                "dex_population_filter",
                "full",
                "status",
                kwargs={"excludes": ["SENTINEL_VALUE_1"]},
            ),
            _test(
                "dex_population_filter",
                "full",
                "region",
                kwargs={"includes": ["SENTINEL_VALUE_2"]},
                claim=_filter_claim("full", "region", "includes", ["SENTINEL_VALUE_2"]),
            ),
            _test(
                "dex_population_filter",
                "half",
                "status",
                kwargs={"excludes": ["SENTINEL_VALUE_1"]},
            ),
            # A different value: a different filter.
            _test(
                "dex_population_filter",
                "wrong_values",
                "status",
                kwargs={"excludes": ["SENTINEL_VALUE_9"]},
            ),
            # The values match, but the claim names another list.
            _test(
                "dex_population_filter",
                "stale",
                "status",
                kwargs={"excludes": ["SENTINEL_VALUE_1"]},
                claim=_filter_claim(
                    "stale", "status", "excludes", ["SENTINEL_VALUE_9"]
                ),
            ),
            # Carrying an `excludes` list, the test checks that list alone,
            # never the `includes` list beside it.
            _test(
                "dex_population_filter",
                "mixed_mode",
                "status",
                kwargs={
                    "includes": ["SENTINEL_VALUE_3"],
                    "excludes": ["SENTINEL_VALUE_1"],
                },
            ),
            _test(
                "dex_population_filter",
                "other_column",
                "region",
                kwargs={"excludes": ["SENTINEL_VALUE_1"]},
            ),
        ],
    )
    out = _run(manifest)
    status = _fact(out, "population_filter", "status", model="full")
    assert (status["checked_by"], status["tests"], status["matched_by"]) == (
        "both",
        ["dex_population_filter"],
        "kind_and_column",
    )
    region = _fact(out, "population_filter", "region", model="full")
    assert (region["checked_by"], region["matched_by"]) == ("both", "claim")
    # One list without its test leaves the other list's fact unchecked.
    assert _fact(out, "population_filter", "status", model="half")["checked_by"] == (
        "both"
    )
    assert _fact(out, "population_filter", "region", model="half")["checked_by"] == (
        "dex_verify"
    )
    for model in ("wrong_values", "stale", "mixed_mode", "other_column"):
        fact = _fact(out, "population_filter", "status", model=model)
        assert fact["checked_by"] == "dex_verify", model
        assert fact["tests"] == []
        assert "matched_by" not in fact
    for mode in (False, True):
        dumped = json.dumps(_run(manifest, values_free=mode))
        assert "SENTINEL_VALUE" not in dumped


def test_a_filter_entry_with_both_lists_needs_both_of_its_tests() -> None:
    entry = {
        "column": "status",
        "includes": ["SENTINEL_VALUE_3"],
        "excludes": ["SENTINEL_VALUE_1"],
    }
    manifest = _manifest(
        [
            _model("dual", dex={"population": {"filters": [entry]}}),
            _model("dual_half", dex={"population": {"filters": [entry]}}),
        ],
        [
            _test(
                "dex_population_filter",
                "dual",
                "status",
                kwargs={"includes": ["SENTINEL_VALUE_3"]},
                claim=_filter_claim("dual", "status", "includes", ["SENTINEL_VALUE_3"]),
            ),
            _test(
                "dex_population_filter",
                "dual",
                "status",
                kwargs={"excludes": ["SENTINEL_VALUE_1"]},
            ),
            _test(
                "dex_population_filter",
                "dual_half",
                "status",
                kwargs={"includes": ["SENTINEL_VALUE_3"]},
            ),
        ],
    )
    out = _run(manifest)
    # One list matched by claim, the other by kind and column: the fact
    # reads `kind_and_column`, since not every list matched by claim.
    dual = _fact(out, "population_filter", "status", model="dual")
    assert (dual["checked_by"], dual["tests"], dual["matched_by"]) == (
        "both",
        ["dex_population_filter"],
        "kind_and_column",
    )
    half = _fact(out, "population_filter", "status", model="dual_half")
    assert half["checked_by"] == "dex_verify"
    assert half["tests"] == []


def _parent_claim(
    model: str, key: dict, direction: str, parent: str, filters=None
) -> str:
    # The key maps a child column to its parent column: the claim's
    # identity is the child side, its parent_key the parent side, the same
    # split production makes (#1550).
    params: dict[str, Any] = {
        "check": "dex_parent_population",
        "direction": direction,
        "parent": parent,
        "parent_key": [column.lower() for column in key.values()],
    }
    if direction == "missing" and filters:
        params["filters"] = filters
    return claim_id(model, "population", list(key), params)


def _parent_tests(
    on: str,
    *,
    parent: str = "subs",
    key: dict | None = None,
    filters=None,
    claim_prefix: str | None = None,
) -> list[dict]:
    key = key if key is not None else {"subscription_id": "subscription_id"}
    tests = []
    for direction in ("missing", "unexpected"):
        kwargs: dict[str, Any] = {
            "parent": f"ref('{parent}')",
            "key": dict(key),
            "direction": direction,
        }
        if direction == "missing" and filters:
            kwargs["filters"] = filters
        claim = None
        if claim_prefix is not None:
            claim = _parent_claim(
                on,
                dict(key),
                direction,
                parent,
                filters if direction == "missing" else None,
            )
        tests.append(
            _test(
                "dex_parent_population",
                on,
                kwargs=kwargs,
                reads=[on, parent],
                claim=claim,
            )
        )
    return tests


def test_a_parent_population_is_checked_only_by_both_of_its_direction_tests() -> None:
    filters = [{"column": "status", "excludes": ["SENTINEL_VALUE_1"]}]
    population = {
        "filters": filters,
        "parent": {"model": "subs", "key": {"subscription_id": "subscription_id"}},
    }
    manifest = _manifest(
        [
            _model("subs"),
            _model("other"),
            _model("claimed", dex={"population": population}),
            _model("full", dex={"population": population}),
            _model("half", dex={"population": population}),
            _model(
                "wrong_parent",
                dex={"population": population},
            ),
            _model("wrong_key", dex={"population": population}),
            _model("wrong_filters", dex={"population": population}),
            _model("stale", dex={"population": population}),
            # The child and parent columns differ: the claim's identity is
            # the child side, its parent_key the parent side (#1550).
            _model(
                "asymmetric",
                dex={
                    "population": {
                        "filters": filters,
                        "parent": {
                            "model": "subs",
                            "key": {"subscription_id": "id"},
                        },
                    }
                },
            ),
        ],
        [
            *_parent_tests("claimed", filters=filters, claim_prefix="claimed"),
            *_parent_tests("full", filters=filters),
            # Only the `missing` direction: the `unexpected` half is unchecked.
            _parent_tests("half", filters=filters)[0],
            *_parent_tests("wrong_parent", parent="other", filters=filters),
            *_parent_tests(
                "wrong_key",
                key={"subscription_id": "other_id"},
                filters=filters,
            ),
            *_parent_tests(
                "wrong_filters",
                filters=[{"column": "status", "excludes": ["SENTINEL_VALUE_9"]}],
            ),
            *_parent_tests("stale", filters=filters),
            *_parent_tests(
                "asymmetric",
                key={"subscription_id": "id"},
                filters=filters,
                claim_prefix="asymmetric",
            ),
        ],
    )
    # The stale model's `missing` test carries a claim for another parent.
    stale_missing = next(
        t
        for t in manifest["nodes"].values()
        if isinstance(t, dict)
        and t.get("resource_type") == "test"
        and t.get("attached_node") == _uid("stale")
        and (t.get("test_metadata") or {}).get("kwargs", {}).get("direction")
        == "missing"
    )
    stale_missing["config"]["meta"] = {
        "dex": {
            "claim": _parent_claim(
                "stale",
                {"subscription_id": "subscription_id"},
                "missing",
                "other",
                filters,
            )
        }
    }
    out = _run(manifest)
    claimed = _fact(out, "population_parent", model="claimed")
    assert (claimed["checked_by"], claimed["tests"], claimed["matched_by"]) == (
        "both",
        ["dex_parent_population"],
        "claim",
    )
    full = _fact(out, "population_parent", model="full")
    assert (full["checked_by"], full["tests"], full["matched_by"]) == (
        "both",
        ["dex_parent_population"],
        "kind_and_column",
    )
    asymmetric = _fact(out, "population_parent", model="asymmetric")
    assert (
        asymmetric["checked_by"],
        asymmetric["tests"],
        asymmetric["matched_by"],
    ) == (
        "both",
        ["dex_parent_population"],
        "claim",
    )
    for model in ("half", "wrong_parent", "wrong_key", "wrong_filters", "stale"):
        fact = _fact(out, "population_parent", model=model)
        assert fact["checked_by"] == "dex_verify", model
        assert fact["tests"] == []
        assert "matched_by" not in fact
    for mode in (False, True):
        dumped = json.dumps(_run(manifest, values_free=mode))
        assert "SENTINEL_VALUE" not in dumped


def test_a_join_coverage_test_checks_only_the_reference_it_joins() -> None:
    manifest = _manifest(
        [
            _model("customers"),
            _model("vendors"),
            _model(
                "orders",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers"],
            ),
            _model(
                "wrong_parent",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers"],
            ),
            _model(
                "wrong_field",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers"],
            ),
            _model(
                "wrong_column",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers"],
            ),
            _model(
                "two_refs",
                columns={
                    "customer_id": {"references": "customers.customer_id"},
                    "store_id": {"references": "customers.store_id"},
                },
                reads=["customers"],
            ),
        ],
        [
            _test(
                "dex_join_coverage",
                "orders",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["customer_id"],
                    "parent": "ref('customers')",
                    "parent_columns": ["customer_id"],
                },
                reads=["orders", "customers"],
            ),
            _test(
                "dex_join_coverage",
                "wrong_parent",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["customer_id"],
                    "parent": "ref('vendors')",
                    "parent_columns": ["customer_id"],
                },
                reads=["wrong_parent", "vendors"],
            ),
            _test(
                "dex_join_coverage",
                "wrong_field",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["customer_id"],
                    "parent": "ref('customers')",
                    "parent_columns": ["legacy_id"],
                },
                reads=["wrong_field", "customers"],
            ),
            _test(
                "dex_join_coverage",
                "wrong_column",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["store_id"],
                    "parent": "ref('customers')",
                    "parent_columns": ["customer_id"],
                },
                reads=["wrong_column", "customers"],
            ),
            # One test over a two-column join checks both references it joins.
            _test(
                "dex_join_coverage",
                "two_refs",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["customer_id", "store_id"],
                    "parent": "ref('customers')",
                    "parent_columns": ["customer_id", "store_id"],
                },
                reads=["two_refs", "customers"],
            ),
        ],
    )
    out = _run(manifest)
    joined = _fact(out, "references", "customer_id", model="orders")
    assert joined["checked_by"] == "both"
    assert joined["tests"] == ["dex_join_coverage"]
    for model in ("wrong_parent", "wrong_field", "wrong_column"):
        fact = _fact(out, "references", "customer_id", model=model)
        assert fact["checked_by"] == "dex_verify", model
        assert fact["tests"] == []
    for column in ("customer_id", "store_id"):
        fact = _fact(out, "references", column, model="two_refs")
        assert fact["tests"] == ["dex_join_coverage"], column


def test_a_referenced_key_test_checks_the_reference_from_the_parent_side() -> None:
    # Each child reads a parent of its own: a test on one parent says
    # nothing about a reference to another.
    manifest = _manifest(
        [
            _model("customers"),
            _model("vendors"),
            _model("suppliers"),
            _model("partners"),
            _model(
                "orders",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers"],
            ),
            _model(
                "claimed",
                columns={"vendor_id": {"references": "customers.vendor_id"}},
                reads=["customers"],
            ),
            _model(
                "wrong_column",
                columns={"vendor_id": {"references": "vendors.vendor_id"}},
                reads=["vendors"],
            ),
            _model(
                "wrong_parent",
                columns={"supplier_id": {"references": "suppliers.supplier_id"}},
                reads=["suppliers"],
            ),
            _model(
                "stale",
                columns={"partner_id": {"references": "partners.partner_id"}},
                reads=["partners"],
            ),
        ],
        [
            _test("dex_referenced_key", "customers", "customer_id"),
            # On another field of the same parent, so each reference is
            # checked by one test only: the claimless one for `orders`, this
            # claimed one for `claimed` (#1550).
            _test(
                "dex_referenced_key",
                "customers",
                "vendor_id",
                claim=claim_id(
                    "customers",
                    "referenced_key",
                    ["vendor_id"],
                    {"check": "dex_referenced_key"},
                ),
            ),
            # On the parent's other column: not the referenced field.
            _test("dex_referenced_key", "vendors", "legacy_id"),
            # The columns match, but the claim names another field.
            _test(
                "dex_referenced_key",
                "partners",
                "partner_id",
                claim=claim_id(
                    "partners",
                    "referenced_key",
                    ["other_id"],
                    {"check": "dex_referenced_key"},
                ),
            ),
        ],
    )
    out = _run(manifest)
    # Each reference is checked by one test only: the claimless one on
    # customers.customer_id for `orders`, the claimed one on
    # customers.vendor_id for `claimed`.
    assert _fact(out, "references", "customer_id", model="orders")["tests"] == [
        "dex_referenced_key"
    ]
    assert _fact(out, "references", "vendor_id", model="claimed")["tests"] == [
        "dex_referenced_key"
    ]
    for model, column in (
        ("wrong_column", "vendor_id"),
        ("wrong_parent", "supplier_id"),
        ("stale", "partner_id"),
    ):
        fact = _fact(out, "references", column, model=model)
        assert fact["checked_by"] == "dex_verify", model
        assert fact["tests"] == []


def test_a_composite_parent_key_matches_whichever_way_its_pairs_are_listed() -> None:
    # The key's pairs are a set; the claim keeps the declaration's order.
    population = {"parent": {"model": "subs", "key": {"a": "a_id", "b": "b_id"}}}
    tests = [
        _test(
            "dex_parent_population",
            "child",
            kwargs={
                "parent": "ref('subs')",
                "key": {"b": "b_id", "a": "a_id"},
                "direction": direction,
            },
            reads=["child", "subs"],
            claim=claim_id(
                "child",
                "population",
                ["a", "b"],
                {
                    "check": "dex_parent_population",
                    "direction": direction,
                    "parent": "subs",
                    "parent_key": ["a_id", "b_id"],
                },
            ),
        )
        for direction in ("missing", "unexpected")
    ]
    manifest = _manifest(
        [_model("subs"), _model("child", dex={"population": population})], tests
    )
    out = _run(manifest)
    fact = _fact(out, "population_parent", model="child")
    assert (fact["checked_by"], fact["tests"], fact["matched_by"]) == (
        "both",
        ["dex_parent_population"],
        "claim",
    )
    assert fact["column"] == "a, b"


def test_a_versioned_models_test_matches_by_its_bare_name_claim() -> None:
    # A declaration on every version renders its tests under the bare name.
    model = _model("orders", dex={"grain": ["order_id", "line_no"]})
    model.update({"version": 2, "latest_version": 2})
    manifest = _manifest(
        [model],
        [
            _test(
                "dex_unique_combination",
                "orders",
                kwargs={"columns": ["order_id", "line_no"]},
                claim=_grain_claim("orders", ["order_id", "line_no"]),
            ),
        ],
    )
    out = _run(manifest)
    fact = _fact(out, "grain", model="orders.v2")
    assert (fact["checked_by"], fact["tests"], fact["matched_by"]) == (
        "both",
        ["dex_unique_combination"],
        "claim",
    )


def test_a_population_filter_match_is_typed_unordered_and_excludes_only() -> None:
    # A filter's values are typed, so `True` and `1` are two predicates, and
    # order is not part of identity; a test carrying an `excludes` list
    # checks that list alone, whatever `includes` it carries (#1550).
    manifest = _manifest(
        [
            _model(
                "filtered",
                dex={
                    "population": {
                        "filters": [
                            {"column": "flag", "includes": [True]},
                            {"column": "status", "includes": ["a", "b"]},
                            {"column": "region", "excludes": ["x"]},
                        ]
                    }
                },
            ),
            _model(
                "bool_only",
                dex={"population": {"filters": [{"column": "flag", "includes": [1]}]}},
            ),
        ],
        [
            # `1` is not `True`: no credit for the `flag` filter.
            _test(
                "dex_population_filter",
                "filtered",
                "flag",
                kwargs={"includes": [1]},
            ),
            # The same values in another order still check the filter.
            _test(
                "dex_population_filter",
                "filtered",
                "status",
                kwargs={"includes": ["b", "a"]},
            ),
            # The `excludes` list is checked alone, even with an `includes`
            # list beside it.
            _test(
                "dex_population_filter",
                "filtered",
                "region",
                kwargs={"includes": ["y"], "excludes": ["x"]},
            ),
            # The matching value, for the model that declares it.
            _test(
                "dex_population_filter",
                "bool_only",
                "flag",
                kwargs={"includes": [1]},
            ),
        ],
    )
    out = _run(manifest)
    flag = _fact(out, "population_filter", "flag", model="filtered")
    assert flag["checked_by"] == "dex_verify"
    assert flag["tests"] == []
    for model, column in (("filtered", "status"), ("filtered", "region")):
        fact = _fact(out, "population_filter", column, model=model)
        assert (fact["checked_by"], fact["tests"], fact["matched_by"]) == (
            "both",
            ["dex_population_filter"],
            "kind_and_column",
        )
    one = _fact(out, "population_filter", "flag", model="bool_only")
    assert (one["checked_by"], one["tests"], one["matched_by"]) == (
        "both",
        ["dex_population_filter"],
        "kind_and_column",
    )


def test_a_join_coverage_test_checks_the_parent_its_parent_argument_names() -> None:
    # A join test is credited for the parent its `parent` argument names,
    # not merely for a parent its reads name: a test whose driving input is
    # the declared parent and whose parent is another model with the same
    # key name checks another join (#1550).
    manifest = _manifest(
        [
            _model("customers"),
            _model("vendors"),
            _model(
                "flipped",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers", "vendors"],
            ),
            _model(
                "packaged",
                columns={"customer_id": {"references": "customers.customer_id"}},
                reads=["customers"],
            ),
        ],
        [
            # The parent is another model, though the test reads the
            # declared parent too: no credit.
            _test(
                "dex_join_coverage",
                "flipped",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["customer_id"],
                    "parent": "ref('vendors')",
                    "parent_columns": ["customer_id"],
                },
                reads=["flipped", "customers", "vendors"],
            ),
            # A two-part ref names its model by its last argument.
            _test(
                "dex_join_coverage",
                "packaged",
                kwargs={
                    "child": "ref('stg_orders')",
                    "child_columns": ["customer_id"],
                    "parent": "ref('pkg', 'customers')",
                    "parent_columns": ["customer_id"],
                },
                reads=["packaged", "customers"],
            ),
        ],
    )
    out = _run(manifest)
    flipped = _fact(out, "references", "customer_id", model="flipped")
    assert flipped["checked_by"] == "dex_verify"
    assert flipped["tests"] == []
    packaged = _fact(out, "references", "customer_id", model="packaged")
    assert packaged["tests"] == ["dex_join_coverage"]


def test_a_malformed_population_predicate_never_crashes_the_report() -> None:
    # A filter mode that is not a list is malformed: it earns no test
    # credit, and the report still runs (#1550).
    manifest = _manifest(
        [
            _model(
                "scalar_int",
                dex={"population": {"filters": [{"column": "flag", "includes": 1}]}},
            ),
            _model(
                "scalar_str",
                dex={"population": {"filters": [{"column": "flag", "includes": "a"}]}},
            ),
            _model(
                "falsy_scalar",
                dex={"population": {"filters": [{"column": "flag", "includes": 0}]}},
            ),
            _model(
                "valid",
                dex={
                    "population": {"filters": [{"column": "flag", "includes": ["a"]}]}
                },
            ),
            _model(
                "parent_scalar",
                dex={
                    "population": {
                        "filters": [{"column": "flag", "includes": 1}],
                        "parent": {
                            "model": "subs",
                            "key": {"subscription_id": "subscription_id"},
                        },
                    }
                },
            ),
            _model("subs"),
        ],
        [
            _test(
                "dex_population_filter",
                "scalar_int",
                "flag",
                kwargs={"includes": [1]},
            ),
            _test(
                "dex_population_filter",
                "scalar_str",
                "flag",
                kwargs={"includes": ["a"]},
            ),
            _test(
                "dex_population_filter",
                "falsy_scalar",
                "flag",
                kwargs={"includes": [0]},
            ),
            _test(
                "dex_population_filter",
                "valid",
                "flag",
                kwargs={"includes": ["a"]},
            ),
            *_parent_tests(
                "parent_scalar",
                filters=[{"column": "flag", "includes": [1]}],
            ),
        ],
    )
    out = _run(manifest)
    for model in ("scalar_int", "scalar_str"):
        fact = _fact(out, "population_filter", "flag", model=model)
        assert fact["checked_by"] == "dex_verify"
        assert fact["tests"] == []
    # A falsy scalar states no predicate, so it states no fact.
    assert not [
        f
        for f in out["facts"]
        if f["model"] == "falsy_scalar" and f["fact"] == "population_filter"
    ]
    valid = _fact(out, "population_filter", "flag", model="valid")
    assert (valid["checked_by"], valid["tests"]) == (
        "both",
        ["dex_population_filter"],
    )
    parent = _fact(out, "population_parent", model="parent_scalar")
    assert parent["checked_by"] == "dex_verify"
    assert parent["tests"] == []
