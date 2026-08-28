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
from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    Relationship,
    ValueCount,
    ValueDomain,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import DexConfig, save_config
from exmergo_dex_core.dbt_project import DeclaredCompositeKey, ProjectDefinitions
from exmergo_dex_core.explore import summary
from exmergo_dex_core.explore.commands import _annotate_grain, _merged_hints
from exmergo_dex_core.storage import FilesystemStore


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


# --- profile payload order and column summary (#288) --------------------------


def test_profile_leads_with_the_verdict_not_columns(duckdb_file: Path, capsys):
    payload = _run(
        ["explore", "profile", "customers", "--path", str(duckdb_file)], capsys
    )
    keys = list(payload["data"]["datasets"][0].keys())
    assert keys.index("columns") == len(keys) - 2  # elided_column_count trails it
    for verdict_field in ("grain", "candidate_keys", "data_quality", "row_count"):
        assert keys.index(verdict_field) < keys.index("columns")


def test_profile_columns_default_summarizes_to_findings(
    profile_findings_duckdb: Path, capsys
):
    payload = _run(
        ["explore", "profile", "wide_profile", "--path", str(profile_findings_duckdb)],
        capsys,
    )
    dataset = payload["data"]["datasets"][0]
    names = {c["name"] for c in dataset["columns"]}
    assert names == {"id", "email"}  # key + PII survive; amount/notes don't
    assert dataset["elided_column_count"] == 2


def test_profile_columns_all_restores_every_column(
    profile_findings_duckdb: Path, capsys
):
    payload = _run(
        [
            "explore",
            "profile",
            "wide_profile",
            "--columns",
            "all",
            "--path",
            str(profile_findings_duckdb),
        ],
        capsys,
    )
    dataset = payload["data"]["datasets"][0]
    names = {c["name"] for c in dataset["columns"]}
    assert names == {"id", "email", "amount", "tag"}
    assert dataset["elided_column_count"] == 0


def test_columns_with_findings_word_boundary_and_value_domain():
    """The two triggers a full profiling run can't isolate cleanly: a
    data-quality mention has to match on a word boundary, not a substring
    (`am` inside `amount` must not count), and a populated value_domain is a
    finding on its own even with no PII, no nulls, and no key role."""

    dataset = Dataset(
        identifier="db.main.t",
        columns=[
            ColumnProfile(name="amount", data_type="DOUBLE"),
            ColumnProfile(name="am", data_type="VARCHAR"),
            ColumnProfile(
                name="status",
                data_type="VARCHAR",
                value_domain=ValueDomain(values=[ValueCount(value="ok", count=1)]),
            ),
            ColumnProfile(name="boring", data_type="VARCHAR"),
        ],
        data_quality=["amount is declared DOUBLE but holds only integers"],
    )

    kept, dropped = dataset.columns_with_findings()
    kept_names = {c.name for c in kept}

    assert "amount" in kept_names  # named in the note
    assert "am" not in kept_names  # substring of "amount", not a word match
    assert "status" in kept_names  # value_domain alone is a finding
    assert "boring" not in kept_names
    assert dropped == 2

    everything, dropped_everything = dataset.columns_with_findings(everything=True)
    assert len(everything) == 4
    assert dropped_everything == 0


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


def test_relationships_reuses_fresh_cached_profiles(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """A second `explore relationships` with nothing changed re-scans nothing,
    yet still re-infers joins over the reused profiles and can be forced to
    re-profile with --refresh."""

    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [
        "explore",
        "relationships",
        "--path",
        str(duckdb_file),
        "--repo-root",
        str(repo),
    ]
    _run(argv, capsys)
    store = FilesystemStore(repo)
    first_stamps = {d.identifier: d.profiled_at for d in store.load_cache().datasets}

    payload = _run(argv, capsys)
    assert payload["data"]["profiled_count"] == 0
    assert payload["data"]["cache_hit_count"] == 2
    # Inference still runs over the reused profiles, so the join survives.
    assert payload["data"]["inferred_count"] >= 1
    assert any("reused 2 fresh cached profile" in n for n in payload["data"]["notes"])
    assert {
        d.identifier: d.profiled_at for d in store.load_cache().datasets
    } == first_stamps

    refreshed = _run([*argv, "--refresh"], capsys)
    assert refreshed["data"]["profiled_count"] == 2
    assert refreshed["data"]["cache_hit_count"] == 0


def test_relationships_freshness_zero_disables_reuse(
    duckdb_file: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    save_config(DexConfig(profile_freshness_hours=0.0), repo)
    argv = [
        "explore",
        "relationships",
        "--path",
        str(duckdb_file),
        "--repo-root",
        str(repo),
    ]
    _run(argv, capsys)
    payload = _run(argv, capsys)
    assert payload["data"]["profiled_count"] == 2
    assert payload["data"]["cache_hit_count"] == 0


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

    store = FilesystemStore(repo)
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


def test_map_summary_is_a_budgeted_map_not_a_schema_dump(
    duckdb_file: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)], capsys
    )["data"]
    # The summary answers the question it was asked (issue #202): findings, not a
    # receipt. What it still never does is paste the cache, so the whole-dataset
    # dump `explore profile` returns stays out of it.
    assert "datasets" not in data
    assert data["profiled_count"] <= data["object_count"]

    assert data["objects"], "a map returns the objects it mapped"
    first = data["objects"][0]
    assert first["identifier"]
    assert set(first["columns"][0]) == {"name", "data_type", "role", "pii"}
    assert data["edges"], "a map returns the joins it inferred"
    assert set(data["edges"][0]) == {
        "from_dataset",
        "from_columns",
        "to_dataset",
        "to_columns",
        "kind",
        "confidence",
        "verified",
        "orphan_fraction",
        "declared_by",
    }, "edges are shaped exactly like `explore relationships` returns them"


def test_map_counts_reconcile_with_the_objects_beside_them(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """A count the payload contradicts is worse than a count with no payload.

    Found by dogfooding: an eligibility rule borrowed from `explore diagram` drops
    objects that carry no join, so a warehouse whose only PII sat on an unjoined
    lookup table reported `pii_column_count: 6` beside objects accounting for 2.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)], capsys
    )["data"]

    assert data["pii_column_count"] == sum(
        o["pii_column_count"] for o in data["objects"]
    )
    assert data["data_quality_note_count"] == sum(
        len(o["data_quality"]) + o["elided_data_quality_count"] for o in data["objects"]
    )
    assert data["relationship_count"] == len(data["edges"]) + data["elided_edge_count"]


def test_map_reports_a_profiled_object_that_joins_to_nothing(tmp_path: Path, capsys):
    """An isolated object is a finding, not noise. `explore diagram` drops it
    because a box with no edge draws nothing; a findings payload must not."""

    duckdb = pytest.importorskip("duckdb")
    warehouse = tmp_path / "isolated.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute("create table lonely (site_name varchar, city varchar)")
    con.execute("insert into lonely values ('a', 'Berlin'), ('b', 'Lisbon')")
    con.execute("create table parent (id integer)")
    con.execute("create table child (id integer, parent_id integer)")
    con.close()

    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        ["explore", "map", "--path", str(warehouse), "--repo-root", str(repo)], capsys
    )["data"]
    reported = {o["identifier"].rsplit(".", 1)[-1] for o in data["objects"]}
    assert "lonely" in reported
    assert data["pii_column_count"] == sum(
        o["pii_column_count"] for o in data["objects"]
    )


def test_map_payload_carries_no_column_value(duckdb_file: Path, tmp_path: Path, capsys):
    """The cache holds min/max and value domains for the columns that earned them.
    This command deliberately does not read them; `explore profile` is where a
    caller asks for a value domain, one object at a time."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)], capsys
    )
    blob = json.dumps(payload["data"])
    for banned in ("min_value", "max_value", "value_domain"):
        assert banned not in blob
    for obj in payload["data"]["objects"]:
        for column in obj["columns"]:
            assert column["pii"] is None or set(column["pii"]) == {
                "category",
                "confidence",
            }


def test_map_always_reports_notes_so_silence_is_a_positive_statement(
    duckdb_file: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)], capsys
    )["data"]
    assert "notes" in data


def test_map_caps_bind_and_every_elision_is_counted(
    wide_tables_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        ["explore", "map", "--path", str(wide_tables_duckdb), "--repo-root", str(repo)],
        capsys,
    )["data"]

    assert data["object_count"] == 32
    assert len(data["objects"]) == summary.MAX_OBJECTS
    assert data["elided_object_count"] == 32 - summary.MAX_OBJECTS
    note = next(n for n in data["notes"] if "capped at" in n and "object" in n)
    assert str(summary.MAX_OBJECTS) in note
    assert "explore profile" in note, "a note names the way to read what it cut"

    assert data["elided_column_count"] > 0
    assert any("column(s) are not described" in n for n in data["notes"])
    assert all(
        len(o["columns"]) <= summary.MAX_COLUMNS_PER_OBJECT for o in data["objects"]
    )
    assert len(data["edges"]) <= summary.MAX_EDGES


def test_map_detail_widens_the_selection_but_never_the_caps(
    wide_tables_duckdb: Path, tmp_path: Path, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [
        "explore",
        "map",
        "--path",
        str(wide_tables_duckdb),
        "--repo-root",
        str(repo),
    ]
    default = _run(argv, capsys)["data"]
    detailed = _run([*argv, "--refresh", "--detail"], capsys)["data"]

    def wide(data: dict) -> dict:
        return next(o for o in data["objects"] if o["identifier"].endswith(".wide"))

    assert len(wide(default)["columns"]) < len(wide(detailed)["columns"])
    assert wide(detailed)["elided_column_count"] < wide(default)["elided_column_count"]
    # `--detail` widens what is eligible. It lifts neither cap, so a table with
    # twenty-two eligible columns still reports twelve, and still says so.
    assert len(wide(detailed)["columns"]) == summary.MAX_COLUMNS_PER_OBJECT
    assert wide(detailed)["elided_column_count"] > 0
    assert len(detailed["objects"]) == summary.MAX_OBJECTS
    assert detailed["elided_object_count"] == default["elided_object_count"]


def test_map_and_diagram_agree_on_which_columns_are_notable(
    duckdb_file: Path, tmp_path: Path, capsys
):
    """Both commands read one predicate (`Dataset.notable_columns`). A caller who
    reads the map and then draws the diagram must not see two different answers."""

    repo = tmp_path / "repo"
    repo.mkdir()
    mapped = _run(
        ["explore", "map", "--path", str(duckdb_file), "--repo-root", str(repo)], capsys
    )["data"]
    drawn = _run(["explore", "diagram", "--repo-root", str(repo)], capsys)["data"]

    joined = {
        o["identifier"]
        for o in mapped["objects"]
        if any(c["role"] == "join" for c in o["columns"])
    }
    for entity in drawn["entities"]:
        if entity["identifier"] not in joined:
            continue
        obj = next(
            o for o in mapped["objects"] if o["identifier"] == entity["identifier"]
        )
        for column in obj["columns"]:
            assert column["name"] in drawn["mermaid"]


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
    store = FilesystemStore(repo)
    first_stamps = {d.identifier: d.profiled_at for d in store.load_cache().datasets}
    assert all(first_stamps.values()), "a full map stamps every profile"

    # --refresh forces the selected top-25 back through profiling (fresh stamp),
    # isolating the below-cutoff carry-forward path from skip-if-cached reuse.
    payload = _run([*argv, "--refresh"], capsys)
    assert payload["data"]["profiled_count"] == 25
    assert payload["data"]["cache_hit_count"] == 0
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


def _stub_meta(identifier: str):
    from exmergo_dex_core.adapters.base import ObjectMeta

    schema = identifier.rsplit(".", 2)[-2]
    name = identifier.rsplit(".", 1)[-1]
    return ObjectMeta(
        identifier=identifier,
        object_type="table",
        schema=schema,
        name=name,
        row_count=10,
        byte_size=100,
        column_count=1,
    )


def _stub_dataset(identifier: str, *, profiled_at: str = "2026-01-01T00:00:00+00:00"):
    from exmergo_dex_core.cache import ColumnProfile

    return Dataset(
        identifier=identifier,
        columns=[ColumnProfile(name="id", data_type="INTEGER")],
        profiled_at=profiled_at,
    )


def test_compose_datasets_carries_forward_objects_outside_this_runs_scope():
    """An object in the prior cache but entirely absent from this run's
    inventory (a --scope/--dataset narrower than what built the prior cache)
    is carried forward untouched rather than dropped from the new cache
    (issue #111): the prior bug replaced the whole cache with just this run's
    scope, silently losing every other dataset's profile."""

    from exmergo_dex_core.explore.commands import _compose_datasets

    prior = DexCache(
        datasets=[
            _stub_dataset("db.shop.customers"),
            _stub_dataset("db.marts.orders"),
        ]
    )
    # This run's inventory only saw the marts dataset (a narrower --scope).
    metas = [_stub_meta("db.marts.orders")]
    datasets, carried, out_of_scope, dropped = _compose_datasets(metas, [], {}, prior)

    identifiers = {d.identifier for d in datasets}
    assert identifiers == {"db.shop.customers", "db.marts.orders"}
    assert carried == 1  # marts itself: in scope, not freshly profiled
    assert out_of_scope == 1  # customers: never even in this run's inventory
    assert dropped == []
    carried_ds = next(d for d in datasets if d.identifier == "db.shop.customers")
    assert carried_ds.profiled_at == "2026-01-01T00:00:00+00:00"
    assert carried_ds.columns  # not degraded to an inventory-only stub


def test_compose_datasets_out_of_scope_dataset_keeps_its_own_rank_score():
    """An out-of-scope carry is not part of this run's ranking pass, so its
    rank_score is left alone rather than reset to None (which would rank it
    last for no reason connectivity or naming ever said was true)."""

    from exmergo_dex_core.explore.commands import _compose_datasets

    out_of_scope_ds = _stub_dataset("db.shop.customers")
    out_of_scope_ds.rank_score = 0.75
    prior = DexCache(datasets=[out_of_scope_ds])
    metas = [_stub_meta("db.marts.orders")]
    profiled = [_stub_dataset("db.marts.orders")]

    datasets, _carried, out_of_scope, _dropped = _compose_datasets(
        metas, profiled, {"db.marts.orders": 0.5}, prior
    )
    assert out_of_scope == 1
    carried_ds = next(d for d in datasets if d.identifier == "db.shop.customers")
    assert carried_ds.rank_score == 0.75


def test_compose_datasets_without_a_prior_cache_reports_no_out_of_scope():
    from exmergo_dex_core.explore.commands import _compose_datasets

    metas = [_stub_meta("db.shop.customers")]
    datasets, carried, out_of_scope, dropped = _compose_datasets(metas, [], {}, None)
    assert [d.identifier for d in datasets] == ["db.shop.customers"]
    assert carried == 0
    assert out_of_scope == 0
    assert dropped == []


def test_compose_datasets_drops_object_gone_from_an_observed_namespace():
    """#149: a prior dataset whose namespace this run's inventory DID look at,
    but who is not itself in metas, is gone from the warehouse -- dropped, not
    carried forward as a ghost that would later pin as a false table_dropped."""

    from exmergo_dex_core.explore.commands import _compose_datasets

    prior = DexCache(
        datasets=[
            _stub_dataset("db.marts.orders"),
            _stub_dataset("db.marts.customers"),  # deleted from the warehouse
        ]
    )
    # This run's inventory saw db.marts (orders survives), so the namespace
    # was observed -- customers' absence is a deletion, not out of scope.
    metas = [_stub_meta("db.marts.orders")]
    datasets, _carried, out_of_scope, dropped = _compose_datasets(
        metas, [], {}, prior, observed_namespaces={"db.marts"}
    )

    assert {d.identifier for d in datasets} == {"db.marts.orders"}
    assert dropped == ["db.marts.customers"]
    assert out_of_scope == 0


def test_compose_datasets_still_carries_forward_a_truly_unscoped_namespace():
    """The counterpart control: a prior dataset in a namespace this run's
    inventory never touched at all stays carried forward untouched (issue
    #111 preserved) even when observed_namespaces is passed."""

    from exmergo_dex_core.explore.commands import _compose_datasets

    prior = DexCache(datasets=[_stub_dataset("db.shop.customers")])
    metas = [_stub_meta("db.marts.orders")]
    datasets, _carried, out_of_scope, dropped = _compose_datasets(
        metas, [], {}, prior, observed_namespaces={"db.marts"}
    )

    assert {d.identifier for d in datasets} == {
        "db.marts.orders",
        "db.shop.customers",
    }
    assert out_of_scope == 1
    assert dropped == []


def _stub_relationship(from_ds: str, to_ds: str) -> Relationship:
    return Relationship(
        from_dataset=from_ds, from_columns=["id"], to_dataset=to_ds, to_columns=["id"]
    )


def test_carry_forward_relationships_drops_edge_with_a_gone_endpoint():
    """#149: an edge whose endpoint's namespace was observed this run but the
    endpoint itself is gone from the warehouse must not resurrect either."""

    from exmergo_dex_core.explore.commands import _carry_forward_relationships

    prior = DexCache(
        relationships=[_stub_relationship("db.marts.orders", "db.marts.customers")]
    )
    examined = {"db.marts.orders"}  # customers no longer profiled: it's gone
    rels, carried = _carry_forward_relationships(
        prior, examined, [], observed_namespaces={"db.marts"}
    )
    assert rels == []
    assert carried == 0


def test_map_drops_relation_deleted_from_the_warehouse_between_maps(
    tmp_path: Path, capsys
):
    """#149: a relation dropped from the warehouse between two `explore map`
    runs must not be resurrected as a carried-forward ghost -- that ghost is
    what `maintain snapshot` would pin and `maintain check` would then report
    as a false high-severity table_dropped for something the operator
    deliberately removed."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA marts")
    conn.execute("CREATE TABLE marts.orders (id INTEGER)")
    conn.execute("CREATE TABLE marts.customers (id INTEGER)")
    conn.close()

    argv = ["explore", "map", "--path", str(path)]
    _run(argv, capsys)

    # The operator drops `customers` outside dex (a raw SQL statement,
    # mirroring a dbt run-operation) -- `orders` in the same schema survives.
    conn = duckdb.connect(str(path))
    conn.execute("DROP TABLE marts.customers")
    conn.close()

    payload = _run(argv, capsys)
    data = payload["data"]
    assert data["dropped_count"] == 1
    assert any("customers" in w for w in payload["warnings"])

    cache = FilesystemStore(tmp_path).load_cache()
    identifiers = {d.identifier for d in cache.datasets}
    assert not any(i.endswith(".customers") for i in identifiers)
    assert any(i.endswith(".orders") for i in identifiers)


def test_carry_forward_relationships_still_carries_a_truly_unscoped_edge():
    from exmergo_dex_core.explore.commands import _carry_forward_relationships

    prior = DexCache(
        relationships=[_stub_relationship("db.marts.orders", "db.shop.customers")]
    )
    examined = {"db.marts.orders"}
    rels, carried = _carry_forward_relationships(
        prior, examined, [], observed_namespaces={"db.marts"}
    )
    assert carried == 1
    assert rels[0].to_dataset == "db.shop.customers"


def test_map_reuses_fresh_cached_profiles(
    many_tables_duckdb: Path, tmp_path: Path, capsys
):
    """A second `map --full` with nothing changed re-scans nothing: every
    selected object is served from the fresh cache, stamps untouched."""

    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [
        "explore",
        "map",
        "--path",
        str(many_tables_duckdb),
        "--repo-root",
        str(repo),
        "--full",
    ]
    _run(argv, capsys)
    store = FilesystemStore(repo)
    first_stamps = {d.identifier: d.profiled_at for d in store.load_cache().datasets}

    payload = _run(argv, capsys)
    assert payload["data"]["profiled_count"] == 0
    assert payload["data"]["cache_hit_count"] == 60
    assert payload["data"]["skipped_count"] == 0
    assert any("reused 60 fresh cached profile" in n for n in payload["data"]["notes"])

    cache = store.load_cache()
    assert all(d.columns for d in cache.datasets)
    assert {d.identifier: d.profiled_at for d in cache.datasets} == first_stamps, (
        "reused profiles keep their original stamp; nothing was re-scanned"
    )


def test_map_reprofiles_only_the_schema_changed_object(
    many_tables_duckdb: Path, tmp_path: Path, capsys
):
    """Altering one table's columns between two `map --full` runs re-profiles
    only that object; the rest stay fresh cache hits."""

    duckdb = pytest.importorskip("duckdb")
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [
        "explore",
        "map",
        "--path",
        str(many_tables_duckdb),
        "--repo-root",
        str(repo),
        "--full",
    ]
    _run(argv, capsys)
    store = FilesystemStore(repo)
    first_stamps = {d.identifier: d.profiled_at for d in store.load_cache().datasets}

    conn = duckdb.connect(str(many_tables_duckdb))
    conn.execute("ALTER TABLE t_00 ADD COLUMN extra VARCHAR")
    conn.close()

    payload = _run(argv, capsys)
    assert payload["data"]["profiled_count"] == 1
    assert payload["data"]["cache_hit_count"] == 59

    cache = store.load_cache()
    reprofiled = [
        d.identifier
        for d in cache.datasets
        if d.profiled_at != first_stamps[d.identifier]
    ]
    assert len(reprofiled) == 1
    assert reprofiled[0].endswith(".t_00")
    altered = next(d for d in cache.datasets if d.identifier.endswith(".t_00"))
    assert any(c.name == "extra" for c in altered.columns), (
        "re-profile saw the new column"
    )


def test_map_freshness_zero_disables_reuse(
    many_tables_duckdb: Path, tmp_path: Path, capsys
):
    """profile_freshness_hours: 0 forces a re-profile of every selected object
    even when the schema is unchanged and the cache was just written."""

    repo = tmp_path / "repo"
    repo.mkdir()
    save_config(DexConfig(profile_freshness_hours=0.0), repo)
    argv = [
        "explore",
        "map",
        "--path",
        str(many_tables_duckdb),
        "--repo-root",
        str(repo),
        "--full",
    ]
    _run(argv, capsys)
    payload = _run(argv, capsys)
    assert payload["data"]["profiled_count"] == 60
    assert payload["data"]["cache_hit_count"] == 0


def test_map_refresh_forces_full_reprofile(
    many_tables_duckdb: Path, tmp_path: Path, capsys
):
    """`--refresh` re-scans every selected object even when all are fresh."""

    repo = tmp_path / "repo"
    repo.mkdir()
    argv = [
        "explore",
        "map",
        "--path",
        str(many_tables_duckdb),
        "--repo-root",
        str(repo),
        "--full",
    ]
    _run(argv, capsys)
    payload = _run([*argv, "--refresh"], capsys)
    assert payload["data"]["profiled_count"] == 60
    assert payload["data"]["cache_hit_count"] == 0


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


def _orphan_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A warehouse with a staging model's built table (stg_orders) plus a
    same-schema leftover (stg_legacy_orders) no model or source claims."""

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE stg_orders (id INTEGER)")
    conn.execute("CREATE TABLE stg_legacy_orders (id INTEGER)")
    conn.close()

    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models" / "stg_orders.sql").write_text("select * from x", encoding="utf-8")
    return db, repo


def test_map_downranks_and_badges_orphan_relation(tmp_path: Path, capsys):
    db, repo = _orphan_repo(tmp_path)
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
    cache = FilesystemStore(repo).load_cache()
    orphan = next(
        d for d in cache.datasets if d.identifier.endswith(".stg_legacy_orders")
    )
    backed = next(d for d in cache.datasets if d.identifier.endswith(".stg_orders"))
    assert any("orphaned residue" in n for n in orphan.data_quality)
    assert orphan.rank_score < backed.rank_score


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
    cache = FilesystemStore(repo).load_cache()
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
    cache = FilesystemStore(repo).load_cache()
    (orders,) = [d for d in cache.datasets if d.identifier.endswith(".orders")]
    assert any(
        "status is declared unique" in n and "duplicates" in n
        for n in orders.data_quality
    )
    # Measurement-only: the contradicted declared key must not appear as a
    # candidate key just because the project claims it.
    assert ["status"] not in orders.candidate_keys


# --- declared composite keys (#169): _annotate_grain unit tests --------------


def _col(name: str, *, is_unique: bool | None = None, null_fraction: float = 0.0):
    return ColumnProfile(
        name=name, data_type="INTEGER", is_unique=is_unique, null_fraction=null_fraction
    )


def _composite_defs(*columns: str, model: str = "line_items") -> ProjectDefinitions:
    return ProjectDefinitions(
        present=True,
        declared_composite_keys=[
            DeclaredCompositeKey(model=model, columns=list(columns), source="yaml")
        ],
    )


def test_declared_composite_fills_in_when_measurement_finds_no_grain():
    """The gap the issue reports: measurement alone has nothing (no single
    column is unique, no composite was probed), so declaration is the only
    route to a grain at all."""

    ds = Dataset(
        identifier="db.main.line_items",
        columns=[_col("order_id"), _col("line_number"), _col("amount")],
    )
    _annotate_grain([ds], _composite_defs("order_id", "line_number"))
    assert ds.grain == ["order_id", "line_number"]
    # candidate_keys stays measurement-only: nothing was measured here.
    assert ds.candidate_keys == []


def test_declared_composite_overrides_a_measured_composite_guess():
    ds = Dataset(
        identifier="db.main.line_items",
        columns=[
            _col("order_id"),
            _col("line_number"),
            _col("warehouse_id"),
            _col("bin_id"),
        ],
        composite_keys=[["warehouse_id", "bin_id"]],
    )
    _annotate_grain([ds], _composite_defs("order_id", "line_number"))
    assert ds.grain == ["order_id", "line_number"]
    assert any("declared composite key" in n for n in ds.data_quality)


def test_proven_single_column_wins_over_declared_composite():
    """The override guard: a measurement-proven single-column grain is not
    silently discarded just because a composite is also declared."""

    ds = Dataset(
        identifier="db.main.line_items",
        columns=[
            _col("id", is_unique=True, null_fraction=0.0),
            _col("order_id"),
            _col("line_number"),
        ],
    )
    _annotate_grain([ds], _composite_defs("order_id", "line_number"))
    assert ds.grain == ["id"]
    assert any("also declared for this table" in n for n in ds.data_quality)


def test_declared_composite_with_missing_column_is_not_applied():
    ds = Dataset(identifier="db.main.line_items", columns=[_col("order_id")])
    _annotate_grain([ds], _composite_defs("order_id", "line_number"))
    assert ds.grain is None
    assert any("not applied" in n for n in ds.data_quality)


def test_multiple_declared_composites_uses_first_and_notes_the_rest():
    ds = Dataset(
        identifier="db.main.line_items",
        columns=[
            _col("order_id"),
            _col("line_number"),
            _col("sku"),
            _col("warehouse_id"),
        ],
    )
    defs = ProjectDefinitions(
        present=True,
        declared_composite_keys=[
            DeclaredCompositeKey(
                model="line_items", columns=["order_id", "line_number"], source="yaml"
            ),
            DeclaredCompositeKey(
                model="line_items", columns=["sku", "warehouse_id"], source="yaml"
            ),
        ],
    )
    _annotate_grain([ds], defs)
    assert ds.grain == ["order_id", "line_number"]
    assert any("2 composite keys are declared" in n for n in ds.data_quality)


# --- declared composite keys: end-to-end plumbing -----------------------------


def _composite_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A line_items table where no single column is unique but (order_id,
    line_number) is, plus a schema.yml declaring exactly that combination."""

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(
        "CREATE TABLE line_items (order_id INTEGER, line_number INTEGER, amount DOUBLE)"
    )
    conn.execute(
        "INSERT INTO line_items VALUES "
        "(1, 1, 10.0), (1, 2, 20.0), (2, 1, 10.0), (2, 2, 20.0)"
    )
    conn.close()

    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    (repo / "models" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: line_items\n"
        "    tests:\n"
        "      - unique_combination_of_columns:\n"
        "          combination_of_columns: [order_id, line_number]\n",
        encoding="utf-8",
    )
    return db, repo


def test_map_declared_composite_key_is_elected_as_grain(tmp_path: Path, capsys):
    db, repo = _composite_repo(tmp_path)
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
    cache = FilesystemStore(repo).load_cache()
    (line_items,) = [d for d in cache.datasets if d.identifier.endswith(".line_items")]
    assert line_items.grain == ["order_id", "line_number"]


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
    cache = FilesystemStore(repo).load_cache()
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


# --- the semantic layer, folded into the map (#360, #361) ---------------------


def _semantic_layer_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A warehouse whose two tables join on differently named columns, plus a
    project whose compiled semantic layer declares that join and sits one of its
    models on a third table nothing exposes.

    `buyer_id` against `id` is the join name-based inference cannot find, and
    `audit_log` is the object the layer does not expose, so both halves of the
    annotation have something to say.
    """

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE customers (id INTEGER, region VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'eu'), (2, 'us')")
    conn.execute("CREATE TABLE orders (order_id INTEGER, buyer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 1), (2, 1), (3, 2)")
    conn.execute("CREATE TABLE audit_log (event_id INTEGER)")
    conn.execute("INSERT INTO audit_log VALUES (1), (2)")
    conn.close()

    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "target").mkdir(parents=True)
    (repo / "dbt_project.yml").write_text(
        'name: dex_test\nversion: "1.0.0"\nmodel-paths: ["models"]\n',
        encoding="utf-8",
    )
    manifest = {
        "semantic_models": [
            {
                "name": "customers_sm",
                # Quoted and with a database component that disagrees with the
                # DuckDB file stem, which is the ordinary case: the suffix pins it.
                "node_relation": {
                    "alias": "customers",
                    "relation_name": '"analytics"."main"."customers"',
                },
                "entities": [{"name": "customer", "type": "primary", "expr": "id"}],
                "dimensions": [{"name": "region", "type": "categorical"}],
                "measures": [{"name": "customer_count", "agg": "count", "expr": "id"}],
            },
            {
                "name": "orders_sm",
                "node_relation": {
                    "alias": "orders",
                    "relation_name": '"analytics"."main"."orders"',
                },
                "entities": [
                    {"name": "order", "type": "primary", "expr": "order_id"},
                    {"name": "customer", "type": "foreign", "expr": "buyer_id"},
                ],
                "dimensions": [],
                "measures": [{"name": "order_count", "agg": "count"}],
            },
        ],
        "metrics": [
            {
                "name": "orders",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "order_count"}]},
            }
        ],
    }
    (repo / "target" / "semantic_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return db, repo


def _map_with_project(db: Path, repo: Path, capsys) -> dict:
    return _run(
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
    )["data"]


def test_map_says_which_objects_the_semantic_layer_exposes(tmp_path: Path, capsys):
    """The physical half of the link, and what makes "is this table load-bearing"
    answerable from the map: a relation several metrics are built on is a
    different object from one nothing reads, and row count and PII flags cannot
    tell them apart."""

    db, repo = _semantic_layer_repo(tmp_path)

    data = _map_with_project(db, repo, capsys)

    exposure = {
        o["identifier"].rsplit(".", 1)[-1]: o["semantic_models"]
        for o in data["objects"]
    }
    assert exposure["customers"] == ["customers_sm"]
    assert exposure["orders"] == ["orders_sm"]
    # Exposed through nothing is an answer, not a gap.
    assert exposure["audit_log"] == []
    assert any(
        "exposed through the project's semantic layer" in n for n in data["notes"]
    )


def test_map_draws_the_join_the_semantic_layer_declares(tmp_path: Path, capsys):
    """`buyer_id` against `id` shares no name, so nothing dex infers would find
    it. The layer states it outright, with the key named per model."""

    db, repo = _semantic_layer_repo(tmp_path)

    data = _map_with_project(db, repo, capsys)

    declared = [e for e in data["edges"] if e["declared_by"]]
    assert len(declared) == 1
    edge = declared[0]
    assert edge["from_dataset"].endswith(".orders")
    assert edge["from_columns"] == ["buyer_id"]
    assert edge["to_dataset"].endswith(".customers")
    assert edge["to_columns"] == ["id"]
    assert edge["kind"] == "declared" and edge["confidence"] == 1.0
    assert edge["declared_by"] == "semantic entity 'customer'"
    assert any("not found by name-based inference" in n for n in data["notes"])


def test_exploration_still_starts_bare_without_use_project(tmp_path: Path, capsys):
    """The default path is unchanged, which is what keeps this a minor release and
    what keeps a warehouse observation independent of whichever repo dex runs
    from."""

    db, repo = _semantic_layer_repo(tmp_path)

    data = _run(
        ["explore", "map", "--path", str(db), "--repo-root", str(repo)], capsys
    )["data"]

    assert all(o["semantic_models"] == [] for o in data["objects"])
    assert all(e["declared_by"] is None for e in data["edges"])


def test_a_model_dropped_from_the_layer_clears_its_annotation(tmp_path: Path, capsys):
    """A stale exposure claim is worse than none: it says a table backs a metric
    that no longer reads it. Every dataset in view is rewritten whenever a
    catalog was read, so the annotation cannot outlive the declaration."""

    db, repo = _semantic_layer_repo(tmp_path)
    _map_with_project(db, repo, capsys)

    manifest = json.loads((repo / "target" / "semantic_manifest.json").read_text())
    manifest["semantic_models"] = [
        m for m in manifest["semantic_models"] if m["name"] != "orders_sm"
    ]
    (repo / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))

    data = _map_with_project(db, repo, capsys)

    exposure = {
        o["identifier"].rsplit(".", 1)[-1]: o["semantic_models"]
        for o in data["objects"]
    }
    assert exposure["orders"] == []
    assert exposure["customers"] == ["customers_sm"]
    # The join went with it: the layer no longer declares that entity on orders.
    assert not [e for e in data["edges"] if e["declared_by"]]


def test_the_semantic_read_survives_a_project_with_no_compiled_layer(
    tmp_path: Path, capsys
):
    """An uncompiled project is an ordinary state on the explore path, where the
    warehouse is the subject and a project is a bonus. `explore semantic list` is
    the command whose subject is the layer, and it is the one that refuses."""

    db, repo = _semantic_layer_repo(tmp_path)
    (repo / "target" / "semantic_manifest.json").unlink()

    data = _map_with_project(db, repo, capsys)

    assert data["objects"], "the map still describes the warehouse"
    assert all(o["semantic_models"] == [] for o in data["objects"])
