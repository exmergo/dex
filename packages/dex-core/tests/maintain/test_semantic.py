"""maintain semantic: impact analysis over definitions, references, and
dimension cardinality. No dimension value ever reaches the envelope."""

from __future__ import annotations

import json

from .conftest import SEMANTIC_YAML

SEMANTIC_PATH = "models/marts/orders_semantic.yml"


def test_clean_project_reports_no_semantic_drift(maintain_repo):
    maintain_repo.snapshot()
    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 0 and payload["status"] == "ok"
    assert payload["data"]["finding_count"] == 0


def test_definition_change_and_churn_are_reported(maintain_repo):
    maintain_repo.snapshot()
    changed = SEMANTIC_YAML.replace("label: Revenue", "label: Gross revenue").replace(
        "  - name: order_volume\n"
        "    label: Order volume\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: order_count\n",
        "  - name: orders_shipped\n"
        "    label: Orders shipped\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: order_count\n",
    )
    maintain_repo.edit(SEMANTIC_PATH, changed)

    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 0 and payload["status"] == "ok"
    by_code = {}
    for finding in payload["data"]["findings"]:
        by_code.setdefault(finding["code"], []).append(finding)

    changed_names = {f["data"]["name"] for f in by_code["definition_changed"]}
    assert changed_names == {"revenue"}
    assert {f["data"]["name"] for f in by_code["definition_added"]} == {
        "orders_shipped"
    }
    assert {f["data"]["name"] for f in by_code["definition_removed"]} == {
        "order_volume"
    }


def test_dangling_model_reference_when_model_is_deleted(maintain_repo):
    maintain_repo.snapshot()
    (maintain_repo.project_dir / "models" / "staging" / "stg_orders.sql").unlink()

    _rc, payload = maintain_repo.dex("maintain", "semantic")
    dangling = [
        f for f in payload["data"]["findings"] if f["code"] == "dangling_reference"
    ]
    assert any(
        f["data"].get("missing_model") == "stg_orders"
        and f["impacted_metrics"] == ["order_volume", "revenue"]
        for f in dangling
    )


def test_dangling_column_reference_when_dimension_column_drops(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql("ALTER TABLE stg_orders DROP status")

    _rc, payload = maintain_repo.dex("maintain", "semantic")
    dangling = [
        f for f in payload["data"]["findings"] if f["code"] == "dangling_reference"
    ]
    assert len(dangling) == 1
    finding = dangling[0]
    assert finding["identifier"] == "warehouse.main.stg_orders"
    assert finding["column"] == "status"
    assert finding["severity"] == "high"
    assert finding["data"] == {
        "semantic_model": "orders",
        "role": "dimension",
        "name": "status",
    }
    # A broken dimension breaks the whole semantic model, so all its metrics.
    assert finding["impacted_metrics"] == ["order_volume", "revenue"]
    assert finding["impacted_models"] == ["stg_orders"]


def test_new_categorical_value_is_a_cardinality_delta_never_a_value(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.sql(
        "INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', DATE '2024-03-01')"
    )

    _rc, payload = maintain_repo.dex("maintain", "semantic")
    findings = [
        f
        for f in payload["data"]["findings"]
        if f["code"] == "dimension_cardinality_changed"
    ]
    assert len(findings) == 1
    finding = findings[0]
    assert finding["identifier"] == "warehouse.main.stg_orders"
    assert finding["column"] == "status"
    assert finding["data"]["distinct_before"] == 5
    assert finding["data"]["distinct_after"] == 6
    assert finding["data"]["dimension"] == "status"
    assert "widened" in finding["detail"]
    assert finding["impacted_metrics"] == ["order_volume", "revenue"]

    # The whole point of cardinality-delta detection: the new value itself
    # never reaches the envelope (or `.dex/`); naming it is a job for a
    # firewalled `explore query` if the user asks.
    assert "refunded" not in json.dumps(payload)


def test_semantic_needs_a_dbt_project(maintain_repo):
    maintain_repo.snapshot()
    (maintain_repo.project_dir / "dbt_project.yml").unlink()

    rc, payload = maintain_repo.dex("maintain", "semantic")
    assert rc == 1 and payload["status"] == "error"
    assert "dbt project" in payload["errors"][0]


def test_scope_by_semantic_name(maintain_repo):
    maintain_repo.snapshot()
    maintain_repo.edit(
        SEMANTIC_PATH, SEMANTIC_YAML.replace("label: Revenue", "label: Gross")
    )
    maintain_repo.sql("INSERT INTO stg_orders VALUES (999, 1, 5.0, 'refunded', NULL)")

    _rc, payload = maintain_repo.dex("maintain", "semantic", "status")
    codes = {f["code"] for f in payload["data"]["findings"]}
    assert codes == {"dimension_cardinality_changed"}
