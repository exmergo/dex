"""An Ossie dataset derived from a declared mart, and declarations on
`semantic ossie` (#500).

The Ossie implementation of the derivation hook (#927): a dbt mart under
`transform.marts_paths` whose YAML declares it becomes a dataset of the configured
document holding the datasets it references, included as a plan beside the
mart's, or a document of its own, proposed, where no configured document can
hold it. Nothing here opens a warehouse.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from transform.test_semantic_derivation import MART, MART_SQL, _fake_parse, _payload

from exmergo_dex_core.cli import main
from exmergo_dex_core.semantic_source import SemanticDerivationSource
from exmergo_dex_core.semantic_source_conformance import SemanticDerivationContract

COMMERCE = "semantic/commerce.ossie.yaml"
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
    },
}

#: A hand-written document: the customer field is named differently from its
#: column, so a relationship to it has to name the field (Name Resolution).
COMMERCE_TEXT = (
    "# Hand-written; every byte here stays.\n"
    'version: "0.2.0.dev0"\n'
    "name: commerce\n"
    "datasets:\n"
    "  # The customer dimension.\n"
    "  - name: customers\n"
    "    source: demo.main.stg_customers\n"
    "    primary_key: [customer_key]\n"
    "    fields:\n"
    "      - name: customer_key\n"
    "        expression:\n"
    "          sql: id\n"
    "      - name: region\n"
    "        expression:\n"
    "          sql: region   # kept as written\n"
)


@pytest.fixture
def no_warehouse(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", refuse)


def put(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def read(root: Path, name: str) -> str:
    return (root / name).read_bytes().decode("utf-8")


def run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _configure(root: Path, files: list[str]) -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: ossie\n  ossie:\n    files:\n"
        + "".join(f"      - {name}\n" for name in files),
        encoding="utf-8",
    )


def _plan(
    root: Path, capsys, declaration: dict | None = None, sql: str = MART_SQL
) -> tuple[int, dict[str, Any]]:
    body = {
        "edits": [{"path": SQL_PATH, "kind": "model_sql", "content": sql}],
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


def _proposed(envelope: dict[str, Any]) -> dict[str, Any]:
    return envelope["data"]["semantic_proposal"]


def _dataset(model: dict[str, Any], name: str = "fct_orders") -> dict[str, Any]:
    document = yaml.safe_load(model["content"])
    return next(d for d in document["datasets"] if d["name"] == name)


def _stored_plans(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / ".dex" / "plans").glob("*.json"))
    ]


@pytest.fixture
def project(dbt_project_dir: Path, tmp_path: Path, monkeypatch) -> Path:
    _fake_parse(monkeypatch)
    return tmp_path


def test_the_ossie_layer_implements_the_derivation_hook(tmp_path: Path):
    from exmergo_dex_core.ossie.project import OssieSemanticLayer

    assert isinstance(
        OssieSemanticLayer(tmp_path, [COMMERCE]), SemanticDerivationSource
    )


# --- derive: a declared mart becomes a dataset ---------------------------------------


def test_the_document_holding_the_parent_gets_the_dataset_as_a_companion_plan(
    project: Path, capsys, no_warehouse
):
    put(project, COMMERCE, COMMERCE_TEXT)
    _configure(project, [COMMERCE])

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope["errors"]
    proposal = _proposed(envelope)
    assert proposal["vendor"] == "ossie"
    assert proposal["mode"] == "included", proposal["models"]
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "included", model
    assert model["path"] == COMMERCE
    for check in ("ossie", "catalog", "plan"):
        assert model["validation"][check]["result"] == "passed", model["validation"]
    # Appended, not rewritten: the hand-written document is a prefix of the plan.
    assert model["content"].startswith(COMMERCE_TEXT)
    companion = proposal["plan_id"]
    dbt_plan = envelope["data"]["plan_id"]
    assert companion != dbt_plan
    plans = {p["plan_id"]: p for p in _stored_plans(project)}
    assert [e["path"] for e in plans[companion]["edits"]] == [COMMERCE]

    for plan_id in (dbt_plan, companion):
        rc, applied = run(capsys, project, "transform", "apply", plan_id)
        assert rc == 0, applied
    assert read(project, COMMERCE) == model["content"]

    rc, listed = run(capsys, project, "explore", "semantic", "list")
    assert rc == 0, listed
    models = {m["name"]: m for m in listed["data"]["semantic_models"]}
    assert models["commerce.fct_orders"]["relation"] == "demo.main.fct_orders"
    assert "fct_orders.order_total_sum" in {
        m["name"] for m in listed["data"]["metrics"]
    }


def test_the_derived_dataset_states_what_the_declaration_says(
    project: Path, capsys, no_warehouse
):
    put(project, COMMERCE, COMMERCE_TEXT)
    _configure(project, [COMMERCE])

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    body = _dataset(model)
    # The database and schema the sibling dataset already reads.
    assert body["source"] == "demo.main.fct_orders"
    assert body["primary_key"] == ["order_id"]
    fields = {f["name"]: f for f in body["fields"]}
    assert fields["order_id"]["expression"] == {"sql": "order_id"}
    assert fields["ordered_at"]["dimension"] == {"is_time": True}
    assert fields["ordered_at"]["datatype"] == "Date"
    assert fields["status"]["description"] == "Lifecycle status."
    assert fields["order_total"]["ai_context"] == {
        "dex": {"unit": "currency:EUR", "additivity": "additive", "role": "measure"}
    }
    # A measure is a field, and its declared aggregation a dataset metric.
    assert body["metrics"] == [
        {
            "name": "order_total_sum",
            "expression": {"sql": "SUM(order_total)"},
            "description": "Order value.",
        }
    ]
    # The semi-additive balance has no exact Ossie equivalent and is left out.
    assert any("balance" in lim for lim in model["limitations"])
    assert "columns.balance.aggregation" in model["not_carried"]
    # The relationship names the parent's field, not its column `id`.
    (relationship,) = yaml.safe_load(model["content"])["relationships"]
    assert relationship == {
        "name": "fct_orders_to_customers",
        "from": "fct_orders",
        "to": "customers",
        "from_columns": ["customer_id"],
        "to_columns": ["customer_key"],
    }
    for field in (
        "grain",
        "columns.customer_id.references",
        "columns.order_total.unit",
        "columns.order_total.aggregation",
    ):
        assert field in model["carried"]


def test_a_population_is_ai_context_and_a_sentence_never_a_filter(
    project: Path, capsys, no_warehouse
):
    put(project, COMMERCE, COMMERCE_TEXT)
    _configure(project, [COMMERCE])

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    body = _dataset(model)
    sentence = (
        "Population: Paid and shipped orders; status is one of 'paid' or 'shipped'."
    )
    assert body["description"] == f"One row per order. {sentence}"
    assert body["ai_context"]["instructions"] == sentence
    assert body["ai_context"]["dex"] == {
        "population": POPULATED["population"],
        "assumptions": POPULATED["assumptions"],
    }
    assert "custom_extensions" not in json.dumps(body)
    # Ossie has no filter on a dataset or a metric, and none is invented.
    assert set(body) <= {
        "name",
        "source",
        "primary_key",
        "description",
        "ai_context",
        "fields",
        "metrics",
    }
    assert all(set(m) <= {"name", "expression", "description"} for m in body["metrics"])
    assert "population" in model["carried"]
    assert "assumptions" in model["carried"]


def test_with_no_document_holding_a_parent_it_is_proposed_as_its_own(
    project: Path, capsys, no_warehouse
):
    other = COMMERCE_TEXT.replace("stg_customers", "elsewhere_customers")
    put(project, COMMERCE, other)
    _configure(project, [COMMERCE])

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope["errors"]
    proposal = _proposed(envelope)
    assert proposal["mode"] == "proposed"
    assert "plan_id" not in proposal
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "proposed", model
    assert model["path"] == "semantic/fct_orders.ossie.yaml"
    document = yaml.safe_load(model["content"])
    assert document["version"] == "0.2.0.dev0"
    assert document["name"] == "fct_orders"
    assert "relationships" not in document
    assert "columns.customer_id.references" in model["not_carried"]
    assert "dex config set semantic.ossie.files" in proposal["next"]
    assert model["validation"]["ossie"]["result"] == "passed"
    assert not (project / model["path"]).exists()


@pytest.mark.parametrize("command", ["plan", "declare"])
def test_a_configured_missing_document_is_included_as_an_applicable_plan(
    project: Path, capsys, no_warehouse, command: str
):
    path = "semantic/fct_orders.ossie.yaml"
    _configure(project, [path])
    if command == "plan":
        rc, envelope = _plan(project, capsys)
    else:
        put(project / "analytics", SQL_PATH, MART_SQL)
        rc, envelope = run(
            capsys,
            project,
            "transform",
            "declare",
            "fct_orders",
            "--edits-file",
            _payload(project, {"declarations": [POPULATED]}),
        )

    assert rc == 0, envelope["errors"]
    proposal = _proposed(envelope)
    assert proposal["mode"] == "included"
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "included"
    assert model["path"] == path
    for check in ("ossie", "catalog", "plan"):
        assert model["validation"][check]["result"] == "passed"
    assert "dex config set" not in proposal.get("next", "")
    assert not (project / path).exists()

    rc, applied = run(capsys, project, "transform", "apply", proposal["plan_id"])
    assert rc == 0, applied
    assert read(project, path) == model["content"]


def test_a_new_document_does_not_bypass_validation_of_other_configured_files(
    project: Path, capsys, no_warehouse
):
    _configure(
        project, ["semantic/fct_orders.ossie.yaml", "semantic/missing.ossie.yaml"]
    )

    _rc, envelope = _plan(project, capsys)

    proposal = _proposed(envelope)
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "failed"
    assert model["validation"]["plan"]["result"] == "failed"
    assert "semantic/missing.ossie.yaml" in model["reason"]
    assert "plan_id" not in proposal
    assert not (project / "semantic/fct_orders.ossie.yaml").exists()


def test_parents_in_different_documents_are_proposed_not_split(
    project: Path, capsys, no_warehouse
):
    stores = "semantic/stores.ossie.yaml"
    put(project, COMMERCE, COMMERCE_TEXT)
    put(
        project,
        stores,
        'version: "0.2.0.dev0"\nname: stores\ndatasets:\n  - name: dim_stores\n'
        "    source: demo.main.dim_stores\n    fields:\n      - name: store_id\n"
        "        expression: {sql: store_id}\n",
    )
    _configure(project, [COMMERCE, stores])
    declaration = copy.deepcopy(POPULATED)
    declaration["columns"]["store_id"] = {
        "role": "foreign_key",
        "references": "dim_stores.store_id",
    }

    body = {
        "edits": [
            {
                "path": SQL_PATH,
                "kind": "model_sql",
                "content": MART_SQL.rstrip("\n") + ", 1 as store_id\n",
            },
            {
                "path": "models/marts/dim_stores.sql",
                "kind": "model_sql",
                "content": "select 1 as store_id\n",
            },
        ],
        "declarations": [declaration],
    }
    _rc, envelope = run(
        capsys,
        project,
        "transform",
        "plan",
        "two parents",
        "--edits-file",
        _payload(project, body),
    )

    assert "semantic_proposal" in envelope["data"], envelope["errors"]
    model = _proposed(envelope)["models"]["fct_orders"]
    assert model["status"] == "proposed", model
    assert any("different documents" in n for n in model["notes"])


def test_an_authored_dataset_on_the_mart_is_never_overwritten(
    project: Path, capsys, no_warehouse
):
    authored = COMMERCE_TEXT + (
        "  - name: orders\n    source: demo.main.fct_orders\n    fields:\n"
        "      - name: order_id\n        expression: {sql: order_id}\n"
    )
    put(project, COMMERCE, authored)
    _configure(project, [COMMERCE])

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert model["status"] == "skipped"
    assert "'orders'" in model["reason"]


def test_a_dataset_name_another_table_holds_fails_rather_than_being_renamed(
    project: Path, capsys, no_warehouse
):
    authored = COMMERCE_TEXT + (
        "  - name: fct_orders\n    source: demo.main.orders_legacy\n    fields:\n"
        "      - name: order_id\n        expression: {sql: order_id}\n"
    )
    put(project, COMMERCE, authored)
    _configure(project, [COMMERCE])

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert model["status"] == "failed"
    assert "already declared" in model["reason"]
    assert "plan_id" not in _proposed(envelope)


def test_an_unmappable_aggregation_is_named_not_guessed(
    project: Path, capsys, no_warehouse
):
    put(project, COMMERCE, COMMERCE_TEXT)
    _configure(project, [COMMERCE])
    declaration = copy.deepcopy(POPULATED)
    declaration["columns"]["order_total"]["aggregation"] = "median"

    _rc, envelope = _plan(project, capsys, declaration)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert "metrics" not in _dataset(model)
    assert "median" in model["not_carried"]["columns.order_total.aggregation"]


def test_under_dbt_nothing_ossie_shaped_is_proposed(
    project: Path, capsys, no_warehouse
):
    _rc, envelope = _plan(project, capsys)

    proposal = _proposed(envelope)
    assert proposal["vendor"] == "dbt"
    assert "ai_context" not in json.dumps(proposal)


def test_transform_declare_reports_the_companion_plan(
    project: Path, capsys, no_warehouse
):
    put(project, COMMERCE, COMMERCE_TEXT)
    _configure(project, [COMMERCE])
    mart = project / "analytics" / SQL_PATH
    mart.parent.mkdir(parents=True, exist_ok=True)
    mart.write_text(MART_SQL, encoding="utf-8")

    rc, envelope = run(
        capsys,
        project,
        "transform",
        "declare",
        "fct_orders",
        "--edits-file",
        _payload(project, {"declarations": [POPULATED]}, name="declare.json"),
    )

    assert rc == 0, envelope["errors"]
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["vendor"] == "ossie"
    assert proposal["models"]["fct_orders"]["status"] == "included"
    assert proposal["plan_id"] in envelope["data"]["next"]


# --- annotate: declarations on `semantic ossie` --------------------------------------


ORDERS_TEXT = COMMERCE_TEXT + (
    "\n"
    "  # Orders keep their comment.\n"
    "  - name: orders\n"
    "    source: demo.main.orders\n"
    "    fields:\n"
    "      - name: order_id\n"
    "        expression: {sql: o_orderkey}\n"
    "      - name: buyer\n"
    "        expression: {sql: o_custkey}\n"
    "      - name: amount\n"
    "        expression: {sql: o_total}\n"
)


def _ossie_plan(root: Path, capsys, body: dict) -> tuple[int, dict[str, Any]]:
    return run(
        capsys,
        root,
        "semantic",
        "ossie",
        "plan",
        "declare orders",
        "--edits-file",
        _payload(root, body, name="ossie.json"),
    )


def test_declarations_annotate_one_dataset_through_its_fields(
    tmp_path: Path, capsys, no_warehouse
):
    put(tmp_path, COMMERCE, ORDERS_TEXT)
    _configure(tmp_path, [COMMERCE])

    rc, envelope = _ossie_plan(
        tmp_path,
        capsys,
        {
            "declarations": [
                {
                    "model": "orders",
                    "description": "One row per order.",
                    "grain": ["o_orderkey"],
                    "population": {"rule": "Orders that were placed"},
                    "columns": {
                        "o_custkey": {
                            "role": "foreign_key",
                            "references": "stg_customers.id",
                        },
                        "o_total": {
                            "role": "measure",
                            "aggregation": "sum",
                            "unit": "currency:EUR",
                        },
                    },
                }
            ]
        },
    )

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["updated"] == ["commerce"]
    (stored,) = _stored_plans(tmp_path)
    (edit,) = stored["edits"]
    text = edit["new_content"]
    # Everything before the annotated dataset is byte-identical, comments too.
    head = ORDERS_TEXT[: ORDERS_TEXT.index("  - name: orders")]
    assert text.startswith(head)
    document = yaml.safe_load(text)
    orders = next(d for d in document["datasets"] if d["name"] == "orders")
    # The grain names a column; the key names the field exposing it.
    assert orders["primary_key"] == ["order_id"]
    assert orders["description"] == (
        "One row per order. Population: Orders that were placed."
    )
    assert orders["ai_context"]["dex"]["population"] == {
        "rule": "Orders that were placed"
    }
    assert orders["metrics"] == [
        {"name": "amount_sum", "expression": {"sql": "SUM(amount)"}}
    ]
    amount = next(f for f in orders["fields"] if f["name"] == "amount")
    assert amount["ai_context"] == {"dex": {"unit": "currency:EUR", "role": "measure"}}
    assert document["relationships"] == [
        {
            "name": "orders_to_customers",
            "from": "orders",
            "to": "customers",
            "from_columns": ["buyer"],
            "to_columns": ["customer_key"],
        }
    ]


def test_a_grain_column_no_field_exposes_is_refused(
    tmp_path: Path, capsys, no_warehouse
):
    put(tmp_path, COMMERCE, ORDERS_TEXT)
    _configure(tmp_path, [COMMERCE])

    rc, envelope = _ossie_plan(
        tmp_path,
        capsys,
        {"declarations": [{"model": "orders", "grain": ["o_orderkey", "line_no"]}]},
    )

    assert rc != 0
    assert "no field exposing the grain column line_no" in " ".join(envelope["errors"])
    assert not list((tmp_path / ".dex").glob("plans/*"))


def test_a_declaration_naming_no_dataset_is_refused(
    tmp_path: Path, capsys, no_warehouse
):
    put(tmp_path, COMMERCE, ORDERS_TEXT)
    _configure(tmp_path, [COMMERCE])

    rc, envelope = _ossie_plan(
        tmp_path, capsys, {"declarations": [{"model": "refunds", "grain": ["id"]}]}
    )

    assert rc != 0
    assert "declares a dataset named 'refunds'" in " ".join(envelope["errors"])


def test_declarations_ride_beside_the_payloads_own_edits(
    tmp_path: Path, capsys, no_warehouse
):
    _configure(tmp_path, [COMMERCE])
    unkeyed = COMMERCE_TEXT.replace("    primary_key: [customer_key]\n", "")

    rc, envelope = _ossie_plan(
        tmp_path,
        capsys,
        {
            "edits": [{"path": COMMERCE, "content": unkeyed}],
            "declarations": [{"model": "customers", "grain": ["id"]}],
        },
    )

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["defined"] == ["commerce"]
    (stored,) = _stored_plans(tmp_path)
    document = yaml.safe_load(stored["edits"][0]["new_content"])
    assert document["datasets"][0]["primary_key"] == ["customer_key"]


# --- the shipped contract ------------------------------------------------------------


class TestOssieDerivation(SemanticDerivationContract):
    """The derivation hook's promises, held by the Ossie layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path
        put(tmp_path, COMMERCE, COMMERCE_TEXT)

    def make_derivation_source(self) -> Any:
        from exmergo_dex_core.ossie.project import OssieSemanticLayer
        from exmergo_dex_core.semantic_source import SemanticSourceContext

        return OssieSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"files": [COMMERCE]},
            )
        )

    def a_derive_request(self) -> Any:
        from exmergo_dex_core.semantic_source import (
            DeclaredReference,
            SemanticDeclaration,
            SemanticDerivationRequest,
        )

        declared = {k: v for k, v in POPULATED.items() if k != "model"}
        return SemanticDerivationRequest(
            mode="derive",
            declarations=(
                SemanticDeclaration(
                    model="fct_orders",
                    declared=declared,
                    column_types={"ordered_at": ("date", None)},
                    references=(
                        DeclaredReference(
                            column="customer_id",
                            parent_model="stg_customers",
                            parent_column="id",
                            parent_identifier="stg_customers",
                        ),
                    ),
                ),
            ),
            documents={COMMERCE: read(self.root, COMMERCE)},
            project_identifiers={
                "stg_customers": "stg_customers",
                "fct_orders": "fct_orders",
            },
            connector="duckdb",
        )
