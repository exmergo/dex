"""Non-dbt semantic.vendor must not fold MetricFlow under --use-project (#922).

Under ossie|slayer|cube the vendor layer is the one semantic layer. A dbt
project that still carries MetricFlow models must not contribute
primary_entities grain or metric-backing rank to explore profile|map.
Under vendor: dbt the MetricFlow fold is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.dbt_project import ProjectDefinitions
from exmergo_dex_core.explore.commands import _drop_metricflow_under_native_vendor
from exmergo_dex_core.project_definitions import DeclaredKey

pytest.importorskip("duckdb")


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


def _mf_repo(tmp_path: Path) -> tuple[Path, Path]:
    """DuckDB warehouse + dbt project with MetricFlow grain and a metric."""

    import duckdb

    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE orders (id INTEGER, order_code INTEGER, status VARCHAR)")
    conn.execute(
        "INSERT INTO orders VALUES (1, 101, 'open'), (2, 102, 'open'), "
        "(3, 103, 'closed')"
    )
    conn.close()

    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "target").mkdir()
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: orders\n"
        "    columns:\n"
        "      - name: status\n"
        "        tests: [unique]\n",
        encoding="utf-8",
    )
    (repo / "target" / "semantic_manifest.json").write_text(
        json.dumps(
            {
                "semantic_models": [
                    {
                        "name": "orders_sm",
                        "node_relation": {
                            "alias": "orders",
                            "relation_name": '"wh"."main"."orders"',
                        },
                        "entities": [
                            {
                                "name": "order",
                                "type": "primary",
                                "expr": "order_code",
                            }
                        ],
                        "dimensions": [],
                        "measures": [{"name": "order_count", "agg": "count"}],
                    }
                ],
                "metrics": [
                    {
                        "name": "orders",
                        "type": "simple",
                        "type_params": {"input_measures": [{"name": "order_count"}]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return db, repo


def _orders_map(payload: dict) -> dict:
    return next(
        o for o in payload["data"]["objects"] if o["identifier"].endswith(".orders")
    )


def _orders_profile(payload: dict) -> dict:
    return next(
        d for d in payload["data"]["datasets"] if d["identifier"].endswith(".orders")
    )


def _notes(payload: dict) -> list[str]:
    return list(payload.get("notes") or []) + list(
        payload.get("data", {}).get("notes") or []
    )


@pytest.mark.parametrize("vendor", ["ossie", "slayer", "cube"])
def test_drop_helper_clears_metricflow_for_native_vendors(vendor: str):
    from types import SimpleNamespace

    engine = SimpleNamespace(
        config=SimpleNamespace(semantic=SimpleNamespace(vendor=vendor))
    )
    defs = ProjectDefinitions(
        present=True,
        primary_entities={"orders": "order_code"},
        metric_models=["orders"],
        semantic_source="manifest",
        declared_keys=[
            DeclaredKey(model="orders", column="status", unique=True, source="yaml")
        ],
    )
    cleaned = _drop_metricflow_under_native_vendor(engine, defs)
    assert cleaned.primary_entities == {}
    assert cleaned.metric_models == []
    assert cleaned.semantic_source is None
    # Transformation declarations stay.
    assert cleaned.declared_keys == defs.declared_keys


def test_drop_helper_leaves_dbt_vendor_unchanged():
    from types import SimpleNamespace

    engine = SimpleNamespace(
        config=SimpleNamespace(semantic=SimpleNamespace(vendor="dbt"))
    )
    defs = ProjectDefinitions(
        present=True,
        primary_entities={"orders": "order_code"},
        metric_models=["orders"],
        semantic_source="manifest",
    )
    assert _drop_metricflow_under_native_vendor(engine, defs) is defs


def test_vendor_dbt_still_folds_metricflow_grain_and_metric_rank(
    tmp_path: Path, capsys
):
    db, repo = _mf_repo(tmp_path)
    save_config(DexConfig(connector="duckdb", path=str(db)), repo)

    mapped = _run(
        [
            "explore",
            "map",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    orders = _orders_map(mapped)
    assert orders["grain"] == ["order_code"]
    assert (
        any(
            "back metric definitions" in n or "metric" in n.lower()
            for n in _notes(mapped)
        )
        or orders.get("rank_score") is not None
    )  # metric fold still ran

    profiled = _run(
        [
            "explore",
            "profile",
            "orders",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    hit = _orders_profile(profiled)
    assert hit["grain"] == ["order_code"]
    assert any("declared primary entity" in n for n in hit.get("data_quality") or [])


def test_ossie_vendor_drops_metricflow_grain_keeps_unique_test(tmp_path: Path, capsys):
    """Acceptance for ossie: MF grain/metric gone; unique-test declaration stays."""
    from tests.ossie.conftest import dataset, document, field, model, write

    db, repo = _mf_repo(tmp_path)
    # Ossie document over the same orders table with a different key (id).
    write(
        repo,
        "commerce.ossie.yaml",
        document(
            model(
                "commerce",
                dataset(
                    "orders",
                    "wh.main.orders",
                    field("id"),
                    field("order_code"),
                    field("status"),
                    primary_key=["id"],
                ),
            )
        ),
    )
    (repo / ".dex").mkdir(exist_ok=True)
    (repo / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        f"path: {db}\n"
        "semantic:\n"
        "  vendor: ossie\n"
        "  ossie:\n"
        "    files:\n"
        "      - commerce.ossie.yaml\n",
        encoding="utf-8",
    )

    mapped = _run(
        [
            "explore",
            "map",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    orders = _orders_map(mapped)
    # Must not be the MetricFlow primary entity.
    assert orders["grain"] != ["order_code"]
    # Ossie primary key (id) should win as declared grain when elected.
    # Heuristic also prefers id; either way order_code from MF is gone.
    assert "order_code" not in (orders.get("grain") or [])
    note_blob = " ".join(_notes(mapped))
    assert "back metric definitions" not in note_blob

    profiled = _run(
        [
            "explore",
            "profile",
            "orders",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    hit = _orders_profile(profiled)
    assert "order_code" not in (hit.get("grain") or [])
    # Unique test on status is a transformation declaration and stays.
    assert any("declared unique" in n for n in hit.get("data_quality") or [])
