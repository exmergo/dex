"""Normalization: what dex claims from a document, and what it refuses to.

Almost every test here asserts an *absence*. That is the point of the module
under test: Ossie is a specification still under revision, and every place it is
silent is a place where a plausible inference reaches a payload indistinguishable
from a fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.adapters.project import ProjectContext
from exmergo_dex_core.ossie import OssieProject

from .conftest import dataset, document, expression, field, model, write


def build(root: Path, *names: str, connector: str = "duckdb") -> OssieProject:
    return OssieProject.from_context(
        ProjectContext(
            repo_root=str(root), connector=connector, options={"files": list(names)}
        )
    )


@pytest.fixture
def catalog(repo: Path):
    return build(repo, "commerce.ossie.yaml").semantic_catalog()


@pytest.fixture
def declared(repo: Path):
    return build(repo, "commerce.ossie.yaml").definitions()


def dimension(catalog, name):
    return next(d for d in catalog.dimensions if d.name == name)


def metric(catalog, name):
    return next(m for m in catalog.metrics if m.name == name)


# --- semantic models -------------------------------------------------------


def test_a_dataset_becomes_a_semantic_model_namespaced_by_its_layer(catalog):
    """The namespace is what lets two documents each declare `orders`."""

    assert [m.name for m in catalog.semantic_models] == [
        "commerce.orders",
        "commerce.customers",
        "commerce.order_items",
        "commerce.recent_orders",
    ]
    assert dimension(catalog, "orders__order_id").semantic_model == "commerce.orders"


def test_a_dataset_source_that_parses_as_a_relation_resolves(catalog):
    model_ = next(m for m in catalog.semantic_models if m.name == "commerce.orders")

    assert model_.relation == "demo.main.orders"
    assert model_.label == "orders"


def test_nothing_metricflow_shaped_is_invented(catalog):
    """Ossie has no measures, entities, ratios, grains, or join planning.

    A format that filled those with the nearest available thing would be putting
    words in the document author's mouth, and the payload gives a reader no way
    to tell an inference from a declaration.
    """

    assert catalog.entities == []
    assert catalog.measures == []
    for model_ in catalog.semantic_models:
        assert model_.model_ref is None
        assert model_.agg_time_dimension is None
        assert model_.primary_entity is None
    for metric_ in catalog.metrics:
        assert metric_.composition is None
        assert metric_.input_measures is None
        assert metric_.filter is None
        assert metric_.time_axis is None


# --- physical linkage ------------------------------------------------------


def test_a_bare_identifier_on_a_relation_source_links(catalog):
    assert dimension(catalog, "orders__order_id").column == "order_id"
    assert catalog.physical_columns["orders__order_id"] == (
        "demo.main.orders",
        "order_id",
    )


def test_a_computed_expression_carries_no_column_and_says_why(catalog):
    """A column guessed out of an expression makes the PII gate over-claim.

    It would screen a column that is not the one behind the element and report
    the verdict as evidence-backed, which is worse than screening nothing.
    """

    assert dimension(catalog, "orders__net_total").column is None
    assert "orders__net_total" not in catalog.physical_columns
    assert any("net_total" in note and "expression" in note for note in catalog.notes)


def test_a_quoted_identifier_does_not_link(catalog):
    """A documented, tested limitation rather than an emergent one.

    An unquoted Ossie identifier is folded the way the warehouse folds it while a
    quoted one is exact, so the two do not name the same relation on Snowflake or
    Postgres, and dex cannot tell which the author meant.
    """

    assert dimension(catalog, "orders__region").column is None
    assert "orders__region" not in catalog.physical_columns


def test_a_query_backed_source_produces_no_relation_and_no_columns(catalog):
    """The specification permits a table reference *or a query*, with no
    portable way to tell them apart, so the acceptor decides.

    Accepting a query as a relation would put a SQL string in front of the PII
    gate as physical evidence.
    """

    opaque = next(
        m for m in catalog.semantic_models if m.name == "commerce.recent_orders"
    )

    assert opaque.relation is None
    assert dimension(catalog, "recent_orders__order_id").column is None, (
        "`column` is read together with the model's `relation` to form an "
        "address, so half an address with no other half claims a link that "
        "does not exist"
    )
    assert "recent_orders__order_id" not in catalog.physical_columns
    assert any("recent_orders" in note for note in catalog.notes)


def test_a_field_declaring_only_a_non_sql_dialect_links_nothing(tmp_path: Path):
    write(
        tmp_path,
        "mdx.ossie.yaml",
        document(
            model(
                "m",
                dataset(
                    "d",
                    "demo.main.d",
                    {"name": "f", "expression": expression(MDX="[a].[b]")},
                ),
            )
        ),
    )

    catalog = build(tmp_path, "mdx.ossie.yaml").semantic_catalog()

    assert dimension(catalog, "d__f").column is None
    assert dimension(catalog, "d__f").vendor_params["dialects"] == {"MDX": "[a].[b]"}
    assert any("not SQL" in note for note in catalog.notes)


def test_the_connector_decides_whether_a_source_is_a_relation(tmp_path: Path):
    """ClickHouse relations are two parts, so a three-part source is not one.

    Which is exactly why the rule is the connector's and not the format's.
    """

    write(
        tmp_path,
        "ch.ossie.yaml",
        document(model("m", dataset("d", "demo.main.d", field("f")))),
    )

    duck = build(tmp_path, "ch.ossie.yaml", connector="duckdb").semantic_catalog()
    click = build(tmp_path, "ch.ossie.yaml", connector="clickhouse").semantic_catalog()

    assert duck.semantic_models[0].relation == "demo.main.d"
    assert click.semantic_models[0].relation is None


def test_a_source_is_normalized_the_way_the_connector_folds_it(tmp_path: Path):
    """So an authored `Main.Orders` matches the cache's `main.orders`."""

    write(
        tmp_path,
        "case.ossie.yaml",
        document(model("m", dataset("d", "Demo.Main.Orders", field("f")))),
    )

    for connector, expected in (
        ("snowflake", "DEMO.MAIN.ORDERS"),
        ("postgres", "demo.main.orders"),
        ("duckdb", "Demo.Main.Orders"),
    ):
        catalog = build(
            tmp_path, "case.ossie.yaml", connector=connector
        ).semantic_catalog()
        assert catalog.semantic_models[0].relation == expected


# --- dimensions ------------------------------------------------------------


def test_a_temporal_datatype_makes_a_dimension_time(catalog):
    assert dimension(catalog, "orders__placed_on").type == "time"


def test_an_explicit_time_role_wins_over_the_datatype(catalog):
    assert dimension(catalog, "orders__status_changed").type == "time"


def test_an_explicit_false_time_role_suppresses_the_datatype_default(
    tmp_path: Path,
):
    """The schema documents `is_time` as defaulting from the datatype, so an
    explicit false is a decision rather than an absence."""

    write(
        tmp_path,
        "notime.ossie.yaml",
        document(
            model(
                "m",
                dataset(
                    "d",
                    "demo.main.d",
                    field("stamp", datatype="Date", dimension={"is_time": False}),
                ),
            )
        ),
    )

    catalog = build(tmp_path, "notime.ossie.yaml").semantic_catalog()

    assert dimension(catalog, "d__stamp").type == "categorical"


def test_a_categorical_dimension_states_that_it_has_no_grain(catalog):
    """An empty list is a fact; absent would mean "nobody asked"."""

    assert dimension(catalog, "orders__order_id").queryable_granularities == []


def test_a_time_dimension_leaves_its_grains_unstated(catalog):
    """Ossie declares no grain vocabulary, so dex was never told.

    An empty list here would positively state that no granularity is queryable,
    which is a different claim and a false one. It is also, specifically, not a
    place to stash dialect metadata: that has its own declared slot.
    """

    assert dimension(catalog, "orders__placed_on").queryable_granularities is None


def test_every_declared_dialect_and_the_ai_context_are_preserved(tmp_path: Path):
    """Neither is dropped, and neither is smuggled into an unrelated field."""

    write(
        tmp_path,
        "keep.ossie.yaml",
        document(
            model(
                "m",
                dataset(
                    "d",
                    "demo.main.d",
                    {
                        "name": "f",
                        "datatype": "String",
                        "ai_context": {"instructions": "use sparingly"},
                        "custom_extensions": [
                            {"vendor_name": "DBT", "data": '{"x": 1}'}
                        ],
                        "expression": expression(
                            ANSI_SQL="f", SNOWFLAKE="F", MDX="[d].[f]"
                        ),
                    },
                ),
            )
        ),
    )

    params = dimension(
        build(tmp_path, "keep.ossie.yaml").semantic_catalog(), "d__f"
    ).vendor_params

    assert params["dialects"] == {"ANSI_SQL": "f", "SNOWFLAKE": "F", "MDX": "[d].[f]"}
    assert params["dialect"] == "ANSI_SQL"
    assert params["expression"] == "f"
    assert params["ai_context"] == {"instructions": "use sparingly"}
    assert params["custom_extensions"] == [{"vendor_name": "DBT", "data": '{"x": 1}'}]
    assert params["datatype"] == "String"


def test_vendor_params_are_flat_and_absent_when_there_is_nothing_to_carry(
    tmp_path: Path,
):
    """One declared escape hatch, one convention: the shipped dbt backends
    write flat keys, and the catalog already names its vendor at top level."""

    write(
        tmp_path,
        "flat.ossie.yaml",
        document(model("m", dataset("d", "demo.main.d", field("f")))),
    )

    params = dimension(
        build(tmp_path, "flat.ossie.yaml").semantic_catalog(), "d__f"
    ).vendor_params

    assert "ossie" not in params
    assert set(params) == {"expression", "dialect", "dialects"}


# --- metrics ---------------------------------------------------------------


def test_metric_lineage_comes_only_from_references_that_resolve(catalog):
    assert metric(catalog, "revenue").semantic_models == ["commerce.orders"]


def test_unresolved_metric_lineage_is_empty_rather_than_maximal(catalog):
    """Ossie states no metric-to-dataset reference at all.

    Naming every dataset in the semantic model would be the maximal claim
    dressed as a conservative one, and a reader has no way to tell it from a
    fact the document stated.
    """

    assert metric(catalog, "order_count").semantic_models is None
    assert any("order_count" in note for note in catalog.notes)


def test_a_coincidental_dotted_pair_resolves_to_nothing(tmp_path: Path):
    """Resolution is what makes scanning for references safe."""

    write(
        tmp_path,
        "coincidence.ossie.yaml",
        document(
            model(
                "m",
                dataset("d", "demo.main.d", field("f")),
                metrics=[
                    {
                        "name": "m1",
                        "expression": expression(ANSI_SQL="SUM(other.thing)"),
                    }
                ],
            )
        ),
    )

    catalog = build(tmp_path, "coincidence.ossie.yaml").semantic_catalog()

    assert metric(catalog, "m1").semantic_models is None


def test_metric_groupability_is_unstated_rather_than_inferred(catalog):
    """Lineage says an expression mentions a dataset. It does not say a field
    can group the metric, nor that a relationship path is executable."""

    assert metric(catalog, "revenue").dimensions == []


def test_a_possible_metric_reference_stays_opaque_expression_text(tmp_path: Path):
    """Ossie has not defined the grammar or scope for a metric reference, so
    promoting one into neutral composition would make an interpretation a fact."""

    write(
        tmp_path,
        "refs.ossie.yaml",
        document(
            model(
                "m",
                dataset("d", "demo.main.d", field("f")),
                metrics=[
                    {"name": "a", "expression": expression(ANSI_SQL="SUM(d.f)")},
                    {"name": "b", "expression": expression(ANSI_SQL="a / 2")},
                ],
            )
        ),
    )

    catalog = build(tmp_path, "refs.ossie.yaml").semantic_catalog()
    derived = metric(catalog, "b")

    assert derived.composition is None
    assert derived.vendor_params["expression"] == "a / 2"


# --- tier-1 declarations ---------------------------------------------------


def test_a_single_column_key_is_a_declared_key(declared):
    keys = {(k.model, k.column, k.unique) for k in declared.declared_keys}

    assert ("commerce.orders", "order_id", True) in keys
    assert ("commerce.customers", "customer_id", True) in keys


def test_a_composite_key_keeps_every_column_in_order_and_never_splits(declared):
    """Splitting it would say each column is unique on its own, which is a
    much stronger claim the document does not make and reconcile would act on."""

    assert [(k.model, k.columns) for k in declared.declared_composite_keys] == [
        ("commerce.order_items", ["order_id", "line_no"])
    ]
    assert not [k for k in declared.declared_keys if k.model == "commerce.order_items"]


def test_a_single_column_relationship_becomes_a_declared_join(declared):
    join = next(j for j in declared.foreign_keys)

    assert (join.model, join.column) == ("commerce.orders", "customer_id")
    assert (join.to_model, join.to_column) == ("commerce.customers", "customer_id")
    assert (join.relation, join.to_relation) == (
        "demo.main.orders",
        "demo.main.customers",
    )
    assert join.source == "ossie"


def test_a_composite_relationship_preserves_every_ordered_pair(declared):
    """A composite join is one declaration, never several dangerous halves."""

    assert len(declared.foreign_keys) == 1
    composite = next(
        rel for rel in declared.declared_relationships if rel.name == "items_to_orders"
    )
    assert composite.column_pairs == [("order_id", "order_id"), ("line_no", "line_no")]
    assert composite.source == "ossie"


def test_relationship_columns_need_not_be_declared_fields(tmp_path: Path):
    """Upstream requires only non-empty arrays, and a valid document routinely
    names a physical foreign key that no field entry declares."""

    write(
        tmp_path,
        "undeclared.ossie.yaml",
        document(
            model(
                "m",
                dataset("child", "demo.main.child", field("x"), primary_key=["x"]),
                dataset("parent", "demo.main.parent", field("y"), primary_key=["y"]),
                relationships=[
                    {
                        "name": "r",
                        "from": "child",
                        "to": "parent",
                        "from_columns": ["parent_fk"],
                        "to_columns": ["y"],
                    }
                ],
            )
        ),
    )

    declared = build(tmp_path, "undeclared.ossie.yaml").definitions()

    assert declared.foreign_keys[0].column == "parent_fk"


def test_built_relation_names_stays_empty_and_says_so(declared):
    """Ossie declares the relations it reads and builds none, so explore cannot
    use it to tell that a warehouse relation is unaccounted for."""

    assert declared.built_relation_names == []
    assert any("builds none" in note for note in declared.notes)


def test_the_declarations_name_ossie_as_their_channel(declared):
    assert declared.present
    assert declared.relationship_source == "ossie"
    assert declared.semantic_source == "ossie"
