"""Connector-aware cost gating: the preflight-before-spend rule.

Cost is surfaced as a preflight estimate before any spend, and nothing that spends
runs without explicit confirmation. The check order is deliberate:

1. Over-ceiling blocks first, so a blown budget can never be pushed through with
   ``--confirm``.
2. A billed paradigm (bytes-scanned, compute-time, DB load) with no ceiling at all
   is refused: nothing runs without a ceiling.
3. An unconfirmed command on a billed paradigm raises
   :class:`ConfirmationRequiredError` carrying the cost, which the command layer
   maps to a ``needs_confirmation`` envelope.
4. A confirmed, budgeted command in a project that has never decided whether the
   *day's* total is bounded raises
   :class:`SessionCeilingDecisionRequiredError`, a subclass of the above, so the
   default state of every new project is a decision made once rather than a
   warning repeated forever (issue #283). Last, because it is the only check
   whose ask is worth making after the cost has been agreed to, and because the
   cost ask can then carry its suggestion as advice.

``FREE_LOCAL`` (DuckDB) skips step 3 (issue #197): the confirm handshake exists to
gate spend, and a connector that cannot bill has none to gate, so asking for
confirmation there confirms nothing and trains a caller to click through a
handshake that, on every other connector, does. It still skips the ceiling
requirement in step 2, for the same reason (the spend is always zero, so a
numeric budget is meaningless), and DuckDB resource bounds (memory, threads,
read-only) are enforced by the adapter, not here. Billed paradigms are
unaffected: both steps still bind exactly as before.

**The cumulative ceiling is settled against the ledger, so admitting a command
has to be atomic with booking its headroom.** Reading the day's spend and then
deciding leaves a window in which a second command reads the same number and
decides the same way, and both are then legal individually and wrong together.
So an admitted command writes a reservation at its estimate before it runs and
releases it when it settles, both under the store's spend lock. See
:meth:`CostGate.settle` for what an interrupted command leaves behind.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from ..envelope import Cost, Paradigm
from ..errors import DexError
from ..results import ConfirmationRequest


class CostGuardError(DexError):
    """Base for every cost-guard refusal.

    Every refusal carries the :class:`Cost` it refused on, so the transport layer
    can report the paradigm, the estimate, and the ceiling that bound rather than
    a message the caller has to parse. One attribute on the base rather than one
    per subclass: the boundary reads ``exc.cost`` without knowing which refusal
    it caught.
    """

    def __init__(self, message: str, *, cost: Cost | None = None):
        super().__init__(message)
        self.cost = cost


class OverCeilingError(CostGuardError):
    """The estimate exceeds the ceiling; confirmation cannot override this."""


class CeilingRequiredError(CostGuardError):
    """A billed paradigm was invoked with no ceiling; nothing runs without one."""


class SpendLockTimeoutError(CostGuardError):
    """The spend-admission lock could not be taken, so nothing was admitted.

    A refusal rather than a proceed-anyway, because the lock is the thing that
    makes the cumulative ceiling bind: running without it is the defect this
    guard exists to close. It is also the recoverable kind of refusal, since
    whatever holds the lock is a billed command that will finish.
    """


class LedgerUnreadableError(CostGuardError):
    """The spend ledger could not be read, so billed work was not admitted.

    Fails closed for the same reason :class:`SpendLockTimeoutError` does: the
    day's spend is what the cumulative ceiling is measured against, and admitting
    work against a reading nobody took would put a number in the envelope that no
    ceiling stands behind. Recoverable, because the backend is the thing that is
    unwell rather than the command.

    Only billed admission raises this. A command that cannot spend has no stake
    in the ledger and must not inherit its availability, which is why the day's
    spend is read where work is admitted rather than where the gate is built.
    """


class ConfirmationRequiredError(CostGuardError):
    """The command would spend but was not confirmed, so nothing ran.

    Raised by the gate with just the preflight :class:`Cost`, which is all the
    gate knows. The command layer catches it, attaches ``request`` (the
    agent-facing payload: the estimate in the connector's own unit, the
    per-table breakdown, the hint naming what to re-issue with), and re-raises
    the same exception. One class for one event, enriched as it travels: a
    second class at the command layer would mean two things to catch and two
    things to confuse.

    Not to be confused with :class:`OverCeilingError` or
    :class:`CeilingRequiredError`: confirmation cannot override either.
    """

    # Narrower than the base's optional: this refusal is always raised from a
    # priced gate, so callers read `exc.cost.estimate` without a None check.
    cost: Cost

    def __init__(self, cost: Cost, message: str | None = None):
        super().__init__(
            message
            or (
                "confirmation required: re-run with --confirm (and a --budget "
                "on billed connectors) after reviewing the cost estimate"
            ),
            cost=cost,
        )
        # Starts as just the cost, which is all the gate knows; the command layer
        # replaces it with the full agent-facing payload on the way out. Never
        # None, so a caller can always read it.
        self.request = ConfirmationRequest(cost=cost)


class SessionCeilingDecisionRequiredError(ConfirmationRequiredError):
    """The project has never decided whether the day's total spend is bounded.

    A subclass of :class:`ConfirmationRequiredError` because it is the same
    event in the same channel: a priced ask, nothing spent, recoverable by
    re-issuing the command with one more flag. Every boundary that already turns
    an unmet confirmation into a ``needs_confirmation`` envelope therefore turns
    this one into one too, and a host that catches the base class keeps working
    without knowing this exists.

    Distinct as a class for the one thing that differs: what re-issuing takes.
    The cost ask is answered by ``--confirm --budget``, this one by
    ``--session-ceiling <value>`` or ``--no-session-ceiling``, and the command
    layer has to build a different payload for it.

    ``suggested`` is the starting point the ask names, derived from this
    command's own estimate (see :func:`suggested_session_ceiling`).
    """

    def __init__(self, cost: Cost, *, suggested: float):
        super().__init__(
            cost,
            "no cumulative spend ceiling has been decided for this project: "
            "budget.session_ceiling is unset in .dex/config.yml, so nothing "
            "bounds the day's total across commands. Re-run with "
            f"--session-ceiling {suggested:.0f} to set one, or with "
            "--no-session-ceiling to record that this project runs without "
            "one. You are asked once either way",
        )
        self.suggested = suggested


#: What the one-time ask suggests, as a multiple of the asking command's own
#: estimate. Small on purpose: the number has to be plausible enough to accept
#: unread and loose enough that accepting it does not refuse the next few
#: commands, and a day that runs several commands the size of this one is the
#: shape of an ordinary session. It is a starting point the caller overrides by
#: passing their own number, never a recommendation dex stands behind.
SUGGESTED_SESSION_CEILING_MULTIPLE = 5.0


def suggested_session_ceiling(estimate: float) -> float:
    """The cumulative ceiling the one-time ask names, in the paradigm's own unit.

    Derived from the command in hand rather than from a table of per-connector
    defaults, because the one number dex can honestly reason from is what this
    caller's own work costs: a byte figure that suits a warehouse of ten tables
    is meaningless against one of ten thousand, and a default that fits neither
    would be refused or ignored on its first day.
    """

    return float(math.ceil(estimate * SUGGESTED_SESSION_CEILING_MULTIPLE))


def session_ceiling_undecided(
    paradigm: Paradigm,
    session_ceiling: float | None,
    *,
    declined: bool,
    config_path: Path | None,
    estimate: float,
) -> bool:
    """Whether a billed command has to stop and ask for a cumulative ceiling
    (issue #283).

    ``budget.ceiling`` is refused when missing and ``budget.session_ceiling`` is
    only warned about, and the gap between those two defensible positions is
    where an unbounded day lives. The warning was accurate, well worded, and
    attached to the default state of every new project, which is the condition
    under which warnings stop being read: five billed commands can run bound by
    their per-command caps alone, each carrying the same sentence, with the
    aggregate bounded by nothing. So the default becomes a decision, asked once.

    Four conditions narrow it to exactly that:

    - A billed paradigm with no cumulative ceiling set. ``FREE_LOCAL`` bills
      nothing, so there is no day's total for a cap to bound.
    - The decision was never recorded. Set a ceiling or decline one and this is
      permanently quiet in that project; nothing here re-opens a settled
      decision.
    - There is a committed ``.dex/config.yml`` to record it in. An ad-hoc read
      has nowhere to write an answer, so asking would be asking a question the
      caller cannot answer, and it keeps the warning instead.
    - The command actually priced something. A zero estimate bills nothing and
      gives the ask no honest starting point to name, so the project is asked by
      the next command that does price work.
    """

    return (
        paradigm is not Paradigm.FREE_LOCAL
        and session_ceiling is None
        and not declined
        and config_path is not None
        and estimate > 0
    )


def ledger_field(paradigm: Paradigm) -> str:
    """The ledger key actual spend is recorded under, per paradigm.

    Module level because the session-budget read has to name the same key the
    write will use, and that read happens while building the gate, before there
    is a gate to ask.
    """

    return (
        "billed_seconds"
        if paradigm in (Paradigm.COMPUTE_TIME, Paradigm.DB_LOAD)
        else "billed_bytes"
    )


def spend_field(paradigm: Paradigm) -> str:
    """The envelope key actual spend is reported under, per paradigm.

    Deliberately not :func:`ledger_field`'s spelling: the ledger writes
    ``billed_bytes`` and the envelope reports ``bytes_billed``, and both are
    load-bearing where they are. Module level for the same reason as its
    sibling, so a settlement outside the gate reports the key the gate would.
    """

    return (
        "seconds_billed"
        if paradigm in (Paradigm.COMPUTE_TIME, Paradigm.DB_LOAD)
        else "bytes_billed"
    )


def utc_day_start() -> str:
    """The cutoff the cumulative session budget settles against: today, UTC.

    Module level for :func:`ledger_field`'s reason, and one more: the gate reads
    the day's spend against this cutoff while being built, and ``transform
    build`` settles its own ledger entry outside any gate. Two spellings of
    "today" would put two commands on different days at a UTC boundary.
    """

    return (
        datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    )


def no_session_ceiling_warning(
    paradigm: Paradigm, session_ceiling: float | None, *, declined: bool = False
) -> list[str]:
    """The warning a billed command carries when no cumulative cap is set.

    A warning rather than a refusal, deliberately: the per-command ceiling is
    refused when missing because nothing should run unbudgeted, but refusing the
    cumulative one would break every existing user who never set it. The
    asymmetry that made this worth surfacing is that both halves look equally
    enforced from outside, so an unset ``session_ceiling`` reads as a cap that
    bound rather than one that never existed.

    It survives the one-time ask (issue #283) unchanged, including for a project
    that answered it by declining: what the caller needs to know on a result is
    that the day's total was bounded by nothing, and that is equally true either
    way. ``declined`` only names *why* it is unset, so a reader can tell a
    project that decided this from one that was never asked, and so the sentence
    does not keep proposing a decision that has already been made.

    Module level rather than only a :class:`CostGate` method because ``transform
    build`` prices itself outside the gate on the degraded-pricing path and has
    to reach the same sentence; two spellings would drift.
    """

    if paradigm is Paradigm.FREE_LOCAL or session_ceiling is not None:
        return []
    decided = (
        " This project recorded that choice deliberately "
        "(budget.session_ceiling_declined), so nothing will ask again; set "
        "budget.session_ceiling to bound the day."
        if declined
        else ""
    )
    return [
        "no cumulative spend ceiling: budget.session_ceiling is unset in "
        ".dex/config.yml, so this command was bound by its own budget alone and "
        "nothing bounds the day's total across commands. Config is read from "
        "this repo root only, so a ceiling set in another root does not apply "
        f"here.{decided}"
    ]


def unserialized_ledger_warning(
    paradigm: Paradigm, session_ceiling: float | None, serialized: bool
) -> list[str]:
    """The warning a billed command carries when its store cannot serialize the
    spend admission.

    Only when a cumulative ceiling is actually set: with none, there is nothing
    for the lock to protect and :func:`no_session_ceiling_warning` is already
    saying the more useful thing. Both shipped backends implement the lock, so
    this is silent unless a host selected a backend of its own.

    A warning rather than a refusal, which is the same call made for an unset
    cumulative ceiling: refusing would break a host whose backend predates the
    capability, and the reservation still narrows the race from the length of a
    warehouse query to the microseconds around the ledger read.

    Module level beside its siblings for the reason this file already documents:
    ``transform build`` prices itself outside the gate and has to reach the same
    sentence.
    """

    if paradigm is Paradigm.FREE_LOCAL or session_ceiling is None or serialized:
        return []
    return [
        "the cumulative spend ceiling is advisory on this cache backend: it "
        "does not implement the optional spend lock, so two overlapping billed "
        "commands can be admitted against the same headroom and the day's total "
        "can exceed budget.session_ceiling. Select a backend that implements it "
        "(both backends dex ships do), or run billed commands one at a time"
    ]


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

    Over-ceiling blocks regardless of paradigm, unconditionally, even for
    ``FREE_LOCAL``: an estimate that exceeds an explicitly configured ceiling is
    the caller's own contradiction to resolve, not a spend question. Everything
    after it is a confirmation handshake is emitted only where spend is
    possible (issue #197): ``FREE_LOCAL`` cannot bill, so neither the
    ceiling-required nor the confirmation check applies to it, confirmed or
    not. Every other paradigm is unaffected.
    """

    cost = Cost(paradigm=paradigm, estimate=estimate, ceiling=ceiling)

    if estimate is not None and ceiling is not None and estimate > ceiling:
        raise OverCeilingError(
            f"estimated cost {estimate} exceeds the ceiling {ceiling} "
            f"({paradigm.value}); raise the budget or narrow the work",
            cost=cost,
        )
    if paradigm is Paradigm.FREE_LOCAL:
        return cost
    if ceiling is None:
        raise CeilingRequiredError(
            f"no ceiling set for a {paradigm.value} connector; pass --budget or "
            "set one in .dex/config.yml",
            cost=cost,
        )
    if not confirmed:
        raise ConfirmationRequiredError(cost)
    return cost


def skipped_handshake_warning(paradigm: Paradigm, confirmed: bool) -> list[str]:
    """The warning a command carries when the confirm handshake would have
    fired but the paradigm cannot bill, so nothing needed confirming
    (issue #197).

    Empty when the caller already confirmed: passing ``--confirm`` on a free
    connector does no harm and asking why it was unnecessary would only be
    noise, so this is reserved for the case that would otherwise have been a
    silent, meaningless ask. Empty on a billed paradigm too, since there the
    handshake still gates real spend and nothing was skipped.
    """

    if paradigm is not Paradigm.FREE_LOCAL or confirmed:
        return []
    return [
        "no confirm handshake: this connector cannot bill, so there was "
        "nothing to confirm and the command ran without asking"
    ]


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

    Three functional dependencies rather than a store, because the gate needs
    three verbs and a store is sixteen: ``read_session_spent`` for the day's
    total, ``record`` to append, ``lock`` to make the pair of them atomic. A
    caller with no ledger (DuckDB, and any gate built directly) passes a plain
    float for the first and omits the other two, which is why those paths need
    no store at all.
    """

    def __init__(
        self,
        *,
        paradigm: Paradigm,
        ceiling: float | None,
        session_ceiling: float | None,
        session_spent: float | Callable[[], float],
        confirmed: bool,
        connector: str,
        command: str | None = None,
        record: Callable[[dict], None] | None = None,
        lock: Callable[[], AbstractContextManager[None]] | None = None,
        session_ceiling_declined: bool = False,
        config_path: Path | None = None,
    ):
        self.paradigm = paradigm
        self.ceiling = ceiling
        self.session_ceiling = session_ceiling
        # The two inputs to the one-time cumulative-ceiling ask (issue #283),
        # beside `session_ceiling` itself: whether this project already declined
        # one, and whether there is a committed config to record an answer in.
        # Both default to the state that keeps the ask silent, so a gate built
        # directly (a test, a host assembling its own) behaves as it did before
        # the ask existed.
        self.session_ceiling_declined = session_ceiling_declined
        self.config_path = config_path
        self.confirmed = confirmed
        self.connector = connector
        self.command = command
        self._record = record
        self._lock = lock
        self._estimated = 0.0
        self._billed = 0.0
        self._command_estimate: float | None = None
        # What this gate has itself appended to the ledger, netted. Subtracted
        # from every live reading so `session_spent` keeps meaning what it meant
        # when it was a constructor argument: the day's spend belonging to OTHER
        # commands. Without it a gate would read its own reservation back and
        # charge itself for it twice, and every ceiling below would tighten by
        # its own estimate.
        self._ledger_written = 0.0
        self._reserved = 0.0
        self._settled = False
        self._reservation_id = uuid.uuid4().hex[:16]
        self._read_session_spent: Callable[[], float] = (
            session_spent
            if callable(session_spent)
            else (lambda fixed=float(session_spent): fixed)
        )
        # `None` means nobody has read the day's spend yet, which is different
        # from having read a zero. A reader is a ledger call that can fail, and a
        # gate is built for every command on a billed connector including the
        # ones that cannot spend, so reading here would make free work depend on
        # the ledger's availability: `_admission` reads it where the reading is
        # actually load-bearing. A caller who handed over a plain float handed
        # over the answer rather than a way to get it, so there is nothing to
        # defer and that form seeds immediately.
        self.session_spent: float | None = (
            None if callable(session_spent) else self._read_session_spent()
        )

    @property
    def serialized(self) -> bool:
        """Whether the spend admission is safe from being raced.

        True with a lock, and also true with no ledger writer at all: a gate that
        appends nowhere shares no ledger, so there is nothing for a concurrent
        command to race it on. The second case is every free path and every gate
        built without a store, and folding it in here is what keeps those from
        warning about a ledger they do not have.
        """

        return self._lock is not None or self._record is None

    @contextmanager
    def _admission(self, *, strict: bool = True) -> Iterator[None]:
        """The critical section around read, decide, and book.

        Held for the decision only, never across a warehouse query: a lock that
        spans execution would serialize commands that have no reason to wait for
        each other, and would ask a hosted backend to hold one for minutes.

        ``strict`` is about what an unreadable ledger means at this point.
        Admitting work needs the reading, so a failure there refuses
        (:class:`LedgerUnreadableError`) rather than deciding a ceiling from
        nothing. Settling does not: it re-reads only so the spend summary can
        report the day's total, and the release it performs needs no reading at
        all, so a backend that went away mid-command must not turn a command that
        already ran into a refusal at the end of it. Non-strict leaves the day's
        spend unknown and carries on.
        """

        try:
            with self._lock() if self._lock is not None else nullcontext():
                self._refresh(strict=strict)
                yield
        except TimeoutError as exc:
            raise SpendLockTimeoutError(
                f"could not take the spend lock ({exc}); another billed command "
                "is being admitted. Nothing ran, so re-issuing the same command "
                "is safe",
                cost=Cost(
                    paradigm=self.paradigm,
                    estimate=self._command_estimate,
                    ceiling=self.ceiling,
                ),
            ) from exc

    def _refresh(self, *, strict: bool = True) -> None:
        try:
            spent = self._read_session_spent()
        except Exception as exc:
            # Deliberately broad: the reader is a backend's own code reached
            # through a one-method protocol, so the failure modes are the
            # backend's (a permission error on a file, a driver error, a socket)
            # and enumerating them here would be enumerating every backend that
            # could ever exist. What the gate needs to know is only whether it
            # got a number.
            if strict:
                raise LedgerUnreadableError(
                    f"could not read the spend ledger ({exc}); the day's spend "
                    "is what the cumulative ceiling is measured against, so "
                    "nothing was admitted. Nothing ran, so re-issuing the same "
                    "command is safe",
                    cost=Cost(
                        paradigm=self.paradigm,
                        estimate=self._command_estimate,
                        ceiling=self.ceiling,
                    ),
                ) from exc
            self.session_spent = None
            return
        self.session_spent = spent - self._ledger_written

    def session_spent_now(self) -> float | None:
        """The day's spend for a caller that wants to report it, not spend against it.

        The one read outside admission, and guarded rather than fail-closed:
        ``connect test`` exists to say what the connection and the budget look
        like, so a ledger it cannot reach is an answer of "unavailable" on an
        otherwise healthy report rather than a failed command. Nothing decides a
        ceiling from this, which is what makes swallowing the error the right
        call here and the wrong one in :meth:`_admission`.
        """

        self._refresh(strict=False)
        return self.session_spent

    def _append(self, kind: str, amount: float, **extra) -> None:
        if self._record is None:
            return
        self._record(
            {
                "at": datetime.now(UTC).isoformat(),
                "connector": self.connector,
                "command": self.command,
                "entry": kind,
                "reservation_id": self._reservation_id,
                self.ledger_field(): amount,
                **extra,
            }
        )
        self._ledger_written += amount

    def _reserve_to(self, target: float) -> None:
        """Hold ``target`` of the day's headroom, topping up what is held.

        Only with a cumulative ceiling set: a reservation exists to be seen by a
        concurrent command settling against ``session_ceiling``, so with none
        there is nothing it could protect, and writing one anyway would put a
        number in the ledger that no ceiling reads. That keeps the ledger
        byte-identical for every project that never set a daily cap.
        """

        if self.session_ceiling is None or self.paradigm is Paradigm.FREE_LOCAL:
            return
        if target > self._reserved:
            self._append("reservation", target - self._reserved)
            self._reserved = target

    def effective_ceiling(self) -> float | None:
        """The binding ceiling: the per-command budget or what remains of the
        session budget, whichever is tighter. ``None`` only when neither is
        set, which :func:`preflight` then refuses for billed paradigms.

        The session bound needs a reading of the day's spend to exist, and it
        drops out when there is none. That is not a loosened ceiling: admission
        always reads before it decides, so every path that could spend has the
        bound. What reaches here unread is a command that reports a cost without
        ever pricing one, and telling it the session remainder would mean reading
        a ledger on behalf of work that cannot touch it.
        """

        remaining_session = (
            max(self.session_ceiling - self.session_spent, 0.0)
            if self.session_ceiling is not None and self.session_spent is not None
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

        The whole check runs inside the admission section, against a reading
        taken there rather than at construction, and an admitted command books
        its estimate before the section ends. **A refusal books nothing**: every
        branch below leaves through the exception, so an unconfirmed handshake
        (much the most common outcome, since it is how every billed command
        starts) touches the ledger not at all.

        ``FREE_LOCAL`` skips the ceiling-required and confirmation checks
        (issue #197): no adapter attaches a gate for a connector that cannot
        bill, so a caller cannot actually construct one with this paradigm
        today, but the same rule binds here too rather than leaving a gate one
        misuse away from asking for confirmation of zero spend. Over-ceiling
        still blocks regardless of paradigm: an estimate that exceeds an
        explicitly configured ceiling is the caller's own contradiction to
        resolve, not a spend question.
        """

        self._command_estimate = estimate
        with self._admission():
            ceiling = self.effective_ceiling()
            cost = Cost(paradigm=self.paradigm, estimate=estimate, ceiling=ceiling)
            if ceiling is not None and estimate > ceiling:
                raise OverCeilingError(
                    f"estimated cost {estimate} exceeds the ceiling {ceiling} "
                    f"({self.paradigm.value}); raise the budget or narrow the "
                    "work",
                    cost=cost,
                )
            if self.paradigm is not Paradigm.FREE_LOCAL:
                if not self.confirmed:
                    raise ConfirmationRequiredError(cost)
                if ceiling is None:
                    raise CeilingRequiredError(
                        f"no ceiling set for a {self.paradigm.value} connector; "
                        "pass --budget or set one in .dex/config.yml",
                        cost=cost,
                    )
                self.require_session_ceiling_decision(estimate, cost=cost)
            self._reserve_to(estimate)
        return cost

    def require_session_ceiling_decision(
        self, estimate: float, *, cost: Cost | None = None
    ) -> None:
        """Ask once, per project, whether the day's total spend is bounded.

        Deliberately last of the four checks in :meth:`preflight_command`. The
        cost ask comes first because it is how the caller learns the estimate
        this suggestion is derived from, and it can then carry the suggestion as
        advice, so a caller who reads its payload answers both asks in one
        re-run and never sees this exception at all. What reaches here is a
        command that was confirmed and budgeted while stepping over the one
        question nothing else in the guard forces: whether anything bounds the
        day.

        Before the reservation is booked and therefore before any spend, like
        every other refusal in that method: the ask leaves through the
        exception, so an unanswered one touches the ledger not at all.
        """

        if not self.session_ceiling_pending(estimate):
            return
        raise SessionCeilingDecisionRequiredError(
            cost
            if cost is not None
            else Cost(
                paradigm=self.paradigm,
                estimate=estimate,
                ceiling=self.effective_ceiling(),
            ),
            suggested=suggested_session_ceiling(estimate),
        )

    def session_ceiling_pending(self, estimate: float) -> bool:
        """Whether this project still owes the one-time cumulative-ceiling answer.

        Read by the command layer as well as by the raise above: the cost ask
        fires first and carries the suggestion as advice, which is how a caller
        that reads one payload answers both asks in one re-run.
        """

        return session_ceiling_undecided(
            self.paradigm,
            self.session_ceiling,
            declined=self.session_ceiling_declined,
            config_path=self.config_path,
            estimate=estimate,
        )

    def preflight_phase(self, estimate: float) -> Cost:
        """Mid-command gate for a phase whose cost is only knowable after
        earlier spend (verify probes are priced only after inference finds
        candidates). Runs on an already-confirmed command, so it asks for
        confirmation only when the phase would not fit what remains of the
        ceiling. The raised cost's estimate is the whole-command total the
        re-run needs (charged so far plus the phase), so the agent can pick a
        budget directly from it. Raises :class:`ConfirmationRequiredError`
        rather than :class:`OverCeilingError` because a bigger budget on
        re-run can cover it; ``needs_confirmation`` is the recovery channel.

        An admitted phase extends the reservation rather than adding a second
        one, because the command holds one hold for its whole life and the
        phase raises what that hold is worth.

        ``FREE_LOCAL`` never asks here either (issue #197), even if a numeric
        ceiling happens to be configured for it: the phase spends nothing to
        fit against.
        """

        with self._admission():
            ceiling = self.effective_ceiling()
            needed = self._estimated + estimate
            cost = Cost(paradigm=self.paradigm, estimate=needed, ceiling=ceiling)
            if (
                self.paradigm is not Paradigm.FREE_LOCAL
                and ceiling is not None
                and needed > ceiling
            ):
                raise ConfirmationRequiredError(cost)
            self._reserve_to(needed)
        return cost

    def charge(self, estimate: float) -> None:
        """Gate one statement on the confirmed run. Accumulates estimates so a
        sequence of statements is bounded as a whole, not just individually.

        A statement inside what the command already booked takes the cheap local
        path: the headroom is held, so no reading can change whether it fits.
        **An estimate that drifts past the booking is re-admitted** against a
        live reading instead, because past that point the command is asking for
        headroom it never reserved, and the frozen reading it was admitted on
        cannot see what a concurrent command has taken since. Only the drift pays
        for the lock, and drift is the exception rather than the rule.

        A cumulative ceiling with no reading behind it also goes through
        admission, which is the case a caller reaches by charging a statement
        without having gone through :meth:`preflight_command` first. The local
        path would compute a ceiling with the session bound simply missing, so a
        configured cumulative cap would silently not apply, and on an unreadable
        ledger the statement would spend where the whole command should refuse.
        The reading has to exist before a ceiling derived from it can be trusted.
        """

        needed = self._estimated + estimate
        unmeasured_session = (
            self.session_ceiling is not None and self.session_spent is None
        )
        if unmeasured_session or (self._reserved and needed > self._reserved):
            with self._admission():
                preflight(
                    needed,
                    self.effective_ceiling(),
                    paradigm=self.paradigm,
                    confirmed=self.confirmed,
                )
                self._reserve_to(needed)
        else:
            preflight(
                needed,
                self.effective_ceiling(),
                paradigm=self.paradigm,
                confirmed=self.confirmed,
            )
        self._estimated = needed

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

        Bounded by the booking as well as by the ceiling, when there is one. The
        two differ exactly when another command is holding headroom, and handing
        the warehouse the wider of them is how the backstop that is supposed to
        bind when an estimate is wrong ends up permitting the pair to overspend
        together. :meth:`charge` widens the booking when the estimate genuinely
        drifts, so this tightens the cap without capping honest work.

        **A 0 here is a refusal, so only the ceiling may produce one.** The
        result is an integer because every connector's cap setting takes one,
        and on the time-paradigm connectors a cap of 0 does not mean "spend
        nothing" but *no limit* (Postgres ``statement_timeout``, ClickHouse
        ``max_execution_time``), so the adapters refuse when this returns under
        one unit rather than handing the server a 0.

        That makes which term produced a sub-unit value load-bearing, and
        conflating the two was a real defect. ``effective_ceiling`` minus what
        has actually been *billed* running low means the budget is genuinely
        nearly gone, and refusing is right. The booking is a different thing: it
        is headroom this command reserved for work that has not happened, and a
        cheap statement legitimately books a fraction of a unit. Letting that
        truncate to 0 refused perfectly affordable work, and it fired on every
        small query the moment ``session_ceiling`` was set, since that is what
        creates a reservation at all. So the exhaustion test reads the ceiling,
        and the booking is only ever allowed to *tighten* the cap, never to turn
        it into a refusal.
        """

        ceiling = self.effective_ceiling()
        remaining = None if ceiling is None else ceiling - self._billed
        if remaining is not None and remaining < 1:
            return 0
        if self._reserved:
            booked = self._reserved - self._billed
            remaining = booked if remaining is None else min(remaining, booked)
        if remaining is None:
            return None
        return max(math.ceil(remaining), 1)

    def ledger_field(self) -> str:
        """The ledger key actual spend is recorded under. Paradigm-specific so
        a bytes total and a seconds total can never silently sum together."""

        return ledger_field(self.paradigm)

    def warnings(self) -> list[str]:
        """What the guard has to say about its own reach, for the envelope.

        A guard that is narrower than it looks is worse than one that is absent,
        so anything the caller would wrongly assume is enforced belongs here.
        """

        return [
            *no_session_ceiling_warning(
                self.paradigm,
                self.session_ceiling,
                declined=self.session_ceiling_declined,
            ),
            *unserialized_ledger_warning(
                self.paradigm, self.session_ceiling, self.serialized
            ),
        ]

    def record_billed(
        self, billed: float, *, job_id: str | None = None, statement: str = ""
    ) -> None:
        """Account one executed statement's actual spend and append it to the
        ledger. Statements are stored as a hash, never as text, so the ledger
        can hold no values.

        Deliberately outside the admission lock. This is a pure append with
        nothing read back, so it races nothing, and taking the lock here would
        hold it once per statement on a path that runs while the warehouse is
        working.
        """

        self._billed += billed
        self._append(
            "settlement",
            billed,
            job_id=job_id,
            statement_sha256=hashlib.sha256(statement.encode("utf-8")).hexdigest()[:16]
            if statement
            else None,
        )

    def settle(self) -> None:
        """Release the unspent hold and re-read the day, once per command.

        Called from the command layer's settlement funnel, from the engine when
        it rebuilds a gate, and from engine shutdown, so an interrupted command
        releases as reliably as a completed one. Idempotent, because those three
        overlap by design rather than by accident.

        **A process killed outright leaves its reservation standing**, and it
        holds the estimate against the day's headroom until the UTC rollover.
        That is the safe direction for a spend guard and it is why there is no
        expiry: expiring a hold would mean teaching every backend's
        ``spend_since`` to tell the entry kinds apart and reason about time,
        which is a protocol change for a case the three funnels already cover.
        """

        if self._settled:
            return
        self._settled = True
        # Entering the section re-reads the day, which is what makes
        # `spend_summary` report the true total rather than this command's view
        # of it. Releasing after that needs no second read: the release lowers
        # the live total and this gate's own written total by the same amount,
        # so the difference between them, which is what `session_spent` holds,
        # does not move.
        with self._admission(strict=False):
            if self._reserved:
                self._append("release", -self._reserved)
                self._reserved = 0.0

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
        envelope-sanitizer pattern and carry the paradigm's unit.

        ``session_spent_today`` is the day's total across every command, not
        this one's contribution to it, which is why it is read after
        :meth:`settle`: the sum of what other commands have settled plus what
        this one just did. Two commands running at once used to report their own
        spend under a name that promised the day's, so a caller reading either
        one saw a fraction of the truth.

        It reports ``None`` when the ledger could not be read at settlement.
        What this command billed is still exact, because that number comes from
        the warehouse rather than from the ledger; it is only the day's total that
        is unavailable, and saying so beats reporting this command's spend as if
        it were the day's.
        """

        return {
            spend_field(self.paradigm): self._billed,
            "session_spent_today": (
                self.session_spent + self._billed
                if self.session_spent is not None
                else None
            ),
        }
