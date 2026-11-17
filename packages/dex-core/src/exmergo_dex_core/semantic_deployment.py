"""Finding a deployed semantic object over the source allowlist, vendor-neutral.

A semantic layer whose governed query runs against an object deployed in the
warehouse (a Snowflake Semantic View, a Databricks Metric View) names that object
in the repository by a short name only: the file's ``name`` key, or its stem. Dex
never deploys one, so before it renders it has to find the one deployed object
the name means, among those the source allowlist's scopes hold, with a
warehouse-free metadata read the vendor's adapter supplies.

Two answers are refusals, and they are the same on every vendor, so they are
stated once here (#964):

- **defined only in the repository**: no allowlisted scope holds an object of
  that name, so there is nothing to query, and dex will not deploy one;
- **ambiguous**: more than one scope holds one, so dex cannot tell which one the
  repository's file describes, and ``--scope`` narrows the command to one.

What a vendor adds is its own words: what the object is called, how a scope is
spelled, and how one is deployed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import DexError

__all__ = ["LocateRefused", "ObjectKind", "locate"]


class LocateRefused(DexError):  # noqa: N818  (a refusal, not an error)
    """The object cannot be located on the connection, said by name."""


@dataclass(frozen=True)
class ObjectKind:
    """How a vendor names its deployed semantic object in a refusal.

    ``noun`` is what the object is called (``semantic view``); ``scope`` how a
    ``--scope`` entry narrowing to one schema is spelled
    (``<database>.<schema>``); ``deploy`` the sentence saying how to deploy the
    reviewed file; ``narrow`` anything said after the ``--scope`` advice.
    """

    noun: str
    scope: str
    deploy: str
    narrow: str = ""


def locate(listed: Iterable[str], name: str, kind: ObjectKind) -> str:
    """The one allowlisted fully qualified name deployed as ``name``.

    ``listed`` is every object of the kind the allowlist's scopes hold, fully
    qualified, as the vendor spells them. Matched on the last part ignoring
    case, since each vendor folds an unquoted name (Snowflake to upper case,
    Unity Catalog to lower case) and the repository may spell it either way.
    """

    matches = sorted(
        {fq for fq in listed if fq.rsplit(".", 1)[-1].lower() == name.lower()}
    )
    if not matches:
        raise LocateRefused(
            f"refused: {kind.noun} '{name}' is defined only in the repository: no "
            f"schema in the source allowlist holds a {kind.noun} of that name. "
            f"dex never deploys one; {kind.deploy}"
        )
    if len(matches) > 1:
        raise LocateRefused(
            f"refused: {kind.noun} '{name}' is deployed in more than one "
            f"allowlisted schema ({', '.join(matches)}), so dex cannot tell which "
            "one the repository's file describes. Narrow the command with "
            f"`--scope {kind.scope}`{kind.narrow}"
        )
    return matches[0]
