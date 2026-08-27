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
from exmergo_dex_core.explore.semantic import SemanticBackendError
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


def reference_hosted_metrics() -> list[dict]:
    """The conformance reference layer as the dbt Cloud API's nested
    ``metrics { ... }`` answer, for `FakeHostedBackend(metrics=...)`.

    Rendered here rather than in the shipped contract because it is a fake of one
    vendor's transport, and it reproduces that vendor's real asymmetries rather
    than a convenient shape: a `SemanticModel` with only a name, an `Entity` with
    no label, a `Measure` with no words, and a measure `expr` that is the
    expression dbt *compiled* rather than the one the author wrote. A renderer
    that filled those in would make the hosted backend look like the local one and
    the parity assertions would be asserting the renderer.
    """

    from exmergo_dex_core.explore.semantic.conformance import REFERENCE_LAYER

    models = {model["name"]: model for model in REFERENCE_LAYER["semantic_models"]}
    owner_of_dimension: dict[str, str] = {}
    owner_of_measure: dict[str, str] = {}
    for model in models.values():
        for dimension in model["dimensions"]:
            owner_of_dimension[dimension["name"]] = model["name"]
        for measure in model["measures"]:
            owner_of_measure[measure["name"]] = model["name"]

    # Every time dimension in this layer is day-grained, so every metric reports
    # the standard ladder, which is what the real API answers for such a layer.
    grains = ["DAY", "WEEK", "MONTH", "QUARTER", "YEAR"]
    lowered = [grain.lower() for grain in grains]

    payload = []
    for metric in REFERENCE_LAYER["metrics"]:
        measures = list(metric.get("input_measures") or [])
        if metric.get("measure"):
            measures = [metric["measure"]]
        owners = sorted({owner_of_measure[name] for name in measures})

        dimensions = []
        for groupable in metric["groupable"]:
            if groupable == "metric_time":
                # Synthesized by the layer rather than declared, so it carries no
                # owning model and no words, exactly as the real API returns it.
                dimensions.append(
                    {
                        "name": groupable,
                        "type": "TIME",
                        "queryableGranularities": grains,
                        "queryableTimeGranularities": lowered,
                    }
                )
                continue
            bare = groupable.rsplit("__", 1)[-1]
            owner = models[owner_of_dimension[bare]]
            declared = next(d for d in owner["dimensions"] if d["name"] == bare)
            is_time = declared["type"] == "time"
            dimensions.append(
                {
                    "name": groupable,
                    "type": "TIME" if is_time else "CATEGORICAL",
                    "label": declared["label"],
                    "description": declared["description"],
                    "expr": None,
                    "semanticModel": {"name": owner["name"]},
                    "queryableGranularities": grains if is_time else [],
                    "queryableTimeGranularities": lowered if is_time else [],
                }
            )

        entities = [
            {
                "name": entity["name"],
                "type": entity["type"].upper(),
                # No `label`: the API's Entity type has no such field.
                "description": entity["description"],
                "expr": entity["expr"],
                "role": None,
                "semanticModel": {"name": owner},
            }
            for owner in owners
            for entity in models[owner]["entities"]
        ]

        params: dict = {"inputMeasures": [{"name": name} for name in measures]}
        if metric.get("measure"):
            params["measure"] = {"name": metric["measure"]}
        if metric.get("numerator"):
            params["numerator"] = {"name": metric["numerator"]}
            params["denominator"] = {"name": metric["denominator"]}

        payload.append(
            {
                "name": metric["name"],
                "type": metric["type"].upper(),
                "label": metric["label"],
                "description": metric["description"],
                "queryableGranularities": grains,
                "queryableTimeGranularities": lowered,
                "requiresMetricTime": False,
                "filter": (
                    {"whereSqlTemplate": metric["filter"]}
                    if metric.get("filter")
                    else None
                ),
                "typeParams": params,
                "measures": [
                    {
                        "name": name,
                        "agg": next(
                            m["agg"]
                            for m in models[owner_of_measure[name]]["measures"]
                            if m["name"] == name
                        ).upper(),
                        # The compiled expression, not the authored one: a plain
                        # `count` comes back from the real API as a CASE WHEN and
                        # therefore resolves to no column, where a project read of
                        # the same measure does resolve one.
                        "expr": _compiled_expr(
                            next(
                                m
                                for m in models[owner_of_measure[name]]["measures"]
                                if m["name"] == name
                            )
                        ),
                        "aggTimeDimension": models[owner_of_measure[name]][
                            "agg_time_dimension"
                        ],
                    }
                    for name in measures
                ],
                # Only a name: the API's SemanticModel type carries nothing else.
                "semanticModels": [{"name": owner} for owner in owners],
                "dimensions": dimensions,
                "entities": entities,
            }
        )
    return payload


def _compiled_expr(measure: dict) -> str:
    """A measure's expression as dbt compiles it for the API.

    A `count` is rendered as the CASE WHEN that MetricFlow sums, which is why a
    hosted measure resolves to no column where a local one does. Reproduced rather
    than smoothed over, because that asymmetry is documented and a test that hid
    it would make the documentation unverifiable.
    """

    if measure["agg"] == "count":
        return f"CASE WHEN {measure['expr']} IS NOT NULL THEN 1 ELSE 0 END"
    return measure["expr"]


def reference_hosted_dimension_meta() -> dict[str, list[dict]]:
    """The reference layer's per-metric `dimensions(metrics:)` answer.

    Keyed per metric so the fake reproduces the field's real intersection
    behavior, which is the contract the PII gate has to be built against.
    """

    from exmergo_dex_core.explore.semantic.conformance import REFERENCE_LAYER

    return {
        metric["name"]: [
            {"name": groupable, "config": {"meta": None}}
            for groupable in metric["groupable"]
        ]
        for metric in REFERENCE_LAYER["metrics"]
    }


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
        values_need_a_metric: bool = False,
    ) -> None:
        super().__init__("fake.host", "42", SECRET_TOKEN, limits or QueryLimits())
        self._metrics = metrics or []
        self._dimensions_meta = dimensions_meta if dimensions_meta is not None else []
        self._result = result
        self._status = status
        self._error = error
        # The real layer refuses a distinct-values request for a dimension reached
        # through a join, because there is no measure to join from, and accepts the
        # same request once it is scoped to a metric. That refusal is the whole
        # reason the backend renders twice, so the fake reproduces it rather than
        # answering both shapes.
        self._values_need_a_metric = values_need_a_metric
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
        # The real layer resolves a values request in two places, and the
        # difference is the whole reason the backend probes before it executes:
        # `compileDimensionValuesSql` refuses synchronously and for free, while
        # `createDimensionValuesQuery` accepts the same request and reports the
        # refusal asynchronously, at poll time. A fake that refused the mutation
        # would make the probe look unnecessary.
        if "compileDimensionValuesSql" in query:
            if self._values_need_a_metric and "metrics:" not in query:
                raise SemanticBackendError(
                    "semantic layer error: the given input does not match any of "
                    "the available group-by-items for a distinct values query "
                    "without metrics"
                )
            return {"compileDimensionValuesSql": {"queryId": "FAKE_CID"}}
        if "createDimensionValuesQuery" in query:
            return {"createDimensionValuesQuery": {"queryId": "FAKE_VID"}}
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
