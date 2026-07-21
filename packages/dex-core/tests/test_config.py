"""`.dex/config.yml` round-trips: what a user commits is what the engine reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core.config import (
    BlobOverride,
    DexConfig,
    PIIOverride,
    blob_override_paths,
    load_config,
    pii_override_paths,
    save_config,
)


def test_pii_overrides_round_trip(tmp_path: Path):
    config = DexConfig(
        pii_overrides=[
            PIIOverride(
                column="MY_DB.PUBLIC.REGION.R_NAME",
                reason="TPC-H region labels; reviewed",
            ),
            PIIOverride(column="my_db.public.part.p_name"),
        ]
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert [e.column for e in loaded.pii_overrides] == [
        "MY_DB.PUBLIC.REGION.R_NAME",
        "my_db.public.part.p_name",
    ]
    assert loaded.pii_overrides[0].reason == "TPC-H region labels; reviewed"
    assert loaded.pii_overrides[1].reason is None


def test_override_paths_are_case_insensitive():
    paths = pii_override_paths([PIIOverride(column="  MY_DB.PUBLIC.REGION.R_NAME ")])
    assert "my_db.public.region.r_name" in paths


def test_pii_override_pattern_round_trip(tmp_path: Path):
    config = DexConfig(
        pii_overrides=[
            PIIOverride(
                column_name="document_name",
                scope="proj.raw_*",
                reason="Firestore resource path, not a person name",
            ),
        ]
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    (entry,) = loaded.pii_overrides
    assert entry.column is None
    assert entry.column_name == "document_name"
    assert entry.scope == "proj.raw_*"
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "column:" not in text


@pytest.mark.parametrize(
    "kwargs",
    [
        # both shapes at once
        {"column": "db.t.c", "column_name": "c", "scope": "db.*"},
        # neither shape
        {},
        # pattern half-given
        {"column_name": "c"},
        {"scope": "db.*"},
    ],
)
def test_pii_override_rejects_invalid_shapes(kwargs):
    with pytest.raises(ValueError):
        PIIOverride(**kwargs)


def test_pii_override_pattern_matches_across_scope():
    paths = pii_override_paths(
        [PIIOverride(column_name="document_name", scope="db.raw_*")]
    )
    assert "db.raw_dev.t1.document_name" in paths
    assert "db.raw_qa.t2.document_name" in paths
    assert "db.other.t1.document_name" not in paths
    # a different column name on a matching table is untouched
    assert "db.raw_dev.t1.other_column" not in paths


def test_pii_override_pattern_matching_is_case_insensitive():
    paths = pii_override_paths(
        [PIIOverride(column_name="Document_Name", scope="DB.RAW_*")]
    )
    assert "db.raw_dev.t1.document_name" in paths


def test_config_without_overrides_stays_clean(tmp_path: Path):
    # exclude_unset keeps the committed file a record of explicit choices; an
    # untouched pii_overrides list must not appear in it.
    save_config(DexConfig(connector="duckdb"), tmp_path)
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "pii_overrides" not in text
    assert load_config(tmp_path).pii_overrides == []


def test_blob_overrides_round_trip(tmp_path: Path):
    config = DexConfig(
        blob_overrides=[
            BlobOverride(
                column="MY_DB.PUBLIC.SESSIONS.PAYLOAD",
                reason="small serialized state; worth profiling",
            ),
            BlobOverride(column="my_db.public.events.raw_blob"),
        ]
    )
    save_config(config, tmp_path)
    loaded = load_config(tmp_path)
    assert [e.column for e in loaded.blob_overrides] == [
        "MY_DB.PUBLIC.SESSIONS.PAYLOAD",
        "my_db.public.events.raw_blob",
    ]
    assert loaded.blob_overrides[0].reason == "small serialized state; worth profiling"
    assert loaded.blob_overrides[1].reason is None


def test_blob_override_paths_are_case_insensitive():
    paths = blob_override_paths(
        [BlobOverride(column="  MY_DB.PUBLIC.SESSIONS.PAYLOAD ")]
    )
    assert paths == {"my_db.public.sessions.payload"}


def test_config_without_blob_overrides_stays_clean(tmp_path: Path):
    save_config(DexConfig(connector="duckdb"), tmp_path)
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "blob_overrides" not in text
    assert load_config(tmp_path).blob_overrides == []
