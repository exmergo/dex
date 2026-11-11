"""A Metric View derived from a declared mart, and declarations on
`semantic databricks` (#1258).

The Databricks implementation of the derivation hook (#927): a dbt mart under
`transform.marts_paths` whose YAML declares it becomes a Metric View file of its
own, included as a plan beside the mart's where its path is already on the
layer's write surface, proposed otherwise. A `declarations` payload on
`semantic databricks plan` annotates a view already in the layer. Nothing here
opens a workspace or a warehouse, and every view written reads back through the
#1257 reader.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from transform.test_semantic_derivation import MART, MART_SQL, _fake_parse, _payload

from exmergo_dex_core.metric_views.loader import dump_yaml, read_yaml
from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
from exmergo_dex_core.semantic_source import (
    DeclaredReference,
    SemanticDeclaration,
    SemanticDerivationRequest,
    SemanticDerivationSource,
    SemanticSourceContext,
)
from exmergo_dex_core.semantic_source_conformance import SemanticDerivationContract

from .conftest import view
from .test_authoring import put, read, run

CUSTOMERS_FILE = "views/customers.yaml"
#: The shared mart, with the rate column POPULATED declares.
MART_RATE_SQL = MART_SQL.rstrip("\n") + ",\n  0.1 as discount_rate\n"

FCT = "views/fct_orders.yaml"
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
        "discount_rate": {
            "role": "measure",
            "aggregation": "average",
            "additivity": "non_additive",
            "unit": "fraction",
        },
    },
}

CUSTOMERS = view(
    source="demo.main.stg_customers",
    comment="Customers.",
    fields=[{"name": "region", "expr": "region"}],
    measures=[{"name": "customers", "expr": "COUNT(id)"}],
)


@pytest.fixture
def no_warehouse(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", refuse)


@pytest.fixture
def project(dbt_project_dir: Path, tmp_path: Path, monkeypatch) -> Path:
    _fake_parse(monkeypatch)
    # The parent declares its grain, so the derived join may assert `rely`.
    (dbt_project_dir / "models" / "staging" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: stg_customers\n"
        "    config:\n"
        "      meta:\n"
        "        dex:\n"
        "          grain: [id]\n"
        "    columns:\n"
        "      - name: id\n"
        "        tests: [not_null]\n",
        encoding="utf-8",
    )
    return tmp_path


def _configure(root: Path, files: list[str]) -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: databricks\n"
        "  databricks:\n    files:\n" + "".join(f"      - {n}\n" for n in files),
        encoding="utf-8",
    )


def _layer(root: Path, *, configure_fct: bool) -> None:
    put(root, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
    _configure(root, [CUSTOMERS_FILE, *([FCT] if configure_fct else [])])


def _source(root: Path, files: list[str]) -> MetricViewSemanticLayer:
    return MetricViewSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(root), connector="duckdb", options={"files": files}
        )
    )


def _request(
    root: Path, declared: dict | None = None, *, parent_grain=("id",)
) -> SemanticDerivationRequest:
    body = {k: v for k, v in (declared or POPULATED).items() if k != "model"}
    return SemanticDerivationRequest(
        mode="derive",
        declarations=(
            SemanticDeclaration(
                model="fct_orders",
                declared=body,
                column_types={"ordered_at": ("date", None)},
                relation="demo.main.fct_orders",
                identifier="fct_orders",
                references=(
                    DeclaredReference(
                        column="customer_id",
                        parent_model="stg_customers",
                        parent_column="id",
                        parent_identifier="stg_customers",
                        parent_grain=parent_grain,
                    ),
                ),
            ),
        ),
        documents={CUSTOMERS_FILE: read(root, CUSTOMERS_FILE)},
        project_identifiers={
            "stg_customers": "stg_customers",
            "fct_orders": "fct_orders",
        },
    )


def _derive(root: Path, *, configure_fct: bool = True, **kwargs) -> Any:
    _layer(root, configure_fct=configure_fct)
    files = [CUSTOMERS_FILE, *([FCT] if configure_fct else [])]
    return _source(root, files).derive_semantic(_request(root, **kwargs))


def _plan(root: Path, capsys, declaration: dict | None = None) -> tuple[int, dict]:
    body = {
        "edits": [{"path": SQL_PATH, "kind": "model_sql", "content": MART_RATE_SQL}],
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


def test_the_layer_implements_the_derivation_hook(tmp_path: Path):
    assert isinstance(
        MetricViewSemanticLayer(tmp_path, [FCT]), SemanticDerivationSource
    )


# --- derive: a declared mart becomes a view ----------------------------------------


def test_a_declared_mart_becomes_a_metric_view(tmp_path: Path, no_warehouse):
    result = _derive(tmp_path)

    definition = result.definitions["fct_orders"]
    assert definition.status == "included", definition.reason
    assert definition.path == FCT
    body = read_yaml(definition.content)
    assert body["version"] == 1.1
    # The siblings' catalog and schema, not the manifest's dev relation.
    assert body["source"] == "demo.main.fct_orders"
    assert body["comment"] == (
        "One row per order. Population: Paid and shipped orders; status is one of "
        "'paid' or 'shipped'."
    )
    assert "filter" not in body
    fields = {f["name"]: f for f in body["fields"]}
    assert set(fields) == {"order_id", "customer_id", "status", "ordered_at"}
    assert fields["status"]["comment"] == "Lifecycle status."
    assert fields["ordered_at"]["format"] == {
        "type": "date",
        "date_format": "year_month_day",
    }
    measures = {m["name"]: m for m in body["measures"]}
    assert measures["order_total_sum"] == {
        "name": "order_total_sum",
        "expr": "SUM(order_total)",
        "comment": "Order value.",
        "format": {"type": "currency", "currency_code": "EUR"},
    }
    assert measures["discount_rate_avg"]["format"] == {"type": "percentage"}
    # semi_additive has no exact Metric View equivalent without an ordering.
    assert "balance_sum" not in measures
    assert "balance" in " ".join(definition.limitations)
    (join,) = body["joins"]
    assert join == {
        "name": "stg_customers",
        "source": "demo.main.stg_customers",
        "on": "source.customer_id = stg_customers.id",
        "rely": {"at_most_one_match": True},
    }


def test_the_join_key_reads_back_as_text_on(tmp_path: Path):
    content = _derive(tmp_path).definitions["fct_orders"].content
    assert "\n    on: source.customer_id" in content
    assert "'on'" not in content and "true:" not in content


def test_a_derived_view_round_trips_through_the_reader(tmp_path: Path):
    result = _derive(tmp_path)
    put(tmp_path, FCT, result.definitions["fct_orders"].content)
    layer = _source(tmp_path, [CUSTOMERS_FILE, FCT])

    loaded = layer.loaded()
    assert not loaded.errors, loaded.notes()
    assert not [d for d in loaded.diagnostics if d.file == FCT], loaded.notes()

    catalog = layer.semantic_catalog()
    model = next(m for m in catalog.semantic_models if m.name == "fct_orders")
    # The population sentence is withheld from the catalog, as every native
    # catalog withholds it.
    assert model.description == "One row per order."
    assert "population sentence" in " ".join(catalog.notes)
    time = next(d for d in catalog.dimensions if d.name == "fct_orders.ordered_at")
    assert time.type == "time"
    assert {m.name for m in catalog.metrics} >= {
        "fct_orders.order_total_sum",
        "fct_orders.discount_rate_avg",
    }

    definitions = layer.declared_definitions()
    assert any(
        (rel.to_relation or "").endswith("stg_customers")
        and rel.column_pairs == [("customer_id", "id")]
        for rel in definitions.declared_relationships
    ), definitions.declared_relationships
    assert any(
        (key.relation or "").endswith("stg_customers")
        and key.column == "id"
        and key.unique
        for key in definitions.declared_keys
    ), definitions.declared_keys


def test_the_join_reads_the_source_of_the_view_on_the_parent(tmp_path: Path):
    """No sibling prefix and no manifest relation for the parent: the view
    already sitting on its table names the relation to join."""

    _layer(tmp_path, configure_fct=True)
    request = _request(tmp_path)
    (declaration,) = request.declarations
    (reference,) = declaration.references
    bare = SemanticDerivationRequest(
        mode="derive",
        declarations=(declaration,),
        documents=request.documents,
        project_identifiers={},
    )
    definition = (
        _source(tmp_path, [CUSTOMERS_FILE, FCT]).derive_semantic(bare).definitions
    )["fct_orders"]
    (join,) = read_yaml(definition.content)["joins"]
    assert reference.parent_relation is None
    assert join["source"] == "demo.main.stg_customers"
    assert any("the view already on stg_customers" in n for n in definition.notes)


def test_a_sibling_the_same_plan_creates_places_the_view(
    project: Path, capsys, no_warehouse
):
    """The sibling prefix counts a model the plan itself creates (#1258)."""

    put(
        project,
        CUSTOMERS_FILE,
        dump_yaml(dict(CUSTOMERS, source="prod_db.marts.stg_accounts")),
    )
    _configure(project, [CUSTOMERS_FILE, FCT])
    body = {
        "edits": [
            {
                "path": "models/staging/stg_accounts.sql",
                "kind": "model_sql",
                "content": "select 1 as account_id\n",
            },
            {"path": SQL_PATH, "kind": "model_sql", "content": MART_RATE_SQL},
        ],
        "declarations": [POPULATED],
    }

    rc, envelope = run(
        capsys,
        project,
        "transform",
        "plan",
        "accounts and orders",
        "--edits-file",
        _payload(project, body),
    )

    assert rc == 0, envelope["errors"]
    content = envelope["data"]["semantic_proposal"]["models"]["fct_orders"]["content"]
    assert read_yaml(content)["source"] == "prod_db.marts.fct_orders"


def test_no_rely_without_a_declared_parent_grain(tmp_path: Path):
    result = _derive(tmp_path, parent_grain=None)
    definition = result.definitions["fct_orders"]
    (join,) = read_yaml(definition.content)["joins"]
    assert "rely" not in join
    assert any("without `rely`" in note for note in definition.notes)


def test_what_a_metric_view_cannot_hold_is_named(tmp_path: Path):
    definition = _derive(tmp_path).definitions["fct_orders"]
    assert "population" in definition.carried
    assert "description" in definition.carried
    assert "columns.order_total.unit" in definition.carried
    assert "columns.customer_id.references" in definition.carried
    not_carried = definition.not_carried
    assert "metadata key" in not_carried["assumptions"]
    assert "metadata key" in not_carried["columns.order_total.additivity"]
    assert "own source" in not_carried["grain"]


def test_a_percent_unit_and_a_count_keep_no_format(tmp_path: Path):
    declared = copy.deepcopy(POPULATED)
    declared["columns"]["discount_rate"]["unit"] = "percent"
    declared["columns"]["order_total"]["aggregation"] = "count"
    definition = _derive(tmp_path, declared=declared).definitions["fct_orders"]
    measures = {m["name"]: m for m in read_yaml(definition.content)["measures"]}
    assert "format" not in measures["discount_rate_avg"]
    assert "format" not in measures["order_total_count"]
    assert "100" in definition.not_carried["columns.discount_rate.unit"]
    assert "count" in definition.not_carried["columns.order_total.unit"]


def test_an_unconfigured_path_is_proposed_with_the_command_that_adopts_it(
    tmp_path: Path,
):
    result = _derive(tmp_path, configure_fct=False)
    definition = result.definitions["fct_orders"]
    assert definition.status == "proposed"
    assert not result.edits
    assert result.proposal["vendor"] == "databricks"
    assert result.proposal["mode"] == "proposed"
    assert (
        f"dex config set semantic.databricks.files '[{CUSTOMERS_FILE}, {FCT}]'"
        in result.proposal["next"]
    )
    (edit,) = result.proposal["definitions"]["edits"]
    assert edit["path"] == FCT and edit["kind"] == "semantic_document"


def test_a_mart_with_a_view_already_is_skipped(tmp_path: Path):
    put(
        tmp_path,
        FCT,
        dump_yaml(
            view(
                source="demo.main.fct_orders",
                measures=[{"name": "n", "expr": "COUNT(1)"}],
            )
        ),
    )
    _layer(tmp_path, configure_fct=True)
    layer = _source(tmp_path, [CUSTOMERS_FILE, FCT])
    request = _request(tmp_path)
    request = SemanticDerivationRequest(
        mode="derive",
        declarations=request.declarations,
        documents={**request.documents, FCT: read(tmp_path, FCT)},
        project_identifiers=request.project_identifiers,
    )
    definition = layer.derive_semantic(request).definitions["fct_orders"]
    assert definition.status == "skipped"
    assert "already has a Metric View" in definition.reason


def test_a_name_another_view_holds_fails(tmp_path: Path):
    other = "views/other/FCT_ORDERS.yaml"
    put(
        tmp_path,
        other,
        dump_yaml(
            view(
                source="demo.main.somewhere_else",
                measures=[{"name": "n", "expr": "COUNT(1)"}],
            )
        ),
    )
    _layer(tmp_path, configure_fct=False)
    layer = _source(tmp_path, [CUSTOMERS_FILE, other])
    request = _request(tmp_path)
    request = SemanticDerivationRequest(
        mode="derive",
        declarations=request.declarations,
        documents={**request.documents, other: read(tmp_path, other)},
        project_identifiers=request.project_identifiers,
    )
    definition = layer.derive_semantic(request).definitions["fct_orders"]
    assert definition.status == "failed"
    assert "already taken" in definition.reason


# --- the plan routes ---------------------------------------------------------------


def test_transform_plan_stores_the_view_as_a_companion_plan(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=True)

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope["errors"]
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["vendor"] == "databricks"
    assert proposal["models"]["fct_orders"]["status"] == "included", proposal
    assert proposal["plan_id"] in envelope["data"]["next"]
    content = proposal["models"]["fct_orders"]["content"]
    # The parent's own declaration states `id` as its grain.
    assert read_yaml(content)["joins"][0]["rely"] == {"at_most_one_match": True}

    rc, applied = run(capsys, project, "transform", "apply", proposal["plan_id"])
    assert rc == 0, applied["errors"]
    assert read(project, FCT) == content


def test_transform_plan_proposes_an_unconfigured_view(project: Path, capsys):
    _layer(project, configure_fct=False)
    rc, envelope = _plan(project, capsys)
    assert rc == 0, envelope["errors"]
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["mode"] == "proposed"
    assert "plan_id" not in proposal
    assert "semantic databricks define" in proposal["next"]


# --- annotate: declarations on `semantic databricks plan` ---------------------------

ANNOTATED_FILE = (
    "# the orders view; this comment stays\n"
    "version: 1.1\n"
    "source: demo.main.fct_orders  # prod\n"
    "fields:\n"
    "  - name: status\n"
    "    expr: status\n"
    "  # a hand-written note between members\n"
    "  - name: customer_id\n"
    "    expr: customer_id\n"
    "measures:\n"
    "  - name: order_total_sum\n"
    "    expr: SUM(order_total)\n"
)


def _annotate(root: Path, capsys, declarations: list[dict]) -> tuple[int, dict]:
    return run(
        capsys,
        root,
        "semantic",
        "databricks",
        "plan",
        "annotate",
        "--edits-file",
        _payload(root, {"declarations": declarations}),
    )


def test_annotate_splices_only_what_changes(tmp_path: Path, capsys, no_warehouse):
    put(tmp_path, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
    put(tmp_path, FCT, ANNOTATED_FILE)
    _configure(tmp_path, [CUSTOMERS_FILE, FCT])
    declaration = {
        "model": "fct_orders",
        "description": "One row per order.",
        "population": {"rule": "Placed orders"},
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
    assert text.startswith("# the orders view; this comment stays\n")
    assert "source: demo.main.fct_orders  # prod\n" in text
    assert "  # a hand-written note between members\n" in text
    body = read_yaml(text)
    assert body["comment"] == "One row per order. Population: Placed orders."
    fields = {f["name"]: f for f in body["fields"]}
    assert fields["status"]["comment"] == "Lifecycle status."
    measures = {m["name"]: m for m in body["measures"]}
    assert measures["order_total_sum"]["format"] == {
        "type": "currency",
        "currency_code": "USD",
    }
    assert measures["quantity_count"]["expr"] == "COUNT(quantity)"
    (join,) = body["joins"]
    assert join == {
        "name": "customers",
        "source": "demo.main.stg_customers",
        "on": "source.customer_id = customers.id",
    }
    assert "\n    on: source.customer_id" in text

    rc, applied = run(
        capsys, tmp_path, "transform", "apply", envelope["data"]["plan_id"]
    )
    assert rc == 0, applied["errors"]
    assert read(tmp_path, FCT) == text


def test_annotate_names_an_aggregation_the_authored_measure_contradicts(
    tmp_path: Path, capsys
):
    put(tmp_path, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
    _configure(tmp_path, [CUSTOMERS_FILE])
    declaration = {
        "model": "customers",
        "columns": {
            # The view counts `customers` with COUNT(id); the declaration says sum.
            "customers": {"role": "measure", "aggregation": "sum"},
            "region": {"role": "dimension", "unit": "count"},
            "id": {"role": "measure", "aggregation": "count", "unit": "currency:USD"},
        },
    }

    rc, envelope = _annotate(tmp_path, capsys, [declaration])

    assert rc == 0, envelope["errors"]
    notes = " ".join(envelope["data"]["notes"])
    assert "aggregates with COUNT, and the declaration says sum" in notes
    assert "a count needs no format" in notes
    assert "number of things" in notes
    (stored,) = _stored_plans(tmp_path)
    measures = {
        m["name"]: m for m in read_yaml(stored["edits"][0]["new_content"])["measures"]
    }
    # The authored expression is never rewritten, and a count takes no format.
    assert measures["customers"]["expr"] == "COUNT(id)"
    assert "format" not in measures["id_count"]


def test_annotate_refuses_a_view_no_file_defines(tmp_path: Path, capsys):
    put(tmp_path, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
    _configure(tmp_path, [CUSTOMERS_FILE])
    rc, envelope = _annotate(
        tmp_path, capsys, [{"model": "nowhere", "description": "x"}]
    )
    assert rc != 0
    assert "no configured Metric View is named 'nowhere'" in " ".join(
        envelope["errors"]
    )


def test_annotate_with_nothing_to_change_keeps_the_file(tmp_path: Path, capsys):
    put(tmp_path, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))
    _configure(tmp_path, [CUSTOMERS_FILE])
    rc, envelope = _annotate(
        tmp_path, capsys, [{"model": "customers", "description": "Customers."}]
    )
    assert rc != 0
    assert "already state every" in " ".join(envelope["errors"])


# --- the shipped contract -----------------------------------------------------------


class TestMetricViewDerivation(SemanticDerivationContract):
    """The derivation hook's promises, held by the Metric View layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path
        put(tmp_path, CUSTOMERS_FILE, dump_yaml(CUSTOMERS))

    def make_derivation_source(self) -> Any:
        return _source(self.root, [CUSTOMERS_FILE, FCT])

    def a_derive_request(self) -> Any:
        return _request(self.root)
