"""A cube derived from a declared mart, and declarations on `semantic cube` (#911).

The Cube implementation of the derivation hook (#927): a dbt mart under
`transform.marts_paths` whose YAML declares it becomes a Cube model file of its
own, included as a plan beside the mart's where its path is already on the
layer's write surface, proposed otherwise. Nothing here starts a Cube server or
opens a warehouse.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from transform.test_semantic_derivation import MART, MART_SQL, _fake_parse, _payload

from exmergo_dex_core.semantic_source import SemanticDerivationSource
from exmergo_dex_core.semantic_source_conformance import SemanticDerivationContract

from .conftest import cube
from .test_authoring import dump, put, read, run

CUSTOMERS_FILE = "model/customers.yml"
FCT = "model/fct_orders.yml"
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


@pytest.fixture
def no_warehouse(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", refuse)


def customers(sql_table: str = "demo.main.stg_customers", **fields: Any) -> dict:
    return cube(
        "customers",
        sql_table=sql_table,
        dimensions=[
            {"name": "id", "sql": "id", "type": "number", "primary_key": True},
            {"name": "region", "sql": "{CUBE}.region", "type": "string"},
        ],
        **fields,
    )


def _configure(root: Path, files: list[str], *, extra: str = "") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: cube\n"
        + extra
        + "  cube:\n    files:\n"
        + "".join(f"      - {name}\n" for name in files),
        encoding="utf-8",
    )


def _layer(
    root: Path,
    *,
    configure_fct: bool,
    sibling_table: str = "demo.main.stg_customers",
) -> None:
    put(root, CUSTOMERS_FILE, dump(customers(sibling_table)))
    _configure(root, [CUSTOMERS_FILE, *([FCT] if configure_fct else [])])


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


def _cube(model: dict[str, Any]) -> dict[str, Any]:
    (body,) = yaml.safe_load(model["content"])["cubes"]
    return body


def _stored_plans(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / ".dex" / "plans").glob("*.json"))
    ]


@pytest.fixture
def project(dbt_project_dir: Path, tmp_path: Path, monkeypatch) -> Path:
    _fake_parse(monkeypatch)
    return tmp_path


def test_the_cube_layer_implements_the_derivation_hook(tmp_path: Path):
    from exmergo_dex_core.cube.project import CubeSemanticLayer

    assert isinstance(CubeSemanticLayer(tmp_path, [FCT]), SemanticDerivationSource)


# --- derive: a declared mart becomes a cube ----------------------------------------


def test_a_mart_whose_path_is_not_configured_is_proposed_with_how_to_adopt_it(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=False)

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope["errors"]
    proposal = _proposed(envelope)
    assert proposal["vendor"] == "cube"
    assert proposal["mode"] == "proposed"
    assert "plan_id" not in proposal
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "proposed", model
    assert model["cube"] == "fct_orders"
    assert model["validation"]["cube"]["result"] == "passed"
    assert model["validation"]["catalog"]["result"] == "passed"
    assert "dex config set semantic.cube.files" in proposal["next"]
    assert FCT in proposal["next"]
    (edit,) = proposal["definitions"]["edits"]
    assert edit["path"] == FCT and edit["kind"] == "semantic_document"
    (stored,) = _stored_plans(project)
    assert all(not e["path"].startswith("model/") for e in stored["edits"])
    assert not (project / FCT).exists()


def test_the_derived_cube_states_what_the_declaration_says(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=False)

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    body = _cube(model)
    assert body["name"] == "fct_orders"
    # The database and schema the sibling cube already reads.
    assert body["sql_table"] == "demo.main.fct_orders"
    assert "data_source" not in body
    dimensions = {d["name"]: d for d in body["dimensions"]}
    assert dimensions["order_id"] == {
        "name": "order_id",
        "sql": "{CUBE}.order_id",
        # The SQL selects a bare literal, so nothing free states its type.
        "type": "string",
        "primary_key": True,
        "meta": {"dex": {"role": "key"}},
    }
    assert dimensions["ordered_at"]["type"] == "time"
    assert dimensions["status"]["description"] == "Lifecycle status."
    # A measure column is a measure, not also a dimension.
    assert "order_total" not in dimensions
    assert body["measures"] == [
        {
            "name": "order_total",
            "sql": "{CUBE}.order_total",
            "type": "sum",
            "description": "Order value.",
            "meta": {
                "dex": {
                    "unit": "currency:EUR",
                    "additivity": "additive",
                    "role": "measure",
                }
            },
        }
    ]
    assert body["joins"] == [
        {
            "name": "customers",
            "sql": "{CUBE}.customer_id = {customers}.id",
            "relationship": "many_to_one",
        }
    ]
    # The semi-additive balance has no exact Cube equivalent and is left out.
    assert any("balance" in lim for lim in model["limitations"])
    assert "columns.balance.aggregation" in model["not_carried"]
    for field in (
        "grain",
        "columns.customer_id.references",
        "columns.order_total.unit",
    ):
        assert field in model["carried"]


def test_a_population_is_metadata_and_a_sentence_never_a_filter(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=False)

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    body = _cube(model)
    assert "segments" not in body
    assert all("filters" not in m for m in body.get("measures", []))
    assert body["meta"]["dex"]["population"] == POPULATED["population"]
    assert body["meta"]["dex"]["assumptions"] == POPULATED["assumptions"]
    assert body["description"] == (
        "One row per order. Population: Paid and shipped orders; status is one of "
        "'paid' or 'shipped'."
    )
    assert "population" in model["carried"]
    assert "assumptions" in model["carried"]


def test_a_configured_path_is_included_as_a_companion_plan_and_both_apply(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=True)

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope["errors"]
    proposal = _proposed(envelope)
    assert proposal["mode"] == "included"
    assert proposal["models"]["fct_orders"]["status"] == "included"
    assert proposal["models"]["fct_orders"]["validation"]["plan"]["result"] == "passed"
    companion = proposal["plan_id"]
    dbt_plan = envelope["data"]["plan_id"]
    assert companion != dbt_plan
    assert (
        companion in envelope["data"]["next"] and dbt_plan in envelope["data"]["next"]
    )
    plans = {p["plan_id"]: p for p in _stored_plans(project)}
    assert [e["path"] for e in plans[companion]["edits"]] == [FCT]
    assert FCT not in [e["path"] for e in plans[dbt_plan]["edits"]]

    for plan_id in (dbt_plan, companion):
        rc, applied = run(capsys, project, "transform", "apply", plan_id)
        assert rc == 0, applied
    assert read(project, FCT) == proposal["models"]["fct_orders"]["content"]

    # It reads back through the layer's own catalog, keys and joins included.
    rc, listed = run(capsys, project, "explore", "semantic", "list")
    assert rc == 0, listed
    models = {m["name"]: m for m in listed["data"]["semantic_models"]}
    assert models["fct_orders"]["relation"] == "demo.main.fct_orders"
    assert "fct_orders.order_total" in {m["name"] for m in listed["data"]["metrics"]}

    from exmergo_dex_core.cube.project import CubeSemanticLayer

    definitions = CubeSemanticLayer(
        project, [CUSTOMERS_FILE, FCT]
    ).declared_definitions()
    rendered = json.dumps(definitions.model_dump(), default=str)
    assert "fct_orders" in rendered and "customer_id" in rendered


def test_two_marts_derived_together_join_within_the_batch(
    project: Path, capsys, no_warehouse
):
    dim = "model/dim_stores.yml"
    put(project, CUSTOMERS_FILE, dump(customers()))
    _configure(project, [CUSTOMERS_FILE, FCT, dim])
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
                "content": "select 1 as store_id, 'x' as store_name\n",
            },
        ],
        "declarations": [
            {
                "model": "dim_stores",
                "grain": ["store_id"],
                "columns": {
                    "store_id": {"role": "key"},
                    "store_name": {"role": "dimension"},
                },
            },
            declaration,
        ],
    }

    rc, envelope = run(
        capsys,
        project,
        "transform",
        "plan",
        "two marts",
        "--edits-file",
        _payload(project, body),
    )

    assert rc == 0, envelope["errors"]
    models = _proposed(envelope)["models"]
    assert models["dim_stores"]["status"] == "included", models["dim_stores"]
    assert models["fct_orders"]["status"] == "included", models["fct_orders"]
    joins = {j["name"] for j in _cube(models["fct_orders"])["joins"]}
    assert joins == {"customers", "dim_stores"}


def test_without_a_sibling_the_manifest_relation_is_used_and_named(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=False, sibling_table="elsewhere.raw.customers")
    target = project / "analytics" / "target"
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "metadata": {},
                "nodes": {
                    "model.dex_test.fct_orders": {
                        "resource_type": "model",
                        "name": "fct_orders",
                        "alias": "fct_orders",
                        "relation_name": '"dev"."dbt_dev"."fct_orders"',
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert _cube(model)["sql_table"] == "dev.dbt_dev.fct_orders"
    assert any("compiled dbt manifest" in n for n in model["notes"])


def test_without_a_sibling_or_a_manifest_the_bare_name_is_a_named_limitation(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=False, sibling_table="elsewhere.raw.customers")

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    body = _cube(model)
    assert body["sql_table"] == "fct_orders"
    assert any("bare table name" in lim for lim in model["limitations"])
    # No cube sits on the referenced staging model, so no join is invented.
    assert "joins" not in body
    assert any("no cube sits on stg_customers" in n for n in model["notes"])


def test_a_non_default_data_source_is_written_on_the_cube(
    project: Path, capsys, no_warehouse
):
    put(project, CUSTOMERS_FILE, dump(customers()))
    _configure(project, [CUSTOMERS_FILE], extra="")
    config = project / ".dex" / "config.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + "    data_source: warehouse\n",
        encoding="utf-8",
    )

    _rc, envelope = _plan(project, capsys)

    assert _cube(_proposed(envelope)["models"]["fct_orders"])["data_source"] == (
        "warehouse"
    )


def test_an_authored_cube_on_the_mart_is_never_overwritten(
    project: Path, capsys, no_warehouse
):
    authored = "model/orders.yml"
    put(project, CUSTOMERS_FILE, dump(customers()))
    put(project, authored, dump(cube("orders", sql_table="demo.main.fct_orders")))
    _configure(project, [CUSTOMERS_FILE, authored])

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert model["status"] == "skipped"
    assert "'orders'" in model["reason"]


def test_a_cube_name_another_table_holds_fails_rather_than_being_renamed(
    project: Path, capsys, no_warehouse
):
    put(project, CUSTOMERS_FILE, dump(customers()))
    put(project, FCT, dump(cube("fct_orders", sql_table="demo.main.orders_legacy")))
    _configure(project, [CUSTOMERS_FILE, FCT])

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert model["status"] == "failed"
    assert "already declared" in model["reason"]
    assert "plan_id" not in _proposed(envelope)


def test_an_unmappable_aggregation_is_named_not_guessed(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=False)
    declaration = copy.deepcopy(POPULATED)
    declaration["columns"]["order_total"]["aggregation"] = "median"

    _rc, envelope = _plan(project, capsys, declaration)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert "measures" not in _cube(model)
    assert "median" in model["not_carried"]["columns.order_total.aggregation"]


def test_under_dbt_nothing_cube_shaped_is_proposed(project: Path, capsys, no_warehouse):
    _rc, envelope = _plan(project, capsys)

    proposal = _proposed(envelope)
    assert proposal["vendor"] == "dbt"
    assert "cubes:" not in json.dumps(proposal)


# --- annotate: declarations on `semantic cube` -------------------------------------


def _cube_plan(root: Path, capsys, body: dict) -> tuple[int, dict[str, Any]]:
    return run(
        capsys,
        root,
        "semantic",
        "cube",
        "plan",
        "declare customers",
        "--edits-file",
        _payload(root, body, name="cube.json"),
    )


SHARED = "model/shared.yml"
SHARED_TEXT = (
    "# Hand-written; every byte outside `customers` stays.\n"
    "cubes:\n"
    "  # The customer dimension.\n"
    "  - name: customers\n"
    "    sqlTable: demo.main.stg_customers\n"
    "    dimensions:\n"
    "      - name: id\n"
    "        sql: id\n"
    "        type: number\n"
    "      - name: region\n"
    '        sql: "{CUBE}.region"   # quoted on purpose\n'
    "        type: string\n"
    "\n"
    "  # Orders keep their comments.\n"
    "  - name: orders\n"
    "    sql_table: demo.main.orders\n"
    "    dimensions:\n"
    "      - {name: order_id, sql: order_id, type: number, primary_key: true}\n"
)


def test_declarations_annotate_one_cube_and_leave_the_rest_of_the_file(
    tmp_path: Path, capsys, no_warehouse
):
    put(tmp_path, SHARED, SHARED_TEXT)
    _configure(tmp_path, [SHARED])

    rc, envelope = _cube_plan(
        tmp_path,
        capsys,
        {
            "declarations": [
                {
                    "model": "customers",
                    "description": "One row per customer.",
                    "grain": ["id"],
                    "population": {"rule": "Customers with an account"},
                    "columns": {
                        "region": {"role": "dimension", "description": "Sales region."}
                    },
                }
            ]
        },
    )

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["updated"] == ["customers"]
    assert envelope["data"]["unchanged"] == ["orders"]
    (stored,) = _stored_plans(tmp_path)
    (edit,) = stored["edits"]
    text = edit["new_content"]
    # Everything after the annotated cube is byte-identical, comments included.
    tail = SHARED_TEXT[SHARED_TEXT.index("\n  # Orders keep") :]
    assert text.endswith(tail)
    assert text.startswith("# Hand-written; every byte outside `customers` stays.\n")
    body = {c["name"]: c for c in yaml.safe_load(text)["cubes"]}["customers"]
    assert body["description"] == (
        "One row per customer. Population: Customers with an account."
    )
    assert body["meta"]["dex"]["population"] == {"rule": "Customers with an account"}
    # The author's camelCase spelling is kept, and the key went where it belongs.
    assert "sqlTable" in body and "sql_table" not in body
    dims = {d["name"]: d for d in body["dimensions"]}
    assert dims["id"]["primary_key"] is True
    assert dims["region"]["description"] == "Sales region."
    assert "segments" not in body


def test_a_declaration_with_nothing_new_keeps_the_bytes(
    tmp_path: Path, capsys, no_warehouse
):
    put(tmp_path, SHARED, SHARED_TEXT)
    _configure(tmp_path, [SHARED])

    rc, envelope = _cube_plan(
        tmp_path, capsys, {"declarations": [{"model": "orders", "grain": ["order_id"]}]}
    )

    assert rc != 0
    assert "nothing to plan" in " ".join(envelope["errors"])


def test_a_declaration_naming_no_cube_or_a_view_is_refused(
    tmp_path: Path, capsys, no_warehouse
):
    put(
        tmp_path,
        SHARED,
        SHARED_TEXT + "views:\n  - name: sales\n    cubes:\n      - join_path: orders\n"
        "        includes: '*'\n",
    )
    _configure(tmp_path, [SHARED])

    rc, envelope = _cube_plan(
        tmp_path, capsys, {"declarations": [{"model": "refunds", "grain": ["id"]}]}
    )
    assert rc != 0
    assert "no configured cube is named 'refunds'" in " ".join(envelope["errors"])

    rc, envelope = _cube_plan(
        tmp_path, capsys, {"declarations": [{"model": "sales", "grain": ["id"]}]}
    )
    assert rc != 0
    assert "is a view" in " ".join(envelope["errors"])
    assert not list((tmp_path / ".dex").glob("plans/*"))


def test_declarations_ride_beside_the_payloads_own_edits(
    tmp_path: Path, capsys, no_warehouse
):
    _configure(tmp_path, [CUSTOMERS_FILE])
    unkeyed = cube(
        "customers",
        sql_table="demo.main.stg_customers",
        dimensions=[{"name": "id", "sql": "id", "type": "number"}],
    )

    rc, envelope = _cube_plan(
        tmp_path,
        capsys,
        {
            "edits": [{"path": CUSTOMERS_FILE, "content": dump(unkeyed)}],
            "declarations": [{"model": "customers", "grain": ["id"]}],
        },
    )

    assert rc == 0, envelope["errors"]
    assert envelope["data"]["defined"] == ["customers"]
    (stored,) = _stored_plans(tmp_path)
    (body,) = yaml.safe_load(stored["edits"][0]["new_content"])["cubes"]
    assert body["dimensions"][0]["primary_key"] is True


# --- the shipped contract --------------------------------------------------------


class TestCubeDerivation(SemanticDerivationContract):
    """The derivation hook's promises, held by the Cube layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path
        put(tmp_path, CUSTOMERS_FILE, dump(customers()))

    def make_derivation_source(self) -> Any:
        from exmergo_dex_core.cube.project import CubeSemanticLayer
        from exmergo_dex_core.semantic_source import SemanticSourceContext

        return CubeSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"files": [CUSTOMERS_FILE, FCT]},
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
            documents={CUSTOMERS_FILE: read(self.root, CUSTOMERS_FILE)},
            project_identifiers={
                "stg_customers": "stg_customers",
                "fct_orders": "fct_orders",
            },
        )


def test_transform_declare_reports_the_companion_plan(
    project: Path, capsys, no_warehouse
):
    _layer(project, configure_fct=True)
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
    assert proposal["vendor"] == "cube"
    assert proposal["models"]["fct_orders"]["status"] == "included"
    assert proposal["plan_id"] in envelope["data"]["next"]


def test_an_included_cube_joining_a_proposed_one_is_held_back_with_it(
    project: Path, capsys, no_warehouse
):
    dim = "model/dim_stores.yml"
    put(project, CUSTOMERS_FILE, dump(customers()))
    # The fact's path is configured; the dimension it joins is not.
    _configure(project, [CUSTOMERS_FILE, FCT])
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
        "declarations": [
            {"model": "dim_stores", "grain": ["store_id"], "columns": {}},
            declaration,
        ],
    }

    rc, envelope = run(
        capsys,
        project,
        "transform",
        "plan",
        "two marts",
        "--edits-file",
        _payload(project, body),
    )

    assert rc == 0, envelope["errors"]
    proposal = _proposed(envelope)
    models = proposal["models"]
    assert models["dim_stores"]["status"] == "proposed"
    assert models["fct_orders"]["status"] == "proposed", models["fct_orders"]
    assert any("dim_stores" in n for n in models["fct_orders"]["notes"])
    assert dim in models["fct_orders"]["next"]
    assert "plan_id" not in proposal
