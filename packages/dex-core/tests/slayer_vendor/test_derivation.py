"""A SLayer model derived from a declared mart, and declarations on `semantic
slayer` (#927).

The SLayer implementation of the derivation hook: a dbt mart under
`transform.marts_paths` whose YAML declares it becomes a SLayer model file,
included as a plan of its own beside the mart's where its path is already on the
layer's write surface, proposed otherwise. Nothing here opens SLayer storage or
a warehouse.
"""

from __future__ import annotations

import copy
import json
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

from slayer_vendor.test_authoring import (
    BASE,
    CUSTOMERS,
    customers,
    dump,
    model,
    put,
    read,
    run,
)
from transform.test_semantic_derivation import (
    MART,
    MART_SQL,
    _fake_parse,
    _payload,
)

from exmergo_dex_core.semantic_source import (
    SemanticDerivationSource,
)
from exmergo_dex_core.slayer.loader import FORMAT_VERSION


@pytest.fixture
def no_storage_or_warehouse(monkeypatch):
    """Fail the test if SLayer storage is opened or a warehouse is reached."""

    from slayer.storage import yaml_storage

    def refuse(*_args, **_kwargs):
        raise AssertionError("SLayer storage was opened")

    monkeypatch.setattr(yaml_storage.YAMLStorage, "__init__", refuse)

    def no_connection(*_args, **_kwargs):
        raise AssertionError("a warehouse connection was opened")

    monkeypatch.setattr("exmergo_dex_core.connect.open_adapter", no_connection)


FCT = "models/wh/fct_orders.yaml"
SQL_PATH = "models/marts/fct_orders.sql"

POPULATED = {
    **MART,
    "population": {
        "rule": "Paid and shipped orders",
        "filters": [{"column": "status", "includes": ["paid", "shipped"]}],
    },
    "columns": {
        **MART["columns"],
        "order_total": {**MART["columns"]["order_total"], "unit": "currency:EUR"},
    },
}


def _configure(root: Path, models: list[str], *, extra: str = "") -> None:
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(
        "connector: duckdb\nsemantic:\n  vendor: slayer\n  slayer:\n"
        f"    base_dir: {BASE}\n    models:\n"
        + "".join(f"      - {name}\n" for name in models)
        + extra,
        encoding="utf-8",
    )


def _layer(root: Path, *, configure_fct: bool, sibling_table: str) -> None:
    """A SLayer layer with a `customers` model on the dbt project's staging model."""

    put(root, CUSTOMERS, dump(customers(sql_table=sibling_table)))
    _configure(root, [CUSTOMERS, *([FCT] if configure_fct else [])])


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


def _stored_plans(root: Path) -> list[dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / ".dex" / "plans").glob("*.json"))
    ]


@pytest.fixture
def project(dbt_project_dir: Path, tmp_path: Path, monkeypatch) -> Path:
    _fake_parse(monkeypatch)
    return tmp_path


# --- the capability --------------------------------------------------------------


def test_the_slayer_layer_implements_the_derivation_hook(tmp_path: Path):
    from exmergo_dex_core.semantic_source import SemanticSourceContext
    from exmergo_dex_core.slayer.project import SlayerSemanticLayer

    layer = SlayerSemanticLayer.from_context(
        SemanticSourceContext(
            repo_root=str(tmp_path),
            connector="duckdb",
            options={"base_dir": BASE, "models": [CUSTOMERS]},
        )
    )
    assert isinstance(layer, SemanticDerivationSource)


# --- derive: a declared mart becomes a SLayer model ------------------------------


def test_a_mart_whose_path_is_not_configured_is_proposed_with_how_to_adopt_it(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=False, sibling_table="demo.main.stg_customers")

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope
    proposal = _proposed(envelope)
    assert proposal["vendor"] == "slayer"
    assert proposal["mode"] == "proposed"
    assert "plan_id" not in proposal
    model = proposal["models"]["fct_orders"]
    assert model["status"] == "proposed", model
    assert model["validation"]["slayer"]["result"] == "passed"
    assert model["validation"]["catalog"]["result"] == "passed"
    assert "dex config set semantic.slayer.models" in proposal["next"]
    assert FCT in proposal["next"]
    (edit,) = proposal["definitions"]["edits"]
    assert edit == {
        "path": FCT,
        "kind": "semantic_document",
        "content": edit["content"],
    }
    # Only the dbt plan is stored: nothing SLayer-shaped is written or planned.
    (stored,) = _stored_plans(project)
    assert all(not e["path"].endswith(".yaml") for e in stored["edits"])
    assert not (project / BASE / FCT).exists()


def test_the_derived_model_states_what_the_declaration_says(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=False, sibling_table="demo.main.stg_customers")

    _rc, envelope = _plan(project, capsys)

    content = _proposed(envelope)["models"]["fct_orders"]["content"]
    body = yaml.safe_load(content)
    assert body["version"] == FORMAT_VERSION
    assert body["name"] == "fct_orders"
    assert body["data_source"] == "wh"
    # The database and schema the sibling model already reads.
    assert body["sql_table"] == "demo.main.fct_orders"
    assert body["default_time_dimension"] == "ordered_at"
    columns = {c["name"]: c for c in body["columns"]}
    assert columns["order_id"]["primary_key"] is True
    assert columns["ordered_at"]["type"] == "DATE"
    assert columns["order_total"]["meta"] == {
        "dex": {"unit": "currency:EUR", "additivity": "additive", "role": "measure"}
    }
    assert body["joins"] == [
        {
            "target_model": "customers",
            "join_pairs": [["customer_id", "id"]],
            "cardinality": "many_to_one",
        }
    ]
    # One saved measure per declared aggregation, named <column>_<agg>; the
    # semi-additive balance has no exact SLayer equivalent and is left out.
    assert body["measures"] == [
        {"name": "order_total_sum", "formula": "order_total:sum"}
    ]
    model = _proposed(envelope)["models"]["fct_orders"]
    assert any("balance" in lim and ":last" in lim for lim in model["limitations"])
    assert "columns.balance.aggregation" in model["not_carried"]


def test_a_population_is_metadata_and_a_sentence_never_a_filter(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=False, sibling_table="demo.main.stg_customers")

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    body = yaml.safe_load(model["content"])
    assert "filters" not in body
    assert all("filter" not in c for c in body["columns"])
    assert body["meta"]["dex"]["population"] == POPULATED["population"]
    assert body["description"] == (
        "One row per order. Population: Paid and shipped orders; status is one of "
        "'paid' or 'shipped'."
    )
    assert "population" in model["carried"]


def test_a_configured_path_is_included_as_a_companion_plan_and_both_apply(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=True, sibling_table="demo.main.stg_customers")

    rc, envelope = _plan(project, capsys)

    assert rc == 0, envelope
    proposal = _proposed(envelope)
    assert proposal["mode"] == "included"
    assert proposal["models"]["fct_orders"]["status"] == "included"
    companion = proposal["plan_id"]
    dbt_plan = envelope["data"]["plan_id"]
    assert companion != dbt_plan
    assert companion in envelope["data"]["next"]
    assert dbt_plan in envelope["data"]["next"]
    # The dbt plan holds the mart; the SLayer plan holds the model file alone.
    plans = {p["plan_id"]: p for p in _stored_plans(project)}
    assert [e["path"] for e in plans[companion]["edits"]] == [FCT]
    assert plans[companion].get("edit_target") == "semantic"
    assert FCT not in [e["path"] for e in plans[dbt_plan]["edits"]]

    for plan_id in (dbt_plan, companion):
        rc, applied = run(capsys, project, "transform", "apply", plan_id)
        assert rc == 0, applied
    written = read(project, FCT)
    assert written == proposal["models"]["fct_orders"]["content"]

    # It reads back through the layer's own catalog.
    rc, listed = run(capsys, project, "explore", "semantic", "list")
    assert rc == 0, listed
    models = {m["name"]: m for m in listed["data"]["semantic_models"]}
    assert "fct_orders" in models
    assert "fct_orders.order_total_sum" in {
        m["name"] for m in listed["data"]["metrics"]
    }


def test_without_a_sibling_the_manifest_relation_is_used_and_named(
    project: Path, capsys, no_storage_or_warehouse
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
    assert yaml.safe_load(model["content"])["sql_table"] == "dev.dbt_dev.fct_orders"
    assert any("compiled dbt manifest" in n for n in model["notes"])


def test_without_a_sibling_or_a_manifest_the_bare_name_is_a_named_limitation(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=False, sibling_table="elsewhere.raw.customers")

    _rc, envelope = _plan(project, capsys)

    model = _proposed(envelope)["models"]["fct_orders"]
    assert yaml.safe_load(model["content"])["sql_table"] == "fct_orders"
    assert any("bare table name" in lim for lim in model["limitations"])
    # No SLayer model sits on the referenced staging model, so no join is
    # invented; the note says it is derived once one does.
    assert "joins" not in yaml.safe_load(model["content"])
    assert any("no SLayer model sits on stg_customers" in n for n in model["notes"])


def test_models_over_more_than_one_data_source_are_not_guessed_between(
    project: Path, capsys, no_storage_or_warehouse
):
    other = "models/lake/events.yaml"
    put(project, CUSTOMERS, dump(customers(sql_table="demo.main.stg_customers")))
    put(project, other, dump(model("events", data_source="lake")))
    _configure(project, [CUSTOMERS, other])

    _rc, envelope = _plan(project, capsys)

    model_data = _proposed(envelope)["models"]["fct_orders"]
    assert model_data["status"] == "failed"
    assert "more than one data source" in model_data["reason"]


def test_an_authored_model_on_the_mart_is_never_overwritten(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=False, sibling_table="demo.main.stg_customers")
    authored = "models/wh/orders.yaml"
    put(project, authored, dump(model("orders", sql_table="demo.main.fct_orders")))
    _configure(project, [CUSTOMERS, authored])

    _rc, envelope = _plan(project, capsys)

    model_data = _proposed(envelope)["models"]["fct_orders"]
    assert model_data["status"] == "skipped"
    assert "'orders'" in model_data["reason"]


def test_a_model_name_another_table_holds_fails_rather_than_being_renamed(
    project: Path, capsys, no_storage_or_warehouse
):
    put(project, CUSTOMERS, dump(customers(sql_table="demo.main.stg_customers")))
    put(project, FCT, dump(model("fct_orders", sql_table="demo.main.orders_legacy")))
    _configure(project, [CUSTOMERS, FCT])

    _rc, envelope = _plan(project, capsys)

    model_data = _proposed(envelope)["models"]["fct_orders"]
    assert model_data["status"] == "failed"
    assert "already declared" in model_data["reason"]
    assert "plan_id" not in _proposed(envelope)


def test_a_measure_name_its_own_columns_hold_fails(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=False, sibling_table="demo.main.stg_customers")
    declaration = copy.deepcopy(POPULATED)
    declaration["columns"]["order_total_sum"] = {"role": "attribute"}

    sql = MART_SQL.rstrip("\n") + ", 1 as order_total_sum\n"

    _rc, envelope = _plan(project, capsys, declaration, sql=sql)

    model_data = _proposed(envelope)["models"]["fct_orders"]
    assert model_data["status"] == "failed"
    assert "order_total_sum" in model_data["reason"]


def test_under_dbt_nothing_slayer_shaped_is_proposed(
    project: Path, capsys, no_storage_or_warehouse
):
    _rc, envelope = _plan(project, capsys)

    proposal = _proposed(envelope)
    assert proposal["vendor"] == "dbt"
    assert all(
        not d["path"].endswith(".yaml") for d in proposal.get("definitions") or []
    )


# --- annotate: declarations on `semantic slayer` ----------------------------------


def _slayer_plan(root: Path, capsys, body: dict) -> tuple[int, dict[str, Any]]:
    return run(
        capsys,
        root,
        "semantic",
        "slayer",
        "plan",
        "declare customers",
        "--edits-file",
        _payload(root, body, name="slayer.json"),
    )


def test_declarations_annotate_a_model_the_layer_already_defines(
    tmp_path: Path, capsys, no_storage_or_warehouse
):
    put(tmp_path, CUSTOMERS, dump(customers(), comment="# authored\n"))
    _configure(tmp_path, [CUSTOMERS])

    rc, envelope = _slayer_plan(
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

    assert rc == 0, envelope
    (stored,) = _stored_plans(tmp_path)
    (edit,) = stored["edits"]
    assert edit["path"] == CUSTOMERS
    body = yaml.safe_load(edit["new_content"])
    assert body["description"] == (
        "One row per customer. Population: Customers with an account."
    )
    assert body["meta"]["dex"]["population"] == {"rule": "Customers with an account"}
    region = next(c for c in body["columns"] if c["name"] == "region")
    assert region["description"] == "Sales region."
    assert "filters" not in body


def test_declarations_ride_beside_the_payloads_own_edits(
    tmp_path: Path, capsys, no_storage_or_warehouse
):
    _configure(tmp_path, [CUSTOMERS])

    rc, envelope = _slayer_plan(
        tmp_path,
        capsys,
        {
            "edits": [{"path": CUSTOMERS, "content": dump(customers())}],
            "declarations": [{"model": "customers", "grain": ["id"]}],
        },
    )

    assert rc == 0, envelope
    assert envelope["data"]["defined"] == ["customers"]


def test_a_declaration_naming_no_defined_model_is_refused(
    tmp_path: Path, capsys, no_storage_or_warehouse
):
    put(tmp_path, CUSTOMERS, dump(customers()))
    _configure(tmp_path, [CUSTOMERS])

    rc, envelope = _slayer_plan(
        tmp_path, capsys, {"declarations": [{"model": "orders", "grain": ["id"]}]}
    )

    assert rc != 0
    assert "orders" in " ".join(envelope["errors"])
    assert not list((tmp_path / ".dex").glob("plans/*"))


def test_a_malformed_declaration_is_refused_by_name(
    tmp_path: Path, capsys, no_storage_or_warehouse
):
    put(tmp_path, CUSTOMERS, dump(customers()))
    _configure(tmp_path, [CUSTOMERS])

    rc, envelope = _slayer_plan(
        tmp_path, capsys, {"declarations": [{"model": "customers", "grian": ["id"]}]}
    )

    assert rc != 0
    assert "grian is not a declaration field" in " ".join(envelope["errors"])


# --- the shipped contract --------------------------------------------------------

from exmergo_dex_core.semantic_source_conformance import (  # noqa: E402
    SemanticDerivationContract,
)


class TestSlayerDerivation(SemanticDerivationContract):
    """The derivation hook's promises, held by the SLayer layer."""

    @pytest.fixture(autouse=True)
    def _root(self, tmp_path: Path) -> None:
        self.root = tmp_path
        put(tmp_path, CUSTOMERS, dump(customers(sql_table="demo.main.stg_customers")))

    def make_derivation_source(self) -> Any:
        from exmergo_dex_core.semantic_source import SemanticSourceContext
        from exmergo_dex_core.slayer.project import SlayerSemanticLayer

        return SlayerSemanticLayer.from_context(
            SemanticSourceContext(
                repo_root=str(self.root),
                connector="duckdb",
                options={"base_dir": BASE, "models": [CUSTOMERS, FCT]},
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
            documents={CUSTOMERS: read(self.root, CUSTOMERS)},
            project_identifiers={
                "stg_customers": "stg_customers",
                "fct_orders": "fct_orders",
            },
        )


def test_transform_declare_reports_the_companion_plan(
    project: Path, capsys, no_storage_or_warehouse
):
    _layer(project, configure_fct=True, sibling_table="demo.main.stg_customers")
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

    assert rc == 0, envelope
    proposal = envelope["data"]["semantic_proposal"]
    assert proposal["models"]["fct_orders"]["status"] == "included"
    assert proposal["plan_id"] in envelope["data"]["next"]
