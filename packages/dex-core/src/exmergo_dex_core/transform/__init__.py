"""Transform: the authoring capability, spanning dbt SQL models, tests and docs,
and the semantic layer (dbt semantic models / MetricFlow YAML).

``commands`` holds the CLI orchestrators; the sibling modules are the engine they
drive: ``plans`` (propose/apply as reviewable diffs), ``validate`` (per-kind edit
checks), ``build`` (gated dev-target dbt builds), ``semantic`` (MetricFlow YAML),
and ``scaffold`` (deterministic staging skeletons). The Viz preview is not part
of this engine: it is a separate product surface and stays a stub here.

The engine entry points are re-exported here so callers address the capability,
not its file layout: ``transform.plan(...)``, ``transform.apply(...)``,
``transform.build(...)``.
"""

from .build import ProdTargetRefusedError, assert_dev_target, build
from .plans import (
    EditKind,
    PlanEdit,
    PlanError,
    PlanNotFoundError,
    PlanStore,
    TransformPlan,
    apply,
    plan,
)

__all__ = [
    "EditKind",
    "PlanEdit",
    "PlanError",
    "PlanNotFoundError",
    "PlanStore",
    "ProdTargetRefusedError",
    "TransformPlan",
    "apply",
    "assert_dev_target",
    "build",
    "plan",
]
