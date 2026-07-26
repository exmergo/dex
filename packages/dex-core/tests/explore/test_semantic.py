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
from typing import ClassVar

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
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore import semantic as sem
from exmergo_dex_core.explore.semantic import (
    SemanticQuery,
    cap_columnar,
    requested_dimension_refs,
    screen_dimension_refs,
)
from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.storage import MemoryStore


def _engine(config: DexConfig | None = None, **kwargs) -> DexEngine:
    """A real engine, never opened. The backends read config and store off it and
    route their one billed path through it, so a stand-in would test the stand-in."""

    return DexEngine(config=config or DexConfig(), store=MemoryStore(), **kwargs)


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
        classmethod(lambda cls, config, source=None: "HOSTED"),
    )
    monkeypatch.setattr(
        local_mod.LocalMetricFlowBackend,
        "from_engine",
        classmethod(lambda cls, engine: "LOCAL"),
    )
    engine = _engine()
    assert sem.resolve_backend(engine, api=True) == "HOSTED"
    assert sem.resolve_backend(engine, local=True) == "LOCAL"
    # default (no flag) follows config; a bare project defaults to local
    assert sem.resolve_backend(engine) == "LOCAL"
    cloud = _engine(DexConfig(semantic={"backend": "dbt_cloud"}))
    assert sem.resolve_backend(cloud) == "HOSTED"
    with pytest.raises(sem.SemanticBackendError):
        sem.resolve_backend(engine, api=True, local=True)


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
    result = backend.query(
        SemanticQuery(metrics=["sessions"], group_by=["metric_time__month"], limit=5)
    )
    # honest posture: paradigm hosted, no estimate/ceiling, explicit warning
    assert result.cost.paradigm == env.Paradigm.HOSTED
    assert result.cost.estimate is None and result.cost.ceiling is None
    assert any("cost guard unavailable" in w for w in result.warnings)
    # the pandas index column is dropped; shape matches explore query
    assert result.columns == ["metric_time__month", "sessions"]
    assert result.row_count == 2
    assert result.query_id == "FAKE_QID"
    # and the same shape survives the trip to stdout
    envelope = to_envelope(result)
    assert envelope.status == env.Status.OK
    assert envelope.data["columns"] == ["metric_time__month", "sessions"]
    assert envelope.data["query_id"] == "FAKE_QID"


def test_hosted_pii_gate_blocks_before_execution():
    backend = FakeHostedBackend()
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        backend.query(SemanticQuery(metrics=["sessions"], group_by=["user__email"]))
    # the refusal happens before the query is submitted for execution
    assert not any("createQuery" in posted for posted in backend.posted)


def test_hosted_failed_query_surfaces_error():
    backend = FakeHostedBackend(status="FAILED", error="bad grain")
    with pytest.raises(sem.SemanticBackendError, match="bad grain"):
        backend.query(SemanticQuery(metrics=["sessions"]))


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
        _write_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
    )
    catalog = backend.list_definitions()
    assert catalog.backend == "local"
    orders = next(m for m in catalog.metrics if m.name == "orders")
    assert "order__status" in orders.dimensions
    assert "metric_time" in orders.dimensions
    assert any(e.name == "order" for e in catalog.entities)


def test_local_list_missing_manifest_errors(tmp_path: Path):
    backend = LocalMetricFlowBackend(tmp_path, _engine(), "duckdb", QueryLimits())
    with pytest.raises(sem.SemanticBackendError):
        backend.list_definitions()


def test_local_query_pii_gate_blocks_before_render(tmp_path: Path):
    # No manifest and no metricflow needed: the PII gate runs before rendering.
    backend = LocalMetricFlowBackend(tmp_path, _engine(), "duckdb", QueryLimits())
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        backend.query(SemanticQuery(metrics=["orders"], group_by=["customer__email"]))


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
        _relation_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
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
        _relation_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
    )
    cache = _cache_with([ColumnProfile(name="contact_col", data_type="VARCHAR")])
    assert backend._cache_pii_lookup(cache)("order__contact") == {"pii": False}


def test_local_cache_lookup_is_silent_on_unknown_dimensions(tmp_path: Path):
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
    )
    lookup = backend._cache_pii_lookup(_cache_with([]))
    # Not in the manifest at all, and a column the cache never profiled: both must
    # return None so the name heuristic stays in charge.
    assert lookup("nowhere__thing") is None
    assert lookup("order__status") is None


def test_namespace_precheck_refuses_a_foreign_relation(tmp_path: Path):
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
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
        _relation_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
    )
    cache = _cache_with([ColumnProfile(name="status", data_type="VARCHAR")])
    for sql in ("SELECT status FROM main.orders", "SELECT status FROM orders"):
        assert backend._namespace_mismatch(sql, cache, "duckdb") is None


def test_namespace_precheck_noops_without_an_inventory(tmp_path: Path):
    # No `explore map` yet: nothing to check against, so metric queries still run.
    backend = LocalMetricFlowBackend(
        _relation_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
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


def test_local_render_reaches_the_metricflow_engine(tmp_path: Path):
    """`_render` must resolve the MetricFlow engine, not the dex engine.

    The backend holds both, and for a while it held them under the same name, so
    rendering called the `DexEngine` object. Nothing caught it because every
    other local-backend test stops at the PII gate, which runs before rendering.
    This one goes through `_render` with the MetricFlow side faked, so the two
    engines can never collide again unnoticed.
    """

    pytest.importorskip("metricflow")

    class _Explained:
        sql_statement = type("_Sql", (), {"sql": "select 1 as orders"})()

    class _FakeMetricFlow:
        def __init__(self):
            self.requests = []

        def explain(self, request):
            self.requests.append(request)
            return _Explained()

    backend = LocalMetricFlowBackend(
        _write_manifest(tmp_path), _engine(), "duckdb", QueryLimits()
    )
    fake = _FakeMetricFlow()
    backend._metricflow_engine = lambda: fake

    sql = backend._render(SemanticQuery(metrics=["orders"], group_by=["order__status"]))
    assert sql == "select 1 as orders"
    assert len(fake.requests) == 1
    # And the dex engine is still reachable under its own name, for the one
    # billed path that needs a connection.
    assert backend._dex.store is backend._store


# ---- the injected token: reaching the layer as this request's principal -------
#
# The hosted backend used to read its token from `os.environ` and nowhere else,
# which is process-global. A host serving several end users could only make that
# work by mutating the environment per request, racing one user's token against
# another's, so per-end-user access control was not expressible on this surface at
# all. Every test here unsets the ambient sources, because one that merely passes a
# token would still pass if discovery had quietly answered instead.


@pytest.fixture
def no_ambient_token(monkeypatch, tmp_path):
    """No token discoverable anywhere: not the environment, not a home-dir file."""

    monkeypatch.delenv("DBT_SL_TOKEN", raising=False)
    monkeypatch.delenv("DBT_SL_HOST", raising=False)
    monkeypatch.delenv("DBT_SL_ENV_ID", raising=False)
    # _dbt_cloud_service_token honors this, so the ~/.dbt fallback finds nothing.
    monkeypatch.setenv("DBT_CLOUD_CONFIG_DIR", str(tmp_path / "empty"))


def _hosted_config(**overrides) -> DexConfig:
    fields = {
        "backend": "dbt_cloud",
        "host": "sl.cloud.getdbt.com",
        "environment_id": "42",
    }
    fields.update(overrides)
    return DexConfig(semantic=fields)


class _RecordingClient:
    """Stands in for `httpx.Client`, capturing what actually went on the wire.

    Patched at the httpx seam rather than at `_post`, because the claim under test
    is that the injected token is what authenticates the request, and the
    Authorization header is built inside `_post`.
    """

    posted: ClassVar[list[dict]] = []

    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def post(self, url, *, headers, json):
        _RecordingClient.posted.append({"url": url, "headers": headers, "body": json})
        return _RecordingResponse()


class _RecordingResponse:
    status_code = 200

    @staticmethod
    def json() -> dict:
        return {"data": {"metrics": []}}


@pytest.fixture
def recording_httpx(monkeypatch):
    pytest.importorskip("httpx")
    import httpx

    _RecordingClient.posted = []
    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    return _RecordingClient


def test_an_injected_token_authenticates_the_request(no_ambient_token, recording_httpx):
    """The whole point: the token a host holds per end user is what reaches dbt
    Cloud, with nothing ambient available to answer instead."""

    from exmergo_dex_core import SemanticSource

    engine = _engine(
        _hosted_config(), semantic_source=SemanticSource(token=lambda: "tok-user-1")
    )
    backend = sem.resolve_backend(engine)
    backend.list_definitions()

    assert recording_httpx.posted, "the backend never posted"
    assert recording_httpx.posted[0]["headers"]["Authorization"] == "Bearer tok-user-1"
    assert recording_httpx.posted[0]["url"] == "https://sl.cloud.getdbt.com/api/graphql"


def test_two_engines_hold_different_tokens_concurrently(
    no_ambient_token, recording_httpx
):
    """The property the process-global environment could not provide, and the
    reason this seam exists rather than a documented `os.environ` convention: two
    requests in one process reach the layer as two different principals."""

    from exmergo_dex_core import SemanticSource

    first = _engine(
        _hosted_config(), semantic_source=SemanticSource(token=lambda: "tok-alice")
    )
    second = _engine(
        _hosted_config(environment_id="99"),
        semantic_source=SemanticSource(token=lambda: "tok-bob"),
    )
    # Interleaved rather than sequential, which is the shape a web app actually has.
    alice, bob = sem.resolve_backend(first), sem.resolve_backend(second)
    bob.list_definitions()
    alice.list_definitions()

    sent = [p["headers"]["Authorization"] for p in recording_httpx.posted]
    assert sent == ["Bearer tok-bob", "Bearer tok-alice"]
    assert alice._env == "42" and bob._env == "99"


def test_the_token_callable_runs_once_per_command_not_once_per_request(
    no_ambient_token, recording_httpx
):
    """A single metric query polls dbt Cloud until the result is ready, so a token
    read per HTTP call would charge a host dozens of datastore reads for one
    question. Read once, when the backend is built."""

    from exmergo_dex_core import SemanticSource

    reads: list[int] = []

    def token() -> str:
        reads.append(1)
        return "tok-once"

    engine = _engine(_hosted_config(), semantic_source=SemanticSource(token=token))
    backend = sem.resolve_backend(engine)
    backend.list_definitions()
    backend.list_definitions()

    assert len(recording_httpx.posted) == 2
    assert reads == [1]


def test_an_injected_token_ignores_the_ambient_environment(
    recording_httpx, monkeypatch
):
    """A host holding one config per end user has already chosen the deployment. A
    single process-wide DBT_SL_HOST outranking it would redirect every tenant's
    metric query at once, which is a wrong-deployment bug that looks like working
    software."""

    from exmergo_dex_core import SemanticSource

    monkeypatch.setenv("DBT_SL_HOST", "someone-elses.cloud.getdbt.com")
    monkeypatch.setenv("DBT_SL_ENV_ID", "666")
    monkeypatch.setenv("DBT_SL_TOKEN", "ambient-token-must-not-win")

    engine = _engine(
        _hosted_config(), semantic_source=SemanticSource(token=lambda: "tok-user-1")
    )
    sem.resolve_backend(engine).list_definitions()

    posted = recording_httpx.posted[0]
    assert posted["url"] == "https://sl.cloud.getdbt.com/api/graphql"
    assert posted["headers"]["Authorization"] == "Bearer tok-user-1"
    assert "666" not in posted["body"]["query"]


def test_a_source_returning_nothing_is_refused_not_fallen_back_from(no_ambient_token):
    """Falling back to an ambient token here would reach the layer as the process
    instead of as this request's principal, which is the failure the seam exists to
    prevent, so an empty token is a refusal."""

    from exmergo_dex_core import SemanticSource

    engine = _engine(_hosted_config(), semantic_source=SemanticSource(token=lambda: ""))
    with pytest.raises(sem.SemanticBackendError, match="returned no token"):
        sem.resolve_backend(engine)


def test_missing_hosted_coordinates_name_the_config_not_the_environment(
    no_ambient_token,
):
    """The fix a host has is editing the object it passed, so telling it to export
    a variable would be wrong advice."""

    from exmergo_dex_core import SemanticSource

    engine = _engine(
        DexConfig(semantic={"backend": "dbt_cloud"}),
        semantic_source=SemanticSource(token=lambda: "tok"),
    )
    with pytest.raises(sem.SemanticBackendError) as exc:
        sem.resolve_backend(engine)
    message = str(exc.value)
    assert "DexConfig you passed" in message
    assert "export" not in message


def test_a_semantic_source_on_the_local_backend_is_refused():
    """Honored or named in an error, never accepted and dropped. A host that
    believes it supplied this request's principal, and in fact ran under whatever
    the process could discover, has lost the access control it came here for."""

    from exmergo_dex_core import SemanticSource

    engine = _engine(semantic_source=SemanticSource(token=lambda: "tok"))
    with pytest.raises(sem.SemanticBackendError, match="has no meaning for the local"):
        sem.resolve_backend(engine, local=True)


def test_a_project_less_deployment_is_told_to_use_the_hosted_backend():
    """The local backend is the default, so a deployment with no dbt project lands
    here without asking to. It used to surface a bare ValueError from
    require_repo_root, which names neither the backend that needed the project nor
    the choice available, and which `resolve_backend` promises not to raise."""

    engine = _engine()  # no repo_root: nothing on disk
    with pytest.raises(sem.SemanticBackendError) as exc:
        sem.resolve_backend(engine, local=True)
    message = str(exc.value)
    assert "needs a dbt project on disk" in message
    assert "semantic.backend: dbt_cloud" in message


def test_the_hosted_surface_needs_nothing_on_the_filesystem(
    no_ambient_token, recording_httpx, tmp_path, monkeypatch
):
    """The composition claim, asserted as a byte snapshot: config injected, token
    injected, no repo root, no store selected, no connector. A metric catalog comes
    back and the working tree is untouched."""

    from exmergo_dex_core import DexEngine, SemanticSource

    monkeypatch.chdir(tmp_path)
    before = {p.name for p in tmp_path.rglob("*")}

    with DexEngine(
        config=_hosted_config(),
        semantic_source=SemanticSource(token=lambda: "tok"),
    ) as eng:
        result = eng.semantic_list()

    assert result.catalog is not None
    assert {p.name for p in tmp_path.rglob("*")} == before
    assert not (tmp_path / ".dex").exists()
