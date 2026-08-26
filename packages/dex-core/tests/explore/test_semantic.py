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
from pydantic import ValidationError

from exmergo_dex_core import envelope as env
from exmergo_dex_core.adapters.project import DbtProject
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
from exmergo_dex_core.explore.results import SemanticQueryResult
from exmergo_dex_core.explore.semantic import (
    SemanticQuery,
    cap_columnar,
    requested_dimension_refs,
    screen_dimension_refs,
)
from exmergo_dex_core.explore.semantic import commands as semantic_commands
from exmergo_dex_core.explore.semantic.local import (
    LocalMetricFlowBackend,
    _unprofiled_note,
)
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.storage import MemoryStore


def _engine(config: DexConfig | None = None, **kwargs) -> DexEngine:
    """A real engine, never opened. The backends read config and store off it and
    route their one billed path through it, so a stand-in would test the stand-in."""

    return DexEngine(config=config or DexConfig(), store=MemoryStore(), **kwargs)


def _local(project: Path, engine: DexEngine | None = None, connector: str = "duckdb"):
    """The local backend with a project format injected, as `from_engine` wires it.

    Injected rather than discovered so the read is exercised without a repo root
    or a chdir: the backend reads the catalog and its PII column map through the
    project seam now, and a real `DbtProject` on the other side is what makes that
    a test of the seam rather than of a stand-in.
    """

    return LocalMetricFlowBackend(
        project,
        engine or _engine(),
        connector,
        QueryLimits(),
        DbtProject(project.parent, project),
    )


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


def test_query_accepts_comma_joined_name_lists():
    # Issue #135: `--group-by a,b` is the natural first guess and a common CLI
    # convention; the repeated flag was the only form that worked. Normalizing on
    # the query object covers both backends and a library caller at once.
    q = SemanticQuery(
        metrics=["sessions,queries"],
        group_by=["user__pricing_tier, metric_time", "session__mode"],
        order_by=["-sessions,metric_time"],
    )
    assert q.metrics == ["sessions", "queries"]
    assert q.group_by == ["user__pricing_tier", "metric_time", "session__mode"]
    assert q.order_by == ["-sessions", "metric_time"]


def test_cli_group_by_reaches_the_backend_split(monkeypatch):
    """`--group-by a,b` and `--group-by a --group-by b` must arrive identically.

    Through the real parser and the real command handler, because the wiring is
    what issue #135 was about: the flag stays `action="append"` and the splitting
    happens on the query object, so both spellings and their mixture converge.
    """

    from exmergo_dex_core.cli import _build_parser

    seen: list[SemanticQuery] = []

    class _Recorder:
        name = "local"

        def query(self, q):
            seen.append(q)
            return SemanticQueryResult(backend="local")

    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: _Recorder())
    parser = _build_parser()
    for flags in (
        ["--group-by", "user__pricing_tier,metric_time"],
        ["--group-by", "user__pricing_tier", "--group-by", "metric_time"],
    ):
        args = parser.parse_args(
            ["explore", "semantic", "query", "--metric", "sessions", "--local", *flags]
        )
        semantic_commands.cmd_semantic(args, _engine())
    assert [q.group_by for q in seen] == [["user__pricing_tier", "metric_time"]] * 2


def test_cli_semantic_query_accepts_positional_metrics_and_keeps_the_flag(monkeypatch):
    from exmergo_dex_core.cli import _build_parser

    seen: list[SemanticQuery] = []

    class _Recorder:
        name = "local"

        def query(self, q):
            seen.append(q)
            return SemanticQueryResult(backend="local")

    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: _Recorder())
    parser = _build_parser()
    for metric_args in (
        ["sessions"],
        ["--metric", "sessions"],
        ["sessions", "--metric", "queries"],
    ):
        args = parser.parse_args(
            ["explore", "semantic", "query", *metric_args, "--local"]
        )
        semantic_commands.cmd_semantic(args, _engine())

    assert [query.metrics for query in seen] == [
        ["sessions"],
        ["sessions"],
        ["sessions", "queries"],
    ]


def test_cli_semantic_metric_without_explicit_query_mode_is_rejected():
    from exmergo_dex_core.cli import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["explore", "semantic", "sessions"])


def test_query_leaves_where_clauses_alone():
    # A Jinja filter carries commas of its own, so the split that helps names would
    # cut a clause in half. This is the one list that must never be normalized.
    clause = "{{ TimeDimension('metric_time', 'month') }} > '2020-01-01'"
    assert SemanticQuery(metrics=["m"], where=[clause]).where == [clause]


def test_query_drops_empty_name_tokens():
    q = SemanticQuery(metrics=["m"], group_by=["a,, b ", "  ", ""])
    assert q.group_by == ["a", "b"]


def test_a_metric_list_that_normalizes_to_nothing_is_refused():
    # `--metric ,` is as empty as no flag at all, and the refusal must not depend
    # on which backend would have answered.
    from exmergo_dex_core.explore.semantic.commands import semantic_query

    with pytest.raises(sem.SemanticBackendError, match="at least one metric"):
        semantic_query(_engine(), [","], api=True)


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


def test_resolve_backend_reads_the_deployment_axis(monkeypatch):
    # `backend` collapsed vendor and deployment into one enum. Both spellings
    # select, because the released one keeps working and the new one is what a
    # second vendor would extend.
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
    by_deployment = _engine(DexConfig(semantic={"deployment": "dbt_cloud"}))
    assert sem.resolve_backend(by_deployment) == "HOSTED"
    assert sem.resolve_backend(by_deployment, local=True) == "LOCAL"
    spelled_out = _engine(DexConfig(semantic={"vendor": "dbt", "deployment": "local"}))
    assert sem.resolve_backend(spelled_out) == "LOCAL"
    assert sem.resolve_backend(spelled_out, api=True) == "HOSTED"


def test_a_semantic_vendor_dex_does_not_ship_is_refused():
    # Refused at the config seam, by name, rather than resolving to whatever
    # happens to be the default backend.
    with pytest.raises(ValidationError, match=r"semantic\.vendor"):
        DexConfig(semantic={"vendor": "cube"})


def test_the_backend_declares_who_executes():
    # The axis the guards read. `execution` is derived from the backend, never
    # configured, because it decides whether the cost guard can apply at all.
    from exmergo_dex_core.explore.semantic.hosted import HostedDbtCloudBackend
    from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend

    assert HostedDbtCloudBackend.execution == sem.EXECUTION_VENDOR
    assert LocalMetricFlowBackend.execution == sem.EXECUTION_DEX
    cost, warnings = sem.cost_posture(HostedDbtCloudBackend)
    assert cost.paradigm == env.Paradigm.HOSTED
    assert cost.estimate is None and cost.ceiling is None
    assert any("cost guard unavailable" in w for w in warnings)
    # A dex-executed backend gets its cost from the adapter, not from here.
    assert sem.cost_posture(LocalMetricFlowBackend) == (None, [])


# ---- hosted backend (fake transport) ----------------------------------------


def _viz_like_metrics():
    return [
        {
            "name": "sessions",
            "type": "SIMPLE",
            "label": "Sessions",
            "description": "Total sessions.",
            "dimensions": [
                # metric_time stays bare: a real deployment populates these
                # fields unevenly, and the unpopulated case is half the contract.
                {"name": "metric_time", "type": "TIME"},
                {
                    "name": "user__pricing_tier",
                    "type": "CATEGORICAL",
                    "label": "Pricing tier",
                    "description": "The plan the user is billed on.",
                },
            ],
            # No `label` key here, and none in the selection set: the API's
            # Entity type has no such field.
            "entities": [
                {"name": "user", "type": "PRIMARY", "description": "A viz user."}
            ],
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


def test_hosted_list_carries_the_projects_own_words():
    catalog = FakeHostedBackend(metrics=_viz_like_metrics()).list_definitions()
    tier = next(d for d in catalog.dimensions if d.name == "user__pricing_tier")
    assert tier.label == "Pricing tier"
    assert tier.description == "The plan the user is billed on."
    # A dimension the project never described says nothing, rather than guessing.
    assert next(d for d in catalog.dimensions if d.name == "metric_time").label is None
    user = next(e for e in catalog.entities if e.name == "user")
    assert user.description == "A viz user."
    # Structurally unavailable over GraphQL, and the catalog says so rather than
    # letting the absence read as an undeclared label.
    assert user.label is None
    assert any("no label on entities" in note for note in catalog.notes)


def test_hosted_catalog_query_never_asks_for_an_entity_label():
    """`label` on an Entity is not a missing field, it is a rejected query.

    dbt Cloud answers `Cannot query field 'label' on type 'Entity'` and fails the
    whole request, so the widened selection set takes the metrics list down with
    it if anyone adds one.
    """

    backend = FakeHostedBackend(metrics=_viz_like_metrics())
    backend.list_definitions()
    posted = backend.posted[0]
    assert "label" not in posted.split("entities {")[1]
    # Every other field on the widened set was verified against the live schema by
    # introspection before it was written, which is the only way to add one.
    assert "entities { name type description expr role semanticModel { name } }" in (
        posted
    )
    assert "dimensions { name type label description semanticModel { name } }" in (
        posted
    )
    assert "measures { name agg expr aggTimeDimension }" in posted


def test_hosted_list_keeps_the_first_non_null_field_not_the_first_metric():
    """A dimension is nested under every metric that can group by it, and the
    copies need not agree. Under a whole-element `setdefault`, whichever metric
    sorted first blanked out text another one carried."""

    metrics = [
        {
            "name": "aaa_sessions",
            "type": "SIMPLE",
            "dimensions": [{"name": "user__pricing_tier", "type": "CATEGORICAL"}],
            "entities": [{"name": "user", "type": "PRIMARY"}],
        },
        {
            "name": "zzz_queries",
            "type": "SIMPLE",
            "dimensions": [
                {
                    "name": "user__pricing_tier",
                    "type": "CATEGORICAL",
                    "description": "The plan the user is billed on.",
                }
            ],
            "entities": [
                {"name": "user", "type": "PRIMARY", "description": "A viz user."}
            ],
        },
    ]
    catalog = FakeHostedBackend(metrics=metrics).list_definitions()
    tier = next(d for d in catalog.dimensions if d.name == "user__pricing_tier")
    assert tier.description == "The plan the user is billed on."
    assert next(e for e in catalog.entities if e.name == "user").description == (
        "A viz user."
    )


def test_hosted_list_without_entities_says_nothing_about_entity_labels():
    metrics = [{"name": "sessions", "type": "SIMPLE", "dimensions": [], "entities": []}]
    catalog = FakeHostedBackend(metrics=metrics).list_definitions()
    assert catalog.entities == []
    assert not any("label on entities" in note for note in catalog.notes)
    # The gap itself is still declared, because it is a property of this backend
    # rather than of this layer: a consumer branching on it must not have to see
    # an entity first to learn that an entity here cannot carry a label.
    assert catalog.unavailable["entities"] == ["label"]


def test_semantic_list_payload_omits_what_the_project_never_set(monkeypatch):
    """An unset optional field is absent from the payload, never a null.

    One rule across metrics, dimensions and entities: a catalog is agent context,
    and a project that documents nothing would otherwise pay kilobytes of
    placeholders per list. An empty list survives, because "no groupable
    dimensions" is an answer where a null is not.
    """

    metrics = [
        *_viz_like_metrics(),
        {"name": "queries", "type": "SIMPLE", "dimensions": [], "entities": []},
    ]
    backend = FakeHostedBackend(metrics=metrics)
    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    envelope = to_envelope(semantic_commands.semantic_list(_engine()))

    dims = {d["name"]: d for d in envelope.data["dimensions"]}
    assert dims["user__pricing_tier"]["label"] == "Pricing tier"
    assert set(dims["metric_time"]) == {"name", "type"}

    entities = {e["name"]: e for e in envelope.data["entities"]}
    assert entities["user"]["description"] == "A viz user."
    assert "label" not in entities["user"]

    listed = {m["name"]: m for m in envelope.data["metrics"]}
    assert listed["sessions"]["label"] == "Sessions"
    assert "label" not in listed["queries"] and "description" not in listed["queries"]
    assert listed["queries"]["dimensions"] == []

    # The catalog's own note reaches the caller once, at envelope level.
    assert any("no label on entities" in note for note in envelope.data["notes"])


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


def test_hosted_discloses_name_only_screening():
    # The layer's own config.meta is what makes the hosted gate authoritative, the
    # way a column profile does locally. Where it says nothing, only the name
    # heuristic ran, and the result has to say so rather than letting the weaker
    # screening pass for the stronger one.
    backend = FakeHostedBackend(
        result=table_json_result(["sessions"], ["string"], [[5.0]]),
        dimensions_meta=[
            {"name": "user__pricing_tier", "config": {"meta": {"pii": False}}}
        ],
    )
    result = backend.query(
        SemanticQuery(
            metrics=["sessions"], group_by=["user__pricing_tier", "session__mode"]
        )
    )
    note = next(n for n in result.notes if "name heuristic" in n)
    # adjudicated by the layer, so absent; unknown to the layer, so named
    assert "user__pricing_tier" not in note
    assert "session__mode" in note
    assert "meta: {pii: true}" in note


def test_hosted_says_nothing_when_the_layer_adjudicated_everything():
    backend = FakeHostedBackend(
        result=table_json_result(["sessions"], ["string"], [[5.0]]),
        dimensions_meta=[
            {"name": "user__pricing_tier", "config": {"meta": {"pii": False}}}
        ],
    )
    result = backend.query(
        SemanticQuery(metrics=["sessions"], group_by=["user__pricing_tier"])
    )
    assert not any("name heuristic" in n for n in result.notes)


def test_hosted_discloses_a_metadata_call_that_never_answered():
    # A failed metadata call degrades to the name heuristic for every ref. That was
    # silent, which is the one direction a PII posture must never fail quietly in;
    # it is also a different fix from a layer that answered and knows nothing.
    class _NoMetadata(FakeHostedBackend):
        def _post(self, query: str) -> dict:
            if "dimensions(environmentId" in query:
                raise sem.SemanticBackendError("dbt Cloud did not answer")
            return super()._post(query)

    backend = _NoMetadata(result=table_json_result(["sessions"], ["string"], [[5.0]]))
    result = backend.query(
        SemanticQuery(metrics=["sessions"], group_by=["user__pricing_tier"])
    )
    note = next(n for n in result.notes if "name heuristic" in n)
    assert "did not answer" in note
    # and the gate still bound on the way there, on the heuristic alone
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        backend.query(SemanticQuery(metrics=["sessions"], group_by=["user__email"]))


def test_both_payloads_name_the_axes_the_backend_enum_collapsed():
    # A caller reading a result should not have to know that "dbt_cloud" implies
    # "the vendor executed this, so no cost guard applied". `backend` stays for
    # compatibility; the three axes say it outright.
    backend = FakeHostedBackend(
        metrics=[
            {"name": "sessions", "type": "SIMPLE", "dimensions": [], "entities": []}
        ],
        result=table_json_result(["sessions"], ["number"], [[5.0]]),
    )
    catalog = backend.list_definitions().to_data()
    assert catalog["backend"] == "dbt_cloud"
    assert catalog["vendor"] == "dbt"
    assert catalog["deployment"] == "dbt_cloud"
    assert catalog["execution"] == sem.EXECUTION_VENDOR

    payload = backend.query(SemanticQuery(metrics=["sessions"])).data()
    assert payload["backend"] == "dbt_cloud"
    assert payload["execution"] == sem.EXECUTION_VENDOR


def test_hosted_metadata_is_the_union_across_a_query_s_metrics():
    # `dimensions(metrics: [a, b])` returns the dimensions common to both, so
    # asking once for a multi-metric query returns the intersection and the
    # authoritative map shrinks as the query grows. One aliased field per metric
    # is the union, which is what the gate has to screen against.
    backend = FakeHostedBackend(
        dimensions_meta={
            "sessions": [{"name": "session__mode", "config": {"meta": {}}}],
            "agent_runs": [{"name": "agent__mode", "config": {"meta": {}}}],
        }
    )
    meta = backend._dimension_meta(["sessions", "agent_runs"])
    assert set(meta) == {"session__mode", "agent__mode"}


def test_hosted_asks_the_layer_once_however_many_metrics_it_asks_about():
    # Aliases, not N calls: the union costs one round trip on a path that already
    # pays for createQuery and a poll loop, so nothing here needs concurrency.
    backend = FakeHostedBackend(
        dimensions_meta={"sessions": [], "agent_runs": [], "chat_turns": []}
    )
    backend._dimension_meta(["sessions", "agent_runs", "chat_turns"])
    assert len(backend.posted) == 1
    assert backend.posted[0].count("dimensions(environmentId") == 3


def test_hosted_metadata_asks_once_per_distinct_metric():
    backend = FakeHostedBackend(dimensions_meta={"sessions": []})
    backend._dimension_meta(["sessions", "sessions"])
    assert backend.posted[0].count("dimensions(environmentId") == 1


def test_hosted_metadata_lets_pii_win_where_two_metrics_disagree():
    # Same dimension, two metrics, contradictory metadata. The merge has one job
    # in that case and it is not to be even-handed.
    backend = FakeHostedBackend(
        dimensions_meta={
            "sessions": [{"name": "user__handle", "config": {"meta": {"pii": False}}}],
            "agent_runs": [{"name": "user__handle", "config": {"meta": {"pii": True}}}],
        }
    )
    meta = backend._dimension_meta(["sessions", "agent_runs"])
    assert meta["user__handle"] == {"pii": True}
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        backend.query(
            SemanticQuery(metrics=["sessions", "agent_runs"], group_by=["user__handle"])
        )


def test_hosted_metadata_says_nothing_never_displaces_a_flag():
    # The reverse order, and the null config a synthesized dimension returns.
    backend = FakeHostedBackend(
        dimensions_meta={
            "sessions": [{"name": "user__handle", "config": {"meta": {"pii": True}}}],
            "agent_runs": [{"name": "user__handle", "config": None}],
        }
    )
    assert backend._dimension_meta(["sessions", "agent_runs"])["user__handle"] == {
        "pii": True
    }


def test_hosted_screens_a_grain_suffixed_dimension_against_the_layer():
    # A group-by token may carry a time grain that no dimension name has, so a
    # flagged time dimension would otherwise need only `__month` on the end to
    # drop to the name heuristic.
    backend = FakeHostedBackend(
        dimensions_meta={
            "sessions": [
                {"name": "user__signed_up_at", "config": {"meta": {"pii": True}}}
            ]
        }
    )
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        backend.query(
            SemanticQuery(metrics=["sessions"], group_by=["user__signed_up_at__month"])
        )
    assert not any("createQuery" in posted for posted in backend.posted)


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
    backend = _local(_write_manifest(tmp_path))
    catalog = backend.list_definitions()
    assert catalog.backend == "local"
    orders = next(m for m in catalog.metrics if m.name == "orders")
    assert "order__status" in orders.dimensions
    assert "metric_time" in orders.dimensions
    assert any(e.name == "order" for e in catalog.entities)


def _described_manifest(tmp_path: Path) -> Path:
    """Two semantic models, unevenly documented, which is how a real project
    looks: one dimension labelled and described and one bare, and an entity whose
    only description lives in the second model that declares it."""

    project = tmp_path / "described"
    (project / "target").mkdir(parents=True)
    manifest = {
        "semantic_models": [
            {
                "name": "orders",
                "entities": [
                    {
                        "name": "order",
                        "type": "primary",
                        "label": "Order",
                        "description": "One placed order.",
                    },
                    {"name": "customer", "type": "foreign"},
                ],
                "dimensions": [
                    {
                        "name": "status",
                        "type": "categorical",
                        "label": "Order status",
                        "description": "Where the order is in fulfilment.",
                    },
                    {"name": "channel", "type": "categorical"},
                ],
                "measures": [{"name": "order_count", "agg": "count"}],
            },
            {
                "name": "payments",
                "entities": [
                    {
                        "name": "customer",
                        "type": "foreign",
                        "description": "A paying customer.",
                    }
                ],
                "dimensions": [],
                "measures": [],
            },
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


def test_local_list_carries_the_projects_own_words(tmp_path: Path):
    backend = _local(_described_manifest(tmp_path))
    catalog = backend.list_definitions()

    status = next(d for d in catalog.dimensions if d.name == "order__status")
    # The catalog key is entity-qualified; the label and description are the
    # project's own text about the dimension, unqualified.
    assert status.label == "Order status"
    assert status.description == "Where the order is in fulfilment."
    channel = next(d for d in catalog.dimensions if d.name == "order__channel")
    assert channel.label is None and channel.description is None

    order = next(e for e in catalog.entities if e.name == "order")
    # Unlike the hosted path, the manifest declares entity labels and dex reads them.
    assert order.label == "Order"
    assert order.description == "One placed order."
    # Declared in both models and described in only the second: the first
    # declaration must not blank it out.
    customer = next(e for e in catalog.entities if e.name == "customer")
    assert customer.description == "A paying customer."


def test_local_list_invents_no_words_for_metric_time(tmp_path: Path):
    """`metric_time` is dex's own synthesis, not a manifest entry. Everything
    described in the catalog is described by the dbt project."""

    backend = _local(_described_manifest(tmp_path))
    metric_time = next(
        d for d in backend.list_definitions().dimensions if d.name == "metric_time"
    )
    assert metric_time.type == "time"
    assert metric_time.label is None and metric_time.description is None


def test_local_list_missing_manifest_errors(tmp_path: Path):
    backend = _local(tmp_path)
    with pytest.raises(sem.SemanticBackendError):
        backend.list_definitions()


def test_local_query_pii_gate_blocks_before_render(tmp_path: Path):
    # No manifest and no metricflow needed: the PII gate runs before rendering.
    backend = LocalMetricFlowBackend(tmp_path, _engine(), "duckdb", QueryLimits())
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        backend.query(SemanticQuery(metrics=["orders"], group_by=["customer__email"]))


# ---- the object model: semantic models, measures, composition, entity roles ---
#
# One layer, expressed twice, so both backends can be held to the same ground
# truth. The shape that matters is an entity declared in more than one semantic
# model with a different type and a different join key in each, because that is
# the fact a single flat record cannot carry and the one both backends used to get
# wrong (in opposite directions, on the same layer).


def _graph_metrics():
    """A hosted catalog over two semantic models: a shared entity, a ratio metric
    spanning both, a filtered metric, and a cumulative one."""

    order_entity = {
        "name": "order",
        "type": "PRIMARY",
        "expr": "order_key",
        "description": "The order this row is about.",
        "semanticModel": {"name": "orders"},
    }
    order_as_foreign = {
        "name": "order",
        "type": "FOREIGN",
        "expr": "parent_order_id",
        "description": "Nullable on refunds booked without an order.",
        "semanticModel": {"name": "refunds"},
    }
    order_status = {
        "name": "order__status",
        "type": "CATEGORICAL",
        "label": "Order status",
        "semanticModel": {"name": "orders"},
    }
    refund_reason = {
        "name": "order__refund__reason",
        "type": "CATEGORICAL",
        "semanticModel": {"name": "refunds"},
    }
    return [
        {
            "name": "orders",
            "type": "SIMPLE",
            "dimensions": [{"name": "metric_time", "type": "TIME"}, order_status],
            "entities": [order_entity],
            "measures": [
                {
                    "name": "order_count",
                    "agg": "SUM",
                    "expr": "CASE WHEN order_key IS NOT NULL THEN 1 ELSE 0 END",
                    "aggTimeDimension": "ordered_at",
                }
            ],
            "semanticModels": [{"name": "orders"}],
            "typeParams": {
                "measure": {"name": "order_count"},
                "inputMeasures": [{"name": "order_count"}],
            },
        },
        {
            "name": "refund_rate",
            "type": "RATIO",
            "dimensions": [order_status, refund_reason],
            "entities": [order_entity, order_as_foreign],
            "measures": [
                {"name": "order_count", "agg": "SUM"},
                {"name": "refund_count", "agg": "SUM"},
            ],
            "semanticModels": [{"name": "orders"}, {"name": "refunds"}],
            "typeParams": {
                "numerator": {"name": "refunds"},
                "denominator": {"name": "orders"},
                "inputMeasures": [
                    {"name": "refund_count"},
                    {"name": "order_count"},
                ],
            },
        },
        {
            "name": "paid_orders",
            "type": "SIMPLE",
            "dimensions": [order_status],
            "entities": [order_entity],
            "measures": [{"name": "order_count", "agg": "SUM"}],
            "semanticModels": [{"name": "orders"}],
            "filter": {"whereSqlTemplate": "{{ Dimension('order__status') }} = 'paid'"},
            "typeParams": {"inputMeasures": [{"name": "order_count"}]},
        },
        {
            "name": "orders_to_date",
            "type": "CUMULATIVE",
            "dimensions": [order_status],
            "entities": [order_entity],
            "measures": [{"name": "order_count", "agg": "SUM"}],
            "semanticModels": [{"name": "orders"}],
            "typeParams": {
                "inputMeasures": [{"name": "order_count"}],
                "window": {"count": 7, "granularity": "DAY"},
                "grainToDate": "MONTH",
            },
        },
    ]


def _graph_manifest(tmp_path: Path) -> Path:
    """The same layer as a compiled semantic manifest."""

    project = tmp_path / "graph"
    (project / "target").mkdir(parents=True, exist_ok=True)
    manifest = {
        "semantic_models": [
            {
                "name": "orders",
                "label": "Orders",
                "description": "One row per order.",
                "defaults": {"agg_time_dimension": "ordered_at"},
                "node_relation": {
                    "alias": "fct_orders",
                    "relation_name": '"wh"."main"."fct_orders"',
                },
                "entities": [
                    {
                        "name": "order",
                        "type": "primary",
                        "expr": "order_key",
                        "label": "Order",
                        "description": "The order this row is about.",
                    }
                ],
                "dimensions": [
                    {"name": "status", "type": "categorical", "label": "Order status"},
                    {"name": "ordered_at", "type": "time"},
                ],
                "measures": [
                    {
                        "name": "order_count",
                        "agg": "count",
                        "expr": "order_key",
                        "label": "Orders",
                    }
                ],
            },
            {
                "name": "refunds",
                "description": "One row per refund.",
                "defaults": {"agg_time_dimension": "refunded_at"},
                "node_relation": {
                    "alias": "fct_refunds",
                    "relation_name": '"wh"."main"."fct_refunds"',
                },
                "entities": [
                    {
                        "name": "refund",
                        "type": "primary",
                        "expr": "refund_key",
                    },
                    {
                        "name": "order",
                        "type": "foreign",
                        "expr": "parent_order_id",
                        "description": ("Nullable on refunds booked without an order."),
                    },
                ],
                "dimensions": [{"name": "reason", "type": "categorical"}],
                "measures": [{"name": "refund_count", "agg": "count"}],
            },
        ],
        "metrics": [
            {
                "name": "orders",
                "type": "simple",
                "type_params": {
                    "measure": {"name": "order_count"},
                    "input_measures": [{"name": "order_count"}],
                },
            },
            {
                "name": "refund_rate",
                "type": "ratio",
                "type_params": {
                    "numerator": {"name": "refunds"},
                    "denominator": {"name": "orders"},
                    "input_measures": [
                        {"name": "refund_count"},
                        {"name": "order_count"},
                    ],
                },
            },
            {
                "name": "paid_orders",
                "type": "simple",
                "filter": {
                    "where_filters": [
                        {
                            "where_sql_template": (
                                "{{ Dimension('order__status') }} = 'paid'"
                            )
                        }
                    ]
                },
                "type_params": {"input_measures": [{"name": "order_count"}]},
            },
            {
                "name": "orders_to_date",
                "type": "cumulative",
                "type_params": {
                    "input_measures": [{"name": "order_count"}],
                    "window": {"count": 7, "granularity": "day"},
                    "grain_to_date": "month",
                },
            },
        ],
    }
    (project / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))
    return project


def _catalogs(tmp_path: Path):
    """The same layer from both backends, for the assertions that must hold on
    either. Parametrizing gives two failures for one cause; a pair keeps the
    ground truth stated once."""

    return {
        "local": _local(_graph_manifest(tmp_path)).list_definitions(),
        "hosted": FakeHostedBackend(metrics=_graph_metrics()).list_definitions(),
    }


def test_an_entity_carries_every_declaration_and_a_derived_type(tmp_path: Path):
    """The bug this replaces: `type` was folded to whichever copy came first.

    `order` is primary in the model it keys and foreign in the one that joins to
    it. Collapsing that reported iteration order, and the two backends reported
    opposite answers for the same layer. Now every declaration survives and the
    single `type` is derived, so both backends agree and both are right.
    """

    for name, catalog in _catalogs(tmp_path).items():
        order = next(e for e in catalog.entities if e.name == "order")
        assert order.type == "primary", name
        roles = {r.semantic_model: r for r in order.roles}
        assert set(roles) == {"orders", "refunds"}, name
        assert roles["orders"].type == "primary", name
        assert roles["refunds"].type == "foreign", name
        # The join key differs per model for the same entity, which is the whole
        # reason a single record cannot carry it.
        assert roles["orders"].expr == "order_key", name
        assert roles["refunds"].expr == "parent_order_id", name
        # And each declaration keeps its own words: the refunds one documents a
        # nullable key, which is exactly the caveat the old merge discarded.
        assert "Nullable" in roles["refunds"].description, name


def test_a_semantic_model_is_an_object_with_the_hosted_gap_declared(tmp_path: Path):
    catalogs = _catalogs(tmp_path)
    for name, catalog in catalogs.items():
        assert [m.name for m in catalog.semantic_models] == ["orders", "refunds"], name

    local = next(m for m in catalogs["local"].semantic_models if m.name == "orders")
    assert local.label == "Orders"
    assert local.model_ref == "fct_orders"
    assert local.agg_time_dimension == "ordered_at"
    assert local.primary_entity == "order"

    # The hosted `SemanticModel` type carries only a name, so the absence is a
    # property of the path and is declared rather than left to look like a project
    # that documented nothing.
    hosted = next(m for m in catalogs["hosted"].semantic_models if m.name == "orders")
    assert hosted.label is None and hosted.model_ref is None
    assert "label" in catalogs["hosted"].unavailable["semantic_models"]
    assert "model_ref" in catalogs["hosted"].unavailable["semantic_models"]
    assert catalogs["local"].unavailable == {}


def test_measures_carry_the_aggregation_behind_the_number(tmp_path: Path):
    catalogs = _catalogs(tmp_path)
    for name, catalog in catalogs.items():
        assert {m.name for m in catalog.measures} == {"order_count", "refund_count"}, (
            name
        )
        order_count = next(m for m in catalog.measures if m.name == "order_count")
        assert order_count.agg
        assert order_count.semantic_model == "orders", name
        # A measure with no time dimension of its own resolves to its model's
        # default, which is the column a time grouping actually aggregates by.
        assert order_count.agg_time_dimension == "ordered_at", name

    # The expression is the point on a conditional measure: `agg` alone would read
    # as a plain count of rows.
    assert (
        "CASE WHEN"
        in next(m for m in catalogs["hosted"].measures if m.name == "order_count").expr
    )
    assert catalogs["local"].unavailable == {}
    assert catalogs["hosted"].unavailable["measures"] == ["label", "description"]


def test_a_ratio_metric_carries_both_of_its_sides(tmp_path: Path):
    for name, catalog in _catalogs(tmp_path).items():
        ratio = next(m for m in catalog.metrics if m.name == "refund_rate")
        assert ratio.composition.numerator == "refunds", name
        assert ratio.composition.denominator == "orders", name
        assert set(ratio.input_measures) == {"order_count", "refund_count"}, name
        # Both sides come from different semantic models, which is what decides
        # whether a given group-by is valid on both of them.
        assert ratio.semantic_models == ["orders", "refunds"], name


def test_a_filtered_metric_discloses_that_it_measures_a_subset(tmp_path: Path):
    for name, catalog in _catalogs(tmp_path).items():
        paid = next(m for m in catalog.metrics if m.name == "paid_orders")
        assert paid.filter == "{{ Dimension('order__status') }} = 'paid'", name


def test_metricflow_only_detail_stays_under_the_vendor_key(tmp_path: Path):
    """A cumulative window is real and is this vendor's semantics.

    Promoting it into the neutral core would make the shared shape mean something
    only one format can fill; leaving it out would drop a fact a caller needs. One
    declared key is the third option.
    """

    catalogs = _catalogs(tmp_path)
    for name, catalog in catalogs.items():
        cumulative = next(m for m in catalog.metrics if m.name == "orders_to_date")
        assert cumulative.vendor_params["window"]["count"] == 7, name
        assert cumulative.vendor_params["grain_to_date"], name
        payload = _element_payload(catalog, "orders_to_date")
        assert "window" not in payload, name
        assert "grain_to_date" not in payload, name

    simple = next(m for m in catalogs["local"].metrics if m.name == "orders")
    assert simple.vendor_params is None


def _element_payload(catalog, metric: str) -> dict:
    return next(m for m in catalog.to_data()["metrics"] if m["name"] == metric)


def test_a_dimension_row_says_what_kind_of_row_it_is(tmp_path: Path):
    """The 44%-divergent dimension counts, explained rather than reconciled away.

    A project read returns one row per declaration, single-hop qualified. The API
    returns one row per token a query may group by, so a dimension reached through
    a join appears once per path. Both are honest; a caller comparing counts needs
    to be told which it is holding, and `definition` is what lets it see that two
    paths reach one declaration.
    """

    catalogs = _catalogs(tmp_path)
    assert catalogs["local"].dimension_scope == "declarations"
    assert catalogs["hosted"].dimension_scope == "queryable_paths"

    status = next(d for d in catalogs["local"].dimensions if d.name == "order__status")
    assert status.definition == "status"
    assert status.semantic_model == "orders"

    # A two-hop path on the hosted side resolves to the declaration behind it.
    joined = next(
        d for d in catalogs["hosted"].dimensions if d.name == "order__refund__reason"
    )
    assert joined.definition == "reason"
    assert joined.semantic_model == "refunds"

    # dex's own synthesis points at no declaration, because there is none.
    for name, catalog in catalogs.items():
        metric_time = next(d for d in catalog.dimensions if d.name == "metric_time")
        assert metric_time.definition is None, name
        assert metric_time.semantic_model is None, name


def test_the_widened_payload_still_omits_what_was_never_set(tmp_path: Path):
    """The rule the catalog has carried since labels arrived, held across five
    element kinds rather than three: absent means unset, and nobody pays bytes for
    a null. `metric_time` is the case that proves it, being dex's own synthesis
    with nothing behind it."""

    for name, catalog in _catalogs(tmp_path).items():
        dims = {d["name"]: d for d in catalog.to_data()["dimensions"]}
        assert set(dims["metric_time"]) == {"name", "type"}, name
        simple = _element_payload(catalog, "orders")
        assert "vendor_params" not in simple, name
        # A simple metric's composition is one key, not a shell of five nulls.
        assert set(simple["composition"]) <= {"measure"}, name


def test_the_catalog_costs_one_round_trip_and_no_warehouse_query(tmp_path: Path):
    """A discovery call that priced a scan would be a discovery call nobody makes.

    Asserted rather than assumed because the temptation in every follow-on change
    is to reach for one: a value domain, a granularity, a join resolution.
    """

    hosted = FakeHostedBackend(metrics=_graph_metrics())
    hosted.list_definitions()
    assert len(hosted.posted) == 1

    engine = _engine()
    backend = _local(_graph_manifest(tmp_path), engine)
    backend.list_definitions()
    # No adapter was ever built, so nothing could have been estimated or run.
    assert engine._adapter_instance is None


def test_the_local_catalog_is_read_through_the_project_seam(tmp_path: Path):
    """The tier is defined and must be load-bearing.

    The local backend used to call a private function on the dbt module and parse
    the compiled artifact itself, which hardwired the read to dbt while a
    format-neutral seam sat unused beside it. This is the control that keeps it
    routed: a project that records its calls must see the catalog asked for.
    """

    from exmergo_dex_core.semantic_catalog import SemanticCatalogView

    class _Recorded:
        name = "recorded"
        calls: ClassVar[list[str]] = []

        def definitions(self):
            self.calls.append("definitions")
            raise AssertionError("the catalog read must not go through tier 1")

        def semantic_catalog(self):
            self.calls.append("semantic_catalog")
            return SemanticCatalogView(notes=["from the format"])

    project = _Recorded()
    backend = LocalMetricFlowBackend(
        tmp_path, _engine(), "duckdb", QueryLimits(), project
    )
    catalog = backend.list_definitions()
    assert project.calls == ["semantic_catalog"]
    # The format's own note travels with the value, ahead of the backend's.
    assert catalog.notes[0] == "from the format"


def test_a_format_that_reads_no_semantic_layer_is_refused_by_name(tmp_path: Path):
    """Not an empty catalog, which would read as a layer with nothing in it."""

    class _NoSemantics:
        name = "graph"

        def definitions(self):
            return None

    backend = LocalMetricFlowBackend(
        tmp_path, _engine(), "duckdb", QueryLimits(), _NoSemantics()
    )
    with pytest.raises(sem.SemanticBackendError, match="semantic_catalog"):
        backend.list_definitions()


def test_an_uncompiled_project_is_told_what_to_run(tmp_path: Path):
    (tmp_path / "target").mkdir()
    with pytest.raises(sem.SemanticBackendError, match="dbt parse"):
        _local(tmp_path).list_definitions()


# ---- scoping the catalog to the metrics a caller came for --------------------


def test_scoping_keeps_only_what_the_named_metrics_reach(tmp_path: Path):
    """Discovery on a large layer is one payload, and most of it is about
    something else. Widening every element made that worse, so the scope arrives
    with it rather than after."""

    backend = _local(_graph_manifest(tmp_path))
    catalog, unknown = backend.list_definitions().narrowed_to(["orders"])
    assert unknown == []
    assert [m.name for m in catalog.metrics] == ["orders"]
    assert [m.name for m in catalog.semantic_models] == ["orders"]
    assert [m.name for m in catalog.measures] == ["order_count"]
    assert all(d.semantic_model in {"orders", None} for d in catalog.dimensions)
    # refund_rate spans both models, so scoping to it keeps both.
    both, _ = backend.list_definitions().narrowed_to(["refund_rate"])
    assert [m.name for m in both.semantic_models] == ["orders", "refunds"]


def test_scoping_keeps_an_entitys_declarations_whole(tmp_path: Path):
    """Pruning roles to the scope would turn a primary entity into a foreign one,
    which is a false statement about the layer rather than a smaller one."""

    catalog, _ = (
        _local(_graph_manifest(tmp_path)).list_definitions().narrowed_to(["orders"])
    )
    order = next(e for e in catalog.entities if e.name == "order")
    assert order.type == "primary"
    assert {r.semantic_model for r in order.roles} == {"orders", "refunds"}


def test_a_scoped_catalog_says_so_in_the_payload(tmp_path: Path, monkeypatch):
    backend = _local(_graph_manifest(tmp_path))
    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    envelope = to_envelope(
        semantic_commands.semantic_list(_engine(), metrics=["orders"])
    )
    assert envelope.data["scoped_to"] == ["orders"]
    assert len(envelope.data["metrics"]) == 1
    assert any("scoped to 1 of 4 metrics" in note for note in envelope.data["notes"])
    # And an unscoped catalog does not carry the key at all, so a complete answer
    # is never shaped like a subset.
    whole = to_envelope(semantic_commands.semantic_list(_engine()))
    assert "scoped_to" not in whole.data


def test_cli_semantic_list_scopes_through_every_spelling(tmp_path: Path, monkeypatch):
    """Through the real parser, because the wiring is the whole question here.

    `--metric` and the positional metrics were declared on this subparser for
    `query`, so scoping `list` needed no new flag; what it needed was the command
    shim reading them in list mode too, and nothing about the parser shows whether
    it does. Bare `explore semantic` reaches list by default, so that spelling is
    checked as well.
    """

    from exmergo_dex_core.cli import _build_parser

    backend = _local(_graph_manifest(tmp_path))
    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    parser = _build_parser()

    for argv in (
        ["explore", "semantic", "list", "--local", "--metric", "orders"],
        ["explore", "semantic", "--local", "--metric", "orders"],
        ["explore", "semantic", "list", "orders", "--local"],
    ):
        envelope = semantic_commands.cmd_semantic(parser.parse_args(argv), _engine())
        assert envelope.data["scoped_to"] == ["orders"], argv
        assert len(envelope.data["metrics"]) == 1, argv

    both = semantic_commands.cmd_semantic(
        parser.parse_args(
            ["explore", "semantic", "list", "--local", "--metric", "orders,paid_orders"]
        ),
        _engine(),
    )
    assert both.data["scoped_to"] == ["orders", "paid_orders"]

    # And an unscoped list is still the whole layer, with no scope key at all.
    whole = semantic_commands.cmd_semantic(
        parser.parse_args(["explore", "semantic", "list", "--local"]), _engine()
    )
    assert "scoped_to" not in whole.data
    assert len(whole.data["metrics"]) == 4


def test_a_misspelled_metric_is_refused_rather_than_returning_nothing(
    tmp_path: Path, monkeypatch
):
    backend = _local(_graph_manifest(tmp_path))
    monkeypatch.setattr(sem, "resolve_backend", lambda *a, **k: backend)
    with pytest.raises(sem.SemanticBackendError, match="no such metric"):
        semantic_commands.semantic_list(_engine(), metrics=["ordrs"])


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
    backend = _local(_relation_manifest(tmp_path))
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
    backend = _local(_relation_manifest(tmp_path))
    cache = _cache_with([ColumnProfile(name="contact_col", data_type="VARCHAR")])
    assert backend._cache_pii_lookup(cache)("order__contact") == {"pii": False}


def test_local_cache_lookup_is_silent_on_unknown_dimensions(tmp_path: Path):
    backend = _local(_relation_manifest(tmp_path))
    lookup = backend._cache_pii_lookup(_cache_with([]))
    # Not in the manifest at all, and a column the cache never profiled: both must
    # return None so the name heuristic stays in charge.
    assert lookup("nowhere__thing") is None
    assert lookup("order__status") is None


class _Inventory:
    """A live inventory listing, counting how often the pre-check asked for it."""

    def __init__(self, identifiers: list[str], *, fails: bool = False) -> None:
        self.identifiers = identifiers
        self.fails = fails
        self.calls = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        if self.fails:
            raise RuntimeError("inventory unavailable")
        return self.identifiers


def test_relation_precheck_refuses_a_foreign_database(tmp_path: Path):
    # The manifest was compiled against another database entirely. No allowlist
    # could bring it into scope, so this is the mismatch the check exists for.
    backend = _local(_relation_manifest(tmp_path))
    cache = _cache_with([ColumnProfile(name="status", data_type="VARCHAR")])
    live = _Inventory(["wh.main.orders", "wh.staging.customers"])
    message, _unprofiled = backend._relation_precheck(
        "SELECT status FROM other_db.main.orders", cache, "duckdb", live
    )
    assert message is not None
    assert "different namespace" in message
    assert "other_db.main.orders" in message


def test_relation_precheck_refuses_a_relation_gone_from_a_listed_schema(tmp_path: Path):
    # Same database, same schema, and the inventory looked: the model was renamed,
    # dropped, or never built into this target. A different problem, so a different
    # message; blaming the compiled namespace would send the reader the wrong way.
    backend = _local(_relation_manifest(tmp_path))
    live = _Inventory(["wh.main.customers"])
    message, _unprofiled = backend._relation_precheck(
        "SELECT status FROM wh.main.orders", None, "duckdb", live
    )
    assert message is not None
    assert "was listed and the relation was not in it" in message
    assert "Build the model" in message


def test_relation_precheck_accepts_a_suffix_match(tmp_path: Path):
    # The cache is connector-normalized, so a legitimate spelling that resolves by
    # suffix must pass rather than being rejected on an exact string compare.
    backend = _local(_relation_manifest(tmp_path))
    cache = _cache_with([ColumnProfile(name="status", data_type="VARCHAR")])
    live = _Inventory(["wh.main.orders"])
    for sql in ("SELECT status FROM main.orders", "SELECT status FROM orders"):
        assert backend._relation_precheck(sql, cache, "duckdb", live) == (None, [])
    # resolved from the cache alone, so the connection was never asked
    assert live.calls == 0


def test_relation_precheck_accepts_a_built_but_unprofiled_relation(tmp_path: Path):
    # Issue #134: `transform build` creates the relation, `explore profile` is what
    # puts it in the cache, and the query must not need the second step. The cache
    # holds a different table, so the miss is real and the live listing decides.
    backend = _local(_relation_manifest(tmp_path))
    cache = _cache_with(
        [ColumnProfile(name="status", data_type="VARCHAR")],
        identifier="wh.main.customers",
    )
    live = _Inventory(["wh.main.customers", "wh.main.orders"])
    message, unprofiled = backend._relation_precheck(
        "SELECT status FROM wh.main.orders", cache, "duckdb", live
    )
    assert message is None
    assert live.calls == 1
    # queryable, and the weaker PII screening it implies is disclosed
    assert unprofiled == ["wh.main.orders"]
    note = _unprofiled_note(unprofiled)[0]
    assert "wh.main.orders" in note and "explore profile" in note


def test_relation_precheck_runs_without_a_cache(tmp_path: Path):
    # No `explore map` yet is no longer a hole in the guard: the connection itself
    # is the authority, so a foreign relation is still refused before any spend.
    backend = _local(_relation_manifest(tmp_path))
    live = _Inventory(["wh.main.orders"])
    for cache in (None, DexCache(datasets=[])):
        message, _unprofiled = backend._relation_precheck(
            "SELECT status FROM wh.main.gone", cache, "duckdb", live
        )
        assert message is not None
    assert backend._relation_precheck(
        "SELECT status FROM wh.main.orders", None, "duckdb", live
    ) == (None, ["wh.main.orders"])


def test_relation_precheck_never_refuses_an_unlisted_schema(tmp_path: Path):
    # The dataset allowlist can be narrower than the dbt project. `elsewhere` is a
    # schema of a database this connection does carry, so it is out of the
    # listing's scope rather than out of reach, and dex must not answer for it.
    backend = _local(_relation_manifest(tmp_path))
    live = _Inventory(["wh.main.orders"])
    message, unprofiled = backend._relation_precheck(
        "SELECT status FROM wh.elsewhere.orders", None, "duckdb", live
    )
    assert message is None
    assert unprofiled == ["wh.elsewhere.orders"]


def test_relation_precheck_does_not_refuse_on_an_unreadable_inventory(tmp_path: Path):
    # An introspection failure is not evidence of a missing relation, and a
    # genuinely missing one still fails at planning without billing.
    backend = _local(_relation_manifest(tmp_path))
    live = _Inventory([], fails=True)
    message, _unprofiled = backend._relation_precheck(
        "SELECT status FROM wh.main.orders", None, "duckdb", live
    )
    assert message is None
    assert live.calls == 1


def test_relation_precheck_ignores_cte_names(tmp_path: Path):
    # MetricFlow renders a stack of named subqueries. A CTE is defined by the
    # statement, not looked up in the connection, so it is not a missing relation.
    backend = _local(_relation_manifest(tmp_path))
    live = _Inventory(["wh.main.orders"])
    sql = "WITH subq_0 AS (SELECT status FROM wh.main.orders) SELECT status FROM subq_0"
    assert backend._relation_precheck(sql, None, "duckdb", live) == (
        None,
        ["wh.main.orders"],
    )


def test_relation_precheck_says_nothing_about_a_profiled_relation(tmp_path: Path):
    # A profile in the cache is what the PII gate needs, so there is nothing to
    # disclose. An inventory-only entry (no columns) is not a profile.
    backend = _local(_relation_manifest(tmp_path))
    live = _Inventory(["wh.main.orders"])
    sql = "SELECT status FROM wh.main.orders"
    profiled = _cache_with([ColumnProfile(name="status", data_type="VARCHAR")])
    assert backend._relation_precheck(sql, profiled, "duckdb", live) == (None, [])
    inventory_only = DexCache(datasets=[Dataset(identifier="wh.main.orders")])
    assert backend._relation_precheck(sql, inventory_only, "duckdb", live) == (
        None,
        ["wh.main.orders"],
    )


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

    backend = _local(_write_manifest(tmp_path))
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
    assert "semantic.deployment: dbt_cloud" in message


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
