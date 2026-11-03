"""The Cube model as dex reads it: cubes, views, members and joins.

Plain dataclasses over what a validated YAML file declares, with keys already
normalized to snake_case (Cube camelizes every key before it compiles, so
``sql_table`` and ``sqlTable`` are one key) and enumerations already
canonicalized (``belongs_to``, ``belongsTo`` and ``many_to_one`` are one
relationship). Nothing here reads a file or imports sqlglot; :mod:`.loader`
builds these, and :mod:`.catalog` and :mod:`.snapshot` read them.

``raw`` keeps each element as written (keys normalized), because the
fingerprint hashes the definition and authoring (#911) rewrites it, and neither
should depend on which fields this reader happened to model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Cube",
    "Join",
    "Member",
    "View",
    "ViewInclude",
    "ViewMember",
    "ViewSource",
]

#: Member kinds, as Cube names the sections that hold them.
DIMENSION = "dimension"
MEASURE = "measure"
SEGMENT = "segment"


@dataclass(frozen=True)
class Member:
    """One dimension, measure or segment of a cube."""

    name: str
    kind: str
    type: str | None = None
    sql: str | None = None
    title: str | None = None
    description: str | None = None
    primary_key: bool = False
    public: bool | None = None
    #: A time dimension's custom granularities, by name.
    granularities: tuple[str, ...] = ()
    #: A measure's `filters[].sql`, each a predicate in Cube's SQL dialect.
    filters: tuple[str, ...] = ()
    rolling_window: dict[str, Any] | None = None
    multi_stage: bool = False
    #: Defined by `case` (or, for a geo dimension, `latitude`/`longitude`)
    #: rather than by `sql`, so there is no one expression behind it.
    case: bool = False
    #: A `switch` dimension's declared values.
    values: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Join:
    """One join a cube declares, to the cube ``target``.

    ``relationship`` is canonical: ``many_to_one``, ``one_to_many`` or
    ``one_to_one``. ``alias`` names a second instance of the target reached
    through this join, which a view's ``join_path`` may walk through.
    """

    target: str
    sql: str
    relationship: str
    alias: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def reached_as(self) -> str:
        """The name a join path uses for this hop."""

        return self.alias or self.target


@dataclass
class Cube:
    """One cube: a relation (``sql_table``) or a query (``sql``) and its members."""

    name: str
    file: str
    sql_table: str | None = None
    sql: str | None = None
    title: str | None = None
    description: str | None = None
    data_source: str | None = None
    public: bool | None = None
    dimensions: list[Member] = field(default_factory=list)
    measures: list[Member] = field(default_factory=list)
    segments: list[Member] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    #: Keys Cube declares that dex's catalog has no home for, in written order.
    unmapped: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def member(self, name: str) -> Member | None:
        for member in (*self.dimensions, *self.measures, *self.segments):
            if member.name == name:
                return member
        return None

    def join_to(self, segment: str) -> Join | None:
        """The join a path segment walks: by alias first, then by target."""

        for join in self.joins:
            if join.alias == segment:
                return join
        for join in self.joins:
            if join.alias is None and join.target == segment:
                return join
        return None


@dataclass(frozen=True)
class ViewInclude:
    """One entry of a view's ``includes``: a member, renamed or relabelled."""

    name: str
    alias: str | None = None
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class ViewSource:
    """One entry of a view's ``cubes``: a join path and the members it takes."""

    join_path: tuple[str, ...]
    includes: tuple[ViewInclude, ...] | None
    excludes: tuple[str, ...] = ()
    prefix: bool = False
    alias: str | None = None
    split: bool = False

    @property
    def everything(self) -> bool:
        """``includes: "*"``."""

        return self.includes is None


@dataclass(frozen=True)
class ViewMember:
    """One member a view exposes, and the cube member it is."""

    name: str
    kind: str
    cube: str
    member: Member
    join_path: tuple[str, ...]
    title: str | None = None
    description: str | None = None

    @property
    def source(self) -> str:
        """The member it exposes, as Cube names it: ``cube.member``."""

        return f"{self.cube}.{self.member.name}"


@dataclass
class View:
    """One view: the governed public surface over a set of cubes."""

    name: str
    file: str
    title: str | None = None
    description: str | None = None
    public: bool | None = None
    sources: list[ViewSource] = field(default_factory=list)
    #: Resolved by the loader from ``sources`` against the cubes in the set.
    members: list[ViewMember] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def members_of(self, kind: str) -> list[ViewMember]:
        return [member for member in self.members if member.kind == kind]
