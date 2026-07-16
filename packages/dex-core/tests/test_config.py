"""`.dex/config.yml` round-trips: what a user commits is what the engine reads."""

from __future__ import annotations

from pathlib import Path

from exmergo_dex_core.config import (
    DexConfig,
    PIIOverride,
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
    assert paths == {"my_db.public.region.r_name"}


def test_config_without_overrides_stays_clean(tmp_path: Path):
    # exclude_unset keeps the committed file a record of explicit choices; an
    # untouched pii_overrides list must not appear in it.
    save_config(DexConfig(connector="duckdb"), tmp_path)
    text = (tmp_path / ".dex" / "config.yml").read_text()
    assert "pii_overrides" not in text
    assert load_config(tmp_path).pii_overrides == []
