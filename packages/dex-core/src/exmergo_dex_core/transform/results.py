"""What the transform commands return.

The authoring commands all end the same way, in a stored plan of reviewable
diffs that nothing has applied yet, so :class:`PlanResult` serves most of them.
Applying and building are the two that touch something, and each says exactly
what it touched.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..results import Result


class InitResult(Result):
    """A scaffolded dbt project. Files are already written, unlike every other
    transform command: bootstrapping a project has nothing to plan against."""

    project_name: str = ""
    project_dir: str = ""
    connector: str = ""
    # Where the connector came from, because a build that targets the wrong
    # warehouse because a config was picked up implicitly is the failure this
    # command exists to prevent.
    connector_source: str = ""
    created: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "project_dir": self.project_dir,
            "connector": self.connector,
            "connector_source": self.connector_source,
            "created": self.created,
        }


class PlanResult(Result):
    """A stored plan of reviewable diffs. Nothing is applied by planning."""

    plan_id: str = ""
    intent: str = ""
    paths: list[str] = Field(default_factory=list)
    plan_path: str = ""
    # Semantic authoring classifies each name it touched, so a mixed payload
    # reports which definitions it created and which it evolved.
    defined: list[str] | None = None
    updated: list[str] | None = None

    def data(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plan_id": self.plan_id,
            "intent": self.intent,
            "edit_count": len(self.paths),
            "paths": self.paths,
            "plan_path": self.plan_path,
        }
        if self.defined is not None:
            payload["defined"] = self.defined
        if self.updated is not None:
            payload["updated"] = self.updated
        return payload


class MacroListResult(Result):
    """The shipped macros available to scaffold."""

    macros: list[dict[str, str]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {"macros": self.macros}


class MacroResult(PlanResult):
    """One shipped macro, either already current or planned into the project.

    A :class:`PlanResult` rather than a wrapper around one: scaffolding a macro
    *is* planning an edit, and the plan's diffs and warnings are this result's
    diffs and warnings. When the project's copy already matches, ``up_to_date``
    is set and the plan fields stay empty, because there was nothing to plan.
    """

    macro: str = ""
    path: str = ""
    up_to_date: bool = False

    def data(self) -> dict[str, Any]:
        payload = {"macro": self.macro, "path": self.path}
        if self.up_to_date:
            return {**payload, "up_to_date": True}
        return {**payload, **super().data()}


class ApplyResult(Result):
    """What applying a plan wrote, or the conflicts that stopped it.

    Conflicts with ``written`` empty is the propose-don't-impose refusal: files
    changed after the plan was made, human edits are authoritative, and nothing
    was overwritten. Re-running confirmed moves those paths into
    ``conflicts_overridden`` instead.
    """

    plan_id: str = ""
    written: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    conflicts_overridden: list[str] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        if self.pending_confirmation is not None:
            return {"plan_id": self.plan_id, "conflicts": self.conflicts}
        return {
            "plan_id": self.plan_id,
            "written": self.written,
            "conflicts_overridden": self.conflicts_overridden,
        }


class PlanListResult(Result):
    """Stored plans, pending and applied, newest first."""

    plans: list[dict[str, Any]] = Field(default_factory=list)

    def data(self) -> dict[str, Any]:
        return {"plans": self.plans, "count": len(self.plans)}


class DepsResult(Result):
    """A ``dbt deps`` run, or the reason there was nothing to install."""

    ran: bool = False
    reason: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        if not self.ran:
            return {"ran": False, "reason": self.reason}
        return {"ran": True, **self.summary}


class BuildResult(Result):
    """A finished dbt run, dev-target only and cost-surfaced beforehand.

    ``summary`` is dbt's own per-node accounting, kept as the adapter shaped it
    because each connector reports different figures and flattening them would
    lose the ones that matter for that warehouse.
    """

    success: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        return dict(self.summary)
