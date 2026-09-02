"""Native Ossie catalog coverage, intentionally independent of MetricFlow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig
from exmergo_dex_core.explore.semantic.backend import resolve_backend
from exmergo_dex_core.ossie import catalog

_MODEL = """\
version: 0.2.0.dev0
semantic_model:
  - name: commerce
    datasets:
      - name: orders
        source: main.orders
        fields:
          - name: order_id
            expression: {dialects: [{dialect: ANSI_SQL, expression: order_id}]}
          - name: ordered_at
            datatype: Date
            dimension: {is_time: true}
            expression: {dialects: [{dialect: ANSI_SQL, expression: ordered_at}]}
      - name: customers
        source: main.customers
        fields:
          - name: id
            expression: {dialects: [{dialect: ANSI_SQL, expression: id}]}
    relationships:
      - name: orders_to_customers
        from: orders
        to: customers
        from_columns: [customer_id]
        to_columns: [id]
    metrics:
      - name: revenue
        description: Gross sales.
        expression: {dialects: [{dialect: ANSI_SQL, expression: SUM(orders.amount)}]}
"""


def test_ossie_catalog_reads_native_documents(tmp_path: Path):
    path = tmp_path / "commerce.ossie.yaml"
    path.write_text(_MODEL)
    view = catalog(tmp_path, [path.name], "duckdb")

    assert [model.name for model in view.semantic_models] == [
        "commerce.orders",
        "commerce.customers",
    ]
    assert [metric.name for metric in view.metrics] == ["revenue"]
    assert view.metrics[0].semantic_models == ["commerce.orders"]
    assert view.physical_columns["orders__order_id"] == ("main.orders", "order_id")
    assert (
        next(d for d in view.dimensions if d.name == "orders__ordered_at").type
        == "time"
    )


def test_ossie_config_requires_explicit_files():
    with pytest.raises(ValueError, match=r"semantic\.ossie\.files"):
        DexConfig(semantic={"vendor": "ossie"})


def test_ossie_config_rejects_hosted_dbt_coordinates():
    with pytest.raises(ValueError, match=r"semantic\.host"):
        DexConfig(
            semantic={
                "vendor": "ossie",
                "host": "semantic.example.test",
                "ossie": {"files": ["commerce.ossie.yaml"]},
            }
        )


def test_ossie_backend_refuses_generic_query(tmp_path: Path):
    path = tmp_path / "commerce.ossie.yaml"
    path.write_text(_MODEL)

    class Engine:
        repo_root = tmp_path
        connector = "duckdb"
        config = DexConfig(
            semantic={"vendor": "ossie", "ossie": {"files": [path.name]}}
        )
        semantic_source = None

    backend = resolve_backend(Engine())
    assert backend.list_definitions().metrics[0].name == "revenue"
    with pytest.raises(Exception, match="no portable query runtime"):
        backend.query(None)


def test_cli_lists_ossie_catalog_without_metricflow(tmp_path: Path, capsys):
    (tmp_path / "commerce.ossie.yaml").write_text(_MODEL)
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "semantic:\n  vendor: ossie\n  ossie:\n    files: [commerce.ossie.yaml]\n"
    )

    assert main(["--repo-root", str(tmp_path), "explore", "semantic", "list"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["data"]["vendor"] == "ossie"
    assert result["data"]["metrics"][0]["name"] == "revenue"
