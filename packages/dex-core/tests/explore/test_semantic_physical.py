"""The link between the semantic layer and the physical catalog (#360, #361).

Two directions and one rule. Forwards: a semantic model resolves to the relation
it sits on, and every dimension, entity declaration and measure resolves to its
column on that relation, so a caller holding a metric can reach the table behind
it. Backwards: the declared entity graph resolves to join edges between two real
relations, which is a join the layer performs rather than one dex guessed at.

The rule underneath both is that an expression resolves to **nothing**. That is
the assertion worth having: an invented column reads exactly like a real one, and
the PII gate adjudicates a dimension by resolving it to a column, so a guess there
screens the wrong column and reports the verdict as evidence-backed.
"""

from __future__ import annotations

import json
from pathlib import Path

from fakes.semantic import FakeHostedBackend

from exmergo_dex_core import dbt_project as dbt_project_module
from exmergo_dex_core.dbt_project import ResolvedPath
from exmergo_dex_core.semantic_catalog import column_reference, entity_joins

# ---- the shared rule --------------------------------------------------------


def test_column_reference_resolves_a_reference_and_refuses_an_expression():
    # A bare identifier is the column, whatever the element is called.
    assert column_reference("customer_id", "customer") == "customer_id"
    # No expr at all: the element references the column its own name spells.
    assert column_reference(None, "status") == "status"
    # An expression has no single column, and guessing one is what makes every
    # reader downstream over-claim.
    assert column_reference("CASE WHEN x THEN 1 ELSE 0 END", "flagged") is None
    assert column_reference("base_rate * 1.2", "markup") is None
    assert column_reference("lower(email)", "email") is None


# ---- forwards: the catalog reaches the warehouse ----------------------------


def _joined_manifest(tmp_path: Path) -> Path:
    """Two models on two relations, joined by a shared entity whose key differs
    per side, plus one dimension and one measure that are expressions.

    The differing key is the point: `users.id` against `orders.buyer_id` is the
    join a name-matching rule cannot find and the layer states outright.
    """

    project = tmp_path / "joined"
    (project / "target").mkdir(parents=True)
    manifest = {
        "semantic_models": [
            {
                "name": "customers_sm",
                "node_relation": {
                    "alias": "stg_customers",
                    "relation_name": "wh.main.stg_customers",
                },
                "defaults": {"agg_time_dimension": "signed_up_at"},
                "entities": [{"name": "customer", "type": "primary", "expr": "id"}],
                "dimensions": [
                    {"name": "signed_up_at", "type": "time"},
                    {"name": "region", "type": "categorical", "expr": "region_code"},
                    {"name": "markup", "type": "categorical", "expr": "base_rate * 2"},
                ],
                "measures": [{"name": "customer_count", "agg": "count", "expr": "id"}],
            },
            {
                "name": "orders_sm",
                "node_relation": {
                    "alias": "stg_orders",
                    "relation_name": "wh.main.stg_orders",
                },
                "defaults": {"agg_time_dimension": "ordered_at"},
                "entities": [
                    {"name": "order", "type": "primary"},
                    {"name": "customer", "type": "foreign", "expr": "buyer_id"},
                ],
                "dimensions": [{"name": "ordered_at", "type": "time"}],
                "measures": [
                    {"name": "order_count", "agg": "count", "expr": "order_id"},
                    {
                        "name": "net_revenue",
                        "agg": "sum",
                        "expr": "gross - discounts",
                    },
                ],
            },
        ],
        "metrics": [
            {
                "name": "orders",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "order_count"}]},
            }
        ],
    }
    (project / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))
    return project


def _catalog(project: Path, resolve_paths=lambda _text: None):
    return dbt_project_module.semantic_catalog(project, resolve_paths=resolve_paths)


def test_a_semantic_model_carries_the_relation_it_sits_on(tmp_path: Path):
    catalog = _catalog(_joined_manifest(tmp_path))

    relations = {m.name: m.relation for m in catalog.semantic_models}
    assert relations == {
        "customers_sm": "wh.main.stg_customers",
        "orders_sm": "wh.main.stg_orders",
    }


def test_the_relation_is_carried_once_and_never_repeated_on_an_element(
    tmp_path: Path,
):
    """The payload shape decision, asserted rather than left to review.

    A layer holds many more elements than models, and a fully qualified relation
    is long, so repeating it per element is what would make the link dominate a
    catalog whose byte budget is still open work. The element carries its column
    and the `semantic_model` pointer it already had; a caller joins the two.
    """

    catalog = _catalog(_joined_manifest(tmp_path))

    for element in (*catalog.dimensions, *catalog.measures):
        assert not hasattr(element, "relation")
        assert element.semantic_model or element.column is None


def test_a_dimension_measure_and_entity_carry_their_columns(tmp_path: Path):
    catalog = _catalog(_joined_manifest(tmp_path))

    dimensions = {d.name: d.column for d in catalog.dimensions}
    # No expr: the column the name spells. An expr that is a column: that column.
    assert dimensions["customer__signed_up_at"] == "signed_up_at"
    assert dimensions["customer__region"] == "region_code"

    measures = {m.name: m.column for m in catalog.measures}
    assert measures["order_count"] == "order_id"
    assert measures["customer_count"] == "id"

    customer = next(e for e in catalog.entities if e.name == "customer")
    by_model = {r.semantic_model: r.column for r in customer.roles}
    # The whole reason an entity cannot be one record: the key differs per model.
    assert by_model == {"customers_sm": "id", "orders_sm": "buyer_id"}


def test_an_expression_backed_element_carries_no_column(tmp_path: Path):
    catalog = _catalog(_joined_manifest(tmp_path))

    markup = next(d for d in catalog.dimensions if d.name == "customer__markup")
    assert markup.column is None, (
        "a computed dimension was given a column. The PII gate resolves a token to "
        "a column and reads that column's evidence, so an invented one screens the "
        "wrong column and calls the verdict authoritative"
    )
    net_revenue = next(m for m in catalog.measures if m.name == "net_revenue")
    assert net_revenue.column is None


def test_a_join_resolved_path_inherits_the_column_of_what_it_reaches(
    tmp_path: Path,
):
    """A path the join resolution added reaches a declaration, and the column it
    resolves to is that declaration's, not a guess from the token.

    Without this the PII gate falls back to the name heuristic on exactly the
    tokens the resolution added, which is the weaker screening on the half of the
    catalog a caller is most likely to group by.
    """

    project = _joined_manifest(tmp_path)

    def resolved(_text: str):
        return {
            "orders": [
                ResolvedPath(
                    "customer__region", "region", "customers_sm", "categorical"
                ),
                # Reached through two joins, so no single declaring model: absent
                # is the honest answer rather than one of the two candidates.
                ResolvedPath("order__customer__region", type="categorical"),
            ]
        }

    catalog = _catalog(project, resolve_paths=resolved)
    by_name = {d.name: d for d in catalog.dimensions}
    assert by_name["customer__region"].column == "region_code"
    assert by_name["order__customer__region"].column is None
    assert by_name["order__customer__region"].semantic_model is None


def test_the_synthesized_time_token_carries_no_column(tmp_path: Path):
    """`metric_time` is dex's own token over many physical columns, so naming one
    would be a claim the layer does not make."""

    catalog = _catalog(_joined_manifest(tmp_path))

    metric_time = next(d for d in catalog.dimensions if d.name == "metric_time")
    assert metric_time.column is None
    assert metric_time.semantic_model is None


# ---- backwards: the declared entity graph ------------------------------------


def test_a_shared_entity_becomes_a_join_between_two_relations(tmp_path: Path):
    joins = entity_joins(_catalog(_joined_manifest(tmp_path)))

    assert len(joins) == 1
    join = joins[0]
    assert join.entity == "customer"
    # Parent is the side that keys the entity, child the side that joins to it,
    # written in the direction a Relationship is.
    assert (join.parent_relation, join.parent_column) == ("wh.main.stg_customers", "id")
    assert (join.child_relation, join.child_column) == (
        "wh.main.stg_orders",
        "buyer_id",
    )


def test_an_entity_declared_in_one_model_yields_no_join(tmp_path: Path):
    """`order` is primary in one model and foreign in none, so there is nothing to
    join it to. A self-edge would be a box pointing at itself."""

    joins = entity_joins(_catalog(_joined_manifest(tmp_path)))

    assert not [j for j in joins if j.entity == "order"]


def _entity_manifest(tmp_path: Path, name: str, models: list[dict]) -> Path:
    project = tmp_path / name
    (project / "target").mkdir(parents=True)
    (project / "target" / "semantic_manifest.json").write_text(
        json.dumps({"semantic_models": models, "metrics": []})
    )
    return project


def _model(name: str, relation: str | None, entities: list[dict]) -> dict:
    return {
        "name": name,
        "node_relation": (
            {"alias": name, "relation_name": relation} if relation else None
        ),
        "entities": entities,
        "dimensions": [],
        "measures": [],
    }


def test_two_primaries_for_one_entity_yield_nothing(tmp_path: Path):
    """Which side keys the join is then not something the layer states, and
    resolution here never guesses, the same rule declared-endpoint resolution
    follows."""

    project = _entity_manifest(
        tmp_path,
        "twoprimary",
        [
            _model("a_sm", "wh.main.a", [{"name": "thing", "type": "primary"}]),
            _model("b_sm", "wh.main.b", [{"name": "thing", "type": "primary"}]),
            _model("c_sm", "wh.main.c", [{"name": "thing", "type": "foreign"}]),
        ],
    )

    assert entity_joins(_catalog(project)) == []


def test_a_unique_entity_keys_the_join_when_no_model_declares_a_primary(
    tmp_path: Path,
):
    project = _entity_manifest(
        tmp_path,
        "uniquekey",
        [
            _model(
                "a_sm",
                "wh.main.a",
                [{"name": "thing", "type": "unique", "expr": "tid"}],
            ),
            _model("b_sm", "wh.main.b", [{"name": "thing", "type": "foreign"}]),
        ],
    )

    joins = entity_joins(_catalog(project))
    assert [(j.parent_column, j.child_column) for j in joins] == [("tid", "thing")]


def test_a_natural_key_is_not_drawn_as_a_plain_join(tmp_path: Path):
    """A natural key identifies a row in a slowly-changing table, where a correct
    join also needs a validity window this catalog does not carry. An edge drawn
    without it is wrong rather than merely unproven."""

    project = _entity_manifest(
        tmp_path,
        "natural",
        [
            _model("a_sm", "wh.main.a", [{"name": "thing", "type": "primary"}]),
            _model("b_sm", "wh.main.b", [{"name": "thing", "type": "natural"}]),
        ],
    )

    assert entity_joins(_catalog(project)) == []


def test_a_side_with_no_relation_or_no_column_yields_nothing(tmp_path: Path):
    project = _entity_manifest(
        tmp_path,
        "partial",
        [
            _model("a_sm", "wh.main.a", [{"name": "thing", "type": "primary"}]),
            # No relation to draw between.
            _model("b_sm", None, [{"name": "thing", "type": "foreign"}]),
            # A key that is an expression, so no column to join on.
            _model(
                "c_sm",
                "wh.main.c",
                [{"name": "thing", "type": "foreign", "expr": "coalesce(x, y)"}],
            ),
        ],
    )

    assert entity_joins(_catalog(project)) == []


# ---- the hosted asymmetry, declared rather than implied ----------------------


def _hosted_metrics():
    """The hosted catalog as the live API actually answers it, verified by
    introspection and by one read of a real deployment: `Dimension`, `Entity` and
    `Measure` each carry `expr`, `SemanticModel` carries only a name, and most
    dimensions declare no `expr` at all because their name is the column."""

    return [
        {
            "name": "orders",
            "type": "SIMPLE",
            "dimensions": [
                {"name": "metric_time", "type": "TIME"},
                {
                    "name": "customer__region",
                    "type": "CATEGORICAL",
                    "expr": "region_code",
                    "semanticModel": {"name": "customers_sm"},
                },
                {
                    "name": "customer__signed_up_at",
                    "type": "TIME",
                    "semanticModel": {"name": "customers_sm"},
                },
            ],
            "entities": [
                {
                    "name": "customer",
                    "type": "FOREIGN",
                    "expr": "buyer_id",
                    "semanticModel": {"name": "orders_sm"},
                }
            ],
            "measures": [
                {"name": "order_count", "agg": "count", "expr": "order_id"},
                {"name": "net_revenue", "agg": "sum", "expr": "gross - discounts"},
            ],
            "semanticModels": [{"name": "orders_sm"}],
        }
    ]


def test_hosted_declares_that_it_cannot_reach_a_relation():
    catalog = FakeHostedBackend(metrics=_hosted_metrics()).list_definitions()

    assert "relation" in catalog.unavailable["semantic_models"], (
        "the hosted SemanticModel type carries only a name, and an absence a "
        "consumer must branch on has to be machine-readable rather than a note"
    )
    assert all(m.relation is None for m in catalog.semantic_models)
    assert any("no physical relation" in note for note in catalog.notes)


def test_hosted_carries_the_column_it_can_reach():
    """The gap is the relation, not the column: the API returns `expr` on
    dimensions, entities and measures, so a hosted catalog names the column behind
    every element and simply cannot say which table it is in."""

    catalog = FakeHostedBackend(metrics=_hosted_metrics()).list_definitions()

    assert "column" not in catalog.unavailable.get("dimensions", [])
    assert "column" not in catalog.unavailable.get("entities", [])
    assert "column" not in catalog.unavailable.get("measures", [])

    dimensions = {d.name: d.column for d in catalog.dimensions}
    # An expr that is a column, and a dimension with no expr whose bare name is
    # the column. Never the queryable token, which is a path nobody can select.
    assert dimensions["customer__region"] == "region_code"
    assert dimensions["customer__signed_up_at"] == "signed_up_at"
    # dex's own token over many columns, so no column and no declaration.
    assert dimensions["metric_time"] is None

    measures = {m.name: m.column for m in catalog.measures}
    assert measures["order_count"] == "order_id"
    assert measures["net_revenue"] is None, "an expression resolves to no column"

    customer = next(e for e in catalog.entities if e.name == "customer")
    assert [r.column for r in customer.roles] == ["buyer_id"]
