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


def _orders_declared() -> dict:
    return _model(
        "orders",
        dex={"grain": ["order_id"]},
        columns={
            "order_id": {"role": "key"},
            "customer_id": {"references": "customers.customer_id"},
            "placed_at": {"null_rule": "never"},
        },
    )


def test_tests_rendered_with_claims_are_matched_by_claim() -> None:
    # Each test carries its fact's claim, on a column and under a name the
    # kind-and-column match would not take: the claim alone matches it (#1498).
    def grain(check: str) -> str:
        return claim_id("orders", "grain", ["order_id"], {"check": check})

    reference = claim_id(
        "orders", "references", ["customer_id"], {"references": "customers.customer_id"}
    )
    never = claim_id("orders", "null_rule", ["placed_at"], {"null_rule": "never"})
    manifest = _manifest(
        [_orders_declared(), _model("customers")],
        [
            _test("unique", "orders", "ORDER_ID", claim=grain("unique")),
            _test("not_null", "orders", "order_id", claim=grain("not_null")),
            _test(
                "relationships",
                "orders",
                "customer_id",
                kwargs={"to": "ref('customers')", "field": "customer_id"},
                reads=["orders", "customers"],
                claim=reference,
            ),
            _test("not_null", "orders", "placed_at_renamed", claim=never),
        ],
    )
    out = _run(manifest)
    tested = [f for f in out["facts"] if f["tests"]]
    assert {f["fact"] for f in tested} == {"grain", "references", "null_rule", "key"}
    for fact in tested:
        assert fact["matched_by"] == "claim", fact
    # The key on the grain column is checked by the grain's `unique`.
    assert _fact(out, "key", "order_id")["tests"] == ["unique"]
    assert _fact(out, "grain")["tests"] == ["unique"]
    assert _fact(out, "null_rule", "placed_at")["tests"] == ["not_null"]
    assert out["stale_claims"] == []
    assert out["summary"]["stale_claims"] == 0


def test_a_hand_written_unique_on_the_grain_is_matched_by_kind_and_column() -> None:
    manifest = _manifest(
        [_model("orders", dex={"grain": ["order_id"]})],
        [_test("unique", "orders", "order_id")],
    )
    grain = _fact(_run(manifest), "grain")
    assert grain["tests"] == ["unique"]
    assert grain["matched_by"] == "kind_and_column"
    assert grain["checked_by"] == "both"


def test_a_claim_no_declared_fact_states_is_listed_apart() -> None:
    # The grain moved from `legacy_id` to `order_id`; the old render's `unique`
    # stayed, and a `unique` now on `order_id` carries the old claim too. Both
    # are listed apart; the one on the grain column still counts by kind and
    # column, as it does for a test without a claim.
    old = claim_id("orders", "grain", ["legacy_id"], {"check": "unique"})
    manifest = _manifest(
        [_model("orders", dex={"grain": ["order_id"]})],
        [
            _test("unique", "orders", "legacy_id", claim=old),
            _test("unique", "orders", "order_id", claim=old),
        ],
    )
    out = _run(manifest)
    grain = _fact(out, "grain")
    assert grain["tests"] == ["unique"]
    assert grain["checked_by"] == "both"
    assert grain["matched_by"] == "kind_and_column"
    assert out["stale_claims"] == [
        {"model": "orders", "test": "unique", "column": "legacy_id", "claim": old},
        {"model": "orders", "test": "unique", "column": "order_id", "claim": old},
    ]
    assert out["summary"]["stale_claims"] == 2
    assert any("stale_claims" in note for note in out["notes"])


def test_each_test_in_a_resources_tests_list_shows_its_claim() -> None:
    from exmergo_dex_core.maintain.coverage import inventory

    claim = claim_id("orders", "grain", ["order_id"], {"check": "unique"})
    manifest = _manifest(
        [_model("orders", dex={"grain": ["order_id"]})],
        [
            _test("unique", "orders", "order_id", claim=claim),
            _test("not_null", "orders", "order_id"),
        ],
    )
    [orders] = inventory(manifest).sections["models"]
    by_test = {t["test"]: t for t in orders["tests"]}
    assert by_test["unique"]["claim"] == claim
    assert "claim" not in by_test["not_null"]


def test_a_renamed_model_keeps_its_coverage_and_lists_its_claims_apart() -> None:
    # `transform rename` keeps each test's claim as written (#1448): `sales` was
    # `orders`, so its tests carry the old model's claims.
    def old(check: str) -> str:
        return claim_id("orders", "grain", ["order_id"], {"check": check})

    manifest = _manifest(
        [
            _model(
                "sales",
                dex={"grain": ["order_id"]},
                columns={"order_id": {"role": "key"}},
            )
        ],
        [
            _test("unique", "sales", "order_id", claim=old("unique")),
            _test("not_null", "sales", "order_id", claim=old("not_null")),
        ],
    )
    out = _run(manifest)
    grain = _fact(out, "grain")
    assert (grain["checked_by"], grain["tests"]) == ("both", ["unique"])
    assert grain["matched_by"] == "kind_and_column"
    key = _fact(out, "key", "order_id")
    assert (key["checked_by"], key["tests"]) == ("both", ["unique"])
    assert [s["claim"] for s in out["stale_claims"]] == [
        old("unique"),
        old("not_null"),
    ]


def test_a_renamed_column_keeps_its_coverage_and_lists_its_claim_apart() -> None:
    # `legacy_id` was renamed `order_id`; its `not_null` keeps the old claim.
    old = claim_id("orders", "null_rule", ["legacy_id"], {"null_rule": "never"})
    manifest = _manifest(
        [_model("orders", columns={"order_id": {"null_rule": "never"}})],
        [_test("not_null", "orders", "order_id", claim=old)],
    )
    out = _run(manifest)
    fact = _fact(out, "null_rule", "order_id")
    assert (fact["checked_by"], fact["tests"]) == ("dbt_test", ["not_null"])
    assert fact["matched_by"] == "kind_and_column"
    assert out["stale_claims"] == [
        {"model": "orders", "test": "not_null", "column": "order_id", "claim": old}
    ]


def _grain_claim(check: str, columns=("order_id",)) -> str:
    return claim_id("orders", "grain", list(columns), {"check": check})


def test_a_grain_is_not_checked_by_its_claimed_not_null_alone() -> None:
    # The rendered `unique` was deleted; the claimed `not_null` cannot show the
    # grain unique, so the grain reads as main does, and the claim is not stale.
    manifest = _manifest(
        [_model("orders", dex={"grain": ["order_id"]})],
        [_test("not_null", "orders", "order_id", claim=_grain_claim("not_null"))],
    )
    out = _run(manifest)
    grain = _fact(out, "grain")
    assert (grain["checked_by"], grain["tests"]) == ("dex_verify", [])
    assert "matched_by" not in grain
    assert out["stale_claims"] == []


def test_a_grain_unique_with_a_stale_claim_still_checks_it_by_kind_and_column() -> None:
    stale = _grain_claim("unique", ["legacy_id"])
    manifest = _manifest(
        [_model("orders", dex={"grain": ["order_id"]})],
        [
            _test("unique", "orders", "order_id", claim=stale),
            _test("not_null", "orders", "order_id", claim=_grain_claim("not_null")),
        ],
    )
    out = _run(manifest)
    grain = _fact(out, "grain")
    assert (grain["checked_by"], grain["tests"]) == ("both", ["unique"])
    assert grain["matched_by"] == "kind_and_column"
    assert out["stale_claims"] == [
        {"model": "orders", "test": "unique", "column": "order_id", "claim": stale}
    ]
