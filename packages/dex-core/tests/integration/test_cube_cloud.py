"""Live runs against a Cube Cloud deployment (#914), both ways.

The deployment serves ``scripts/cube/model`` over an in-memory DuckDB built from
``scripts/cube/shop.sql`` (``scripts/setup_cube_cloud_ci.sh`` deploys it), and
dex reads a DuckDB file built from the same statements. So every question can
be asked three ways and has to come back the same:

- by hand, in SQL over dex's file;
- ``--api`` (the deployment's default): Cube Cloud executes over ``/v1/load``,
  and the result says no cost guard could apply;
- ``--local``: Cube Cloud renders over ``/v1/sql`` and dex executes on its own
  file, held to the reviewed model files by the stale-renderer check.

Needs ``DEX_TEST_CUBE_CLOUD_URL`` (the deployment's REST API base, ending in
``/cubejs-api``) and ``CUBE_API_TOKEN`` (a deployment API token), plus the
``[cube,semantic-api,duckdb]`` extras.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from cube_vendor.cube_cases import MODEL, MODEL_FILES, QUERIES, VALUES, write_shop

from exmergo_dex_core.cli import main

from .conftest import assert_ok

pytestmark = [pytest.mark.integration, pytest.mark.cube_cloud]

# The question each case asks, by hand, over dex's own file.
BY_HAND = {
    "by_status": "SELECT status, sum(total), count(id) FROM app.orders GROUP BY 1",
    "by_month": (
        "SELECT date_trunc('month', timezone('UTC', ordered_at)), sum(total)"
        " FROM app.orders GROUP BY 1"
    ),
    "filtered": (
        "SELECT status, count(id), sum(CASE WHEN status = 'paid' THEN total END)"
        " FROM app.orders WHERE status IN ('paid', 'shipped') GROUP BY 1"
    ),
    "two_hops": (
        "SELECT p.category, o.status, sum(i.quantity), sum(i.quantity * i.unit_price)"
        " FROM app.order_items i LEFT JOIN app.orders o ON i.order_id = o.id"
        " LEFT JOIN app.products p ON i.product_id = p.id GROUP BY 1, 2"
    ),
    "view": "SELECT status, sum(total) FROM app.orders GROUP BY 1",
}


def _run(capsys, root: Path, *argv: str) -> tuple[int, dict]:
    rc = main(["--repo-root", str(root), *argv])
    return rc, json.loads(capsys.readouterr().out)


def _argv(case: str) -> list[str]:
    spec = QUERIES[case]
    argv = ["explore", "semantic", "query", *spec["metrics"]]
    for token in spec.get("group_by", []):
        argv += ["--group-by", token]
    for clause in spec.get("where", []):
        argv += ["--where", clause]
    for token in spec.get("order_by", []):
        argv.append(f"--order-by={token}")
    if spec.get("grain"):
        argv += ["--grain", spec["grain"]]
    if spec.get("limit"):
        argv += ["--limit", str(spec["limit"])]
    return argv


def _repo(root: Path, url: str) -> Path:
    shutil.copytree(MODEL, root / "model")
    write_shop(root / "shop.duckdb")
    config = {
        "connector": "duckdb",
        "duckdb": {"path": str(root / "shop.duckdb")},
        "semantic": {
            "vendor": "cube",
            "deployment": "cube_cloud",
            "cube": {"url": url, "files": [f"model/{n}" for n in MODEL_FILES]},
        },
    }
    (root / ".dex").mkdir(exist_ok=True)
    (root / ".dex" / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def _rounded(rows) -> list[tuple]:
    def one(value):
        if value is None:
            return value
        if isinstance(value, str):
            return value[:10] if value[:4].isdigit() and "-" in value else value
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return str(value)[:10]

    return sorted(tuple(one(v) for v in row) for row in rows)


@pytest.fixture
def cloud(tmp_path: Path) -> Path:
    return _repo(tmp_path, os.environ["DEX_TEST_CUBE_CLOUD_URL"])


def _by_hand(root: Path, sql: str):
    import duckdb

    conn = duckdb.connect(str(root / "shop.duckdb"), read_only=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_cube_cloud_answers_every_case_both_ways(cloud, capsys):
    for case, sql in BY_HAND.items():
        want = _rounded(_by_hand(cloud, sql))

        rc, api = _run(capsys, cloud, *_argv(case))
        data = assert_ok(rc, api)["data"]
        assert (data["deployment"], data["execution"]) == ("cube_cloud", "vendor")
        assert data["definition_source"] == "cube_cloud"
        assert len([w for w in api["warnings"] if "cost guard" in w]) == 1, case
        assert _rounded(data["cells"]) == want, case

        rc, local = _run(capsys, cloud, *_argv(case), "--local")
        data = assert_ok(rc, local)["data"]
        assert (data["deployment"], data["execution"]) == ("cube_cloud", "dex")
        assert not [w for w in local["warnings"] if "cost guard" in w], case
        assert _rounded(data["cells"]) == want, case


def test_cube_cloud_values_both_ways(cloud, capsys):
    for case, (dimension, metrics) in VALUES.items():
        argv = ["explore", "semantic", "values", dimension]
        for metric in metrics:
            argv += ["--metric", metric]
        rc, api = _run(capsys, cloud, *argv)
        rc_local, local = _run(capsys, cloud, *argv, "--local")
        assert _rounded(assert_ok(rc, api)["data"]["cells"]) == _rounded(
            assert_ok(rc_local, local)["data"]["cells"]
        ), case


def test_cube_cloud_lists_its_compiled_model(cloud, capsys):
    rc, envelope = _run(capsys, cloud, "explore", "semantic", "list")
    data = assert_ok(rc, envelope)["data"]

    assert {m["name"] for m in data["semantic_models"]} >= {
        "orders",
        "customers",
        "order_items",
        "order_facts",
    }
    assert "relation" in data["unavailable"]["semantic_models"]
    assert any("not checked against" in w for w in envelope["warnings"])


def test_the_cloud_token_never_reaches_an_envelope(cloud, capsys):
    outputs = [
        _run(capsys, cloud, "explore", "semantic", "list")[1],
        _run(capsys, cloud, *_argv("by_status"))[1],
        _run(capsys, cloud, *_argv("by_status"), "--local")[1],
    ]

    assert os.environ["CUBE_API_TOKEN"] not in json.dumps(outputs)


def test_a_host_outside_cube_cloud_never_receives_the_token(tmp_path, capsys):
    root = _repo(tmp_path, "https://cube.example.com/cubejs-api")
    _, envelope = _run(capsys, root, *_argv("by_status"))

    assert envelope["status"] == "error"
    assert "not a Cube Cloud host" in envelope["errors"][0]
    assert os.environ["CUBE_API_TOKEN"] not in json.dumps(envelope)
