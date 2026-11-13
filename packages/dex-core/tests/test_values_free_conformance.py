"""The shipped values-free contract, run against this release (#591).

The contract lives in the package so a host can run it against the release it
pins; this is the same suite run here, so a change that breaks the guarantee
fails this repository's CI before it reaches a host's.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from exmergo_dex_core.values_free_conformance import (
    SENTINELS,
    ValuesFreeContract,
    find_sentinels,
)


class TestValuesFreeConformance(ValuesFreeContract):
    pass


# The numeric sentinels: the ones a run of digits the run itself wrote can spell.
_NUMERIC = [s for s in SENTINELS if s.isdigit()]


@pytest.mark.parametrize("sentinel", _NUMERIC)
@pytest.mark.parametrize("offset", [0, 1])
def test_a_timestamp_s_microseconds_are_not_read_as_a_sentinel(
    sentinel: str, offset: int
) -> None:
    """A cache stamp whose microseconds spell a sentinel is not a leak (#1225)."""

    micros = ("0" * offset + sentinel + "0")[:6]
    stamp = f'{{"profiled_at": "2026-11-09T00:34:22.{micros}+00:00"}}'
    assert sentinel in stamp
    assert find_sentinels(stamp) == []
    assert find_sentinels(stamp.replace("T", " ")) == []


@pytest.mark.parametrize(
    "leak, sentinel",
    [
        ('"min_value": "731950.0001"}', "73195"),
        ('"max_value": 27182.75}', "27182"),
        ('"value_domain": ["9404401"]}', "94044"),
        ('"min_value": "880000"}', "88000"),
        ('"min_value": "1911-03-17T13:37:42"}', "1911-0"),
        ('"max_value": "1911-03-18 15:37:42"}', "1911-0"),
    ],
)
def test_a_seeded_value_is_still_found_beside_a_masked_stamp(
    leak: str, sentinel: str
) -> None:
    """Masking fractional seconds hides no value the fixture seeded."""

    text = '{"updated_at": "2026-11-09T00:34:22.731950+00:00", ' + leak
    assert find_sentinels(text) == [sentinel]
