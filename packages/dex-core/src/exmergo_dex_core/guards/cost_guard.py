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

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime

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


class CostGate:
    """Stateful spend meter for one billed command (class DI: built once in
    ``connect.open_adapter`` and carried by the adapter as ``cost_gate``).

    It wraps :func:`preflight` so the check order stays single-sourced at both
    scopes: once for the whole command (the strict confirm handshake, from a
    free dry-run total) and again per statement on the confirmed run (defense
    in depth, so a drifting estimate stops mid-command instead of overrunning).
    Dry-runs are free and never gated; execution never happens unconfirmed or
    without a ceiling. Billed bytes are appended to the ``.dex/spend.jsonl``
    ledger through ``record`` (functional DI), and the session ceiling is
    settled against spend already in that ledger.
    """

    def __init__(
        self,
        *,
        paradigm: Paradigm,
        ceiling: float | None,
        session_ceiling: float | None,
        session_spent: float,
        confirmed: bool,
        connector: str,
        command: str | None = None,
        record: Callable[[dict], None] | None = None,
    ):
        self.paradigm = paradigm
        self.ceiling = ceiling
        self.session_ceiling = session_ceiling
        self.session_spent = session_spent
        self.confirmed = confirmed
        self.connector = connector
        self.command = command
        self._record = record
        self._estimated = 0.0
        self._billed = 0.0
        self._command_estimate: float | None = None

    def effective_ceiling(self) -> float | None:
        """The binding ceiling: the per-command budget or what remains of the
        session budget, whichever is tighter. ``None`` only when neither is
        set, which :func:`preflight` then refuses for billed paradigms."""

        remaining_session = (
            max(self.session_ceiling - self.session_spent, 0.0)
            if self.session_ceiling is not None
            else None
        )
        bounds = [b for b in (self.ceiling, remaining_session) if b is not None]
        return min(bounds) if bounds else None

    def preflight_command(self, estimate: float) -> Cost:
        """The confirm handshake, called once per command with the free
        whole-command dry-run total.

        The order differs from per-statement :func:`preflight` in one spot: an
        unconfirmed call raises ``ConfirmationRequiredError`` even without a
        ceiling, because the first call is exactly how the agent learns the
        estimate it needs to pick a budget (nothing has been spent yet). An
        over-ceiling estimate still refuses first (confirmation cannot
        override it), and a confirmed run without a ceiling still refuses:
        nothing executes unbudgeted.
        """

        self._command_estimate = estimate
        ceiling = self.effective_ceiling()
        cost = Cost(paradigm=self.paradigm, estimate=estimate, ceiling=ceiling)
        if ceiling is not None and estimate > ceiling:
            raise OverCeilingError(
                f"estimated cost {estimate} exceeds the ceiling {ceiling} "
                f"({self.paradigm.value}); raise the budget or narrow the work"
            )
        if not self.confirmed:
            raise ConfirmationRequiredError(cost)
        if self.paradigm is not Paradigm.FREE_LOCAL and ceiling is None:
            raise CeilingRequiredError(
                f"no ceiling set for a {self.paradigm.value} connector; pass "
                "--budget or set one in .dex/config.yml"
            )
        return cost

    def charge(self, estimate: float) -> None:
        """Gate one statement on the confirmed run. Accumulates estimates so a
        sequence of statements is bounded as a whole, not just individually."""

        preflight(
            self._estimated + estimate,
            self.effective_ceiling(),
            paradigm=self.paradigm,
            confirmed=self.confirmed,
        )
        self._estimated += estimate

    def try_charge(self, estimate: float) -> bool:
        """Non-raising :meth:`charge` for optional spend (e.g. distinct-count
        escalation): False when the remaining budget cannot cover it."""

        try:
            self.charge(estimate)
        except CostGuardError:
            return False
        return True

    def remaining_for_statement(self) -> int | None:
        """The server-side cap for the next statement, in the paradigm's unit
        (bytes for ``maximum_bytes_billed``, seconds for a statement timeout):
        what remains of the effective ceiling after everything already charged.
        """

        ceiling = self.effective_ceiling()
        if ceiling is None:
            return None
        return max(int(ceiling - self._billed), 0)

    def ledger_field(self) -> str:
        """The ledger key actual spend is recorded under. Paradigm-specific so
        a bytes total and a seconds total can never silently sum together."""

        return (
            "billed_seconds"
            if self.paradigm is Paradigm.COMPUTE_TIME
            else "billed_bytes"
        )

    def record_billed(
        self, billed: float, *, job_id: str | None = None, statement: str = ""
    ) -> None:
        """Account one executed statement's actual spend and append it to the
        ledger. Statements are stored as a hash, never as text, so the ledger
        can hold no values."""

        self._billed += billed
        if self._record is not None:
            self._record(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "connector": self.connector,
                    "command": self.command,
                    self.ledger_field(): billed,
                    "job_id": job_id,
                    "statement_sha256": hashlib.sha256(
                        statement.encode("utf-8")
                    ).hexdigest()[:16]
                    if statement
                    else None,
                }
            )

    def cost(self) -> Cost:
        """The preflight cost to stamp into the envelope: the whole-command
        estimate when the handshake produced one, else what statements have
        charged so far."""

        estimate = (
            self._command_estimate
            if self._command_estimate is not None
            else self._estimated
        )
        return Cost(
            paradigm=self.paradigm,
            estimate=estimate,
            ceiling=self.effective_ceiling(),
        )

    def spend_summary(self) -> dict:
        """Actual spend for the envelope's ``data`` (the ``cost`` field stays a
        preflight estimate by contract). Key names deliberately avoid every
        envelope-sanitizer pattern and carry the paradigm's unit."""

        key = (
            "seconds_billed"
            if self.paradigm is Paradigm.COMPUTE_TIME
            else "bytes_billed"
        )
        return {
            key: self._billed,
            "session_spent_today": self.session_spent + self._billed,
        }
