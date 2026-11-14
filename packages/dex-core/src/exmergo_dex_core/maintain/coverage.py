"""``maintain coverage``: which project resources the dbt tests reach (#1280).

An inventory read from a parsed ``manifest.json`` alone: no warehouse statement,
no estimate, no connection. It answers the question a user asks before any
mutation run, where the gaps are, and reports each dimension apart with its own
numerator and denominator rather than one blended percentage (#1287).

What it counts, and against what:

- **Resources.** The root project's enabled models, sources, snapshots and
  seeds, each in its own section. Package and disabled resources are excluded
  from every denominator and named under ``excluded`` with the reason. An
  ephemeral model is counted and marked: its tests run through its consumers.
- **Own tests, by kind.** ``generic`` (attached to the resource), ``singular``
  (a test file that reads it), ``unit`` (a unit test of that model) and
  ``freshness`` (a source's declared check), with each data test's severity.
  A disabled test counts nowhere but ``excluded``.
- **Downstream tests.** Data tests on the resources that read this one, listed
  apart and never counted toward its own coverage. A unit test on a downstream
  model is not one: it reads fixtures, not this resource.
- **Incremental models.** Whether any unit test overrides ``is_incremental`` to
  true, since a full refresh and an ordinary unit test never run that branch.
- **Columns.** A documented column is ``asserted`` when a generic test is
  attached to it. A column with a test that ``schema.yml`` does not document is
  listed under ``undocumented_tested`` and counted nowhere.

A generic test is reported by the test it applies (``not_null``,
``dbt_utils.unique_combination_of_columns``), never by its node name or
arguments: dbt names an ``accepted_values`` test after the values it accepts,
so its name can carry a value, and the payload is reviewed for values-free mode.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .. import dbt_tests

#: The resource types inventoried, and the section each one is reported in.
SECTIONS: dict[str, str] = {
    "model": "models",
    "source": "sources",
    "snapshot": "snapshots",
    "seed": "seeds",
}

#: Resources listed per section before the cut. Untested resources sort first,
#: so a cut never hides a gap behind a covered resource.
RESOURCE_CAP = 25
#: Tests listed per resource, and columns listed per column list.
TEST_CAP = 12
COLUMN_CAP = 12
#: Excluded resources named before the cut.
EXCLUDED_CAP = 25
#: Downstream resources named per resource.
DOWNSTREAM_CAP = 10

_NOUN = {
    "models": "models",
    "sources": "sources",
    "snapshots": "snapshots",
    "seeds": "seeds",
}


@dataclass
class _Resource:
    unique_id: str
    node: dict[str, Any]
    resource_type: str
    label: str
    tests: list[dict[str, Any]] = field(default_factory=list)
    test_ids: set[str] = field(default_factory=set)
    asserted: set[str] = field(default_factory=set)
    undocumented_tested: set[str] = field(default_factory=set)
    downstream: set[str] = field(default_factory=set)
    downstream_via: set[str] = field(default_factory=set)
    incremental_branch_tests: list[str] = field(default_factory=list)


@dataclass
class Inventory:
    """The inventory's payload pieces, in the order ``data`` prints them."""

    summary: dict[str, Any]
    sections: dict[str, list[dict[str, Any]]]
    excluded: dict[str, Any]
    elided: dict[str, int]
    scope: dict[str, Any]
    notes: list[str]
    warnings: list[str]


def _label(unique_id: str, node: dict[str, Any]) -> str:
    from ..dbt_project import manifest_node_label

    if node.get("resource_type") == "source":
        return f"{node.get('source_name')}.{node.get('name')}"
    return manifest_node_label(unique_id, node)


def _ratio(numerator: int, denominator: int, of: str) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator, "of": of}


def _documented(node: dict[str, Any]) -> dict[str, str]:
    """Documented column names, keyed lowercase, as ``schema.yml`` spells them."""

    columns = node.get("columns") or {}
    return {
        str(column.get("name") or key).lower(): str(column.get("name") or key)
        for key, column in columns.items()
        if isinstance(column, dict)
    }


def _materialized(node: dict[str, Any]) -> str | None:
    value = (node.get("config") or {}).get("materialized")
    return str(value) if value else None


def _names_for(resource: _Resource) -> set[str]:
    """Every spelling a selector may use for this resource, lowercase."""

    node = resource.node
    names = {resource.label.lower(), str(node.get("name") or "").lower()}
    if resource.resource_type == "model" and node.get("version") not in (None, ""):
        stem = PurePosixPath(str(node.get("original_file_path") or "")).stem
        if stem:
            names.add(stem.lower())
    if resource.resource_type == "source":
        names.add(str(node.get("identifier") or "").lower())
    return {name for name in names if name}


def _excluded_entry(unique_id: str, node: dict[str, Any], reason: str) -> dict:
    entry = {
        "name": _label(unique_id, node),
        "resource_type": node.get("resource_type"),
        "reason": reason,
    }
    if reason == "package":
        entry["package"] = node.get("package_name")
    return entry


def inventory(
    manifest: dict[str, Any],
    *,
    selector: Iterable[str] | None = None,
    full: bool = False,
) -> Inventory:
    """Inventory the tests ``manifest`` declares for the root project."""

    project = (manifest.get("metadata") or {}).get("project_name")
    nodes: dict[str, dict] = manifest.get("nodes") or {}
    sources: dict[str, dict] = manifest.get("sources") or {}
    units: dict[str, dict] = manifest.get("unit_tests") or {}
    notes: list[str] = []
    warnings: list[str] = []

    resources: dict[str, _Resource] = {}
    excluded: list[dict[str, Any]] = []
    for unique_id, node in [*nodes.items(), *sources.items()]:
        resource_type = node.get("resource_type")
        if resource_type not in SECTIONS:
            continue
        if project and node.get("package_name") != project:
            excluded.append(_excluded_entry(unique_id, node, "package"))
            continue
        if (node.get("config") or {}).get("enabled") is False:
            excluded.append(_excluded_entry(unique_id, node, "disabled"))
            continue
        # dbt writes the id on the node too; the unit-test binding reads it.
        node = {**node, "unique_id": unique_id}
        resources[unique_id] = _Resource(
            unique_id, node, resource_type, _label(unique_id, node)
        )

    disabled_tests = 0
    for unique_id, entries in (manifest.get("disabled") or {}).items():
        for node in entries if isinstance(entries, list) else [entries]:
            if not isinstance(node, dict):
                continue
            resource_type = node.get("resource_type")
            if resource_type in SECTIONS:
                if project and node.get("package_name") != project:
                    excluded.append(_excluded_entry(unique_id, node, "package"))
                else:
                    excluded.append(_excluded_entry(unique_id, node, "disabled"))
            elif resource_type in {"test", "unit_test"} and (
                not project or node.get("package_name") == project
            ):
                disabled_tests += 1

    # Scope. Selection narrows the inventory; downstream reads are still walked
    # through the whole project, since a selected model's protection comes from
    # models the selector did not name.
    wanted = {name.lower() for name in selector or []}
    scope: dict[str, Any]
    if wanted:
        matched: set[str] = set()
        selected: set[str] = set()
        for unique_id, resource in resources.items():
            names = _names_for(resource)
            hit = names & wanted
            if not hit and resource.resource_type == "model":
                hit = {resource.label.split(".", 1)[0].lower()} & wanted
            if hit:
                selected.add(unique_id)
                matched |= hit
        unmatched = sorted(wanted - matched)
        if unmatched:
            warnings.append(
                "maintain coverage selected nothing for "
                + ", ".join(f"'{name}'" for name in unmatched)
                + ": no enabled model, source, snapshot or seed of this project "
                "has that name (package and disabled resources are not "
                "inventoried)"
            )
        scope = {"selected": sorted(resources[uid].label for uid in selected)}
    else:
        selected = set(resources)
        scope = {"selected": "all"}

    # Who reads whom, through the project's own resources.
    children: dict[str, set[str]] = defaultdict(set)
    for unique_id, node in nodes.items():
        for parent in (node.get("depends_on") or {}).get("nodes") or []:
            if node.get("resource_type") in SECTIONS:
                children[parent].add(unique_id)

    def descendants(unique_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [unique_id]
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return seen

    totals = dict.fromkeys(dbt_tests.TEST_KINDS, 0)
    severities = {"error": 0, "warn": 0}
    package_tests = 0
    tests_on_excluded = 0
    # Each data test, with what it reads, for the downstream walk below.
    data_tests: list[tuple[str, dict, set[str], set[str]]] = []
    for unique_id, test in nodes.items():
        if test.get("resource_type") != "test":
            continue
        if not dbt_tests.test_enabled(test):
            disabled_tests += 1
            continue
        if project and test.get("package_name") != project:
            package_tests += 1
            continue
        kind = dbt_tests.data_test_kind(test)
        reads = set(dbt_tests.test_reads(test))
        if kind == "generic":
            owners = set(dbt_tests.generic_test_targets(test))
        else:
            owners = reads
        mine = owners & set(resources)
        if not mine:
            tests_on_excluded += 1
            continue
        data_tests.append((unique_id, test, mine, reads))
        if not mine & selected:
            continue
        totals[kind] += 1
        severity = dbt_tests.test_severity(test)
        severities[severity] += 1
        for owner in mine:
            resource = resources[owner]
            resource.test_ids.add(unique_id)
            record: dict[str, Any] = {"kind": kind, "severity": severity}
            if kind == "generic":
                record["test"] = dbt_tests.generic_test_name(test)
                column = test.get("column_name")
                if column:
                    record["column"] = str(column)
                    documented = _documented(resource.node)
                    key = str(column).strip('"`[]').lower()
                    if key in documented:
                        resource.asserted.add(key)
                    else:
                        resource.undocumented_tested.add(str(column))
            else:
                record["name"] = test.get("name")
            resource.tests.append(record)

    for unique_id, unit in units.items():
        if project and unit.get("package_name") not in (None, project):
            package_tests += 1
            continue
        unit_owners = [
            uid
            for uid, resource in resources.items()
            if resource.resource_type == "model"
            and dbt_tests.covers(unit, resource.node)
        ]
        if not unit_owners:
            tests_on_excluded += 1
            continue
        if not set(unit_owners) & selected:
            continue
        totals["unit"] += 1
        for owner in unit_owners:
            resource = resources[owner]
            resource.test_ids.add(unique_id)
            record = {"kind": "unit", "name": unit.get("name")}
            if _materialized(resource.node) == "incremental":
                value = ((unit.get("overrides") or {}).get("macros") or {}).get(
                    "is_incremental"
                )
                takes_branch = value is True or str(value).lower() == "true"
                record["is_incremental"] = takes_branch
                if takes_branch:
                    resource.incremental_branch_tests.append(str(unit.get("name")))
            resource.tests.append(record)

    for unique_id in selected:
        resource = resources[unique_id]
        if resource.resource_type != "source":
            continue
        policy = dbt_tests.freshness_policy(resource.node)
        if policy is not None:
            totals["freshness"] += 1
            resource.tests.append({"kind": "freshness", "severity": policy})

    # Downstream: data tests on the resources that read this one, and tests
    # that read it without being attached to it (a relationships test points
    # at its parent but asserts the child).
    for unique_id in selected:
        resource = resources[unique_id]
        below = descendants(unique_id)
        for test_id, _test, owners, reads in data_tests:
            if test_id in resource.test_ids:
                continue
            if reads & ({unique_id} | below):
                resource.downstream.add(test_id)
                resource.downstream_via |= {
                    resources[owner].label for owner in owners if owner != unique_id
                }

    elided = {
        "models": 0,
        "sources": 0,
        "snapshots": 0,
        "seeds": 0,
        "tests": 0,
        "columns": 0,
        "downstream_resources": 0,
        "excluded": 0,
    }
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in _NOUN}
    for unique_id in selected:
        resource = resources[unique_id]
        sections[SECTIONS[resource.resource_type]].append(_record(resource))
    for name, records in sections.items():
        records.sort(key=lambda r: (r["tested"], r["name"]))
        if not full and len(records) > RESOURCE_CAP:
            elided[name] = len(records) - RESOURCE_CAP
            del records[RESOURCE_CAP:]
        if not full:
            for record in records:
                _cap(record, elided)

    summary = _summary(resources, selected, totals, severities, scoped=bool(wanted))

    excluded.sort(key=lambda e: (e["reason"], str(e["resource_type"]), e["name"]))
    excluded_block: dict[str, Any] = {
        "resource_count": len(excluded),
        "by_reason": {
            reason: sum(1 for entry in excluded if entry["reason"] == reason)
            for reason in ("package", "disabled")
        },
        "resources": excluded if full else excluded[:EXCLUDED_CAP],
        "tests": {
            "disabled": disabled_tests,
            "package": package_tests,
            "on_excluded_resource": tests_on_excluded,
        },
    }
    if not full and len(excluded) > EXCLUDED_CAP:
        elided["excluded"] = len(excluded) - EXCLUDED_CAP

    notes.extend(_elision_notes(elided))
    if summary["models"]["ephemeral"]:
        notes.append(
            "an ephemeral model builds no relation: dbt inlines it into its own "
            "tests and into the models that read it. It is counted like any "
            "other model and marked `ephemeral`"
        )
    return Inventory(
        summary=summary,
        sections=sections,
        excluded=excluded_block,
        elided=elided,
        scope=scope,
        notes=notes,
        warnings=warnings,
    )


def _cap(record: dict[str, Any], elided: dict[str, int]) -> None:
    """Cut one listed record's lists to their caps, counting every cut."""

    columns = record["columns"]
    for key in ("unasserted", "undocumented_tested"):
        if len(columns[key]) > COLUMN_CAP:
            elided["columns"] += len(columns[key]) - COLUMN_CAP
            columns[key] = columns[key][:COLUMN_CAP]
    incremental = record.get("incremental")
    if incremental and len(incremental["incremental_branch_tests"]) > TEST_CAP:
        branch = incremental["incremental_branch_tests"]
        elided["tests"] += len(branch) - TEST_CAP
        incremental["incremental_branch_tests"] = branch[:TEST_CAP]
    if len(record["tests"]) > TEST_CAP:
        elided["tests"] += len(record["tests"]) - TEST_CAP
        record["tests"] = record["tests"][:TEST_CAP]
    via = record["downstream_tests"]["on"]
    if len(via) > DOWNSTREAM_CAP:
        elided["downstream_resources"] += len(via) - DOWNSTREAM_CAP
        record["downstream_tests"]["on"] = via[:DOWNSTREAM_CAP]


def _record(resource: _Resource) -> dict:
    node = resource.node
    own = dict.fromkeys(dbt_tests.TEST_KINDS, 0)
    severity = {"error": 0, "warn": 0}
    for test in resource.tests:
        own[test["kind"]] += 1
        if test["kind"] in {"generic", "singular"}:
            severity[test["severity"]] += 1
    if resource.resource_type != "source":
        own.pop("freshness")
    if resource.resource_type != "model":
        own.pop("unit")

    record: dict[str, Any] = {
        "name": resource.label,
        "resource_type": resource.resource_type,
        "tested": any(own.values()),
        "own_tests": own,
        "severity": severity,
    }
    path = node.get("original_file_path")
    if path:
        record["path"] = str(path)
    if resource.resource_type == "model":
        materialized = _materialized(node)
        record["materialized"] = materialized
        record["ephemeral"] = materialized == "ephemeral"
        if materialized == "incremental":
            branch = sorted(resource.incremental_branch_tests)
            record["incremental"] = {
                "unit_tests": own["unit"],
                "incremental_branch_tests": branch,
                "incremental_branch_tested": bool(branch),
            }

    documented = _documented(node)
    unasserted = sorted(
        documented[key] for key in documented if key not in resource.asserted
    )
    undocumented = sorted(resource.undocumented_tested)
    columns: dict[str, Any] = {
        "documented": len(documented),
        "asserted": len(resource.asserted & set(documented)),
        "unasserted": unasserted,
        "undocumented_tested": undocumented,
    }
    record["columns"] = columns

    tests = sorted(
        resource.tests,
        key=lambda t: (
            t["kind"],
            t.get("column") or "",
            t.get("test") or t.get("name") or "",
        ),
    )
    record["tests"] = tests
    record["downstream_tests"] = {
        "count": len(resource.downstream),
        "on": sorted(resource.downstream_via),
    }
    return record


def _summary(
    resources: dict[str, _Resource],
    selected: set[str],
    totals: dict[str, int],
    severities: dict[str, int],
    *,
    scoped: bool,
) -> dict[str, Any]:
    prefix = "selected " if scoped else ""
    by_type: dict[str, list[_Resource]] = defaultdict(list)
    for unique_id in selected:
        by_type[resources[unique_id].resource_type].append(resources[unique_id])

    def own_tested(resource: _Resource) -> bool:
        return bool(resource.tests)

    def columns(group: list[_Resource], noun: str) -> dict[str, Any]:
        documented = sum(len(_documented(r.node)) for r in group)
        asserted = sum(len(r.asserted & set(_documented(r.node))) for r in group)
        return _ratio(
            asserted,
            documented,
            f"documented columns of {prefix}enabled project {noun}",
        )

    models = by_type["model"]
    incremental = [r for r in models if _materialized(r.node) == "incremental"]
    summary: dict[str, Any] = {
        "models": {
            "count": len(models),
            "ephemeral": sum(1 for r in models if _materialized(r.node) == "ephemeral"),
            "with_own_tests": _ratio(
                sum(1 for r in models if own_tested(r)),
                len(models),
                f"{prefix}enabled project models",
            ),
            "with_unit_tests": _ratio(
                sum(1 for r in models if any(t["kind"] == "unit" for t in r.tests)),
                len(models),
                f"{prefix}enabled project models",
            ),
            "downstream_only": _ratio(
                sum(1 for r in models if not own_tested(r) and r.downstream),
                len(models),
                f"{prefix}enabled project models",
            ),
            "incremental_branch_tested": _ratio(
                sum(1 for r in incremental if r.incremental_branch_tests),
                len(incremental),
                f"{prefix}enabled incremental project models",
            ),
            "columns_asserted": columns(models, "models"),
        }
    }
    for resource_type in ("source", "snapshot", "seed"):
        group = by_type[resource_type]
        noun = SECTIONS[resource_type]
        entry: dict[str, Any] = {
            "count": len(group),
            "with_own_tests": _ratio(
                sum(1 for r in group if own_tested(r)),
                len(group),
                f"{prefix}enabled project {noun}",
            ),
            "columns_asserted": columns(group, noun),
        }
        if resource_type == "source":
            entry["with_freshness"] = _ratio(
                sum(1 for r in group if any(t["kind"] == "freshness" for t in r.tests)),
                len(group),
                f"{prefix}enabled project sources",
            )
        summary[noun] = entry
    summary["tests"] = {"by_kind": dict(totals), "by_severity": dict(severities)}
    return summary


def _elision_notes(elided: dict[str, int]) -> list[str]:
    notes = [
        f"{elided[section]} {section} are not listed: each section is capped at "
        f"{RESOURCE_CAP}, untested ones first; the summary counts every one. "
        "Pass --full, or name resources, to list them"
        for section in ("models", "sources", "snapshots", "seeds")
        if elided[section]
    ]
    if elided["tests"]:
        notes.append(
            f"{elided['tests']} test(s) are not listed: each resource lists at "
            f"most {TEST_CAP}; its own_tests counts every one. Pass --full to "
            "list them"
        )
    if elided["columns"]:
        notes.append(
            f"{elided['columns']} column name(s) are not listed: each column "
            f"list is capped at {COLUMN_CAP}; the counts cover every column. "
            "Pass --full to list them"
        )
    if elided["downstream_resources"]:
        notes.append(
            f"{elided['downstream_resources']} downstream resource name(s) are "
            f"not listed: each resource names at most {DOWNSTREAM_CAP}; "
            "downstream_tests.count covers every test. Pass --full to list them"
        )
    if elided["excluded"]:
        notes.append(
            f"{elided['excluded']} excluded resource(s) are not named: the list "
            f"is capped at {EXCLUDED_CAP}; resource_count counts every one. "
            "Pass --full to name them"
        )
    return notes
