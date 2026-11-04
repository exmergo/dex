"""`transform test --scaffold` resolves a source() input to the relation its
declaration names, not to whichever cached table shares its name."""

from __future__ import annotations

from pathlib import Path

import yaml

from exmergo_dex_core.cache import CacheProvenance, ColumnProfile, Dataset, DexCache
from exmergo_dex_core.dbt_project import load
from exmergo_dex_core.transform.test_scaffold import unit_test_scaffold_edits


def test_a_source_input_is_typed_from_the_relation_it_declares(dbt_project_dir: Path):
    """Two catalogs each hold an `orders` (a lake copy and the operational one,
    the everyday shape on Trino). The source declares which one the model
    reads, so the fixture is typed from that one rather than refused as
    ambiguous, and never typed from the other."""

    staging = dbt_project_dir / "models" / "staging"
    (staging / "_dex_sources.yml").write_text(
        "version: 2\n\nsources:\n  - name: memory_app\n    database: memory\n"
        "    schema: app\n    tables:\n      - name: orders\n",
        encoding="utf-8",
    )
    (staging / "stg_shop_orders.sql").write_text(
        "select id, status from {{ source('memory_app', 'orders') }}\n",
        encoding="utf-8",
    )
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="memory.app.orders",
                columns=[
                    ColumnProfile(name="id", data_type="bigint"),
                    ColumnProfile(name="status", data_type="varchar(9)"),
                ],
            ),
            Dataset(
                identifier="lake.app.orders",
                columns=[ColumnProfile(name="orderkey", data_type="bigint")],
            ),
        ],
        provenance=CacheProvenance(connector="trino"),
    )

    edits, inputs = unit_test_scaffold_edits(
        load(dbt_project_dir), cache, "stg_shop_orders"
    )

    assert inputs == ["orders"]
    test = yaml.safe_load(edits[0].new_content)["unit_tests"][0]
    assert test["given"][0]["input"] == "source('memory_app', 'orders')"
    assert set(test["given"][0]["rows"][0]) == {"id", "status"}


def test_a_source_whose_identifier_differs_from_its_name_is_found(
    dbt_project_dir: Path,
):
    """A source table named `scans` that reads the relation `feed_scans_v2`
    (its `identifier`) is cached under the relation, so looking it up by the
    name the model writes found nothing and the scaffold was refused (#1111)."""

    staging = dbt_project_dir / "models" / "staging"
    (staging / "_dex_sources.yml").write_text(
        "version: 2\n\nsources:\n  - name: feed\n    schema: raw\n    tables:\n"
        "      - name: scans\n        identifier: feed_scans_v2\n",
        encoding="utf-8",
    )
    (staging / "stg_scans.sql").write_text(
        "select scan_id from {{ source('feed', 'scans') }}\n", encoding="utf-8"
    )
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="raw.feed_scans_v2",
                columns=[ColumnProfile(name="scan_id", data_type="INTEGER")],
            )
        ],
        provenance=CacheProvenance(connector="duckdb"),
    )

    edits, inputs = unit_test_scaffold_edits(load(dbt_project_dir), cache, "stg_scans")

    assert inputs == ["scans"]
    test = yaml.safe_load(edits[0].new_content)["unit_tests"][0]
    assert test["given"][0] == {
        "input": "source('feed', 'scans')",
        "rows": [{"scan_id": 1}],
    }
