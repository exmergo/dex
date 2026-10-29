"""Reading SLayer models into the semantic catalog (#598).

What this release promises under `semantic.vendor: slayer`: each configured model
file is validated by SLayer's own `SlayerModel`, a stale or newer format is
refused before SLayer can migrate it, every broken file is refused with the file
and the element named, the catalog says what does not map instead of inventing
it, and reading never writes, moves or renames anything under the storage root.

Needs the real `[slayer]` extra, so it skips without it; the `tests` CI job
is where it runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import SLAYER_VERSION

if dependency.installed_version() != SLAYER_VERSION:
    pytest.skip(
        f"the [slayer] extra (SLayer {SLAYER_VERSION}) is not installed",
        allow_module_level=True,
    )

from exmergo_dex_core.cli import main
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.errors import ProjectError
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.backend import (
    resolve_semantic_layer,
)
from exmergo_dex_core.explore.semantic.conformance import (
    REFERENCE_LAYER,
    SemanticBackendContract,
    SemanticCatalogContract,
    declared_fields,
)
from exmergo_dex_core.explore.semantic.slayer import (
    SLAYER_CATALOG_GAPS,
    LocalSlayerLayer,
)
from exmergo_dex_core.maintain.commands import _semantic_layer
from exmergo_dex_core.semantic_source import (
    SemanticCatalogSource,
    SemanticSnapshotSource,
    SemanticSourceContext,
)
from exmergo_dex_core.semantic_source_conformance import (
    SemanticCatalogSourceContract,
    SemanticDeclarationContract,
    SemanticFingerprintContract,
    SemanticSourceFactoryContract,
)
from exmergo_dex_core.slayer.catalog import dimension_token
from exmergo_dex_core.slayer.loader import FORMAT_VERSION
from exmergo_dex_core.slayer.project import SlayerSemanticLayer

BASE = "semantics/slayer"


def model(name: str, *, data_source: str = "wh", **fields: Any) -> dict[str, Any]:
    """A current-format model, on a fully qualified DuckDB relation by default."""

    return {
        "version": FORMAT_VERSION,
        "name": name,
        "sql_table": f"demo.main.{name}",
        "data_source": data_source,
        **fields,
    }


def write(root: Path, *models: dict[str, Any], raw: dict[str, str] | None = None):
    """Write each model where SLayer keeps it, and return the configured names."""

    names: list[str] = []
    for entry in models:
        name = f"models/{entry.get('data_source') or 'wh'}/{entry['name']}.yaml"
        _put(root, name, yaml.safe_dump(entry, sort_keys=False))
        names.append(name)
    for name, text in (raw or {}).items():
        _put(root, name, text)
        names.append(name)
    return names


def _put(root: Path, name: str, text: str) -> None:
    path = root / BASE / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def context(root: Path, names: list[str]) -> SemanticSourceContext:
    return SemanticSourceContext(
        repo_root=str(root),
        connector="duckdb",
        options={"base_dir": BASE, "models": names},
    )


def source(root: Path, *models: dict[str, Any], **kwargs) -> SlayerSemanticLayer:
    return SlayerSemanticLayer.from_context(
        context(root, write(root, *models, **kwargs))
    )


# A layer that answers every field SLayer can answer: two models joined, a
# label and a description wherever SLayer has a place for one, a time
# dimension, a computed column, a model filter, and a saved measure on each.
ORDERS = model(
    "orders",
    description="One row per order.",
    default_time_dimension="ordered_at",
    filters=["status <> 'test'"],
    columns=[
        {"name": "order_id", "type": "INT", "primary_key": True},
        {"name": "customer_id", "type": "INT"},
        {"name": "amount", "type": "DOUBLE", "label": "Amount"},
        {"name": "ordered_at", "type": "TIMESTAMP", "description": "Placed at."},
        {"name": "status", "sampled_values": ["SECRET-A", "SECRET-B"]},
        {"name": "amount_cents", "type": "DOUBLE", "sql": "amount * 100"},
    ],
    measures=[
        {
            "name": "revenue",
            "formula": "sum(amount)",
            "label": "Revenue",
            "description": "Gross revenue.",
        },
        {"name": "aov", "formula": "sum(amount) / count(*)"},
    ],
    joins=[
        {
            "target_model": "customers",
            "join_pairs": [["customer_id", "id"]],
            "cardinality": "many_to_one",
        }
    ],
)
CUSTOMERS = model(
    "customers",
    columns=[
        {"name": "id", "type": "INT", "primary_key": True, "sql": "customer_key"},
        {"name": "region", "label": "Region"},
        {"name": "signed_up_at", "type": "DATE"},
    ],
    measures=[{"name": "customer_count", "formula": "count(*)"}],
)


# --- the shipped source contracts ---------------------------------------------------


class TestSlayerSemanticSource(
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
        return SlayerSemanticLayer.from_context(context)

    def empty_source_context(self) -> SemanticSourceContext:
        """One model naming its relation and declaring nothing about it."""

        return context(self.root, write(self.root, model("thing")))

    def absent_document_context(self) -> SemanticSourceContext:
        return context(self.root, ["models/wh/absent.yaml"])

    def make_semantic_source(self):
        return self.build_source(self.empty_source_context())

    def an_unreadable_semantic_source(self):
        return SlayerSemanticLayer.from_context(
            context(
                self.root,
                write(self.root, raw={"models/wh/broken.yaml": "name: [x,\n"}),
            )
        )

    def a_source_declaring_a_unique_key(self):
        declared = model("orders", columns=[{"name": "order_id", "primary_key": True}])
        return source(self.root, declared), "orders", "order_id"

    def a_source_declaring_a_join(self):
        orders = model(
            "orders",
            columns=[{"name": "customer_id"}],
            joins=[
                {
                    "target_model": "customers",
                    "join_pairs": [["customer_id", "customer_id"]],
                }
            ],
        )
        customers = model("customers", columns=[{"name": "customer_id"}])
        return (
            source(self.root, orders, customers),
            "orders",
            "customer_id",
            "customers",
            "customer_id",
        )

    def a_source_declaring_a_composite_key(self):
        declared = model(
            "line_items",
            columns=[
                {"name": "order_id", "primary_key": True},
                {"name": "line_no", "primary_key": True},
                {"name": "region", "primary_key": True},
                {"name": "sku"},
            ],
        )
        return (
            source(self.root, declared),
            "line_items",
            ("order_id", "line_no", "region"),
        )

    def a_source_declaring_a_join_with_differently_named_sides(self):
        orders = model(
            "orders",
            columns=[{"name": "buyer"}],
            joins=[{"target_model": "customers", "join_pairs": [["buyer", "id"]]}],
        )
        customers = model("customers", columns=[{"name": "id"}])
        return (
            source(self.root, orders, customers),
            "orders",
            "buyer",
            "customers",
            "id",
        )

    def a_source_declaring_a_semantic_model(self):
        declared = model(
            "orders",
            columns=[
                {"name": "order_id", "type": "INT"},
                {"name": "status"},
                {"name": "ordered_at", "type": "TIMESTAMP"},
                {"name": "amount_cents", "type": "DOUBLE", "sql": "amount * 100"},
                {"name": "buyer", "sql": "customer_id"},
            ],
            measures=[{"name": "revenue", "formula": "sum(amount_cents)"}],
        )
        dimensions = {
            "order_id": "order_id",
            "status": "status",
            "ordered_at": "ordered_at",
            "amount_cents": None,
            "buyer": "customer_id",
        }
        # SLayer declares no catalog measures (a saved measure is a formula,
        # listed as a metric), so there is no measure-to-column map to keep.
        return source(self.root, declared), "orders", dimensions, {}


class TestSlayerBackend(SemanticBackendContract):
    """The `explore semantic` layer, held to the backend contract."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def make_backend(self):
        return LocalSlayerLayer(source(self.root, ORDERS, CUSTOMERS))


# --- the reference layer, seeded as SLayer model files ------------------------------


def reference_slayer_models() -> list[dict[str, Any]]:
    """`REFERENCE_LAYER` as SLayer model files, derived rather than hand-copied
    so a wider reference layer is a wider seed.

    The mapping is SLayer's native one, not a lossy one chosen to pass:

    - an entity is its key column (primary on the model it keys), and a shared
      entity is a join from the model holding it as foreign to the one keying it;
    - a dimension is a column;
    - a measure over a bare column aggregates that column, and a measure over an
      expression aggregates a computed column carrying it;
    - a simple metric is a saved measure on its measure's model, and a filtered
      one aggregates a filtered column, which is where SLayer puts a predicate
      that applies to one metric rather than to every metric on the model;
    - a ratio is a saved measure dividing its two sides by their saved names.
    """

    declared = REFERENCE_LAYER["semantic_models"]
    owner = {m["name"]: model for model in declared for m in model["measures"]}
    by_measure = {m["name"]: m for model in declared for m in model["measures"]}
    keyed = {
        entity["name"]: (model["name"], entity["expr"])
        for model in declared
        for entity in model["entities"]
        if entity["type"] == "primary"
    }
    simple = {m["name"]: m for m in REFERENCE_LAYER["metrics"] if m.get("measure")}

    seeded: dict[str, dict[str, Any]] = {}
    for entry in declared:
        columns: dict[str, dict[str, Any]] = {}
        joins = []
        for entity in entry["entities"]:
            columns[entity["expr"]] = {
                "name": entity["expr"],
                "type": "INT",
                "label": entity["label"],
                "description": entity["description"],
                **({"primary_key": True} if entity["type"] == "primary" else {}),
            }
            if entity["type"] == "foreign":
                target, key = keyed[entity["name"]]
                joins.append(
                    {
                        "target_model": target,
                        "join_pairs": [[entity["expr"], key]],
                        "cardinality": "many_to_one",
                    }
                )
        for dimension in entry["dimensions"]:
            columns[dimension["name"]] = {
                "name": dimension["name"],
                "type": "TIMESTAMP" if dimension["type"] == "time" else "TEXT",
                "label": dimension["label"],
                "description": dimension["description"],
            }
        seeded[entry["name"]] = model(
            entry["name"],
            sql_table=entry["relation"],
            description=entry["description"],
            default_time_dimension=entry["agg_time_dimension"],
            columns=columns,
            measures=[],
            joins=joins,
        )

    def aggregated(measure: dict[str, Any], columns: dict, where: str | None) -> str:
        """The column a measure aggregates, added to the model if it has to be."""

        expr = measure["expr"]
        if expr.isidentifier() and where is None:
            columns.setdefault(expr, {"name": expr, "type": "DOUBLE"})
            return expr
        name = f"{measure['name']}_where" if where else f"{measure['name']}_value"
        columns[name] = {
            "name": name,
            "type": "DOUBLE",
            "sql": expr,
            **({"filter": where} if where else {}),
        }
        return name

    for metric in REFERENCE_LAYER["metrics"]:
        if metric["type"] == "simple":
            measure = by_measure[metric["measure"]]
            home = owner[measure["name"]]["name"]
            where = _sql_predicate(metric.get("filter"))
            column = aggregated(measure, seeded[home]["columns"], where)
            formula = f"{measure['agg']}({column})"
        elif metric["type"] == "ratio":
            home = owner[simple[metric["numerator"]]["measure"]]["name"]
            formula = f"{metric['numerator']} / {metric['denominator']}"
        else:  # pragma: no cover - a reference shape this seed does not map yet
            raise AssertionError(f"no SLayer seeding for a {metric['type']} metric")
        seeded[home]["measures"].append(
            {
                "name": metric["name"],
                "formula": formula,
                "label": metric["label"],
                "description": metric["description"],
            }
        )

    for entry in seeded.values():
        entry["columns"] = list(entry["columns"].values())
    return list(seeded.values())


def _sql_predicate(template: str | None) -> str | None:
    """A MetricFlow filter template as the SQL predicate a SLayer column filter
    takes: `{{ Dimension('entity__column') }}` is the column."""

    if template is None:
        return None
    return re.sub(
        r"\{\{\s*Dimension\('[^']*__([^']+)'\)\s*\}\}", r"\1", template
    ).strip()


# The token SLayer reports for each reference metric: a saved measure is
# queried as `model.measure`, so the metric's name carries the model it is on.
REFERENCE_METRIC_TOKENS = {
    measure["name"]: dimension_token(entry["name"], measure["name"])
    for entry in reference_slayer_models()
    for measure in entry["measures"]
}


class TestSlayerReferenceLayer(SemanticCatalogContract):
    """The catalog contract's content assertions, against the reference layer
    seeded as SLayer model files. What SLayer cannot carry is a declared gap, so
    a failure here is a bug in the read, not a shape to skip."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def make_reference_backend(self):
        # Through the engine, because `values` screens a dimension through dex's
        # own governed spine. The config names no warehouse: the PII gate refuses
        # before any connection is asked for.
        return resolve_semantic_layer(
            DexEngine.from_repo(_repo(self.root, *reference_slayer_models()))
        )

    def reported_name(self, kind: str, name: str) -> str:
        return REFERENCE_METRIC_TOKENS[name] if kind == "metrics" else name


# --- the declared gaps --------------------------------------------------------------


def test_every_catalog_field_is_answered_or_declared_a_gap(tmp_path):
    """The catalog suite's silence rule, against a layer that answers everything
    SLayer can: a field with no value on any element is a field SLayer cannot
    supply, and it has to be named in `unavailable`."""

    catalog = LocalSlayerLayer(source(tmp_path, ORDERS, CUSTOMERS)).list_definitions()
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
    assert catalog.to_data()["unavailable"] == SLAYER_CATALOG_GAPS


def test_a_filtered_column_is_the_filter_of_a_metric_reading_only_it(tmp_path):
    """SLayer has no per-measure filter: a predicate meant for one metric sits on
    the column it aggregates. A ratio of that column to an unfiltered one is
    narrowed in one part only, so the predicate is not the ratio's."""

    tiers = model(
        "users",
        filters=["status <> 'test'"],
        columns=[
            {"name": "user_id", "type": "INT"},
            {"name": "tier"},
            {"name": "paid", "type": "INT", "sql": "1", "filter": "tier <> 'free'"},
        ],
        measures=[
            {"name": "paying", "formula": "sum(paid)"},
            {"name": "everyone", "formula": "count(user_id)"},
            {"name": "share", "formula": "paying / everyone"},
            {"name": "doubled", "formula": "sum(paid) + sum(paid)"},
        ],
    )
    metrics = {m.name: m for m in source(tmp_path, tiers).semantic_catalog().metrics}

    assert metrics["users.paying"].filter == "(status <> 'test') AND (tier <> 'free')"
    assert metrics["users.doubled"].filter == metrics["users.paying"].filter
    assert metrics["users.everyone"].filter == "status <> 'test'"
    assert metrics["users.share"].filter == "status <> 'test'"


def test_an_unread_formula_says_its_column_filter_is_unknown(tmp_path, monkeypatch):
    from exmergo_dex_core.slayer import loader

    tiers = model(
        "users",
        columns=[{"name": "paid", "type": "INT", "filter": "tier <> 'free'"}],
        measures=[{"name": "paying", "formula": "sum(paid)"}],
    )
    monkeypatch.setattr(loader, "find_spec", lambda name: None)
    catalog = source(tmp_path, tiers).semantic_catalog()

    (metric,) = catalog.metrics
    assert metric.filter is None
    assert any("'paying' could not be read" in note for note in catalog.notes)


def test_a_saved_measure_is_a_formula_metric_and_never_a_measure(tmp_path):
    catalog = source(tmp_path, ORDERS, CUSTOMERS).semantic_catalog()
    metrics = {m.name: m for m in catalog.metrics}

    assert catalog.measures == []
    assert catalog.entities == []
    revenue = metrics["orders.revenue"]
    assert revenue.type == "formula"
    assert revenue.composition.expr == "sum(amount)"
    assert revenue.semantic_models == ["orders"]
    assert revenue.filter == "status <> 'test'"
    assert revenue.time_axis == ["ordered_at"]
    assert "orders.status" in revenue.dimensions
    assert "customers.region" not in revenue.dimensions
    assert metrics["customers.customer_count"].time_axis == ["signed_up_at"]


def test_a_column_maps_to_its_physical_column_and_an_expression_to_none(tmp_path):
    catalog = source(tmp_path, ORDERS, CUSTOMERS).semantic_catalog()
    rows = {d.name: d for d in catalog.dimensions}

    assert rows["customers.id"].column == "customer_key"
    assert rows["orders.amount_cents"].column is None
    assert rows["orders.ordered_at"].type == "time"
    assert rows["orders.status"].queryable_granularities == []
    assert catalog.physical_columns["customers.id"] == (
        "demo.main.customers",
        "customer_key",
    )
    assert "orders.amount_cents" not in catalog.physical_columns
    assert any("amount_cents is computed" in note for note in catalog.notes)


def test_a_model_on_sql_or_a_partial_name_carries_no_relation(tmp_path):
    on_sql = model("recent", sql="select * from orders", sql_table=None)
    partial = model("partial", sql_table="main.partial")
    catalog = source(tmp_path, on_sql, partial).semantic_catalog()

    assert {m.name: m.relation for m in catalog.semantic_models} == {
        "recent": None,
        "partial": None,
    }
    assert any("backed by SQL" in note for note in catalog.notes)
    assert any("'main.partial'" in note for note in catalog.notes)


def test_cached_warehouse_values_never_leave_the_file(tmp_path):
    """`sampled_values` are values SLayer cached from the warehouse, and values
    reach agent context only through the query firewall."""

    layer = source(tmp_path, ORDERS, CUSTOMERS)
    payload = LocalSlayerLayer(layer).list_definitions().to_data()

    for read in (
        payload,
        layer.declared_definitions().model_dump(),
        layer.semantic_layer().model_dump(),
    ):
        assert "SECRET" not in json.dumps(read, default=str)


# --- the declaration channel --------------------------------------------------------


def test_joins_and_keys_arrive_as_physical_declarations(tmp_path):
    defs = source(tmp_path, ORDERS, CUSTOMERS).declared_definitions()

    (relationship,) = defs.declared_relationships
    assert (relationship.model, relationship.to_model) == ("orders", "customers")
    assert relationship.column_pairs == [("customer_id", "customer_key")]
    assert relationship.relation == "demo.main.orders"
    (fk,) = defs.foreign_keys
    assert (fk.column, fk.to_column) == ("customer_id", "customer_key")
    assert {(k.model, k.column) for k in defs.declared_keys} == {
        ("orders", "order_id"),
        ("customers", "customer_key"),
    }


def test_snowflake_columns_keys_and_joins_are_folded_the_way_snowflake_resolves_them(
    tmp_path,
):
    """An unquoted column is the column the warehouse folded it to: reading it
    as written made every SLayer column look gone from a Snowflake table (#923)."""

    orders = model(
        "orders",
        columns=[
            {"name": "order_id", "type": "INT", "primary_key": True},
            {"name": "customer_id", "type": "INT"},
            {"name": "nick", "type": "TEXT", "sql": '"Nick"'},
        ],
        joins=[
            {
                "target_model": "customers",
                "join_pairs": [["customer_id", "customer_key"]],
                "cardinality": "many_to_one",
            }
        ],
    )
    customers = model(
        "customers",
        columns=[{"name": "customer_key", "type": "INT", "primary_key": True}],
    )
    layer = SlayerSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(tmp_path),
            connector="snowflake",
            options={
                "base_dir": BASE,
                "models": list(write(tmp_path, orders, customers)),
            },
        )
    )

    rows = {d.name: d for d in layer.semantic_catalog().dimensions}
    assert rows["orders.order_id"].column == "ORDER_ID"
    assert rows["orders.customer_id"].column == "CUSTOMER_ID"
    assert rows["orders.nick"].column is None

    defs = layer.declared_definitions()
    (relationship,) = defs.declared_relationships
    assert relationship.column_pairs == [("CUSTOMER_ID", "CUSTOMER_KEY")]
    assert {(k.model, k.column) for k in defs.declared_keys} == {
        ("orders", "ORDER_ID"),
        ("customers", "CUSTOMER_KEY"),
    }


def test_a_one_to_many_join_is_written_child_to_parent(tmp_path):
    customers = model(
        "customers",
        columns=[{"name": "id"}],
        joins=[
            {
                "target_model": "orders",
                "join_pairs": [["id", "customer_id"]],
                "cardinality": "one_to_many",
            }
        ],
    )
    orders = model("orders", columns=[{"name": "customer_id"}])
    (relationship,) = (
        source(tmp_path, customers, orders)
        .declared_definitions()
        .declared_relationships
    )

    assert (relationship.model, relationship.to_model) == ("orders", "customers")
    assert relationship.column_pairs == [("customer_id", "id")]


def test_a_many_to_many_join_is_a_relationship_and_not_a_foreign_key(tmp_path):
    left = model(
        "left_side",
        columns=[{"name": "k"}],
        joins=[
            {
                "target_model": "right_side",
                "join_pairs": [["k", "k"]],
                "cardinality": "many_to_many",
            }
        ],
    )
    right = model("right_side", columns=[{"name": "k"}])
    defs = source(tmp_path, left, right).declared_definitions()

    assert len(defs.declared_relationships) == 1
    assert defs.foreign_keys == []


def test_a_composite_join_keeps_every_pair_in_order(tmp_path):
    lines = model(
        "lines",
        columns=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        joins=[
            {
                "target_model": "parent",
                "join_pairs": [["a", "x"], ["b", "y"], ["c", "z"]],
            }
        ],
    )
    parent = model("parent", columns=[{"name": "x"}, {"name": "y"}, {"name": "z"}])
    defs = source(tmp_path, lines, parent).declared_definitions()

    (relationship,) = defs.declared_relationships
    assert relationship.column_pairs == [("a", "x"), ("b", "y"), ("c", "z")]
    assert defs.foreign_keys == []


# --- refusals -----------------------------------------------------------------------


def _refused(layer: SlayerSemanticLayer) -> str:
    with pytest.raises(ProjectError) as raised:
        layer.semantic_catalog()
    return str(raised.value)


@pytest.mark.parametrize(
    ("broken", "file", "element"),
    [
        (model("bad.name"), "models/wh/bad.name.yaml", "name"),
        (model("orphan", data_source=""), "models/wh/orphan.yaml", "(root)"),
        (
            model(
                "orders",
                columns=[{"name": "ok"}, {"name": "a.b"}],
            ),
            "models/wh/orders.yaml",
            "columns['a.b'].name",
        ),
        (
            model(
                "orders",
                columns=[{"name": "k"}],
                joins=[{"target_model": "nowhere", "join_pairs": [["k", "k"]]}],
            ),
            "models/wh/orders.yaml",
            "joins[0].target_model",
        ),
    ],
    ids=["invalid-name", "empty-data-source", "invalid-column", "unknown-join"],
)
def test_a_broken_model_is_refused_naming_the_file_and_element(
    tmp_path, broken, file, element
):
    message = _refused(source(tmp_path, broken))

    assert f"{file}: {element}:" in message


def test_a_duplicate_name_refuses_both_files(tmp_path):
    first = model("orders", data_source="wh")
    second = model("orders", data_source="lake")
    message = _refused(source(tmp_path, first, second))

    assert "models/wh/orders.yaml: name: [duplicate_name]" in message
    assert "models/lake/orders.yaml: name: [duplicate_name]" in message


def test_a_join_to_a_missing_target_column_is_refused(tmp_path):
    orders = model(
        "orders",
        columns=[{"name": "customer_id"}],
        joins=[{"target_model": "customers", "join_pairs": [["customer_id", "nope"]]}],
    )
    customers = model("customers", columns=[{"name": "id"}])
    catalog = source(tmp_path, orders, customers).semantic_catalog()

    assert [m.name for m in catalog.semantic_models] == ["customers"]
    assert any("joins[0].join_pairs: [join_key]" in n for n in catalog.notes)


def test_a_join_to_an_unreadable_model_says_so_rather_than_unknown(tmp_path):
    orders = model(
        "orders",
        columns=[{"name": "k"}],
        joins=[{"target_model": "customers", "join_pairs": [["k", "k"]]}],
    )
    stale = {**model("customers", columns=[{"name": "k"}]), "version": 3}
    message = _refused(source(tmp_path, orders, stale))

    assert "configured but could not be read" in message


@pytest.mark.parametrize(
    ("version", "says"),
    [
        (FORMAT_VERSION - 1, "is stale"),
        (FORMAT_VERSION + 1, "newer than this dex reads"),
        (None, "carries no `version`"),
        ("fifteen", "not a format version number"),
    ],
)
def test_a_format_other_than_the_pinned_one_is_refused(tmp_path, version, says):
    entry = model("orders")
    if version is None:
        entry.pop("version")
    else:
        entry["version"] = version
    message = _refused(source(tmp_path, entry))

    assert "models/wh/orders.yaml: version: [format_version]" in message
    assert says in message
    assert str(FORMAT_VERSION) in message


def test_a_model_filed_under_another_name_is_refused(tmp_path):
    names = write(
        tmp_path, raw={"models/wh/orders.yaml": yaml.safe_dump(model("sales"))}
    )
    message = _refused(SlayerSemanticLayer.from_context(context(tmp_path, names)))

    assert "[path]" in message and "'sales'" in message


def test_a_symlink_out_of_the_storage_root_is_refused(tmp_path):
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(yaml.safe_dump(model("orders")), encoding="utf-8")
    link = tmp_path / BASE / "models" / "wh" / "orders.yaml"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    message = _refused(
        SlayerSemanticLayer.from_context(context(tmp_path, ["models/wh/orders.yaml"]))
    )

    assert "[confinement]" in message


def test_one_broken_file_leaves_the_rest_readable_with_a_note(tmp_path):
    layer = source(tmp_path, CUSTOMERS, raw={"models/wh/broken.yaml": "{"})
    catalog = layer.semantic_catalog()

    assert [m.name for m in catalog.semantic_models] == ["customers"]
    assert any("models/wh/broken.yaml" in note for note in catalog.notes)
    assert layer.declared_definitions().notes


def test_an_unknown_key_is_read_past_with_a_note(tmp_path):
    """SLayer reads a file carrying a key it does not declare, so dex does too,
    and says the key does nothing (#620)."""

    layer = source(tmp_path, {**CUSTOMERS, "entities": [{"name": "customer"}]})
    catalog = layer.semantic_catalog()

    assert [m.name for m in catalog.semantic_models] == ["customers"]
    assert any(
        "models/wh/customers.yaml: entities: [unknown_key]" in note
        for note in catalog.notes
    )


def test_the_declaration_and_snapshot_channels_never_raise(tmp_path):
    layer = source(tmp_path, raw={"models/wh/broken.yaml": "{"})

    assert layer.declared_definitions().declared_keys == []
    assert layer.semantic_layer().semantic_models == []


# --- reading writes nothing ---------------------------------------------------------


def _tree(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_reading_never_writes_moves_or_renames_under_the_storage_root(tmp_path):
    """Including the files SLayer's own storage would migrate or move on open:
    a stale model, legacy top-level YAML, and a datasource."""

    stale = {**model("legacy", columns=[{"name": "k"}]), "version": 9}
    names = write(tmp_path, ORDERS, CUSTOMERS, stale)
    _put(tmp_path, "datasources/wh.yaml", "name: wh\npassword: hunter2\n")
    _put(tmp_path, "embeddings.yaml", "{}\n")
    _put(tmp_path, "memories.yaml", "[]\n")
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: slayer\n  slayer:\n"
        f"    base_dir: {BASE}\n    models:\n"
        + "".join(f"      - {name}\n" for name in names),
        encoding="utf-8",
    )
    before = _tree(tmp_path / BASE)

    layer = SlayerSemanticLayer.from_context(context(tmp_path, names))
    layer.semantic_catalog()
    layer.declared_definitions()
    layer.semantic_layer()
    assert main(["--repo-root", str(tmp_path), "explore", "semantic", "list"]) == 0

    assert _tree(tmp_path / BASE) == before


# --- the fingerprint ----------------------------------------------------------------


def test_a_re_profile_is_not_a_definition_change(tmp_path):
    before = source(tmp_path, ORDERS, CUSTOMERS).semantic_layer()
    resampled = json.loads(json.dumps(ORDERS))
    resampled["columns"][4]["sampled_values"] = ["OTHER"]
    after = source(tmp_path, resampled, CUSTOMERS).semantic_layer()

    assert before == after


def test_a_rewritten_formula_changes_its_metric_and_its_model(tmp_path):
    before = source(tmp_path, ORDERS, CUSTOMERS).semantic_layer()
    rewritten = json.loads(json.dumps(ORDERS))
    rewritten["measures"][0]["formula"] = "sum(amount_cents)"
    after = source(tmp_path, rewritten, CUSTOMERS).semantic_layer()

    def hashes(layer):
        return {m.name: m.content_sha256 for m in layer.metrics}

    assert hashes(before)["orders.revenue"] != hashes(after)["orders.revenue"]
    assert hashes(before)["orders.aov"] == hashes(after)["orders.aov"]
    assert after.relationships_and_keys_captured
    (relationship,) = after.relationships
    assert relationship.column_pairs == [("customer_id", "customer_key")]


# --- through the engine, with no warehouse ------------------------------------------


def _repo(root: Path, *models: dict[str, Any]) -> Path:
    names = write(root, *models)
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: slayer\n  slayer:\n"
        f"    base_dir: {BASE}\n    models:\n"
        + "".join(f"      - {name}\n" for name in names),
        encoding="utf-8",
    )
    return root


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_explore_semantic_list_reads_slayer_with_no_warehouse(tmp_path, capsys):
    """The config names DuckDB and no path, so any connection would refuse."""

    root = _repo(tmp_path, ORDERS, CUSTOMERS)

    rc, envelope = _run(capsys, root, "explore", "semantic", "list")

    assert rc == 0, envelope["errors"]
    data = envelope["data"]
    assert (data["vendor"], data["execution"]) == ("slayer", "dex")
    assert {m["name"] for m in data["metrics"]} == {
        "orders.revenue",
        "orders.aov",
        "customers.customer_count",
    }
    assert data["unavailable"] == SLAYER_CATALOG_GAPS


def test_explore_semantic_list_narrows_by_metric_and_dimension(tmp_path, capsys):
    root = _repo(tmp_path, ORDERS, CUSTOMERS)

    _, scoped = _run(
        capsys, root, "explore", "semantic", "list", "--metric", "orders.revenue"
    )
    _, grouped = _run(
        capsys,
        root,
        "explore",
        "semantic",
        "list",
        "--for-dimension",
        "customers.region",
    )

    assert [m["name"] for m in scoped["data"]["metrics"]] == ["orders.revenue"]
    assert [m["name"] for m in grouped["data"]["metrics"]] == [
        "customers.customer_count"
    ]


def test_a_slayer_values_reaches_dex_s_own_connection(tmp_path, capsys):
    """It used to be refused for want of a runtime. Now it is resolved and
    rendered, and what stops it is dex's connection, which this config names no
    warehouse for: SLayer opened nothing of its own."""

    root = _repo(tmp_path, ORDERS, CUSTOMERS)

    rc, envelope = _run(capsys, root, "explore", "semantic", "values", "orders.status")

    assert rc != 0
    assert envelope["errors"] == [
        "no DuckDB path: pass --path or set duckdb.path in .dex/config.yml"
    ]


def test_a_slayer_query_reaches_dex_s_own_connection(tmp_path, capsys):
    """It used to be refused for want of a runtime. Now it is translated and
    rendered, and the only thing between it and a result is dex's connection,
    which this config names no warehouse for: SLayer opened nothing of its own."""

    root = _repo(tmp_path, ORDERS, CUSTOMERS)

    rc, envelope = _run(capsys, root, "explore", "semantic", "query", "orders.revenue")

    assert rc != 0
    assert envelope["errors"] == [
        "no DuckDB path: pass --path or set duckdb.path in .dex/config.yml"
    ]


def test_the_engine_resolves_the_slayer_layer_and_its_declarations(tmp_path):
    engine = DexEngine.from_repo(_repo(tmp_path, ORDERS, CUSTOMERS))

    layer = resolve_semantic_layer(engine, local=True)
    keys, composite = layer.declared_keys()

    assert isinstance(layer, LocalSlayerLayer)
    assert {k.column for k in keys} == {"order_id", "customer_key"}
    assert composite == []
    assert len(layer.declared_relationships()) == 1
    with pytest.raises(SemanticBackendError, match="no hosted"):
        resolve_semantic_layer(engine, api=True)


def test_maintain_fingerprints_the_slayer_layer(tmp_path):
    engine = DexEngine.from_repo(_repo(tmp_path, ORDERS, CUSTOMERS))
    built = engine.semantic_catalog_source()

    assert isinstance(built, SemanticCatalogSource)
    assert isinstance(built, SemanticSnapshotSource)
    layer = _semantic_layer(engine, None)
    assert {m.name for m in layer.semantic_models} == {"orders", "customers"}


def test_a_model_symlinked_to_a_datasource_file_is_not_read(tmp_path):
    """Inside the storage root is not enough: a datasource file carries
    credentials, and a model path symlinked to one must not reach the parser."""

    secret = tmp_path / BASE / "datasources" / "wh.yaml"
    secret.parent.mkdir(parents=True)
    secret.write_text("password: hunter2\n", encoding="utf-8")
    link = tmp_path / BASE / "models" / "wh" / "orders.yaml"
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)

    loaded = SlayerSemanticLayer.from_context(
        context(tmp_path, ["models/wh/orders.yaml"])
    )._load()

    assert loaded.models == []
    (diagnostic,) = loaded.diagnostics
    assert diagnostic.rule == "confinement"
    assert "hunter2" not in diagnostic.render()
