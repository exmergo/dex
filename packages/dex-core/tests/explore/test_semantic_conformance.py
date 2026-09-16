"""The shipped `SemanticBackend` conformance contract, bound to both backends.

The contract itself lives in `exmergo_dex_core.explore.semantic.conformance`,
because a backend outside this distribution has to be able to run it. This file
is dex holding its own two backends to it, which is what stops the contract from
drifting into a description of something nobody satisfies.

Three bindings, not two, and the third is the point. `--local` reports one
dimension row per queryable path where MetricFlow resolved the join graph and one
per declaration where it could not, and both are legitimate reads declared in the
payload. Binding both states that as a fact about the contract rather than
leaving it to whichever extras the machine running the suite happens to have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes.semantic import (
    FakeHostedBackend,
    reference_hosted_dimension_meta,
    reference_hosted_metrics,
)

from exmergo_dex_core import dbt_project as dbt_project_module
from exmergo_dex_core.adapters.project import DbtProject
from exmergo_dex_core.config import DexConfig, QueryLimits
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.explore.semantic.conformance import (
    REFERENCE_LAYER,
    SemanticBackendContract,
    SemanticCatalogContract,
    reference_dbt_manifest,
    write_reference_project,
)
from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend
from exmergo_dex_core.storage import MemoryStore


class _Layer(DbtProject):
    """A dbt project whose join resolution the binding states.

    The same shape `test_semantic.py` uses: the resolver is injected into the
    format rather than monkeypatched onto the module, because the format is where
    the read happens and the backend reaches it through the project seam.
    """

    def __init__(self, root: Path, project: Path, resolve_paths) -> None:
        super().__init__(root, project)
        self._resolve_paths = resolve_paths

    def semantic_catalog(self):
        return dbt_project_module.semantic_catalog(
            self.project_dir, resolve_paths=self._resolve_paths
        )


def _local_backend(tmp_path: Path, resolve_paths) -> LocalMetricFlowBackend:
    project = write_reference_project(tmp_path)
    return LocalMetricFlowBackend(
        project,
        DexEngine(config=DexConfig(), store=MemoryStore()),
        "duckdb",
        QueryLimits(),
        _Layer(project.parent, project, resolve_paths),
    )


def _no_joins(_manifest_text: str) -> None:
    """What an install with no `[semantic]` extra has: no resolver, so the read is
    the declared single-hop view and says so in `dimension_scope`."""

    return None


def _real_joins(manifest_text: str):
    """MetricFlow's own resolver, skipping the binding where it is not installed.

    Verified against the reference layer during development: it resolves exactly
    the token set `REFERENCE_LAYER` declares per metric, including the two the
    single-hop scheme cannot express. A binding that quietly fell back to `None`
    here would turn the join-resolved half of the contract into a skip nobody
    noticed, so an absent extra skips the whole class instead.
    """

    resolved = dbt_project_module.resolve_group_by_paths(manifest_text)
    if resolved is None:
        pytest.skip(
            "MetricFlow is not installed, so the join-resolved read cannot be "
            "exercised here; install the [semantic] extra to run this binding"
        )
    return resolved


class TestTheReferenceLayerItself:
    """The fixture, before anything is asserted against it.

    The contract's assertions are only as good as the layer they run on, and two
    of its claims are about that layer rather than about a backend: that the dbt
    rendering is what MetricFlow itself accepts, and that the groupable token sets
    written into the neutral description are the ones a resolver produces. Left
    unchecked, a fixture that drifted would relax the contract silently.
    """

    def test_the_dbt_rendering_carries_every_declaration(self):
        manifest = reference_dbt_manifest()

        assert len(manifest["semantic_models"]) == len(
            REFERENCE_LAYER["semantic_models"]
        )
        assert len(manifest["metrics"]) == len(REFERENCE_LAYER["metrics"])
        json.dumps(manifest)

    def test_metricflow_resolves_the_groupable_tokens_the_layer_declares(self):
        resolved = dbt_project_module.resolve_group_by_paths(
            json.dumps(reference_dbt_manifest())
        )
        if resolved is None:
            pytest.skip("MetricFlow is not installed")

        for metric in REFERENCE_LAYER["metrics"]:
            assert {path.token for path in resolved[metric["name"]]} == set(
                metric["groupable"]
            ), f"{metric['name']} resolves to a different token set than declared"


class TestLocalBackendJoinResolved(SemanticBackendContract, SemanticCatalogContract):
    """`--local` as a real install runs it: MetricFlow resolves the join graph."""

    @pytest.fixture(autouse=True)
    def _project(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def make_backend(self):
        return _local_backend(self._tmp_path, _real_joins)

    def make_reference_backend(self):
        return self.make_backend()


class TestLocalBackendDeclarationsOnly(
    SemanticBackendContract, SemanticCatalogContract
):
    """`--local` on an install with no `[semantic]` extra: the declared single-hop
    view, which is a narrower answer the payload states rather than a wrong one."""

    @pytest.fixture(autouse=True)
    def _project(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def make_backend(self):
        return _local_backend(self._tmp_path, _no_joins)

    def make_reference_backend(self):
        return self.make_backend()


class TestHostedBackend(SemanticBackendContract, SemanticCatalogContract):
    """`--api` against a transport answering the same layer, with the dbt Cloud
    API's own asymmetries intact."""

    def make_backend(self):
        return FakeHostedBackend(
            metrics=reference_hosted_metrics(),
            dimensions_meta=reference_hosted_dimension_meta(),
        )

    def make_reference_backend(self):
        return self.make_backend()


def test_the_two_backends_answer_the_same_layer_the_same_way(tmp_path: Path):
    """Parity where parity is claimed, on one identical layer.

    The contract asserts each backend against the reference layer separately, which
    catches a backend that is wrong. This catches the other thing: two backends
    that are each internally consistent and disagree with each other, which is what
    they did in production. Everything compared here is something both are
    documented to answer; everything they legitimately differ on is a declared gap
    and is checked by the contract instead.
    """

    local = _local_backend(tmp_path, _real_joins).list_definitions()
    hosted = FakeHostedBackend(
        metrics=reference_hosted_metrics(),
        dimensions_meta=reference_hosted_dimension_meta(),
    ).list_definitions()

    assert local.dimension_scope == hosted.dimension_scope
    assert [m.name for m in local.semantic_models] == [
        m.name for m in hosted.semantic_models
    ]
    assert {m.name: sorted(m.dimensions) for m in local.metrics} == {
        m.name: sorted(m.dimensions) for m in hosted.metrics
    }
    assert {m.name: m.type for m in local.metrics} == {
        m.name: m.type for m in hosted.metrics
    }
    assert {m.name: m.time_axis for m in local.metrics} == {
        m.name: m.time_axis for m in hosted.metrics
    }
    assert {d.name: d.definition for d in local.dimensions} == {
        d.name: d.definition for d in hosted.dimensions
    }
    assert {e.name: e.type for e in local.entities} == {
        e.name: e.type for e in hosted.entities
    }
    # The join keys, per declaration, which is the fact an agent works the join
    # graph out from and the one a flat entity record used to lose.
    assert {
        (e.name, r.semantic_model): (r.type, r.column)
        for e in local.entities
        for r in e.roles
    } == {
        (e.name, r.semantic_model): (r.type, r.column)
        for e in hosted.entities
        for r in e.roles
    }
