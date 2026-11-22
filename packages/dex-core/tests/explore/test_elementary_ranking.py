"""Elementary's own tables are left out of inventory and map ranking (#1311), and
of the diagram and relationship inference (#1494).

Each guarantee is a pair, as #1303 asks: ``--include-elementary`` is the arm built
to break it (the package's tables, made the largest here, outrank the project's),
and the default is the arm built to hold it. The fixture project's run of the
same pair, with the real package, is in
``tests/transform/test_elementary_fixture.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from exmergo_dex_core.adapters.base import ObjectMeta
from exmergo_dex_core.cache import ColumnProfile, Dataset, DexCache, Relationship
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore import elementary, summary
from exmergo_dex_core.explore.diagram import render_er_mermaid

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


def _joined_warehouse(path: Path, *, bridged: bool = False) -> Path:
    """The project's `orders -> customers` join, and beside it the package's
    schema with a join of its own, three tables' `dbt_model_id` onto `dbt_models`,
    so an include run has joins among the package's tables to infer and cache.

    ``bridged`` adds `main.model_runs`, a project table whose only join is onto
    the package's `dbt_models`."""

    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path))
    con.execute("create table main.customers as select i as id from range(40) t(i)")
    con.execute(
        "create table main.orders as "
        "select i as id, i % 40 as customer_id from range(200) t(i)"
    )
    if bridged:
        con.execute(
            "create table main.model_runs as "
            "select i as id, i % 50 as dbt_model_id from range(100) t(i)"
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
    return path


def test_joins_among_the_package_s_tables_do_not_weigh_on_the_project_s_rank(
    tmp_path: Path, capsys
):
    """An include run caches joins among the package's tables. The next default
    map must score the project's tables exactly as a map that never saw them.

    Today the carry-forward drops those joins itself (an unexamined endpoint in
    an observed namespace reads as gone), so this pins the outcome; the map's own
    filter on the joins it ranks with is what keeps it should that change, and
    what keeps a declared join to a package table out of the scale."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")

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


def test_the_note_s_wording_for_each_command():
    """The one sentence, word for word: ranking's by default, and the clauses
    the diagram and relationship inference put in its place."""

    found = elementary.recognise(_metas(*(f"main_elementary.{n}" for n in SIGNATURE)))
    readable = (
        "data.excluded names them. They stay readable: `explore inventory` "
        "without --rank lists them, and `explore profile` and `explore query` "
        "read them. Pass --include-elementary to "
    )

    assert elementary.note(found, command="explore map") == (
        "left 4 table(s) of the Elementary dbt package out of the ranking "
        "(in main_elementary): the package writes them on every run, so their "
        "size says nothing about the project's own tables. "
        + readable
        + "rank them with the rest in `explore map`"
    )
    assert elementary.note(
        found,
        command="explore diagram",
        left_out_of="the diagram",
        why="they say nothing about the project's own model",
        way_back="draw them with the rest",
    ) == (
        "left 4 table(s) of the Elementary dbt package out of the diagram "
        "(in main_elementary): the package writes them on every run, so they say "
        "nothing about the project's own model. "
        + readable
        + "draw them with the rest in `explore diagram`"
    )
    assert elementary.note(
        found,
        command="explore relationships",
        left_out_of="inference",
        why=(
            "the joins among them are the package's own, and profiling them "
            "says nothing about the project's tables"
        ),
        way_back="profile them and infer joins among them",
    ) == (
        "left 4 table(s) of the Elementary dbt package out of inference "
        "(in main_elementary): the package writes them on every run, so the joins "
        "among them are the package's own, and profiling them says nothing about "
        "the project's tables. "
        + readable
        + "profile them and infer joins among them in `explore relationships`"
    )


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


# --- the diagram and relationship inference leave them out too (#1494) ------------

_JOINED_PROJECT = {"warehouse.main.customers", "warehouse.main.orders"}
_JOINED_ELEMENTARY = {
    f"warehouse.main_elementary.{name}"
    for name in (
        "dbt_models",
        "dbt_run_results",
        "data_monitoring_metrics",
        "elementary_test_results",
    )
}


def _endpoints(relationships: list[dict]) -> set[str]:
    return {r["from_dataset"] for r in relationships} | {
        r["to_dataset"] for r in relationships
    }


def _elementary_note(notes: list[str], command: str) -> str:
    [note] = [n for n in notes if "Elementary" in n]
    assert "main_elementary" in note
    assert "--include-elementary" in note
    assert f"`{command}`" in note
    return note


def test_the_renderer_leaves_excluded_objects_out_before_the_cap():
    """An excluded object is no entity and no elision; the edge onto it is
    dropped as any edge whose endpoint is not drawn, and counted."""

    def profiled(identifier: str) -> Dataset:
        return Dataset(
            identifier=identifier,
            columns=[ColumnProfile(name="id", data_type="INTEGER", is_unique=True)],
        )

    cache = DexCache(
        datasets=[
            profiled("main.orders"),
            profiled("main.customers"),
            profiled("main_elementary.dbt_run_results"),
            profiled("main_elementary.dbt_models"),
        ],
        relationships=[
            Relationship(
                from_dataset="main.orders",
                from_columns=["customer_id"],
                to_dataset="main.customers",
                to_columns=["id"],
            ),
            Relationship(
                from_dataset="main_elementary.dbt_run_results",
                from_columns=["dbt_model_id"],
                to_dataset="main_elementary.dbt_models",
                to_columns=["id"],
            ),
        ],
    )
    excluded = frozenset(
        {"main_elementary.dbt_run_results", "main_elementary.dbt_models"}
    )

    rendered = render_er_mermaid(cache, full=True, max_entities=2, excluded=excluded)

    assert set(rendered.entities.values()) == {"main.orders", "main.customers"}
    assert rendered.elided_entity_count == 0
    assert rendered.edge_count == 1
    assert rendered.elided_edge_count == 1
    assert "dbt_run_results" not in rendered.mermaid


def _keyed(identifier: str) -> Dataset:
    return Dataset(
        identifier=identifier,
        columns=[ColumnProfile(name="id", data_type="INTEGER", is_unique=True)],
    )


def _edge(child: str, parent: str, column: str = "customer_id") -> Relationship:
    return Relationship(
        from_dataset=child, from_columns=[column], to_dataset=parent, to_columns=["id"]
    )


def test_the_renderer_matches_excluded_identifiers_without_regard_to_case():
    """Snowflake spells identifiers in upper case; `excluded` is lowered, as the
    recogniser hands it over, and the renderer lowers each identifier to meet
    it."""

    package = ("DB.MAIN_ELEMENTARY.DBT_RUN_RESULTS", "DB.MAIN_ELEMENTARY.DBT_MODELS")
    cache = DexCache(
        datasets=[
            _keyed("DB.MAIN.ORDERS"),
            _keyed("DB.MAIN.CUSTOMERS"),
            *(_keyed(identifier) for identifier in package),
            _keyed("DB.MAIN_ELEMENTARY.ELEMENTARY_TEST_RESULTS"),
        ],
        relationships=[
            _edge("DB.MAIN.ORDERS", "DB.MAIN.CUSTOMERS"),
            _edge(*package, "dbt_model_id"),
        ],
    )
    found = elementary.recognise((), cached=[d.identifier for d in cache.datasets])
    assert len(found.identifiers) == 3

    rendered = render_er_mermaid(cache, full=True, excluded=found.lowered)

    assert set(rendered.entities.values()) == {"DB.MAIN.ORDERS", "DB.MAIN.CUSTOMERS"}
    assert rendered.elided_entity_count == 0
    assert rendered.edge_count == 1
    assert rendered.elided_edge_count == 1
    assert "ELEMENTARY" not in rendered.mermaid


def test_the_reported_relationships_drop_excluded_endpoints_without_regard_to_case():
    """The filter `relationships` reports through and `map` ranks with."""

    from exmergo_dex_core.explore.commands import _without_endpoints

    project = _edge("DB.MAIN.ORDERS", "DB.MAIN.CUSTOMERS")
    package = _edge(
        "DB.MAIN_ELEMENTARY.DBT_RUN_RESULTS", "DB.MAIN_ELEMENTARY.DBT_MODELS", "x"
    )
    mixed = _edge("DB.MAIN.MODEL_RUNS", "DB.MAIN_ELEMENTARY.DBT_MODELS", "x")
    left_out = frozenset(
        {"db.main_elementary.dbt_run_results", "db.main_elementary.dbt_models"}
    )

    assert _without_endpoints([project, package, mixed], left_out) == [project]
    assert _without_endpoints([project, package], frozenset()) == [project, package]


def test_an_edge_onto_an_excluded_object_buys_no_connectivity():
    """A project table whose only join is onto a package table is isolated once
    that edge cannot be drawn: the default diagram does not draw it, so it takes
    no cap slot as an edge-less box. `--full` still draws it, as it draws every
    isolated object."""

    cache = DexCache(
        datasets=[
            _keyed("main.orders"),
            _keyed("main.customers"),
            _keyed("main.model_runs"),  # the project's, reading the package's
            _keyed("main_elementary.dbt_models"),
        ],
        relationships=[
            _edge("main.orders", "main.customers"),
            _edge("main.model_runs", "main_elementary.dbt_models", "dbt_model_id"),
        ],
    )
    excluded = frozenset({"main_elementary.dbt_models"})

    rendered = render_er_mermaid(cache, excluded=excluded)

    assert set(rendered.entities.values()) == {"main.orders", "main.customers"}
    assert rendered.edge_count == 1
    assert rendered.elided_edge_count == 1
    assert rendered.elided_entity_count == 0
    assert "model_runs" not in rendered.mermaid

    # With a cap of two, the isolated table must not take a slot either.
    capped = render_er_mermaid(cache, max_entities=2, excluded=excluded)
    assert set(capped.entities.values()) == {"main.orders", "main.customers"}
    assert capped.elided_entity_count == 0

    widened = render_er_mermaid(cache, full=True, excluded=excluded)
    assert "main.model_runs" in set(widened.entities.values())


def _diagram(warehouse: Path, repo: Path, capsys, *extra: str, error: bool = False):
    rc = main(
        [
            "explore",
            "diagram",
            "--path",
            str(warehouse),
            "--repo-root",
            str(repo),
            *extra,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    if error:
        assert rc == 1 and payload["status"] == "error", payload
        return payload
    assert rc == 0 and payload["status"] == "ok", payload
    return payload["data"]


def _drawn(data: dict) -> set[str]:
    return {e["identifier"] for e in data["entities"]}


def test_diagram_draws_the_package_s_tables_when_asked(tmp_path: Path, capsys):
    """The arm built to break it: an include map cached the package's profiles
    and the join among them, and an include diagram draws all of it."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")
    repo = tmp_path / "repo"
    _map(warehouse, repo, capsys, "--include-elementary")

    data = _diagram(warehouse, repo, capsys, "--full", "--include-elementary")

    assert _drawn(data) == _JOINED_PROJECT | _JOINED_ELEMENTARY
    assert data["excluded"]["count"] == 0
    assert data["excluded"]["reason"] is None
    assert not any("Elementary" in n for n in data["notes"])


def test_diagram_draws_no_package_table_by_default_and_says_so(tmp_path: Path, capsys):
    """#1494's acceptance: after `map --include-elementary` the cache holds the
    package's profiles, and a default diagram, `--full` included, draws none of
    them, names them under `excluded`, and says so in `notes`."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")
    repo = tmp_path / "repo"
    _map(warehouse, repo, capsys, "--include-elementary")

    for extra in ((), ("--full",)):
        data = _diagram(warehouse, repo, capsys, *extra)

        assert _drawn(data) == _JOINED_PROJECT
        assert data["edge_count"] == 1
        # Left out is not elided: the entity cap did not cut them.
        assert data["elided_entity_count"] == 0
        excluded = data["excluded"]
        assert excluded["count"] == len(_JOINED_ELEMENTARY)
        assert excluded["reason"] == "elementary"
        assert set(excluded["objects"]) == _JOINED_ELEMENTARY
        assert excluded["by_namespace"] == [
            {"namespace": "warehouse.main_elementary", "count": len(_JOINED_ELEMENTARY)}
        ]
        note = _elementary_note(data["notes"], "explore diagram")
        assert "out of the diagram" in note


def test_diagram_leaves_out_a_package_table_profiled_by_hand(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    """A default map keeps the package's tables as inventory entries, which is
    the signature a later hand-run profile is recognised by."""

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

    data = _diagram(elementary_warehouse, repo, capsys, "--full")

    assert _drawn(data) == _PROJECT
    assert target in data["excluded"]["objects"]
    assert set(data["excluded"]["objects"]) == _ELEMENTARY


def test_diagram_cannot_recognise_a_lone_package_table_profiled_into_a_bare_cache(
    elementary_warehouse: Path, tmp_path: Path, capsys
):
    """The signature has to be in the cache to read. A default `relationships`
    writes no entry for the tables it leaves out, so a single package table
    profiled by hand into that cache is a lone name, below MIN_SIGNATURE (one
    copied `elementary_test_results` is nobody's package schema), and `--full`
    draws it with nothing excluded. An `explore map` writes the inventory entries
    that complete the signature, and the next diagram leaves it out."""

    repo = tmp_path / "repo"
    _relationships(elementary_warehouse, repo, capsys)
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
    cache = json.loads((repo / ".dex" / "cache.json").read_text())
    assert {d["identifier"] for d in cache["datasets"]} & _ELEMENTARY == {target}

    drawn = _diagram(elementary_warehouse, repo, capsys, "--full")

    assert _drawn(drawn) == _PROJECT | {target}
    assert drawn["excluded"]["count"] == 0
    assert not any("Elementary" in n for n in drawn["notes"])

    _map(elementary_warehouse, repo, capsys)
    after = _diagram(elementary_warehouse, repo, capsys, "--full")
    assert _drawn(after) == _PROJECT
    assert set(after["excluded"]["objects"]) == _ELEMENTARY


def test_diagram_does_not_draw_a_project_table_joined_only_to_the_package(
    tmp_path: Path, capsys
):
    """An include map cached `model_runs -> dbt_models`, the project's table's one
    join. A default diagram leaves `dbt_models` out, and with it the edge, so
    `model_runs` is isolated and not drawn; `--full` draws it as it draws every
    isolated object."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb", bridged=True)
    repo = tmp_path / "repo"
    bridge = "warehouse.main.model_runs"
    included = _map(warehouse, repo, capsys, "--include-elementary")
    assert any(
        {r["from_dataset"], r["to_dataset"]}
        == {bridge, "warehouse.main_elementary.dbt_models"}
        for r in included["edges"]
    )

    data = _diagram(warehouse, repo, capsys)

    assert _drawn(data) == _JOINED_PROJECT
    assert data["edge_count"] == 1
    assert data["elided_entity_count"] == 0
    assert set(data["excluded"]["objects"]) == _JOINED_ELEMENTARY

    widened = _diagram(warehouse, repo, capsys, "--full")
    assert _drawn(widened) == _JOINED_PROJECT | {bridge}


def _package_only_warehouse(path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path))
    con.execute("create schema main_elementary")
    for name in SIGNATURE:
        con.execute(
            f"create table main_elementary.{name} as select i as id from range(50) t(i)"  # noqa: S608
        )
    con.close()
    return path


def test_diagram_of_a_cache_holding_only_the_package_names_the_way_back(
    tmp_path: Path, capsys
):
    warehouse = _package_only_warehouse(tmp_path / "warehouse.duckdb")
    repo = tmp_path / "repo"
    _map(warehouse, repo, capsys, "--include-elementary")

    refused = _diagram(warehouse, repo, capsys, "--full", error=True)
    assert refused["reason"] == "prerequisite"
    assert "Elementary" in refused["errors"][0]
    assert "--include-elementary" in refused["errors"][0]

    drawn = _diagram(warehouse, repo, capsys, "--full", "--include-elementary")
    assert _drawn(drawn) == {f"warehouse.main_elementary.{name}" for name in SIGNATURE}


def test_diagram_of_a_cache_holding_only_the_package_refuses_the_same_without_full(
    tmp_path: Path, capsys
):
    """Without `--full` the refusal is the same one: the package's tables are
    not the "profiled objects but no edges" that `--full` would draw."""

    warehouse = _package_only_warehouse(tmp_path / "warehouse.duckdb")
    repo = tmp_path / "repo"
    _map(warehouse, repo, capsys, "--include-elementary")

    refused = _diagram(warehouse, repo, capsys, error=True)

    assert refused["reason"] == "prerequisite"
    [message] = refused["errors"]
    assert "--full to draw" not in message
    assert (
        f"no object that can be drawn apart from {len(SIGNATURE)} table(s) of the "
        "Elementary dbt package" in message
    )
    assert "--include-elementary" in message


def _relationships(warehouse: Path, repo: Path, capsys, *extra: str) -> dict:
    repo.mkdir(exist_ok=True)
    return _run(
        [
            "explore",
            "relationships",
            "--path",
            str(warehouse),
            "--repo-root",
            str(repo),
            *extra,
        ],
        capsys,
    )


def test_relationships_profiles_the_package_s_tables_when_asked(tmp_path: Path, capsys):
    """The arm built to break it: every table profiled, and the join the
    package's tables make among themselves inferred with the project's."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")

    data = _relationships(warehouse, tmp_path / "repo", capsys, "--include-elementary")

    assert data["profiled_count"] == len(_JOINED_PROJECT | _JOINED_ELEMENTARY)
    assert _endpoints(data["relationships"]) & _JOINED_ELEMENTARY
    assert data["excluded"]["count"] == 0
    assert not any("Elementary" in n for n in data["notes"])


def test_relationships_profiles_no_package_table_by_default_and_says_so(
    tmp_path: Path, capsys
):
    """#1494's acceptance: none of the package's tables is profiled, no join
    among them is inferred, `excluded` names them, `notes` says so, and the
    project's own join is still found."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")
    repo = tmp_path / "repo"

    data = _relationships(warehouse, repo, capsys)

    assert data["profiled_count"] == len(_JOINED_PROJECT)
    assert _endpoints(data["relationships"]) == _JOINED_PROJECT
    excluded = data["excluded"]
    assert excluded["count"] == len(_JOINED_ELEMENTARY)
    assert excluded["reason"] == "elementary"
    assert set(excluded["objects"]) == _JOINED_ELEMENTARY
    assert excluded["by_namespace"] == [
        {"namespace": "warehouse.main_elementary", "count": len(_JOINED_ELEMENTARY)}
    ]
    note = _elementary_note(data["notes"], "explore relationships")
    assert "out of inference" in note
    # Nothing was written for them: relationships fabricates no inventory stub.
    cache = json.loads((repo / ".dex" / "cache.json").read_text())
    assert not {d["identifier"] for d in cache["datasets"]} & _JOINED_ELEMENTARY


def test_relationships_after_an_include_run_reads_none_of_the_cached_package_profiles(
    tmp_path: Path, capsys
):
    """An include run left fresh profiles of the package's tables in the cache,
    and the joins among them. A default run neither reuses the profiles as cache
    hits nor infers from them, and leaves them in the cache as they were; the
    joins it drops from the cache, as `map` does (an unexamined endpoint in a
    namespace this run listed reads as gone), so none is carried forward."""

    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")
    repo = tmp_path / "repo"
    included = _relationships(warehouse, repo, capsys, "--include-elementary")
    assert _endpoints(included["relationships"]) & _JOINED_ELEMENTARY

    data = _relationships(warehouse, repo, capsys)

    assert data["profiled_count"] == 0
    assert data["cache_hit_count"] == len(_JOINED_PROJECT)
    assert _endpoints(data["relationships"]) == _JOINED_PROJECT
    assert data["carried_relationship_count"] == 0
    assert set(data["excluded"]["objects"]) == _JOINED_ELEMENTARY
    cache = json.loads((repo / ".dex" / "cache.json").read_text())
    by_id = {d["identifier"]: d for d in cache["datasets"]}
    assert all(by_id[i]["columns"] for i in _JOINED_ELEMENTARY)
    assert _endpoints(cache["relationships"]) == _JOINED_PROJECT


def test_relationships_keeps_package_joins_carried_from_outside_its_scope_unreported(
    tmp_path: Path, capsys
):
    """An include run cached the package's profiles and the joins among them. A
    default run over another warehouse (on DuckDB, another file: what a narrower
    `--scope` is on a metered connector) carries every prior join forward, since
    their namespaces were not listed. The package's tables are recognised among
    the carried profiles, as `map` recognises them, so the joins among them stay
    in the cache and out of `data.relationships`, `carried_relationship_count`
    counts only the carried joins the list reports, and `excluded` names them."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = _joined_warehouse(tmp_path / "warehouse.duckdb")
    other = tmp_path / "project.duckdb"
    con = duckdb.connect(str(other))
    con.execute("create table main.accounts as select i as id from range(30) t(i)")
    con.close()
    repo = tmp_path / "repo"
    included = _relationships(warehouse, repo, capsys, "--include-elementary")
    package_joins = [
        r
        for r in included["relationships"]
        if {r["from_dataset"], r["to_dataset"]} <= _JOINED_ELEMENTARY
    ]
    assert package_joins

    scoped = _relationships(other, repo, capsys)

    # Every prior join was carried, into the cache; only the project's is
    # reported, and the count answers for the reported list, not the cache.
    project_joins = len(included["relationships"]) - len(package_joins)
    assert scoped["carried_relationship_count"] == project_joins
    assert scoped["carried_relationship_count"] == len(scoped["relationships"])
    assert scoped["inferred_count"] == len(scoped["relationships"])
    assert _endpoints(scoped["relationships"]) == _JOINED_PROJECT
    excluded = scoped["excluded"]
    assert excluded["count"] == len(_JOINED_ELEMENTARY)
    assert set(excluded["objects"]) == _JOINED_ELEMENTARY
    _elementary_note(scoped["notes"], "explore relationships")
    cache = json.loads((repo / ".dex" / "cache.json").read_text())
    assert _endpoints(cache["relationships"]) >= _JOINED_PROJECT | _JOINED_ELEMENTARY
    by_id = {d["identifier"]: d for d in cache["datasets"]}
    assert all(by_id[i]["columns"] for i in _JOINED_ELEMENTARY)


@pytest.fixture
def shared_schema_warehouse(tmp_path: Path) -> Path:
    """The package built into the project's own schema (a `generate_schema_name`
    that ignores custom schemas): its tables beside `customers`, `orders` and
    `elementary_runs`, a project model reading the package's."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "shared.duckdb"
    con = duckdb.connect(str(path))
    con.execute("create table main.customers as select i as id from range(40) t(i)")
    con.execute(
        "create table main.orders as "
        "select i as id, i % 40 as customer_id from range(200) t(i)"
    )
    con.execute(
        "create table main.elementary_runs as select i as id from range(30) t(i)"
    )
    con.execute("create table main.dbt_models as select i as id from range(50) t(i)")
    for name in (
        "dbt_run_results",
        "data_monitoring_metrics",
        "elementary_test_results",
    ):
        con.execute(
            f"create table main.{name} as "  # noqa: S608
            "select i as id, i % 50 as dbt_model_id from range(5000) t(i)"
        )
    con.close()
    return path


_SHARED_PROJECT = {
    "shared.main.customers",
    "shared.main.orders",
    "shared.main.elementary_runs",
}
_SHARED_ELEMENTARY = {
    f"shared.main.{name}"
    for name in (
        "dbt_models",
        "dbt_run_results",
        "data_monitoring_metrics",
        "elementary_test_results",
    )
}


def test_the_project_s_tables_sharing_the_package_s_schema_are_still_profiled_and_drawn(
    shared_schema_warehouse: Path, tmp_path: Path, capsys
):
    """Only the objects named like the package's models are left out, so the
    project's own tables in that schema are profiled by `relationships` and drawn
    by `diagram`, from a cache an include map filled with the package's profiles."""

    related = _relationships(shared_schema_warehouse, tmp_path / "related", capsys)
    assert related["profiled_count"] == len(_SHARED_PROJECT)
    assert _endpoints(related["relationships"]) == {
        "shared.main.customers",
        "shared.main.orders",
    }
    assert set(related["excluded"]["objects"]) == _SHARED_ELEMENTARY

    repo = tmp_path / "repo"
    _map(shared_schema_warehouse, repo, capsys, "--include-elementary")
    drawn = _diagram(shared_schema_warehouse, repo, capsys, "--full")
    assert _drawn(drawn) == _SHARED_PROJECT
    assert set(drawn["excluded"]["objects"]) == _SHARED_ELEMENTARY


def test_relationships_estimate_on_a_metered_connector_prices_no_package_table(
    tmp_path: Path, monkeypatch
):
    """On BigQuery the profiling scan is priced before anything runs. The
    estimate covers the project's tables and none of the package's; asked to
    include them, it prices them like any other object."""

    bigquery = pytest.importorskip("google.cloud.bigquery")
    from fakes.bigquery import FakeBigQueryClient, FakeTable

    from exmergo_dex_core.adapters.bigquery import BigQueryAdapter
    from exmergo_dex_core.cli import dispatch
    from exmergo_dex_core.config import BigQueryTarget, DexConfig
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.envelope import Paradigm
    from exmergo_dex_core.guards.cost_guard import CostGate
    from exmergo_dex_core.storage import FilesystemStore

    def table(dataset: str, name: str, rows: int) -> FakeTable:
        return FakeTable(
            project="test-proj",
            dataset_id=dataset,
            table_id=name,
            schema=[
                bigquery.SchemaField("id", "INTEGER"),
                bigquery.SchemaField("unique_id", "STRING"),
            ],
            num_rows=rows,
            num_bytes=rows * 100,
        )

    project = {"test-proj.shop.customers", "test-proj.shop.orders"}
    package = {f"test-proj.shop_elementary.{name}" for name in SIGNATURE}
    client = FakeBigQueryClient(
        project="test-proj",
        tables=[
            table("shop", "customers", 1_000),
            table("shop", "orders", 10_000),
            *(table("shop_elementary", name, 5_000_000) for name in SIGNATURE),
        ],
    )
    config = DexConfig(connector="bigquery")

    def opener(self, command=None, *, budget=None, confirmed=None):
        gate = CostGate(
            paradigm=Paradigm.BYTES_SCANNED,
            ceiling=None,
            session_ceiling=config.budget.session_ceiling,
            session_spent=0.0,
            confirmed=False,
            connector="bigquery",
            command="explore",
            session_ceiling_declined=config.budget.session_ceiling_declined,
        )
        return BigQueryAdapter(
            project="test-proj",
            cost_gate=gate,
            target=BigQueryTarget(),
            client=client,
            principal_type="user",
        )

    monkeypatch.setattr(DexEngine, "_adapter", opener)

    def estimate(include: bool) -> tuple[float, set[str]]:
        root = tmp_path / ("included" if include else "default")
        root.mkdir()
        engine = DexEngine(
            connector="bigquery",
            repo_root=str(root),
            store=FilesystemStore(root),
            config=config,
        )
        args = argparse.Namespace(
            connector="bigquery",
            path=None,
            repo_root=str(root),
            confirm=False,
            budget=None,
            group="explore",
            subcommand="relationships",
            include_elementary=include,
        )
        envelope = dispatch(args, engine)
        assert envelope.status.value == "needs_confirmation", envelope
        return envelope.cost.estimate, set(envelope.data["per_table_bytes"])

    default_estimate, default_priced = estimate(include=False)
    included_estimate, included_priced = estimate(include=True)

    assert default_priced == project
    assert included_priced == project | package
    assert default_estimate < included_estimate
