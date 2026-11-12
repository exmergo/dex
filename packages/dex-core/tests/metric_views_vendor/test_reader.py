"""Reading Databricks Metric View files into the semantic catalog (#1257).

What this release promises under `semantic.vendor: databricks`: each configured
Metric View YAML file is validated by dex's own port of the YAML 1.1 rules, every
refused element is named with its file, the catalog says what does not map
instead of inventing it, and nothing opens a workspace or writes a file.

Held to the shipped source contracts, the backend contract, and the catalog
contract against a Metric View rendering of `REFERENCE_LAYER`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlglot")

from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic.backend import resolve_semantic_layer
from exmergo_dex_core.explore.semantic.conformance import (
    REFERENCE_LAYER,
    SemanticBackendContract,
    SemanticCatalogContract,
    declared_fields,
)
from exmergo_dex_core.explore.semantic.metric_views import (
    METRIC_VIEW_CATALOG_GAPS,
    LocalMetricViewLayer,
)
from exmergo_dex_core.metric_views.catalog import member_token
from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.semantic_source_conformance import (
    SemanticCatalogSourceContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticSourceFactoryContract,
)

from .conftest import ORDERS, repo, source, view, write


def _context(root: Path, files: list[str]) -> SemanticSourceContext:
    """A source context naming ``files`` on DuckDB."""
    return SemanticSourceContext(
        repo_root=str(root), connector="duckdb", options={"files": files}
    )


class TestMetricViewSemanticSource(
    SemanticSourceFactoryContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticCatalogSourceContract,
):
    """Construction, declarations, the fingerprint, and the read catalog."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        """Each test writes into its own temporary repository."""
        self.root = tmp_path

    def build_source(self, context: SemanticSourceContext):
        """Build through the factory the engine uses."""
        return MetricViewSemanticLayer.from_context(context)

    def empty_source_context(self) -> SemanticSourceContext:
        """A configured file that declares no view."""

        return _context(self.root, [write(self.root, "views/empty.yaml", raw="")])

    def absent_document_context(self) -> SemanticSourceContext:
        """A configured file that does not exist yet."""
        return _context(self.root, ["views/absent.yaml"])

    def make_semantic_source(self):
        """A source whose one file declares no view."""
        return self.build_source(self.empty_source_context())

    def an_unreadable_semantic_source(self):
        """A file that does not parse as YAML."""
        names = [write(self.root, "views/broken.yaml", raw="version: [1.1,\n")]
        return MetricViewSemanticLayer.from_context(_context(self.root, names))

    def a_source_declaring_a_unique_key(self):
        """The `rely` key on the `customer` join's joined table."""
        return source(self.root, orders=ORDERS), "orders.customer", "id"

    def a_source_declaring_a_join(self):
        """The nested `customer.nation` join, `using: [nation_key]`."""
        return (
            source(self.root, orders=ORDERS),
            "orders.customer",
            "nation_key",
            "orders.customer.nation",
            "nation_key",
        )

    def a_source_declaring_a_composite_key(self):
        """A `rely` join on three `using` columns: one composite key."""
        declared = view(
            "demo.main.line_items",
            joins=[
                {
                    "name": "shipments",
                    "source": "demo.main.shipments",
                    "using": ["order_id", "line_no", "region"],
                    "rely": {"at_most_one_match": True},
                }
            ],
            fields=[{"name": "region", "expr": "region"}],
        )
        return (
            source(self.root, line_items=declared),
            "line_items.shipments",
            ("order_id", "line_no", "region"),
        )

    def a_source_declaring_a_join_with_differently_named_sides(self):
        """`source.customer_id = customer.id`."""
        return (
            source(self.root, orders=ORDERS),
            "orders",
            "customer_id",
            "orders.customer",
            "id",
        )

    def a_source_declaring_a_semantic_model(self):
        """`ORDERS`, with every field and measure's column as expected."""
        dimensions = {
            "order_id": "order_id",
            "status": "status",
            "ordered_on": "placed_at",
            # Read through a join: the column is the joined table's, never the
            # view's own relation's.
            "customer_name": None,
            "nation": None,
            "size": None,
        }
        measures = {
            "revenue": "amount",
            "order_count": None,
            "paid": "amount",
            "units": None,
        }
        return source(self.root, orders=ORDERS), "orders", dimensions, measures


class TestMetricViewBackend(SemanticBackendContract):
    """The `explore semantic` layer, held to the backend contract."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        """Each test writes into its own temporary repository."""
        self.root = tmp_path

    def make_backend(self):
        """The explore backend over `ORDERS`."""
        return LocalMetricViewLayer(source(self.root, orders=ORDERS))


# --- the reference layer, seeded as Metric View files ---------------------------


def reference_metric_views() -> dict[str, dict[str, Any]]:
    """`REFERENCE_LAYER` as Metric Views, derived rather than hand-copied so a
    wider reference layer is a wider seed.

    - a semantic model is a view on its relation, named after its file;
    - an entity's key is a field, and a shared entity is a join from the view
      holding it as foreign to the relation keying it, with
      `rely.at_most_one_match` where it is primary there;
    - a dimension is a field, `format: date` where it is time;
    - a measure is a measure of the same name, and a simple metric is that
      measure (a filtered metric's filter is the measure's `FILTER (WHERE ...)`);
    - a ratio is a measure dividing the two sides with `MEASURE()`.
    """

    declared = REFERENCE_LAYER["semantic_models"]
    keyed = {
        entity["name"]: (model["relation"], entity["expr"])
        for model in declared
        for entity in model["entities"]
        if entity["type"] == "primary"
    }
    owner = {m["name"]: model["name"] for model in declared for m in model["measures"]}
    filters = {
        metric["measure"]: metric["filter"]
        for metric in REFERENCE_LAYER["metrics"]
        if metric.get("measure") and metric.get("filter")
    }
    measure_of = {
        m["name"]: m["measure"] for m in REFERENCE_LAYER["metrics"] if m.get("measure")
    }

    seeded: dict[str, dict[str, Any]] = {}
    for model in declared:
        fields: list[dict[str, Any]] = []
        joins: list[dict[str, Any]] = []
        for entity in model["entities"]:
            fields.append(
                {
                    "name": entity["expr"],
                    "expr": entity["expr"],
                    "display_name": entity["label"],
                    "comment": entity["description"],
                }
            )
            if entity["type"] == "foreign":
                relation, key = keyed[entity["name"]]
                joins.append(
                    {
                        "name": entity["name"],
                        "source": relation,
                        "on": f"source.{entity['expr']} = {entity['name']}.{key}",
                        "rely": {"at_most_one_match": True},
                    }
                )
        fields.extend(
            {
                "name": dimension["name"],
                "expr": dimension["name"],
                "display_name": dimension["label"],
                "comment": dimension["description"],
                **(
                    {"format": {"type": "date", "date_format": "year_month_day"}}
                    if dimension["type"] == "time"
                    else {}
                ),
            }
            for dimension in model["dimensions"]
        )
        measures = []
        for measure in model["measures"]:
            function = {"average": "AVG"}.get(measure["agg"], measure["agg"].upper())
            expr = f"{function}({measure['expr']})"
            if measure["name"] in filters:
                expr += f" FILTER (WHERE {_predicate(filters[measure['name']])})"
            measures.append(
                {
                    "name": measure["name"],
                    "expr": expr,
                    "display_name": measure["label"],
                    "comment": measure["description"],
                }
            )
        seeded[model["name"]] = view(
            model["relation"],
            comment=model["description"],
            joins=joins,
            fields=fields,
            measures=measures,
        )
        if not joins:
            del seeded[model["name"]]["joins"]
    for metric in REFERENCE_LAYER["metrics"]:
        if metric["type"] != "ratio":
            continue
        numerator = measure_of[metric["numerator"]]
        denominator = measure_of[metric["denominator"]]
        seeded[owner[numerator]]["measures"].append(
            {
                "name": metric["name"],
                "expr": f"MEASURE({numerator}) / MEASURE({denominator})",
                "display_name": metric["label"],
                "comment": metric["description"],
            }
        )
    return seeded


def _predicate(template: str) -> str:
    """A MetricFlow filter template as SQL over the view's own columns."""

    import re

    return re.sub(r"\{\{\s*Dimension\('[^']*__([^']+)'\)\s*\}\}", r"\1", template)


def _reference_token(metric: dict[str, Any]) -> str:
    """The `view.measure` token a reference metric is reported as."""
    owner = {
        m["name"]: model["name"]
        for model in REFERENCE_LAYER["semantic_models"]
        for m in model["measures"]
    }
    if metric["type"] == "ratio":
        numerator = next(
            m["measure"]
            for m in REFERENCE_LAYER["metrics"]
            if m["name"] == metric["numerator"]
        )
        return member_token(owner[numerator], metric["name"])
    return member_token(owner[metric["measure"]], metric["measure"])


REFERENCE_METRIC_TOKENS = {
    metric["name"]: _reference_token(metric) for metric in REFERENCE_LAYER["metrics"]
}


class TestMetricViewReferenceLayer(SemanticCatalogContract):
    """The catalog contract's content assertions, against the reference layer
    seeded as Metric Views. What a Metric View cannot carry is a declared gap, so
    a failure here is a bug in the read, not a shape to skip."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        """Each test writes into its own temporary repository."""
        self.root = tmp_path

    def make_reference_backend(self):
        """The reference layer, rendered as Metric Views, through the engine."""
        # Through the engine, as a command reaches it. The config names no
        # warehouse: nothing here may need one.
        return resolve_semantic_layer(
            DexEngine.from_repo(repo(self.root, **reference_metric_views()))
        )

    def reported_name(self, kind: str, name: str) -> str:
        """Metrics are reported as `view.measure`; everything else keeps its name."""
        return REFERENCE_METRIC_TOKENS[name] if kind == "metrics" else name

    def test_the_shared_entity_carries_a_declaration_per_model_with_its_own_key(
        self,
    ) -> None:
        """The join, and the key `rely` asserts on its far side.

        Narrower than the contract's own assertion by one key, and stated here
        rather than skipped: a Metric View declares no key on its own source
        (`sessions_sm.session_key` has nowhere to be written), so the only key
        the layer can state is the joined side's, through
        `rely.at_most_one_match`.
        """

        backend = self.make_reference_backend()
        joins = {
            (relationship.model, relationship.to_model): relationship.column_pairs
            for relationship in backend.declared_relationships()
        }
        assert [tuple(p) for p in joins[("sessions_sm", "users_sm")]] == [
            ("visitor_id", "user_id")
        ]
        keys, _ = backend.declared_keys()
        assert {(k.model, k.column) for k in keys} == {("users_sm", "user_id")}


def test_every_catalog_field_is_answered_or_declared_a_gap(tmp_path):
    """The silence rule against a layer that answers everything a Metric View
    can: a field with no value on any element is one it cannot supply, and it has
    to be named in `unavailable`, which has to say no more than that."""

    catalog = LocalMetricViewLayer(source(tmp_path, orders=ORDERS)).list_definitions()
    elements = {
        "semantic_models": catalog.semantic_models,
        "metrics": catalog.metrics,
        "dimensions": catalog.dimensions,
        "entities": catalog.entities,
        "measures": catalog.measures,
    }
    undeclared = [
        f"{kind}.{name}"
        for kind, found in elements.items()
        for name in declared_fields(kind)
        if not any(getattr(e, name, None) is not None for e in found)
        and name not in catalog.unavailable.get(kind, ())
    ]

    assert undeclared == []
    assert catalog.to_data()["unavailable"] == METRIC_VIEW_CATALOG_GAPS


def test_the_reference_rendering_is_plain_metric_view_yaml(tmp_path):
    """The seed is what an author would write: it reads with no diagnostic."""

    root = repo(tmp_path, **reference_metric_views())
    layer = DexEngine.from_repo(root).semantic_catalog_source()

    assert layer.loaded().diagnostics == []
