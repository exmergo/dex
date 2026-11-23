"""The Elementary package's own tables, left out of ranking (#1311).

A project that installs the ``elementary-data/elementary`` dbt package gets a
schema of the package's metadata tables, written on every run
(``dbt_run_results``, ``data_monitoring_metrics``, ``elementary_test_results``).
They are large and they connect to each other, so on size and shape alone they
can outrank the project's own tables in ``explore inventory --rank`` and
``explore map``, and a map that profiles its top objects spends its scans there.

This module recognises those tables from the warehouse alone, by their
signature: a namespace (schema or dataset) holding at least
:data:`MIN_SIGNATURE` of the package's table names, one of them a name only the
package uses (:data:`ANCHORS`). In such a namespace, each object named like one
of the package's models, or like the ``<name>__tmp_<timestamp>`` tables the
package leaves behind on warehouses that keep them (BigQuery), is Elementary's.
Nothing else in the namespace is, so a project that builds the package into its
own schema keeps its own tables ranked.

The signature is read from the warehouse rather than the project on purpose:
exploration starts bare (see ``_project_definitions``), the package's schema is
whatever the root project's ``+schema`` names (the package sets none of its own),
and a manifest parsed for the dev target names the dev namespace, never the
production one being explored. The names are the union of the package's models
at releases 0.15.2, 0.16.4, 0.23.0 and 0.26.1, read from each release's
``models/`` tree.

``explore diagram`` and ``explore relationships`` apply the same rule (#1494):
the diagram reads the signature over the cache's identifiers (so it has to be
there: a lone package table profiled into a cache holding no other entry of the
package's is below it) and draws none of the package's tables, and relationship
inference neither profiles them nor infers joins among them, so a metered
connector's estimate prices none of them.

Leaving a table out of ranking hides nothing: it stays in an unranked
``explore inventory``, and ``explore profile`` and ``explore query`` read it as
they read any other table.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..adapters.base import ObjectMeta
from .summary import (
    MAX_SKIPPED_NAMESPACES,
    MAX_SKIPPED_OBJECTS,
    ExcludedObjects,
    SkippedNamespace,
)

#: Every model the package builds, at 0.15.2, 0.16.4, 0.23.0 and 0.26.1
#: (``dbt_groups`` from 0.23.0, ``seed_run_results`` from 0.16), each built under
#: its own name: none of them sets an ``alias``.
TABLE_NAMES: frozenset[str] = frozenset(
    {
        "alerts_anomaly_detection",
        "alerts_dbt_models",
        "alerts_dbt_source_freshness",
        "alerts_dbt_tests",
        "alerts_schema_changes",
        "anomaly_threshold_sensitivity",
        "data_monitoring_metrics",
        "dbt_artifacts_hashes",
        "dbt_columns",
        "dbt_exposures",
        "dbt_groups",
        "dbt_invocations",
        "dbt_metrics",
        "dbt_models",
        "dbt_run_results",
        "dbt_seeds",
        "dbt_snapshots",
        "dbt_source_freshness_results",
        "dbt_sources",
        "dbt_tests",
        "elementary_test_results",
        "job_run_results",
        "metadata",
        "metrics_anomaly_score",
        "model_run_results",
        "monitors_runs",
        "schema_columns_snapshot",
        "seed_run_results",
        "snapshot_run_results",
        "test_result_rows",
    }
)

#: Names only this package uses. A namespace holding ``dbt_models``,
#: ``dbt_tests`` and ``metadata`` alone could be anyone's; one of these settles it.
ANCHORS: frozenset[str] = frozenset(
    {
        "elementary_test_results",
        "data_monitoring_metrics",
        "schema_columns_snapshot",
        "dbt_artifacts_hashes",
        "metrics_anomaly_score",
        "anomaly_threshold_sensitivity",
        "alerts_anomaly_detection",
        "alerts_schema_changes",
        "alerts_dbt_tests",
        "alerts_dbt_models",
        "alerts_dbt_source_freshness",
        "monitors_runs",
        "test_result_rows",
    }
)

#: How many of :data:`TABLE_NAMES` a namespace must hold, one an anchor. Three,
#: because a single enabled run of the package builds every model, and a lone
#: ``elementary_test_results`` copied somewhere is not the package's schema.
MIN_SIGNATURE = 3

#: The intermediate relations the package's incremental uploads and tests leave
#: on a warehouse that keeps them (``data_monitoring_metrics__tmp_<timestamp>``,
#: ``accepted_values_fct_band__large__tmp_<timestamp>``, seen on BigQuery). dbt's
#: own suffix is ``__dbt_tmp``, which this does not match.
_TEMP_TABLE = re.compile(r"__tmp_\d{14,}$", re.IGNORECASE)

#: The reason ``data.excluded`` reports, so a later package can share the block.
REASON = "elementary"

#: Caps on what ``data.excluded`` names, the same as ``data.skipped``'s.
MAX_OBJECTS = MAX_SKIPPED_OBJECTS
MAX_NAMESPACES = MAX_SKIPPED_NAMESPACES


@dataclass(frozen=True)
class ElementaryTables:
    """The objects recognised as Elementary's, and the namespaces holding them.

    ``identifiers`` is every recognised object, in inventory order.
    ``namespaces`` maps each namespace that carries the signature to how many
    objects in it were recognised.
    """

    identifiers: tuple[str, ...] = ()
    namespaces: dict[str, int] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.identifiers)

    @property
    def lowered(self) -> frozenset[str]:
        """``identifiers`` lowered, for a case-blind membership test."""

        return frozenset(i.lower() for i in self.identifiers)

    def excluded(self) -> ExcludedObjects:
        """The bounded ``data.excluded`` block for these objects."""

        grouped = sorted(self.namespaces.items(), key=lambda item: (-item[1], item[0]))
        return ExcludedObjects(
            count=len(self.identifiers),
            reason=REASON if self.identifiers else None,
            by_namespace=[
                SkippedNamespace(namespace=name, count=n)
                for name, n in grouped[:MAX_NAMESPACES]
            ],
            objects=sorted(self.identifiers)[:MAX_OBJECTS],
            elided=max(len(self.identifiers) - MAX_OBJECTS, 0),
            elided_namespaces=max(len(grouped) - MAX_NAMESPACES, 0),
        )


def recognise(
    metas: Iterable[ObjectMeta], cached: Iterable[str] = ()
) -> ElementaryTables:
    """The objects in ``metas`` that are Elementary's, by the table signature.

    ``cached`` adds identifiers the warehouse listing did not return but the
    exploration cache still holds, such as profiles ``explore map`` carries
    forward from outside a narrower ``--scope``. They are read by the same rule,
    their name being the identifier's last part, so a package table cached by an
    earlier ``--include-elementary`` run is recognised however a later run is
    scoped.

    Pure and free: names only, no connection, no column read.
    """

    members: list[tuple[str, str]] = [(m.identifier, m.name) for m in metas]
    members += [(i, i.rpartition(".")[2]) for i in cached]
    by_namespace: dict[str, list[tuple[str, str]]] = {}
    for identifier, name in members:
        namespace = identifier.rpartition(".")[0] or identifier
        by_namespace.setdefault(namespace, []).append((identifier, name))

    identifiers: list[str] = []
    namespaces: dict[str, int] = {}
    for namespace, group in by_namespace.items():
        known = {name.lower() for _, name in group} & TABLE_NAMES
        if len(known) < MIN_SIGNATURE or not known & ANCHORS:
            continue
        matched = [
            identifier
            for identifier, name in group
            if name.lower() in TABLE_NAMES or _TEMP_TABLE.search(name)
        ]
        identifiers.extend(matched)
        namespaces[namespace] = len(matched)
    return ElementaryTables(identifiers=tuple(identifiers), namespaces=namespaces)


#: How many namespaces the note names before counting the rest, the same cap
#: ``data.excluded.by_namespace`` keeps.
MAX_NOTE_NAMESPACES = MAX_NAMESPACES


def note(
    found: ElementaryTables,
    *,
    command: str,
    left_out_of: str = "the ranking",
    why: str = "their size says nothing about the project's own tables",
    way_back: str = "rank them with the rest",
) -> str:
    """The sentence ``notes`` carries when a command left Elementary's tables out.

    ``left_out_of``, ``why`` and ``way_back`` say what the command did without
    them and what ``--include-elementary`` does: ranking's words by default, the
    diagram's and relationship inference's where those commands call (#1494).
    """

    names = sorted(found.namespaces)
    where = ", ".join(names[:MAX_NOTE_NAMESPACES])
    if len(names) > MAX_NOTE_NAMESPACES:
        where += f", and {len(names) - MAX_NOTE_NAMESPACES} more namespace(s)"
    count = len(found.identifiers)
    return (
        f"left {count} table(s) of the Elementary dbt package out of {left_out_of} "
        f"(in {where}): the package writes them on every run, so {why}. "
        "data.excluded names them. They stay readable: `explore inventory` "
        "without --rank lists them, and `explore profile` and `explore query` "
        f"read them. Pass --include-elementary to {way_back} in `{command}`"
    )
