"""A behavioral fake of the dbt Cloud Semantic Layer GraphQL transport.

Not a mock: the backend under test is the real ``HostedDbtCloudBackend``; only the
``_post`` transport is replaced. It answers real GraphQL query strings
(introspection, ``createQuery``, poll) from a canned catalog and result, records
every posted query in order (so a test can assert what the backend did and did NOT
call, e.g. that a PII-refused query never reached ``createQuery``), and carries a
recognizable secret token so the no-leak assertions have something to catch.

The ``dimensions`` field reproduces the real API's contract rather than a
convenient one: **it returns the dimensions common to all the listed metrics, not
their union.** That is the whole reason the PII gate asks one metric at a time, so
a fake that answered with the union would make the gate's regression untestable.
"""

from __future__ import annotations

import json
import re

from exmergo_dex_core.config import QueryLimits
from exmergo_dex_core.explore.semantic.hosted import HostedDbtCloudBackend

# A token shaped like a real dbt Cloud Semantic Layer service token; the no-leak
# tests assert this exact string never appears in an emitted envelope.
SECRET_TOKEN = "dbts_FAKE_secret_token_must_not_leak"  # noqa: S105 (test fixture)

# One `dimensions(...)` selection, with the field alias that lets several of them
# share a document. Both forms are matched, because the point of some of these
# tests is what happens when one call carries several metrics.
_DIMENSIONS_FIELD = re.compile(r"(?:(\w+):\s*)?dimensions\((?P<args>[^)]*)\)")
# The `metrics(...)` root field, aliased or not. One document carries both fields
# (the per-metric PII map and the layer's queryable grains), so the fake answers
# per field rather than per document: a transport that recognized only the first
# field it saw would make a one-round-trip claim untestable.
_METRICS_FIELD = re.compile(r"(?:(\w+):\s*)?metrics\(environmentId[^)]*\)")
_METRIC_NAME = re.compile(r'name:\s*"([^"]+)"')


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
    """``dimensions_meta`` takes either shape.

    A flat list is the layer answering the same way for every metric, which is all
    a single-metric test needs. A ``{metric: [dimension, ...]}`` mapping is the
    layer as it really is, where a dimension belongs to the metrics that can group
    by it, and it is what makes the intersection observable.
    """

    def __init__(
        self,
        *,
        metrics: list | None = None,
        dimensions_meta: list | dict | None = None,
        result: str | None = None,
        status: str = "SUCCESSFUL",
        error: str | None = None,
        limits: QueryLimits | None = None,
    ) -> None:
        super().__init__("fake.host", "42", SECRET_TOKEN, limits or QueryLimits())
        self._metrics = metrics or []
        self._dimensions_meta = dimensions_meta if dimensions_meta is not None else []
        self._result = result
        self._status = status
        self._error = error
        self.posted: list[str] = []

    def _dimensions_for(self, metrics: list[str]) -> list[dict]:
        """The dimensions this field answers with: the intersection across the
        metrics it was asked about, keyed by name, in a stable order."""

        if not isinstance(self._dimensions_meta, dict):
            return list(self._dimensions_meta)
        per_metric = [
            {d["name"]: d for d in self._dimensions_meta.get(m, [])} for m in metrics
        ]
        if not per_metric:
            return []
        common = set(per_metric[0])
        for entry in per_metric[1:]:
            common &= set(entry)
        merged: dict[str, dict] = {}
        for entry in per_metric:
            for name in common:
                merged.setdefault(name, entry[name])
        return [merged[name] for name in sorted(merged)]

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
        data: dict = {}
        for match in _DIMENSIONS_FIELD.finditer(query):
            alias = match.group(1) or "dimensions"
            metrics = _METRIC_NAME.findall(match.group("args"))
            data[alias] = self._dimensions_for(metrics)
        for match in _METRICS_FIELD.finditer(query):
            data[match.group(1) or "metrics"] = self._metrics
        if not data:
            raise AssertionError(f"unexpected GraphQL query: {query}")
        return data
