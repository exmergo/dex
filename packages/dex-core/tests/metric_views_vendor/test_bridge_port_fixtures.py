"""The converter's fixtures, through dex's port of it (#1260).

apache/ossie-converters pins the expected output of each direction with
checked-in YAML: ``ossie_{fixtureA,tpcds}_ossie.yaml`` export to their
``*_metric_view.yaml``, and ``ossie_fixtureB_metric_view.yaml`` imports to its
``*_ossie.yaml``. dex's port has to give the same documents, parsed equal and in
the same key order, stash blobs byte for byte, and the same notices. The notice
lists are read off the converter's source (``warnDroppedModel`` and friends),
since no JVM runs here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core.metric_views.loader import dump_yaml
from exmergo_dex_core.metric_views.ossie_bridge import (
    metric_view_to_ossie,
    ossie_to_metric_view,
)
from exmergo_dex_core.metric_views.ossie_bridge.common import (
    MV_VERSION,
    OSSIE_VERSION,
    parse_yaml,
)
from exmergo_dex_core.yaml_items import dump_items

from .conftest import FIXTURES

FIXTURE_NAMES = ["fixtureA", "fixtureB", "tpcds"]
PK_NOTICE = (
    "primary_key/unique_keys not stored as columns; used to set rely.at_most_one_match "
    "on a matching many_to_one join where applicable"
)
EXPORT_NOTICES = {
    "fixtureA": [
        f"[dataset 'orders'] {PK_NOTICE}",
        "[dataset 'orders'] dataset-level description dropped (no per-source "
        "comment field)",
        f"[dataset 'customer'] {PK_NOTICE}",
    ],
    "fixtureB": [f"[dataset 'orders'] {PK_NOTICE}"],
    "tpcds": [
        f"[dataset 'date_dim'] {PK_NOTICE}",
        f"[dataset 'item'] {PK_NOTICE}",
        f"[dataset 'customer'] {PK_NOTICE}",
    ],
}


def _text(fixture: str, side: str) -> str:
    suffix = "_ossie" if side == "ossie" else ""
    return (FIXTURES / f"converter_{fixture}{suffix}.yaml").read_text(encoding="utf-8")


def _keys_in_order(value: Any) -> Any:
    """``value`` with every mapping as its list of (key, value) pairs, so an
    equality test also checks key order."""
    if isinstance(value, dict):
        return [(k, _keys_in_order(v)) for k, v in value.items()]
    if isinstance(value, list):
        return [_keys_in_order(v) for v in value]
    return value


@pytest.mark.parametrize("fixture", ["fixtureA", "tpcds"])
def test_the_export_fixtures_match_the_converter(fixture):
    """``fixtureAExportMatchesFixture``, ``tpcdsExportMatchesFixture``."""
    converted = ossie_to_metric_view(_text(fixture, "ossie"))
    expected = parse_yaml(_text(fixture, "metric_view"))
    assert converted.value == expected
    assert _keys_in_order(converted.value) == _keys_in_order(expected)
    assert converted.converter_notices() == EXPORT_NOTICES[fixture]


def test_the_import_fixture_matches_the_converter():
    """``fixtureBImportMatchesFixture``: the stash blobs compared as strings."""
    converted = metric_view_to_ossie(_text("fixtureB", "metric_view"))
    expected = parse_yaml(_text("fixtureB", "ossie"))
    assert converted.value == expected
    assert _keys_in_order(converted.value) == _keys_in_order(expected)
    assert converted.converter_notices() == []


def test_the_stash_blob_uses_the_pinned_spacing():
    """``stashBlobUsesExpectedSpacing``."""
    model = metric_view_to_ossie(_text("fixtureB", "metric_view")).value
    blob = model["custom_extensions"][0]["data"]
    assert blob.startswith('{"_v": 1, ')
    assert blob == '{"_v": 1, "filter": "l_returnflag = \'N\'"}'


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_every_metric_view_fixture_round_trips(fixture):
    """MV -> Ossie -> MV gives the fixture back, through dex's own writers."""
    view = parse_yaml(_text(fixture, "metric_view"))
    ossie = dump_items(metric_view_to_ossie(_text(fixture, "metric_view")).value)
    back = ossie_to_metric_view(ossie).value
    assert back == view
    assert parse_yaml(dump_yaml(back)) == view


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_every_ossie_fixture_exports_with_the_listed_notices(fixture):
    assert (
        ossie_to_metric_view(_text(fixture, "ossie")).converter_notices()
        == EXPORT_NOTICES[fixture]
    )


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_every_import_emits_no_converter_notice(fixture):
    """The converter notices only what it drops, and these imports drop nothing."""
    assert metric_view_to_ossie(_text(fixture, "metric_view")).converter_notices() == []


def test_the_versions_are_the_ones_dex_reads():
    """A version bump on either side fails here, loudly, rather than as a
    refused proposal."""
    import json

    from exmergo_dex_core.metric_views.dependency import METRIC_VIEW_SPEC_VERSION

    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "src/exmergo_dex_core/ossie/schema/ossie-schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["version"]["const"] == OSSIE_VERSION
    assert MV_VERSION == METRIC_VIEW_SPEC_VERSION
