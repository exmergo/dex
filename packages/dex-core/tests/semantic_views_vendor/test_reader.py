"""Reading Snowflake Semantic View files into the semantic catalog (#1291).

What this release promises under `semantic.vendor: snowflake`: each configured
Semantic View YAML file is validated by dex's own port of Snowflake's rules,
every refused element is named with its file, the catalog says what does not map
instead of inventing it, and nothing opens a Snowflake connection or writes a
file.

Held to the shipped source contracts, the backend contract, and the catalog
contract against a Semantic View rendering of `REFERENCE_LAYER`.
"""

from __future__ import annotations

import re
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
from exmergo_dex_core.explore.semantic.semantic_views import (
    SEMANTIC_VIEW_CATALOG_GAPS,
    LocalSemanticViewLayer,
)
from exmergo_dex_core.semantic_source import SemanticSourceContext
from exmergo_dex_core.semantic_source_conformance import (
    SemanticCatalogSourceContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticSourceFactoryContract,
)
from exmergo_dex_core.semantic_views.loader import load_files
from exmergo_dex_core.semantic_views.project import SemanticViewSemanticLayer

from .conftest import SHOP, relationship, repo, source, sv, table, write


def _context(root: Path, files: list[str]) -> SemanticSourceContext:
    """A source context naming ``files`` on DuckDB."""
    return SemanticSourceContext(
        repo_root=str(root), connector="duckdb", options={"files": files}
    )


class TestSemanticViewSemanticSource(
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
        return SemanticViewSemanticLayer.from_context(context)

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
        names = [write(self.root, "views/broken.yaml", raw="name: [x,\n")]
        return SemanticViewSemanticLayer.from_context(_context(self.root, names))

    def a_source_declaring_a_unique_key(self):
        """`customers.email`, a declared unique key."""
        return source(self.root, SHOP), "shop.customers", "email"

    def a_source_declaring_a_join(self):
        """`lines.order_id` references `orders.order_id`."""
        return (
            source(self.root, SHOP),
            "shop.lines",
            "order_id",
            "shop.orders",
            "order_id",
        )

    def a_source_declaring_a_composite_key(self):
        """A three-column primary key."""
        declared = sv(
            "shipping",
            table(
                "shipments",
                "demo.main.shipments",
                ["order_id", "line_no", "region"],
                dimensions=[{"name": "region", "expr": "region"}],
            ),
        )
        return (
            source(self.root, declared),
            "shipping.shipments",
            ("order_id", "line_no", "region"),
        )

    def a_source_declaring_a_join_with_differently_named_sides(self):
        """`visits.visitor_id` references `people.person_id`."""
        declared = sv(
            "traffic",
            table(
                "visits",
                "demo.main.visits",
                ["visit_id"],
                metrics=[{"name": "visit_count", "expr": "COUNT(visit_id)"}],
            ),
            table(
                "people",
                "demo.main.people",
                ["person_id"],
                dimensions=[{"name": "tier", "expr": "tier"}],
            ),
            relationships=[
                relationship(
                    "visits_people", "visits", "people", ("visitor_id", "person_id")
                )
            ],
        )
        return (
            source(self.root, declared),
            "traffic.visits",
            "visitor_id",
            "traffic.people",
            "person_id",
        )

    def a_source_declaring_a_semantic_model(self):
        """`shop.orders`, with every row-level member and measure's column."""
        dimensions = {
            "status": "status",
            # Reads the `status` member, followed to its column.
            "status_label": "status",
            "size": None,
            "ordered_on": "placed_at",
            "amount": "amount",
            "discount": "discount",
        }
        measures = {
            "revenue": "amount",
            "order_count": "order_id",
            # An aggregate over a computed expression names no single column.
            "net": None,
        }
        return source(self.root, SHOP), "shop.orders", dimensions, measures


class TestSemanticViewBackend(SemanticBackendContract):
    """The `explore semantic` layer, held to the backend contract."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        """Each test writes into its own temporary repository."""
        self.root = tmp_path

    def make_backend(self):
        """The explore backend over `SHOP`."""
        return LocalSemanticViewLayer(source(self.root, SHOP))


# --- the reference layer, seeded as one Semantic View -----------------------------

REFERENCE_VIEW = "reference"


def reference_semantic_view() -> dict[str, Any]:
    """`REFERENCE_LAYER` as one Semantic View, derived rather than hand-copied so
    a wider reference layer is a wider seed.

    - a semantic model is a logical table on its relation, keyed on its primary
      entity's column;
    - a shared entity is a relationship from the table holding it as foreign to
      the table keying it;
    - a dimension is a dimension, a time dimension where it is time;
    - a measure is a table metric of the same name aggregating its expression,
      a filtered metric's filter folded into it as `CASE WHEN` (a Semantic View
      metric has no filter of its own), and a simple metric is that table
      metric;
    - a ratio is a view-level metric dividing the two table metrics.
    """

    declared = REFERENCE_LAYER["semantic_models"]
    keyed = {
        entity["name"]: (model["name"], entity["expr"])
        for model in declared
        for entity in model["entities"]
        if entity["type"] == "primary"
    }
    owner = {m["name"]: model["name"] for model in declared for m in model["measures"]}
    measure_of = {
        m["name"]: m["measure"] for m in REFERENCE_LAYER["metrics"] if m.get("measure")
    }
    filters = {
        m["measure"]: m["filter"]
        for m in REFERENCE_LAYER["metrics"]
        if m.get("measure") and m.get("filter")
    }

    tables: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for model in declared:
        primary = next(e for e in model["entities"] if e["type"] == "primary")
        for entity in model["entities"]:
            if entity["type"] == "foreign":
                target, key = keyed[entity["name"]]
                relationships.append(
                    relationship(
                        f"{model['name']}_{target}",
                        model["name"],
                        target,
                        (entity["expr"], key),
                    )
                )
        tables.append(
            table(
                model["name"],
                model["relation"],
                [primary["expr"]],
                description=model["description"],
                dimensions=[
                    {
                        "name": d["name"],
                        "expr": d["name"],
                        "description": d["description"],
                    }
                    for d in model["dimensions"]
                    if d["type"] != "time"
                ],
                time_dimensions=[
                    {
                        "name": d["name"],
                        "expr": d["name"],
                        "description": d["description"],
                    }
                    for d in model["dimensions"]
                    if d["type"] == "time"
                ],
                metrics=[
                    {
                        "name": m["name"],
                        "expr": _aggregate(m, filters.get(m["name"])),
                        "description": m["description"],
                    }
                    for m in model["measures"]
                ],
            )
        )
    derived = [
        {
            "name": metric["name"],
            "description": metric["description"],
            "expr": (
                f"{owner[measure_of[metric['numerator']]]}."
                f"{measure_of[metric['numerator']]} / "
                f"{owner[measure_of[metric['denominator']]]}."
                f"{measure_of[metric['denominator']]}"
            ),
        }
        for metric in REFERENCE_LAYER["metrics"]
        if metric["type"] == "ratio"
    ]
    return sv(REFERENCE_VIEW, *tables, relationships=relationships, metrics=derived)


def _function(agg: str) -> str:
    """The SQL aggregate a catalog aggregation names."""
    return {"average": "AVG"}.get(agg, agg.upper())


def _aggregate(measure: dict[str, Any], template: str | None) -> str:
    """A reference measure as one aggregate, a filtered metric's filter folded
    in (`SUM(CASE WHEN <filter> THEN <expr> END)`), since a Semantic View metric
    has no filter apart from its expression."""

    expr = measure["expr"]
    if template:
        predicate = re.sub(
            r"\{\{\s*Dimension\('[^']*__([^']+)'\)\s*\}\}", r"\1", template
        )
        expr = f"CASE WHEN {predicate} THEN {expr} END"
    return f"{_function(measure['agg'])}({expr})"


def _reported(kind: str, name: str) -> str:
    """How the Semantic View rendering names a reference element."""
    if kind == "semantic_models":
        return f"{REFERENCE_VIEW}.{name}"
    if kind == "metrics":
        metric = next(m for m in REFERENCE_LAYER["metrics"] if m["name"] == name)
        if metric["type"] == "ratio":
            return f"{REFERENCE_VIEW}.{name}"
        owner = next(
            model["name"]
            for model in REFERENCE_LAYER["semantic_models"]
            if any(m["name"] == metric["measure"] for m in model["measures"])
        )
        return f"{REFERENCE_VIEW}.{owner}.{metric['measure']}"
    if kind == "entities":
        model = next(
            model["name"]
            for model in REFERENCE_LAYER["semantic_models"]
            for entity in model["entities"]
            if entity["name"] == name and entity["type"] == "primary"
        )
        return f"{REFERENCE_VIEW}.{model}"
    if kind == "dimensions":
        model = next(
            model["name"]
            for model in REFERENCE_LAYER["semantic_models"]
            if any(d["name"] == name for d in model["dimensions"])
        )
        return f"{REFERENCE_VIEW}.{model}.{name}"
    return name


class TestSemanticViewReferenceLayer(SemanticCatalogContract):
    """The catalog contract's content assertions, against the reference layer
    seeded as one Semantic View. What a Semantic View cannot carry is a declared
    gap, so a failure here is a bug in the read, not a shape to skip."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        """Each test writes into its own temporary repository."""
        self.root = tmp_path

    def make_reference_backend(self):
        """The reference layer, rendered as a Semantic View, through the engine."""
        # Through the engine, as a command reaches it. The config names no
        # warehouse: nothing here may need one.
        return resolve_semantic_layer(
            DexEngine.from_repo(repo(self.root, reference_semantic_view()))
        )

    def reported_name(self, kind: str, name: str) -> str:
        """Every element is named within its view: `view.table[.member]`."""
        return _reported(kind, name)

    def test_the_shared_entity_carries_a_declaration_per_model_with_its_own_key(
        self,
    ) -> None:
        """The contract's own assertion, with the names a view gives.

        A Semantic View entity is the table its primary key keys, so the shared
        `user` entity is `reference.users_sm`, declared primary there on
        `user_id` and foreign on `reference.sessions_sm` on `visitor_id`: the
        same join graph the contract asserts, under the view's names.
        """

        catalog = self.make_reference_backend().list_definitions()
        user = next(e for e in catalog.entities if e.name == "reference.users_sm")
        by_model = {role.semantic_model: role for role in user.roles}

        assert set(by_model) == {"reference.users_sm", "reference.sessions_sm"}
        assert by_model["reference.users_sm"].type == "primary"
        assert by_model["reference.sessions_sm"].type == "foreign"
        assert by_model["reference.users_sm"].column == "user_id"
        assert by_model["reference.sessions_sm"].column == "visitor_id"
        assert user.type == "primary"


def test_every_catalog_field_is_answered_or_declared_a_gap(tmp_path):
    """The silence rule against a layer that answers everything a Semantic View
    can: a field with no value on any element is one it cannot supply, and it has
    to be named in `unavailable`, which has to say no more than that."""

    catalog = LocalSemanticViewLayer(source(tmp_path, SHOP)).list_definitions()
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
    assert catalog.to_data()["unavailable"] == SEMANTIC_VIEW_CATALOG_GAPS


def test_the_reference_rendering_is_plain_semantic_view_yaml(tmp_path):
    """The seed is what an author would write: it reads with no diagnostic."""
    names = [write(tmp_path, "views/reference.yaml", reference_semantic_view())]
    loaded = load_files(tmp_path, names)

    assert loaded.diagnostics == [], loaded.notes()
