"""Composing a local and a hosted semantic catalog into one read view (#408).

`_semantic_catalog` reads both sources when both `--use-project` and
`--use-hosted-semantic-layer` are passed, and folds them through
`_compose_semantic_catalogs` rather than one winning by accidental precedence.
These tests exercise that fold directly against hand-built catalogs, since the
fold itself needs no engine or repository.
"""

from __future__ import annotations

from exmergo_dex_core.explore.commands import _compose_semantic_catalogs
from exmergo_dex_core.semantic_catalog import (
    DimensionInfo,
    EntityInfo,
    EntityRole,
    MeasureInfo,
    MetricInfo,
    SemanticCatalogView,
    SemanticModelInfo,
)


def test_semantic_models_metrics_dimensions_and_measures_union_by_name():
    local = SemanticCatalogView(
        semantic_models=[SemanticModelInfo(name="orders", relation="db.orders")],
        metrics=[MetricInfo(name="revenue", type="simple")],
        dimensions=[DimensionInfo(name="order_date", type="time")],
        measures=[MeasureInfo(name="order_total", semantic_model="orders")],
    )
    hosted = SemanticCatalogView(
        semantic_models=[SemanticModelInfo(name="customers", relation=None)],
        metrics=[MetricInfo(name="active_customers", type="simple")],
        dimensions=[DimensionInfo(name="signup_date", type="time")],
        measures=[MeasureInfo(name="customer_count", semantic_model="customers")],
    )

    composed = _compose_semantic_catalogs(local, hosted)

    assert {m.name for m in composed.semantic_models} == {"orders", "customers"}
    assert {m.name for m in composed.metrics} == {"revenue", "active_customers"}
    assert {d.name for d in composed.dimensions} == {"order_date", "signup_date"}
    assert {m.name for m in composed.measures} == {"order_total", "customer_count"}


def test_a_name_both_sides_declare_keeps_the_local_entry():
    local = SemanticCatalogView(
        semantic_models=[SemanticModelInfo(name="orders", relation="db.orders")]
    )
    hosted = SemanticCatalogView(
        semantic_models=[SemanticModelInfo(name="orders", relation=None)]
    )

    composed = _compose_semantic_catalogs(local, hosted)

    (model,) = composed.semantic_models
    assert model.relation == "db.orders"


def test_an_entity_declared_on_both_sides_merges_roles_and_rederives_type():
    local = SemanticCatalogView(
        entities=[
            EntityInfo(
                name="customer",
                type="foreign",
                roles=[EntityRole(semantic_model="orders", type="foreign")],
            )
        ]
    )
    hosted = SemanticCatalogView(
        entities=[
            EntityInfo(
                name="customer",
                type="primary",
                roles=[EntityRole(semantic_model="customers", type="primary")],
            )
        ]
    )

    composed = _compose_semantic_catalogs(local, hosted)

    (entity,) = composed.entities
    assert {r.semantic_model for r in entity.roles} == {"orders", "customers"}
    assert entity.type == "primary"


def test_an_entity_role_declared_identically_on_both_sides_is_not_duplicated():
    role = EntityRole(semantic_model="orders", type="foreign", column="customer_id")
    local = SemanticCatalogView(
        entities=[EntityInfo(name="customer", type="foreign", roles=[role])]
    )
    hosted = SemanticCatalogView(
        entities=[
            EntityInfo(
                name="customer",
                type="foreign",
                roles=[
                    EntityRole(
                        semantic_model="orders", type="foreign", column="customer_id"
                    )
                ],
            )
        ]
    )

    composed = _compose_semantic_catalogs(local, hosted)

    (entity,) = composed.entities
    assert len(entity.roles) == 1


def test_an_entity_declared_on_only_one_side_passes_through_unchanged():
    local = SemanticCatalogView(
        entities=[
            EntityInfo(
                name="customer",
                type="primary",
                roles=[EntityRole(semantic_model="customers", type="primary")],
            )
        ]
    )
    hosted = SemanticCatalogView()

    composed = _compose_semantic_catalogs(local, hosted)

    (entity,) = composed.entities
    assert entity.name == "customer"
    assert entity.type == "primary"


def test_notes_concatenate_and_physical_columns_prefer_the_local_side():
    local = SemanticCatalogView(
        notes=["local note"],
        physical_columns={"orders.order_id": ("db.orders", "order_id")},
    )
    hosted = SemanticCatalogView(
        notes=["hosted note"],
        physical_columns={
            "orders.order_id": ("hosted.orders", "id"),
            "customers.customer_id": ("hosted.customers", "id"),
        },
    )

    composed = _compose_semantic_catalogs(local, hosted)

    assert composed.notes == ["local note", "hosted note"]
    assert composed.physical_columns["orders.order_id"] == ("db.orders", "order_id")
    assert composed.physical_columns["customers.customer_id"] == (
        "hosted.customers",
        "id",
    )
