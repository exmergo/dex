"""Focused tests for the hosted transport seam."""

import json

import pytest

from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.hosted_transport import (
    await_result,
    shape_json_result,
)


def test_query_and_values_share_one_polling_contract():
    seen: list[str] = []
    responses = iter(
        [
            {"query": {"status": "RUNNING"}},
            {"query": {"status": "SUCCESSFUL", "jsonResult": "answer"}},
        ]
    )

    def post(query: str):
        seen.append(query)
        return next(responses)

    assert (
        await_result(
            post,
            environment_id="123",
            query_id="query-id",
            attempts=2,
            interval=0,
        )
        == "answer"
    )
    assert len(seen) == 2
    assert all("query-id" in query for query in seen)


def test_invalid_hosted_tabular_json_is_a_clean_backend_error():
    with pytest.raises(SemanticBackendError, match="invalid tabular JSON"):
        shape_json_result("not json", limits=QueryLimits())


def test_hosted_table_decoder_drops_the_pandas_index():
    result = shape_json_result(
        json.dumps(
            {
                "schema": {
                    "fields": [
                        {"name": "index", "type": "integer"},
                        {"name": "sessions", "type": "number"},
                    ]
                },
                "data": [{"index": 0, "sessions": 3}],
            }
        ),
        limits=QueryLimits(),
    )

    assert result["columns"] == ["sessions"]
    assert result["types"] == ["number"]
    assert result["cells"] == [[3]]


def test_a_failed_hosted_query_never_carries_the_value_it_failed_on():
    """dbt Cloud relays the warehouse's words, which can quote a row's value
    (#1070)."""

    def post(query: str):
        return {
            "query": {
                "status": "FAILED",
                "error": "Numeric value 'zq_sentinel_7' is not recognized",
            }
        }

    with pytest.raises(SemanticBackendError) as raised:
        await_result(post, environment_id="123", query_id="q", attempts=1, interval=0)
    assert "zq_sentinel_7" not in str(raised.value)
    assert "Numeric value <value withheld> is not recognized" in str(raised.value)
