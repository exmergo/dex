"""The validated shape of a Snowflake Semantic View, as :mod:`.validator` builds it.

Frozen, and each element keeps ``raw``, the mapping as written, so the
fingerprint hashes what the author wrote and a later authoring release can
rewrite one element without re-rendering its neighbours.

Snowflake identifiers are case-insensitive unless quoted, and a view read back
from Snowflake spells every name in upper case, so every lookup here matches
names ignoring case. A name keeps the spelling it was written in.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DERIVED",
    "DIMENSION",
    "FACT",
    "METRIC",
    "ROW_LEVEL",
    "TIME_DIMENSION",
    "ColumnPair",
    "LogicalTable",
    "Member",
    "Relationship",
    "SemanticView",
]

DIMENSION = "dimension"
TIME_DIMENSION = "time_dimension"
FACT = "fact"
METRIC = "metric"
#: A view-level (derived) metric, which belongs to no logical table.
DERIVED = "derived_metric"

#: The kinds that hold one value per row of their table.
ROW_LEVEL = frozenset({DIMENSION, TIME_DIMENSION, FACT})

PUBLIC = "public_access"
PRIVATE = "private_access"


@dataclass(frozen=True)
class Member:
    """A dimension, time dimension, fact, table metric or derived metric.

    ``table`` is the logical table it belongs to, ``None`` for a derived metric.
    ``sample_value_count`` is how many ``sample_values`` it carries: their
    content is never kept, since Snowflake does not mask them.
    """

    kind: str
    name: str
    expr: str
    table: str | None = None
    description: str | None = None
    synonyms: tuple[str, ...] = ()
    data_type: str | None = None
    access: str = PUBLIC
    is_enum: bool = False
    sample_value_count: int = 0
    non_additive: tuple[dict[str, Any], ...] = ()
    using_relationships: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    #: Whether the expression is a window function (set by the loader once the
    #: expression is parsed).
    window: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def private(self) -> bool:
        """Whether it is ``private_access``: usable by other expressions and
        never queried directly."""
        return self.access == PRIVATE

    @property
    def row_level(self) -> bool:
        """Whether it holds one value per row of its table."""
        return self.kind in ROW_LEVEL


@dataclass(frozen=True)
class ColumnPair:
    """One entry of a relationship's ``relationship_columns``.

    ``kind`` is ``equal`` for a plain key pair, ``asof`` for the ASOF column, and
    ``range`` for a value joined to the ``[start, end)`` interval of the right
    table, where ``right`` is ``None`` and ``right_range`` names the interval.
    """

    left: str
    right: str | None
    kind: str = "equal"
    right_range: tuple[str, str] | None = None


@dataclass(frozen=True)
class Relationship:
    """One relationship: ``left_table`` holds the foreign key, ``right_table``
    the key it references."""

    name: str
    left_table: str
    right_table: str
    columns: tuple[ColumnPair, ...]
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def equi(self) -> bool:
        """Whether every pair is a plain key equality (no ASOF, no range)."""
        return all(pair.kind == "equal" for pair in self.columns)

    @property
    def left_columns(self) -> tuple[str, ...]:
        """The foreign-key columns, in order."""
        return tuple(pair.left for pair in self.columns)

    @property
    def right_columns(self) -> tuple[str, ...]:
        """The referenced columns, in order (a range pair names its interval)."""
        found: list[str] = []
        for pair in self.columns:
            if pair.right is not None:
                found.append(pair.right)
            elif pair.right_range is not None:
                found.extend(pair.right_range)
        return tuple(found)


@dataclass(frozen=True)
class LogicalTable:
    """One logical table: a base table (or a ``definition`` query) and the keys,
    members and filters declared on it."""

    name: str
    database: str | None = None
    schema: str | None = None
    table: str | None = None
    definition: str | None = None
    description: str | None = None
    synonyms: tuple[str, ...] = ()
    primary_key: tuple[str, ...] | None = None
    unique_keys: tuple[tuple[str, ...], ...] = ()
    #: ``(start_column, end_column)`` of each ``distinct_range`` constraint.
    ranges: tuple[tuple[str, str], ...] = ()
    dimensions: tuple[Member, ...] = ()
    time_dimensions: tuple[Member, ...] = ()
    facts: tuple[Member, ...] = ()
    metrics: tuple[Member, ...] = ()
    filters: tuple[dict[str, Any], ...] = ()
    #: The names of members the reader refused, kept so an expression reading
    #: one is refused too rather than read as a base column of that name.
    refused: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def base_table(self) -> str | None:
        """``database.schema.table``, or ``None`` for a ``definition`` query."""
        if self.database and self.schema and self.table:
            return f"{self.database}.{self.schema}.{self.table}"
        return None

    def members(self) -> Iterator[Member]:
        """Every member, in declaration order by kind."""
        yield from self.dimensions
        yield from self.time_dimensions
        yield from self.facts
        yield from self.metrics

    def member(self, name: str) -> Member | None:
        """The member named ``name``, matched ignoring case."""
        lowered = name.lower()
        return next((m for m in self.members() if m.name.lower() == lowered), None)

    def declared_keys(self) -> list[tuple[str, ...]]:
        """The primary key and every unique key, each as its columns."""
        found = [self.primary_key] if self.primary_key else []
        return [*found, *self.unique_keys]

    def is_refused(self, name: str) -> bool:
        """Whether the reader refused a member of this name."""
        return name.lower() in {r.lower() for r in self.refused}

    def is_key(self, columns: tuple[str, ...]) -> bool:
        """Whether ``columns`` cover a declared key, ignoring case and order."""
        wanted = {c.lower() for c in columns}
        return any(
            {c.lower() for c in key} <= wanted for key in self.declared_keys() if key
        )


@dataclass(frozen=True)
class SemanticView:
    """One configured file: one Semantic View, named by its own ``name``."""

    name: str
    file: str
    description: str | None = None
    tables: tuple[LogicalTable, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    metrics: tuple[Member, ...] = ()
    #: Keys carried verbatim and not modelled, by name (``verified_queries``,
    #: ``custom_instructions``, ``variables``, ``max_staleness``, ...).
    carried: dict[str, Any] = field(default_factory=dict, compare=False)
    #: The names of view-level metrics the reader refused.
    refused_metrics: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def table(self, name: str) -> LogicalTable | None:
        """The logical table named ``name``, matched ignoring case."""
        lowered = name.lower()
        return next((t for t in self.tables if t.name.lower() == lowered), None)

    def metric(self, name: str) -> Member | None:
        """The derived metric named ``name``, matched ignoring case."""
        lowered = name.lower()
        return next((m for m in self.metrics if m.name.lower() == lowered), None)

    def relationship(self, name: str) -> Relationship | None:
        """The relationship named ``name``, matched ignoring case."""
        lowered = name.lower()
        return next((r for r in self.relationships if r.name.lower() == lowered), None)
