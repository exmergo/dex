"""The columns singular tests read: ``maintain coverage``'s ``referenced`` tier (#1282).

A generic test is attached to a column, so the inventory can say the column is
``asserted``. A singular test is a SELECT that must return no rows, and it
names no column at all: ``select * from {{ ref('orders') }} where amount < 0``
asserts ``amount`` without saying so. This module reads each singular test's
SQL with the lineage reader ``transform plan`` drafts from, and reports every
column it reads, resolved to the resource it belongs to, with the role it is
read in: a ``predicate`` (WHERE, HAVING, QUALIFY, a CASE condition), a ``join``
key, an ``aggregation``, a ``grouping`` key or a plain ``projection``. A reader
judges from the role whether the test asserts anything about the column; a
join on ``customer_id`` does not.

**Never asserted.** A referenced column counts toward no ``asserted`` figure
and no headline ratio; its own ratio, beside the asserted one, says how many
documented columns only a singular test reads.

**Free.** ``maintain coverage`` reads a ``dbt parse`` manifest, which holds a
singular test's Jinja and not its compiled SQL. The test is rendered
statically, ``ref()``, ``source()`` and a ``var()`` with a literal default
substituted and nothing else; a test any other Jinja generates is read from
the newest compiled manifest on disk instead, only where that manifest holds
the test with the same checksum, so its SQL is the SQL of the tree as it
stands. Otherwise it is listed under ``unresolved`` with a reason code, never
dropped and never read as a test that reads nothing.

Names and counts only: no SQL text, expression or literal leaves this module.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .. import dbt_tests

#: Tests named per list before the cut; every list keeps its count.
TEST_NAME_CAP = 25
#: Tests named per referenced column.
TESTS_PER_COLUMN = 5

#: How a test was read: statically from its Jinja, or from a compiled
#: manifest a build left on disk.
READ_FROM = ("raw_code", "compiled_artifact")


def _reason_code(incomplete: list[str]) -> str:
    text = " ".join(incomplete).lower()
    if "sqlglot is not installed" in text:
        return "parser_unavailable"
    if "statement block" in text:
        return "jinja_block"
    if "could not read a relation name" in text:
        return "dynamic_relation"
    if "only dbt can render" in text or "entirely jinja" in text:
        return "macro_generated"
    if "did not parse" in text:
        return "unparseable"
    if "not a select" in text:
        return "not_a_select"
    return "unreadable"


def _identity(parts: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        part.strip().strip('"`[]').lower() for part in parts if part and part.strip()
    )


def _relation_parts(relation_name: str | None) -> tuple[str, ...]:
    if not relation_name:
        return ()
    return _identity(str(relation_name).replace("`", "").split("."))


def _node_of(manifest: dict[str, Any], unique_id: str) -> dict[str, Any] | None:
    return (manifest.get("nodes") or {}).get(unique_id) or (
        manifest.get("sources") or {}
    ).get(unique_id)


def _resolver(manifest: dict[str, Any], depends: list[str]):
    """Map a relation the reader names to one of the test's dependencies."""

    by_name: dict[str, list[str]] = defaultdict(list)
    by_relation: list[tuple[tuple[str, ...], str]] = []
    for unique_id in depends:
        node = _node_of(manifest, unique_id)
        if node is None:
            continue
        names = {str(node.get("name") or "").lower()}
        if node.get("resource_type") == "source":
            names.add(str(node.get("identifier") or "").lower())
        for name in names - {""}:
            by_name[name].append(unique_id)
        parts = _relation_parts(node.get("relation_name"))
        if parts:
            by_relation.append((parts, unique_id))

    def resolve(relation: str, is_ref: bool) -> tuple[str | None, str | None]:
        if is_ref:
            hits = sorted(set(by_name.get(relation.lower(), [])))
        else:
            wanted = _identity(relation.split("."))
            hits = sorted(
                {
                    uid
                    for parts, uid in by_relation
                    if wanted and parts[-len(wanted) :] == wanted
                }
            )
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "ambiguous_relation"
        return None, "unknown_relation"

    return resolve


def singular_reads(
    manifest: dict[str, Any],
    *,
    artifact: dict[str, Any] | None = None,
    dialect: str | None = None,
    full: bool = False,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    """What each enabled singular test of the root project reads.

    Returns the reads by resource unique id (lowercased column name to its
    ``name``, ``roles`` and ``tests``) and the ``singular_tests`` block: how
    many tests were read, from where, and every one read only in part or not
    at all, with a reason code.
    """

    from ..transform.drafting import read

    project = (manifest.get("metadata") or {}).get("project_name")
    compiled = (artifact or {}).get("nodes") or {}
    reads: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    counts = {"resolved": 0, "partial": 0, "unresolved": 0}
    read_from = dict.fromkeys(READ_FROM, 0)
    unresolved: list[dict[str, str]] = []
    partial: list[dict[str, str]] = []
    total = 0

    for unique_id, test in sorted((manifest.get("nodes") or {}).items()):
        if test.get("resource_type") != "test":
            continue
        if dbt_tests.data_test_kind(test) != "singular":
            continue
        if not dbt_tests.test_enabled(test):
            continue
        if project and test.get("package_name") != project:
            continue
        total += 1
        name = str(test.get("name") or unique_id.rsplit(".", 1)[-1])
        depends = list((test.get("depends_on") or {}).get("nodes") or [])
        raw = test.get("raw_code") or ""
        if not raw.strip():
            counts["unresolved"] += 1
            unresolved.append({"test": name, "reason_code": "no_sql"})
            continue

        reading = read(raw, None, dialect=dialect or "", vars=None, references=True)
        source = "raw_code"
        if not reading.referenced and reading.incomplete:
            prior = compiled.get(unique_id) or {}
            same = prior.get("checksum") and prior.get("checksum") == test.get(
                "checksum"
            )
            if same and prior.get("compiled_code"):
                again = read(
                    prior["compiled_code"],
                    None,
                    dialect=dialect or "",
                    vars=None,
                    references=True,
                )
                if again.referenced:
                    reading, source = again, "compiled_artifact"
        if not reading.referenced:
            counts["unresolved"] += 1
            code = (
                _reason_code(reading.incomplete)
                if reading.incomplete and not reading.unreferenced
                else "untraced_columns"
                if reading.unreferenced
                else "reads_no_column"
            )
            unresolved.append({"test": name, "reason_code": code})
            continue

        read_from[source] += 1
        resolve = _resolver(manifest, depends)
        problems: set[str] = set()
        for (relation, column, is_ref), roles in reading.referenced.items():
            owner, problem = resolve(relation, is_ref)
            if owner is None:
                problems.add(problem or "unknown_relation")
                continue
            entry = reads[owner].setdefault(
                column.lower(), {"name": column, "roles": set(), "tests": set()}
            )
            entry["roles"] |= roles
            entry["tests"].add(name)
        if reading.unreferenced:
            problems.add("untraced_columns")
        if problems:
            counts["partial"] += 1
            partial.append({"test": name, "reason_code": sorted(problems)[0]})
        else:
            counts["resolved"] += 1

    block: dict[str, Any] = {
        "count": total,
        "read": counts,
        "read_from": read_from,
        "unresolved": unresolved if full else unresolved[:TEST_NAME_CAP],
        "partial": partial if full else partial[:TEST_NAME_CAP],
        "elided": {
            "unresolved": 0 if full else max(0, len(unresolved) - TEST_NAME_CAP),
            "partial": 0 if full else max(0, len(partial) - TEST_NAME_CAP),
        },
    }
    return dict(reads), block


def referenced_entries(
    reads: dict[str, dict[str, Any]],
    documented: dict[str, str],
    asserted: set[str],
    *,
    full: bool = False,
) -> list[dict[str, Any]]:
    """One resource's referenced columns, as the resource record lists them."""

    entries = []
    for key, entry in reads.items():
        tests = sorted(entry["tests"])
        listed: dict[str, Any] = {
            "column": documented.get(key, entry["name"]),
            "documented": key in documented,
            "asserted": key in asserted,
            "roles": sorted(entry["roles"]),
            "tests": tests if full else tests[:TESTS_PER_COLUMN],
            "test_count": len(tests),
        }
        entries.append(listed)
    # The columns only a singular test reads first: they are what the
    # asserted figure cannot see.
    entries.sort(key=lambda e: (e["asserted"], not e["documented"], e["column"]))
    return entries
