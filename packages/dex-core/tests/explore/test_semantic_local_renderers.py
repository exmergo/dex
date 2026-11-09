"""Every connector local metric queries claim names a renderer MetricFlow ships."""

from __future__ import annotations

import pytest

from exmergo_dex_core.explore.semantic import local_runtime


@pytest.mark.parametrize("connector", sorted(local_runtime._RENDERERS))
def test_each_supported_connector_builds_its_metricflow_renderer(connector):
    # A misspelled class name is invisible until a query runs on that connector:
    # Postgres shipped as `PostgresSqlPlanRenderer`, which MetricFlow 0.213 calls
    # `PostgresSQLSqlPlanRenderer`, so every local Postgres query failed.
    pytest.importorskip("metricflow.sql.render")

    client = local_runtime.sql_client(connector)

    assert client.sql_plan_renderer is not None
    assert client.sql_engine_type.name == local_runtime._RENDERERS[connector][2]
