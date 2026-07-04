"""The `.dex/` store's spend ledger: the substrate of the session budget."""

from __future__ import annotations

import json
from pathlib import Path

from exmergo_dex_core.cache import SPEND_FILE, DexStore


def test_spend_log_appends_jsonl(tmp_path: Path):
    store = DexStore(tmp_path)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
    store.append_spend_log({"at": "2026-07-03T11:00:00+00:00", "billed_bytes": 250})
    lines = (tmp_path / ".dex" / SPEND_FILE).read_text().splitlines()
    assert [json.loads(line)["billed_bytes"] for line in lines] == [100, 250]


def test_spend_since_sums_only_from_the_cutoff(tmp_path: Path):
    store = DexStore(tmp_path)
    store.append_spend_log({"at": "2026-07-02T23:59:00+00:00", "billed_bytes": 1_000})
    store.append_spend_log({"at": "2026-07-03T00:01:00+00:00", "billed_bytes": 200})
    store.append_spend_log({"at": "2026-07-03T12:00:00+00:00", "billed_bytes": 300})
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 500


def test_spend_since_is_zero_without_a_ledger(tmp_path: Path):
    assert DexStore(tmp_path).spend_since("2026-07-03T00:00:00+00:00") == 0.0


def test_spend_since_skips_malformed_lines(tmp_path: Path):
    store = DexStore(tmp_path)
    store.append_spend_log({"at": "2026-07-03T10:00:00+00:00", "billed_bytes": 100})
    path = tmp_path / ".dex" / SPEND_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"at": None, "billed_bytes": 50}) + "\n")
        handle.write(json.dumps({"at": "2026-07-03T11:00:00+00:00"}) + "\n")
    assert store.spend_since("2026-07-03T00:00:00+00:00") == 100
