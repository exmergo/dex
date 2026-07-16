"""Explore engine: inventory/rank, profile (+PII), relationships, and map.

These exercise the engine end to end through the CLI envelope (what the agent
actually sees), so they double as output-quality assertions for the sanitized
boundary: aggregates and flags only, never rows, never secrets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cache import DexStore
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.explore.commands import _merged_hints


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


def _inventory_rank(argv: list[str], capsys) -> tuple[list[str], dict[str, float]]:
    """Run an inventory --rank and return (order, scores) keyed by bare name."""

    objects = _run(argv, capsys)["data"]["objects"]
    order = [o["identifier"].split(".")[-1] for o in objects]
    scores = {name: o["rank_score"] for name, o in zip(order, objects, strict=True)}
    return order, scores


# --- inventory + rank --------------------------------------------------------


def test_inventory_ranks_without_dumping_schema(duckdb_file: Path, capsys):
    payload = _run(
        ["explore", "inventory", "--rank", "--path", str(duckdb_file)], capsys
    )
    data = payload["data"]
    assert data["ranked"] is True
    assert data["object_count"] == 2
    scores = [o["rank_score"] for o in data["objects"]]
    assert all(s is not None for s in scores)
    assert scores == sorted(scores, reverse=True), "ranked descending"
    # Sense-making, not enumeration: object-level only, no per-column listing.
    assert all("columns" not in o for o in data["objects"])


def test_inventory_without_rank_has_no_scores(duckdb_file: Path, capsys):
    payload = _run(["explore", "inventory", "--path", str(duckdb_file)], capsys)
    assert payload["data"]["ranked"] is False
    assert all(o["rank_score"] is None for o in payload["data"]["objects"])


def test_inventory_rank_without_hints_orders_larger_table_first(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """Baseline (no configured hints): the larger table outranks the smaller one,
    and a hint-shaped substring has no effect when nothing is configured."""

    repo = tmp_path / "repo"
    repo.mkdir()
    order, scores = _inventory_rank(
        [
            "explore",
            "inventory",
            "--rank",
            "--path",
            str(duckdb_file),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert order[0] == "orders"
    assert scores["orders"] > scores["customers"]


def test_inventory_rank_honors_configured_ranking_hints(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """A configured ranking_hint lifts the matching object's naming signal, which
    flips the order vs the no-hints baseline. Guards the inventory/map parity:
    map already applied hints; inventory --rank must too."""

    repo = tmp_path / "repo"
    repo.mkdir()
    save_config(DexConfig(ranking_hints=["customer"]), repo)

    order, scores = _inventory_rank(
        [
            "explore",
            "inventory",
            "--rank",
            "--path",
            str(duckdb_file),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert order[0] == "customers"
    assert scores["customers"] > scores["orders"]


# --- profile (+PII) ----------------------------------------------------------


def test_profile_is_aggregate_derived_and_passes_sanitizer(duckdb_file: Path, capsys):
    payload = _run(
        ["explore", "profile", "customers", "--path", str(duckdb_file)], capsys
    )
    cols = {c["name"]: c for c in payload["data"]["datasets"][0]["columns"]}
    assert cols["id"]["distinct_count"] == 2
    assert cols["id"]["null_fraction"] == 0.0
    # The envelope was emitted, so it already passed sanitize(); assert again.
    env.sanitize(env.ok(payload["data"]))


def test_profile_suppresses_min_max_on_string_and_pii(duckdb_file: Path, capsys):
    payload = _run(
        ["explore", "profile", "customers", "--path", str(duckdb_file)], capsys
    )
    cols = {c["name"]: c for c in payload["data"]["datasets"][0]["columns"]}
    # email is VARCHAR and PII: a min/max would be a raw value, so suppressed.
    assert cols["email"]["min_value"] is None
    assert cols["email"]["max_value"] is None
    # id is a safe numeric column: min/max present.
    assert cols["id"]["min_value"] == 1
    assert cols["id"]["max_value"] == 2


def test_pii_flag_structure_from_aggregates(duckdb_file: Path, capsys):
    payload = _run(
        ["explore", "profile", "customers", "--path", str(duckdb_file)], capsys
    )
    cols = {c["name"]: c for c in payload["data"]["datasets"][0]["columns"]}
    pii = cols["email"]["pii"]
    assert pii["category"] == "email"
    assert 0 < pii["confidence"] <= 0.95
    assert set(pii) == {"category", "confidence"}  # never an example value
    assert cols["id"]["pii"] is None


# --- relationships -----------------------------------------------------------


def test_relationships_infers_orders_to_customers(duckdb_file: Path, capsys):
    payload = _run(["explore", "relationships", "--path", str(duckdb_file)], capsys)
    rels = payload["data"]["relationships"]
    assert payload["data"]["inferred_count"] >= 1
    fk = next(r for r in rels if r["from_dataset"].endswith(".orders"))
    assert fk["from_columns"] == ["customer_id"]
    assert fk["to_dataset"].endswith(".customers")
    assert fk["to_columns"] == ["id"]
    assert fk["kind"] == "inferred"
    assert fk["confidence"] and fk["confidence"] >= 0.6


# --- map ---------------------------------------------------------------------


def test_map_writes_cache_and_preserves_created_at(
    duckdb_file: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)]

    first = _run(argv, capsys)
    assert first["data"]["object_count"] == 2
    assert first["data"]["pii_column_count"] >= 1

    store = DexStore(repo)
    cache1 = store.load_cache()
    assert cache1 is not None
    created = cache1.provenance.created_at
    assert created is not None
    assert any(d.grain for d in cache1.datasets)

    second = _run(argv, capsys)
    cache2 = store.load_cache()
    assert cache2.provenance.created_at == created, "created_at stable across runs"
    assert cache2.provenance.updated_at >= created
    assert second["data"]["relationship_count"] >= 1


def test_map_summary_is_counts_not_a_schema_dump(
    duckdb_file: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)], capsys
    )
    # The printed summary carries counts and a small top list, never the columns.
    assert "datasets" not in payload["data"]
    assert payload["data"]["profiled_count"] <= payload["data"]["object_count"]


def test_map_announces_profile_cutoff(many_tables_duckdb: Path, tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        [
            "explore",
            "map",
            "--path",
            str(many_tables_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert payload["data"]["object_count"] == 60
    assert payload["data"]["profiled_count"] == 25
    assert payload["data"]["skipped_count"] == 35
    note = next(n for n in payload["data"]["notes"] if "profile_top_n" in n)
    assert "--full" in note


def test_map_full_has_no_cutoff(many_tables_duckdb: Path, tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        [
            "explore",
            "map",
            "--path",
            str(many_tables_duckdb),
            "--repo-root",
            str(repo),
            "--full",
        ],
        capsys,
    )
    assert payload["data"]["profiled_count"] == 60
    assert payload["data"]["skipped_count"] == 0
    assert not any("profile_top_n" in n for n in payload["data"]["notes"])


def test_map_carries_forward_prior_profiles(
    many_tables_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [
        "explore",
        "map",
        "--path",
        str(many_tables_duckdb),
        "--repo-root",
        str(repo),
    ]
    _run([*argv, "--full"], capsys)
    store = DexStore(repo)
    first_stamps = {d.identifier: d.profiled_at for d in store.load_cache().datasets}
    assert all(first_stamps.values()), "a full map stamps every profile"

    payload = _run(argv, capsys)
    assert payload["data"]["profiled_count"] == 25
    assert payload["data"]["carried_forward_count"] == 35
    assert any("carried forward 35" in n for n in payload["data"]["notes"])

    cache = store.load_cache()
    # Nothing degraded to inventory-only: every dataset kept full column detail.
    assert all(d.columns for d in cache.datasets)
    unchanged = [
        d.identifier
        for d in cache.datasets
        if d.profiled_at == first_stamps[d.identifier]
    ]
    assert len(unchanged) == 35, "carried profiles keep their original stamp"


def test_cache_without_new_fields_still_loads():
    from exmergo_dex_core.cache import DexCache

    old_shape = {
        "schema_version": 2,
        "datasets": [
            {
                "identifier": "db.main.t",
                "columns": [{"name": "id", "data_type": "INTEGER"}],
            }
        ],
        "relationships": [],
    }
    cache = DexCache.model_validate(old_shape)
    assert cache.datasets[0].profiled_at is None
    assert cache.datasets[0].columns[0].distinct_count_exact is False
    # A v2 cache (pre pii_overridden) loads with the audit field defaulted; its
    # stored flags keep blocking as they did when written (fail closed).
    assert cache.datasets[0].columns[0].pii_overridden is None


# --- edge cases --------------------------------------------------------------


@pytest.fixture
def edge_duckdb(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "edge.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE empty_t (id INTEGER, note VARCHAR)")  # zero rows
    conn.execute("CREATE TABLE people (id INTEGER, full_name VARCHAR)")
    conn.execute("INSERT INTO people VALUES (1, 'Ada'), (2, 'Grace')")
    conn.execute("CREATE VIEW people_v AS SELECT id FROM people")
    conn.close()
    return path


def test_empty_table_and_view_profile_without_error(edge_duckdb: Path, capsys):
    payload = _run(
        ["explore", "profile", "empty_t", "people_v", "--path", str(edge_duckdb)],
        capsys,
    )
    ds = {d["identifier"].split(".")[-1]: d for d in payload["data"]["datasets"]}
    empty = ds["empty_t"]
    assert any("empty" in note for note in empty["data_quality"])
    assert empty["columns"][0]["null_fraction"] is None  # no rows -> undefined
    assert "people_v" in ds  # a view profiles fine


# --- free priors from the dbt project ------------------------------------------


def _semantic_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A warehouse plus a dbt project whose semantic layer declares a grain
    (order_code, disagreeing with the id heuristic) and whose schema.yml
    declares status unique (contradicted by the data)."""

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
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models" / "semantic.yml").write_text(
        "semantic_models:\n"
        "  - name: orders\n"
        "    model: ref('orders')\n"
        "    entities:\n"
        "      - name: order\n"
        "        type: primary\n"
        "        expr: order_code\n",
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
    return db, repo


def test_map_declared_grain_overrides_heuristic(tmp_path: Path, capsys):
    db, repo = _semantic_repo(tmp_path)
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
    cache = DexStore(repo).load_cache()
    (orders,) = [d for d in cache.datasets if d.identifier.endswith(".orders")]
    # The heuristic would pick the id-shaped key; the declared primary entity wins.
    assert orders.grain == ["order_code"]
    assert any("declared primary entity" in n for n in orders.data_quality)
    assert any("heuristic suggested id" in n for n in orders.data_quality)


def test_map_notes_declared_unique_contradiction(tmp_path: Path, capsys):
    db, repo = _semantic_repo(tmp_path)
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
    cache = DexStore(repo).load_cache()
    (orders,) = [d for d in cache.datasets if d.identifier.endswith(".orders")]
    assert any(
        "status is declared unique" in n and "duplicates" in n
        for n in orders.data_quality
    )
    # Measurement-only: the contradicted declared key must not appear as a
    # candidate key just because the project claims it.
    assert ["status"] not in orders.candidate_keys


def test_profile_gets_declared_grain_too(tmp_path: Path, capsys):
    db, repo = _semantic_repo(tmp_path)
    payload = _run(
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
    (orders,) = payload["data"]["datasets"]
    assert orders["grain"] == ["order_code"]


def test_exploration_starts_bare_and_discovers_the_project(tmp_path: Path, capsys):
    """Without --use-project the warehouse is explored as-is: heuristic grain,
    no declared influence, and a discovery note pointing at the flag."""

    db, repo = _semantic_repo(tmp_path)
    payload = _run(
        ["explore", "map", "--path", str(db), "--repo-root", str(repo)],
        capsys,
    )
    notes = payload["data"]["notes"]
    assert any("--use-project" in n for n in notes)
    # The empty-declared note adapts: the project exists, it just was not used.
    assert not any("no dbt project" in n for n in notes)
    cache = DexStore(repo).load_cache()
    (orders,) = [d for d in cache.datasets if d.identifier.endswith(".orders")]
    assert orders.grain == ["id"]
    assert not any("declared" in n for n in orders.data_quality)


def _metric_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Two structurally identical tables; only beta_events backs a metric."""

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    for name in ("alpha_events", "beta_events"):
        conn.execute(f"CREATE TABLE {name} (id INTEGER, amount INTEGER)")
        conn.execute(f"INSERT INTO {name} VALUES (1, 10), (2, 20)")  # noqa: S608
    conn.close()

    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models" / "semantic.yml").write_text(
        "semantic_models:\n"
        "  - name: events\n"
        "    model: ref('beta_events')\n"
        "    entities:\n"
        "      - name: id\n"
        "        type: primary\n"
        "    measures:\n"
        "      - name: event_amount\n"
        "        agg: sum\n"
        "        expr: amount\n"
        "metrics:\n"
        "  - name: total_amount\n"
        "    type: simple\n"
        "    type_params:\n"
        "      measure: event_amount\n",
        encoding="utf-8",
    )
    return db, repo


def test_map_metric_backed_table_outranks_equivalent(tmp_path: Path, capsys):
    db, repo = _metric_repo(tmp_path)
    payload = _run(
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
    top = {
        o["identifier"].split(".")[-1]: o["rank_score"]
        for o in payload["data"]["top_objects"]
    }
    assert top["beta_events"] > top["alpha_events"]
    assert any("back metric definitions" in n for n in payload["data"]["notes"])


def test_map_metric_hints_do_not_displace_user_hints(tmp_path: Path, capsys):
    db, repo = _metric_repo(tmp_path)
    save_config(DexConfig(ranking_hints=["alpha_events"]), repo)
    payload = _run(
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
    top = {
        o["identifier"].split(".")[-1]: o["rank_score"]
        for o in payload["data"]["top_objects"]
    }
    # The user's hint keeps its full naming boost alongside the metric's.
    assert top["alpha_events"] == top["beta_events"]


def test_merged_hints_appends_without_displacing():
    assert _merged_hints(["customer"], ["Customer", "orders"]) == [
        "customer",
        "orders",
    ]
