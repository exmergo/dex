"""One import for a host that splits the dex lifecycle across processes.

A host application that authors a change where the model runs, applies it offline
in a disposable checkout, and validates it in a sandbox holding only a dev
credential needs four things the interactive path never has to expose: a plan a
second process can carry and check, the dependencies that plan introduces, what
its edits actually contain, and whether the build that reported success validated
anything. Those live in the modules that own them, because that is where the
logic they reuse lives. This is the door.

    from exmergo_dex_core.host import verify_plan_document, BuildOutcome

    plan = verify_plan_document(document, expect_digest=pinned)

Importing from here rather than from
:mod:`exmergo_dex_core.transform.portable` and its neighbours is worth the extra
name: it keeps the internal layout free to move under a host that is pinned to a
released version, and it puts the whole contract in one place a reader can scan.

Two things this deliberately does not do. It does not re-implement anything: every
name is the same object the engine uses, so a host and the engine can never
disagree about what a plan digest is or what makes a build validated. And it does
not include a way to run anything: the verbs stay on
:class:`~exmergo_dex_core.engine.DexEngine`, which owns the connection, the store,
and the guards, and a host reaching around it would be reaching around those too.

:func:`conformance_vectors` returns the shipped fixtures: a payload per case, with
the verdict each should produce. A host asserts its own reader against them, so
"we read plan documents correctly" is something it can prove in its own suite
rather than something it hopes.
"""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

#: Where each public name actually lives. Resolved on first access for the same
#: reason the package root does it: a bare install with no connector extra must
#: be able to import this module and reach the offline half of the contract
#: without pulling the dialect engine or a warehouse client.
_EXPORTS = {
    # The portable plan lifecycle.
    "PLAN_DOCUMENT_SCHEMA_VERSION": "transform.portable",
    "PlanDigestMismatchError": "transform.portable",
    "PlanDocumentError": "transform.portable",
    "PortableEdit": "transform.portable",
    "PortablePlan": "transform.portable",
    "apply_document": "transform.portable",
    "plan_digest": "transform.portable",
    "plan_document": "transform.portable",
    "verify_plan_document": "transform.portable",
    # What an edit contains.
    "ArtifactClass": "transform.classify",
    "ArtifactClassification": "transform.classify",
    "ArtifactSignal": "transform.classify",
    "classify_content": "transform.classify",
    "classify_edit": "transform.classify",
    # What a plan depends on.
    "AmbiguousReference": "transform.grounding",
    "GroundedDependency": "transform.grounding",
    "Grounding": "transform.grounding",
    "GroundingBinding": "transform.grounding",
    "UnresolvedReference": "transform.grounding",
    "config_digest": "transform.grounding",
    "ground_plan": "transform.grounding",
    "packages_digest": "transform.grounding",
    "source_digest": "transform.grounding",
    # What a build established.
    "BuildCoverage": "transform.evidence",
    "BuildDigests": "transform.evidence",
    "BuildEvidence": "transform.evidence",
    "BuildNode": "transform.evidence",
    "BuildOutcome": "transform.evidence",
    "BuildSelection": "transform.evidence",
    "PlanDrift": "transform.evidence",
    "build_evidence": "transform.evidence",
    "plan_drift": "transform.evidence",
    "required_nodes": "transform.evidence",
    # What a guarded build may install and may run.
    "DeclaredPackage": "transform.build",
    "DependencyPolicy": "transform.build",
    "MissingPackagesError": "transform.build",
    "declared_packages": "transform.build",
    "installed_packages": "transform.build",
    "missing_packages": "transform.build",
    "unverifiable_packages": "transform.build",
    "GuardedExecution": "guards.execution",
    "ProviderControl": "guards.execution",
    "StatementVerdict": "guards.execution",
    "guarded_execution_preflight": "guards.execution",
    "guarded_statement_verdict": "guards.execution",
    "NotSelectOnlyError": "guards.sql_guard",
    "RefusalReason": "guards.sql_guard",
    # What it cost, and in what.
    "Cost": "envelope",
    "EstimateQuality": "envelope",
    "Paradigm": "envelope",
    "paradigm_unit": "envelope",
    # The edit vocabulary a host builds payloads from. Reachable nowhere else
    # from the package root, which is why it is here rather than assumed.
    "Conflict": "edits",
    "Edit": "edits",
    "EditOp": "edits",
    "content_hash": "edits",
    "EditKind": "transform.plans",
    "PlanEdit": "transform.plans",
    "PlanError": "transform.plans",
    "PlanNotFoundError": "transform.plans",
    "TransformPlan": "transform.plans",
}

if TYPE_CHECKING:  # what a type checker and an IDE see; never run
    from ..edits import Conflict, Edit, EditOp, content_hash
    from ..envelope import Cost, EstimateQuality, Paradigm, paradigm_unit
    from ..guards.execution import (
        GuardedExecution,
        ProviderControl,
        StatementVerdict,
        guarded_execution_preflight,
        guarded_statement_verdict,
    )
    from ..guards.sql_guard import NotSelectOnlyError, RefusalReason
    from ..transform.build import (
        DeclaredPackage,
        DependencyPolicy,
        MissingPackagesError,
        declared_packages,
        installed_packages,
        missing_packages,
        unverifiable_packages,
    )
    from ..transform.classify import (
        ArtifactClass,
        ArtifactClassification,
        ArtifactSignal,
        classify_content,
        classify_edit,
    )
    from ..transform.evidence import (
        BuildCoverage,
        BuildDigests,
        BuildEvidence,
        BuildNode,
        BuildOutcome,
        BuildSelection,
        PlanDrift,
        build_evidence,
        plan_drift,
        required_nodes,
    )
    from ..transform.grounding import (
        AmbiguousReference,
        GroundedDependency,
        Grounding,
        GroundingBinding,
        UnresolvedReference,
        config_digest,
        ground_plan,
        packages_digest,
        source_digest,
    )
    from ..transform.plans import (
        EditKind,
        PlanEdit,
        PlanError,
        PlanNotFoundError,
        TransformPlan,
    )
    from ..transform.portable import (
        PLAN_DOCUMENT_SCHEMA_VERSION,
        PlanDigestMismatchError,
        PlanDocumentError,
        PortableEdit,
        PortablePlan,
        apply_document,
        plan_digest,
        plan_document,
        verify_plan_document,
    )

#: Spelled out rather than derived from `_EXPORTS`, so a reader sees the whole
#: contract without executing anything. `tests/test_host_contract.py` asserts the
#: two stay in step and that every name resolves.
__all__ = [
    "PLAN_DOCUMENT_SCHEMA_VERSION",
    "AmbiguousReference",
    "ArtifactClass",
    "ArtifactClassification",
    "ArtifactSignal",
    "BuildCoverage",
    "BuildDigests",
    "BuildEvidence",
    "BuildNode",
    "BuildOutcome",
    "BuildSelection",
    "Conflict",
    "Cost",
    "DeclaredPackage",
    "DependencyPolicy",
    "Edit",
    "EditKind",
    "EditOp",
    "EstimateQuality",
    "GroundedDependency",
    "Grounding",
    "GroundingBinding",
    "GuardedExecution",
    "MissingPackagesError",
    "NotSelectOnlyError",
    "Paradigm",
    "PlanDigestMismatchError",
    "PlanDocumentError",
    "PlanDrift",
    "PlanEdit",
    "PlanError",
    "PlanNotFoundError",
    "PortableEdit",
    "PortablePlan",
    "ProviderControl",
    "RefusalReason",
    "StatementVerdict",
    "TransformPlan",
    "UnresolvedReference",
    "apply_document",
    "build_evidence",
    "classify_content",
    "classify_edit",
    "config_digest",
    "conformance_vectors",
    "content_hash",
    "declared_packages",
    "ground_plan",
    "guarded_execution_preflight",
    "guarded_statement_verdict",
    "installed_packages",
    "missing_packages",
    "packages_digest",
    "paradigm_unit",
    "plan_digest",
    "plan_document",
    "plan_drift",
    "required_nodes",
    "source_digest",
    "unverifiable_packages",
    "verify_plan_document",
]

VECTORS_DIR = Path(__file__).parent / "vectors"


def conformance_vectors() -> list[dict[str, Any]]:
    """The shipped fixtures, one per case, sorted by name.

    Each carries ``name``, ``contract``, ``description``, ``payload``, and
    ``expect``. A host runs its own reader over the payload and checks the
    verdict, which is how "we read plan documents correctly" becomes something it
    proves in its own suite rather than something it hopes.

    Data, not assertions, and deliberately: the shape a host has to agree with is
    the payload, and a shipped test would only tell it whether *dex* agrees with
    dex. The engine's own suite runs these too, so a vector that stops matching
    the engine fails here before it reaches anybody.
    """

    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(VECTORS_DIR.glob("*.json"))
    ]


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"..{module}", __name__), name)


def __dir__() -> list[str]:
    return list(__all__)
