"""`explore profile --use-project` reads project claims at output time (#926).

A cached profile and a rescan of unchanged data have to carry the same
declaration notes, and `semantic_models` has to come from the layer this run
read. Neither may depend on which command last wrote `.dex/cache.json`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cli import main

_SCHEMA = (
    "version: 2\n"
    "models:\n"
    "  - name: orders\n"
    "    columns:\n"
    "      - name: status\n"
    "        tests: [unique]\n"
)

_PRIMARY_NOTE = (
    "grain order_code comes from the project's declared primary entity "
    "(heuristic suggested id)"
)
_UNIQUE_NOTE = (
    "status is declared unique in the dbt project but profiling found duplicates"
)


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    duckdb = pytest.importorskip("duckdb")
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
    (repo / "models" / "schema.yml").write_text(_SCHEMA, encoding="utf-8")
    _write_manifest(repo, present=True)
    return db, repo


def _write_manifest(repo: Path, *, present: bool) -> None:
    models = []
    if present:
        models.append(
            {
                "name": "orders_sm",
                "node_relation": {
                    "alias": "orders",
                    "relation_name": '"wh"."main"."orders"',
                },
                "entities": [
                    {"name": "order", "type": "primary", "expr": "order_code"}
                ],
                "dimensions": [],
                "measures": [],
            }
        )
    (repo / "target" / "semantic_manifest.json").write_text(
        json.dumps({"semantic_models": models, "metrics": []}),
        encoding="utf-8",
    )


def _args(db: Path, repo: Path, *extra: str) -> list[str]:
    return [
        "explore",
        "profile",
        "orders",
        *extra,
        "--path",
        str(db),
        "--repo-root",
        str(repo),
    ]


def _orders(payload: dict, key: str = "datasets") -> dict:
    return next(d for d in payload["data"][key] if d["identifier"].endswith(".orders"))


def _cached_orders(repo: Path) -> dict:
    cache = json.loads((repo / ".dex" / "cache.json").read_text(encoding="utf-8"))
    return next(d for d in cache["datasets"] if d["identifier"].endswith(".orders"))


def test_cached_use_project_matches_refresh(tmp_path: Path, capsys):
    db, repo = _repo(tmp_path)
    _run(_args(db, repo), capsys)

    cached = _run(_args(db, repo, "--use-project"), capsys)
    assert cached["data"]["cache_hit_count"] == 1
    refreshed = _run(_args(db, repo, "--use-project", "--refresh"), capsys)
    assert refreshed["data"]["profiled_count"] == 1

    hit = _orders(cached)
    fresh = _orders(refreshed)
    assert hit["grain"] == ["order_code"]
    assert hit["semantic_models"] == ["orders_sm"]
    assert _PRIMARY_NOTE in hit["data_quality"]
    assert _UNIQUE_NOTE in hit["data_quality"]
    assert hit["data_quality"] == fresh["data_quality"]
    assert hit["grain"] == fresh["grain"]
    assert hit["semantic_models"] == fresh["semantic_models"]

    stored = _cached_orders(repo)
    assert stored["grain"] == ["id"]
    assert stored["semantic_models"] == []
    assert _PRIMARY_NOTE not in stored["data_quality"]
    assert _UNIQUE_NOTE not in stored["data_quality"]


def test_fresh_profile_reports_semantic_models_before_map(tmp_path: Path, capsys):
    db, repo = _repo(tmp_path)
    payload = _run(_args(db, repo, "--use-project"), capsys)
    assert payload["data"]["cache_hit_count"] == 0
    assert _orders(payload)["semantic_models"] == ["orders_sm"]
    assert _cached_orders(repo)["semantic_models"] == []


def test_removing_claims_clears_a_cache_hit(tmp_path: Path, capsys):
    db, repo = _repo(tmp_path)
    first = _orders(_run(_args(db, repo, "--use-project"), capsys))
    assert first["grain"] == ["order_code"]
    assert first["semantic_models"] == ["orders_sm"]
    assert _PRIMARY_NOTE in first["data_quality"]
    assert _UNIQUE_NOTE in first["data_quality"]

    (repo / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: orders\n", encoding="utf-8"
    )
    _write_manifest(repo, present=False)
    again = _run(_args(db, repo, "--use-project"), capsys)
    assert again["data"]["cache_hit_count"] == 1
    orders = _orders(again)
    assert orders["grain"] == ["id"]
    assert orders["semantic_models"] == []
    assert _PRIMARY_NOTE not in orders["data_quality"]
    assert _UNIQUE_NOTE not in orders["data_quality"]


def test_profile_after_map_drops_a_removed_claim_without_refresh(
    tmp_path: Path, capsys
):
    db, repo = _repo(tmp_path)
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
    assert _PRIMARY_NOTE in _cached_orders(repo)["data_quality"]
    assert _orders(mapped, "objects")["semantic_models"] == ["orders_sm"]

    (repo / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: orders\n", encoding="utf-8"
    )
    _write_manifest(repo, present=False)
    profiled = _run(_args(db, repo, "--use-project"), capsys)
    assert profiled["data"]["cache_hit_count"] == 1
    orders = _orders(profiled)
    assert orders["grain"] == ["id"]
    assert orders["semantic_models"] == []
    assert _PRIMARY_NOTE not in orders["data_quality"]
    assert _UNIQUE_NOTE not in orders["data_quality"]


def test_map_after_profile_keeps_the_declared_grain(tmp_path: Path, capsys):
    db, repo = _repo(tmp_path)
    _run(_args(db, repo, "--use-project"), capsys)
    assert _cached_orders(repo)["grain"] == ["id"]

    _run(
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
    stored = _cached_orders(repo)
    assert stored["grain"] == ["order_code"]
    assert _PRIMARY_NOTE in stored["data_quality"]
