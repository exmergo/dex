"""The Semantic Views deployed on a Snowflake account, read back and compared.

A governed query reads the deployed view and answers with the reviewed
repository definition or not at all, so before anything is priced dex:

1. **locates** the view: a warehouse-free ``SHOW SEMANTIC VIEWS`` over the source
   allowlist, matched by name. A view the account does not have is refused as
   defined only in the repository (dex never deploys one); one name in two
   allowlisted schemas is refused as ambiguous, and ``--scope`` narrows it;
2. **reads it back** with ``SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW``, which
   Snowflake serves from its cloud-services layer without resuming a warehouse
   (checked on the dogfood account), so the read bills no warehouse time and the refusal
   below comes before any spend;
3. **compares** every member the query names, the metrics they read, the
   relationships between the tables they reach, and those tables' keys and base
   tables, refusing on the first difference and naming the member and the field.

What Snowflake does to a view on the way back is not a difference: it spells
every name in upper case, writes ``access_modifier`` and an empty ``synonyms``
and ``description`` where none was written, re-infers each ``data_type``, and
drops ``sample_values``. Expressions are compared as parsed SQL with unquoted
identifiers folded, so whitespace and case are not differences either.

A role that holds no REFERENCES or OWNERSHIP on the view does not see its private
facts and metrics in the read-back. Such a member is reported as unreadable, the
coverage of that view as ``limited``, and a query that needs it is refused rather
than run unchecked.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from ..errors import DexError
from .model import LogicalTable, Member, SemanticView

__all__ = [
    "Coverage",
    "DeployedRead",
    "Difference",
    "LocateRefused",
    "compare",
    "locate",
    "read_deployed",
    "respell",
]

FULL = "full"
LIMITED = "limited"
REFUSED = "refused"


class LocateRefused(DexError):  # noqa: N818  (a refusal, not an error)
    """The view cannot be located on the account, said by name."""


@dataclass(frozen=True)
class Difference:
    """One field on which the deployed view differs from the repository."""

    element: str
    field: str
    repository: Any
    deployed: Any

    def render(self) -> str:
        """One clause for a refusal."""
        if self.field == "missing":
            return f"{self.element} is not in the deployed view"
        if self.field == "unreadable":
            return (
                f"{self.element} is private and this role cannot read it back, "
                "so it could not be compared"
            )
        return (
            f"{self.element} has a different {self.field} there "
            f"(repository {self.repository!r}, deployed {self.deployed!r})"
        )


@dataclass
class Coverage:
    """How much of a deployed view this role could read back."""

    view: str
    deployed_as: str | None
    status: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The payload form."""
        found: dict[str, Any] = {
            "view": self.view,
            "deployed_as": self.deployed_as,
            "coverage": self.status,
        }
        if self.reasons:
            found["reasons"] = list(self.reasons)
        return found


@dataclass
class DeployedRead:
    """One deployed view read back: its text, its validated form, its coverage."""

    name: str
    deployed_as: str
    text: str | None
    view: SemanticView | None
    coverage: Coverage
    diagnostics: list[str] = field(default_factory=list)


# --- locating ---------------------------------------------------------------------


def locate(listed: Iterable[str], name: str) -> str:
    """The one allowlisted ``DB.SCHEMA.NAME`` deployed as ``name``.

    ``listed`` is every semantic view the allowlist's scopes hold, as SHOW spells
    them. Matched ignoring case, since an unquoted name is stored upper-cased.
    """

    matches = sorted(
        {fq for fq in listed if fq.rsplit(".", 1)[-1].lower() == name.lower()}
    )
    if not matches:
        raise LocateRefused(
            f"refused: semantic view '{name}' is defined only in the repository: no "
            "schema in the source allowlist holds a semantic view of that name. "
            "dex never deploys one; deploy the reviewed file (after "
            "`SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML(..., TRUE)` verifies it) into a "
            "schema the allowlist names, then query again"
        )
    if len(matches) > 1:
        raise LocateRefused(
            f"refused: semantic view '{name}' is deployed in more than one "
            f"allowlisted schema ({', '.join(matches)}), so dex cannot tell which "
            "one the repository's file describes. Narrow the command with "
            "`--scope <database>.<schema>`"
        )
    return matches[0]


def read_deployed(
    name: str,
    deployed_as: str,
    reader: Callable[[str], str],
    repository: SemanticView | None,
) -> DeployedRead:
    """Read ``deployed_as`` back and validate it as the reader reads a file.

    ``reader`` returns the YAML ``SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW`` does, or
    raises; a refused read is coverage ``refused`` with the reason, never a
    crash, because ``list --deployed`` reports every view it could not read.
    """

    from .loader import validate_view_contents

    try:
        text = reader(deployed_as)
    except Exception as exc:  # a permission refusal is an answer, not a crash
        reason = _first_line(str(exc)) or type(exc).__name__
        return DeployedRead(
            name=name,
            deployed_as=deployed_as,
            text=None,
            view=None,
            coverage=Coverage(name, deployed_as, REFUSED, [reason]),
        )
    loaded = validate_view_contents({f"{deployed_as}.yaml": text})
    view = loaded.views[0] if loaded.views else None
    diagnostics = [d.render() for d in loaded.errors]
    coverage = Coverage(name, deployed_as, FULL)
    if view is None:
        coverage.status = REFUSED
        coverage.reasons = diagnostics or ["the read-back did not validate as a view"]
    elif repository is not None:
        hidden = _hidden_privates(repository, view)
        if hidden:
            coverage.status = LIMITED
            coverage.reasons = [
                "this role reads no private member back (it needs REFERENCES or "
                f"OWNERSHIP on the view): {', '.join(hidden)} could not be read"
            ]
    return DeployedRead(name, deployed_as, text, view, coverage, diagnostics)


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else ""


def _hidden_privates(repository: SemanticView, deployed: SemanticView) -> list[str]:
    """Private members of the repository the read-back does not carry."""

    hidden: list[str] = []
    for table in repository.tables:
        other = deployed.table(table.name)
        hidden.extend(
            f"{table.name}.{member.name}"
            for member in table.members()
            if member.private and (other is None or other.member(member.name) is None)
        )
    hidden.extend(
        member.name
        for member in repository.metrics
        if member.private and deployed.metric(member.name) is None
    )
    return hidden


# --- comparing ---------------------------------------------------------------------


def canonical(sql: str | None) -> str | None:
    """An expression in the form two spellings of it share; see
    :func:`.lineage.canonical`."""

    from .lineage import canonical as _canonical

    return _canonical(sql)


def _upper(values: Iterable[str] | None) -> tuple[str, ...]:
    return tuple(v.upper() for v in values or ())


def _non_additive(member: Member) -> tuple[tuple[str, ...], ...]:
    return tuple(
        sorted(
            tuple(str(entry.get(key, "")).upper() for key in sorted(entry))
            for entry in member.non_additive
        )
    )


def _member_fields(member: Member) -> dict[str, Any]:
    return {
        "kind": member.kind,
        "expr": canonical(member.expr),
        "access_modifier": "private" if member.private else "public",
        "non_additive_dimensions": _non_additive(member),
        "using_relationships": tuple(sorted(_upper(member.using_relationships))),
    }


def _table_fields(table: LogicalTable) -> dict[str, Any]:
    return {
        "base_table": (table.base_table or "").upper() or None,
        "definition": canonical(table.definition),
        "primary_key": tuple(sorted(_upper(table.primary_key))),
        "unique_keys": tuple(
            sorted(tuple(sorted(_upper(k))) for k in table.unique_keys)
        ),
    }


def compare(
    repository: SemanticView,
    deployed: SemanticView,
    members: Iterable[Member],
    tables: Iterable[str],
) -> list[Difference]:
    """Every difference between the two views on what a query relies on.

    ``members`` is each member the query names and every metric they read;
    ``tables`` the logical tables it reaches. Each table's base table and keys
    are compared, and every relationship between two of them.
    """

    found: list[Difference] = []
    for member in members:
        label = f"{member.table}.{member.name}" if member.table else member.name
        if member.table is None:
            other = deployed.metric(member.name)
        else:
            owner = deployed.table(member.table)
            other = owner.member(member.name) if owner is not None else None
        if other is None:
            field_ = "unreadable" if member.private else "missing"
            found.append(Difference(label, field_, None, None))
            continue
        mine, theirs = _member_fields(member), _member_fields(other)
        found.extend(
            Difference(label, key, mine[key], theirs[key])
            for key in mine
            if mine[key] != theirs[key]
        )
    reached = {t.lower() for t in tables}
    for name in sorted(reached):
        table = repository.table(name)
        if table is None:
            continue
        other = deployed.table(name)
        if other is None:
            found.append(Difference(f"table {table.name}", "missing", None, None))
            continue
        mine, theirs = _table_fields(table), _table_fields(other)
        found.extend(
            Difference(f"table {table.name}", key, mine[key], theirs[key])
            for key in mine
            if mine[key] != theirs[key]
        )
    for rel in repository.relationships:
        if (
            rel.left_table.lower() not in reached
            or rel.right_table.lower() not in reached
        ):
            continue
        other_rel = deployed.relationship(rel.name)
        label = f"relationship {rel.name}"
        if other_rel is None:
            found.append(Difference(label, "missing", None, None))
            continue
        mine_rel = _relationship_fields(rel)
        theirs_rel = _relationship_fields(other_rel)
        found.extend(
            Difference(label, key, mine_rel[key], theirs_rel[key])
            for key in mine_rel
            if mine_rel[key] != theirs_rel[key]
        )
    return found


def _relationship_fields(rel: Any) -> dict[str, Any]:
    return {
        "tables": (rel.left_table.upper(), rel.right_table.upper()),
        "columns": tuple(
            (
                pair.left.upper(),
                (pair.right or "").upper(),
                pair.kind,
                _upper(pair.right_range),
            )
            for pair in rel.columns
        ),
    }


def respell(deployed: SemanticView, repository: SemanticView) -> SemanticView:
    """``deployed`` with every name the repository also has spelled as the
    repository spells it.

    Snowflake reads an unquoted name back upper-cased, so a deployed catalog
    would list ``sales.ORDERS.REVENUE`` beside the repository's
    ``sales.orders.revenue``: the same member, matched ignoring case everywhere
    dex resolves a token, and still two strings a reader has to reconcile. A
    name only the deployment has keeps Snowflake's spelling.
    """

    def name_of(found: Any, fallback: str) -> str:
        return found.name if found is not None else fallback

    tables: list[LogicalTable] = []
    for table in deployed.tables:
        mine = repository.table(table.name)
        table_name = name_of(mine, table.name)

        def members(group: tuple[Member, ...], mine=mine, table_name=table_name):
            return tuple(
                replace(
                    m,
                    name=name_of(mine.member(m.name) if mine else None, m.name),
                    table=table_name,
                )
                for m in group
            )

        tables.append(
            replace(
                table,
                name=table_name,
                dimensions=members(table.dimensions),
                time_dimensions=members(table.time_dimensions),
                facts=members(table.facts),
                metrics=members(table.metrics),
            )
        )

    def table_name(name: str) -> str:
        return name_of(repository.table(name), name)

    relationships = tuple(
        replace(
            rel,
            name=name_of(repository.relationship(rel.name), rel.name),
            left_table=table_name(rel.left_table),
            right_table=table_name(rel.right_table),
        )
        for rel in deployed.relationships
    )
    metrics = tuple(
        replace(m, name=name_of(repository.metric(m.name), m.name))
        for m in deployed.metrics
    )
    return replace(
        deployed,
        name=repository.name,
        tables=tuple(tables),
        relationships=relationships,
        metrics=metrics,
    )
