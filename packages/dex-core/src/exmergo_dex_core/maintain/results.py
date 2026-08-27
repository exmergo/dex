"""What the maintain commands return.

Detection results share one shape, :class:`DriftResult`, because every detector
answers the same question at a different cost: what has moved since the baseline
someone vouched for. Which axes ran is part of the answer, not an implementation
detail, so it is on the record: "no findings" from a run that skipped the grain
axis means something quite different from "no findings" from a full sweep.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..results import Result
from .drift import AxisResult, DriftFinding
from .reconcile import Proposal
from .snapshot import Snapshot


class LayerFingerprint(BaseModel):
    """Counts from a fingerprinted dbt layer, or nothing when there was none.

    ``model_count`` and ``file_count`` are also what the ``schema`` axis's
    ``model_added``/``model_removed``/``model_changed`` findings diff against
    the next time ``maintain check``/``maintain schema`` runs (see
    :func:`~.drift.transform_drift`); these counters are the pinned side of
    that comparison, legible on their own so a reviewer glancing at the
    snapshot's shape is not left guessing what a ``file_count`` of zero
    beside a dozen models means.
    """

    file_count: int | None = None
    model_count: int | None = None
    source_count: int | None = None
    semantic_model_count: int | None = None
    metric_count: int | None = None

    def transform_data(self) -> dict[str, Any]:
        return {
            "file_count": self.file_count,
            "model_count": self.model_count,
            "source_count": self.source_count,
        }

    def semantic_data(self) -> dict[str, Any]:
        return {
            "semantic_model_count": self.semantic_model_count,
            "metric_count": self.metric_count,
        }


class SnapshotResult(Result):
    """The captured baseline. ``snapshot`` is the object itself, for a caller
    that wants to diff or archive it without reading it back from the store."""

    snapshot: Snapshot | None = None
    snapshot_path: str = ""
    warehouse_from: str = ""
    dataset_count: int = 0
    relationship_count: int = 0
    grain_baseline_count: int = 0
    #: How many pinned objects carry column detail. Below ``dataset_count`` means
    #: the rest were pinned as metadata alone, so the schema axis has no columns
    #: to compare for them. Structural rather than prose-only because a host
    #: automating the accept needs to gate on it.
    column_detail_count: int = 0
    cache_updated_at: str | None = None
    transform_layer: LayerFingerprint | None = None
    semantic_layer: LayerFingerprint | None = None

    def data(self) -> dict[str, Any]:
        return {
            "snapshot_path": self.snapshot_path,
            "baseline": {
                "from": self.warehouse_from,
                "dataset_count": self.dataset_count,
                "relationship_count": self.relationship_count,
                "grain_baseline_count": self.grain_baseline_count,
                "column_detail_count": self.column_detail_count,
                "cache_updated_at": self.cache_updated_at,
            },
            "transform_layer": (
                self.transform_layer.transform_data()
                if self.transform_layer is not None
                else None
            ),
            "semantic_layer": (
                self.semantic_layer.semantic_data()
                if self.semantic_layer is not None
                else None
            ),
        }


class DriftResult(Result):
    """Findings from one or more drift axes, ranked most-severe first.

    ``by_axis`` is the per-axis split and ``findings`` the merged ranking. Both
    are reported because they answer different questions: the ranking says what
    to look at, the split says what was looked at. The split carries the actual
    findings too: an empty per-axis list must mean that axis found nothing, not
    that its findings live only in the merged ranking.
    """

    findings: list[DriftFinding] = Field(default_factory=list)
    by_axis: dict[str, AxisResult] = Field(default_factory=dict)
    snapshot_created_at: str | None = None
    warehouse_from: str = ""
    drift_path: str = ""

    def data(self) -> dict[str, Any]:
        return {
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "finding_count": len(self.findings),
            "axes_run": sorted(self.by_axis),
            "axes": {
                axis: {
                    **result.model_dump(mode="json"),
                    "finding_count": len(result.findings),
                }
                for axis, result in self.by_axis.items()
            },
            "baseline": {
                "snapshot_created_at": self.snapshot_created_at,
                "from": self.warehouse_from,
            },
            "drift_path": self.drift_path,
        }


class ReconcileResult(Result):
    """Proposed fixes for detected drift, as a stored plan of reviewable diffs.

    ``mechanical`` proposals have edits behind them and land in a plan;
    ``advisory`` ones are decisions only a human can make, so a result with
    proposals but no ``plan_id`` is the normal shape, not a failure.
    """

    proposals: list[Proposal] = Field(default_factory=list)
    plan_id: str | None = None

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "proposals": [p.model_dump(mode="json") for p in self.proposals],
            "proposal_count": len(self.proposals),
        }
        if self.proposals:
            # The mechanical/advisory split is how a reader decides what to
            # review and what to act on by hand. With nothing to split it is
            # two zeroes restating the count above, so it stays off.
            payload["mechanical_count"] = sum(
                1 for p in self.proposals if p.kind == "mechanical"
            )
            payload["advisory_count"] = sum(
                1 for p in self.proposals if p.kind == "advisory"
            )
        if self.plan_id is not None:
            payload["plan_id"] = self.plan_id
        return payload
