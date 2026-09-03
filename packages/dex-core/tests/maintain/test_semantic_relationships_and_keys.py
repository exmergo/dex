"""Composite relationships and dataset keys on the semantic drift axis (#409).

`semantic_free_drift` is a pure comparison, so these drive it directly with
hand-built snapshots rather than through a warehouse, the same style
`test_semantic.py::test_a_pathless_definition_omits_its_provenance` uses.
"""

from __future__ import annotations

from exmergo_dex_core.cache import ColumnProfile, Dataset
from exmergo_dex_core.maintain.drift import semantic_free_drift
from exmergo_dex_core.maintain.snapshot import (
    RelationshipDef,
    SemanticLayer,
    SemanticModelDef,
    Snapshot,
)


def _dataset(identifier: str, *columns: str) -> Dataset:
    return Dataset(
        identifier=identifier,
        columns=[ColumnProfile(name=c, data_type="VARCHAR") for c in columns],
    )


def _snap(baseline: SemanticLayer | None) -> Snapshot:
    return Snapshot(created_at="2026-01-01T00:00:00", semantic_layer=baseline)


def test_a_model_with_no_model_ref_is_matched_by_its_own_relation():
    """A format with no build step (Ossie) states `relation` instead of
    `model_ref`; the column check has to fall back to it or it never runs at
    all for that format's own semantic models."""

    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="shop.orders",
                content_sha256="x",
                relation="db.main.orders",
                dimensions={"status": "status"},
            )
        ],
        relationships_and_keys_captured=True,
    )
    datasets = [_dataset("db.main.orders", "order_id")]  # "status" is gone

    findings = semantic_free_drift(None, current, datasets, _snap(current))

    dangling = [f for f in findings if f.code == "dangling_reference"]
    assert len(dangling) == 1
    assert dangling[0].data == {
        "semantic_model": "shop.orders",
        "role": "dimension",
        "name": "status",
    }
    assert dangling[0].impacted_models == []  # no model_ref to name


def test_a_missing_key_column_is_flagged_when_captured():
    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="shop.orders",
                content_sha256="x",
                relation="db.main.orders",
                keys=[["order_id", "line_no"]],
            )
        ],
        relationships_and_keys_captured=True,
    )
    datasets = [_dataset("db.main.orders", "order_id")]  # "line_no" is gone

    findings = semantic_free_drift(None, current, datasets, _snap(current))

    key_findings = [f for f in findings if f.data.get("role") == "key"]
    assert len(key_findings) == 1
    assert key_findings[0].code == "dangling_reference"
    assert key_findings[0].severity == "high"
    assert key_findings[0].identifier == "db.main.orders"
    assert key_findings[0].data["columns"] == ["order_id", "line_no"]
    assert "line_no" in key_findings[0].detail


def test_a_missing_key_column_is_silent_when_not_captured():
    """The #409 hazard, guarded directly: a layer that never captured keys
    (every baseline pinned before this field existed, and dbt today) must not
    report a false "missing column" for a key it never actually recorded."""

    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="shop.orders",
                content_sha256="x",
                relation="db.main.orders",
                keys=[["order_id", "line_no"]],
            )
        ],
        relationships_and_keys_captured=False,
    )
    datasets = [_dataset("db.main.orders", "order_id")]

    findings = semantic_free_drift(None, current, datasets, _snap(current))

    assert [f for f in findings if f.data.get("role") == "key"] == []


def test_a_relationship_naming_a_vanished_model_is_broken():
    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="shop.orders", content_sha256="x", relation="db.main.orders"
            )
        ],
        relationships=[
            RelationshipDef(
                name="orders_to_customers",
                content_sha256="y",
                model="shop.orders",
                to_model="shop.customers",
                column_pairs=[("customer_id", "customer_id")],
            )
        ],
        relationships_and_keys_captured=True,
    )
    datasets = [_dataset("db.main.orders", "customer_id")]

    findings = semantic_free_drift(None, current, datasets, _snap(current))

    broken = [f for f in findings if f.code == "broken_relationship"]
    assert len(broken) == 1
    assert broken[0].data == {
        "relationship": "orders_to_customers",
        "missing_model": "shop.customers",
    }


def test_a_relationship_whose_column_pair_no_longer_resolves_is_broken():
    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="shop.orders", content_sha256="x", relation="db.main.orders"
            ),
            SemanticModelDef(
                name="shop.customers", content_sha256="z", relation="db.main.customers"
            ),
        ],
        relationships=[
            RelationshipDef(
                name="orders_to_customers",
                content_sha256="y",
                model="shop.orders",
                to_model="shop.customers",
                column_pairs=[("customer_id", "customer_id")],
            )
        ],
        relationships_and_keys_captured=True,
    )
    datasets = [
        _dataset("db.main.orders", "order_id"),  # customer_id is gone
        _dataset("db.main.customers", "customer_id"),
    ]

    findings = semantic_free_drift(None, current, datasets, _snap(current))

    broken = [f for f in findings if f.code == "broken_relationship"]
    assert len(broken) == 1
    assert broken[0].identifier == "db.main.orders"
    assert broken[0].data["relationship"] == "orders_to_customers"
    assert broken[0].data["missing_pairs"] == [["customer_id", "customer_id"]]


def test_an_intact_relationship_over_captured_layers_is_silent():
    current = SemanticLayer(
        semantic_models=[
            SemanticModelDef(
                name="shop.orders", content_sha256="x", relation="db.main.orders"
            ),
            SemanticModelDef(
                name="shop.customers", content_sha256="z", relation="db.main.customers"
            ),
        ],
        relationships=[
            RelationshipDef(
                name="orders_to_customers",
                content_sha256="y",
                model="shop.orders",
                to_model="shop.customers",
                column_pairs=[("customer_id", "customer_id")],
            )
        ],
        relationships_and_keys_captured=True,
    )
    datasets = [
        _dataset("db.main.orders", "customer_id"),
        _dataset("db.main.customers", "customer_id"),
    ]

    findings = semantic_free_drift(None, current, datasets, _snap(current))

    assert [f for f in findings if f.code == "broken_relationship"] == []


def test_relationship_added_removed_changed_only_when_both_sides_captured():
    rel = RelationshipDef(
        name="orders_to_customers",
        content_sha256="y",
        model="shop.orders",
        to_model="shop.customers",
        column_pairs=[("customer_id", "customer_id")],
    )
    baseline = SemanticLayer(relationships_and_keys_captured=True)
    current = SemanticLayer(relationships=[rel], relationships_and_keys_captured=True)

    findings = semantic_free_drift(None, current, [], _snap(baseline))

    added = [f for f in findings if f.code == "definition_added"]
    assert added and added[0].data == {
        "kind": "relationship",
        "name": "orders_to_customers",
    }


def test_an_uncaptured_baseline_does_not_report_a_relationship_as_added():
    """The exact hazard the issue names: an old baseline's empty relationship
    list must never be compared as if it meant "declares none"."""

    rel = RelationshipDef(
        name="orders_to_customers",
        content_sha256="y",
        model="shop.orders",
        to_model="shop.customers",
        column_pairs=[("customer_id", "customer_id")],
    )
    baseline = SemanticLayer(relationships_and_keys_captured=False)
    current = SemanticLayer(relationships=[rel], relationships_and_keys_captured=True)

    findings = semantic_free_drift(None, current, [], _snap(baseline))

    assert [
        f for f in findings if f.code in ("definition_added", "definition_removed")
    ] == []


def test_a_changed_relationship_is_reported_when_both_sides_captured():
    baseline_rel = RelationshipDef(
        name="orders_to_customers",
        content_sha256="before",
        model="shop.orders",
        to_model="shop.customers",
        column_pairs=[("customer_id", "customer_id")],
    )
    current_rel = RelationshipDef(
        name="orders_to_customers",
        content_sha256="after",
        model="shop.orders",
        to_model="shop.customers",
        column_pairs=[("region", "country_code")],
    )
    baseline = SemanticLayer(
        relationships=[baseline_rel], relationships_and_keys_captured=True
    )
    current = SemanticLayer(
        relationships=[current_rel], relationships_and_keys_captured=True
    )

    findings = semantic_free_drift(None, current, [], _snap(baseline))

    changed = [f for f in findings if f.code == "definition_changed"]
    assert changed and changed[0].data["kind"] == "relationship"
