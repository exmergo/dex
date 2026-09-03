"""`explore diagram` end to end: the cache gate, the cardinality claims the
renderer is and is not allowed to make, the selection and its elision notes, and
an envelope that carries a Mermaid string but never a column value.

Two layers. The renderer is a pure function of the cache, so it is unit-tested
against hand-built caches where every proof (a declared edge, a probe-verified
overlap, a proven composite key) can be set exactly; the envelope layer runs the
real CLI over DuckDB to prove the wiring, the free-cost posture, and that the
command never opens a connection.
"""

from __future__ import annotations

import json
from itertools import permutations
from pathlib import Path

import pytest

from exmergo_dex_core.cache import (
    CacheProvenance,
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
    Relationship,
    RelationshipKind,
)
from exmergo_dex_core.cli import main
from exmergo_dex_core.explore.diagram import render_er_mermaid


def _col(name: str, data_type: str = "INTEGER", **kwargs) -> ColumnProfile:
    return ColumnProfile(name=name, data_type=data_type, **kwargs)


def _ds(identifier: str, columns: list[ColumnProfile], **kwargs) -> Dataset:
    return Dataset(identifier=identifier, columns=columns, **kwargs)


def _rel(
    from_dataset: str,
    from_columns: list[str],
    to_dataset: str,
    to_columns: list[str],
    **kwargs,
) -> Relationship:
    return Relationship(
        from_dataset=from_dataset,
        from_columns=from_columns,
        to_dataset=to_dataset,
        to_columns=to_columns,
        **kwargs,
    )


def _cache(datasets: list[Dataset], relationships: list[Relationship]) -> DexCache:
    return DexCache(
        datasets=datasets,
        relationships=relationships,
        provenance=CacheProvenance(
            connector="duckdb", updated_at="2026-08-03T00:00:00Z"
        ),
    )


def _star(**rel_kwargs) -> DexCache:
    """One parent with a proven key and one child pointing at it."""

    customers = _ds(
        "shop.main.customers",
        [_col("customer_id", is_unique=True), _col("city", "VARCHAR")],
        candidate_keys=[["customer_id"]],
        grain=["customer_id"],
    )
    orders = _ds(
        "shop.main.orders",
        [_col("order_id", is_unique=True), _col("customer_id")],
        candidate_keys=[["order_id"]],
        grain=["order_id"],
    )
    rel = _rel(
        "shop.main.orders",
        ["customer_id"],
        "shop.main.customers",
        ["customer_id"],
        **rel_kwargs,
    )
    return _cache([customers, orders], [rel])


def _edge_line(mermaid: str) -> str:
    return next(line.strip() for line in mermaid.splitlines() if " : " in line)


# --- cardinality: what the renderer may and may not claim ---------------------


def test_a_declared_edge_onto_a_proven_key_claims_exactly_one():
    rendered = render_er_mermaid(_star(kind=RelationshipKind.DECLARED, confidence=1.0))
    assert "customers ||--o{ orders" in rendered.mermaid


def test_a_verified_inference_with_no_orphans_claims_exactly_one():
    rendered = render_er_mermaid(
        _star(confidence=0.85, verified=True, orphan_fraction=0.0)
    )
    assert "customers ||..o{ orders" in rendered.mermaid


def test_an_unverified_inference_will_not_claim_exactly_one():
    """Nothing measured the overlap, so the parent may be absent for some child
    row. The glyph degrades to zero-or-one rather than asserting a fact the
    cache does not hold."""

    rendered = render_er_mermaid(_star(confidence=0.85))
    assert "customers |o..o{ orders" in rendered.mermaid


def test_a_verified_inference_with_orphans_will_not_claim_exactly_one():
    rendered = render_er_mermaid(
        _star(confidence=0.85, verified=True, orphan_fraction=0.12)
    )
    assert "customers |o..o{ orders" in rendered.mermaid
    assert "verified 12.0% orphans" in _edge_line(rendered.mermaid)


def test_an_unproven_parent_key_is_drawn_as_many_to_many():
    """The strongest statement available when uniqueness was never established:
    a "one" side would be an invention, and a picture is believed more readily
    than the JSON it came from."""

    customers = _ds(
        "shop.main.customers", [_col("customer_id"), _col("city", "VARCHAR")]
    )
    orders = _ds(
        "shop.main.orders", [_col("order_id", is_unique=True), _col("customer_id")]
    )
    cache = _cache(
        [customers, orders],
        [
            _rel(
                "shop.main.orders",
                ["customer_id"],
                "shop.main.customers",
                ["customer_id"],
            )
        ],
    )
    assert "customers }o..o{ orders" in render_er_mermaid(cache).mermaid


def test_a_unique_child_key_narrows_the_child_side_to_at_most_one():
    profile = _ds(
        "shop.main.profiles",
        [_col("customer_id", is_unique=True)],
        candidate_keys=[["customer_id"]],
    )
    customers = _ds(
        "shop.main.customers",
        [_col("customer_id", is_unique=True)],
        candidate_keys=[["customer_id"]],
    )
    cache = _cache(
        [customers, profile],
        [
            _rel(
                "shop.main.profiles",
                ["customer_id"],
                "shop.main.customers",
                ["customer_id"],
                kind=RelationshipKind.DECLARED,
            )
        ],
    )
    assert "customers ||--o| profiles" in render_er_mermaid(cache).mermaid


def test_a_probe_proven_composite_key_counts_as_proof():
    """`composite_keys` is the probe-proven form and outranks the derived
    `candidate_keys`, so a composite parent key still supports a one side."""

    parent = _ds(
        "shop.main.line_items",
        [_col("order_id"), _col("line_no")],
        composite_keys=[["order_id", "line_no"]],
    )
    child = _ds("shop.main.shipments", [_col("order_id"), _col("line_no")])
    cache = _cache(
        [parent, child],
        [
            _rel(
                "shop.main.shipments",
                ["order_id", "line_no"],
                "shop.main.line_items",
                ["order_id", "line_no"],
                kind=RelationshipKind.DECLARED,
            )
        ],
    )
    assert "line_items ||--o{ shipments" in render_er_mermaid(cache).mermaid


def test_declared_edges_are_solid_and_inferred_edges_are_dotted():
    declared = render_er_mermaid(_star(kind=RelationshipKind.DECLARED)).mermaid
    inferred = render_er_mermaid(_star(confidence=0.6)).mermaid
    assert "--o{" in declared and ".." not in _edge_line(declared)
    assert "..o{" in inferred


def test_the_edge_label_states_the_kind_the_confidence_and_the_verification():
    rendered = render_er_mermaid(
        _star(confidence=0.85, verified=True, orphan_fraction=0.0)
    )
    assert _edge_line(rendered.mermaid).endswith(
        ': "customer_id, inferred 0.85, verified, no orphans"'
    )


# --- what never crosses into the diagram --------------------------------------


def test_no_column_value_reaches_the_diagram_and_pii_is_marked_not_hidden():
    """Profile-don't-exfiltrate, restated for a shareable artifact: a diagram
    gets pasted into issues and commits, so the min/max the cache holds for
    non-flagged columns must not ride along. A PII flag is (category,
    confidence) and is drawn, because flagging is the point."""

    cache = _cache(
        [
            _ds(
                "shop.main.customers",
                [
                    _col(
                        "customer_id",
                        is_unique=True,
                        min_value=1,
                        max_value=99999,
                    ),
                    _col(
                        "email",
                        "VARCHAR",
                        min_value="aaron@example.com",
                        max_value="zoe@example.com",
                        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.95),
                    ),
                ],
                candidate_keys=[["customer_id"]],
            ),
            _ds("shop.main.orders", [_col("order_id"), _col("customer_id")]),
        ],
        [
            _rel(
                "shop.main.orders",
                ["customer_id"],
                "shop.main.customers",
                ["customer_id"],
            )
        ],
    )
    mermaid = render_er_mermaid(cache, full=True).mermaid
    for value in ("99999", "aaron@example.com", "zoe@example.com"):
        assert value not in mermaid
    assert "pii:email 0.95" in mermaid


def test_an_approximate_distinct_count_is_marked_approximate():
    cache = _cache(
        [
            _ds(
                "a.b.c",
                [
                    _col("exact_col", distinct_count=10, distinct_count_exact=True),
                    _col("sketch_col", distinct_count=10),
                ],
            )
        ],
        [],
    )
    mermaid = render_er_mermaid(cache, full=True).mermaid
    assert '"10 distinct"' in mermaid
    assert '"~10 distinct"' in mermaid


# --- Mermaid syntax hygiene ---------------------------------------------------


def test_a_dotted_identifier_becomes_a_legal_entity_name_with_a_legend():
    rendered = render_er_mermaid(_star(confidence=0.8))
    assert "    customers {" in rendered.mermaid
    assert "%% customers = shop.main.customers" in rendered.mermaid
    assert rendered.entities["customers"] == "shop.main.customers"


def test_same_named_objects_in_two_schemas_get_distinct_entity_names():
    """Mermaid names carry no dots, so two `orders` tables would collide into
    one box and silently merge two different objects."""

    cache = _cache(
        [
            _ds("shop.raw.orders", [_col("order_id", is_unique=True)]),
            _ds("shop.mart.orders", [_col("order_id", is_unique=True)]),
        ],
        [],
    )
    rendered = render_er_mermaid(cache, full=True)
    assert set(rendered.entities.values()) == {"shop.raw.orders", "shop.mart.orders"}
    assert len(rendered.entities) == 2


@pytest.mark.parametrize(
    ("declared", "rendered"),
    [
        ("NUMERIC(10,2)", "NUMERIC"),
        ("STRUCT<a INT, b INT>", "STRUCT"),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP_WITH_TIME_ZONE"),
        ("character varying", "character_varying"),
        ("", "unknown"),
    ],
)
def test_a_data_type_is_normalized_to_one_mermaid_safe_token(declared, rendered):
    """Parameterization and element types are what break Mermaid's attribute
    parser, which wants a single bare token."""

    cache = _cache([_ds("a.b.c", [_col("value", declared)])], [])
    assert f"        {rendered} value" in render_er_mermaid(cache, full=True).mermaid


def test_a_quote_in_a_column_name_cannot_break_out_of_the_comment():
    cache = _cache(
        [
            _ds(
                "a.b.c",
                [_col('we"ird', "VARCHAR", distinct_count=3)],
            )
        ],
        [],
    )
    mermaid = render_er_mermaid(cache, full=True).mermaid
    assert 'we"ird' not in mermaid
    assert "we_ird" in mermaid


def test_every_edge_uses_one_of_the_four_glyph_pairs_the_policy_allows():
    """The output was validated against the real Mermaid parser once, by hand,
    across every case below; that toolchain is not in CI, so this pins the
    vocabulary instead. An edge shape outside this set is either a syntax error
    Mermaid will reject or a cardinality claim the policy does not sanction, and
    both are worth failing on here rather than in a reader's browser."""

    allowed = {
        "||--o{",
        "||..o{",
        "|o..o{",
        "}o..o{",
        "||--o|",
        "||..o|",
        "|o..o|",
        "}o..o|",
    }
    caches = [
        _star(kind=RelationshipKind.DECLARED, confidence=1.0),
        _star(confidence=0.85),
        _star(confidence=0.85, verified=True, orphan_fraction=0.0),
        _star(confidence=0.85, verified=True, orphan_fraction=0.37),
    ]
    seen = set()
    for cache in caches:
        for line in render_er_mermaid(cache, full=True).mermaid.splitlines():
            if " : " not in line:
                continue
            glyph = line.split()[1]
            assert glyph in allowed, line
            seen.add(glyph)
    assert len(seen) >= 3, "the fixtures must exercise more than one claim level"


def test_every_rendered_line_is_a_comment_a_brace_or_a_known_construct():
    """A cheap structural guard: nothing should reach the output that is not one
    of the four shapes the conservative erDiagram subset uses."""

    mermaid = render_er_mermaid(_star(confidence=0.8), full=True).mermaid
    lines = [line for line in mermaid.splitlines() if line.strip()]
    assert lines[0] == "erDiagram"
    for line in lines[1:]:
        stripped = line.strip()
        assert (
            stripped.startswith("%%")
            or stripped.endswith("{")
            or stripped == "}"
            or " : " in stripped
            or line.startswith("        ")
        ), line


# --- selection, and saying what was left out ----------------------------------


def test_the_default_draws_only_profiled_and_connected_objects():
    cache = _cache(
        [
            _ds(
                "a.b.connected_parent",
                [_col("id", is_unique=True)],
                candidate_keys=[["id"]],
            ),
            _ds("a.b.connected_child", [_col("id"), _col("parent_id")]),
            _ds("a.b.isolated", [_col("id")]),
            _ds("a.b.never_profiled", []),
        ],
        [_rel("a.b.connected_child", ["parent_id"], "a.b.connected_parent", ["id"])],
    )
    default = render_er_mermaid(cache)
    assert set(default.entities.values()) == {
        "a.b.connected_parent",
        "a.b.connected_child",
    }

    full = render_er_mermaid(cache, full=True)
    assert "a.b.isolated" in full.entities.values()


def test_an_object_with_no_profile_and_no_join_is_named_rather_than_dropped():
    cache = _cache(
        [
            _ds(
                "a.b.connected_parent",
                [_col("id", is_unique=True)],
                candidate_keys=[["id"]],
            ),
            _ds("a.b.connected_child", [_col("id"), _col("parent_id")]),
            _ds("a.b.never_profiled", []),
        ],
        [_rel("a.b.connected_child", ["parent_id"], "a.b.connected_parent", ["id"])],
    )
    rendered = render_er_mermaid(cache, full=True)
    assert any("a.b.never_profiled" in note for note in rendered.notes)


def test_the_entity_cap_binds_and_reports_what_it_cut():
    datasets = [
        _ds(f"a.b.t{i}", [_col("id", is_unique=True)], rank_score=1.0 - i / 100)
        for i in range(10)
    ]
    rendered = render_er_mermaid(_cache(datasets, []), full=True, max_entities=3)
    assert rendered.entity_count == 3
    assert rendered.elided_entity_count == 7
    assert any("7 eligible object(s) were not drawn" in n for n in rendered.notes)
    # Kept by rank, not by cache order.
    assert set(rendered.entities.values()) == {"a.b.t0", "a.b.t1", "a.b.t2"}


def test_columns_outside_the_default_selection_are_counted_in_the_notes():
    cache = _cache(
        [
            _ds(
                "a.b.wide",
                [
                    _col("id", is_unique=True),
                    _col("noise_one", "VARCHAR"),
                    _col("noise_two", "VARCHAR"),
                ],
                candidate_keys=[["id"]],
                grain=["id"],
            ),
            _ds("a.b.child", [_col("wide_id")]),
        ],
        [_rel("a.b.child", ["wide_id"], "a.b.wide", ["id"])],
    )
    rendered = render_er_mermaid(cache)
    assert rendered.elided_column_count == 2
    assert any("2 column(s) were not drawn" in note for note in rendered.notes)
    assert "noise_one" not in rendered.mermaid
    assert "noise_one" in render_er_mermaid(cache, full=True).mermaid


def test_an_edge_whose_endpoint_was_cut_is_reported_not_silently_dropped():
    cache = _star(confidence=0.8)
    rendered = render_er_mermaid(cache, max_entities=1)
    assert rendered.edge_count == 0
    assert rendered.elided_edge_count == 1
    assert any("relationship(s) were not drawn" in note for note in rendered.notes)


def test_the_header_carries_the_connector_and_the_cache_timestamp():
    """A diagram outlives the terminal it was printed in, so how old the picture
    is has to travel inside the artifact."""

    mermaid = render_er_mermaid(_star(confidence=0.8)).mermaid
    assert "connector: duckdb" in mermaid
    assert "cached: 2026-08-03T00:00:00Z" in mermaid


def test_the_same_cache_renders_byte_identically_however_it_is_ordered():
    """Determinism is what makes a committed .mmd file diff cleanly and what
    makes every assertion above stable. Every permutation rather than a few
    shuffles: the set is small enough to be exhaustive, so this cannot pass by
    drawing a lucky ordering."""

    cache = _star(confidence=0.85)
    cache.datasets.append(
        _ds(
            "shop.main.items",
            [_col("item_id", is_unique=True)],
            candidate_keys=[["item_id"]],
        )
    )
    cache.relationships.append(
        _rel(
            "shop.main.orders",
            ["item_id"],
            "shop.main.items",
            ["item_id"],
            confidence=0.7,
        )
    )
    first = render_er_mermaid(cache, full=True).mermaid
    for datasets in permutations(cache.datasets):
        for relationships in permutations(cache.relationships):
            cache.datasets = list(datasets)
            cache.relationships = list(relationships)
            assert render_er_mermaid(cache, full=True).mermaid == first


# --- the envelope, through the real CLI ---------------------------------------


def _run(argv: list[str], capsys, *, expect_error: bool = False) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one envelope line"
    payload = json.loads(out)
    if expect_error:
        assert rc == 1 and payload["status"] == "error", payload
    else:
        assert rc == 0 and payload["status"] == "ok", payload
    return payload


def _mapped_repo(db: Path, tmp_path: Path, capsys) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert main(["explore", "map", "--path", str(db), "--repo-root", str(repo)]) == 0
    capsys.readouterr()  # drain the map envelope
    return repo


def test_diagram_renders_the_mapped_star_schema(f1_duckdb: Path, tmp_path, capsys):
    repo = _mapped_repo(f1_duckdb, tmp_path, capsys)
    payload = _run(
        ["explore", "diagram", "--path", str(f1_duckdb), "--repo-root", str(repo)],
        capsys,
    )
    data = payload["data"]
    assert data["mermaid"].startswith("erDiagram\n")
    assert data["entity_count"] == 3
    assert data["edge_count"] == 2
    # A list of records, never an object keyed by table name: the sanitizer
    # matches key names, so a table called `access_tokens` would otherwise fail
    # the boundary check. Pinned in the safety spine.
    assert {e["identifier"] for e in data["entities"]} == {
        "f1.main.races",
        "f1.main.drivers",
        "f1.main.results",
    }
    assert "results" in data["mermaid"] and "raceId" in data["mermaid"]
    # Free everywhere: nothing to estimate and nothing to confirm.
    assert payload["cost"]["estimate"] is None
    assert payload["cost"]["paradigm"] == "free_local"


def test_diagram_always_reports_notes_so_silence_is_a_positive_statement(
    f1_duckdb: Path, tmp_path, capsys
):
    repo = _mapped_repo(f1_duckdb, tmp_path, capsys)
    payload = _run(
        ["explore", "diagram", "--path", str(f1_duckdb), "--repo-root", str(repo)],
        capsys,
    )
    assert "notes" in payload["data"]


def test_diagram_full_widens_the_columns_it_draws(f1_duckdb: Path, tmp_path, capsys):
    repo = _mapped_repo(f1_duckdb, tmp_path, capsys)
    default = _run(
        ["explore", "diagram", "--path", str(f1_duckdb), "--repo-root", str(repo)],
        capsys,
    )["data"]
    full = _run(
        [
            "explore",
            "diagram",
            "--full",
            "--path",
            str(f1_duckdb),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )["data"]
    assert full["full"] is True
    assert full["elided_column_count"] == 0
    assert len(full["mermaid"]) > len(default["mermaid"])


def test_diagram_opens_no_connection(f1_duckdb: Path, tmp_path, capsys):
    """The whole command is a store read, so it must succeed with a target that
    could not possibly be opened. This is the evidence for the free declaration:
    a command that never reaches the warehouse cannot bill for one."""

    repo = _mapped_repo(f1_duckdb, tmp_path, capsys)
    payload = _run(
        [
            "explore",
            "diagram",
            "--path",
            str(tmp_path / "does-not-exist.duckdb"),
            "--repo-root",
            str(repo),
        ],
        capsys,
    )
    assert payload["data"]["entity_count"] == 3


def test_diagram_without_a_cache_refuses_as_a_prerequisite_naming_the_fix(
    f1_duckdb: Path, tmp_path, capsys
):
    repo = tmp_path / "empty"
    repo.mkdir()
    payload = _run(
        ["explore", "diagram", "--path", str(f1_duckdb), "--repo-root", str(repo)],
        capsys,
        expect_error=True,
    )
    assert payload["reason"] == "prerequisite"
    assert "explore map" in payload["errors"][0]


# --- a join the semantic layer declares (#361) --------------------------------


def test_a_semantic_edge_names_the_entity_it_came_from():
    """A solid line has two possible sources and a reader cannot tell them apart
    from the glyph, so the one that carries a lookupable name says it.

    `explore semantic list` is where that name resolves, which is what makes the
    label actionable rather than decorative.
    """

    cache = _star(
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
        declared_by="semantic entity 'customer'",
    )

    line = _edge_line(render_er_mermaid(cache).mermaid)

    assert "declared: semantic entity 'customer'" in line
    # Solid, like any declared edge: the layer states this join.
    assert "--" in line and ".." not in line


def test_a_semantic_edge_still_may_not_claim_exactly_one_without_proof():
    """A primary entity is the layer's claim, and the diagram's rule is that the
    *cache* must have proven the parent key unique before a crow's foot says
    "exactly one". Semantic edges inherit that unchanged, which is the whole
    reason they could be admitted at the declared tier without loosening
    anything.
    """

    cache = _star(
        kind=RelationshipKind.DECLARED,
        confidence=1.0,
        declared_by="semantic entity 'customer'",
    )
    parent = next(d for d in cache.datasets if d.identifier.endswith("customers"))
    parent.candidate_keys = []
    parent.columns[0].is_unique = None

    line = _edge_line(render_er_mermaid(cache).mermaid)

    assert line.startswith("customers }o--")


def test_an_inferred_edge_carries_no_declaration():
    line = _edge_line(render_er_mermaid(_star(confidence=0.62)).mermaid)

    assert "semantic entity" not in line
    assert "inferred 0.62" in line


def test_the_legend_names_both_sources_of_a_solid_line():
    mermaid = render_er_mermaid(_star(kind=RelationshipKind.DECLARED)).mermaid

    legend = next(line for line in mermaid.splitlines() if "solid lines" in line)
    assert "relationships test" in legend and "semantic-layer entity" in legend
