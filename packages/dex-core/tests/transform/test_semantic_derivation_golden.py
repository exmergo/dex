"""The MetricFlow derivation's exact output, pinned (#927).

The derivation moved behind the per-vendor hook (`SemanticDerivationSource`).
These goldens were first recorded against the derivation as #499 shipped it,
and the move passed against them unchanged, so the move changed no byte of what
a caller sees: the proposal, the warnings that speak of it, and the file it
splices into the plan. They were then re-recorded once, deliberately, for
#927's own output change (population, assumptions, units and NULL rules carried
as `config.meta.dex`, the population sentence in the description, `vendor`,
and per-model `carried` / `not_carried`). A later deliberate change re-records
them with ``DEX_UPDATE_GOLDENS=1`` and says why in the same change.

dbt's parse is faked so the goldens hold on any machine; the parse itself is
covered by `test_semantic_derivation.py`.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
from transform.test_semantic_derivation import (
    LAYER,
    MART,
    MART_SQL,
    _add_layer,
    _add_time_spine,
    _fake_parse,
    _payload,
    _run,
    _stored_content,
)

GOLDENS = Path(__file__).parent / "goldens" / "semantic_derivation.json"

CUSTOMERS_MART_SQL = "select 1 as customer_id, 'eu' as region, 3 as order_count\n"

CUSTOMERS_MART = {
    "model": "dim_customers",
    "description": "One row per customer.",
    "grain": ["customer_id"],
    "columns": {
        "customer_id": {"role": "key", "description": "The customer."},
        "region": {"role": "dimension"},
        "order_count": {"role": "measure", "aggregation": "count"},
    },
}


def _with_population() -> dict[str, Any]:
    declared = copy.deepcopy(MART)
    declared["population"] = {
        "rule": "Paid and shipped orders, test accounts removed",
        "filters": [{"column": "status", "includes": ["paid", "shipped"]}],
    }
    declared["assumptions"] = [
        {
            "decision": "what counts as an order",
            "chosen": "paid or shipped",
            "evidence": "request",
        }
    ]
    declared["columns"]["order_total"]["unit"] = "currency:EUR"
    declared["columns"]["status"]["null_rule"] = "never"
    return declared


def _composite() -> dict[str, Any]:
    declared = copy.deepcopy(MART)
    declared["grain"] = ["order_id", "customer_id"]
    return declared


def _plan(tmp_path: Path, capsys, marts: list[tuple[str, str, dict]]) -> dict:
    body = {
        "edits": [
            {"path": path, "kind": "model_sql", "content": sql}
            for path, sql, _declared in marts
        ],
        "declarations": [declared for _p, _s, declared in marts],
    }
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "transform",
            "plan",
            "orders mart",
            "--edits-file",
            _payload(tmp_path, body),
        ],
        capsys,
    )
    assert rc == 0, envelope
    return envelope


def _semantic_plan(tmp_path: Path, capsys) -> dict:
    """`semantic plan` declaring an existing mart, which derives too."""

    mart = tmp_path / "analytics" / "models" / "marts" / "fct_orders.sql"
    mart.parent.mkdir(parents=True, exist_ok=True)
    mart.write_text(MART_SQL, encoding="utf-8")
    rc, envelope = _run(
        [
            "--repo-root",
            str(tmp_path),
            "semantic",
            "plan",
            "declare the orders mart",
            "--edits-file",
            _payload(
                tmp_path,
                {
                    "edits": [
                        {
                            "path": "models/semantic/semantic.yml",
                            "content": LAYER + "# reviewed\n",
                        }
                    ],
                    "declarations": [MART],
                },
            ),
        ],
        capsys,
    )
    assert rc == 0, envelope["errors"]
    return envelope


def _observed(tmp_path: Path, envelope: dict) -> dict[str, Any]:
    data = envelope["data"]
    semantic_paths = sorted(
        p for p in data.get("paths", []) if "semantic" in p and p.endswith(".yml")
    )
    observed = {
        "semantic_proposal": data.get("semantic_proposal"),
        "warnings": [w for w in envelope["warnings"] if "semantic" in w],
        "semantic_files": {
            p: _stored_content(tmp_path, data["plan_id"], p) for p in semantic_paths
        },
    }
    return json.loads(json.dumps(observed).replace(str(tmp_path), "<tmp>"))


ORDERS = ("models/marts/fct_orders.sql", MART_SQL, MART)
CUSTOMERS = ("models/marts/dim_customers.sql", CUSTOMERS_MART_SQL, CUSTOMERS_MART)
COMPOSITE = ("models/marts/fct_orders.sql", MART_SQL, _composite())
POPULATED = ("models/marts/fct_orders.sql", MART_SQL, _with_population())


def _no_layer(project: Path) -> None:
    return None


def _layer_and_spine(project: Path) -> None:
    _add_time_spine(project)
    _add_layer(project)


def _layer_without_spine(project: Path) -> None:
    _add_layer(project)


def _authored_on_mart(project: Path) -> None:
    _add_time_spine(project)
    _add_layer(
        project,
        LAYER + "  - name: orders\n"
        "    model: ref('fct_orders')\n"
        "    entities:\n"
        "      - name: order\n"
        "        type: primary\n"
        "        expr: order_id\n",
    )


def _measure_collision(project: Path) -> None:
    _add_time_spine(project)
    _add_layer(
        project,
        LAYER + "    measures:\n"
        "      - name: order_total\n"
        "        agg: count\n"
        "        expr: id\n",
    )


def _vendor_ossie(project: Path) -> None:
    dex = project.parent / ".dex"
    dex.mkdir(exist_ok=True)
    (dex / "config.yml").write_text(
        "connector: duckdb\n"
        "semantic:\n"
        "  vendor: ossie\n"
        "  ossie:\n"
        "    files: [semantic/orders.ossie.yaml]\n",
        encoding="utf-8",
    )


SCENARIOS: dict[str, tuple[Any, list, dict]] = {
    "no_layer_proposed": (_no_layer, [ORDERS], {}),
    "layer_included": (_layer_and_spine, [ORDERS], {}),
    "two_marts_included_in_one_file": (_layer_and_spine, [ORDERS, CUSTOMERS], {}),
    "layer_without_spine": (_layer_without_spine, [ORDERS], {}),
    "authored_model_skipped": (_authored_on_mart, [ORDERS], {}),
    "measure_name_collision": (_measure_collision, [ORDERS], {}),
    "composite_grain": (_layer_and_spine, [COMPOSITE], {}),
    "parse_fails": (_layer_and_spine, [ORDERS], {"fails_with_derivation": True}),
    "dbt_unavailable": (_layer_and_spine, [ORDERS], {"available": False}),
    "vendor_ossie": (_vendor_ossie, [ORDERS], {}),
    # #927: population and the other declared metadata, carried as metadata.
    "population_carried": (_layer_and_spine, [POPULATED], {}),
}


def _golden() -> dict[str, Any]:
    if not GOLDENS.is_file():
        return {}
    return json.loads(GOLDENS.read_text(encoding="utf-8"))


def _check(name: str, observed: dict[str, Any]) -> None:
    goldens = _golden()
    if os.environ.get("DEX_UPDATE_GOLDENS") == "1":
        goldens[name] = observed
        GOLDENS.parent.mkdir(parents=True, exist_ok=True)
        GOLDENS.write_text(
            json.dumps(goldens, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return
    assert name in goldens, f"no golden for {name}; record with DEX_UPDATE_GOLDENS=1"
    assert observed == goldens[name]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_transform_plan_derivation_matches_its_golden(
    name: str, dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    arrange, marts, parse = SCENARIOS[name]
    arrange(dbt_project_dir)
    _fake_parse(monkeypatch, **parse)

    envelope = _plan(tmp_path, capsys, marts)

    _check(name, _observed(tmp_path, envelope))


def test_semantic_plan_derivation_matches_its_golden(
    dbt_project_dir: Path, tmp_path: Path, capsys, monkeypatch
):
    _layer_and_spine(dbt_project_dir)
    _fake_parse(monkeypatch)

    envelope = _semantic_plan(tmp_path, capsys)

    _check("semantic_plan_included", _observed(tmp_path, envelope))
