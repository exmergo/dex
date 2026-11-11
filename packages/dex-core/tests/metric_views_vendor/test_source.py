"""The Metric View source behind the engine's seam, and what it never imports."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from exmergo_dex_core.envelope import Reason, reason_for
from exmergo_dex_core.metric_views import dependency

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "exmergo_dex_core"


def test_the_engine_builds_the_source_through_the_seam(tmp_path):
    pytest.importorskip("sqlglot")
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.metric_views.project import MetricViewSemanticLayer
    from exmergo_dex_core.semantic_source import SemanticSnapshotSource

    from .conftest import ORDERS, repo

    engine = DexEngine.from_repo(repo(tmp_path, orders=ORDERS))
    built = engine.semantic_catalog_source()

    assert isinstance(built, MetricViewSemanticLayer)
    assert isinstance(built, SemanticSnapshotSource)


def test_a_missing_dialect_engine_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(dependency, "find_spec", lambda name: None)

    with pytest.raises(dependency.MetricViewDependencyError) as caught:
        dependency.ensure_available()

    assert "exmergo-dex-core[metric-views]" in str(caught.value)
    assert reason_for(caught.value) == Reason.PREREQUISITE


def test_maintain_does_not_fingerprint_metricflow_under_this_vendor(tmp_path):
    pytest.importorskip("sqlglot")
    from exmergo_dex_core.engine import DexEngine
    from exmergo_dex_core.maintain.commands import _semantic_layer

    from .conftest import ORDERS, repo

    class Project:
        def semantic_layer(self):
            raise AssertionError("read the MetricFlow layer under vendor: databricks")

    engine = DexEngine.from_repo(repo(tmp_path, orders=ORDERS))
    layer = _semantic_layer(engine, Project())

    assert layer is not None and layer.vendor == "databricks"


def test_the_extra_is_the_dialect_engine():
    import tomllib

    pyproject = tomllib.loads(
        (PACKAGE.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    extras = pyproject["project"]["optional-dependencies"]

    assert extras["metric-views"] == ["exmergo-dex-core[sql]"]
    assert "metric-views" in extras["all"][0]


def test_only_lineage_imports_sqlglot():
    importers = sorted(
        path.name
        for path in (PACKAGE / "metric_views").glob("*.py")
        if re.search(r"^\s*(import|from)\s+sqlglot", path.read_text(), re.MULTILINE)
    )

    assert importers == ["lineage.py"]


def test_loading_the_config_and_the_package_leaves_sqlglot_unimported(tmp_path):
    code = (
        "import sys\n"
        "import exmergo_dex_core.metric_views\n"
        "import exmergo_dex_core.metric_views.project\n"
        "import exmergo_dex_core.metric_views.loader\n"
        "from exmergo_dex_core.config import DexConfig\n"
        "DexConfig.model_validate({'connector': 'duckdb', 'semantic': "
        "{'vendor': 'databricks', 'databricks': {'files': ['v.yaml']}}})\n"
        "assert 'sqlglot' not in sys.modules, 'sqlglot was imported'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603
