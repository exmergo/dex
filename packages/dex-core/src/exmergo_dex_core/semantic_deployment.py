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

Reading a located object back is neutral too (#1363): how much of it this role
could read is a :class:`Coverage`, ``full``, ``limited`` or ``refused`` with its
reasons, and a read the vendor refuses (a permission, a missing definition) is
coverage ``refused`` with that reason rather than a crash, because ``list
--deployed`` reports every object it could not read. What stays with the vendor
is what it knows about its format: the reader that validates the text, what it
normalizes on the way back, and the comparison with the repository.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import DexError

__all__ = [
    "FULL",
    "LIMITED",
    "REFUSED",
    "Coverage",
    "DeployedRead",
    "LocateRefused",
    "ObjectKind",
    "judge",
    "locate",
    "read_text",
]

FULL = "full"
LIMITED = "limited"
REFUSED = "refused"


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


# --- reading back -------------------------------------------------------------------


@dataclass
class Coverage:
    """How much of a deployed object this role could read back."""

    view: str
    deployed_as: str | None
    status: str
    reasons: list[str] = field(default_factory=list)
    #: Where a repository file describes the object and was not checked against
    #: it (Snowflake's ``snowflake_account``, #1357): that file.
    repository_file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The payload form."""
        found: dict[str, Any] = {
            "view": self.view,
            "deployed_as": self.deployed_as,
            "coverage": self.status,
        }
        if self.reasons:
            found["reasons"] = list(self.reasons)
        if self.repository_file is not None:
            found["repository_file"] = self.repository_file
        return found


@dataclass
class DeployedRead:
    """One deployed object read back: its text, its validated form (the vendor's
    own model, or None where it did not validate), and its coverage."""

    name: str
    deployed_as: str
    text: str | None
    view: Any
    coverage: Coverage
    diagnostics: list[str] = field(default_factory=list)
    #: Elements of the read-back the vendor could not use, by name: a Snowflake
    #: read naming a private member this role cannot read back, a Metric View
    #: element the reader refused. A member missing for one of these is
    #: unreadable there, not absent.
    unresolved: list[str] = field(default_factory=list)


def read_text(
    name: str, deployed_as: str, reader: Callable[[str], str]
) -> tuple[str | None, Coverage | None]:
    """The text ``reader`` returns for ``deployed_as``, or None with coverage
    ``refused`` naming why: a refused read is an answer, not a crash."""

    try:
        return reader(deployed_as), None
    except Exception as exc:  # a permission refusal is an answer, not a crash
        reason = first_line(str(exc)) or type(exc).__name__
        return None, Coverage(name, deployed_as, REFUSED, [reason])


def judge(
    name: str,
    deployed_as: str,
    view: Any,
    diagnostics: Sequence[str],
    limits: Sequence[str] = (),
) -> Coverage:
    """The coverage of a read-back the vendor's reader has judged.

    ``view`` is what validated (None where nothing did, which is ``refused``),
    ``diagnostics`` what the reader refused in it, and ``limits`` what the vendor
    knows this role could not read; either makes a validated view ``limited``,
    so a partial read never reads as complete.
    """

    if view is None:
        return Coverage(
            name,
            deployed_as,
            REFUSED,
            list(diagnostics) or ["the read-back did not validate as a view"],
        )
    reasons = [*limits, *diagnostics]
    return Coverage(name, deployed_as, LIMITED if reasons else FULL, reasons)


def first_line(message: str) -> str:
    """The first line of an error's text, which is the reason it was refused."""
    return message.strip().splitlines()[0] if message.strip() else ""
