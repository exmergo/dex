"""Tests for `explore semantic values`: one dimension's value domain.

The command that answers the precondition for writing a filter, on a surface where
no other dex command can reach it: `explore profile` cannot see a semantic
dimension, and on a hosted layer there is no SQL path at all.

Three things here are the contract rather than the implementation, and each has a
test that would fail if it were quietly changed. The dimension is resolved against
the layer before it is asked for, so a token the layer does not have is refused by
name. The whole output is values, so a PII-flagged dimension refuses the command
instead of being screened alongside others. And a dimension reached through a join
is only answerable in the context of a metric, so dex renders the cheap form first,
escalates once, and says which of the two it did.

Kept out of `test_semantic.py`, which is past 2,500 lines; the split of that file
into a package is its own change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes.semantic import FakeHostedBackend, table_json_result

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
from exmergo_dex_core.explore.results import SemanticValuesResult
from exmergo_dex_core.explore.semantic import commands as semantic_commands
from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend
from exmergo_dex_core.results import to_envelope
from exmergo_dex_core.storage import MemoryStore

# ---- fixtures ---------------------------------------------------------------
#
# One layer expressed twice, as elsewhere in this suite: a dimension of the model
# that owns it (answerable on its own) and a dimension reached through a join
# (answerable only through a metric). The difference between those two is what the
# command is designed around, so both backends are held to the same shape.


def _hosted_metrics():
    return [
        {
            "name": "sessions",
            "queryableGranularities": ["DAY", "MONTH"],
            "dimensions": [
                {"name": "metric_time"},
                {"name": "user__pricing_tier"},
                {"name": "user__created_at"},
                {"name": "user__email"},
            ],
        },
        {
            "name": "agent_runs",
            "queryableGranularities": ["DAY", "MONTH"],
            "dimensions": [
                {"name": "metric_time"},
                {"name": "user__pricing_tier"},
                {"name": "session__user__pricing_tier"},
            ],
        },
    ]


def _tiers():
    return table_json_result(["user__pricing_tier"], ["string"], [["free"], ["pro"]])


def _hosted(**kwargs) -> FakeHostedBackend:
    return FakeHostedBackend(metrics=_hosted_metrics(), result=_tiers(), **kwargs)


def _engine(config: DexConfig | None = None, **kwargs) -> DexEngine:
    return DexEngine(config=config or DexConfig(), store=MemoryStore(), **kwargs)


def _manifest(tmp_path: Path) -> Path:
    """Two semantic models joined on `user`, one of them carrying a PII column.

    `sessions` reaches `user__pricing_tier` through the join, which is the shape
    that cannot be asked for without a metric.
    """

    project = tmp_path / "proj"
    (project / "target").mkdir(parents=True)
    manifest = {
        "semantic_models": [
            {
                "name": "users",
                "node_relation": {"alias": "users", "relation_name": "wh.main.users"},
                "entities": [{"name": "user", "type": "primary", "expr": "user_id"}],
                "dimensions": [
                    {"name": "pricing_tier", "type": "categorical"},
                    {"name": "contact", "type": "categorical", "expr": "contact_col"},
                    {"name": "created_at", "type": "time"},
                ],
                "measures": [{"name": "user_count", "agg": "count"}],
            },
            {
                "name": "sessions",
                "node_relation": {
                    "alias": "sessions",
                    "relation_name": "wh.main.sessions",
                },
                "entities": [
                    {"name": "session", "type": "primary", "expr": "session_key"},
                    {"name": "user", "type": "foreign", "expr": "user_id"},
                ],
                "dimensions": [{"name": "mode", "type": "categorical"}],
                "measures": [{"name": "session_count", "agg": "count"}],
            },
            {
                # No measures, so no metric reaches it: the half of a layer a
                # hosted read cannot see at all, and the case with no second
                # rendering to attempt.
                "name": "regions",
                "node_relation": {
                    "alias": "regions",
                    "relation_name": "wh.main.regions",
                },
                "entities": [
                    {"name": "region", "type": "primary", "expr": "region_id"}
                ],
                "dimensions": [{"name": "label", "type": "categorical"}],
                "measures": [],
            },
        ],
        "metrics": [
            {
                "name": "users",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "user_count"}]},
            },
            {
                "name": "sessions",
                "type": "simple",
                "type_params": {"input_measures": [{"name": "session_count"}]},
            },
        ],
    }
    (project / "target" / "semantic_manifest.json").write_text(json.dumps(manifest))
    return project


def _local(project: Path, engine: DexEngine | None = None) -> LocalMetricFlowBackend:
    """The local backend with a real project format injected, as `from_engine`
    wires it: the catalog read and the gate's column resolution both come through
    that seam, so a stand-in would test the stand-in."""

    return LocalMetricFlowBackend(
        project,
        engine or _engine(),
        "duckdb",
        QueryLimits(),
        DbtProject(project.parent, project),
    )


class _Rendered:
    """A MetricFlow engine that records what it was asked to render.

    Only `explain_get_dimension_values` is faked, and it refuses the requests the
    real one refuses: a dimension reached through a join has no distinct-values
    rendering without a measure. A fake that answered both shapes would make the
    escalation untestable, which is the behavior most worth pinning here.
    """

    def __init__(self, needs_a_metric: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._needs_a_metric = needs_a_metric

    def explain_get_dimension_values(self, *, metric_names, get_group_by_values):
        metrics = list(metric_names or [])
        self.calls.append((get_group_by_values, metrics))
        if get_group_by_values in self._needs_a_metric and not metrics:
            raise RuntimeError(
                "The given input does not match any of the available "
                "group-by-items for a distinct values query without metrics."
            )
        return type(
            "_Explained",
            (),
            {
                "sql_statement": type(
                    "_Sql", (), {"sql": "SELECT pricing_tier FROM wh.main.users"}
                )()
            },
        )()


# ---- resolving the request --------------------------------------------------


def test_an_unknown_dimension_is_refused_by_name_on_both_backends(tmp_path: Path):
    """A typo must not read as a fact about the layer.

    The hosted API's own reverse-lookup field answers `[]` for an unknown name and
    for a real dimension no metric can reach alike, so resolving against the
    catalog is what makes the two tellable apart.
    """

    hosted = _hosted()
    with pytest.raises(sem.SemanticBackendError, match="no such dimension"):
        hosted.values("user__princing_tier", [])
    assert not any("createDimensionValuesQuery" in q for q in hosted.posted)

    with pytest.raises(sem.SemanticBackendError, match="no such dimension"):
        _local(_manifest(tmp_path)).values("user__princing_tier", [])


def test_a_refusal_says_the_token_is_entity_qualified(tmp_path: Path):
    """The likeliest mistake is naming the column, not misspelling the token."""

    with pytest.raises(sem.SemanticBackendError, match="entity-qualified"):
        _local(_manifest(tmp_path)).values("pricing_tier", [])


def test_an_unknown_metric_scope_is_refused_by_name(tmp_path: Path):
    hosted = _hosted()
    with pytest.raises(sem.SemanticBackendError, match="no such metric"):
        hosted.values("user__pricing_tier", ["sesions"])
    assert not any("createDimensionValuesQuery" in q for q in hosted.posted)

    with pytest.raises(sem.SemanticBackendError, match="no such metric"):
        _local(_manifest(tmp_path)).values("user__pricing_tier", ["userz"])


def test_a_grain_suffix_is_split_for_the_lookup_and_kept_for_the_query(tmp_path: Path):
    """No dimension name carries a grain, so validating the spelled token would
    refuse `user__created_at__month`, which both layers answer."""

    hosted = _hosted()
    hosted.values("user__created_at__month", [])
    mutation = next(q for q in hosted.posted if "createDimensionValuesQuery" in q)
    assert '{name: "user__created_at", grain: MONTH}' in mutation

    backend = _local(_manifest(tmp_path))
    rendered = _Rendered()
    backend._metricflow_engine = lambda: rendered
    request = sem.resolve_values_request(
        backend._semantic_view(), "user__created_at__month", []
    )
    assert (request.name, request.grain) == ("user__created_at", "month")
    # MetricFlow spells the grain into the token, so the token goes through whole.
    backend._render_values(request)
    assert rendered.calls[0][0] == "user__created_at__month"


def test_the_grain_vocabulary_is_the_layers_own(tmp_path: Path):
    """A project may define a granularity of its own and spell it into a token the
    same way, so the split reads the layer rather than a constant dex keeps."""

    view = _local(_manifest(tmp_path))._semantic_view()
    request = sem.resolve_values_request(view, "user__pricing_tier", [])
    assert set(request.grains) >= {"day", "month"}


# ---- the PII gate -----------------------------------------------------------


def test_a_flagged_dimension_refuses_the_command_rather_than_being_screened(
    tmp_path: Path,
):
    """A metric query can drop a flagged dimension from its grouping and still
    answer. This command's whole output is values, so there is nothing to fall
    back to and the request is refused."""

    project = _manifest(tmp_path)
    backend = _local(project)
    cache = DexCache(
        datasets=[
            Dataset(
                identifier="wh.main.users",
                columns=[
                    ColumnProfile(
                        name="contact_col",
                        data_type="VARCHAR",
                        pii=PIIFlag(category=PIICategory.EMAIL, confidence=0.9),
                    )
                ],
            )
        ]
    )
    backend._load_cache = lambda: cache
    rendered = _Rendered()
    backend._metricflow_engine = lambda: rendered

    with pytest.raises(sem.SemanticQueryRefusedError, match="nothing but the values"):
        backend.values("user__contact", [])
    # Refused before anything was rendered, let alone executed.
    assert rendered.calls == []


def test_a_pii_shaped_name_is_the_floor_when_nothing_authoritative_speaks():
    hosted = _hosted()
    with pytest.raises(sem.SemanticQueryRefusedError, match="PII"):
        hosted.values("user__email", [])
    assert not any("createDimensionValuesQuery" in q for q in hosted.posted)


def test_a_refusal_names_the_durable_way_to_clear_it():
    hosted = _hosted()
    with pytest.raises(sem.SemanticQueryRefusedError, match="pii_overrides"):
        hosted.values("user__email", [])


def test_name_only_screening_is_disclosed_on_the_result():
    """The floor is not equivalent to evidence, so a result screened on a name
    alone says so rather than letting the weaker screening pass for the stronger."""

    record = _hosted().values("user__pricing_tier", [])
    assert any("name heuristic alone" in note for note in record.notes)


def test_the_hosted_gate_asks_about_every_metric_that_reaches_the_dimension():
    """`dimensions(metrics:)` returns the intersection across the metrics it is
    given, so one call per metric unioned is what keeps the layer authoritative
    here. Asking about all of them at once would shrink the map as the dimension
    got more reachable, which is backwards."""

    hosted = _hosted()
    hosted.values("user__pricing_tier", [])
    metadata = next(q for q in hosted.posted if "config { meta }" in q)
    assert "a0: dimensions(" in metadata and "a1: dimensions(" in metadata
    assert '"sessions"' in metadata and '"agent_runs"' in metadata


# ---- reaching a joined dimension --------------------------------------------


def test_the_cheap_shape_is_tried_first_and_carries_no_metric():
    hosted = _hosted()
    record = hosted.values("user__pricing_tier", [])
    mutations = [q for q in hosted.posted if "createDimensionValuesQuery" in q]
    assert len(mutations) == 1
    assert "metrics:" not in mutations[0]
    assert record.scoped_to == []
    assert not any("reached through a join" in note for note in record.notes)


def test_a_joined_dimension_escalates_once_and_says_so():
    """Both layers refuse a distinct-values request they cannot reach without a
    measure. The scoped shape is the only one that exists, and it answers a
    narrower question, so the metric it settled on is reported rather than
    silently chosen."""

    hosted = _hosted(values_need_a_metric=True)
    record = hosted.values("session__user__pricing_tier", [])
    assert record.scoped_to == ["agent_runs"]
    note = next(n for n in record.notes if "reached through a join" in n)
    assert "agent_runs" in note and "--metric" in note


def test_the_escalation_is_settled_for_free_before_any_query_is_created():
    """dbt Cloud accepts the values mutation and reports a resolution failure at
    poll time, so deciding after it would mean running a second query once the
    first had already been submitted. The free compile resolves the same request
    synchronously, which is what keeps the fallback from costing a query."""

    hosted = _hosted(values_need_a_metric=True)
    hosted.values("session__user__pricing_tier", [])
    probes = [q for q in hosted.posted if "compileDimensionValuesSql" in q]
    created = [q for q in hosted.posted if "createDimensionValuesQuery" in q]
    assert len(probes) == 2 and "metrics:" not in probes[0]
    # Exactly one query is created, and it is the shape the probe settled on.
    assert len(created) == 1 and '{name: "agent_runs"}' in created[0]
    # The probe never asks for the SQL: this is a resolution check, not a dry run.
    assert "sql" not in probes[0]


def test_an_explicit_metric_scope_is_used_directly_and_needs_no_probe():
    """With nothing to choose between, there is nothing to probe for, so the
    caller's own shape goes straight to the layer."""

    hosted = _hosted(values_need_a_metric=True)
    record = hosted.values("session__user__pricing_tier", ["agent_runs"])
    assert not any("compileDimensionValuesSql" in q for q in hosted.posted)
    mutations = [q for q in hosted.posted if "createDimensionValuesQuery" in q]
    assert len(mutations) == 1
    assert '{name: "agent_runs"}' in mutations[0]
    assert record.scoped_to == ["agent_runs"]
    # The caller chose, so there is nothing to disclose about dex's choice.
    assert not any("reached through a join" in note for note in record.notes)


def test_the_local_backend_escalates_the_same_way(tmp_path: Path):
    backend = _local(_manifest(tmp_path))
    rendered = _Rendered(needs_a_metric=("session__user__pricing_tier",))
    backend._metricflow_engine = lambda: rendered
    view = backend._semantic_view()
    request = sem.resolve_values_request(view, "user__pricing_tier", [])
    used, _sql = backend._render_values(request)
    assert used == [] and len(rendered.calls) == 1


def test_a_dimension_no_metric_reaches_is_not_rendered_twice(tmp_path: Path):
    """A known dimension with no metric behind it has no second attempt to make,
    and rendering the identical request again would only produce the same error."""

    backend = _local(_manifest(tmp_path))
    rendered = _Rendered()
    backend._metricflow_engine = lambda: rendered
    request = sem.resolve_values_request(backend._semantic_view(), "region__label", [])
    assert request.reachable == []
    backend._render_values(request)
    assert len(rendered.calls) == 1


# ---- the result -------------------------------------------------------------


def test_the_result_says_which_question_it_answers():
    record = _hosted().values("user__pricing_tier", [])
    assert isinstance(record, SemanticValuesResult)
    payload = record.data()
    assert payload["dimension"] == "user__pricing_tier"
    assert payload["scoped_to"] == []
    assert payload["columns"] == ["user__pricing_tier"]
    assert payload["row_count"] == 2
    assert payload["execution"] == "vendor"
    assert payload["query_id"] == "FAKE_VID"


def test_the_result_is_columnar_and_passes_the_sanitizer(capsys):
    """The sanitizer refuses anything shaped like raw rows and any key that reads
    like a secret, and it hard-fails the command on the way out, so a new payload
    shape has to be put through it rather than assumed to pass."""

    env.emit(to_envelope(_hosted().values("user__pricing_tier", [])))
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["data"]["cells"] == [["free"], ["pro"]]


def test_the_hosted_result_says_the_cost_guard_did_not_apply():
    """dbt Cloud executes this server-side under its own credential, so there is
    no estimate dex could honestly report and no ceiling it could have set."""

    record = _hosted().values("user__pricing_tier", [])
    assert record.cost.paradigm == env.Paradigm.HOSTED
    assert record.cost.estimate is None
    assert any("cost guard unavailable" in w for w in record.warnings)


def test_the_domain_is_capped_like_every_other_columnar_result():
    """A high-cardinality dimension comes back cut and saying so, rather than as
    thousands of values. dex reports what came back and never claims a cardinality
    it would have to bill a second scan for."""

    rows = [[f"value_{i}"] for i in range(50)]
    backend = FakeHostedBackend(
        metrics=_hosted_metrics(),
        result=table_json_result(["user__pricing_tier"], ["string"], rows),
        limits=QueryLimits(max_rows=10),
    )
    record = backend.values("user__pricing_tier", [])
    assert record.row_count == 10
    assert record.truncated is True
    assert any("truncated to 10 rows" in note for note in record.notes)


# ---- the command surface ----------------------------------------------------


def _api_engine() -> DexEngine:
    return _engine(
        DexConfig(
            semantic={
                "backend": "dbt_cloud",
                "host": "sl.cloud.getdbt.com",
                "environment_id": "42",
            }
        )
    )


def test_the_command_takes_exactly_one_dimension(monkeypatch):
    import argparse

    monkeypatch.setattr(
        semantic_commands, "semantic_values", lambda *a, **k: pytest.fail("routed")
    )
    args = argparse.Namespace(
        mode="values", metrics=["a", "b"], metric=None, for_dimension=None
    )
    envelope = semantic_commands.cmd_semantic(args, _api_engine())
    assert envelope.status == env.Status.ERROR
    assert any("exactly one dimension" in e for e in envelope.errors)


def test_a_flag_that_means_nothing_in_this_mode_is_refused_not_dropped():
    import argparse

    for mode in ("values", "query"):
        args = argparse.Namespace(
            mode=mode,
            metrics=["user__pricing_tier"],
            metric=None,
            for_dimension=["user__pricing_tier"],
        )
        envelope = semantic_commands.cmd_semantic(args, _api_engine())
        assert envelope.status == env.Status.ERROR
        assert any("--for-dimension" in e for e in envelope.errors)


def test_a_backend_without_the_member_is_named_rather_than_crashing(monkeypatch):
    class _Partial:
        name = "cube_cloud"

    monkeypatch.setattr(
        semantic_commands.__name__.rsplit(".", 1)[0] and sem,
        "resolve_backend",
        lambda *a, **k: _Partial(),
    )
    with pytest.raises(sem.SemanticBackendError, match="does not read a dimension"):
        semantic_commands.semantic_values(_api_engine(), "user__pricing_tier")


def test_the_engine_and_the_cli_reach_the_same_command(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        semantic_commands,
        "semantic_values",
        lambda engine, dimension, **kwargs: seen.append((dimension, kwargs)),
    )
    engine = _api_engine()
    engine.semantic_values("user__pricing_tier", metrics=["sessions"], api=True)
    assert seen[-1][0] == "user__pricing_tier"
    assert seen[-1][1]["metrics"] == ["sessions"]
