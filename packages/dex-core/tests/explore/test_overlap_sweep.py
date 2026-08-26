"""explore relationships/map --infer-by-overlap: proposing joins from
measured value overlap when no column name matches (issue #220).

Name-based inference exhausts every naming convention before this ever
runs; what's left is exactly the case naming cannot help with (`acct_id_fk`
to `ws_id`, or a source that names every key `id`). These tests cover the
candidate pool (key-or-near-key restriction, type compatibility, direction,
cap/elision), the probe threshold that turns a candidate into a proposed
edge, the persistence fix that keeps a discovered edge from vanishing on a
plain re-run, and the end-to-end CLI wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    Relationship,
    RelationshipKind,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore.commands import _carry_forward_overlap_edges
from exmergo_dex_core.explore.relationships import (
    overlap_sweep_candidates,
    overlap_sweep_statements,
    probe_overlap_candidates,
)


def _run(argv: list[str], capsys) -> dict:
    rc = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["status"] == "ok", payload
    return payload


def _col(
    name: str,
    data_type: str = "INTEGER",
    *,
    unique: bool = False,
    distinct: int | None = None,
    pii=None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        data_type=data_type,
        null_fraction=0.0,
        is_unique=unique,
        distinct_count=distinct,
        pii=pii,
    )


def _ds(
    identifier: str, columns: list[ColumnProfile], rows: int | None = None
) -> Dataset:
    return Dataset(identifier=identifier, row_count=rows, columns=columns)


# --- candidate pool: key-or-near-key restriction, type filter, exclusion ----


def test_overlap_sweep_finds_a_pair_with_no_naming_signal_at_all():
    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    billing = _ds("db.s.billing_profiles", [_col("profile_ref", unique=True)], rows=100)
    candidates, elided, cap = overlap_sweep_candidates([accounts, billing], set())
    assert elided == 0
    assert len(candidates) == 1
    [candidate] = candidates
    assert candidate.kind is RelationshipKind.OVERLAP_INFERRED
    assert candidate.verified is False
    assert candidate.confidence is None
    assert cap > 0


def test_overlap_sweep_excludes_a_column_already_matched():
    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    billing = _ds("db.s.billing_profiles", [_col("profile_ref", unique=True)], rows=100)
    matched = {("db.s.accounts", "acct_num")}
    candidates, elided, _cap = overlap_sweep_candidates([accounts, billing], matched)
    assert candidates == []
    assert elided == 0


def test_overlap_sweep_excludes_a_column_that_is_not_key_or_near_key_shaped():
    # A many-valued, ordinary numeric column: not unique, and nowhere near
    # NEAR_UNIQUE_RATIO of the row count.
    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    orders = _ds("db.s.orders", [_col("customer_ref", distinct=5)], rows=100)
    candidates, _elided, _cap = overlap_sweep_candidates([accounts, orders], set())
    assert candidates == []


def test_overlap_sweep_includes_a_near_key_short_of_proof():
    # distinct_count clears NEAR_UNIQUE_RATIO (0.75) of row_count without
    # is_unique being set (the escalation-cap-missed case).
    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    near = _ds("db.s.near", [_col("almost_unique", distinct=80)], rows=100)
    candidates, _elided, _cap = overlap_sweep_candidates([accounts, near], set())
    assert len(candidates) == 1


def test_overlap_sweep_excludes_a_pii_column():
    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    people = _ds(
        "db.s.people",
        [
            _col(
                "email_id",
                "VARCHAR",
                unique=True,
                pii={"category": "email", "confidence": 0.9},
            )
        ],
        rows=100,
    )
    candidates, _elided, _cap = overlap_sweep_candidates([accounts, people], set())
    assert candidates == []


def test_overlap_sweep_excludes_type_incompatible_pairs():
    accounts = _ds(
        "db.s.accounts", [_col("acct_num", "INTEGER", unique=True)], rows=100
    )
    labels = _ds("db.s.labels", [_col("label_ref", "VARCHAR", unique=True)], rows=100)
    candidates, _elided, _cap = overlap_sweep_candidates([accounts, labels], set())
    assert candidates == []


def test_overlap_sweep_never_pairs_a_column_with_itself_within_one_dataset():
    same = _ds(
        "db.s.pairs",
        [_col("a", unique=True), _col("b", unique=True)],
        rows=100,
    )
    candidates, _elided, _cap = overlap_sweep_candidates([same], set())
    assert candidates == []


def test_overlap_sweep_direction_prefers_the_proven_key_as_parent():
    proven = _ds("db.s.proven", [_col("k", unique=True)], rows=100)
    near = _ds("db.s.near", [_col("k2", distinct=80)], rows=100)
    [candidate] = overlap_sweep_candidates([proven, near], set())[0]
    assert candidate.to_dataset == "db.s.proven"
    assert candidate.from_dataset == "db.s.near"


def test_overlap_sweep_direction_prefers_the_smaller_table_between_two_proven_keys():
    small = _ds("db.s.small", [_col("k", unique=True)], rows=10)
    large = _ds("db.s.large", [_col("k2", unique=True)], rows=1000)
    [candidate] = overlap_sweep_candidates([small, large], set())[0]
    assert candidate.to_dataset == "db.s.small"
    assert candidate.from_dataset == "db.s.large"


def test_overlap_sweep_caps_and_reports_elision_deterministically():
    datasets = [
        _ds(f"db.s.t{i}", [_col(f"k{i}", unique=True)], rows=10) for i in range(6)
    ]
    # 6 proven keys -> 15 unordered pairs, all type-compatible.
    candidates, elided, cap = overlap_sweep_candidates(datasets, set(), cap=3)
    assert cap == 3
    assert len(candidates) == 3
    assert elided == 15 - 3
    # Deterministic: re-running produces the identical capped slice.
    again, elided_again, _cap = overlap_sweep_candidates(datasets, set(), cap=3)
    assert candidates == again
    assert elided == elided_again


def test_overlap_sweep_excludes_a_composite_key_only_column():
    # `member` is part of a composite key but is not unique or near-unique on
    # its own, so it never enters the pool (same policy `probe_candidates`
    # already applies to `--verify`).
    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    member = _ds(
        "db.s.member_table",
        [_col("member", distinct=3)],
        rows=100,
    )
    member.composite_keys = [["member", "other"]]
    candidates, _elided, _cap = overlap_sweep_candidates([accounts, member], set())
    assert candidates == []


# --- pricing/probe SQL: authored once, transpiled per dialect ---------------


def test_overlap_sweep_statements_transpiles_to_postgres_and_stays_select_only():
    import sqlglot

    from exmergo_dex_core.guards.sql_guard import assert_select_only

    accounts = _ds("db.s.accounts", [_col("acct_num", unique=True)], rows=100)
    billing = _ds("db.s.billing_profiles", [_col("profile_ref", unique=True)], rows=100)
    [candidate] = overlap_sweep_candidates([accounts, billing], set())[0]
    [sql] = overlap_sweep_statements([candidate], "postgres")
    assert_select_only(sql, dialect="postgres")
    parsed = sqlglot.parse_one(sql, read="postgres")
    assert parsed is not None
    assert "accounts" in sql and "billing_profiles" in sql


# --- probe threshold: acceptance/rejection, in-place mutation --------------


class _StubAdapter:
    """Just the surface `probe_overlap_candidates` touches: canned aggregate
    rows keyed by which table the probe's FROM clause names, in call order."""

    dialect = "duckdb"

    def __init__(self, rows: list[tuple[int, int]]):
        self._rows = list(rows)

    def run_query(self, sql: str, *, max_rows: int, timeout_seconds: float):
        from exmergo_dex_core.adapters.base import QueryResult

        nonnull, orphans = self._rows.pop(0)
        return QueryResult(
            columns=["nonnull_fk", "orphans"],
            types=["BIGINT", "BIGINT"],
            cells=[[nonnull, orphans]],
            truncated=False,
        )


def _candidate(from_ds="db.s.child", to_ds="db.s.parent") -> Relationship:
    return Relationship(
        from_dataset=from_ds,
        from_columns=["c"],
        to_dataset=to_ds,
        to_columns=["p"],
        kind=RelationshipKind.OVERLAP_INFERRED,
    )


def test_probe_overlap_candidates_proposes_strong_containment():
    candidate = _candidate()
    adapter = _StubAdapter([(100, 0)])
    rejected = probe_overlap_candidates(adapter, [candidate])
    assert rejected == 0
    assert candidate.verified is True
    assert candidate.orphan_fraction == 0.0
    assert candidate.confidence == 0.95


def test_probe_overlap_candidates_rejects_weak_containment():
    candidate = _candidate()
    adapter = _StubAdapter([(100, 50)])  # 50% orphaned, far past the ceiling
    rejected = probe_overlap_candidates(adapter, [candidate])
    assert rejected == 1
    assert candidate.verified is False
    assert candidate.orphan_fraction is None
    assert candidate.confidence is None


def test_probe_overlap_candidates_rejects_too_little_evidence():
    candidate = _candidate()
    adapter = _StubAdapter([(5, 0)])  # perfect containment, but only 5 rows
    rejected = probe_overlap_candidates(adapter, [candidate])
    assert rejected == 1
    assert candidate.verified is False


def test_probe_overlap_candidates_accepts_a_small_nonzero_orphan_fraction():
    candidate = _candidate()
    adapter = _StubAdapter([(100, 3)])  # 3% orphaned, under the 5% ceiling
    rejected = probe_overlap_candidates(adapter, [candidate])
    assert rejected == 0
    assert candidate.verified is True
    assert candidate.orphan_fraction == 0.03


def test_probe_overlap_candidates_survives_a_mid_loop_ceiling():
    """A candidate already decided before an OverCeilingError hits stays
    decided (in-place mutation), even though the function itself raises."""

    from exmergo_dex_core.guards.cost_guard import OverCeilingError

    class _ExhaustingAdapter(_StubAdapter):
        def run_query(self, sql, *, max_rows, timeout_seconds):
            if not self._rows:
                raise OverCeilingError("budget exhausted")
            return super().run_query(
                sql, max_rows=max_rows, timeout_seconds=timeout_seconds
            )

    accepted = _candidate("db.s.a", "db.s.b")
    never_reached = _candidate("db.s.c", "db.s.d")
    adapter = _ExhaustingAdapter([(100, 0)])
    with pytest.raises(OverCeilingError):
        probe_overlap_candidates(adapter, [accepted, never_reached])
    assert accepted.verified is True
    assert accepted.orphan_fraction == 0.0
    assert never_reached.verified is False


# --- carry-forward: a discovered edge survives a plain re-run --------------


def test_carry_forward_overlap_edges_persists_across_a_plain_run():
    prior = DexCache(
        datasets=[],
        relationships=[
            Relationship(
                from_dataset="db.s.billing_profiles",
                from_columns=["profile_ref"],
                to_dataset="db.s.accounts",
                to_columns=["acct_num"],
                kind=RelationshipKind.OVERLAP_INFERRED,
                verified=True,
                orphan_fraction=0.0,
                confidence=0.95,
            )
        ],
    )
    prior.provenance.connector = "duckdb"
    known = {"db.s.billing_profiles", "db.s.accounts"}
    rels, carried = _carry_forward_overlap_edges(prior, "duckdb", known, [])
    assert carried == 1
    assert rels[0].kind is RelationshipKind.OVERLAP_INFERRED


def test_carry_forward_overlap_edges_drops_an_edge_with_a_gone_endpoint():
    prior = DexCache(
        datasets=[],
        relationships=[
            Relationship(
                from_dataset="db.s.billing_profiles",
                from_columns=["profile_ref"],
                to_dataset="db.s.accounts",
                to_columns=["acct_num"],
                kind=RelationshipKind.OVERLAP_INFERRED,
            )
        ],
    )
    prior.provenance.connector = "duckdb"
    known = {"db.s.accounts"}  # billing_profiles is gone
    rels, carried = _carry_forward_overlap_edges(prior, "duckdb", known, [])
    assert carried == 0
    assert rels == []


def test_carry_forward_overlap_edges_ignores_a_different_connectors_cache():
    prior = DexCache(datasets=[], relationships=[])
    prior.provenance.connector = "bigquery"
    rels, carried = _carry_forward_overlap_edges(
        prior, "duckdb", {"db.s.a", "db.s.b"}, []
    )
    assert carried == 0
    assert rels == []


def test_carry_forward_overlap_edges_ignores_a_name_inferred_edge():
    prior = DexCache(
        datasets=[],
        relationships=[
            Relationship(
                from_dataset="db.s.orders",
                from_columns=["customer_id"],
                to_dataset="db.s.customers",
                to_columns=["customer_id"],
                kind=RelationshipKind.INFERRED,
            )
        ],
    )
    prior.provenance.connector = "duckdb"
    known = {"db.s.orders", "db.s.customers"}
    rels, carried = _carry_forward_overlap_edges(prior, "duckdb", known, [])
    assert carried == 0
    assert rels == []


def test_carry_forward_overlap_edges_does_not_duplicate_an_already_present_edge():
    edge = Relationship(
        from_dataset="db.s.billing_profiles",
        from_columns=["profile_ref"],
        to_dataset="db.s.accounts",
        to_columns=["acct_num"],
        kind=RelationshipKind.OVERLAP_INFERRED,
    )
    prior = DexCache(datasets=[], relationships=[edge])
    prior.provenance.connector = "duckdb"
    known = {"db.s.billing_profiles", "db.s.accounts"}
    rels, carried = _carry_forward_overlap_edges(prior, "duckdb", known, [edge])
    assert carried == 0
    assert rels == [edge]


# --- end-to-end: a real DuckDB pair with zero naming signal -----------------


def _build_overlap_demo(path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE accounts (acct_num INTEGER, plan VARCHAR)")
    conn.executemany(
        "INSERT INTO accounts VALUES (?, ?)",
        [(i, "pro" if i % 2 == 0 else "free") for i in range(1, 101)],
    )
    conn.execute(
        "CREATE TABLE billing_profiles (profile_ref INTEGER, payment_method VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO billing_profiles VALUES (?, ?)",
        [(i, "card" if i % 3 == 0 else "ach") for i in range(1, 101)],
    )
    conn.close()


def test_end_to_end_baseline_finds_nothing_and_names_the_flag(tmp_path: Path, capsys):
    path = tmp_path / "overlap.duckdb"
    _build_overlap_demo(path)
    data = _run(["explore", "relationships", "--path", str(path)], capsys)["data"]
    assert data["relationships"] == []
    assert any("--infer-by-overlap" in n for n in data["notes"])


def test_end_to_end_flag_discovers_the_value_overlap_edge(tmp_path: Path, capsys):
    path = tmp_path / "overlap.duckdb"
    _build_overlap_demo(path)
    data = _run(
        ["explore", "relationships", "--infer-by-overlap", "--path", str(path)], capsys
    )["data"]
    [rel] = data["relationships"]
    assert rel["kind"] == "overlap_inferred"
    assert rel["verified"] is True
    assert rel["orphan_fraction"] == 0.0
    assert {
        rel["from_dataset"].rsplit(".", 1)[-1],
        rel["to_dataset"].rsplit(".", 1)[-1],
    } == {
        "accounts",
        "billing_profiles",
    }
    assert any("overlap sweep proposed 1" in n for n in data["notes"])


def test_end_to_end_discovered_edge_survives_a_plain_rerun(tmp_path: Path, capsys):
    path = tmp_path / "overlap.duckdb"
    _build_overlap_demo(path)
    _run(
        ["explore", "relationships", "--infer-by-overlap", "--path", str(path)], capsys
    )

    data = _run(["explore", "relationships", "--path", str(path)], capsys)["data"]
    [rel] = data["relationships"]
    assert rel["kind"] == "overlap_inferred"
    assert any("carried forward 1 previously discovered" in n for n in data["notes"])
    # Still names the flag, since this run itself did not sweep.
    assert any("--infer-by-overlap" in n for n in data["notes"])


def test_end_to_end_resweeping_does_not_duplicate_or_reprobe(tmp_path: Path, capsys):
    path = tmp_path / "overlap.duckdb"
    _build_overlap_demo(path)
    _run(
        ["explore", "relationships", "--infer-by-overlap", "--path", str(path)], capsys
    )

    data = _run(
        ["explore", "relationships", "--infer-by-overlap", "--path", str(path)], capsys
    )["data"]
    assert len(data["relationships"]) == 1
    assert any("found no unmatched key-shaped column pair" in n for n in data["notes"])


def test_end_to_end_map_wires_the_same_flag(tmp_path: Path, capsys):
    path = tmp_path / "overlap.duckdb"
    _build_overlap_demo(path)
    data = _run(["explore", "map", "--infer-by-overlap", "--path", str(path)], capsys)[
        "data"
    ]
    assert data["relationship_count"] == 1
