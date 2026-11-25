"""The semantic-layer objects that read an incident's column (#1478).

A diagnosis (#1475) says what broke and why. This says who reads it: the
semantic models, and their entities, dimensions and measures, that read the
column a failing test checks, and the metrics built on those measures.

It is a read of what ``maintain`` already fingerprints, not a new analysis.
:class:`~.snapshot.SemanticModelDef` maps each entity, dimension and measure to
the one physical column it references, and maps a computed expression to
``None`` rather than guessing a column out of it; :class:`~.snapshot.MetricDef`
names the measures and metrics each metric draws from. So the answer here is a
lookup, under three rules:

- **Absent is not empty.** A project whose semantic layer declares nothing
  reads ``absent``. ``read`` with no readers means the layer was read and no
  semantic model on the tested model reads the column.
- **The tested model's own semantic models.** A semantic model is matched to
  the model the failing test is attached to, not to the models built from it:
  one on a mart that selects the column from the tested staging model is not
  followed, and every ``read`` says so in its ``detail`` (:data:`SCOPE`).
- **An expression is never guessed.** A member on the tested model whose
  expression is computed is listed as ``unresolved``, with the metrics built on
  it where it is a measure: nothing here can say which column it reads, or
  whether it reads one (``count`` over ``1`` reads none). A semantic model
  whose ``model`` names no model Dex can read is listed the same way.
- **Metrics are reached through measures.** A metric whose input is a measure
  that reads the column reads it, and so does every metric built on that
  metric. A metric whose filter or grouping names a dimension is not traced:
  the layer's fingerprint records a metric's inputs, not its filters.

Pure logic: nothing here opens a connection or reads a file.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from .observations import Value

if TYPE_CHECKING:
    from .snapshot import SemanticLayerSnapshot

#: How the layer was read: ``read`` (the list stands, even empty), ``absent``
#: (the project declares no semantic layer), ``unreadable`` (a layer is there
#: and could not be read) or ``not_read`` (this release does not read it for
#: this incident, with the reason).
ReadStatus = Literal["read", "absent", "unreadable", "not_read"]

#: What a ``read`` covers, carried as its ``detail``: an empty list is about
#: the tested model's own semantic models, never the models built from it.
SCOPE = (
    "semantic models on the tested model only: a semantic model on a model "
    "built from it (a mart that selects the column) is not followed"
)

#: The kinds of member a semantic model holds that map to a column.
MemberKind = Literal["entity", "dimension", "measure"]

#: Why an object is listed as unresolved.
UnresolvedReason = Literal["computed_expression", "model_unresolved"]


class TestedColumn(Value):
    """One failing test's claim, as the diagnosis reads it: the test, the dbt
    model it is attached to (by name), and the columns it checks."""

    test: str
    model: str
    columns: tuple[str, ...]


class Reader(Value):
    """One member that reads a tested column, and the metrics built on it.

    ``metrics`` is set for a measure only, and holds every metric that draws on
    it, directly or through another metric, sorted by name.
    """

    semantic_model: str
    model: str
    kind: MemberKind
    name: str
    column: str
    metrics: tuple[str, ...] = ()


class Unresolved(Value):
    """An object on the tested model whose column cannot be resolved: it may
    read a tested column, another, or none.

    ``kind`` is ``semantic_model`` where the model it sits on could not be read,
    in which case ``model`` is ``None`` and nothing about its members is
    claimed. ``metrics`` follows :class:`Reader`.
    """

    semantic_model: str
    model: str | None = None
    kind: MemberKind | Literal["semantic_model"]
    name: str
    reason: UnresolvedReason
    metrics: tuple[str, ...] = ()


class SemanticReaders(Value):
    """The semantic-layer objects that read an incident's column.

    ``tested`` is what was looked up. ``readers`` and ``unresolved`` are the
    answer where ``status`` is ``read``, and empty otherwise. ``unread_files``
    names semantic-layer files that did not parse: where any is named the list
    may be missing objects, and with nothing else read the status is
    ``unreadable``, never ``absent``.
    """

    status: ReadStatus
    vendor: str | None = None
    reason: str | None = None
    detail: str | None = None
    tested: tuple[TestedColumn, ...] = ()
    readers: tuple[Reader, ...] = ()
    unresolved: tuple[Unresolved, ...] = ()
    unread_files: tuple[str, ...] = ()


def not_read(
    reason: str,
    detail: str,
    *,
    vendor: str | None = None,
    tested: Sequence[TestedColumn] = (),
) -> SemanticReaders:
    """The answer where this release does not read the layer for an incident."""

    return SemanticReaders(
        status="not_read",
        vendor=vendor,
        reason=reason,
        detail=detail,
        tested=tuple(tested),
    )


def unreadable(
    reason: str,
    detail: str,
    *,
    vendor: str | None = None,
    tested: Sequence[TestedColumn] = (),
    unread_files: Sequence[str] = (),
) -> SemanticReaders:
    """The answer where a layer is there and could not be read."""

    return SemanticReaders(
        status="unreadable",
        vendor=vendor,
        reason=reason,
        detail=detail,
        tested=tuple(tested),
        unread_files=tuple(sorted(unread_files)),
    )


def _metrics_on(layer: SemanticLayerSnapshot) -> dict[str, set[str]]:
    """Each measure's metrics, every metric built on them included."""

    direct: dict[str, set[str]] = {}
    built_on: dict[str, set[str]] = {}
    for metric in layer.metrics:
        for measure in metric.input_measures:
            direct.setdefault(measure, set()).add(metric.name)
        for parent in metric.input_metrics:
            built_on.setdefault(parent, set()).add(metric.name)
    reached: dict[str, set[str]] = {}
    for measure, first in direct.items():
        seen: set[str] = set()
        queue = sorted(first)
        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            queue.extend(sorted(built_on.get(name, ())))
        reached[measure] = seen
    return reached


def readers(
    layer: SemanticLayerSnapshot,
    tested: Sequence[TestedColumn],
    *,
    unread_files: Iterable[str] = (),
) -> SemanticReaders:
    """The objects in ``layer`` that read any of the ``tested`` columns.

    A semantic model is matched to a tested model by the model its ``model``
    names (``ref('stg_order_items')``), and a member to a column by the column
    the fingerprint mapped it to, both compared without case. A member on a
    tested model whose column is ``None`` is unresolved, and so is a semantic
    model whose ``model`` named nothing readable.
    """

    unread = tuple(sorted(set(unread_files)))
    vendor = layer.vendor
    if not layer.semantic_models and not layer.metrics:
        if unread:
            return unreadable(
                "unparsed_files",
                f"{len(unread)} semantic-layer file(s) did not parse, and nothing "
                "else in the layer was read",
                vendor=vendor,
                tested=tested,
                unread_files=unread,
            )
        return SemanticReaders(
            status="absent",
            vendor=vendor,
            reason="no_semantic_layer",
            detail="the project declares no semantic model or metric",
            tested=tuple(tested),
        )

    wanted: dict[str, set[str]] = {}
    for item in tested:
        wanted.setdefault(item.model.casefold(), set()).update(
            c.casefold() for c in item.columns
        )
    metrics = _metrics_on(layer)

    def metrics_of(kind: str, name: str) -> tuple[str, ...]:
        return tuple(sorted(metrics.get(name, ()))) if kind == "measure" else ()

    found: list[Reader] = []
    unresolved: list[Unresolved] = []
    for model in layer.semantic_models:
        if model.model_ref is None:
            unresolved.append(
                Unresolved(
                    semantic_model=model.name,
                    kind="semantic_model",
                    name=model.name,
                    reason="model_unresolved",
                )
            )
            continue
        columns = wanted.get(model.model_ref.casefold())
        if columns is None:
            continue
        members: Mapping[MemberKind, Mapping[str, str | None]] = {
            "entity": model.entities,
            "dimension": model.dimensions,
            "measure": model.measures,
        }
        for kind, mapping in members.items():
            for name, column in mapping.items():
                if column is None:
                    unresolved.append(
                        Unresolved(
                            semantic_model=model.name,
                            model=model.model_ref,
                            kind=kind,
                            name=name,
                            reason="computed_expression",
                            metrics=metrics_of(kind, name),
                        )
                    )
                elif column.casefold() in columns:
                    found.append(
                        Reader(
                            semantic_model=model.name,
                            model=model.model_ref,
                            kind=kind,
                            name=name,
                            column=column,
                            metrics=metrics_of(kind, name),
                        )
                    )

    def order(item: Reader | Unresolved) -> tuple[str, str, str]:
        return (item.semantic_model, item.kind, item.name)

    detail = SCOPE
    if unread:
        detail += (
            f"; {len(unread)} semantic-layer file(s) did not parse, so objects "
            "they hold are not listed"
        )
    return SemanticReaders(
        status="read",
        vendor=vendor,
        detail=detail,
        tested=tuple(tested),
        readers=tuple(sorted(found, key=order)),
        unresolved=tuple(sorted(unresolved, key=order)),
        unread_files=unread,
    )


__all__ = [
    "SCOPE",
    "MemberKind",
    "ReadStatus",
    "Reader",
    "SemanticReaders",
    "TestedColumn",
    "Unresolved",
    "UnresolvedReason",
    "not_read",
    "readers",
    "unreadable",
]
