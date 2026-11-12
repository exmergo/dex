"""A logical table derived from a declared mart, and declarations on
`semantic snowflake` (#1292).

The Snowflake implementation of the derivation hook (#927): a dbt mart under
`transform.marts_paths` whose YAML declares it becomes a logical table, spliced
into the configured Semantic View holding the tables its foreign keys reference
and included as a plan beside the mart's, or derived as a view of its own,
proposed where its path is not configured. A `declarations` payload on
`semantic snowflake plan` annotates a table already in the layer, splicing only
what changes. Nothing here opens Snowflake or a warehouse, and every table
written reads back through the #1291 reader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlglot")

from transform.test_semantic_derivation import MART, MART_SQL, _fake_parse, _payload

from exmergo_dex_core.semantic_source import (
    DeclaredReference,
    SemanticDeclaration,
    SemanticDerivationRequest,
    SemanticDerivationSource,
    SemanticSourceContext,
)
from exmergo_dex_core.semantic_source_conformance import SemanticDerivationContract
from exmergo_dex_core.semantic_views.catalog import resolution, semantic_catalog
from exmergo_dex_core.semantic_views.loader import (
    dump_yaml,
    read_yaml,
    validate_view_contents,
)
from exmergo_dex_core.semantic_views.project import SemanticViewSemanticLayer

from .conftest import sv, table
from .test_authoring import put, read, run

SHOP_FILE = "views/shop.yaml"
OWN_FILE = "views/fct_orders.yaml"
SQL_PATH = "models/marts/fct_orders.sql"

POPULATED = {
    **MART,
    "population": {
        "rule": "Paid and shipped orders",
        "filters": [{"column": "status", "includes": ["paid", "shipped"]}],
    },
    "assumptions": [{"decision": "Refunds are separate", "evidence": "request"}],
    "columns": {
        **MART["columns"],
        "order_total": {
            **MART["columns"]["order_total"],
            "unit": "currency:EUR",
            "description": "Order value.",
        },
        "status": {**MART["columns"]["status"], "null_rule": "never"},
    },
}

#: The hand-written layer the mart is spliced into: customers, keyed on `id`.
SHOP_TEXT = (
    "# The shop; this comment stays.\n"
    "name: shop\n"
    "tables:\n"
    "  - name: customers\n"
    "    base_table: {database: demo, schema: main, table: stg_customers}\n"
    "    primary_key: {columns: [id]}  # the customer key\n"
    "    dimensions:\n"
    "      - name: region\n"
    "        expr: region\n"
    "    metrics:\n"
    "      - name: customer_count\n"
    "        expr: COUNT(id)\n"
)


@pytest.fixture
def no_warehouse(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", refuse)


@pytest.fixture
def project(dbt_project_dir: Path, tmp_path: Path, monkeypatch) -> Path:
    _fake_parse(monkeypatch)
    return tmp_path


def _configure(root: Path, files: list[str]) -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: snowflake\n"
        "  snowflake:\n    files:\n" + "".join(f"      - {n}\n" for n in files),
        encoding="utf-8",
    )


def _source(root: Path, files: list[str]) -> SemanticViewSemanticLayer:
    return SemanticViewSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root), connector="duckdb", options={"files": files}
        )
    )


def _reference(**overrides) -> DeclaredReference:
    fields = {
        "column": "customer_id",
        "parent_model": "stg_customers",
        "parent_column": "id",
        "parent_identifier": "stg_customers",
        **overrides,
    }
    return DeclaredReference(**fields)


def _request(
    documents: dict[str, str],
    declared: dict | None = None,
    *,
    relation: str | None = "demo.main.fct_orders",
    references: tuple[DeclaredReference, ...] | None = None,
    identifiers: dict[str, str] | None = None,
) -> SemanticDerivationRequest:
    body = {k: v for k, v in (declared or POPULATED).items() if k != "model"}
    return SemanticDerivationRequest(
        mode="derive",
        declarations=(
            SemanticDeclaration(
                model="fct_orders",
                declared=body,
                column_types={"ordered_at": ("date", None)},
                relation=relation,
                identifier="fct_orders",
                references=(_reference(),) if references is None else references,
            ),
        ),
        documents=documents,
        project_identifiers=identifiers
        if identifiers is not None
        else {"stg_customers": "stg_customers", "fct_orders": "fct_orders"},
    )


def _derive(root: Path, files: list[str] | None = None, **kwargs) -> Any:
    put(root, SHOP_FILE, SHOP_TEXT)
    files = files or [SHOP_FILE]
    documents = {f: read(root, f) for f in files if (root / f).exists()}
    return _source(root, files).derive_semantic(_request(documents, **kwargs))


def _plan(root: Path, capsys, declaration: dict | None = None) -> tuple[int, dict]:
    body = {
        "edits": [{"path": SQL_PATH, "kind": "model_sql", "content": MART_SQL}],
        "declarations": [declaration or POPULATED],
    }
    return run(
        capsys,
        root,
        "transform",
        "plan",
        "orders mart",
        "--edits-file",
        _payload(root, body),
    )


def _stored_plans(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / ".dex" / "plans").glob("*.json"))
    ]


def _table(text: str, name: str) -> dict[str, Any]:
    return next(t for t in read_yaml(text)["tables"] if t["name"] == name)


def test_the_layer_implements_the_derivation_hook(tmp_path: Path):
    assert isinstance(
        SemanticViewSemanticLayer(tmp_path, [SHOP_FILE]), SemanticDerivationSource
    )


# --- derive: a declared mart becomes a logical table -------------------------------


def test_a_declared_mart_is_spliced_beside_its_parent(tmp_path: Path, no_warehouse):
    result = _derive(tmp_path)

    definition = result.definitions["fct_orders"]
    assert definition.status == "included", definition.reason
    assert definition.path == SHOP_FILE
    assert definition.name == "shop.fct_orders"
    assert set(definition.validation) == {"semantic_view", "catalog", "plan"}
    (edit,) = result.edits
    text = edit.new_content
    # Every hand-written byte is kept: the new table and relationship are appended.
    assert text.startswith(SHOP_TEXT)
    fct = _table(text, "fct_orders")
    assert fct["base_table"] == {
        "database": "demo",
        "schema": "main",
        "table": "fct_orders",
    }
    assert fct["primary_key"] == {"columns": ["order_id"]}
    assert fct["description"] == (
        "One row per order. Population: Paid and shipped orders; status is one of "
        "'paid' or 'shipped'."
    )
    assert [d["name"] for d in fct["dimensions"]] == [
        "order_id",
        "customer_id",
        "status",
    ]
    assert fct["dimensions"][2]["description"] == "Lifecycle status."
    assert fct["time_dimensions"] == [{"name": "ordered_at", "expr": "ordered_at"}]
    assert [f["name"] for f in fct["facts"]] == ["order_total", "balance"]
    metrics = {m["name"]: m for m in fct["metrics"]}
    assert metrics["order_total_sum"]["expr"] == "SUM(fct_orders.order_total)"
    # Snowflake reads the last value in the stated order: ascending with NULLs
    # first makes that the latest real date (descending would read the earliest,
    # and NULLs last would read an undated row).
    assert metrics["balance_sum"]["non_additive_dimensions"] == [
        {
            "table": "fct_orders",
            "dimension": "ordered_at",
            "sort_direction": "ascending",
            "null_order": "first",
        }
    ]
    assert "non_additive_dimensions" not in metrics["order_total_sum"]
    assert read_yaml(text)["relationships"] == [
        {
            "name": "fct_orders_to_customers",
            "left_table": "fct_orders",
            "right_table": "customers",
            "relationship_columns": [
                {"left_column": "customer_id", "right_column": "id"}
            ],
        }
    ]


def test_a_derived_table_round_trips_through_the_reader(tmp_path: Path):
    text = _derive(tmp_path).edits[0].new_content

    loaded = validate_view_contents({SHOP_FILE: text})
    assert loaded.errors == [], loaded.notes()
    assert not [d for d in loaded.diagnostics if d.rule != "file_name"], loaded.notes()
    catalog = semantic_catalog(loaded, resolution(loaded, "duckdb"))
    models = {m.name: m for m in catalog.semantic_models}
    assert models["shop.fct_orders"].relation == "demo.main.fct_orders"
    assert models["shop.fct_orders"].primary_entity == "shop.fct_orders"
    # The population sentence is withheld from the catalog.
    assert models["shop.fct_orders"].description == "One row per order."
    dimensions = {d.name for d in catalog.dimensions}
    assert {"shop.fct_orders.status", "shop.fct_orders.ordered_at"} <= dimensions
    metrics = {m.name for m in catalog.metrics}
    assert {"shop.fct_orders.order_total_sum", "shop.fct_orders.balance_sum"} <= metrics

    from exmergo_dex_core.semantic_views.catalog import declared_relationships

    view = loaded.view("shop")
    assert view is not None
    (declared,) = declared_relationships(loaded, resolution(loaded, "duckdb"))
    assert declared.cardinality == "many_to_one"


def test_a_composite_grain_is_a_composite_primary_key(tmp_path: Path):
    declared = {
        **POPULATED,
        "grain": ["order_id", "status"],
    }
    text = _derive(tmp_path, declared=declared).edits[0].new_content
    assert _table(text, "fct_orders")["primary_key"] == {
        "columns": ["order_id", "status"]
    }


def test_what_a_semantic_view_cannot_hold_is_named(tmp_path: Path):
    definition = _derive(tmp_path).definitions["fct_orders"]
    not_carried = definition.not_carried
    assert set(not_carried) == {
        "assumptions",
        "columns.order_total.unit",
        "columns.order_total.additivity",
        "columns.status.null_rule",
    }
    assert "custom_instructions" in not_carried["assumptions"]
    # Every declared field is accounted for, once.
    from exmergo_dex_core.semantic_declarations import declared_fields

    body = {k: v for k, v in POPULATED.items() if k != "model"}
    assert sorted([*definition.carried, *not_carried]) == sorted(declared_fields(body))
    assert "columns.balance.additivity" in definition.carried
    assert "columns.customer_id.references" in definition.carried


def test_a_semi_additive_measure_needs_a_time_dimension(tmp_path: Path):
    columns = {k: v for k, v in POPULATED["columns"].items() if k != "ordered_at"}
    definition = _derive(tmp_path, declared={**POPULATED, "columns": columns})
    fct = definition.definitions["fct_orders"]
    assert fct.status == "included", fct.reason
    metrics = {m["name"] for m in _table(fct.content, "fct_orders")["metrics"]}
    assert metrics == {"order_total_sum"}
    assert "no time dimension" in fct.not_carried["columns.balance.aggregation"]


def test_no_relationship_to_a_parent_not_keyed_on_the_column(tmp_path: Path):
    put(tmp_path, SHOP_FILE, SHOP_TEXT.replace("[id]}", "[region]}"))
    documents = {SHOP_FILE: read(tmp_path, SHOP_FILE)}
    result = _source(tmp_path, [SHOP_FILE]).derive_semantic(_request(documents))
    definition = result.definitions["fct_orders"]
    assert definition.status == "included", definition.reason
    assert "relationships" not in read_yaml(definition.content)
    assert (
        "primary key or a unique key"
        in definition.not_carried["columns.customer_id.references"]
    )


def test_a_mart_whose_relation_is_unknown_is_not_derived(tmp_path: Path):
    result = _derive(tmp_path, relation=None, identifiers={})
    definition = result.definitions["fct_orders"]
    assert definition.status == "failed"
    assert "database, schema and table" in definition.reason
    assert result.edits == []


def test_parents_in_no_configured_view_derive_a_view_of_its_own(tmp_path: Path):
    other = sv(
        "stock",
        table(
            "items",
            "demo.main.items",
            ["item_id"],
            dimensions=[{"name": "category", "expr": "category"}],
        ),
    )
    put(tmp_path, "views/stock.yaml", dump_yaml(other))
    result = _derive(
        tmp_path,
        files=["views/stock.yaml", OWN_FILE],
        references=(_reference(parent_model="nowhere", parent_identifier="nowhere"),),
    )
    definition = result.definitions["fct_orders"]
    assert definition.status == "included", definition.reason
    assert definition.path == OWN_FILE
    assert definition.name == "fct_orders.fct_orders"
    body = read_yaml(definition.content)
    assert body["name"] == "fct_orders"
    assert "relationships" not in body
    assert (
        "a view of its own" in definition.not_carried["columns.customer_id.references"]
    )


def test_an_unconfigured_path_is_proposed_with_the_command_that_adopts_it(
    tmp_path: Path,
):
    put(
        tmp_path,
        "views/stock.yaml",
        dump_yaml(
            sv(
                "stock",
                table(
                    "items",
                    "demo.main.items",
                    ["item_id"],
                    dimensions=[{"name": "category", "expr": "category"}],
                ),
            )
        ),
    )
    # Two configured files and no foreign key: nothing says which view it joins.
    result = _derive(tmp_path, files=[SHOP_FILE, "views/stock.yaml"], references=())
    definition = result.definitions["fct_orders"]
    assert definition.status == "proposed", definition.reason
    assert definition.path == OWN_FILE
    assert result.edits == []
    assert (
        f"dex config set semantic.snowflake.files '[{SHOP_FILE}, views/stock.yaml, "
        f"{OWN_FILE}]'"
    ) in (definition.next or "")
    assert result.proposal["definitions"]["edits"][0]["path"] == OWN_FILE


def test_a_mart_with_a_table_already_is_skipped(tmp_path: Path):
    put(
        tmp_path,
        SHOP_FILE,
        SHOP_TEXT + "  - name: orders\n"
        "    base_table: {database: demo, schema: main, table: fct_orders}\n"
        "    dimensions:\n"
        "      - name: status\n"
        "        expr: status\n",
    )
    documents = {SHOP_FILE: read(tmp_path, SHOP_FILE)}
    result = _source(tmp_path, [SHOP_FILE]).derive_semantic(_request(documents))
    definition = result.definitions["fct_orders"]
    assert definition.status == "skipped"
    assert "shop.orders" in definition.reason
    assert result.edits == []


def test_a_table_name_the_view_holds_fails(tmp_path: Path):
    put(
        tmp_path,
        SHOP_FILE,
        SHOP_TEXT + "  - name: fct_orders\n"
        "    base_table: {database: demo, schema: main, table: other}\n"
        "    dimensions:\n"
        "      - name: status\n"
        "        expr: status\n",
    )
    documents = {SHOP_FILE: read(tmp_path, SHOP_FILE)}
    definition = (
        _source(tmp_path, [SHOP_FILE])
        .derive_semantic(_request(documents))
        .definitions["fct_orders"]
    )
    assert definition.status == "failed"
    assert "already declared" in definition.reason


# --- the plan routes ---------------------------------------------------------------


def test_transform_plan_stores_the_table_as_a_companion_plan(
    project: Path, capsys, no_warehouse
):
    put(project, SHOP_FILE, SHOP_TEXT)
    _configure(project, [SHOP_FILE])

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope["errors"]
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["vendor"] == "snowflake"
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "included", proposal
    assert model["logical_table"] == "shop.fct_orders"
    assert proposal["plan_id"] in envelope["data"]["next"]
    content = model["content"]
    assert content.startswith(SHOP_TEXT)

    rc, applied = run(capsys, project, "transform", "apply", proposal["plan_id"])
    assert rc == 0, applied["errors"]
    assert read(project, SHOP_FILE) == content


def test_transform_plan_proposes_an_unconfigured_view(project: Path, capsys):
    put(project, SHOP_FILE, SHOP_TEXT)
    put(
        project,
        "views/stock.yaml",
        dump_yaml(
            sv(
                "stock",
                table(
                    "items",
                    "demo.main.items",
                    ["item_id"],
                    dimensions=[{"name": "category", "expr": "category"}],
                ),
            )
        ),
    )
    _configure(project, [SHOP_FILE, "views/stock.yaml"])
    # No foreign key and two views: the mart is a view of its own.
    columns = dict(POPULATED["columns"])
    columns["customer_id"] = {"role": "dimension"}
    rc, envelope = _plan(project, capsys, {**POPULATED, "columns": columns})
    assert rc == 0, envelope["errors"]
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["mode"] == "proposed", proposal
    assert "plan_id" not in proposal
    assert "semantic snowflake define" in proposal["next"]


# --- annotate: declarations on `semantic snowflake plan` ----------------------------

ANNOTATED = (
    "# the shop view; this comment stays\n"
    "name: shop\n"
    "tables:\n"
    "  - name: customers\n"
    "    base_table: {database: demo, schema: main, table: stg_customers}\n"
    "    primary_key: {columns: [id]}\n"
    "    dimensions:\n"
    "      - name: region\n"
    "        expr: region\n"
    "  - name: orders\n"
    "    base_table: {database: demo, schema: main, table: fct_orders}  # prod\n"
    "    primary_key: {columns: [order_id]}\n"
    "    dimensions:\n"
    "      - name: status\n"
    "        expr: status\n"
    "      # a hand-written note between members\n"
    "      - name: customer_id\n"
    "        expr: customer_id\n"
    "    facts:\n"
    "      - name: order_total\n"
    "        expr: order_total\n"
    "    metrics:\n"
    "      - name: order_total_sum\n"
    "        expr: SUM(orders.order_total)\n"
)


def _annotate(root: Path, capsys, declarations: list[dict]) -> tuple[int, dict]:
    return run(
        capsys,
        root,
        "semantic",
        "snowflake",
        "plan",
        "annotate",
        "--edits-file",
        _payload(root, {"declarations": declarations}),
    )


def test_annotate_splices_only_what_changes(tmp_path: Path, capsys, no_warehouse):
    put(tmp_path, SHOP_FILE, ANNOTATED)
    _configure(tmp_path, [SHOP_FILE])
    declaration = {
        "model": "orders",
        "description": "One row per order.",
        "population": {"rule": "Placed orders"},
        "grain": ["order_id"],
        "columns": {
            "status": {"role": "dimension", "description": "Lifecycle status."},
            "order_total": {
                "role": "measure",
                "aggregation": "sum",
                "unit": "currency:USD",
            },
            "quantity": {"role": "measure", "aggregation": "count"},
            "customer_id": {"role": "foreign_key", "references": "customers.id"},
        },
    }

    rc, envelope = _annotate(tmp_path, capsys, [declaration])

    assert rc == 0, envelope["errors"]
    (stored,) = _stored_plans(tmp_path)
    (edit,) = stored["edits"]
    text = edit["new_content"]
    # Every hand-written byte the declaration did not touch is kept.
    assert text.startswith(
        ANNOTATED.split("  - name: orders\n")[0] + "  - name: orders\n"
    )
    assert "table: fct_orders}  # prod\n" in text
    assert "      # a hand-written note between members\n" in text
    assert "      - name: customer_id\n        expr: customer_id\n" in text
    orders = _table(text, "orders")
    assert orders["description"] == "One row per order. Population: Placed orders."
    dims = {d["name"]: d for d in orders["dimensions"]}
    assert dims["status"]["description"] == "Lifecycle status."
    metrics = {m["name"]: m for m in orders["metrics"]}
    assert metrics["order_total_sum"]["expr"] == "SUM(orders.order_total)"
    assert metrics["quantity_count"]["expr"] == "COUNT(quantity)"
    assert read_yaml(text)["relationships"] == [
        {
            "name": "orders_to_customers",
            "left_table": "orders",
            "right_table": "customers",
            "relationship_columns": [
                {"left_column": "customer_id", "right_column": "id"}
            ],
        }
    ]
    notes = " ".join(envelope["data"]["notes"])
    assert "no unit or format key" in notes
    assert validate_view_contents({SHOP_FILE: text}).errors == []

    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )
    assert rc == 0, applied["errors"]
    assert read(tmp_path, SHOP_FILE) == text


def test_annotate_touches_only_the_annotated_lines(tmp_path: Path, capsys):
    put(tmp_path, SHOP_FILE, ANNOTATED)
    _configure(tmp_path, [SHOP_FILE])
    rc, envelope = _annotate(
        tmp_path,
        capsys,
        [
            {
                "model": "shop.orders",
                "columns": {
                    "status": {"role": "dimension", "description": "Lifecycle."}
                },
            }
        ],
    )
    assert rc == 0, envelope["errors"]
    text = _stored_plans(tmp_path)[0]["edits"][0]["new_content"]
    before, after = ANNOTATED.splitlines(), text.splitlines()
    added = [line for line in after if line not in before]
    assert added == ["        description: Lifecycle."]
    assert [line for line in before if line not in after] == []


def test_annotate_names_an_aggregation_the_authored_metric_contradicts(
    tmp_path: Path, capsys
):
    # The authored `order_total_sum` takes a MAX; the declaration says sum.
    put(
        tmp_path,
        SHOP_FILE,
        ANNOTATED.replace("SUM(orders.order_total)", "MAX(orders.order_total)"),
    )
    _configure(tmp_path, [SHOP_FILE])
    rc, envelope = _annotate(
        tmp_path,
        capsys,
        [
            {
                "model": "orders",
                "description": "Orders.",
                "columns": {
                    "order_total": {"role": "measure", "aggregation": "sum"},
                    "status": {"role": "measure", "aggregation": "max"},
                },
            }
        ],
    )
    assert rc == 0, envelope["errors"]
    notes = " ".join(envelope["data"]["notes"])
    assert "aggregates with MAX, and the declaration says sum" in notes
    text = _stored_plans(tmp_path)[0]["edits"][0]["new_content"]
    metrics = {m["name"]: m for m in _table(text, "orders")["metrics"]}
    # The authored expression is never rewritten; a new aggregation is appended,
    # reading the member it aggregates.
    assert metrics["order_total_sum"]["expr"] == "MAX(orders.order_total)"
    assert metrics["status_max"]["expr"] == "MAX(orders.status)"


def test_annotate_refuses_a_table_no_file_defines(tmp_path: Path, capsys):
    put(tmp_path, SHOP_FILE, ANNOTATED)
    _configure(tmp_path, [SHOP_FILE])
    rc, envelope = _annotate(
        tmp_path, capsys, [{"model": "nowhere", "description": "x"}]
    )
    assert rc != 0
    assert "no configured Semantic View declares a logical table named 'nowhere'" in (
        " ".join(envelope["errors"])
    )


def test_annotate_refuses_a_table_name_two_views_share(tmp_path: Path, capsys):
    put(tmp_path, SHOP_FILE, ANNOTATED)
    put(
        tmp_path,
        "views/other.yaml",
        dump_yaml(
            sv(
                "other",
                table(
                    "orders",
                    "demo.main.orders_archive",
                    ["id"],
                    dimensions=[{"name": "s", "expr": "s"}],
                ),
            )
        ),
    )
    _configure(tmp_path, [SHOP_FILE, "views/other.yaml"])
    rc, envelope = _annotate(
        tmp_path, capsys, [{"model": "orders", "description": "x"}]
    )
    assert rc != 0
    assert "more than one view (shop.orders, other.orders)" in " ".join(
        envelope["errors"]
    ) or "more than one view (other.orders, shop.orders)" in " ".join(
        envelope["errors"]
    )


def test_annotate_with_nothing_to_change_keeps_the_file(tmp_path: Path, capsys):
    put(tmp_path, SHOP_FILE, ANNOTATED)
    _configure(tmp_path, [SHOP_FILE])
    rc, envelope = _annotate(
        tmp_path,
        capsys,
        [{"model": "orders", "columns": {"status": {"role": "dimension"}}}],
    )
    assert rc != 0
    assert "already state every" in " ".join(envelope["errors"])


# --- the shipped contract -----------------------------------------------------------


class TestSemanticViewDerivation(SemanticDerivationContract):
    """The derivation hook's promises, held by the Semantic View layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path
        put(tmp_path, SHOP_FILE, SHOP_TEXT)

    def make_derivation_source(self) -> Any:
        return _source(self.root, [SHOP_FILE])

    def a_derive_request(self) -> Any:
        return _request({SHOP_FILE: read(self.root, SHOP_FILE)})
