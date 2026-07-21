"""A behavioral fake of the dbt Cloud Semantic Layer GraphQL transport.

Not a mock: the backend under test is the real ``HostedDbtCloudBackend``; only the
``_post`` transport is replaced. It answers real GraphQL query strings
(introspection, ``createQuery``, poll) from a canned catalog and result, records
every posted query in order (so a test can assert what the backend did and did NOT
call, e.g. that a PII-refused query never reached ``createQuery``), and carries a
recognizable secret token so the no-leak assertions have something to catch.
"""

from __future__ import annotations

import json

from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.explore.semantic.hosted import HostedDbtCloudBackend

# A token shaped like a real dbt Cloud Semantic Layer service token; the no-leak
# tests assert this exact string never appears in an emitted envelope.
SECRET_TOKEN = "dbts_FAKE_secret_token_must_not_leak"  # noqa: S105 (test fixture)


def table_json_result(columns: list[str], types: list[str], rows: list[list]) -> str:
    """A pandas ``orient='table'`` ``jsonResult`` string, including the leading
    pandas ``index`` column the real API returns (which the backend must drop)."""

    fields = [{"name": "index", "type": "integer"}] + [
        {"name": c, "type": t} for c, t in zip(columns, types, strict=False)
    ]
    data = []
    for i, row in enumerate(rows):
        record = {"index": i}
        record.update(dict(zip(columns, row, strict=False)))
        data.append(record)
    return json.dumps(
        {"schema": {"fields": fields, "primaryKey": ["index"]}, "data": data}
    )


class FakeHostedBackend(HostedDbtCloudBackend):
    def __init__(
        self,
        *,
        metrics: list | None = None,
        dimensions_meta: list | None = None,
        result: str | None = None,
        status: str = "SUCCESSFUL",
        error: str | None = None,
        limits: QueryLimits | None = None,
    ) -> None:
        super().__init__("fake.host", "42", SECRET_TOKEN, limits or QueryLimits())
        self._metrics = metrics or []
        self._dimensions_meta = dimensions_meta or []
        self._result = result
        self._status = status
        self._error = error
        self.posted: list[str] = []

    def _post(self, query: str) -> dict:
        self.posted.append(query)
        if "createQuery" in query:
            return {"createQuery": {"queryId": "FAKE_QID"}}
        if "query(environmentId" in query:
            return {
                "query": {
                    "status": self._status,
                    "error": self._error,
                    "jsonResult": self._result,
                }
            }
        if "dimensions(environmentId" in query:
            return {"dimensions": self._dimensions_meta}
        if "metrics(environmentId" in query:
            return {"metrics": self._metrics}
        raise AssertionError(f"unexpected GraphQL query: {query}")
