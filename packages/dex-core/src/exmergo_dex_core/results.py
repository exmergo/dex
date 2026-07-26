"""What the engine returns, and how the CLI turns it into an envelope.

The :class:`~exmergo_dex_core.envelope.Envelope` is a stdout-transport concern:
one JSON object, sanitized, printed by the command contract. A library caller
wants none of that, so every engine method returns a :class:`Result` instead,
carrying domain objects plus the notes, warnings, and diffs that are genuinely
part of the answer rather than part of the transport.

The split is worth being precise about, because it is easy to put things on the
wrong side. Notes stay on the result: they explain cache reuse, rank cutoffs,
folded replica lineage, suppressed matches, and a caller acting on the answer
needs them however it was invoked. Hints stay in the shim: they name the next
CLI subcommand to run, which is advice only a CLI user can take.

Each subclass adds its own fields and implements :meth:`Result.data`, the payload
that lands under the envelope's ``data`` key. Everything cross-cutting (cost,
actual spend, notes, warnings, diffs, an unmet confirmation) is handled once, by
the base class and by :func:`to_envelope`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from . import envelope as env
from .envelope import Cost


class ConfirmationRequest(BaseModel):
    """A priced ask, waiting on a human or an agent to accept it.

    ``data`` is the agent-facing payload: the estimate in the connector's own
    unit, the per-table breakdown when there is one, and the hint naming the
    flags that re-issue the command. It is deliberately a free dict because each
    connector describes its own estimate (Snowflake translates to credits, the
    bytes-scanned connectors report bytes) and the shape is the adapter's to
    choose.
    """

    cost: Cost = Field(default_factory=Cost)
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BudgetExhaustedError(Exception):
    """The ceiling was reached partway through, and the run stopped there.

    Not the same as refusing up front: work was done and paid for, so the message
    says how much finished and where it was saved, and ``spend`` carries what was
    actually billed. A caller raises the budget and re-runs; the saved partial
    profiles mean the re-run is cheaper than the first attempt.
    """

    def __init__(self, message: str, *, spend: dict[str, Any] | None = None):
        super().__init__(message)
        self.spend = spend


class Result(BaseModel):
    """Base for every engine result.

    ``pending_confirmation`` is the two-phase case, and it is why confirmation is
    not always an exception. Commands that price a second phase only after paying
    for the first (``relationships`` and ``map`` pricing verify probes after
    inference finds candidates; ``check`` pricing its scanning axes after the free
    ones complete) must return the work already paid for *and* the ask for the
    rest. Raising would throw away results the user has already been billed for.
    """

    # Whether the payload carries `notes` even when there is nothing to say.
    # For most commands an absent key and an empty list mean the same thing, so
    # the key is omitted. For the two that return data (`query`, `cluster`) an
    # empty notes list is a positive statement, "nothing was truncated, capped,
    # or dropped", and an agent reads it that way, so those keep it always.
    always_reports_notes: ClassVar[bool] = False

    cost: Cost = Field(default_factory=Cost)
    # Actual spend for this command, in the connector's own unit. The `cost`
    # field above stays a preflight estimate by contract; these are billed
    # figures, and only a billed connector produces them.
    spend: dict[str, Any] | None = None
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Reviewable diffs (propose-don't-impose). Nothing is applied by being here.
    diffs: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmation: ConfirmationRequest | None = None

    def data(self) -> dict[str, Any]:
        """The command-specific payload, keyed as the command contract documents."""

        return {}


class ConnectResult(Result):
    """What ``connect test`` learned: the connector's own capability report.

    The probe is free on every connector, but the cost carries the connector's
    paradigm, so a caller knows what later commands will bill in before running
    one that does.
    """

    capabilities: dict[str, Any] = Field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        return dict(self.capabilities)


def to_envelope(result: Result, *, hints: dict[str, Any] | None = None) -> env.Envelope:
    """Wrap a result for stdout. The only place a result becomes transport.

    ``hints`` carries the CLI-shaped extras a shim derives and the result
    deliberately does not hold: which subcommand to run next, and counts that are
    a plain ``len()`` of something already in the payload.
    """

    data = result.data()
    if hints:
        data.update(hints)
    if result.notes or result.always_reports_notes:
        data["notes"] = result.notes
    if result.spend is not None:
        data["spend"] = result.spend

    if result.pending_confirmation is not None:
        pending = result.pending_confirmation
        # The ask first, then the results already paid for: a two-phase command
        # reports both, and the payload keys never collide because the ask speaks
        # cost and the payload speaks findings.
        return env.needs_confirmation(
            {**pending.data, **data},
            cost=pending.cost,
            warnings=[*result.warnings, *pending.warnings],
            diffs=result.diffs,
        )
    return env.ok(data, cost=result.cost, warnings=result.warnings, diffs=result.diffs)
