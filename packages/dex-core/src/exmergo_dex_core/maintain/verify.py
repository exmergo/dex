"""maintain verify: a baseline-free sweep answering "is this project correct
right now", as opposed to drift's "what changed since the baseline" (#224).

This module carries the first finding class (#225): build-status gaps read
from the compiled manifest and the last run's ``run_results.json``, plus a
project that fails to compile at all. Every function here is pure and reads
only artifacts already on disk (or a warehouse's cheap metadata listing for
:func:`missing_relation_findings`); none of it scans a row, so it is free on
every connector, matching #225's acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..cache import match_identifier
from ..transform.build import shadow_parse
from .drift import DriftFinding

#: dbt's own run_results.json status strings that mean the node did not build.
_FAILURE_STATUSES = frozenset({"error", "fail"})


def compile_check(project_dir: Path) -> tuple[DriftFinding | None, list[str]]:
    """Whether the project parses at all.

    A project that does not compile invalidates every finding computed from
    its manifest (a stale or absent ``target/manifest.json`` looks identical
    to one from a project that simply has not been built yet), so this is
    meant to run first, and the caller suppresses the manifest-derived checks
    on failure (#172's inertness, #225's third acceptance bullet).

    Returns ``(finding, notes)``: a finding only on a proven parse failure.
    ``notes`` carries a reason instead when the check could not run at all
    (no dbt installed, no ``profiles.yml``) rather than silently reporting
    nothing, reusing :func:`~..transform.build.shadow_parse`'s own degrade
    path so this and `transform plan` never disagree about when dbt is
    reachable.
    """

    result = shadow_parse(project_dir, [])
    if not result["available"]:
        return None, [f"compile check skipped ({result['reason']})"]
    if result["success"]:
        return None, []
    detail = result["messages"][0] if result["messages"] else "dbt parse failed"
    finding = DriftFinding(
        axis="build",
        code="project_does_not_compile",
        severity="high",
        detail=detail,
        data={"messages": result["messages"]},
    )
    return finding, []


def _manifest_nodes(project_dir: Path) -> dict[str, dict] | None:
    path = project_dir / "target" / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("nodes", {})


def _run_results(project_dir: Path) -> list[dict] | None:
    path = project_dir / "target" / "run_results.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("results", [])


def build_status_findings(project_dir: Path) -> tuple[list[DriftFinding], list[str]]:
    """Nodes that failed, or were skipped because a parent failed, from the
    last run's ``run_results.json`` joined to the compiled manifest for names
    and the dependency graph. Free: reads artifacts already on disk, opens no
    connection.

    A skip is walked back through however many transitively-skipped parents
    it takes to name the node that actually failed, since a two-layer partial
    build (a grandparent failure skipping both its child and grandchild)
    would otherwise only ever name the immediate, also-skipped parent.
    """

    nodes = _manifest_nodes(project_dir) or {}
    results = _run_results(project_dir)
    if results is None:
        return (
            [],
            [
                "no dbt run results found; run `dbt run` or `dbt build` for "
                "build-status findings"
            ],
        )

    def name_of(uid: str) -> str:
        node = nodes.get(uid)
        if node and node.get("name"):
            return str(node["name"])
        return uid.rsplit(".", 1)[-1]

    status_by_uid = {
        r["unique_id"]: str(r.get("status", "unknown"))
        for r in results
        if r.get("unique_id")
    }

    def failed_ancestor(uid: str, seen: set[str]) -> str | None:
        if uid in seen:
            return None
        seen.add(uid)
        node = nodes.get(uid) or {}
        for dep in (node.get("depends_on") or {}).get("nodes", []):
            status = status_by_uid.get(dep)
            if status in _FAILURE_STATUSES:
                return name_of(dep)
            if status == "skipped":
                found = failed_ancestor(dep, seen)
                if found:
                    return found
        return None

    findings: list[DriftFinding] = []
    for result in results:
        uid = result.get("unique_id")
        status = str(result.get("status", "unknown"))
        if not uid or status not in _FAILURE_STATUSES | {"skipped"}:
            continue
        name = name_of(uid)
        if status in _FAILURE_STATUSES:
            message = result.get("message") or status
            findings.append(
                DriftFinding(
                    axis="build",
                    code="node_failed",
                    identifier=name,
                    severity="high",
                    detail=f"'{name}' failed to build: {message}",
                    data={"status": status},
                )
            )
            continue
        cause = failed_ancestor(uid, set())
        findings.append(
            DriftFinding(
                axis="build",
                code="node_skipped",
                identifier=name,
                # A named cause is a definite causal chain; an unnamed one
                # (a selector exclusion, an upstream error dbt did not
                # attribute) is real but less actionable, so it ranks lower.
                severity="medium" if cause else "low",
                detail=(
                    f"'{name}' was skipped because '{cause}' failed to build"
                    if cause
                    else f"'{name}' was skipped"
                ),
                data={"caused_by": cause} if cause else {},
            )
        )
    return findings, []


def missing_relation_findings(
    model_relations: dict[str, str],
    live_identifiers: list[str],
    already_reported: set[str],
) -> list[DriftFinding]:
    """Manifest models with no corresponding relation in the warehouse.

    ``already_reported`` names models a build-status finding already
    explained (a node that failed or was skipped never produced a relation
    either, and reporting that twice under a different code would say the
    same thing about the same node in two places). ``model_relations`` is
    expected pre-filtered to model names (no ``.``): a source's own
    "declared but absent" case belongs to the schema axis, which already
    reports dangling sources against a baseline.
    """

    findings: list[DriftFinding] = []
    for name, relation in sorted(model_relations.items()):
        if name in already_reported:
            continue
        if match_identifier(relation, live_identifiers):
            continue
        findings.append(
            DriftFinding(
                axis="build",
                code="no_relation",
                identifier=name,
                severity="high",
                detail=(
                    f"'{name}' is declared in the project but has no relation "
                    f"in the warehouse ({relation})"
                ),
                data={"relation_name": relation},
            )
        )
    return findings
