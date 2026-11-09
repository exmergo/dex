"""Reading Cube model files into the semantic catalog (#910).

What this release promises under `semantic.vendor: cube`: each configured plain
YAML file is validated by dex's own port of Cube's model rules, every refused
element is named with its file, a templated file, a JS model and an `extends` are
named as compiled-only and never translated, the catalog says what does not map
instead of inventing it, and nothing needs a Cube server or writes a file.

Held to the shipped source contracts, the backend contract, and the catalog
contract against a Cube rendering of `REFERENCE_LAYER`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("sqlglot")

from exmergo_dex_core.cube.catalog import member_token
from exmergo_dex_core.cube.project import CubeSemanticLayer
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic.backend import resolve_semantic_layer
from exmergo_dex_core.explore.semantic.conformance import (
    REFERENCE_LAYER,
    SemanticBackendContract,
    SemanticCatalogContract,
    declared_fields,
)
from exmergo_dex_core.explore.semantic.cube import CUBE_CATALOG_GAPS, LocalCubeLayer
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.semantic_source_conformance import (
    SemanticCatalogSourceContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticSourceFactoryContract,
)

from .conftest import ORDERS_LAYER, cube, repo, source, write


class TestCubeSemanticSource(
    SemanticSourceFactoryContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticCatalogSourceContract,
):
    """Construction, declarations, the fingerprint, and the read catalog."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def build_source(self, context: SemanticSourceContext):
        return CubeSemanticLayer.from_context(context)

    def empty_source_context(self) -> SemanticSourceContext:
        """One cube naming its relation and declaring nothing about it."""

        return _context(self.root, write(self.root, "model/thing.yml", cube("thing")))

    def absent_document_context(self) -> SemanticSourceContext:
        return _context(self.root, ["model/absent.yml"])

    def make_semantic_source(self):
        return self.build_source(self.empty_source_context())

    def an_unreadable_semantic_source(self):
        names = write(self.root, "model/broken.yml", raw="cubes: [x,\n")
        return CubeSemanticLayer.from_context(_context(self.root, names))

    def a_source_declaring_a_unique_key(self):
        declared = cube(
            "orders",
            dimensions=[
                {
                    "name": "order_id",
                    "sql": "order_id",
                    "type": "number",
                    "primary_key": True,
                }
            ],
        )
        return source(self.root, declared), "orders", "order_id"

    def a_source_declaring_a_join(self):
        orders = cube(
            "orders",
            joins=[
                {
                    "name": "customers",
                    "sql": "{CUBE}.customer_id = {customers}.customer_id",
                    "relationship": "many_to_one",
                }
            ],
        )
        customers = cube("customers")
        return (
            source(self.root, orders, customers),
            "orders",
            "customer_id",
            "customers",
            "customer_id",
        )

    def a_source_declaring_a_composite_key(self):
        key = {"type": "number", "primary_key": True}
        declared = cube(
            "line_items",
            dimensions=[
                {"name": "order_id", "sql": "order_id", **key},
                {"name": "line_no", "sql": "{CUBE}.line_no", **key},
                {
                    "name": "region",
                    "sql": "region",
                    "type": "string",
                    "primary_key": True,
                },
                {"name": "sku", "sql": "sku", "type": "string"},
            ],
        )
        return (
            source(self.root, declared),
            "line_items",
            ("order_id", "line_no", "region"),
        )

    def a_source_declaring_a_join_with_differently_named_sides(self):
        orders = cube(
            "orders",
            dimensions=[{"name": "buyer", "sql": "buyer_id", "type": "number"}],
            joins=[
                {
                    "name": "customers",
                    "sql": "{CUBE.buyer} = {customers.id}",
                    "relationship": "belongs_to",
                }
            ],
        )
        customers = cube(
            "customers",
            dimensions=[{"name": "id", "sql": "customer_key", "type": "number"}],
        )
        return (
            source(self.root, orders, customers),
            "orders",
            "buyer_id",
            "customers",
            "customer_key",
        )

    def a_source_declaring_a_semantic_model(self):
        declared = cube(
            "orders",
            dimensions=[
                {"name": "order_id", "sql": "order_id", "type": "number"},
                {"name": "status", "sql": "{CUBE}.status", "type": "string"},
                {"name": "ordered_at", "sql": "{CUBE.placed_at}", "type": "time"},
                {"name": "placed_at", "sql": "placed_at", "type": "time"},
                {
                    "name": "amount_cents",
                    "sql": "{CUBE}.amount * 100",
                    "type": "number",
                },
                {"name": "buyer", "sql": "{CUBE}.customer_id", "type": "number"},
            ],
            measures=[
                {"name": "revenue", "sql": "amount", "type": "sum"},
                {"name": "orders", "type": "count"},
                {"name": "gross", "sql": "{CUBE}.amount + {CUBE}.tax", "type": "sum"},
                # Calculated, so a metric and not a catalog measure.
                {"name": "aov", "sql": "{revenue} / {orders}", "type": "number"},
            ],
        )
        dimensions = {
            "order_id": "order_id",
            "status": "status",
            "ordered_at": "placed_at",
            "placed_at": "placed_at",
            "amount_cents": None,
            "buyer": "customer_id",
        }
        measures = {"revenue": "amount", "orders": None, "gross": None}
        return source(self.root, declared), "orders", dimensions, measures


class TestCubeBackend(SemanticBackendContract):
    """The `explore semantic` layer, held to the backend contract."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def make_backend(self):
        return LocalCubeLayer(source(self.root, *ORDERS_LAYER))


# --- the reference layer, seeded as Cube model files --------------------------------


def reference_cube_models() -> list[dict[str, Any]]:
    """`REFERENCE_LAYER` as Cube cubes, derived rather than hand-copied so a
    wider reference layer is a wider seed.

    The mapping is Cube's native one:

    - a semantic model is a cube on its relation;
    - an entity is its key column as a dimension (`primary_key` where it is
      primary), and a shared entity is a `many_to_one` join from the cube
      holding it as foreign to the cube keying it;
    - a dimension is a dimension;
    - a measure is an aggregating measure of the same name, and a simple
      metric is that measure (a filtered metric's filter is the measure's
      `filters`, which is where Cube puts a predicate for one measure);
    - a ratio is a `number` measure dividing its two sides.
    """

    declared = REFERENCE_LAYER["semantic_models"]
    keyed = {
        entity["name"]: (model["name"], entity["expr"])
        for model in declared
        for entity in model["entities"]
        if entity["type"] == "primary"
    }
    owner = {m["name"]: model["name"] for model in declared for m in model["measures"]}
    filters = {
        metric["measure"]: _sql_predicate(metric["filter"])
        for metric in REFERENCE_LAYER["metrics"]
        if metric.get("measure") and metric.get("filter")
    }
    measure_of = {
        metric["name"]: metric["measure"]
        for metric in REFERENCE_LAYER["metrics"]
        if metric.get("measure")
    }

    seeded: dict[str, dict[str, Any]] = {}
    for model in declared:
        dimensions = []
        joins = []
        for entity in model["entities"]:
            dimensions.append(
                {
                    "name": entity["expr"],
                    "sql": entity["expr"],
                    "type": "number",
                    "title": entity["label"],
                    "description": entity["description"],
                    **({"primary_key": True} if entity["type"] == "primary" else {}),
                }
            )
            if entity["type"] == "foreign":
                target, key = keyed[entity["name"]]
                joins.append(
                    {
                        "name": target,
                        "sql": f"{{CUBE}}.{entity['expr']} = {{{target}}}.{key}",
                        "relationship": "many_to_one",
                    }
                )
        dimensions.extend(
            {
                "name": dimension["name"],
                "sql": f"{{CUBE}}.{dimension['name']}",
                "type": "time" if dimension["type"] == "time" else "string",
                "title": dimension["label"],
                "description": dimension["description"],
            }
            for dimension in model["dimensions"]
        )
        measures = []
        for measure in model["measures"]:
            expr = measure["expr"]
            measures.append(
                {
                    "name": measure["name"],
                    "sql": expr if not expr.isidentifier() else f"{{CUBE}}.{expr}",
                    "type": {"average": "avg"}.get(measure["agg"], measure["agg"]),
                    "title": measure["label"],
                    "description": measure["description"],
                    **(
                        {"filters": [{"sql": filters[measure["name"]]}]}
                        if measure["name"] in filters
                        else {}
                    ),
                }
            )
        seeded[model["name"]] = cube(
            model["name"],
            sql_table=model["relation"],
            title=model["label"],
            description=model["description"],
            dimensions=dimensions,
            measures=measures,
            joins=joins,
        )
    for metric in REFERENCE_LAYER["metrics"]:
        if metric["type"] != "ratio":
            continue
        numerator = measure_of[metric["numerator"]]
        denominator = measure_of[metric["denominator"]]
        seeded[owner[numerator]]["measures"].append(
            {
                "name": metric["name"],
                "sql": f"{{{numerator}}} / {{{denominator}}}",
                "type": "number",
                "title": metric["label"],
                "description": metric["description"],
            }
        )
    return list(seeded.values())


def _sql_predicate(template: str) -> str:
    """A MetricFlow filter template as Cube SQL: the dimension is `{CUBE}.col`."""

    return re.sub(
        r"\{\{\s*Dimension\('[^']*__([^']+)'\)\s*\}\}", r"{CUBE}.\1", template
    ).strip()


def _reference_token(metric: dict[str, Any]) -> str:
    """The `cube.measure` token Cube reports for a reference metric."""

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


class TestCubeReferenceLayer(SemanticCatalogContract):
    """The catalog contract's content assertions, against the reference layer
    seeded as Cube model files. What Cube cannot carry is a declared gap, so a
    failure here is a bug in the read, not a shape to skip."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def make_reference_backend(self):
        # Through the engine, as a command reaches it. The config names no
        # warehouse: nothing here may need one.
        return resolve_semantic_layer(
            DexEngine.from_repo(repo(self.root, *reference_cube_models()))
        )

    def reported_name(self, kind: str, name: str) -> str:
        return REFERENCE_METRIC_TOKENS[name] if kind == "metrics" else name


def test_every_catalog_field_is_answered_or_declared_a_gap(tmp_path):
    """The silence rule against a layer that answers everything Cube can: a
    field with no value on any element is one Cube cannot supply, and it has to
    be named in `unavailable`, which has to say no more than that."""

    catalog = LocalCubeLayer(source(tmp_path, *ORDERS_LAYER)).list_definitions()
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
    assert catalog.to_data()["unavailable"] == CUBE_CATALOG_GAPS


def test_the_reference_rendering_is_plain_cube_yaml(tmp_path):
    """The seed is what an author would write, not a dex-only shape: it passes
    the reader with no diagnostic at all."""

    root = repo(tmp_path, *reference_cube_models())
    layer = DexEngine.from_repo(root).semantic_catalog_source()

    assert layer.loaded().diagnostics == []
    assert yaml.safe_load((root / "model" / "users_sm.yml").read_text())["cubes"]


def _context(root: Path, names: list[str]) -> SemanticSourceContext:
    return SemanticSourceContext(
        repo_root=str(root), connector="duckdb", options={"files": names}
    )
