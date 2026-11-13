"""Which tests dbt runs for a resource, read from the manifest alone.

Shared by ``transform test --mutate`` (which tests to run against a mutant, and
whether any of them takes an incremental model's ``is_incremental()`` branch),
the build's ``incremental_replay`` obligation, and ``maintain coverage``
(#1280), which inventories the same attachments project-wide. Every function
here is a pure read of a parsed ``manifest.json``: no dbt invocation, no
connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple


def attached_tests(manifest: dict, node: dict) -> list[str]:
    """Every test dbt would run for this model, generic, singular and unit alike.

    Read from the manifest rather than from a run, because it is the free answer
    and it is what makes "this model has no tests" a refusal instead of N dbt
    invocations that all report a survivor.
    """

    unique_id = node.get("unique_id")
    attached = [
        uid
        for uid, other in manifest.get("nodes", {}).items()
        if other.get("resource_type") == "test"
        and (
            other.get("attached_node") == unique_id
            or unique_id in (other.get("depends_on") or {}).get("nodes", [])
        )
    ]
    attached += [
        uid
        for uid, unit in (manifest.get("unit_tests") or {}).items()
        if unique_id in (unit.get("depends_on") or {}).get("nodes", [])
        or unit.get("model") == node.get("name")
    ]
    return attached


class IncrementalUnitTest(NamedTuple):
    """One unit test that runs the incremental branch, as dbt knows it.

    ``name`` is the test as a run reports it, ``select`` the name dbt's
    ``unit_test:`` selector matches. They differ on a versioned model: dbt
    reports each version's instance as ``<name>_v<version>`` while the
    selector matches the definition's ``<name>`` and so every version of it,
    which is why a run's statuses are read for ``name`` alone.
    """

    unique_id: str
    name: str
    select: str


def covers(unit: dict, node: dict) -> bool:
    """Whether this unit test runs ``node`` rather than another version of it.

    dbt binds each unit test instance to one model through ``depends_on``,
    one instance per version on a versioned model. The model name is a
    fallback only for an unversioned model, where it cannot name another one.
    """

    if node.get("unique_id") in (unit.get("depends_on") or {}).get("nodes", []):
        return True
    return node.get("version") is None and unit.get("model") == node.get("name")


def incremental_unit_tests(manifest: dict, node: dict) -> list[IncrementalUnitTest]:
    """The unit tests on this model that run its ``is_incremental()`` branch.

    dbt runs the branch in a unit test only where the test overrides the
    ``is_incremental`` macro to true; every other test runs the full build.
    On a versioned model, only the instances that test this version.
    """

    found = []
    for unique_id, unit in (manifest.get("unit_tests") or {}).items():
        if not covers(unit, node) or not unit.get("name"):
            continue
        value = ((unit.get("overrides") or {}).get("macros") or {}).get(
            "is_incremental"
        )
        if value is not True and str(value).lower() != "true":
            continue
        version = unit.get("version")
        found.append(
            IncrementalUnitTest(
                unique_id=unique_id,
                name=unit["name"] if version is None else f"{unit['name']}_v{version}",
                select=unit["name"],
            )
        )
    return sorted(found, key=lambda test: test.name)


def runs_full_build(
    manifest: dict, node: dict, incremental: Sequence[IncrementalUnitTest]
) -> bool:
    """Whether any test of the model's own runs its full build.

    Compared by unique id, which is what tells one version's unit test from
    another's: a unit test that tests another version of the model, or one that
    runs the incremental branch, does not count.
    """

    branch = {test.unique_id for test in incremental}
    units = manifest.get("unit_tests") or {}
    return any(
        unique_id not in branch
        and (unique_id not in units or covers(units[unique_id], node))
        for unique_id in attached_tests(manifest, node)
    )


# --- what coverage reads (#1280) -------------------------------------------

#: The kinds a test is counted under, apart: a generic data test (a macro
#: applied in YAML, ``test_metadata`` set), a singular data test (a SELECT file
#: under the test paths), a native unit test, and a source's freshness check.
TEST_KINDS = ("generic", "singular", "unit", "freshness")


def data_test_kind(test: dict[str, Any]) -> str:
    """``generic`` or ``singular`` for a manifest test node."""

    return "generic" if test.get("test_metadata") else "singular"


def generic_test_name(test: dict[str, Any]) -> str:
    """The generic test as its YAML names it: ``not_null``, ``dbt_utils.x``.

    Never the node's own name, which dbt builds from the test's arguments, so an
    ``accepted_values`` test is named after the values it accepts.
    """

    metadata = test.get("test_metadata") or {}
    name = str(metadata.get("name") or "test")
    namespace = metadata.get("namespace")
    return f"{namespace}.{name}" if namespace else name


def test_severity(test: dict[str, Any]) -> str:
    """The configured severity, lowercased: dbt writes ``ERROR`` by default."""

    severity = str((test.get("config") or {}).get("severity") or "error").lower()
    return severity if severity in {"error", "warn"} else "error"


def test_enabled(test: dict[str, Any]) -> bool:
    return (test.get("config") or {}).get("enabled") is not False


def generic_test_targets(test: dict[str, Any]) -> list[str]:
    """The resources a generic test is attached to.

    ``attached_node`` names it on a model, a seed or a snapshot. A test on a
    source carries none, and its only dependency is the source it tests.
    """

    attached = test.get("attached_node")
    if attached:
        return [attached]
    reads = (test.get("depends_on") or {}).get("nodes") or []
    return list(reads) if len(reads) == 1 else []


def test_reads(test: dict[str, Any]) -> list[str]:
    """Every resource a data test reads, by unique id."""

    return list((test.get("depends_on") or {}).get("nodes") or [])


def freshness_policy(source: dict[str, Any]) -> str | None:
    """``error`` or ``warn`` where a source declares a freshness check, else None.

    dbt writes the policy on the source and, from 1.10, under its ``config``
    too; a period with no ``count`` sets nothing.
    """

    config = source.get("config") or {}
    for policy in (source.get("freshness"), config.get("freshness")):
        if not isinstance(policy, dict):
            continue
        if ((policy.get("error_after") or {}).get("count")) is not None:
            return "error"
        if ((policy.get("warn_after") or {}).get("count")) is not None:
            return "warn"
    return None
