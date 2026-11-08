"""A declared foreign key against the parent it names, with no dbt artifacts (#1197).

A ``relationships`` test states containment: every non-NULL value of the child
column exists in the parent column. dbt runs that test itself and `maintain
verify` reads the result through ``build_status``. A project format with no dbt
``target/`` has no result to read, so for such a format each
:class:`~..project_definitions.DeclaredForeignKey` is measured here directly,
with the aggregate coverage probe the join contract already runs
(:func:`~..explore.relationships.measure_coverage`). One record per declared key
names the key (``model``, ``column``, ``to_model``, ``to_column``, the fields of
the declaration itself) and its ``status``, so two keys between the same pair of
models stay apart.

Placement is the format's. A model is read where ``model_relations`` puts it,
the way verify's relation and grain checks place it, and a parent the project
reads but does not build where ``source_relations`` does. A key whose model or
parent the format places nowhere is recorded unchecked rather than resolved by
name: a same-named table elsewhere in the warehouse is not the relation the
project declared.

The bound is the declaration's own. A foreign key is a containment claim, so
one unmatched non-NULL value contradicts it (``threshold_source: contract``),
as dbt fails the test on one row and as the join contract already judges a
required join. NULL child values are excluded and counted apart, as dbt's
``relationships`` test excludes them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..cache import Relationship, RelationshipKind, match_identifier
from ..envelope import failure_text
from ..project_definitions import DeclaredForeignKey, ProjectDefinitions
from .drift import DriftFinding

if TYPE_CHECKING:
    from ..results import ConfirmationRequest

#: The finding class `maintain verify` names in ``suppressed`` when no key ran.
CLASS = "foreign_key_contract"

#: What a declared foreign key tolerates: no unmatched non-NULL value.
_THRESHOLDS = {"unmatched_rows": 0}


@dataclass
class ForeignKeyPlan:
    """The declared keys, placed and resolved before anything is priced.

    ``records`` holds the keys already decided without a scan (unplaced, not
    in the warehouse, a column it does not have); ``probes`` pairs each
    measurable key's record with the relationship the probe reads."""

    records: list[dict] = field(default_factory=list)
    probes: list[tuple[dict, Relationship]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def declared(self) -> int:
        return len(self.records) + len(self.probes)


@dataclass
class ForeignKeyResult:
    """The axis's answer: a finding per contradicted key, and one record per
    declared key whatever its status."""

    findings: list[DriftFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    offer: ConfirmationRequest | None = None
    records: list[dict] = field(default_factory=list)


def _placement(
    definitions: ProjectDefinitions, name: str, stated: str | None
) -> str | None:
    """Where the format puts ``name``: its built relation (a versioned model's
    bare name read as the version ``ref()`` resolves to), else the relation it
    reads as a source, else the relation the key itself states, else None."""

    label = definitions.version_defaults.get(name, name)
    return (
        definitions.model_relations.get(label)
        or definitions.source_relations.get(name)
        or stated
    )


def _record(
    fk: DeclaredForeignKey, relation: str | None, to_relation: str | None
) -> dict[str, Any]:
    return {
        "model": fk.model,
        "column": fk.column,
        "to_model": fk.to_model,
        "to_column": fk.to_column,
        "source": fk.source,
        "relation": relation,
        "to_relation": to_relation,
        "null_keys": "excluded",
    }


def _unchecked(record: dict, code: str, reason: str) -> dict:
    record.update(status="unchecked", reason_code=code, reason=reason)
    return record


def _label(fk: DeclaredForeignKey) -> str:
    return f"{fk.model}.{fk.column} -> {fk.to_model}.{fk.to_column}"


def plan(
    definitions: ProjectDefinitions,
    adapter,
    live_identifiers: list[str],
    *,
    scope: set[str] | None = None,
) -> ForeignKeyPlan:
    """Each declared key placed, resolved to one live relation per side, and its
    columns read from metadata in the warehouse's own spelling (the probe
    quotes what it is given). Free: a schema lookup per relation, never a scan.

    ``scope`` is verify's lowered selection; a key belongs to its child model,
    which is also the identifier its finding carries. One key declared through
    two channels is planned once.
    """

    from ..guards.cost_guard import CostGuardError

    out = ForeignKeyPlan()
    described: dict[str, dict[str, str] | str] = {}

    def columns_of(identifier: str) -> dict[str, str] | str:
        """The relation's columns keyed by lowered name, or why they could not
        be read."""

        if identifier not in described:
            try:
                _meta, columns = adapter.table_metadata(identifier)
            except CostGuardError:
                raise
            except Exception as exc:  # one unreadable relation is a record
                described[identifier] = failure_text(exc)
            else:
                described[identifier] = {c.name.lower(): c.name for c in columns}
        return described[identifier]

    seen: set[tuple[str, ...]] = set()
    for fk in definitions.foreign_keys:
        if scope is not None and fk.model.lower() not in scope:
            continue
        key = tuple(
            part.lower() for part in (fk.model, fk.column, fk.to_model, fk.to_column)
        )
        if key in seen:
            continue
        seen.add(key)
        relation = _placement(definitions, fk.model, fk.relation)
        to_relation = _placement(definitions, fk.to_model, fk.to_relation)
        record = _record(fk, relation, to_relation)

        if relation is None or to_relation is None:
            unplaced = [
                name
                for name, placed in ((fk.model, relation), (fk.to_model, to_relation))
                if placed is None
            ]
            out.records.append(
                _unchecked(
                    record,
                    "no_declared_relation",
                    f"the project format places {' and '.join(unplaced)} in no "
                    "warehouse relation, so the key was not resolved by name",
                )
            )
            continue

        sides: list[tuple[str, str]] = []
        for name, placed, column in (
            (fk.model, relation, fk.column),
            (fk.to_model, to_relation, fk.to_column),
        ):
            matches = match_identifier(placed, live_identifiers)
            if len(matches) != 1:
                _unchecked(
                    record,
                    "relation_not_found",
                    f"{name}'s relation {placed} matched "
                    + ("more than one" if matches else "no")
                    + " relation in the warehouse",
                )
                break
            columns = columns_of(matches[0])
            if isinstance(columns, str):
                _unchecked(
                    record,
                    "metadata_unreadable",
                    f"the columns of {matches[0]} could not be read ({columns})",
                )
                break
            spelled = columns.get(column.lower())
            if spelled is None:
                _unchecked(
                    record,
                    "column_not_found",
                    f"{matches[0]} has no column {column}",
                )
                break
            sides.append((matches[0], spelled))
        if "status" in record:
            out.records.append(record)
            continue
        (child, child_column), (parent, parent_column) = sides
        out.probes.append(
            (
                record,
                Relationship(
                    from_dataset=child,
                    from_columns=[child_column],
                    to_dataset=parent,
                    to_columns=[parent_column],
                    kind=RelationshipKind.DECLARED,
                    confidence=1.0,
                    declaration_sources=[fk.source],
                ),
            )
        )

    unmeasured = [r for r in out.records if r["status"] == "unchecked"]
    if unmeasured:
        out.notes.append(
            "declared foreign key(s) were not measured: "
            + "; ".join(
                f"{r['model']}.{r['column']} -> {r['to_model']}.{r['to_column']} "
                f"({r['reason_code']})"
                for r in unmeasured
            )
        )
    return out


def cost(adapter, foreign_keys: ForeignKeyPlan) -> tuple[float, int]:
    """``(estimate, statement_count)`` for measuring every planned key, without
    running anything, so verify's one combined handshake can price this axis
    beside the others before anything asks."""

    from ..explore import relationships as rel_mod
    from .verify import join_probe_cost

    relationships = [rel for _record, rel in foreign_keys.probes]
    if not relationships:
        return 0.0, 0
    estimate, _probes = join_probe_cost(adapter, relationships)
    return estimate, len(rel_mod.coverage_statements(relationships, adapter.dialect))


def coverage(
    adapter,
    foreign_keys: ForeignKeyPlan,
    *,
    timeout_seconds: float = 30.0,
    handshake: Callable[[float, int], ConfirmationRequest | None],
) -> ForeignKeyResult:
    """Every planned key measured and judged, or recorded as why it was not.

    ``handshake`` is the ask verify's combined handshake hands this axis; a
    returned offer leaves every measurable key ``unchecked`` with
    ``scan_offered``, never satisfied.
    """

    from ..explore import relationships as rel_mod

    result = ForeignKeyResult(
        notes=list(foreign_keys.notes), records=list(foreign_keys.records)
    )
    if not foreign_keys.probes:
        return result

    estimate, statements = cost(adapter, foreign_keys)
    offer = handshake(estimate, statements)
    if offer is not None:
        result.offer = offer
        result.notes.append(
            f"{len(foreign_keys.probes)} declared foreign key(s) were not measured: "
            "their coverage probe is a scan, offered above"
        )
        for record, _rel in foreign_keys.probes:
            result.records.append(
                _unchecked(
                    record, "scan_offered", "measuring it is a scan, offered above"
                )
            )
        return result

    measured, refused = rel_mod.measure_coverage(
        adapter,
        [rel for _record, rel in foreign_keys.probes],
        timeout_seconds=timeout_seconds,
    )
    counts_of = {id(rel): counts for rel, counts in measured}
    refused_of = {id(rel): text for rel, text in refused}
    for record, rel in foreign_keys.probes:
        counts = counts_of.get(id(rel))
        if counts is None:
            reason = refused_of.get(id(rel), "the coverage probe returned no row")
            result.notes.append(f"{record['model']}.{record['column']}: {reason}")
            result.records.append(_unchecked(record, "probe_refused", reason))
            continue
        finding = _judge(record, rel, counts)
        if finding is not None:
            result.findings.append(finding)
        result.records.append(record)
    return result


def _judge(record: dict, rel: Relationship, counts) -> DriftFinding | None:
    """Fill ``record`` with the counts and the verdict; return the finding a
    contradicted key earns."""

    eligible_rows, matched_rows = counts.eligible_rows, counts.matched_rows
    eligible_keys, matched_keys = counts.eligible_keys, counts.matched_keys
    unmatched_rows = eligible_rows - matched_rows
    unmatched_keys = eligible_keys - matched_keys
    record["measured"] = {
        "rows": counts.rows,
        "null_key_rows": counts.null_key_rows,
        "eligible_rows": eligible_rows,
        "matched_rows": matched_rows,
        "unmatched_rows": unmatched_rows,
        "eligible_keys": eligible_keys,
        "matched_keys": matched_keys,
        "unmatched_keys": unmatched_keys,
    }
    record["coverage"] = {
        "rows": round(matched_rows / eligible_rows, 4) if eligible_rows else None,
        "keys": round(matched_keys / eligible_keys, 4) if eligible_keys else None,
    }
    record["threshold_source"] = "contract"
    record["thresholds"] = dict(_THRESHOLDS)
    child = f"{rel.from_dataset}.{rel.from_columns[0]}"
    parent = f"{rel.to_dataset}.{rel.to_columns[0]}"
    if eligible_rows == 0:
        record.update(
            status="not_applicable",
            reason_code="empty_input" if counts.rows == 0 else "all_keys_null",
            reason=(
                f"{rel.from_dataset} has no rows, so the key has no coverage to "
                "judge (undefined, not complete)"
                if counts.rows == 0
                else f"every {child} value is NULL, and a foreign key does not "
                "constrain NULLs, so nothing is eligible"
            ),
        )
        return None
    if unmatched_rows == 0:
        record["status"] = "satisfied"
        return None

    record["status"] = "contradicted"
    record["finding"] = "foreign_key_orphans"
    detail = (
        f"'{record['model']}.{record['column']}' is declared a foreign key to "
        f"'{record['to_model']}.{record['to_column']}' ({record['source']}), but "
        f"{unmatched_rows} of {eligible_rows} non-NULL row(s) and {unmatched_keys} "
        f"of {eligible_keys} distinct value(s) of {child} have no match in {parent}"
    )
    if counts.null_key_rows:
        detail += f"; {counts.null_key_rows} NULL row(s) are not constrained"
    return DriftFinding(
        axis=CLASS,
        code="foreign_key_orphans",
        identifier=record["model"],
        column=record["column"],
        severity="high",
        detail=detail,
        data={
            "model": record["model"],
            "column": record["column"],
            "to_model": record["to_model"],
            "to_column": record["to_column"],
            "source": record["source"],
            "from_relation": rel.from_dataset,
            "to_relation": rel.to_dataset,
            **record["measured"],
            "orphan_fraction": round(unmatched_rows / eligible_rows, 4),
            "threshold_source": "contract",
            "threshold": 0,
        },
    )
