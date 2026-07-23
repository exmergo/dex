"""Transform: the authoring capability, spanning dbt SQL models, tests and docs,
and the semantic layer (dbt semantic models / MetricFlow YAML).

``commands`` holds the CLI orchestrators; the sibling modules are the engine they
drive: ``init`` (dbt project bootstrap), ``plans`` (propose/apply as reviewable
diffs), ``validate`` (per-kind edit checks), ``build`` (gated dev-target dbt
builds), ``semantic`` (MetricFlow YAML), and ``scaffold`` (deterministic staging
skeletons). The Viz preview is not part of this engine: it is a separate product
surface and stays a stub here.

The engine entry points are re-exported here so callers address the capability,
not its file layout: ``transform.plan(...)``, ``transform.apply(...)``,
``transform.build(...)``, ``transform.init_project(...)``.
"""

from .build import (
    ProdTargetRefusedError,
    assert_dev_target,
    build,
    deps,
    has_package_spec,
    needs_deps,
    shadow_parse,
)
from .init import InitError, InitResult, init_project
from .plans import (
    EditKind,
    EditOp,
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
    "EditOp",
    "InitError",
    "InitResult",
    "PlanEdit",
    "PlanError",
    "PlanNotFoundError",
    "PlanStore",
    "ProdTargetRefusedError",
    "TransformPlan",
    "apply",
    "assert_dev_target",
    "build",
    "deps",
    "has_package_spec",
    "init_project",
    "needs_deps",
    "plan",
    "shadow_parse",
]
