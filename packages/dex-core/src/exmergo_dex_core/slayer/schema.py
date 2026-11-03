"""SLayer models held to the warehouse schema, by SLayer's own rules.

SLayer's ``validate-models`` compares each model against the live schema and
names the minimal removals that would make the set consistent again: a column
the table no longer has, a column whose type moved to another bucket, and
everything that leaned on either (a measure, a join, a filter, a whole model).
It does that through SLayer's own datasource connection, which carries
credentials dex does not read, so under dex it never runs.

Its rules run here instead, on metadata dex captured through its own
connection. ``diff_sql_table_model`` and ``compute_datasource_drops`` are
SLayer's pure entry points: they take a model and a schema, never a connection,
so this module hands them a schema built from dex's datasets and reads back
what they decided. Nothing here opens SLayer storage or a datasource, and the
comparison is exactly the one SLayer would make, rather than a second reading of
it that drifts from the first.

Two things are dex's own, because SLayer's introspection reaches them through
SQLAlchemy and dex's datasets do not:

- **Types.** dex carries each column's type as the connector spells it.
  :func:`data_type` maps that onto SLayer's ``DataType`` where the spelling is
  unambiguous, and a type it cannot place takes the declared type instead, so
  an unfamiliar spelling is never reported as a conflict.
- **Case.** A declared column matches a warehouse column case-insensitively
  when exactly one does, because a connector that folds unquoted names to upper
  case would otherwise report every column as missing.

**Which findings come out.** One per declared column SLayer would drop
(``declared_column_missing`` or ``declared_type_conflict``), one per model whose
table has gone since the baseline (``declared_relation_missing``), and one per
model that only loses something because another drop cascaded into it
(``declared_dependency_dropped``). Each carries the model's whole removal as
SLayer computed it, which is what ``maintain reconcile`` turns into a plan.
"""

from __future__ import annotations

# First, before any SLayer import: importing SLayer rewrites sqlglot's reserved
# keywords process-wide, and this module undoes that (see `sqlglot_keywords`).
from .sqlglot_keywords import slayer_keywords

# isort: split

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from slayer.core.enums import DataType
from slayer.core.models import is_base_column_sql, physical_column_sql
from slayer.engine.schema_drift import (
    EditModelDelete,
    LiveTable,
    WholeModelDelete,
    compute_datasource_drops,
    data_type_bucket,
    diff_sql_table_model,
)

from ..cache import match_identifier
from ..maintain.drift import DriftFinding
from ..semantic_types import NUMERIC, TEXT, UNPLACEABLE
from ..semantic_types import spelled as spelled_type
from . import catalog as catalog_mod

if TYPE_CHECKING:
    from ..cache import Dataset
    from .loader import LoadedModel, LoadResult

__all__ = ["data_type", "declared_schema_drift"]


def data_type(raw: str | None) -> DataType | None:
    """SLayer's type for a connector's spelling of one, or ``None``.

    Only where the spelling says it plainly, matching what SLayer's own
    introspection maps it to. ``None`` is "cannot tell", never "opaque": an
    opaque live type conflicts with any operable declared one under SLayer's
    rule, and a guess would turn every unfamiliar spelling into a finding.
    """

    spelled = spelled_type(raw)
    if not spelled or spelled.endswith("[]") or spelled.startswith(UNPLACEABLE):
        return None
    if spelled in ("BOOLEAN", "BOOL", "BIT", "LOGICAL"):
        return DataType.BOOLEAN
    if spelled in ("DATE", "DATE32"):
        return DataType.DATE
    if spelled.startswith(("TIME", "DATETIME", "SMALLDATETIME")):
        return DataType.TIMESTAMP
    if "INT" in spelled:
        return DataType.INT
    if spelled.startswith(NUMERIC):
        return DataType.DOUBLE
    if spelled.startswith(TEXT):
        return DataType.TEXT
    return None


def declared_schema_drift(
    loaded: LoadResult,
    current: list[Dataset],
    baseline: list[Dataset],
    *,
    connector: str | None,
) -> tuple[list[DriftFinding], list[str]]:
    """Every model SLayer would edit against this schema, as findings."""

    findings: list[DriftFinding] = []
    notes: list[str] = []
    current_ids = [d.identifier for d in current]
    baseline_ids = [d.identifier for d in baseline]
    by_id = {d.identifier: d for d in current}

    by_source: dict[str, list[LoadedModel]] = defaultdict(list)
    for entry in loaded.models:
        by_source[entry.model.data_source].append(entry)

    for entries in by_source.values():
        names = {entry.model.name for entry in entries}
        diffs: dict[str, tuple[Any, set[str]]] = {}
        located: dict[str, tuple[str, dict[str, str] | None]] = {}
        for entry in entries:
            model = entry.model
            if not model.sql_table:
                kind = "SQL" if model.sql else "source queries"
                notes.append(
                    f"SLayer model '{model.name}' is backed by {kind}, so its "
                    "columns were not compared: SLayer checks those by running "
                    "the query, and dex does not run SLayer queries"
                )
                continue
            relation, note = catalog_mod._relation(model, connector)
            if relation is None:
                notes.append(note or "")
                continue
            matches = match_identifier(relation, current_ids)
            if len(matches) > 1:
                notes.append(
                    f"SLayer model '{model.name}' reads {relation}, which "
                    f"matches several warehouse objects ({', '.join(matches)}), "
                    "so its columns were not compared"
                )
                continue
            if not matches:
                gone = match_identifier(relation, baseline_ids)
                if len(gone) != 1:
                    notes.append(
                        f"SLayer model '{model.name}' reads {relation}, which "
                        "neither this connection nor the baseline lists, so its "
                        "columns were not compared"
                    )
                    continue
                with slayer_keywords():
                    diffs[model.name] = diff_sql_table_model(
                        model=model, live_table=None, available_models_in_ds=names
                    )
                located[model.name] = (gone[0], None)
                continue
            dataset = by_id[matches[0]]
            if not dataset.columns:
                notes.append(
                    f"dex has no column detail for {dataset.identifier}, so "
                    f"SLayer model '{model.name}' was not compared against it"
                )
                continue
            live, spelled = _live_table(model, dataset)
            with slayer_keywords():
                diffs[model.name] = diff_sql_table_model(
                    model=model, live_table=live, available_models_in_ds=names
                )
            located[model.name] = (dataset.identifier, spelled)

        if not diffs:
            continue
        with slayer_keywords():
            drops = compute_datasource_drops(
                models=[entry.model for entry in entries],
                sql_table_diffs=diffs,
                sql_diffs={},
            )
        files = {entry.model.name: entry.file for entry in entries}
        models = {entry.model.name: entry.model for entry in entries}
        for drop in drops:
            name = drop.model_name
            removal = _removal(drop)
            base, _dropped = diffs.get(name, (None, set()))
            identifier, spelled = located.get(name, (None, None))
            context = {
                "semantic_model": name,
                "model_file": files.get(name),
                "removal": removal,
            }
            if base is None:
                findings.append(_cascaded(drop, identifier, context))
            elif isinstance(base, WholeModelDelete):
                findings.append(_relation_missing(models[name], identifier, context))
            else:
                findings.extend(
                    _column_findings(models[name], base, identifier, spelled, context)
                )
    return findings, [note for note in notes if note]


def _live_table(model: Any, dataset: Dataset) -> tuple[LiveTable, dict[str, str]]:
    """The schema SLayer's diff reads, keyed the way the model spells it.

    Returns the table and, for each declared physical column the warehouse
    holds, the warehouse's own spelling and type, which the findings quote.
    """

    live = {c.name: c for c in dataset.columns}
    folded: dict[str, list[str]] = defaultdict(list)
    for name in live:
        folded[name.lower()].append(name)

    columns: dict[str, DataType] = {}
    spelled: dict[str, str] = {}
    for col in model.columns:
        if not is_base_column_sql(col.sql):
            continue
        physical = physical_column_sql(sql=col.sql, name=col.name)
        found = physical if physical in live else None
        if found is None and len(folded.get(physical.lower(), [])) == 1:
            found = folded[physical.lower()][0]
        if found is None:
            continue
        # A type dex cannot place takes the declared one, so it never conflicts.
        columns[physical] = data_type(live[found].data_type) or col.type
        spelled[physical] = live[found].data_type
    return LiveTable(columns=columns), spelled


def _removal(drop: Any) -> dict[str, Any]:
    """What SLayer would remove from one model, as plain data a finding carries."""

    if isinstance(drop, WholeModelDelete):
        return {"whole_model": True}
    return {
        "whole_model": False,
        "columns": list(drop.remove.columns),
        "measures": list(drop.remove.measures),
        "aggregations": list(drop.remove.aggregations),
        "joins": [edge.model_dump(mode="json") for edge in drop.remove.join_edges],
        "filters": list(drop.remove_filters),
    }


def _consequence(removal: dict[str, Any]) -> str:
    """What else goes with a drop, for the finding's sentence."""

    parts = [
        f"{label} {', '.join(items)}"
        for label, items in (
            ("measures", removal.get("measures") or []),
            ("aggregations", removal.get("aggregations") or []),
            ("filters", removal.get("filters") or []),
        )
        if items
    ]
    joins = [edge["target_model"] for edge in removal.get("joins") or []]
    if joins:
        parts.append(f"joins to {', '.join(joins)}")
    return f"; SLayer would also drop {'; '.join(parts)}" if parts else ""


def _column_findings(
    model: Any,
    base: EditModelDelete,
    identifier: str | None,
    spelled: dict[str, str] | None,
    context: dict[str, Any],
) -> list[DriftFinding]:
    findings: list[DriftFinding] = []
    consequence = _consequence(context["removal"])
    for name in base.remove.columns:
        col = model.get_column(name)
        physical = physical_column_sql(sql=col.sql, name=col.name) if col else name
        declared = str(getattr(col.type, "value", col.type)) if col else None
        live_type = (spelled or {}).get(physical)
        if live_type is None:
            findings.append(
                DriftFinding(
                    axis="schema",
                    code="declared_column_missing",
                    identifier=identifier,
                    column=physical,
                    severity="high",
                    detail=(
                        f"SLayer model '{model.name}' declares column '{name}' on "
                        f"{identifier}, which no longer has a column '{physical}'"
                        f"{consequence}"
                    ),
                    data={**context, "declared_column": name},
                )
            )
            continue
        live = data_type(live_type)
        findings.append(
            DriftFinding(
                axis="schema",
                code="declared_type_conflict",
                identifier=identifier,
                column=physical,
                severity="high",
                detail=(
                    f"SLayer model '{model.name}' declares column '{name}' as "
                    f"{declared}, and {identifier}.{physical} is {live_type}, "
                    "which SLayer reads as another kind of value "
                    f"({data_type_bucket(col.type)} against "
                    f"{data_type_bucket(live) if live else 'unknown'})"
                    f"{consequence}"
                ),
                data={
                    **context,
                    "declared_column": name,
                    "declared_type": declared,
                    "warehouse_type": live_type,
                },
            )
        )
    if not findings and base.remove.join_edges:
        # A join dropped with no column of its own model dropped: SLayer found
        # its target missing from the set, which the loader already refuses, so
        # this is reached only if the two rules ever part.
        findings.append(_cascaded(base, identifier, context))
    return findings


def _relation_missing(
    model: Any, identifier: str | None, context: dict[str, Any]
) -> DriftFinding:
    return DriftFinding(
        axis="schema",
        code="declared_relation_missing",
        identifier=identifier,
        severity="high",
        detail=(
            f"SLayer model '{model.name}' reads {identifier}, which was in the "
            "baseline and is gone from the warehouse, so SLayer would drop the "
            "whole model"
        ),
        data=context,
    )


def _cascaded(
    drop: Any, identifier: str | None, context: dict[str, Any]
) -> DriftFinding:
    reasons = [f"{reason.target}: {reason.reason}" for reason in drop.reasons]
    whole = context["removal"].get("whole_model")
    return DriftFinding(
        axis="schema",
        code="declared_dependency_dropped",
        identifier=identifier,
        severity="medium",
        detail=(
            f"SLayer model '{drop.model_name}' "
            + (
                "would be dropped whole"
                if whole
                else "would lose part of its definition"
            )
            + " because something it depends on was dropped"
            + (f" ({'; '.join(reasons)})" if reasons else "")
        ),
        data={**context, "reasons": reasons},
    )
