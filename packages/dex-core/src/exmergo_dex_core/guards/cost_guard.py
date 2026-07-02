"""Connector-aware cost gating: the preflight-before-spend rule.

Cost is surfaced as a preflight estimate before any spend, and nothing that spends
runs without explicit confirmation. The check order is deliberate:

1. Over-ceiling blocks first, so a blown budget can never be pushed through with
   ``--confirm``.
2. A billed paradigm (bytes-scanned, compute-time, DB load) with no ceiling at all
   is refused: nothing runs without a ceiling.
3. An unconfirmed command raises :class:`ConfirmationRequiredError` carrying the cost,
   which the command layer maps to a ``needs_confirmation`` envelope.

``FREE_LOCAL`` (DuckDB) requires only confirmation: the spend is zero, so a
numeric budget is optional, but the confirm handshake still runs so the gating
path is exercised on every connector. Billed paradigms require both a ceiling and
confirmation. DuckDB resource bounds (memory, threads, read-only) are enforced by
the adapter, not here.
"""

from __future__ import annotations

from ..envelope import Cost, Paradigm


class CostGuardError(Exception):
    """Base for every cost-guard refusal."""


class OverCeilingError(CostGuardError):
    """The estimate exceeds the ceiling; confirmation cannot override this."""


class CeilingRequiredError(CostGuardError):
    """A billed paradigm was invoked with no ceiling; nothing runs without one."""


class ConfirmationRequiredError(CostGuardError):
    """The command would spend but was not confirmed.

    Carries the preflight :class:`Cost` so the command layer can surface it in a
    ``needs_confirmation`` envelope for the agent to re-issue with ``--confirm``.
    """

    def __init__(self, cost: Cost):
        super().__init__(
            "confirmation required: re-run with --confirm (and a --budget on "
            "billed connectors) after reviewing the cost estimate"
        )
        self.cost = cost


def preflight(
    estimate: float | None,
    ceiling: float | None,
    *,
    paradigm: Paradigm = Paradigm.FREE_LOCAL,
    confirmed: bool = False,
) -> Cost:
    """Gate a spending command. Returns the cost to stamp into the envelope.

    ``estimate`` and ``ceiling`` are paradigm-relative magnitudes (bytes, credits,
    DBUs, a load score); the unit travels with ``paradigm``.
    """

    cost = Cost(paradigm=paradigm, estimate=estimate, ceiling=ceiling)

    if estimate is not None and ceiling is not None and estimate > ceiling:
        raise OverCeilingError(
            f"estimated cost {estimate} exceeds the ceiling {ceiling} "
            f"({paradigm.value}); raise the budget or narrow the work"
        )
    if paradigm is not Paradigm.FREE_LOCAL and ceiling is None:
        raise CeilingRequiredError(
            f"no ceiling set for a {paradigm.value} connector; pass --budget or "
            "set one in .dex/config.yml"
        )
    if not confirmed:
        raise ConfirmationRequiredError(cost)
    return cost
