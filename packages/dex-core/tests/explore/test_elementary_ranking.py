"""Elementary's own tables are left out of inventory and map ranking (#1311).

Each guarantee is a pair, as #1303 asks: ``--include-elementary`` is the arm built
to break it (the package's tables, made the largest here, outrank the project's),
and the default is the arm built to hold it. The fixture project's run of the
same pair, with the real package, is in
``tests/transform/test_elementary_fixture.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.adapters.base import ObjectMeta
from exmergo_dex_core.cache import Dataset, DexCache
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore import elementary, summary

# --- recognising the package's tables ---------------------------------------------


def _meta(identifier: str, rows: int = 10) -> ObjectMeta:
    namespace, _, name = identifier.rpartition(".")
    return ObjectMeta(
        identifier=identifier,
        object_type="table",
        schema=namespace.rpartition(".")[2],
        name=name,
        row_count=rows,
        byte_size=None,
        column_count=8,
    )


def _metas(*identifiers: str) -> list[ObjectMeta]:
    return [_meta(i) for i in identifiers]


SIGNATURE = (
    "dbt_run_results",
    "data_monitoring_metrics",
    "elementary_test_results",
    "dbt_models",
)


def test_a_dedicated_schema_is_recognised_temp_tables_included():
    metas = _metas(
        *(f"main_elementary.{name}" for name in SIGNATURE),
        "main_elementary.data_monitoring_metrics__tmp_20261118200743655348",
        "main_elementary.accepted_values_fct_band__large__tmp_20261118200646827790",
        "main.orders",
    )

    found = elementary.recognise(metas)

    assert set(found.identifiers) == {
        m.identifier for m in metas if m.identifier.startswith("main_elementary.")
    }
    assert found.namespaces == {"main_elementary": 6}


def test_in_a_shared_schema_only_the_package_s_tables_are_recognised():
    """A project that builds the package into its own schema keeps its tables."""

    metas = _metas(
        *(f"main.{name}" for name in SIGNATURE),
        "main.orders",
        "main.customers",
        "main.elementary_runs",  # a project model reading the package's
    )

    found = elementary.recognise(metas)

    assert set(found.identifiers) == {f"main.{name}" for name in SIGNATURE}
    assert found.namespaces == {"main": 4}


@pytest.mark.parametrize(
    "names",
    [
        # Two names, one an anchor: a copied table, not the package's schema.
        ("dbt_run_results", "elementary_test_results"),
        # Three names, none only the package uses.
        ("dbt_models", "dbt_tests", "metadata"),
    ],
)
def test_a_schema_without_the_whole_signature_is_not_recognised(names):
    found = elementary.recognise(_metas(*(f"raw.{n}" for n in names), "raw.orders"))

    assert not found
    assert found.namespaces == {}


def test_names_are_matched_without_regard_to_case():
    """Snowflake reports unquoted names in upper case."""

    metas = _metas(*(f"DB.ELEMENTARY.{name.upper()}" for name in SIGNATURE))

    assert len(elementary.recognise(metas).identifiers) == len(SIGNATURE)


def test_dbt_s_own_temp_suffix_and_a_lone_temp_table_are_not_the_package_s():
    metas = _metas(
        *(f"main_elementary.{name}" for name in SIGNATURE),
        "main_elementary.fct__dbt_tmp",
        "main.orders__tmp_20261118200743655348",
    )

    found = elementary.recognise(metas)

    assert "main_elementary.fct__dbt_tmp" not in found.identifiers
    assert "main.orders__tmp_20261118200743655348" not in found.identifiers


def test_two_databases_sharing_a_schema_name_are_read_apart():
    metas = _metas(
        *(f"prod.elementary.{name}" for name in SIGNATURE),
        "dev.elementary.dbt_run_results",
        "dev.elementary.dbt_models",
    )

    found = elementary.recognise(metas)

    assert found.namespaces == {"prod.elementary": 4}


def test_the_excluded_block_is_capped_and_counts_every_cut():
    names = sorted(elementary.TABLE_NAMES)
    metas = _metas(*(f"{ns}.{n}" for ns in ("a", "b") for n in names))

    block = elementary.recognise(metas).excluded()

    assert block.count == 2 * len(names)
    assert block.reason == "elementary"
    assert len(block.objects) == summary.MAX_SKIPPED_OBJECTS
    assert block.elided == 2 * len(names) - summary.MAX_SKIPPED_OBJECTS
    assert [(g.namespace, g.count) for g in block.by_namespace] == [
        ("a", len(names)),
        ("b", len(names)),
    ]


def test_nothing_recognised_reports_an_empty_block():
    block = elementary.recognise(_metas("main.orders")).excluded()

    assert block.model_dump() == {
        "count": 0,
        "reason": None,
        "by_namespace": [],
        "objects": [],
        "elided": 0,
        "elided_namespaces": 0,
    }


def test_summarize_map_never_describes_an_excluded_object():
    """Even where an earlier `explore profile` left its profile in the cache."""

    from exmergo_dex_core.cache import ColumnProfile

    def profiled(identifier: str, score: float | None) -> Dataset:
        return Dataset(
            identifier=identifier,
            rank_score=score,
            columns=[ColumnProfile(name="id", data_type="INTEGER")],
        )

    cache = DexCache(
        datasets=[
            profiled("main.orders", 0.4),
            profiled("main_elementary.dbt_run_results", None),
        ]
    )

    view = summary.summarize_map(
        cache, excluded=frozenset({"main_elementary.dbt_run_results"})
    )

    assert [o.identifier for o in view.objects] == ["main.orders"]
    assert view.elided_object_count == 0


# --- through the CLI, on DuckDB ----------------------------------------------------


@pytest.fixture
def elementary_warehouse(tmp_path: Path) -> Path:
    """The project's two tables in ``main``, and the package's schema beside them.

    The package's tables are made far larger than the project's, as they are on
    a warehouse where they are written every run, so on size alone they outrank
    everything the project owns.
    """

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "create table main.customers as "
        "select i as id, 'c' || i as segment from range(40) t(i)"
    )
    con.execute(
        "create table main.orders as select i as id, i % 40 as customer_id, "
        "(i % 90)::double as amount from range(200) t(i)"
    )
    con.execute("create schema main_elementary")
    for name in SIGNATURE:
        con.execute(
            f"create table main_elementary.{name} as select i as id, "  # noqa: S608
            "'x' || i as unique_id, i % 7 as status from range(5000) t(i)"
        )
    con.execute(
        "create table main_elementary.data_monitoring_metrics__tmp_"
        "20261118200743655348 as select 1 as id"
    )
    con.close()
    return path


# DuckDB qualifies each object with the database, named after the file.
_ELEMENTARY = {
    *(f"warehouse.main_elementary.{name}" for name in SIGNATURE),
    "warehouse.main_elementary.data_monitoring_metrics__tmp_20261118200743655348",
}
_PROJECT = {"warehouse.main.customers", "warehouse.main.orders"}


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload["data"]


def _ids(objects: list[dict]) -> list[str]:
    return [o["identifier"] for o in objects]


def test_inventory_rank_includes_the_package_s_tables_when_asked(
    elementary_warehouse: Path, capsys
):
    """The arm built to break it: ranked with the rest, they come first."""

    data = _run(
        [
            "explore",
            "inventory",
            "--rank",
            "--include-elementary",
            "--path",
            str(elementary_warehouse),
        ],
        capsys,
    )

    assert set(_ids(data["objects"])) == _ELEMENTARY | _PROJECT
    assert _ids(data["objects"])[0] in _ELEMENTARY
    assert data["excluded"]["count"] == 0
    assert not any("Elementary" in n for n in data["notes"])


def test_inventory_rank_leaves_the_package_s_tables_out_by_default(
    elementary_warehouse: Path, capsys
):
    data = _run(
        ["explore", "inventory", "--rank", "--path", str(elementary_warehouse)],
        capsys,
    )

    assert set(_ids(data["objects"])) == _PROJECT
    assert all(o["rank_score"] is not None for o in data["objects"])
    excluded = data["excluded"]
    assert excluded["count"] == len(_ELEMENTARY)
    assert excluded["reason"] == "elementary"
    assert set(excluded["objects"]) == _ELEMENTARY
    assert excluded["by_namespace"] == [
        {"namespace": "warehouse.main_elementary", "count": len(_ELEMENTARY)}
    ]
    note = next(n for n in data["notes"] if "Elementary" in n)
    assert "main_elementary" in note
    assert "--include-elementary" in note
    assert "explore profile" in note


def test_inventory_without_rank_still_lists_the_package_s_tables(
    elementary_warehouse: Path, capsys
):
    """The schema stays readable: nothing is left out of an unranked list."""

    data = _run(["explore", "inventory", "--path", str(elementary_warehouse)], capsys)

    assert set(_ids(data["objects"])) == _ELEMENTARY | _PROJECT
    assert "excluded" not in data


def _map(warehouse: Path, repo: Path, capsys, *extra: str) -> dict:
    repo.mkdir(exist_ok=True)
    return _run(
        ["explore", "map", "--path", str(warehouse), "--repo-root", str(repo), *extra],
        capsys,
    )


def test_map_ranks_the_package_s_tables_when_asked(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    data = _map(elementary_warehouse, tmp_path / "repo", capsys, "--include-elementary")

    # Ranked, profiled and described like the project's own; by size alone they
    # would lead (connectivity lifts `orders`, which joins `customers`).
    ranked = {o["identifier"]: o["rank_score"] for o in data["objects"]}
    assert set(ranked) >= _ELEMENTARY
    assert all(ranked[i] is not None for i in _ELEMENTARY)
    assert _ELEMENTARY & set(_ids(data["top_objects"]))
    assert data["profiled_count"] == len(_ELEMENTARY | _PROJECT)
    assert data["excluded"]["count"] == 0


def test_map_ranks_no_package_table_and_says_so(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    """#1311's acceptance: no Elementary table ranks, the notes say so, and the
    project's tables in the same warehouse still rank."""

    data = _map(elementary_warehouse, tmp_path / "repo", capsys)

    assert set(_ids(data["objects"])) == _PROJECT
    assert all(o["rank_score"] is not None for o in data["objects"])
    assert set(_ids(data["top_objects"])) == _PROJECT
    assert data["profiled_count"] == len(_PROJECT)
    assert data["excluded"]["count"] == len(_ELEMENTARY)
    assert set(data["excluded"]["objects"]) == _ELEMENTARY
    # Left out is not skipped: the rank cutoff did not drop them.
    assert data["skipped"]["count"] == 0
    assert any("Elementary" in n and "main_elementary" in n for n in data["notes"])
    # The cache still holds them, unranked, so the landscape stays complete.
    cache = json.loads((tmp_path / "repo" / ".dex" / "cache.json").read_text())
    by_id = {d["identifier"]: d for d in cache["datasets"]}
    assert set(by_id) >= _ELEMENTARY
    assert all(by_id[i].get("rank_score") is None for i in _ELEMENTARY)
    assert all(not by_id[i].get("columns") for i in _ELEMENTARY)


def test_map_full_and_detail_still_leave_them_out(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    data = _map(elementary_warehouse, tmp_path / "repo", capsys, "--full", "--detail")

    assert set(_ids(data["objects"])) == _PROJECT
    assert data["profiled_count"] == len(_PROJECT)
    assert data["excluded"]["count"] == len(_ELEMENTARY)


def test_a_package_table_profiled_by_hand_stays_readable_and_unranked(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = "warehouse.main_elementary.dbt_run_results"
    profiled = _run(
        [
            "explore",
            "profile",
            target,
            "--path",
            str(elementary_warehouse),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert profiled["datasets"][0]["identifier"] == target

    data = _map(elementary_warehouse, repo, capsys)

    assert target not in _ids(data["objects"])
    cache = json.loads((repo / ".dex" / "cache.json").read_text())
    [kept] = [d for d in cache["datasets"] if d["identifier"] == target]
    assert kept["columns"], "the hand-made profile was dropped"
    answered = _run(
        [
            "explore",
            "query",
            f"select count(*) as n from {target}",  # noqa: S608
            "--path",
            str(elementary_warehouse),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert answered["cells"] == [[5000]]


def test_joins_among_the_package_s_tables_do_not_weigh_on_the_project_s_rank(
    tmp_path: Path, capsys
):
    """An include run caches joins among the package's tables. The next default
    map must score the project's tables exactly as a map that never saw them.

    Today the carry-forward drops those joins itself (an unexamined endpoint in
    an observed namespace reads as gone), so this pins the outcome; the map's own
    filter on the joins it ranks with is what keeps it should that change, and
    what keeps a declared join to a package table out of the scale."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute("create table main.customers as select i as id from range(40) t(i)")
    con.execute(
        "create table main.orders as "
        "select i as id, i % 40 as customer_id from range(200) t(i)"
    )
    con.execute("create schema main_elementary")
    con.execute(
        "create table main_elementary.dbt_models as select i as id from range(50) t(i)"
    )
    for name in (
        "dbt_run_results",
        "data_monitoring_metrics",
        "elementary_test_results",
    ):
        con.execute(
            f"create table main_elementary.{name} as "  # noqa: S608
            "select i as id, i % 50 as dbt_model_id from range(5000) t(i)"
        )
    con.close()

    fresh = _map(warehouse, tmp_path / "fresh", capsys)
    included = _map(warehouse, tmp_path / "repo", capsys, "--include-elementary")
    assert included["relationship_count"] > fresh["relationship_count"]
    after = _map(warehouse, tmp_path / "repo", capsys)

    def scores(data: dict) -> dict[str, float]:
        return {o["identifier"]: o["rank_score"] for o in data["objects"]}

    assert scores(after) == scores(fresh)


def test_a_package_table_cached_outside_this_run_s_scope_stays_left_out(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    """An include run caches the package's profiles with a rank. A later default
    map over another warehouse (on DuckDB, another file: the same thing a
    narrower `--scope` is on a metered connector) carries them forward from
    outside its scope, and must leave them out as if it had listed them."""

    duckdb = pytest.importorskip("duckdb")
    other = tmp_path / "project.duckdb"
    con = duckdb.connect(str(other))
    con.execute("create table main.accounts as select i as id from range(30) t(i)")
    con.close()
    repo = tmp_path / "repo"
    included = _map(elementary_warehouse, repo, capsys, "--include-elementary")
    assert set(_ids(included["objects"])) >= _ELEMENTARY

    scoped = _map(other, repo, capsys)

    assert scoped["out_of_scope_carried_count"] >= len(_ELEMENTARY)
    assert not set(_ids(scoped["objects"])) & _ELEMENTARY
    assert not set(_ids(scoped["top_objects"])) & _ELEMENTARY
    assert "project.main.accounts" in _ids(scoped["objects"])
    assert set(scoped["excluded"]["objects"]) == _ELEMENTARY
    assert scoped["excluded"]["count"] == len(_ELEMENTARY)
    cache = json.loads((repo / ".dex" / "cache.json").read_text())
    by_id = {d["identifier"]: d for d in cache["datasets"]}
    # Kept, profiles and all, so `explore profile` and `explore query` still
    # read them; only the rank is gone.
    assert all(by_id[i]["columns"] for i in _ELEMENTARY)
    assert all(by_id[i].get("rank_score") is None for i in _ELEMENTARY)


def test_the_note_names_at_most_25_namespaces_and_counts_the_rest():
    metas = _metas(*(f"ns{n:03d}.{name}" for n in range(30) for name in SIGNATURE))

    note = elementary.note(elementary.recognise(metas), command="explore map")

    assert "ns024" in note
    assert "ns025" not in note
    assert "and 5 more namespace(s)" in note


def test_the_signature_survives_successive_maps_outside_its_scope(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    """One package table profiled by hand, the rest inventory-only. Two maps
    over another warehouse in a row: the second must still recognise the
    signature, so the hand-made profile stays out of the map."""

    duckdb = pytest.importorskip("duckdb")
    other = tmp_path / "project.duckdb"
    con = duckdb.connect(str(other))
    con.execute("create table main.accounts as select i as id from range(30) t(i)")
    con.close()
    repo = tmp_path / "repo"
    _map(elementary_warehouse, repo, capsys)
    target = "warehouse.main_elementary.dbt_run_results"
    _run(
        [
            "explore",
            "profile",
            target,
            "--path",
            str(elementary_warehouse),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )

    for _ in range(2):
        data = _map(other, repo, capsys)
        assert target not in _ids(data["objects"])
        assert target not in _ids(data["top_objects"])
        assert set(data["excluded"]["objects"]) == _ELEMENTARY


def test_an_include_run_outside_the_scope_keeps_the_signature_for_the_next(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    """The same, with the out-of-scope map in between asking to include the
    package: it must not drop the inventory-only entries a later default map
    recognises the hand-made profile by."""

    duckdb = pytest.importorskip("duckdb")
    other = tmp_path / "project.duckdb"
    con = duckdb.connect(str(other))
    con.execute("create table main.accounts as select i as id from range(30) t(i)")
    con.close()
    repo = tmp_path / "repo"
    _map(elementary_warehouse, repo, capsys)
    target = "warehouse.main_elementary.dbt_run_results"
    _run(
        [
            "explore",
            "profile",
            target,
            "--path",
            str(elementary_warehouse),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )

    _map(other, repo, capsys, "--include-elementary")
    data = _map(other, repo, capsys)

    assert target not in _ids(data["objects"])
    assert target not in _ids(data["top_objects"])
    assert set(data["excluded"]["objects"]) == _ELEMENTARY
