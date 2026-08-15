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
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime

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

    def __init__(self, cost: Cost):
        super().__init__(
            "confirmation required: re-run with --confirm (and a --budget on "
            "billed connectors) after reviewing the cost estimate",
            cost=cost,
        )
        # Starts as just the cost, which is all the gate knows; the command layer
        # replaces it with the full agent-facing payload on the way out. Never
        # None, so a caller can always read it.
        self.request = ConfirmationRequest(cost=cost)


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
    paradigm: Paradigm, session_ceiling: float | None
) -> list[str]:
    """The warning a billed command carries when no cumulative cap is set.

    A warning rather than a refusal, deliberately: the per-command ceiling is
    refused when missing because nothing should run unbudgeted, but refusing the
    cumulative one would break every existing user who never set it. The
    asymmetry that made this worth surfacing is that both halves look equally
    enforced from outside, so an unset ``session_ceiling`` reads as a cap that
    bound rather than one that never existed.

    Module level rather than only a :class:`CostGate` method because ``transform
    build`` prices itself outside the gate on the degraded-pricing path and has
    to reach the same sentence; two spellings would drift.
    """

    if paradigm is Paradigm.FREE_LOCAL or session_ceiling is not None:
        return []
    return [
        "no cumulative spend ceiling: budget.session_ceiling is unset in "
        ".dex/config.yml, so this command was bound by its own budget alone and "
        "nothing bounds the day's total across commands. Config is read from "
        "this repo root only, so a ceiling set in another root does not apply "
        "here"
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
    ):
        self.paradigm = paradigm
        self.ceiling = ceiling
        self.session_ceiling = session_ceiling
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
        self.session_spent = self._read_session_spent()

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
    def _admission(self) -> Iterator[None]:
        """The critical section around read, decide, and book.

        Held for the decision only, never across a warehouse query: a lock that
        spans execution would serialize commands that have no reason to wait for
        each other, and would ask a hosted backend to hold one for minutes.
        """

        try:
            with self._lock() if self._lock is not None else nullcontext():
                self._refresh()
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

    def _refresh(self) -> None:
        self.session_spent = self._read_session_spent() - self._ledger_written

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
            self._reserve_to(estimate)
        return cost

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
        """

        needed = self._estimated + estimate
        if self._reserved and needed > self._reserved:
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
        """

        ceiling = self.effective_ceiling()
        if self._reserved:
            ceiling = (
                self._reserved if ceiling is None else min(ceiling, self._reserved)
            )
        if ceiling is None:
            return None
        return max(int(ceiling - self._billed), 0)

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
            *no_session_ceiling_warning(self.paradigm, self.session_ceiling),
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
        with self._admission():
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
        """

        return {
            spend_field(self.paradigm): self._billed,
            "session_spent_today": self.session_spent + self._billed,
        }
