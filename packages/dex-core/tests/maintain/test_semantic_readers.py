"""The semantic-layer objects that read an incident's column (#1478): the pure
lookup over ``maintain``'s own semantic-layer fingerprint."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from exmergo_dex_core.maintain import semantic_readers as sr
from exmergo_dex_core.maintain.diagnose import _Layer
from exmergo_dex_core.maintain.snapshot import (
    MetricDef,
    SemanticLayerSnapshot,
    SemanticModelDef,
)

TESTED = (
    sr.TestedColumn(test="test.shop.unique_x", model="stg_orders", columns=("id",)),
)


def _model(name: str, model_ref: str | None, **members: Any) -> SemanticModelDef:
    return SemanticModelDef(
        name=name, content_sha256="0", model_ref=model_ref, **members
    )


def _metric(
    name: str, measures: list[str] | None = None, metrics: list[str] | None = None
) -> MetricDef:
    return MetricDef(
        name=name,
        content_sha256="0",
        input_measures=measures or [],
        input_metrics=metrics or [],
    )


def _layer(*models: SemanticModelDef, metrics: tuple[MetricDef, ...] = ()):
    return SemanticLayerSnapshot(
        semantic_models=list(models), metrics=list(metrics), vendor="dbt"
    )


def test_metrics_are_reached_through_every_metric_built_on_the_measure():
    layer = _layer(
        _model("orders", "stg_orders", measures={"order_count": "id"}),
        metrics=(
            _metric("orders", measures=["order_count"]),
            _metric("orders_x2", metrics=["orders"]),
            _metric("orders_x4", metrics=["orders_x2"]),
            _metric("unrelated", measures=["other"]),
        ),
    )
    [reader] = sr.readers(layer, TESTED).readers

    assert reader.metrics == ("orders", "orders_x2", "orders_x4")


def test_a_metric_cycle_ends():
    layer = _layer(
        _model("orders", "stg_orders", measures={"order_count": "id"}),
        metrics=(
            _metric("a", measures=["order_count"], metrics=["b"]),
            _metric("b", metrics=["a"]),
        ),
    )
    [reader] = sr.readers(layer, TESTED).readers

    assert reader.metrics == ("a", "b")


def test_the_model_and_column_are_matched_without_case():
    layer = _layer(_model("orders", "STG_Orders", measures={"order_count": "ID"}))
    [reader] = sr.readers(layer, TESTED).readers

    assert (reader.model, reader.column) == ("STG_Orders", "ID")


def test_a_semantic_model_on_another_model_is_not_listed_even_when_computed():
    layer = _layer(
        _model("customers", "stg_customers", measures={"n": "id", "x": None}),
    )
    got = sr.readers(layer, TESTED)

    assert got.status == "read"
    assert (got.readers, got.unresolved) == ((), ())


def test_a_semantic_model_whose_model_cannot_be_read_is_unresolved():
    layer = _layer(_model("orders", None, measures={"order_count": "id"}))
    [item] = sr.readers(layer, TESTED).unresolved

    assert (item.kind, item.name, item.reason, item.model) == (
        "semantic_model",
        "orders",
        "model_unresolved",
        None,
    )


def test_metrics_alone_are_a_layer_that_was_read():
    got = sr.readers(_layer(metrics=(_metric("m", measures=["x"]),)), TESTED)

    assert got.status == "read"
    assert got.readers == ()


def test_unparsed_files_beside_a_read_layer_say_the_list_may_be_short():
    layer = _layer(_model("orders", "stg_orders", measures={"order_count": "id"}))
    got = sr.readers(layer, TESTED, unread_files=["models/b.yml", "models/a.yml"])

    assert got.status == "read"
    assert got.unread_files == ("models/a.yml", "models/b.yml")
    assert got.detail is not None
    assert "not listed" in got.detail
    assert [r.name for r in got.readers] == ["order_count"]


def test_a_native_vendor_is_not_read_and_says_why():
    engine = SimpleNamespace(
        config=SimpleNamespace(semantic=SimpleNamespace(vendor="ossie"))
    )
    got = _Layer.read(engine).readers(TESTED)  # type: ignore[arg-type]

    assert got.status == "not_read"
    assert got.reason == "vendor_not_covered"
    assert got.vendor == "ossie"
    assert got.tested == TESTED


def test_a_project_that_cannot_be_read_is_unreadable():
    from exmergo_dex_core.errors import ProjectError

    def broken() -> None:
        raise ProjectError("no dbt_project.yml")

    engine = SimpleNamespace(
        config=SimpleNamespace(semantic=SimpleNamespace(vendor=None)),
        maintain_project=broken,
    )
    got = _Layer.read(engine).readers(TESTED)  # type: ignore[arg-type]

    assert got.status == "unreadable"
    assert got.reason == "project_unreadable"
    assert got.vendor == "dbt"


def _incident(column: str | None) -> Any:
    return SimpleNamespace(
        tests=("test.shop.volume",), signal=SimpleNamespace(column=column)
    )


def _anomaly_manifest() -> dict[str, Any]:
    return {
        "nodes": {
            "model.shop.stg_orders": {"resource_type": "model", "name": "stg_orders"},
            "test.shop.volume": {
                "resource_type": "test",
                "test_metadata": {"name": "column_anomalies", "kwargs": {}},
                "attached_node": "model.shop.stg_orders",
            },
        }
    }


def test_a_column_anomaly_test_reads_the_column_its_source_recorded():
    from exmergo_dex_core.maintain.diagnose import _tested_columns

    [tested] = _tested_columns(_anomaly_manifest(), _incident("id"))

    assert (tested.model, tested.columns) == ("stg_orders", ("id",))


def test_a_test_on_no_column_and_no_recorded_column_names_nothing():
    from exmergo_dex_core.maintain.diagnose import _tested_columns

    assert _tested_columns(_anomaly_manifest(), _incident(None)) == []


def test_a_semantic_model_on_a_model_built_from_the_tested_one_is_not_followed():
    # The scope every read states: a mart that selects the column from the
    # tested staging model is not traced, and the empty list says so.
    layer = _layer(_model("orders", "fct_orders", measures={"order_count": "id"}))
    got = sr.readers(layer, TESTED)

    assert got.status == "read"
    assert got.readers == ()
    assert got.detail == sr.SCOPE
    assert "not followed" in sr.SCOPE


def test_an_unversioned_tested_model_still_names_its_readers():
    layer = _layer(_model("orders", "stg_orders", measures={"order_count": "id"}))
    got = sr.readers(layer, TESTED)

    assert [r.name for r in got.readers] == ["order_count"]
    assert got.unresolved == ()


def test_a_versioned_tested_model_never_names_a_reader():
    tested = (
        sr.TestedColumn(
            test="test.shop.unique_x", model="stg_orders", columns=("id",), version="1"
        ),
    )
    layer = _layer(
        _model("orders", "stg_orders", measures={"order_count": "id", "n": "x"})
    )
    got = sr.readers(layer, tested)

    assert got.readers == ()
    [item] = got.unresolved
    assert (item.name, item.reason, item.column) == (
        "order_count",
        "version_unresolved",
        "id",
    )


def _test_manifest(test: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": {
            "model.shop.stg_orders": {"resource_type": "model", "name": "stg_orders"},
            "test.shop.volume": {
                "resource_type": "test",
                "attached_node": "model.shop.stg_orders",
                **test,
            },
        }
    }


def test_a_multi_column_uniqueness_test_reads_each_of_its_columns():
    # A unique_combination_of_columns test carries no column_name: its columns
    # are its claim, and each one is looked up.
    from exmergo_dex_core.maintain.diagnose import _tested_columns

    manifest = _test_manifest(
        {
            "test_metadata": {
                "name": "unique_combination_of_columns",
                "kwargs": {"combination_of_columns": ["order_id", "line_no"]},
            }
        }
    )
    tested = _tested_columns(manifest, _incident(None))
    layer = _layer(
        _model(
            "orders",
            "stg_orders",
            entities={"order": "order_id"},
            dimensions={"line": "line_no"},
        )
    )
    got = sr.readers(layer, tested)

    assert [t.columns for t in tested] == [("order_id", "line_no")]
    assert [(r.kind, r.column) for r in got.readers] == [
        ("dimension", "line_no"),
        ("entity", "order_id"),
    ]


def test_the_tested_column_is_matched_without_case():
    tested = (
        sr.TestedColumn(test="test.shop.unique_x", model="stg_orders", columns=("ID",)),
    )
    layer = _layer(_model("orders", "stg_orders", measures={"order_count": "id"}))
    [reader] = sr.readers(layer, tested).readers

    assert (reader.name, reader.column) == ("order_count", "id")


def test_a_dimension_named_like_a_measure_input_carries_no_metrics():
    # Only a measure carries metrics, even where a dimension shares the name a
    # metric lists among its input measures.
    layer = _layer(
        _model("orders", "stg_orders", dimensions={"order_count": "id"}),
        metrics=(_metric("orders", measures=["order_count"]),),
    )
    [reader] = sr.readers(layer, TESTED).readers

    assert (reader.kind, reader.metrics) == ("dimension", ())


def test_a_quoted_attached_column_is_read_unquoted():
    from exmergo_dex_core.maintain.diagnose import _tested_columns

    manifest = _test_manifest(
        {
            "test_metadata": {"name": "not_null", "kwargs": {}},
            "column_name": '"Order_ID"',
        }
    )
    [tested] = _tested_columns(manifest, _incident(None))

    assert tested.columns == ("Order_ID",)


def test_readers_are_sorted_whatever_the_order_they_are_declared_in():
    layer = _layer(
        _model(
            "orders",
            "stg_orders",
            measures={"z_count": "id", "a_count": "id", "m_count": "id"},
        )
    )
    got = sr.readers(layer, TESTED)

    assert [r.name for r in got.readers] == ["a_count", "m_count", "z_count"]
