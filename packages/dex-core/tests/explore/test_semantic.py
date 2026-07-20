"""Tests for `explore semantic`: the backend-neutral abstraction, the hosted dbt
Cloud backend (against the fake GraphQL transport), and the local read-view.

The hosted query path and the local execution path are also live-verified against
real targets during development; these tests lock the offline-checkable behavior:
intent parsing, PII screening, payload capping, backend selection, envelope shape,
and the honest hosted cost posture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes.semantic import SECRET_TOKEN, FakeHostedBackend, table_json_result

from exmergo_dex_core import envelope as env
from exmergo_dex_core.cache import (
    ColumnProfile,
    Dataset,
    DexCache,
    PIICategory,
    PIIFlag,
)
from exmergo_dex_core.config import DexConfig, QueryLimits
from exmergo_dex_core.explore import semantic as sem
from exmergo_dex_core.explore.semantic import (
    SemanticQuery,
    cap_columnar,
    requested_dimension_refs,
    screen_dimension_refs,
)
from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend


class _Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ---- the shared abstraction -------------------------------------------------


def test_requested_dimension_refs_from_group_by_and_where():
    q = SemanticQuery(
        metrics=["m"],
        group_by=["user__pricing_tier", "metric_time"],
        where=[
            "{{ Dimension('session__is_deleted') }} = false",
            "{{ TimeDimension('metric_time', 'month') }} > '2020-01-01'",
            "{{ Entity('user') }} is not null",
        ],
    )
    refs = requested_dimension_refs(q)
    assert "user__pricing_tier" in refs
    assert "session__is_deleted" in refs
    assert "user" in refs
    assert len(refs) == len(set(refs))  # de-duplicated


def test_screen_blocks_pii_name_allows_clean():
    blocked = dict(screen_dimension_refs(["user__email", "user__pricing_tier"]))
    assert "user__email" in blocked
    assert "user__pricing_tier" not in blocked


def test_screen_meta_lookup_is_authoritative():
    blocked = dict(
        screen_dimension_refs(
            ["order__region"],
            meta_lookup=lambda ref: {"pii": True} if ref == "order__region" else None,
        )
    )
    assert "order__region" in blocked


def test_screen_evidence_clears_a_name_flagged_ref():
    # A column the profiler examined and cleared (or a human pii_overrides entry)
    # must stop being re-blocked by its name: evidence beats the heuristic.
    blocked = dict(
        screen_dimension_refs(["user__email"], meta_lookup=lambda _ref: {"pii": False})
    )
    assert blocked == {}


def test_screen_silence_never_clears():
    # A lookup that knows nothing must leave the fail-closed heuristic in charge.
    blocked = dict(screen_dimension_refs(["user__email"], meta_lookup=lambda _r: None))
    assert "user__email" in blocked


def test_cap_columnar_row_and_payload_caps():
    cells = [[i, "x"] for i in range(100)]
    data = cap_columnar(
        ["a", "b"],
        ["int", "str"],
        cells,
        max_rows=10,
        max_cell_chars=5,
        max_payload_bytes=100_000,
    )
    assert data["row_count"] == 10
    assert data["truncated"] is True
    assert any("truncated to 10 rows" in note for note in data["notes"])


def test_cap_columnar_cell_truncation():
    data = cap_columnar(
        ["a"],
        ["str"],
        [["abcdefghij"]],
        max_rows=50,
        max_cell_chars=3,
        max_payload_bytes=100_000,
    )
    assert data["cells"][0][0] == "abc..."


def test_resolve_backend_selection(monkeypatch):
    import exmergo_dex_core.explore.semantic.hosted as hosted_mod
    import exmergo_dex_core.explore.semantic.local as local_mod

    monkeypatch.setattr(
        hosted_mod.HostedDbtCloudBackend,
        "from_config",
        classmethod(lambda cls, args, config: "HOSTED"),
    )
    monkeypatch.setattr(
        local_mod.LocalMetricFlowBackend,
        "from_args",
        classmethod(lambda cls, args, config, root: "LOCAL"),
    )
    cfg = DexConfig()
    assert sem.resolve_backend(_Args(api=True, local=False), cfg, ".") == "HOSTED"
    assert sem.resolve_backend(_Args(api=False, local=True), cfg, ".") == "LOCAL"
    # default (no flag) follows config; a bare project defaults to local
    assert sem.resolve_backend(_Args(api=False, local=False), cfg, ".") == "LOCAL"
    cfg_cloud = DexConfig(semantic={"backend": "dbt_cloud"})
    hosted = sem.resolve_backend(_Args(api=False, local=False), cfg_cloud, ".")
    assert hosted == "HOSTED"
    with pytest.raises(sem.SemanticBackendError):
        sem.resolve_backend(_Args(api=True, local=True), cfg, ".")


# ---- hosted backend (fake transport) ----------------------------------------


def _viz_like_metrics():
    return [
        {
            "name": "sessions",
            "type": "SIMPLE",
            "label": "Sessions",
            "description": "Total sessions.",
            "dimensions": [
                {"name": "metric_time", "type": "TIME"},
                {"name": "user__pricing_tier", "type": "CATEGORICAL"},
            ],
            "entities": [{"name": "user", "type": "PRIMARY"}],
        }
    ]


def test_hosted_list_definitions():
    backend = FakeHostedBackend(metrics=_viz_like_metrics())
    catalog = backend.list_definitions()
    assert catalog.backend == "dbt_cloud"
    assert catalog.metrics[0].name == "sessions"
    assert catalog.metrics[0].type == "simple"  # normalized from SIMPLE
    assert "user__pricing_tier" in catalog.metrics[0].dimensions
    assert any(d.name == "user__pricing_tier" for d in catalog.dimensions)
    assert any(e.name == "user" for e in catalog.entities)


def test_hosted_query_is_warn_only_and_shaped():
    result = table_json_result(
        ["metric_time__month", "sessions"],
        ["datetime", "string"],
        [["2025-01-01", 5.0], ["2025-02-01", 9.0]],
    )
    backend = FakeHostedBackend(result=result)
    envelope = backend.query(
        SemanticQuery(metrics=["sessions"], group_by=["metric_time__month"], limit=5)
    )
    assert envelope.status == env.Status.OK
    # honest posture: paradigm hosted, no estimate/ceiling, explicit warning
    assert envelope.cost.paradigm == env.Paradigm.HOSTED
    assert envelope.cost.estimate is None and envelope.cost.ceiling is None
    assert any("cost guard unavailable" in w for w in envelope.warnings)
    # the pandas index column is dropped; shape matches explore query
    assert envelope.data["columns"] == ["metric_time__month", "sessions"]
    assert envelope.data["row_count"] == 2
    assert envelope.data["query_id"] == "FAKE_QID"


def test_hosted_pii_gate_blocks_before_execution():
    backend = FakeHostedBackend()
    envelope = backend.query(
        SemanticQuery(metrics=["sessions"], group_by=["user__email"])
    )
    assert envelope.status == env.Status.ERROR
    assert "PII" in envelope.errors[0]
    # the refusal happens before the query is submitted for execution
    assert not any("createQuery" in posted for posted in backend.posted)


def test_hosted_failed_query_surfaces_error():
    backend = FakeHostedBackend(status="FAILED", error="bad grain")
    envelope = backend.query(SemanticQuery(metrics=["sessions"]))
    assert envelope.status == env.Status.ERROR
    assert "bad grain" in envelope.errors[0]


# ---- local backend read-view ------------------------------------------------


def _write_manifest(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    manifest = {
        "semantic_models": [
            {
                "name": "orders",
                "entities": [{"name": "order", "type": "primary"}],
                "dimensions": [{"name": "status", "type": "categorical"}],
                "measures": [{"name": "order_count", "agg": "count"}],
            }
        ],
        "metrics": [
            {
                "name": "orders",
                "type": "simple",
                "label": "Orders",
                "type_params": {"input_measures": [{"name": "order_count"}]},
            }
        ],
    }
    (project / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))
    return project


def test_local_list_reads_manifest(tmp_path: Path):
    backend = LocalMetricFlowBackend(
        _write_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    catalog = backend.list_definitions()
    assert catalog.backend == "local"
    orders = next(m for m in catalog.metrics if m.name == "orders")
    assert "order__status" in orders.dimensions
    assert "metric_time" in orders.dimensions
    assert any(e.name == "order" for e in catalog.entities)


def test_local_list_missing_manifest_errors(tmp_path: Path):
    backend = LocalMetricFlowBackend(tmp_path, None, None, "duckdb", QueryLimits())
    with pytest.raises(sem.SemanticBackendError):
        backend.list_definitions()


def test_local_query_pii_gate_blocks_before_render(tmp_path: Path):
    # No manifest and no metricflow needed: the PII gate runs before rendering.
    backend = LocalMetricFlowBackend(tmp_path, None, None, "duckdb", QueryLimits())
    envelope = backend.query(
        SemanticQuery(metrics=["orders"], group_by=["customer__email"])
    )
    assert envelope.status == env.Status.ERROR
    assert "PII" in envelope.errors[0]


# ---- local guards: cache-backed PII + namespace pre-check -------------------


def _cache_with(columns: list[ColumnProfile], identifier: str = "wh.main.orders"):
    return DexCache(datasets=[Dataset(identifier=identifier, columns=columns)])


def _relation_manifest(tmp_path: Path, relation: str = "`wh`.`main`.`orders`") -> Path:
    """A manifest whose semantic model resolves to a physical relation, so the
    dimension-to-column mapping and the relation pre-check have something real."""

    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    manifest = {
        "semantic_models": [
            {
                "name": "orders",
                "node_relation": {"alias": "orders", "relation_name": relation},
                "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                "dimensions": [
                    {"name": "contact", "type": "categorical", "expr": "contact_col"},
                    {"name": "status", "type": "categorical"},
                ],
                "measures": [{"name": "order_count", "agg": "count"}],
            }
        ],
        "metrics": [
            {
                "name": "orders",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "order_count"}]},
            }
        ],
    }
    (project / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))
    return project


def test_local_cache_pii_flag_blocks_a_clean_named_dimension(tmp_path: Path):
    # `order__contact` reads innocuous by name; the cache says its physical column
    # is flagged email. Evidence must block what the heuristic would have allowed.
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    cache = _cache_with(
        [
            ColumnProfile(
                name="contact_col",
                data_type="VARCHAR",
                pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
            )
        ]
    )
    lookup = backend._cache_pii_lookup(cache)
    assert lookup("order__contact") == {"pii": True, "category": "email"}
    blocked = dict(screen_dimension_refs(["order__contact"], meta_lookup=lookup))
    assert "order__contact" in blocked


def test_local_cache_clears_a_profiled_unflagged_column(tmp_path: Path):
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    cache = _cache_with([ColumnProfile(name="contact_col", data_type="VARCHAR")])
    assert backend._cache_pii_lookup(cache)("order__contact") == {"pii": False}


def test_local_cache_lookup_is_silent_on_unknown_dimensions(tmp_path: Path):
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    lookup = backend._cache_pii_lookup(_cache_with([]))
    # Not in the manifest at all, and a column the cache never profiled: both must
    # return None so the name heuristic stays in charge.
    assert lookup("nowhere__thing") is None
    assert lookup("order__status") is None


def test_namespace_precheck_refuses_a_foreign_relation(tmp_path: Path):
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    cache = _cache_with([ColumnProfile(name="status", data_type="VARCHAR")])
    sql = "SELECT status FROM other_db.main.orders"
    message = backend._namespace_mismatch(sql, cache, "duckdb")
    assert message is not None
    assert "different namespace" in message


def test_namespace_precheck_accepts_a_suffix_match(tmp_path: Path):
    # The cache is connector-normalized, so a legitimate spelling that resolves by
    # suffix must pass rather than being rejected on an exact string compare.
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    cache = _cache_with([ColumnProfile(name="status", data_type="VARCHAR")])
    for sql in ("SELECT status FROM main.orders", "SELECT status FROM orders"):
        assert backend._namespace_mismatch(sql, cache, "duckdb") is None


def test_namespace_precheck_noops_without_an_inventory(tmp_path: Path):
    # No `explore map` yet: nothing to check against, so metric queries still run.
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), None, None, "duckdb", QueryLimits()
    )
    sql = "SELECT status FROM anything.at.all"
    assert backend._namespace_mismatch(sql, None, "duckdb") is None
    assert backend._namespace_mismatch(sql, DexCache(datasets=[]), "duckdb") is None


def test_token_never_reaches_the_envelope():
    result = table_json_result(["sessions"], ["string"], [[5.0]])
    backend = FakeHostedBackend(result=result)
    envelope = backend.query(SemanticQuery(metrics=["sessions"]))
    # the sanitizer must accept it (no secret-like keys), and the token value must
    # appear nowhere in the serialized envelope
    env.sanitize(envelope)
    assert SECRET_TOKEN not in json.dumps(envelope.model_dump(mode="json"))
