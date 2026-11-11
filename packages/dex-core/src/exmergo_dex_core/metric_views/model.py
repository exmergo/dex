"""The validated shape of a Metric View, as :mod:`.validator` builds it.

Frozen, and each element keeps ``raw``, the mapping as written, so the
fingerprint hashes what the author wrote and a later authoring release can
rewrite one element without re-rendering its neighbours.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FIELD", "MEASURE", "Join", "Member", "MetricView", "Wildcard"]

FIELD = "field"
MEASURE = "measure"


@dataclass(frozen=True)
class Member:
    """A field (dimension) or a measure.

    ``name`` is as written, or derived from ``expr`` where the author left it
    out (``derived_name``), by the rule Databricks applies.
    """

    kind: str
    name: str
    expr: str
    comment: str | None = None
    display_name: str | None = None
    synonyms: tuple[str, ...] = ()
    format: dict[str, Any] | None = None
    window: tuple[dict[str, Any], ...] = ()
    partition: dict[str, Any] | None = None
    derived_name: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_time(self) -> bool:
        """Whether its ``format`` declares a date or a timestamp.

        Metric Views type no field, and this is the only statement in one that a
        field is temporal; dex reads nothing else as one.
        """

        return bool(self.format) and self.format.get("type") in {"date", "date_time"}


@dataclass(frozen=True)
class Join:
    """One join, at its place in the tree. ``path`` runs from the view's source
    to this join, so a nested join under ``customer`` named ``nation`` has the
    path ``("customer", "nation")``: the prefix a field uses to read its
    columns."""

    name: str
    path: tuple[str, ...]
    source: str
    on: str | None = None
    using: tuple[str, ...] | None = None
    cardinality: str = "many_to_one"
    at_most_one_match: bool | None = None
    joins: tuple[Join, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def dotted(self) -> str:
        return ".".join(self.path)

    @property
    def parent(self) -> tuple[str, ...]:
        return self.path[:-1]


@dataclass(frozen=True)
class Wildcard:
    """A ``source.*`` or ``<join>.* EXCEPT (...)`` entry, which Databricks expands
    when the view is created, from the warehouse schema. Named, never expanded."""

    kind: str
    expr: str


@dataclass(frozen=True)
class MetricView:
    """One configured file: one Metric View, named after the file's stem."""

    name: str
    file: str
    version: str
    source: str
    comment: str | None = None
    filter: str | None = None
    fields: tuple[Member, ...] = ()
    measures: tuple[Member, ...] = ()
    joins: tuple[Join, ...] = ()
    parameters: tuple[dict[str, Any], ...] = ()
    materialization: dict[str, Any] | None = None
    wildcards: tuple[Wildcard, ...] = ()
    #: Which spelling the file used for its field list: ``fields`` or the
    #: backward-compatible ``dimensions``.
    fields_key: str = "fields"
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def all_joins(self) -> Iterator[Join]:
        """Every join, parents before their children."""

        pending = list(self.joins)
        while pending:
            join = pending.pop(0)
            yield join
            pending.extend(join.joins)

    def join(self, path: tuple[str, ...]) -> Join | None:
        """The join at ``path``, matched ignoring case, as Databricks matches."""

        wanted = tuple(p.lower() for p in path)
        return next(
            (j for j in self.all_joins() if tuple(p.lower() for p in j.path) == wanted),
            None,
        )

    def member(self, name: str) -> Member | None:
        lowered = name.lower()
        return next(
            (m for m in (*self.fields, *self.measures) if m.name.lower() == lowered),
            None,
        )

    def measure(self, name: str) -> Member | None:
        lowered = name.strip("`").lower()
        return next((m for m in self.measures if m.name.lower() == lowered), None)
