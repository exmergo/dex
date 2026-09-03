"""Relationship inference, grain detection, and data-quality interpretation.

Unit tests build Dataset models directly to pin the matching and scoring rules;
the envelope tests replay the two field sessions (camelCase F1 star schema,
RAW_-prefixed Airbnb export) that previously returned zero relationships.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_manifest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    Relationship,
    RelationshipKind,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.config import EntityAffixes
from exmergo_dex_core.dbt_project import DeclaredForeignKey, ProjectDefinitions
from exmergo_dex_core.explore.commands import (
    _carry_forward_relationships,
    _declared_relationship_conflicts,
    _fold_semantic_edges,
    _merge_relationships,
    _relationship_conflict_notes,
    _semantic_join_notes,
)
from exmergo_dex_core.explore.profile import profile
from exmergo_dex_core.explore.relationships import (
    candidate_keys,
    data_quality_notes,
    declared_relationships,
    detect_grain,
    fk_candidate_count,
    fold_replica_relationships,
    infer_relationships,
    probe_batches,
    semantic_relationships,
    verify_relationships,
)
from exmergo_dex_core.progress import PROGRESS_FIRST_AFTER, ProgressReporter
from exmergo_dex_core.semantic_catalog import EntityJoin
from exmergo_dex_core.storage import FilesystemStore


def _col(
    name: str,
    data_type: str = "INTEGER",
    *,
    distinct: int | None = None,
    unique: bool = False,
    mn: object | None = None,
    mx: object | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        data_type=data_type,
        null_fraction=0.0,
        distinct_count=distinct,
        is_unique=unique,
        min_value=mn,
        max_value=mx,
    )


def _ds(
    identifier: str, columns: list[ColumnProfile], rows: int | None = None
) -> Dataset:
    return Dataset(identifier=identifier, row_count=rows, columns=columns)


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


# --- matching rules ------------------------------------------------------------


def test_camelcase_fk_matches_camelcase_parent_key():
    races = _ds("db.main.races", [_col("raceId", distinct=2, unique=True)], rows=2)
    results = _ds("db.main.results", [_col("raceId", distinct=2)], rows=3)
    rels = infer_relationships([races, results])
    assert len(rels) == 1
    rel = rels[0]
    assert rel.from_dataset == "db.main.results"
    assert rel.from_columns == ["raceId"]
    assert rel.to_dataset == "db.main.races"
    assert rel.to_columns == ["raceId"]
    assert rel.confidence >= 0.85


def test_layer_prefix_is_stripped_from_parent_name():
    hosts = _ds("db.main.RAW_HOSTS", [_col("ID", distinct=2, unique=True)], rows=2)
    listings = _ds("db.main.RAW_LISTINGS", [_col("HOST_ID", distinct=2)], rows=2)
    rels = infer_relationships([hosts, listings])
    assert len(rels) == 1
    assert rels[0].from_columns == ["HOST_ID"]
    assert rels[0].to_dataset == "db.main.RAW_HOSTS"
    assert rels[0].to_columns == ["ID"]


def test_already_singular_parent_table_matches():
    """`status` must not be mangled to `statu` by the inflector."""

    status = _ds("db.main.status", [_col("statusId", distinct=5, unique=True)], rows=5)
    results = _ds("db.main.results", [_col("statusId", distinct=3)], rows=9)
    rels = infer_relationships([status, results])
    assert len(rels) == 1
    assert rels[0].to_dataset == "db.main.status"


def test_non_unique_parent_key_still_emits_at_reduced_confidence():
    """A broken parent grain is a data-quality problem, not a reason to hide the
    join; the fan-out risk is reported separately by data_quality_notes."""

    hosts = _ds("db.main.RAW_HOSTS", [_col("ID", distinct=9590)], rows=14111)
    listings = _ds("db.main.RAW_LISTINGS", [_col("HOST_ID", distinct=9000)], rows=17500)
    rels = infer_relationships([hosts, listings])
    assert len(rels) == 1
    assert rels[0].confidence < 0.7  # well below the unique-parent base of 0.85
    assert rels[0].confidence > 0.0


def test_distinct_count_violation_lowers_confidence():
    parent = _ds("db.main.customers", [_col("id", distinct=5, unique=True)], rows=5)
    contained = _ds("db.main.orders", [_col("customer_id", distinct=3)], rows=10)
    violating = _ds("db.main.refunds", [_col("customer_id", distinct=9)], rows=10)
    ok = infer_relationships([parent, contained])
    bad = infer_relationships([parent, violating])
    assert ok[0].confidence > bad[0].confidence


def test_numeric_range_containment_raises_confidence():
    parent = _ds(
        "db.main.customers", [_col("id", distinct=5, unique=True, mn=1, mx=5)], rows=5
    )
    inside = _ds(
        "db.main.orders", [_col("customer_id", distinct=3, mn=1, mx=4)], rows=8
    )
    outside = _ds(
        "db.main.events", [_col("customer_id", distinct=3, mn=1, mx=99)], rows=8
    )
    contained = infer_relationships([parent, inside])
    escaped = infer_relationships([parent, outside])
    assert contained[0].confidence > escaped[0].confidence


def test_type_incompatible_columns_do_not_match():
    parent = _ds("db.main.customers", [_col("id", distinct=5, unique=True)], rows=5)
    child = _ds("db.main.orders", [_col("customer_id", "VARCHAR", distinct=3)], rows=8)
    assert infer_relationships([parent, child]) == []


def test_ambiguous_all_caps_id_suffix_is_not_a_fk():
    """HOSTID (no separator) and PAID are not id-shaped; HOST_ID and hostId are."""

    ds = _ds(
        "db.main.t",
        [_col("HOSTID"), _col("PAID"), _col("HOST_ID"), _col("hostId"), _col("id")],
        rows=1,
    )
    assert fk_candidate_count([ds]) == 2


def test_underscore_key_suffix_matches_like_id():
    """Dimensional models commonly use `<entity>_key` surrogate keys instead of
    `<entity>_id`; the same matching rules must apply (issue #45)."""

    customers = _ds(
        "db.main.customers", [_col("customer_key", distinct=2, unique=True)], rows=2
    )
    orders = _ds("db.main.orders", [_col("customer_key", distinct=2)], rows=5)
    rels = infer_relationships([customers, orders])
    assert len(rels) == 1
    assert rels[0].from_columns == ["customer_key"]
    assert rels[0].to_dataset == "db.main.customers"
    assert rels[0].confidence >= 0.85


def test_camelcase_key_suffix_matches():
    parts = _ds("db.main.parts", [_col("partKey", distinct=3, unique=True)], rows=3)
    lines = _ds("db.main.lines", [_col("partKey", distinct=2)], rows=6)
    rels = infer_relationships([parts, lines])
    assert len(rels) == 1
    assert rels[0].to_dataset == "db.main.parts"


def test_bare_key_is_a_key_not_a_foreign_key():
    """A column literally named `key` (like bare `id`) has no entity stem."""

    ds = _ds("db.main.t", [_col("key", unique=True), _col("id")], rows=1)
    assert fk_candidate_count([ds]) == 0


def test_tpch_alias_prefixed_keys_are_inferred():
    """TPC-H names every FK after the child table's own alias, not the parent's
    entity name (`L_ORDERKEY` on LINEITEM, not `ORDERS_KEY`), and concatenates the
    suffix with no separator at all (`CUSTKEY`, not `CUST_KEY`). Neither the
    entity-name branch nor a bare `_id`-only stem detector can see these joins;
    covers the exact chain reported in issue #45."""

    region = _ds(
        "db.tpch.region", [_col("R_REGIONKEY", distinct=5, unique=True)], rows=5
    )
    nation = _ds(
        "db.tpch.nation",
        [
            _col("N_NATIONKEY", distinct=25, unique=True),
            _col("N_REGIONKEY", distinct=5),
        ],
        rows=25,
    )
    supplier = _ds(
        "db.tpch.supplier",
        [
            _col("S_SUPPKEY", distinct=100, unique=True),
            _col("S_NATIONKEY", distinct=25),
        ],
        rows=100,
    )
    customer = _ds(
        "db.tpch.customer",
        [
            _col("C_CUSTKEY", distinct=150, unique=True),
            _col("C_NATIONKEY", distinct=25),
        ],
        rows=150,
    )
    part = _ds("db.tpch.part", [_col("P_PARTKEY", distinct=200, unique=True)], rows=200)
    orders = _ds(
        "db.tpch.orders",
        [
            _col("O_ORDERKEY", distinct=1500, unique=True),
            _col("O_CUSTKEY", distinct=150),
        ],
        rows=1500,
    )
    lineitem = _ds(
        "db.tpch.lineitem",
        [
            _col("L_ORDERKEY", distinct=1500),
            _col("L_PARTKEY", distinct=200),
            _col("L_SUPPKEY", distinct=100),
        ],
        rows=6000,
    )
    datasets = [region, nation, supplier, customer, part, orders, lineitem]
    rels = infer_relationships(datasets)

    found = {(r.from_dataset, r.from_columns[0], r.to_dataset) for r in rels}
    assert ("db.tpch.orders", "O_CUSTKEY", "db.tpch.customer") in found
    assert ("db.tpch.lineitem", "L_ORDERKEY", "db.tpch.orders") in found
    assert ("db.tpch.lineitem", "L_PARTKEY", "db.tpch.part") in found
    assert ("db.tpch.lineitem", "L_SUPPKEY", "db.tpch.supplier") in found
    assert ("db.tpch.supplier", "S_NATIONKEY", "db.tpch.nation") in found
    assert ("db.tpch.customer", "C_NATIONKEY", "db.tpch.nation") in found
    assert ("db.tpch.nation", "N_REGIONKEY", "db.tpch.region") in found


def _cdc_collections(n: int) -> list[Dataset]:
    """`n` unrelated tables, each with its own `document_id` unique key, shaped
    like a Firestore/Mongo/DynamoDB-style CDC export."""

    return [
        _ds(
            f"db.main.collection_{i}",
            [_col("document_id", distinct=5, unique=True)],
            rows=5,
        )
        for i in range(n)
    ]


def test_generic_id_name_shared_across_many_tables_is_not_matched():
    """A column name that's the norm for every collection in Firestore/Mongo/
    DynamoDB-style CDC exports (e.g. `document_id`) is a naming convention, not
    a reference: matching on it alone across many unrelated tables would
    otherwise generate a near-complete cross product (issue #77)."""

    assert infer_relationships(_cdc_collections(4)) == []


def test_generic_id_name_match_is_recorded_as_suppressed():
    suppressed: list = []
    assert infer_relationships(_cdc_collections(4), suppressed=suppressed) == []
    assert len(suppressed) == 4 * 3  # every ordered (child, parent) pair
    assert all(s.shared_name == "document_id" for s in suppressed)
    assert all(s.host_count == 4 for s in suppressed)


def test_generic_name_threshold_is_three_hosts():
    """Below the threshold, a shared exact key name is an ordinary same-named
    FK; at or above it, it's treated as a naming convention."""

    two_hosts = [
        _ds(f"db.main.t{i}", [_col("thing_id", distinct=2, unique=True)], rows=2)
        for i in range(2)
    ]
    assert len(infer_relationships(two_hosts)) == 2  # each matches the other

    three_hosts = [
        _ds(f"db.main.t{i}", [_col("thing_id", distinct=2, unique=True)], rows=2)
        for i in range(3)
    ]
    assert infer_relationships(three_hosts) == []


def test_shared_key_name_below_generic_threshold_still_matches():
    """A same-named FK shared by only one other table (accounts.customer_id and
    orders.customer_id, with no entity-name tie to `orders`) is a real signal,
    not a naming convention, and must still be inferred."""

    accounts = _ds(
        "db.main.accounts", [_col("customer_id", distinct=2, unique=True)], rows=2
    )
    orders = _ds("db.main.orders", [_col("customer_id", distinct=2)], rows=5)
    rels = infer_relationships([accounts, orders])
    assert len(rels) == 1
    assert rels[0].to_dataset == "db.main.accounts"


def test_dealiased_match_skips_when_stripped_to_a_bare_suffix():
    """`x_key` / `y_key` collapse to the bare suffix `key` once dealiased; that's
    too generic to trust, so two unrelated single-letter-prefixed keys must not
    be matched to each other."""

    a = _ds("db.main.alpha", [_col("a_key", distinct=2, unique=True)], rows=2)
    b = _ds("db.main.beta", [_col("b_key", distinct=2)], rows=2)
    assert infer_relationships([a, b]) == []


# --- affix-stripped entity matching (issue #208) -------------------------------


def test_history_data_suffixed_parent_is_not_matched_without_affixes_configured():
    """The affix-stripping tier is opt-in per call: a caller that doesn't pass
    `affixes` sees the exact pre-fix behavior, unchanged."""

    parent = _ds(
        "db.main.conversation_history_data",
        [_col("id", distinct=5, unique=True)],
        rows=5,
    )
    child = _ds(
        "db.main.conversation_part_history_data",
        [_col("conversation_id", distinct=5)],
        rows=20,
    )
    assert infer_relationships([parent, child]) == []


def test_history_data_suffix_is_stripped_when_affixes_are_configured():
    """The exact scenario from issue #208: singularizing conversation_id yields
    conversation, but the parent table carries both a CDC history suffix and a
    landing-zone data suffix. Stripping both in sequence (`_data`, then
    `_history`) recovers the match."""

    parent = _ds(
        "db.main.conversation_history_data",
        [_col("id", distinct=5, unique=True)],
        rows=5,
    )
    child = _ds(
        "db.main.conversation_part_history_data",
        [_col("conversation_id", distinct=5)],
        rows=20,
    )
    rels = infer_relationships([parent, child], affixes=EntityAffixes())
    assert len(rels) == 1
    rel = rels[0]
    assert rel.from_dataset == "db.main.conversation_part_history_data"
    assert rel.from_columns == ["conversation_id"]
    assert rel.to_dataset == "db.main.conversation_history_data"
    assert rel.to_columns == ["id"]
    assert rel.confidence < 0.85


def test_affix_stripped_match_scores_below_an_exact_match_to_the_same_shape():
    """A match that needed stripping must rank below an unambiguous exact
    match, so ranking still prefers the case that needed no help."""

    exact = infer_relationships(
        [
            _ds("db.main.products", [_col("id", distinct=5, unique=True)], rows=5),
            _ds(
                "db.main.inventory_transactions",
                [_col("product_id", distinct=5)],
                rows=20,
            ),
        ],
        affixes=EntityAffixes(),
    )
    stripped = infer_relationships(
        [
            _ds(
                "db.main.product_history_data",
                [_col("id", distinct=5, unique=True)],
                rows=5,
            ),
            _ds("db.main.inventory_events", [_col("product_id", distinct=5)], rows=20),
        ],
        affixes=EntityAffixes(),
    )
    assert len(exact) == 1
    assert len(stripped) == 1
    assert exact[0].confidence > stripped[0].confidence


def test_versioned_parent_table_suffix_is_stripped():
    """A versioned table (`_v2`) is a structural convention, always stripped,
    not part of the configurable affix list."""

    parent = _ds("db.main.products_v2", [_col("id", distinct=5, unique=True)], rows=5)
    child = _ds("db.main.orders", [_col("product_id", distinct=5)], rows=20)
    rels = infer_relationships([parent, child], affixes=EntityAffixes())
    assert len(rels) == 1
    assert rels[0].to_dataset == "db.main.products_v2"


def test_layer_prefix_alone_needs_no_affix_config():
    """A bare layer prefix (already handled by `_LAYER_PREFIX`) must still
    match with `affixes=None`; the new tier only extends what the exact tier
    can't already see."""

    hosts = _ds("db.main.RAW_HOSTS", [_col("ID", distinct=2, unique=True)], rows=2)
    listings = _ds("db.main.RAW_LISTINGS", [_col("HOST_ID", distinct=2)], rows=2)
    rels = infer_relationships([hosts, listings])
    assert len(rels) == 1
    assert rels[0].confidence >= 0.85


def test_ambiguous_affix_stripped_candidates_are_both_proposed():
    """Where stripping produces two candidate parents, both must be proposed;
    the matcher must not pick a winner (issue #208 acceptance criterion)."""

    history = _ds(
        "db.main.conversation_history_data",
        [_col("id", distinct=5, unique=True)],
        rows=5,
    )
    current = _ds(
        "db.main.conversation_current_data",
        [_col("id", distinct=5, unique=True)],
        rows=5,
    )
    replies = _ds("db.main.replies", [_col("conversation_id", distinct=5)], rows=20)
    rels = infer_relationships([history, current, replies], affixes=EntityAffixes())
    targets = {r.to_dataset for r in rels}
    assert targets == {
        "db.main.conversation_history_data",
        "db.main.conversation_current_data",
    }


def test_affix_stripped_match_is_recorded():
    affix_matches: list = []
    parent = _ds(
        "db.main.conversation_history_data",
        [_col("id", distinct=5, unique=True)],
        rows=5,
    )
    child = _ds(
        "db.main.conversation_part_history_data",
        [_col("conversation_id", distinct=5)],
        rows=20,
    )
    rels = infer_relationships(
        [parent, child], affixes=EntityAffixes(), affix_matches=affix_matches
    )
    assert len(rels) == 1
    assert len(affix_matches) == 1
    assert affix_matches[0].child_column == "conversation_id"
    assert affix_matches[0].parent == "db.main.conversation_history_data"
    assert affix_matches[0].stripped_to == "conversation"


def test_configured_affixes_can_be_narrowed_or_disabled():
    """The affix list is configurable and small by default, not exhaustive: a
    project can shrink or empty it, and a suffix outside the configured set
    stays unmatched."""

    parent = _ds(
        "db.main.conversation_history_data",
        [_col("id", distinct=5, unique=True)],
        rows=5,
    )
    child = _ds(
        "db.main.conversation_part_history_data",
        [_col("conversation_id", distinct=5)],
        rows=20,
    )
    assert (
        infer_relationships([parent, child], affixes=EntityAffixes(suffixes=[])) == []
    )


def test_relationships_envelope_explains_affix_stripped_matches(tmp_path: Path, capsys):
    """End-to-end: `explore relationships` wires the default configured
    `entity_affixes` through by default, proposes the affix-stripped edge, and
    explains it in `notes`."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "intercom.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE conversation_history_data (id INTEGER)")
    conn.execute("INSERT INTO conversation_history_data SELECT * FROM range(5)")
    conn.execute(
        "CREATE TABLE conversation_part_history_data (conversation_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO conversation_part_history_data SELECT i % 5 FROM range(20) t(i)"
    )
    conn.close()

    payload = _run(["explore", "relationships", "--path", str(path)], capsys)
    data = payload["data"]
    by_fk = {tuple(r["from_columns"]): r for r in data["relationships"]}
    rel = by_fk[("conversation_id",)]
    assert rel["to_dataset"].endswith(".conversation_history_data")
    assert rel["to_columns"] == ["id"]
    assert any("stripping a configured prefix/suffix" in n for n in data["notes"])


def test_affix_stripped_join_survives_verify(tmp_path: Path, capsys):
    """The exact scenario from issue #208's acceptance criteria: the
    affix-stripped join is proposed and survives `--verify` (zero orphans lift
    its confidence rather than the probe demoting it away)."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "intercom.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE conversation_history_data (id INTEGER)")
    conn.execute("INSERT INTO conversation_history_data SELECT * FROM range(5)")
    conn.execute(
        "CREATE TABLE conversation_part_history_data (conversation_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO conversation_part_history_data SELECT i % 5 FROM range(20) t(i)"
    )
    conn.close()

    payload = _run(
        ["explore", "relationships", "--path", str(path), "--verify"], capsys
    )
    data = payload["data"]
    by_fk = {tuple(r["from_columns"]): r for r in data["relationships"]}
    rel = by_fk[("conversation_id",)]
    assert rel["to_dataset"].endswith(".conversation_history_data")
    assert rel["verified"] is True
    assert rel["orphan_fraction"] == 0.0


# --- same-lineage / replica folding --------------------------------------------


def _mirror_world(dev_schema: str = "dbt_dev") -> list[Dataset]:
    """A source schema and a dev/replica schema holding the same entities: the
    shape that inflated one foreign key into several edges in the field."""

    return [
        _ds(
            "db.main.orders",
            [_col("id", distinct=3, unique=True), _col("customer_id", distinct=2)],
            rows=3,
        ),
        _ds("db.main.customers", [_col("id", distinct=2, unique=True)], rows=2),
        _ds(
            f"db.{dev_schema}.stg_orders",
            [_col("id", distinct=3, unique=True), _col("customer_id", distinct=2)],
            rows=3,
        ),
        _ds(
            f"db.{dev_schema}.dim_customers",
            [_col("id", distinct=2, unique=True)],
            rows=2,
        ),
    ]


def test_replica_dataset_duplicate_edges_are_folded():
    datasets = _mirror_world()
    rels = infer_relationships(datasets)
    assert len(rels) == 4  # source, replica, and two cross-dataset lookalikes

    kept, folded, mirrored = fold_replica_relationships(
        datasets, rels, frozenset({"dbt_dev"})
    )
    assert folded == 3
    assert len(kept) == 1
    # The kept edge is the source-schema one, named by the dev_dataset config.
    assert kept[0].from_dataset == "db.main.orders"
    assert kept[0].to_dataset == "db.main.customers"
    assert mirrored == 2  # the two dbt_dev objects


def test_fold_detects_mirror_structurally_without_config():
    datasets = _mirror_world()
    rels = infer_relationships(datasets)
    kept, folded, mirrored = fold_replica_relationships(datasets, rels)
    # No dev_schemas passed: structural mirror detection still collapses the
    # duplicates (canonical chosen deterministically).
    assert folded == 3
    assert len(kept) == 1
    assert mirrored == 2


def test_fold_is_a_noop_without_a_mirror():
    datasets = [
        _ds(
            "db.main.orders",
            [_col("id", distinct=3, unique=True), _col("customer_id", distinct=2)],
            rows=3,
        ),
        _ds("db.main.customers", [_col("id", distinct=2, unique=True)], rows=2),
    ]
    rels = infer_relationships(datasets)
    kept, folded, mirrored = fold_replica_relationships(
        datasets, rels, frozenset({"dbt_dev"})
    )
    assert kept == rels
    assert folded == 0
    assert mirrored == 0


def test_fold_matches_qualified_dev_dataset_config():
    """A BigQuery-style qualified dev_dataset (`project.dataset`) must still mark
    the replica schema, so the source edge is kept as canonical."""

    datasets = _mirror_world()
    rels = infer_relationships(datasets)
    kept, folded, mirrored = fold_replica_relationships(
        datasets, rels, frozenset({"db.dbt_dev"})
    )
    assert folded == 3
    assert len(kept) == 1
    assert kept[0].from_dataset == "db.main.orders"
    assert kept[0].to_dataset == "db.main.customers"
    assert mirrored == 2


def test_fold_matches_dev_schema_case_insensitively():
    """A lower-case configured dev_schema must match an upper-cased warehouse
    schema (Snowflake/Redshift casing), keeping the source edge as canonical."""

    datasets = _mirror_world(dev_schema="DBT_DEV")
    rels = infer_relationships(datasets)
    kept, folded, mirrored = fold_replica_relationships(
        datasets, rels, frozenset({"dbt_dev"})
    )
    assert folded == 3
    assert len(kept) == 1
    assert kept[0].from_dataset == "db.main.orders"
    assert kept[0].to_dataset == "db.main.customers"
    assert mirrored == 2


def test_map_folds_mirrored_lineage_and_notes_it(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "mirror.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("INSERT INTO customers VALUES (1), (2)")
    conn.execute("CREATE SCHEMA dbt_dev")
    conn.execute("CREATE TABLE dbt_dev.stg_orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO dbt_dev.stg_orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE dbt_dev.dim_customers (id INTEGER)")
    conn.execute("INSERT INTO dbt_dev.dim_customers VALUES (1), (2)")
    conn.close()

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        ["explore", "map", "--path", str(db), "--repo-root", str(repo)], capsys
    )
    assert any("mirror source lineage" in n for n in payload["data"]["notes"])
    # One real foreign key survives instead of the inflated cross-schema fan-out.
    assert payload["data"]["relationship_count"] <= 2


def test_relationships_folds_mirrored_lineage_and_persists_folded_set(
    tmp_path: Path, capsys
):
    """`explore relationships` must fold replica duplicates exactly as `map` does,
    in both the envelope and the persisted cache — the coverage gap that let the
    two commands' caches disagree."""

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "mirror.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("INSERT INTO customers VALUES (1), (2)")
    conn.execute("CREATE SCHEMA dbt_dev")
    conn.execute("CREATE TABLE dbt_dev.stg_orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO dbt_dev.stg_orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE dbt_dev.dim_customers (id INTEGER)")
    conn.execute("INSERT INTO dbt_dev.dim_customers VALUES (1), (2)")
    conn.close()

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        ["explore", "relationships", "--path", str(db), "--repo-root", str(repo)],
        capsys,
    )
    data = payload["data"]
    assert any("mirror source lineage" in n for n in data["notes"])
    # One real foreign key survives instead of the inflated cross-schema fan-out.
    assert len(data["relationships"]) == 1

    cache = FilesystemStore(repo).load_cache()
    assert len(cache.relationships) == 1
    envelope_edge = data["relationships"][0]
    assert (cache.relationships[0].from_dataset, cache.relationships[0].to_dataset) == (
        envelope_edge["from_dataset"],
        envelope_edge["to_dataset"],
    )


# --- grain and data-quality notes ----------------------------------------------


def test_own_key_duplicates_produce_fan_out_warning():
    hosts = _ds(
        "db.main.RAW_HOSTS",
        [_col("ID", distinct=9590), _col("NAME", "VARCHAR")],
        rows=14111,
    )
    notes = data_quality_notes(hosts)
    warning = next(n for n in notes if "not unique" in n)
    assert "ID is not unique: ~9590 distinct over 14111 rows" in warning
    assert "4521 duplicate rows" in warning
    assert "fan out" in warning
    assert any("grain unknown" in n for n in notes)


def test_fan_out_note_gated_on_exactness():
    """Within the approximation noise band, only an exact count may call a
    column non-unique; a shortfall too large for noise still warns unescalated."""

    in_band_approx = _ds("db.main.things", [_col("id", distinct=1100)], rows=1125)
    assert not any("not unique" in n for n in data_quality_notes(in_band_approx))

    in_band_exact = _ds(
        "db.main.things",
        [
            ColumnProfile(
                name="id",
                data_type="INTEGER",
                null_fraction=0.0,
                distinct_count=1100,
                distinct_count_exact=True,
                is_unique=False,
            )
        ],
        rows=1125,
    )
    assert any("not unique" in n for n in data_quality_notes(in_band_exact))

    far_below_band = _ds("db.main.things", [_col("id", distinct=500)], rows=1125)
    assert any("not unique" in n for n in data_quality_notes(far_below_band))


def test_repeated_foreign_key_is_not_a_grain_defect():
    results = _ds(
        "db.main.results",
        [_col("resultId", distinct=100, unique=True), _col("raceId", distinct=20)],
        rows=100,
    )
    assert data_quality_notes(results) == []
    assert detect_grain(results) == ["resultId"]


def test_empty_table_produces_no_grain_notes():
    empty = _ds("db.main.empty_t", [_col("id")], rows=0)
    assert data_quality_notes(empty) == []


# --- composite keys --------------------------------------------------------------


def test_candidate_keys_list_singles_before_composites():
    ds = _ds(
        "db.main.line_items",
        [
            _col("id", distinct=2000, unique=True),
            _col("order_key", distinct=500),
            _col("line_number", distinct=4),
        ],
        rows=2000,
    )
    ds.composite_keys = [["order_key", "line_number"]]
    assert candidate_keys(ds) == [["id"], ["order_key", "line_number"]]


def test_grain_prefers_a_single_key_over_a_composite():
    ds = _ds(
        "db.main.line_items",
        [
            _col("id", distinct=2000, unique=True),
            _col("order_key", distinct=500),
            _col("line_number", distinct=4),
        ],
        rows=2000,
    )
    ds.composite_keys = [["order_key", "line_number"]]
    assert detect_grain(ds) == ["id"]


def test_grain_falls_back_to_the_best_ranked_composite():
    ds = _ds(
        "db.main.line_items",
        [
            _col("order_key", distinct=500),
            _col("line_number", distinct=4),
            _col("quantity", distinct=30),
        ],
        rows=2000,
    )
    ds.composite_keys = [["order_key", "line_number"], ["order_key", "quantity"]]
    assert detect_grain(ds) == ["order_key", "line_number"]
    assert not any("grain unknown" in n for n in data_quality_notes(ds))


def test_composite_member_is_not_a_unique_parent_key():
    """A same-named foreign key must not join to a composite member as if it
    were the parent's unique key: order_key alone repeats in line_items, so an
    edge onto it would fan out."""

    line_items = _ds(
        "db.main.line_items",
        [_col("order_key", distinct=500), _col("line_number", distinct=4)],
        rows=2000,
    )
    line_items.composite_keys = [["order_key", "line_number"]]
    shipments = _ds("db.main.shipments", [_col("order_key", distinct=400)], rows=450)
    assert infer_relationships([line_items, shipments]) == []


# --- envelope: the two field sessions ------------------------------------------


def test_f1_star_schema_join_graph_is_inferred(f1_duckdb: Path, capsys):
    payload = _run(["explore", "relationships", "--path", str(f1_duckdb)], capsys)
    data = payload["data"]
    assert data["inferred_count"] == 2
    by_fk = {tuple(r["from_columns"]): r for r in data["relationships"]}
    race = by_fk[("raceId",)]
    assert race["from_dataset"].endswith(".results")
    assert race["to_dataset"].endswith(".races")
    assert race["to_columns"] == ["raceId"]
    assert race["confidence"] >= 0.85
    driver = by_fk[("driverId",)]
    assert driver["to_dataset"].endswith(".drivers")
    assert driver["confidence"] >= 0.85
    assert all(r["kind"] == "inferred" for r in data["relationships"])


def test_airbnb_joins_inferred_despite_raw_prefix_and_broken_grain(
    airbnb_duckdb: Path, capsys
):
    payload = _run(["explore", "relationships", "--path", str(airbnb_duckdb)], capsys)
    data = payload["data"]
    by_fk = {tuple(r["from_columns"]): r for r in data["relationships"]}

    host = by_fk[("HOST_ID",)]
    assert host["to_dataset"].endswith(".RAW_HOSTS")
    assert host["to_columns"] == ["ID"]
    # The parent key is not unique, so the join is real but demoted.
    assert host["confidence"] < 0.85

    listing = by_fk[("LISTING_ID",)]
    assert listing["to_dataset"].endswith(".RAW_LISTINGS")
    assert listing["confidence"] >= 0.85


def test_relationships_envelope_explains_itself(airbnb_duckdb: Path, capsys):
    payload = _run(["explore", "relationships", "--path", str(airbnb_duckdb)], capsys)
    notes = payload["data"]["notes"]
    assert any("id-shaped column" in n for n in notes)
    assert any("no declared relationships" in n for n in notes)


def test_verify_measures_overlap_and_lifts_clean_joins(airbnb_duckdb: Path, capsys):
    """Every airbnb FK value has a parent, so verification confirms both joins:
    zero orphans, confidence up, and the broken-grain parent still capped below
    the trusted tier."""

    baseline = _run(["explore", "relationships", "--path", str(airbnb_duckdb)], capsys)[
        "data"
    ]["relationships"]
    verified = _run(
        ["explore", "relationships", "--verify", "--path", str(airbnb_duckdb)], capsys
    )["data"]
    assert all(not r["verified"] for r in baseline)

    by_fk = {tuple(r["from_columns"]): r for r in verified["relationships"]}
    base_by_fk = {tuple(r["from_columns"]): r for r in baseline}
    for fk in (("HOST_ID",), ("LISTING_ID",)):
        assert by_fk[fk]["verified"] is True
        assert by_fk[fk]["orphan_fraction"] == 0.0
        assert by_fk[fk]["confidence"] >= base_by_fk[fk]["confidence"]
    assert by_fk[("HOST_ID",)]["confidence"] < 0.85  # parent key still not unique
    assert any("overlap probes" in n for n in verified["notes"])


def test_verify_fast_run_emits_no_stderr_and_one_envelope(airbnb_duckdb: Path, capfd):
    """Contract guard on the verify path: a fast run stays silent on stderr and
    still emits exactly one envelope, so neither the profile nor the verify
    reporter contaminates stdout."""

    rc = main(["explore", "relationships", "--verify", "--path", str(airbnb_duckdb)])
    captured = capfd.readouterr()
    assert rc == 0, captured.out
    assert captured.err == ""  # fast run → no progress lines
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["status"] == "ok"


def test_verify_reporter_emits_one_line_per_probed_join(airbnb_duckdb: Path):
    """Drive the verify emission path directly: a reporter forced past first_after
    advances once per inferred join actually probed, and never on declared joins
    (which the loop skips before probing)."""

    import io

    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    adapter = DuckDBAdapter(airbnb_duckdb)
    try:
        datasets = profile(adapter, [m.identifier for m in adapter.list_objects()])
        inferred = infer_relationships(datasets)
        assert len(inferred) >= 2  # HOST_ID and LISTING_ID joins

        now = [0.0]
        stream = io.StringIO()
        reporter = ProgressReporter(
            len(inferred),
            "verified",
            "joins",
            stream=stream,
            clock=lambda: now[0],
            interval=0.0,
        )
        now[0] = PROGRESS_FIRST_AFTER + 0.1  # every advance is past the threshold
        verify_relationships(adapter, inferred, progress=reporter)
    finally:
        adapter.close()

    lines = stream.getvalue().splitlines()
    # advance() fires for all but the final probed join; done() closes it out.
    assert lines == [
        f"dex: verified {i}/{len(inferred)} joins" for i in range(1, len(inferred))
    ]
    reporter.done()
    assert stream.getvalue().endswith(
        f"dex: verified {len(inferred)}/{len(inferred)} joins\n"
    )


def test_verify_demotes_a_join_with_heavy_orphans(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "orphans.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, plan VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    # 2 of 5 orders point at real customers; 3 are orphans (fraction 0.6).
    conn.execute(
        "INSERT INTO orders VALUES (10, 1), (11, 2), (12, 7), (13, 8), (14, 9)"
    )
    conn.close()

    data = _run(["explore", "relationships", "--verify", "--path", str(path)], capsys)[
        "data"
    ]
    rel = next(r for r in data["relationships"] if r["from_columns"] == ["customer_id"])
    assert rel["verified"] is True
    assert rel["orphan_fraction"] == 0.6
    assert rel["confidence"] < 0.5, "measured non-containment demotes the guess"


# --- issue #163: a declared join is measurable too --------------------------


def test_probe_statements_and_verify_cover_the_same_set():
    """The estimate and the run must select identically.

    `probe_statements` prices what `verify_relationships` will spend, so a
    filter that admits a join to one and not the other under-reports cost
    *before* it is incurred, which no later reconciliation can catch. Both go
    through `probe_candidates`; this asserts the three stay one decision rather
    than three that currently agree.
    """

    from exmergo_dex_core.explore.relationships import (
        probe_batches,
        probe_candidates,
        probe_statements,
    )

    mixed = [
        _rel(kind=RelationshipKind.INFERRED, verified=False),
        _rel(kind=RelationshipKind.DECLARED, verified=False),
        _rel(
            kind=RelationshipKind.DECLARED,
            from_columns=["order_id", "line_no"],
            to_columns=["order_id", "line_no"],
            verified=False,
        ),
    ]

    candidates = probe_candidates(mixed)
    # A statement now answers several joins at once, so "the same set" is about
    # the joins the statements cover, not how many statements there are. Both
    # sides flatten to `candidates`, in order.
    batched = [rel for batch in probe_batches(candidates) for rel in batch]
    assert batched == candidates
    assert len(probe_statements(mixed, "duckdb")) == len(probe_batches(candidates))

    # A composite is probed as the full ordered tuple, never as its first pair.
    assert [len(r.from_columns) for r in candidates] == [1, 1, 2]
    composite_sql = probe_statements(mixed, "duckdb")[-1]
    assert 'd2.pk0 = c."order_id"' in composite_sql
    assert 'd2.pk1 = c."line_no"' in composite_sql
    assert {r.kind for r in candidates} == {
        RelationshipKind.INFERRED,
        RelationshipKind.DECLARED,
    }


def test_verify_measures_a_declared_join_without_touching_its_confidence(
    tmp_path: Path,
):
    """The design split at the spine of #163.

    Same warehouse and same 0.6 orphan rate as the inferred demotion test
    above, but with the join declared by the project. The measurement lands;
    the confidence does not move. A declared join is not a name-based guess
    whose credibility is up for revision -- the project asserted it, and a
    disagreement is a fact about the data, reported as a finding rather than
    smuggled into the number that means "how sure is dex".
    """

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "declared_orphans.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, plan VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute(
        "INSERT INTO orders VALUES (10, 1), (11, 2), (12, 7), (13, 8), (14, 9)"
    )
    conn.close()

    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.explore.relationships import verify_relationships

    declared = _rel(
        from_dataset="declared_orphans.main.orders",
        to_dataset="declared_orphans.main.customers",
        kind=RelationshipKind.DECLARED,
        verified=False,
    )
    declared.confidence = 1.0

    adapter = DuckDBAdapter(str(path))
    try:
        verify_relationships(adapter, [declared])
    finally:
        adapter.close()

    assert declared.verified is True
    assert declared.orphan_fraction == 0.6
    assert declared.confidence == 1.0, "a declaration is not demoted by measurement"


# --- orphan findings (#207): a complete orphan rate is a finding, not just a
# demoted confidence -------------------------------------------------------


def _rel(
    *,
    from_dataset: str = "db.s.orders",
    from_columns: list[str] | None = None,
    to_dataset: str = "db.s.customers",
    to_columns: list[str] | None = None,
    kind: RelationshipKind = RelationshipKind.INFERRED,
    verified: bool = True,
    orphan_fraction: float | None = None,
) -> Relationship:
    return Relationship(
        from_dataset=from_dataset,
        from_columns=from_columns or ["customer_id"],
        to_dataset=to_dataset,
        to_columns=to_columns or ["id"],
        kind=kind,
        verified=verified,
        orphan_fraction=orphan_fraction,
    )


@pytest.mark.parametrize(
    ("rel", "expect_finding"),
    [
        (_rel(orphan_fraction=1.0), True),
        (_rel(orphan_fraction=0.9), True),  # exactly at the threshold
        # The existing heavy-orphan demotion case: a real signal, but not
        # catastrophic enough for this finding -- a different tier.
        (_rel(orphan_fraction=0.6), False),
        (_rel(orphan_fraction=0.0), False),
        (_rel(orphan_fraction=None), False),  # verified but nothing measured
        (_rel(verified=False, orphan_fraction=1.0), False),  # nothing measured
        # Issue #163: a declared join is probed now, so it reaches this
        # decision at all. It qualifies on the same measured threshold as an
        # inferred one -- see the wording split below for why it is not the
        # same finding.
        (_rel(kind=RelationshipKind.DECLARED, orphan_fraction=1.0), True),
        (_rel(kind=RelationshipKind.DECLARED, orphan_fraction=0.6), False),
        (
            _rel(kind=RelationshipKind.DECLARED, verified=False, orphan_fraction=1.0),
            False,
        ),
    ],
)
def test_orphan_findings_decisions(rel: Relationship, expect_finding: bool):
    from exmergo_dex_core.explore.relationships import orphan_findings

    findings = orphan_findings([rel])
    if expect_finding:
        assert len(findings) == 1
        found_rel, text = findings[0]
        assert found_rel is rel
        assert "db.s.orders.customer_id" in text
        assert "db.s.customers.id" in text
    else:
        assert findings == []


def test_orphan_finding_text_differs_by_kind():
    """The two kinds are different findings, not one finding on two inputs.

    An inferred edge's suspect claim is dex's own name match, so the text
    disclaims it. A declared edge has no name coincidence to disclaim: the
    project asserted the key and the data contradicts it. Emitting the
    inferred wording for a declared join would tell a reader their dbt
    relationship test was a dex guess.
    """

    from exmergo_dex_core.explore.relationships import orphan_findings

    ((_, inferred_text),) = orphan_findings([_rel(orphan_fraction=1.0)])
    ((_, declared_text),) = orphan_findings(
        [_rel(kind=RelationshipKind.DECLARED, orphan_fraction=1.0)]
    )

    assert "shares a column name" in inferred_text
    assert "not evidence of a shared key" in inferred_text

    assert "declared as a foreign key" in declared_text
    assert "the project and the warehouse disagree" in declared_text
    assert "shares a column name" not in declared_text


def test_verify_reports_a_complete_orphan_edge_as_a_finding(tmp_path: Path, capsys):
    """The issue's own scenario: two columns share a name and share no
    values at all. The finding names both sides, and the same text survives
    into the child dataset's persisted data_quality."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "no_overlap.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, plan VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    # Every customer_id points at a customer that does not exist: orphan
    # fraction 1.0, a shared name with zero shared data.
    conn.execute("INSERT INTO orders VALUES (10, 7), (11, 8), (12, 9)")
    conn.close()

    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        [
            "explore",
            "relationships",
            "--verify",
            "--path",
            str(path),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )["data"]
    rel = next(r for r in data["relationships"] if r["from_columns"] == ["customer_id"])
    assert rel["verified"] is True
    assert rel["orphan_fraction"] == 1.0

    notes = " ".join(data["notes"])
    assert "orders.customer_id -> " in notes and "customers.id" in notes
    assert "100%" in notes
    assert "not evidence of a shared key" in notes

    cache = FilesystemStore(repo).load_cache()
    orders = next(d for d in cache.datasets if d.identifier.endswith(".orders"))
    assert any("not evidence of a shared key" in n for n in orders.data_quality)


def test_verify_at_zero_orphans_produces_no_finding(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "clean.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, plan VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (10, 1), (11, 2)")
    conn.close()

    data = _run(["explore", "relationships", "--verify", "--path", str(path)], capsys)[
        "data"
    ]
    rel = next(r for r in data["relationships"] if r["from_columns"] == ["customer_id"])
    assert rel["orphan_fraction"] == 0.0
    assert not any("not evidence of a shared key" in n for n in data["notes"])


def test_unverified_edge_produces_no_finding(tmp_path: Path, capsys):
    """Nothing was measured without --verify, so nothing is reported, even
    though the columns share a name."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "unverified.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("INSERT INTO customers VALUES (1), (2)")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (10, 7), (11, 8)")
    conn.close()

    data = _run(["explore", "relationships", "--path", str(path)], capsys)["data"]
    rel = next(r for r in data["relationships"] if r["from_columns"] == ["customer_id"])
    assert rel["verified"] is False
    assert not any("not evidence of a shared key" in n for n in data["notes"])


def test_map_verify_also_reports_orphan_findings(tmp_path: Path, capsys):
    """The second call site (`map --verify`) emits the identical finding
    shape, since both commands share `orphan_findings`."""

    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "map_no_overlap.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER, plan VARCHAR)")
    conn.execute("INSERT INTO customers VALUES (1, 'a'), (2, 'b')")
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (10, 7), (11, 8), (12, 9)")
    conn.close()

    repo = tmp_path / "repo"
    repo.mkdir()
    data = _run(
        ["explore", "map", "--verify", "--path", str(path), "--repo-root", str(repo)],
        capsys,
    )["data"]
    notes = " ".join(data["notes"])
    assert "not evidence of a shared key" in notes

    cache = FilesystemStore(repo).load_cache()
    orders = next(d for d in cache.datasets if d.identifier.endswith(".orders"))
    assert any("not evidence of a shared key" in n for n in orders.data_quality)


def test_relationships_persists_datasets_and_relationships(
    airbnb_duckdb: Path, tmp_path: Path, capsys
):
    """The profiles and inference this run already paid for land in the cache,
    in the same annotated shape a `map`-written cache has."""

    repo = tmp_path / "repo"
    repo.mkdir()
    payload = _run(
        [
            "explore",
            "relationships",
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert payload["data"]["cache_path"].endswith("cache.json")
    assert payload["data"]["updated_at"]

    cache = FilesystemStore(repo).load_cache()
    names = {d.identifier.split(".")[-1] for d in cache.datasets}
    assert names == {"RAW_HOSTS", "RAW_LISTINGS", "RAW_REVIEWS"}
    assert all(d.columns for d in cache.datasets)
    listings = next(d for d in cache.datasets if d.identifier.endswith(".RAW_LISTINGS"))
    assert listings.grain == ["ID"], "grain-annotated before persisting, like map"
    assert len(cache.relationships) == len(payload["data"]["relationships"]) == 2


def test_relationships_then_query_succeeds(airbnb_duckdb: Path, tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(
        [
            "explore",
            "relationships",
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    payload = _run(
        [
            "explore",
            "query",
            "SELECT COUNT(*) AS n FROM RAW_REVIEWS",
            "--path",
            str(airbnb_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert payload["data"]["cells"] == [[2]]


def test_empty_result_is_explained_not_silent(tmp_path: Path, capsys):
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "flat.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE people (id INTEGER, age INTEGER)")
    conn.execute("INSERT INTO people VALUES (1, 30)")
    conn.close()

    payload = _run(["explore", "relationships", "--path", str(path)], capsys)
    data = payload["data"]
    assert data["relationships"] == []
    assert any("nothing to infer" in n for n in data["notes"])


def test_overlap_probe_transpiles_to_postgres_and_stays_select_only():
    """The probe is authored once in DuckDB SQL; on Postgres it must transpile
    to a statement that re-parses in the postgres dialect and passes the
    SELECT-only guard (the dialect risk a new connector carries)."""

    import sqlglot

    from exmergo_dex_core.cache import Relationship
    from exmergo_dex_core.explore.relationships import probe_statements
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    rel = Relationship(
        from_dataset="dexdb.app.order_items",
        from_columns=["product_id"],
        to_dataset="dexdb.app.products",
        to_columns=["id"],
    )
    statements = probe_statements([rel], "postgres")
    assert len(statements) == 1
    sql = statements[0]
    assert_select_only(sql, dialect="postgres")
    parsed = sqlglot.parse_one(sql, read="postgres")
    assert parsed is not None
    # Portable shapes survive the rewrite; DuckDB-only FILTER syntax does not
    # appear (BigQuery lacks it and Postgres parses it differently).
    assert "order_items" in sql and "products" in sql


# --- issue #398: overlap probes share their table references ----------------


def _probe_rel(child: str, fk: str, parent: str, key: str) -> Relationship:
    return Relationship(
        from_dataset=f"wh.main.{child}",
        from_columns=[fk],
        to_dataset=f"wh.main.{parent}",
        to_columns=[key],
        kind=RelationshipKind.INFERRED,
        confidence=0.7,
    )


def test_a_batch_costs_the_graph_s_tables_not_twice_its_edges():
    """The invariant that produces the saving, counted the way the estimator
    counts it.

    A per-table minimum is charged on the distinct tables a statement reads, so
    what matters is that one statement's table set is the graph's table set. Five
    edges unbatched are five statements reading two tables each, ten table
    references to bill; batched they are one statement reading five.
    """

    import sqlglot

    from exmergo_dex_core.explore.relationships import probe_statements

    edges = [
        _probe_rel("order_items", "order_id", "orders", "id"),
        _probe_rel("order_items", "user_id", "users", "id"),
        _probe_rel("order_items", "product_id", "products", "id"),
        _probe_rel("orders", "user_id", "users", "id"),
        _probe_rel("events", "user_id", "users", "id"),
    ]

    [sql] = probe_statements(edges, "bigquery")  # five edges, inside the cap

    parsed = sqlglot.parse_one(sql, read="bigquery")
    referenced = {t.name for t in parsed.find_all(sqlglot.exp.Table)}
    assert referenced == {"order_items", "orders", "users", "products", "events"}
    # And the child side is read once per child, not once per edge, which is
    # what the connectors billing scan time rather than bytes are paying for.
    assert sql.count("`order_items` AS c") == 1


def test_a_child_with_more_edges_than_the_cap_stays_one_statement(monkeypatch):
    """Splitting a child across statements would read that child twice, which
    is the thing batching exists to stop, so the cap yields to it."""

    from exmergo_dex_core.explore import relationships as rel_mod

    monkeypatch.setattr(rel_mod, "_PROBE_BATCH", 2)
    edges = [_probe_rel("wide_fact", f"fk_{i}", f"dim_{i}", "id") for i in range(5)] + [
        _probe_rel("other_fact", "fk_0", "dim_0", "id")
    ]

    batches = rel_mod.probe_batches(edges)

    assert [len(b) for b in batches] == [5, 1]
    assert {r.from_dataset for r in batches[0]} == {"wh.main.wide_fact"}


def test_batching_never_reorders_or_drops_an_edge(monkeypatch):
    """`probe_batches` is what keeps the priced statements and the run
    statements the same statements, so it must be a regrouping of its input and
    nothing else."""

    from exmergo_dex_core.explore import relationships as rel_mod

    monkeypatch.setattr(rel_mod, "_PROBE_BATCH", 3)
    edges = [
        _probe_rel("a", "x", "p", "id"),
        _probe_rel("b", "x", "p", "id"),
        _probe_rel("a", "y", "q", "id"),
        _probe_rel("c", "x", "p", "id"),
        _probe_rel("b", "y", "q", "id"),
    ]

    flattened = [rel for batch in rel_mod.probe_batches(edges) for rel in batch]

    assert sorted(id(r) for r in flattened) == sorted(id(r) for r in edges)
    # Edges sharing a child are adjacent, which is what lets one read answer
    # all of them.
    children = [r.from_dataset for r in flattened]
    assert children == sorted(children, key=children.index)


@pytest.mark.parametrize(
    "dialect",
    ["bigquery", "snowflake", "postgres", "redshift", "databricks", "clickhouse"],
)
def test_a_cross_child_batch_transpiles_and_stays_select_only(dialect: str):
    """The batched statement carries two shapes a single-edge probe never did
    (several LEFT JOINs over one child, and a CROSS JOIN between children), so
    every connector's dialect has to survive both."""

    import sqlglot

    from exmergo_dex_core.explore.relationships import probe_statements
    from exmergo_dex_core.guards.sql_guard import assert_select_only

    [sql] = probe_statements(
        [
            _probe_rel("order_items", "order_id", "orders", "id"),
            _probe_rel("order_items", "user_id", "users", "id"),
            _probe_rel("orders", "user_id", "users", "id"),
        ],
        dialect,
    )

    assert_select_only(sql, dialect=dialect)
    assert sqlglot.parse_one(sql, read=dialect) is not None
    assert "FILTER" not in sql.upper()


def test_batched_verification_matches_a_probe_per_join(tmp_path: Path):
    """The acceptance criterion for #398: batching may change how many
    statements run and nothing else.

    The oracle is the pre-batching probe, written out here rather than reached
    through a knob, so the comparison is against an independent statement per
    join and not against the batcher configured small.

    The warehouse carries every case that could plausibly diverge under a
    batched read: several foreign keys on one child, a dimension shared by two
    children, a non-unique parent key (which a bare join would fan out on), NULL
    foreign keys, an entirely orphaned key, and a key with no non-null values at
    all.
    """

    duckdb = pytest.importorskip("duckdb")

    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter
    from exmergo_dex_core.explore.relationships import verify_relationships

    path = tmp_path / "batched.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("INSERT INTO customers VALUES (1), (2), (2)")  # not unique
    conn.execute("CREATE TABLE products (id INTEGER)")
    conn.execute("INSERT INTO products VALUES (100), (101)")
    conn.execute(
        "CREATE TABLE orders (customer_id INTEGER, product_id INTEGER, "
        "promo_id INTEGER, void_id INTEGER)"
    )
    conn.execute(
        "INSERT INTO orders VALUES (1, 100, 900, NULL), (2, 101, 901, NULL), "
        "(7, 100, 902, NULL), (NULL, 999, 903, NULL)"
    )
    conn.execute("CREATE TABLE returns (customer_id INTEGER)")
    conn.execute("INSERT INTO returns VALUES (1), (55)")
    conn.close()

    def edges() -> list[Relationship]:
        return [
            _probe_rel("orders", "customer_id", "customers", "id"),
            _probe_rel("orders", "product_id", "products", "id"),
            _probe_rel("orders", "promo_id", "products", "id"),  # fully orphaned
            _probe_rel("orders", "void_id", "products", "id"),  # no non-null values
            _probe_rel("returns", "customer_id", "customers", "id"),
        ]

    def qualify(rels: list[Relationship]) -> list[Relationship]:
        for rel in rels:
            rel.from_dataset = rel.from_dataset.replace("wh.main.", "batched.main.")
            rel.to_dataset = rel.to_dataset.replace("wh.main.", "batched.main.")
        return rels

    def one_probe_per_join(adapter, rels: list[Relationship]) -> list[tuple]:
        """The statement `--verify` issued before #398, one join at a time."""

        def quoted(identifier: str) -> str:
            return ".".join(f'"{part}"' for part in identifier.split("."))

        measured = []
        for rel in rels:
            child, parent = quoted(rel.from_dataset), quoted(rel.to_dataset)
            fk, key = rel.from_columns[0], rel.to_columns[0]
            result = adapter.run_query(
                f'SELECT COUNT(c."{fk}") AS nonnull_fk, '  # noqa: S608
                f'COUNT(CASE WHEN c."{fk}" IS NOT NULL AND d.pk IS NULL '
                f"THEN 1 END) AS orphans "
                f"FROM {child} c LEFT JOIN "
                f'(SELECT DISTINCT "{key}" AS pk FROM {parent}) d '
                f'ON d.pk = c."{fk}"',
                max_rows=1,
                timeout_seconds=30.0,
            )
            values = dict(zip(result.columns, result.cells[0], strict=True))
            nonnull = int(values["nonnull_fk"] or 0)
            orphans = int(values["orphans"] or 0)
            fraction = None if nonnull == 0 else round(orphans / nonnull, 4)
            measured.append((True, fraction))
        return measured

    adapter = DuckDBAdapter(str(path))
    try:
        expected = one_probe_per_join(adapter, qualify(edges()))
        batched = qualify(edges())
        verify_relationships(adapter, batched)
    finally:
        adapter.close()

    assert [(r.verified, r.orphan_fraction) for r in batched] == expected
    assert expected[2][1] == 1.0, "the fully orphaned edge is measured as such"
    assert expected[3][1] is None, "no non-null values stays unmeasurable"
    # Five joins, two children: one statement now, five before.
    assert len(probe_batches(batched)) == 1


# --- declared joins from the dbt project -----------------------------------------


def _defs(foreign_keys) -> ProjectDefinitions:
    return ProjectDefinitions(present=True, foreign_keys=foreign_keys)


def _fk(
    model, column, to_model, to_column, relation=None, to_relation=None, source="yaml"
):
    return DeclaredForeignKey(
        model=model,
        relation=relation,
        column=column,
        to_model=to_model,
        to_relation=to_relation,
        to_column=to_column,
        source=source,
    )


def test_declared_resolves_manifest_relation_across_database_alias():
    # The manifest says database "analytics"; the adapter normalized the same
    # objects under the DuckDB file stem "wh". The schema.table suffix pins them.
    defs = _defs(
        [
            _fk(
                "orders",
                "customer_id",
                "customers",
                "id",
                relation="analytics.main.orders",
                to_relation="analytics.main.customers",
                source="manifest",
            )
        ]
    )
    known = ["wh.main.orders", "wh.main.customers"]
    rels, notes = declared_relationships(defs, known)
    assert notes == []
    (rel,) = rels
    assert rel.from_dataset == "wh.main.orders"
    assert rel.to_dataset == "wh.main.customers"
    assert rel.from_columns == ["customer_id"] and rel.to_columns == ["id"]
    assert rel.kind.value == "declared"
    assert rel.confidence == 1.0


def test_declared_yaml_fallback_resolves_by_model_name():
    defs = _defs([_fk("orders", "customer_id", "customers", "id")])
    rels, notes = declared_relationships(defs, ["wh.main.orders", "wh.main.customers"])
    assert notes == []
    (rel,) = rels
    assert rel.from_dataset == "wh.main.orders"


def test_declared_ambiguous_match_is_skipped_with_a_note():
    defs = _defs([_fk("orders", "customer_id", "customers", "id")])
    known = ["wh.a.orders", "wh.b.orders", "wh.main.customers"]
    rels, notes = declared_relationships(defs, known)
    assert rels == []
    assert any("more than one object" in n for n in notes)


def test_declared_missing_relation_is_a_note_not_an_edge():
    defs = _defs([_fk("orders", "payment_id", "payments", "id")])
    rels, notes = declared_relationships(defs, ["wh.main.orders"])
    assert rels == []
    (note,) = notes
    assert "payments.id" in note and "not in this connection's inventory" in note


def test_declared_duplicate_edges_are_deduped():
    fk = _fk("orders", "customer_id", "customers", "id")
    defs = _defs([fk, fk.model_copy()])
    rels, _ = declared_relationships(defs, ["wh.main.orders", "wh.main.customers"])
    assert len(rels) == 1


def test_merge_keeps_declared_over_matching_inferred():
    declared = Relationship(
        from_dataset="wh.main.orders",
        from_columns=["customer_id"],
        to_dataset="wh.main.customers",
        to_columns=["id"],
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
    )
    same_inferred = Relationship(
        from_dataset="WH.MAIN.ORDERS",
        from_columns=["CUSTOMER_ID"],
        to_dataset="wh.main.customers",
        to_columns=["ID"],
        confidence=0.85,
    )
    other = Relationship(
        from_dataset="wh.main.orders",
        from_columns=["host_id"],
        to_dataset="wh.main.hosts",
        to_columns=["id"],
        confidence=0.6,
    )
    merged, confirmed = _merge_relationships([declared], [same_inferred, other])
    assert confirmed == 1
    assert merged == [declared, other]


def test_carry_forward_relationships_keeps_an_edge_this_run_never_examined():
    """A prior relationship with an endpoint outside this run's --scope/
    --dataset (never profiled or reused fresh) could not have been
    regenerated or superseded by this run's inference, so it must not be
    silently dropped from the cache (issue #111)."""

    out_of_scope_edge = Relationship(
        from_dataset="wh.raw.orders",
        from_columns=["customer_id"],
        to_dataset="wh.raw.customers",
        to_columns=["id"],
        confidence=0.85,
    )
    prior = DexCache(relationships=[out_of_scope_edge])
    # This run only examined the marts dataset; raw.orders/raw.customers were
    # never in scope at all.
    examined = {"wh.marts.mart_orders"}
    merged, carried = _carry_forward_relationships(prior, examined, [])
    assert carried == 1
    assert merged == [out_of_scope_edge]


def test_carry_forward_relationships_does_not_duplicate_a_regenerated_edge():
    """When this run's own inference already reproduced the same edge (both
    endpoints examined), the prior copy is not also carried forward."""

    edge = Relationship(
        from_dataset="wh.raw.orders",
        from_columns=["customer_id"],
        to_dataset="wh.raw.customers",
        to_columns=["id"],
        confidence=0.6,
    )
    prior = DexCache(relationships=[edge])
    examined = {"wh.raw.orders", "wh.raw.customers"}
    fresh_edge = edge.model_copy(update={"confidence": 0.9})
    merged, carried = _carry_forward_relationships(prior, examined, [fresh_edge])
    assert carried == 0
    assert merged == [fresh_edge]


def test_carry_forward_relationships_without_a_prior_cache_is_a_noop():
    merged, carried = _carry_forward_relationships(None, {"wh.raw.orders"}, [])
    assert merged == []
    assert carried == 0


def _declared_join_repo(tmp_path: Path, *, with_manifest: bool) -> tuple[Path, Path]:
    """A DuckDB warehouse plus a dbt project declaring orders -> customers."""

    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "wh.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 1), (2, 2), (3, 1)")
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.execute("INSERT INTO customers VALUES (1), (2)")
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
        "  - name: orders\n"
        "    columns:\n"
        "      - name: customer_id\n"
        "        tests:\n"
        "          - relationships:\n"
        "              to: ref('customers')\n"
        "              field: id\n",
        encoding="utf-8",
    )
    if with_manifest:
        # The manifest's database component ("analytics") deliberately differs
        # from the adapter-normalized file stem ("wh"): resolution must absorb it.
        write_manifest(
            repo,
            models={
                "orders": '"analytics"."main"."orders"',
                "customers": '"analytics"."main"."customers"',
            },
            relationship_tests=[("orders", "customer_id", "ref('customers')", "id")],
        )
    return db, repo


def test_relationships_envelope_reports_declared_join(tmp_path: Path, capsys):
    db, repo = _declared_join_repo(tmp_path, with_manifest=True)
    payload = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    data = payload["data"]
    assert data["declared_count"] == 1
    declared = [r for r in data["relationships"] if r["kind"] == "declared"]
    (rel,) = declared
    assert rel["from_dataset"] == "wh.main.orders"
    assert rel["confidence"] == 1.0
    # Inference finds the same edge; the merge keeps only the declared one.
    assert not any(
        r["kind"] == "inferred"
        and r["from_columns"] == ["customer_id"]
        and r["to_dataset"] == "wh.main.customers"
        for r in data["relationships"]
    )
    assert any("match declared tests" in n for n in data["notes"])


def test_relationships_envelope_yaml_fallback_and_note(tmp_path: Path, capsys):
    db, repo = _declared_join_repo(tmp_path, with_manifest=False)
    payload = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    data = payload["data"]
    assert data["declared_count"] == 1
    assert any("name-based" in n for n in data["notes"])


def test_relationships_envelope_notes_stale_manifest(tmp_path: Path, capsys):
    db, repo = _declared_join_repo(tmp_path, with_manifest=False)
    write_manifest(
        repo,
        models={
            "orders": '"analytics"."main"."orders"',
            "customers": '"analytics"."main"."customers"',
        },
        relationship_tests=[("orders", "customer_id", "ref('customers')", "id")],
        generated_at="2020-01-01T00:00:00Z",
    )
    payload = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert any("older than the model sources" in n for n in payload["data"]["notes"])


def test_relationships_envelope_unresolved_declared_is_a_signal(tmp_path: Path, capsys):
    db, repo = _declared_join_repo(tmp_path, with_manifest=False)
    (repo / "models" / "schema.yml").write_text(
        "version: 2\n"
        "models:\n"
        "  - name: orders\n"
        "    columns:\n"
        "      - name: payment_id\n"
        "        tests:\n"
        "          - relationships:\n"
        "              to: ref('payments')\n"
        "              field: id\n",
        encoding="utf-8",
    )
    payload = _run(
        [
            "explore",
            "relationships",
            "--use-project",
            "--path",
            str(db),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    data = payload["data"]
    assert data["declared_count"] == 0
    notes = data["notes"]
    assert any("no declared relationships resolved" in n for n in notes)
    assert any("not in this connection's inventory" in n for n in notes)


# ---- the semantic layer's declared entity graph (#361) -----------------------
#
# A shared entity is a join the layer states, with the key named per model. These
# arrive at the declared tier beside the `relationships` tests, so the assertions
# below are the ones that keep that tier honest: the same never-guess endpoint
# resolution, one edge per join however many channels name it, and the entity
# carried as the thing a reader can look up.


def _join(
    entity: str = "customer",
    *,
    parent_relation: str = "wh.main.customers",
    parent_column: str = "id",
    child_relation: str = "wh.main.orders",
    child_column: str = "buyer_id",
) -> EntityJoin:
    return EntityJoin(
        entity=entity,
        parent_model="customers_sm",
        parent_relation=parent_relation,
        parent_column=parent_column,
        child_model="orders_sm",
        child_relation=child_relation,
        child_column=child_column,
    )


def test_a_declared_entity_becomes_an_edge_naming_the_entity():
    rels, notes = semantic_relationships(
        [_join()], ["wh.main.orders", "wh.main.customers"]
    )

    assert notes == []
    (rel,) = rels
    assert rel.from_dataset == "wh.main.orders" and rel.from_columns == ["buyer_id"]
    assert rel.to_dataset == "wh.main.customers" and rel.to_columns == ["id"]
    # Declared, not inferred: the layer states this join and names its key. A
    # name-based rule would never have found `buyer_id` against `id`.
    assert rel.kind is RelationshipKind.DECLARED
    assert rel.confidence == 1.0
    assert rel.declared_by == "semantic entity 'customer'"


def test_a_semantic_endpoint_resolves_across_a_database_alias():
    """Same rule the `relationships` tests go through, and the same reason: a
    compiled manifest spells the database the way dbt was configured while the
    adapter normalizes it per connector."""

    rels, notes = semantic_relationships(
        [
            _join(
                parent_relation="analytics.main.customers",
                child_relation="analytics.main.orders",
            )
        ],
        ["wh.main.orders", "wh.main.customers"],
    )

    assert notes == []
    assert rels[0].from_dataset == "wh.main.orders"


def test_a_semantic_endpoint_missing_here_is_a_note_not_an_edge():
    rels, notes = semantic_relationships([_join()], ["wh.main.orders"])

    assert rels == []
    (note,) = notes
    assert "semantic entity 'customer'" in note
    assert "not in this connection's inventory" in note


def test_an_ambiguous_semantic_endpoint_is_skipped_rather_than_guessed():
    rels, notes = semantic_relationships(
        [_join()], ["wh.a.orders", "wh.b.orders", "wh.main.customers"]
    )

    assert rels == []
    assert any("more than one object" in note for note in notes)


def test_duplicate_semantic_joins_are_deduped():
    rels, _ = semantic_relationships(
        [_join(), _join()], ["wh.main.orders", "wh.main.customers"]
    )

    assert len(rels) == 1


def test_a_semantic_edge_the_project_already_declares_is_counted_once():
    """Both channels are declarations of the same tier, so an edge in both is one
    edge. Which named it first is not a fact about the warehouse, and doubling it
    would inflate the connectivity ranking."""

    declared = Relationship(
        from_dataset="wh.main.orders",
        from_columns=["buyer_id"],
        to_dataset="wh.main.customers",
        to_columns=["id"],
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
    )
    semantic, _ = semantic_relationships(
        [_join()], ["wh.main.orders", "wh.main.customers"]
    )

    merged, already = _fold_semantic_edges([declared], semantic)

    assert len(merged) == 1 and already == 1
    # The relationships test's edge stands; the semantic channel adds nothing it
    # did not already say.
    assert merged[0].declared_by is None


# --- declared relationship conflicts (#408) -------------------------------------


def test_conflicting_declarations_over_the_same_endpoints_are_both_kept_and_flagged():
    """A relationships test and the semantic layer's shared entity name the same
    two datasets but disagree on which column joins them: dex never picks a
    winner, so both edges survive and the disagreement is named rather than
    one silently overwriting or merging with the other."""

    declared = Relationship(
        from_dataset="wh.main.orders",
        from_columns=["customer_id"],
        to_dataset="wh.main.customers",
        to_columns=["id"],
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
        declaration_sources=["relationships test orders.customer_id"],
    )
    semantic, _ = semantic_relationships(
        [_join(child_column="buyer_id")], ["wh.main.orders", "wh.main.customers"]
    )

    merged, already = _fold_semantic_edges([declared], semantic)
    conflicts = _declared_relationship_conflicts(merged)

    assert already == 0
    assert len(merged) == 2, "neither declaration wins; both edges survive"
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.from_dataset == "wh.main.orders"
    assert conflict.to_dataset == "wh.main.customers"
    pairs = {tuple(tuple(p) for p in d.column_pairs) for d in conflict.declarations}
    assert pairs == {(("customer_id", "id"),), (("buyer_id", "id"),)}
    sources = {d.source for d in conflict.declarations}
    assert "relationships test orders.customer_id" in sources
    assert any("customer" in s for s in sources)  # the semantic entity's name


def test_agreeing_declarations_over_the_same_endpoints_are_not_a_conflict():
    declared = Relationship(
        from_dataset="wh.main.orders",
        from_columns=["buyer_id"],
        to_dataset="wh.main.customers",
        to_columns=["id"],
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
    )
    semantic, _ = semantic_relationships(
        [_join()], ["wh.main.orders", "wh.main.customers"]
    )

    merged, _already = _fold_semantic_edges([declared], semantic)

    assert _declared_relationship_conflicts(merged) == []


def test_a_composite_conflict_carries_every_column_pair():
    declared = Relationship(
        from_dataset="wh.main.order_lines",
        from_columns=["product_id"],
        to_dataset="wh.main.products",
        to_columns=["id"],
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
        declaration_sources=["relationships test order_lines.product_id"],
    )
    composite = Relationship(
        from_dataset="wh.main.order_lines",
        from_columns=["product_id", "variant_id"],
        to_dataset="wh.main.products",
        to_columns=["id", "variant_id"],
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
        declaration_sources=["native relationship 'order_lines_to_products'"],
    )

    (conflict,) = _declared_relationship_conflicts([declared, composite])

    by_pairs = {tuple(tuple(p) for p in d.column_pairs): d for d in conflict.declarations}
    assert (("product_id", "id"),) in by_pairs
    assert (("product_id", "id"), ("variant_id", "variant_id")) in by_pairs


def test_conflict_notes_name_the_endpoints_and_point_at_the_structured_field():
    conflicts = _declared_relationship_conflicts(
        [
            Relationship(
                from_dataset="wh.main.orders",
                from_columns=["customer_id"],
                to_dataset="wh.main.customers",
                to_columns=["id"],
                kind=RelationshipKind.DECLARED,
                confidence=1.0,
            ),
            Relationship(
                from_dataset="wh.main.orders",
                from_columns=["buyer_id"],
                to_dataset="wh.main.customers",
                to_columns=["id"],
                kind=RelationshipKind.DECLARED,
                confidence=1.0,
            ),
        ]
    )

    (note,) = _relationship_conflict_notes(conflicts)
    assert "wh.main.orders -> wh.main.customers" in note
    assert "data.conflicts" in note


def test_no_conflict_notes_when_nothing_disagrees():
    assert _relationship_conflict_notes([]) == []


def test_the_notes_name_what_inference_would_have_missed():
    """The count alone is not the interesting number. An edge the layer declares
    and inference did not find is a join that would otherwise be absent from the
    map, with a key no naming rule could have matched."""

    semantic, _ = semantic_relationships(
        [_join()], ["wh.main.orders", "wh.main.customers"]
    )

    notes = _semantic_join_notes(semantic, 0, [])

    assert any("declared entity graph" in note for note in notes)
    missed = next(note for note in notes if "not found by name-based" in note)
    assert "semantic entity 'customer'" in missed


def test_an_edge_inference_also_found_is_not_reported_as_rescued():
    semantic, _ = semantic_relationships(
        [_join()], ["wh.main.orders", "wh.main.customers"]
    )
    inferred = [
        Relationship(
            from_dataset="wh.main.orders",
            from_columns=["buyer_id"],
            to_dataset="wh.main.customers",
            to_columns=["id"],
            kind=RelationshipKind.INFERRED,
            confidence=0.7,
        )
    ]

    notes = _semantic_join_notes(semantic, 0, inferred)

    assert not any("not found by name-based" in note for note in notes)
