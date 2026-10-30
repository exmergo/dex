"""The governed spine on its own, without the layer that rendered the SQL.

Every layer that leaves execution to dex runs its rendered SQL through
``explore/semantic/governed.py``. These pin the two things that make it shareable:
it is reachable without any layer's library, and it is the only place a semantic
backend runs SQL, so the order it states cannot drift into a second copy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import exmergo_dex_core
from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
)
from exmergo_dex_core.config import DexConfig, QueryLimits
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic.governed import GovernedSql, unprofiled_note
from exmergo_dex_core.explore.semantic.policy import sub_threshold_note
from exmergo_dex_core.guards import PII_BLOCK_CONFIDENCE
from exmergo_dex_core.storage import MemoryStore

SEMANTIC = Path(exmergo_dex_core.__file__).parent / "explore" / "semantic"


def _spine(columns: dict[str, tuple[str, str]], config: DexConfig | None = None):
    engine = DexEngine(config=config or DexConfig(), store=MemoryStore())
    return GovernedSql(
        engine,
        "duckdb",
        QueryLimits(),
        lambda: columns,
        recompile_hint="re-read the layer",
    )


def _flagged_cache() -> DexCache:
    return DexCache(
        datasets=[
            Dataset(
                identifier="wh.main.orders",
                columns=[
                    ColumnProfile(
                        name="contact_col",
                        data_type="VARCHAR",
                        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
                    ),
                    ColumnProfile(name="status", data_type="VARCHAR"),
                ],
            )
        ]
    )


def test_the_spine_imports_no_semantic_layer():
    """Neither MetricFlow nor SLayer, nor dex's own readers for either, in a
    fresh interpreter. The modules the spine imports inside its functions are
    imported too, since a sys.modules probe cannot see them until they run."""

    probe = (
        "import sys;"
        "import exmergo_dex_core.explore.semantic.governed;"
        "import exmergo_dex_core.guards.sql_guard;"
        "import exmergo_dex_core.cache;"
        "print('\\n'.join(sorted(sys.modules)))"
    )
    out = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    pulled = set(out.stdout.split())
    forbidden_roots = {"metricflow", "metricflow_semantics", "slayer", "dbt"}
    forbidden_dex = {
        "exmergo_dex_core.metricflow_dialect",
        "exmergo_dex_core.dbt_project",
        "exmergo_dex_core.dbt_semantic",
        "exmergo_dex_core.explore.semantic.local",
        "exmergo_dex_core.explore.semantic.local_runtime",
        "exmergo_dex_core.slayer",
    }
    offenders = sorted(
        name
        for name in pulled
        if name.split(".")[0] in forbidden_roots
        or any(name == f or name.startswith(f"{f}.") for f in forbidden_dex)
    )
    assert offenders == []


@pytest.mark.parametrize(
    "call",
    ["assert_select_only", "referenced_relations", "billed_handshake", "run_query"],
)
def test_the_metricflow_backend_runs_no_sql_of_its_own(call: str):
    """The order is stated once. A backend that calls a step of it directly is a
    second copy, and the drift between the two would be silent."""

    assert call not in (SEMANTIC / "local.py").read_text(encoding="utf-8")


def test_the_pii_lookup_reads_a_mapping_the_layer_supplies():
    lookup = _spine(
        {
            "order__contact": ("wh.main.orders", "contact_col"),
            "order__status": ("main.orders", "STATUS"),
        }
    ).pii_lookup(_flagged_cache())

    assert lookup("order__contact") == {"pii": True, "category": "email"}
    # resolved by suffix and case-insensitively, and cleared because profiled
    assert lookup("order__status") == {"pii": False}
    assert lookup("order__missing") is None


@pytest.mark.parametrize("staging_first", [True, False])
def test_a_partial_relation_matching_several_datasets_fails_closed(staging_first):
    """`orders` names both relations: a flag on either refuses the column, and a
    clear on one is not a clear while the other cannot speak."""

    flagged = _flagged_cache().datasets[0]
    cleared = Dataset(
        identifier="wh.staging.orders",
        columns=[
            ColumnProfile(name="contact_col", data_type="VARCHAR"),
            ColumnProfile(name="status", data_type="VARCHAR"),
        ],
    )
    unprofiled = Dataset(identifier="wh.staging.orders")
    columns = {
        "order__contact": ("orders", "contact_col"),
        "order__status": ("orders", "status"),
    }
    datasets = [cleared, flagged] if staging_first else [flagged, cleared]

    lookup = _spine(columns).pii_lookup(DexCache(datasets=datasets))
    assert lookup("order__contact") == {"pii": True, "category": "email"}
    assert lookup("order__status") == {"pii": False}

    lookup = _spine(columns).pii_lookup(DexCache(datasets=[flagged, unprofiled]))
    assert lookup("order__status") is None


def test_the_mapping_is_not_asked_for_without_a_cache():
    asked: list[bool] = []

    def columns():
        asked.append(True)
        return {}

    engine = DexEngine(config=DexConfig(), store=MemoryStore())
    spine = GovernedSql(
        engine, "duckdb", QueryLimits(), columns, recompile_hint="re-read"
    )

    assert spine.pii_lookup(None) is None
    assert asked == []


def test_the_recompile_hint_is_the_layers_own():
    spine = _spine({})
    live = ["wh.main.orders", "wh.staging.customers"]
    foreign, _ = spine.relation_precheck(
        "SELECT 1 FROM other_db.main.orders", None, "duckdb", lambda: live
    )
    missing, _ = spine.relation_precheck(
        "SELECT 1 FROM wh.main.gone", None, "duckdb", lambda: live
    )

    assert foreign is not None and "re-read the layer against the target" in foreign
    assert missing is not None and "re-read the layer if the project" in missing
    assert "dbt" not in foreign and "dbt" not in missing


def test_a_config_override_clears_a_flag_in_memory():
    store = MemoryStore()
    store.save_cache(_flagged_cache())
    config = DexConfig(
        pii_overrides=[
            {"column": "wh.main.orders.contact_col", "reason": "reviewed: not PII"}
        ]
    )
    spine = GovernedSql(
        DexEngine(config=config, store=store),
        "duckdb",
        QueryLimits(),
        lambda: {"order__contact": ("wh.main.orders", "contact_col")},
        recompile_hint="re-read",
    )

    assert spine.pii_lookup(spine.load_cache())("order__contact") == {"pii": False}


def test_an_unprofiled_relation_is_disclosed_once_and_in_order():
    note = unprofiled_note(["wh.main.b", "wh.main.a", "wh.main.b"])
    assert len(note) == 1
    assert "wh.main.a, wh.main.b" in note[0]
    assert unprofiled_note([]) == []


def _sub_threshold_cache() -> DexCache:
    return DexCache(
        datasets=[
            Dataset(
                identifier="wh.main.customers",
                columns=[
                    ColumnProfile(
                        name="city",
                        data_type="VARCHAR",
                        pii=PIIFlag(category=PIICategory.ADDRESS, confidence=0.4),
                    ),
                    ColumnProfile(name="status", data_type="VARCHAR"),
                ],
            )
        ]
    )


def test_sub_threshold_flag_clears_with_category_and_confidence():
    """A flag below the block threshold is clearance, but the envelope must see it."""

    lookup = _spine({"customers__city": ("wh.main.customers", "city")}).pii_lookup(
        _sub_threshold_cache()
    )

    assert lookup("customers__city") == {
        "pii": False,
        "category": "address",
        "confidence": 0.4,
    }


def test_sub_threshold_note_names_the_cleared_flag():
    lookup = _spine({"customers__city": ("wh.main.customers", "city")}).pii_lookup(
        _sub_threshold_cache()
    )

    notes = sub_threshold_note(["customers__city"], meta_lookup=lookup)
    assert len(notes) == 1
    assert "customers__city is PII-flagged at confidence 0.4" in notes[0]
    assert f"below the {PII_BLOCK_CONFIDENCE:g} block threshold" in notes[0]


def test_sub_threshold_note_stays_quiet_for_bare_clearance():
    lookup = _spine(
        {
            "order__contact": ("wh.main.orders", "contact_col"),
            "order__status": ("main.orders", "STATUS"),
        }
    ).pii_lookup(_flagged_cache())

    assert lookup("order__status") == {"pii": False}
    assert sub_threshold_note(["order__status"], meta_lookup=lookup) == []
