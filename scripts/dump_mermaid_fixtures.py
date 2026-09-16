#!/usr/bin/env python3
"""Render a corpus of `explore diagram` output for the Mermaid syntax canary.

Usage:
    python scripts/dump_mermaid_fixtures.py <output-dir>

dex emits Mermaid and never renders it, so there is no Python dependency whose
version pins the syntax we have to stay compatible with. The contract is with a
*parser* living in whatever tool a reader opens the diagram in, and the repo's
own rule is that a contract with someone outside this repository deserves a test
that runs outside this repository. This script is the dex half of that test: it
dumps every construct the renderer can emit, and `check_mermaid_syntax.mjs` feeds
them to the real Mermaid parser in CI.

The corpus is **generated from the renderer** rather than checked in, so it can
never drift away from what dex actually produces. It is also self-guarding: the
script fails if the cases it built stopped covering every cardinality glyph the
renderer can emit, because a canary that silently stopped exercising the
interesting paths is worse than no canary (it reports green either way).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "dex-core" / "src"))

from exmergo_dex_core.cache import (  # noqa: E402
    CacheProvenance,
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
    Relationship,
    RelationshipKind,
)
from exmergo_dex_core.explore.diagram import (  # noqa: E402
    render_er_mermaid,
)

# Every glyph pair `_edge` can produce. Kept here as the coverage assertion
# rather than as documentation: if the renderer gains a claim level, this list
# and the corpus below have to grow together or the dump fails.
EXPECTED_GLYPHS = {
    "||--o{",
    "||..o{",
    "|o..o{",
    "}o..o{",
    "||--o|",
}


def _cache(datasets: list[Dataset], relationships: list[Relationship]) -> DexCache:
    return DexCache(
        datasets=datasets,
        relationships=relationships,
        provenance=CacheProvenance(
            connector="duckdb", updated_at="2026-08-03T00:00:00Z"
        ),
    )


def corpus() -> dict[str, DexCache]:
    """One cache per construct worth putting in front of a real parser."""

    proven_parent = Dataset(
        identifier="shop.main.customers",
        columns=[
            ColumnProfile(name="customer_id", data_type="INTEGER", is_unique=True),
            ColumnProfile(name="city", data_type="VARCHAR"),
        ],
        candidate_keys=[["customer_id"]],
        grain=["customer_id"],
        rank_score=0.9,
    )
    child = Dataset(
        identifier="shop.main.orders",
        columns=[
            ColumnProfile(name="order_id", data_type="INTEGER", is_unique=True),
            ColumnProfile(name="customer_id", data_type="INTEGER"),
        ],
        candidate_keys=[["order_id"]],
        grain=["order_id"],
        rank_score=0.8,
    )
    unproven_parent = Dataset(
        identifier="shop.main.loose",
        columns=[ColumnProfile(name="customer_id", data_type="INTEGER")],
    )
    one_to_one_child = Dataset(
        identifier="shop.main.profiles",
        columns=[
            ColumnProfile(name="customer_id", data_type="INTEGER", is_unique=True)
        ],
        candidate_keys=[["customer_id"]],
    )

    def edge(**kwargs: object) -> Relationship:
        return Relationship(
            from_dataset="shop.main.orders",
            from_columns=["customer_id"],
            to_dataset="shop.main.customers",
            to_columns=["customer_id"],
            **kwargs,  # type: ignore[arg-type]
        )

    cases: dict[str, DexCache] = {
        # One per claim level, which is the whole point of the render policy.
        "edge_declared": _cache(
            [proven_parent, child],
            [edge(kind=RelationshipKind.DECLARED, confidence=1.0)],
        ),
        # A join the semantic layer declares. The label carries the entity name in
        # single quotes, which is a character the renderer keeps (it strips only
        # the double quote that would end the label), so it belongs in front of a
        # real parser rather than in a unit test alone.
        "edge_declared_by_semantic_entity": _cache(
            [proven_parent, child],
            [
                edge(
                    kind=RelationshipKind.DECLARED,
                    confidence=1.0,
                    declared_by="semantic entity 'customer'",
                )
            ],
        ),
        "edge_verified_clean": _cache(
            [proven_parent, child],
            [edge(confidence=0.85, verified=True, orphan_fraction=0.0)],
        ),
        "edge_unverified": _cache([proven_parent, child], [edge(confidence=0.85)]),
        "edge_verified_orphans": _cache(
            [proven_parent, child],
            [edge(confidence=0.6, verified=True, orphan_fraction=0.37)],
        ),
        "edge_no_nonnull_keys": _cache(
            [proven_parent, child],
            [edge(confidence=0.7, verified=True, orphan_fraction=None)],
        ),
        "unproven_parent": _cache(
            [unproven_parent, child],
            [
                Relationship(
                    from_dataset="shop.main.orders",
                    from_columns=["customer_id"],
                    to_dataset="shop.main.loose",
                    to_columns=["customer_id"],
                )
            ],
        ),
        "one_to_one": _cache(
            [proven_parent, one_to_one_child],
            [
                Relationship(
                    from_dataset="shop.main.profiles",
                    from_columns=["customer_id"],
                    to_dataset="shop.main.customers",
                    to_columns=["customer_id"],
                    kind=RelationshipKind.DECLARED,
                )
            ],
        ),
        # A composite join, where both column lists carry more than one name.
        "composite_key": _cache(
            [
                Dataset(
                    identifier="shop.main.line_items",
                    columns=[
                        ColumnProfile(name="order_id", data_type="INTEGER"),
                        ColumnProfile(name="line_no", data_type="INTEGER"),
                    ],
                    composite_keys=[["order_id", "line_no"]],
                ),
                Dataset(
                    identifier="shop.main.shipments",
                    columns=[
                        ColumnProfile(name="order_id", data_type="INTEGER"),
                        ColumnProfile(name="line_no", data_type="INTEGER"),
                    ],
                ),
            ],
            [
                Relationship(
                    from_dataset="shop.main.shipments",
                    from_columns=["order_id", "line_no"],
                    to_dataset="shop.main.line_items",
                    to_columns=["order_id", "line_no"],
                    kind=RelationshipKind.DECLARED,
                )
            ],
        ),
        # The names and types a real warehouse will hand the renderer and a
        # naive one would paste straight into the output: parameterized and
        # nested types, an embedded quote, a digit-leading name, punctuation,
        # a name that collides across schemas, and PII at both ends of the
        # confidence range.
        "hostile_names_and_types": _cache(
            [
                Dataset(
                    identifier="vault.main.access_tokens",
                    columns=[
                        ColumnProfile(
                            name="id", data_type="NUMERIC(10,2)", is_unique=True
                        ),
                        ColumnProfile(
                            name='we"ird col',
                            data_type="STRUCT<a INT, b INT>",
                            distinct_count=3,
                        ),
                        ColumnProfile(
                            name="2fa_code",
                            data_type="TIMESTAMP WITH TIME ZONE",
                            pii=PIIFlag(
                                category=PIICategory.CREDENTIAL, confidence=0.9
                            ),
                        ),
                        ColumnProfile(
                            name="ssn",
                            data_type="character varying",
                            null_fraction=0.0123,
                            pii=PIIFlag(
                                category=PIICategory.GOVERNMENT_ID, confidence=0.5
                            ),
                        ),
                        ColumnProfile(name="weird-name!", data_type=""),
                        ColumnProfile(
                            name="ARRAY_COL", data_type="ARRAY<STRUCT<x INT>>"
                        ),
                    ],
                ),
                Dataset(
                    identifier="vault.raw.access_tokens",
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER", is_unique=True)
                    ],
                ),
            ],
            [],
        ),
        # An entity reached only through an edge, so it appears with no
        # attribute block at all.
        "edge_only_entity": _cache(
            [proven_parent, Dataset(identifier="shop.main.bare", columns=[])],
            [
                Relationship(
                    from_dataset="shop.main.bare",
                    from_columns=["customer_id"],
                    to_dataset="shop.main.customers",
                    to_columns=["customer_id"],
                )
            ],
        ),
        # A single entity and nothing else: the smallest legal diagram.
        "single_entity": _cache(
            [
                Dataset(
                    identifier="a.b.solo",
                    columns=[
                        ColumnProfile(name="id", data_type="INTEGER", is_unique=True)
                    ],
                )
            ],
            [],
        ),
    }
    return cases


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    glyphs: set[str] = set()
    for name, cache in corpus().items():
        # Both modes, because the selection rules change which attributes and
        # which entities reach the output and either could produce the syntax
        # error the canary is looking for.
        for suffix, full in (("", False), ("_full", True)):
            rendered = render_er_mermaid(cache, full=full)
            path = out / f"{name}{suffix}.mmd"
            path.write_text(rendered.mermaid)
            written.append(path)
            glyphs.update(
                line.split()[1]
                for line in rendered.mermaid.splitlines()
                if " : " in line
            )

    missing = EXPECTED_GLYPHS - glyphs
    if missing:
        print(
            "the corpus stopped covering these edge shapes: "
            f"{', '.join(sorted(missing))}. A canary that does not exercise the "
            "interesting paths reports green either way; extend the corpus or "
            "update EXPECTED_GLYPHS to match the render policy.",
            file=sys.stderr,
        )
        return 1

    print(f"wrote {len(written)} fixture(s) to {out}")
    print(f"edge shapes covered: {', '.join(sorted(glyphs))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
