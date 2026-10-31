"""The Snowflake adapter: the first compute-time billed connector.

The cost inversion from BigQuery: metadata is cheap (SHOW commands run on the
cloud-services layer with no warehouse), while any data scan costs warehouse
runtime. So inventory, ``connect test``, and all schema facts stay free, and
the guarded quantity is warehouse-seconds, not bytes.

Snowflake has no dry-run, so estimates are a documented heuristic (table bytes
over a conservative per-size scan rate) and every envelope that carries one
says so. The server-side backstop is a per-statement
``STATEMENT_TIMEOUT_IN_SECONDS`` set to the remaining budget before each billed
statement, so a wrong estimate cannot overrun the ceiling: Snowflake kills the
statement.

Snowflake bills warehouse uptime rather than statement time: at least 60
seconds per resume, then per second until the warehouse has idled for its
AUTO_SUSPEND. Actual spend is therefore two parts, both recorded to the ledger
as ``billed_seconds``: wall-clock seconds per statement, and, once the command's
last statement is done, the uptime no statement reported (the resume minimum,
the time between statements, the idle tail). ``_WarehouseUptime`` models the
second part, and the estimate quotes the same model, so the two agree. With
``snowflake.suspend_after_command`` the idle tail is replaced by a suspend, and
a command joining a resume cycle a previous dex command left running pays only
what it adds to that command's claim (issue #664).

Read-only is enforced in depth: the SELECT-only guard in the snowflake dialect
on every data statement through one execution door, an adapter that issues no
mutating statements (SHOW / SELECT / session parameters only, plus, when the
config opts in, ``ALTER WAREHOUSE <pinned> SUSPEND``, which stops compute and
touches no data), and the documented read-only role grants. Billed statements
run only on the warehouse ``.dex/config.yml`` pins; a connection-level default
warehouse is never spent on.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..config import SnowflakeTarget
from ..envelope import EstimateQuality, Paradigm
from ..errors import ConnectorError
from ..guards.cost_guard import UPTIME_LINE, CostGate, OverCeilingError
from ..guards.sql_guard import assert_select_only
from .base import (
    ColumnAggregate,
    ColumnMeta,
    ObjectMeta,
    QueryResult,
    ValueDomainSample,
    affordable_combinations,
    blame,
    distinct_combination_sql,
    is_blob_type,
    is_integer_type,
    is_string_type,
    json_safe,
    key_shape_aggregate_kwargs,
    key_shape_expressions,
    name_list,
    scope_within,
    shape_stat_expressions,
    shape_stat_value,
    temporal_alignment_expressions,
    temporal_continuity_aggregate_kwargs,
    temporal_continuity_sql,
    temporal_units_for,
    type_contradiction_aggregate_kwargs,
    type_contradiction_expressions,
    warehouse_refusal,
    with_dev_target,
)

PARADIGM = "compute_time"
DIALECT = "snowflake"

_BIGINT_TYPE = "BIGINT"

# Snowflake keeps a read-only INFORMATION_SCHEMA in every database and an
# account-internal SNOWFLAKE database. Neither is ever a source, and neither may
# be reached by a scope entry.
_RESERVED_SCHEMA = "INFORMATION_SCHEMA"
_RESERVED_DATABASE = "SNOWFLAKE"


def _reserved(row: dict) -> bool:
    """Whether a SHOW row describes an object in a reserved schema."""

    return str(row.get("schema_name") or "").upper() == _RESERVED_SCHEMA


# How many databases a bare schema name may be searched across before dex asks
# for a qualified <database>.<schema> instead. Each candidate costs one free SHOW
# SCHEMAS round-trip, and an unscoped enterprise account has hundreds.
_BARE_SCOPE_SEARCH_LIMIT = 20

# Columns are profiled in batches so one statement against a very wide table
# does not balloon (up to 4 expressions per column).
_COLUMN_BATCH = 50

# Snowflake bills at least 60 seconds each time a suspended warehouse resumes,
# regardless of query duration: the floor on a resumed command's uptime.
_RESUME_MINIMUM_SECONDS = 60.0

# The estimate heuristic: a deliberately conservative X-Small scan rate.
# Larger sizes scale it by their credit multiple (they bill proportionally
# more per second but scan proportionally faster, so estimated *seconds*
# shrink while estimated *credits* stay comparable).
_XSMALL_SCAN_BYTES_PER_SECOND = 50 * 1024 * 1024

# Every billed statement estimates at least this much: even a metadata-served
# result costs a moment of a running warehouse.
_MIN_STATEMENT_SECONDS = 1.0

# Credits per hour by warehouse size (Gen1 standard). Keyed by the normalized
# size token from SHOW WAREHOUSES. Gen2 warehouses bill a multiplier on top
# (1.35 on AWS/GCP, 1.25 on Azure); the cloud is not cheaply knowable, so the
# conservative 1.35 is applied and the translation is labeled approximate.
_CREDITS_PER_HOUR = {
    "XSMALL": 1.0,
    "SMALL": 2.0,
    "MEDIUM": 4.0,
    "LARGE": 8.0,
    "XLARGE": 16.0,
    "2XLARGE": 32.0,
    "3XLARGE": 64.0,
    "4XLARGE": 128.0,
    "5XLARGE": 256.0,
    "6XLARGE": 512.0,
}
_GEN2_MULTIPLIER = 1.35

# Semi-structured and spatial types: no distinct estimate, no min/max, only a
# non-null count (COUNT works on all of them; the others do not).
_NESTED_TYPE_PREFIXES = (
    "VARIANT",
    "OBJECT",
    "ARRAY",
    "GEOGRAPHY",
    "GEOMETRY",
    "VECTOR",
)

_ESTIMATE_QUALITY_NOTE = (
    "Snowflake has no dry-run: the estimate is a heuristic (table bytes over a "
    "conservative scan rate); the confirmed budget is still hard-enforced by a "
    "per-statement server-side timeout"
)


def _regexp_predicate(qcol: str, pattern: str) -> str:
    return f"RLIKE({qcol}, '{pattern}')"


def _date_trunc_expr(qcol: str, unit: str) -> str:
    return f"DATE_TRUNC('{unit}', {qcol})"


def _date_diff_expr(unit: str, later: str, earlier: str) -> str:
    return f"DATEDIFF({unit}, {earlier}, {later})"


class SnowflakeConnectionError(ConnectorError):
    """Raised when the pinned warehouse (or a queried object) cannot be
    resolved. The message always names the fix, never a credential."""


def _live_state(row: dict) -> dict:
    """What SHOW WAREHOUSES says about the pinned warehouse that can change
    while dex works: its state, its load, when it last resumed, and how long it
    idles before suspending (``None`` when AUTO_SUSPEND is 0 or NULL, which
    Snowflake reads as never). ``resumed_at`` is ``resumed_on`` as epoch
    seconds, or ``None`` when it does not parse, so a resume can be placed
    against this command's own statements."""

    def number(key: str) -> int:
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    resumed_on = row.get("resumed_on")
    return {
        "name": str(row.get("name", "")),
        "state": str(row.get("state", "unknown")).upper(),
        "running": number("running"),
        "queued": number("queued"),
        "resumed_on": None if resumed_on in (None, "") else str(resumed_on),
        "resumed_at": _epoch(resumed_on),
        "auto_suspend": number("auto_suspend") or None,
        # Only an explicit "true" counts: a warehouse whose AUTO_RESUME cannot
        # be read is treated as one that will not wake for the next query.
        "auto_resume": str(row.get("auto_resume", "")).strip().lower() == "true",
    }


def _epoch(value: object) -> float | None:
    """A SHOW timestamp as epoch seconds: the driver hands back an aware
    datetime, a string where a test or another driver does."""

    if isinstance(value, datetime):
        return value.timestamp() if value.tzinfo is not None else None
    if isinstance(value, str) and value:
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo is not None else None
    return None


def _sql_name(name: str) -> str:
    """A Snowflake object name as a statement takes it: bare when it is a plain
    upper-case identifier, double-quoted otherwise."""

    if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _grant(warehouse: str, role: str | None) -> str:
    """The exact grant ``suspend_after_command`` needs, with this session's role
    filled in where the connection reported it."""

    who = _sql_name(role) if role else "<dex role>"
    return f"GRANT OPERATE ON WAREHOUSE {_sql_name(warehouse)} TO ROLE {who}"


def _adoption(warehouse: str, role: str | None) -> str:
    """The two steps that stop the idle tail, as commands to copy."""

    return (
        "`dex config set snowflake.suspend_after_command true` and, run by a "
        f"role that may grant it, `{_grant(warehouse, role)}`"
    )


class _WarehouseUptime:
    """What one command costs the pinned warehouse beyond its statements' own
    time, from the rules Snowflake bills by.

    Snowflake bills a warehouse for being up, not for running statements: at
    least 60 seconds every time a suspended warehouse resumes, then per second
    until it has idled for AUTO_SUSPEND. Statement time alone under-reports
    exactly the small commands an agent runs most: a three-second build on a
    warehouse that suspends after 60 idle seconds bills over a minute. So the
    command is also charged:

    - the time between its own statements, while the warehouse stayed up;
    - a 60-second floor on that uptime when it found the warehouse suspended;
    - the AUTO_SUSPEND tail when it leaves the warehouse idle, which a free SHOW
      WAREHOUSES says once its last statement is done.

    Two things shorten that charge (issue #664). With
    ``snowflake.suspend_after_command``, a command that woke the warehouse and
    leaves it idle suspends it (``suspend``), and is charged its uptime up to
    the suspend, floored at the resume minimum, instead of a tail. And a command
    that joins a warehouse a previous Dex command left running in the same
    resume cycle is charged only what it adds to that command's claim (the gap
    plus its own span), because the previous command already paid a tail that
    the warehouse never idled through. Each command records its claim in the
    spend ledger (``record_claim``) so the next one can read it back
    (``prior_claims``). The cycle rule only ever lowers the charge: it never
    exceeds what the warehouse model alone would charge, so a warehouse other
    users keep running can never make dex claim their uptime.

    The estimate quotes the same model before anything runs (``quote``) and the
    statement timeout holds back what is still owed (``pending``), so estimate,
    cap and settlement agree. It is a model, not a meter: Snowflake's suspend
    check runs about every 30 seconds, so a tail can run up to that much longer
    than AUTO_SUSPEND (most likely when the setting is not a multiple of 30), and
    SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY stays the authority.
    It cannot be settled from that view instead: it is hourly, hours behind, and
    readable only with ACCOUNTADMIN or MONITOR USAGE.

    One per cost gate, so one per command: a held engine keeps its adapter and
    swaps the gate, and a resume the first command paid for must not be
    remembered by the second. ``read_state`` returns a fresh :func:`_live_state`
    (functional DI), ``clock`` is the adapter's statement clock and
    ``wall_clock`` its epoch clock (claims outlive the process, so they are
    stamped in wall time), and statements dex did not run itself (dbt's) are
    reported through :meth:`observe` by whoever ran them.
    """

    def __init__(
        self,
        *,
        read_state: Callable[[], dict],
        clock: Callable[[], float],
        wall_clock: Callable[[], float] = time.time,
        suspend: Callable[[], str] | None = None,
        prior_claims: Callable[[], list[dict]] | None = None,
        record_claim: Callable[[dict], None] | None = None,
        role: str | None = None,
    ):
        self._read_state = read_state
        #: The session's role, for advice that names the grant to run.
        self._role = role
        self._clock = clock
        # Monotonic and wall clocks tick together for the life of one command,
        # so one offset converts a statement's monotonic end into epoch seconds.
        self._wall_offset = wall_clock() - clock()
        self._suspend = suspend
        self._prior_claims = prior_claims
        self._record_claim = record_claim
        self._claims: list[dict] | None = None
        self._before: dict | None = None
        self._before_read = False
        self._first_start: float | None = None
        self._first_end: float | None = None
        self._last_end: float | None = None
        self._recorded = 0.0
        self._admitted = False
        self._charged = False
        self._settled = False

    def seed(self, state: dict) -> None:
        """Take the warehouse as this command found it from a SHOW that already
        ran, rather than asking again."""

        if not self._before_read:
            self._before, self._before_read = state, True

    @property
    def before(self) -> dict | None:
        """The warehouse as this command found it, read once. ``None`` when it
        could not be read, which the model prices as suspended."""

        if not self._before_read:
            self._before_read = True
            with contextlib.suppress(Exception):
                self._before = self._read_state()
        return self._before

    def observe(self, seconds: float, *, span: float | None = None) -> None:
        """Count statement time that just finished on the warehouse, already
        ledgered by whoever ran it, as an interval ending now.

        ``span`` is the wall time those statements were spread over when it is
        longer than their sum, as with a dbt run: its nodes sit between parsing,
        compiling and dbt's own round trips, and the warehouse is up throughout.
        Zero seconds count for nothing, because a dbt run that executed no node
        never woke the warehouse.
        """

        if seconds <= 0:
            return
        end = self._clock()
        start = end - max(float(span or 0.0), seconds)
        if self._first_start is None:
            self._first_start, self._first_end = start, end
        else:
            self._first_start = min(self._first_start, start)
        self._last_end = end if self._last_end is None else max(self._last_end, end)
        self._recorded += seconds

    def pending(self) -> float:
        """What the warehouse will bill this command beyond the statement time
        already recorded, if the command leaves it idle: what an estimate quotes
        before anything runs, and what the statement timeout holds back. Zero
        once settled.

        A suspend is priced in only where it is known to work: the setting is
        on, this command will be the one that resumes the warehouse, and the
        last suspend dex attempted on it succeeded. Otherwise the tail is
        quoted, the suspend is still attempted, and settlement books what
        happened, so a missing grant can over-quote a command and never
        under-quote it.
        """

        if self._settled:
            return 0.0
        before = self.before
        tail = float((before or {}).get("auto_suspend") or 0)
        resumed = self._found_suspended()
        if resumed and self.suspend_expected():
            tail = 0.0
        return self._owed(tail=tail, resumed=resumed)

    def quote(self) -> float:
        """The overhead an estimate carries: :meth:`pending`, the same figure
        on every call until a handshake admits it or a statement charges it,
        then zero.

        A command that prices many statements still resumes the warehouse at
        most once and leaves one tail, so the overhead is added once, by the
        handshake that shows the caller the estimate (see
        ``command_args.with_uptime``), and never per statement (verified live:
        mutation coverage once priced 39 statements at 2,379 seconds, of which
        2,340 were resume floors). It used to be handed to the first estimate
        that asked, and an estimate priced and then discarded (a profile phase
        with nothing stale) took it, so the estimate the caller saw quoted
        statement time alone (issue #936).
        """

        return 0.0 if self._admitted or self._charged else self.pending()

    def admit(self) -> None:
        """The overhead is in an estimate the cost gate admitted, so no later
        phase of this command quotes it again."""

        self._admitted = True

    def charge_share(self) -> float:
        """The same overhead, charged against the gate once on the confirmed
        run, by whichever billed statement goes first."""

        if self._charged:
            return 0.0
        self._charged = True
        return self.pending()

    def suspend_expected(self) -> bool:
        """Whether this command's quote may count on suspending the warehouse
        afterwards: the setting is on and the latest suspend dex recorded for
        this warehouse succeeded."""

        if self._suspend is None:
            return False
        name = str((self.before or {}).get("name") or "").upper()
        outcomes = [
            claim.get("suspend")
            for claim in self._ledger_claims()
            if str(claim.get("warehouse") or "").upper() == name
            and claim.get("suspend") is not None
        ]
        return bool(outcomes) and outcomes[-1] == "suspended"

    def estimate_note(self) -> str | None:
        """What the quoted overhead is, for the handshake beside the number."""

        before = self.before
        if before is None:
            return (
                "the warehouse's state could not be read, so the estimate "
                f"assumes it is suspended and carries the {_RESUME_MINIMUM_SECONDS:g}"
                "-second resume minimum"
            )
        name, auto_suspend = before["name"] or "the warehouse", before["auto_suspend"]
        if auto_suspend is None:
            return (
                f"AUTO_SUSPEND is off on {name}: it bills until it is suspended by "
                "hand, and no estimate can bound that idle time"
            )
        quoted = 0.0 if self._settled else self.pending()
        if not quoted:
            return None
        if self._found_suspended() and self.suspend_expected():
            return (
                f"includes {quoted:g} warehouse-seconds no statement will report: "
                f"{name} is suspended, so the first statement resumes it "
                f"(Snowflake bills at least {_RESUME_MINIMUM_SECONDS:g} seconds per "
                "resume), and snowflake.suspend_after_command suspends it once "
                "the command is done, so no idle tail is quoted"
            )
        if self._found_suspended():
            return (
                f"includes {quoted:g} warehouse-seconds no statement will "
                f"report: {name} is suspended, so the first statement resumes it "
                f"(Snowflake bills at least {_RESUME_MINIMUM_SECONDS:g} seconds per "
                f"resume), and it then idles {auto_suspend} seconds (AUTO_SUSPEND) "
                "before suspending"
            )
        prior = self._prior_claim()
        if prior is not None and quoted < auto_suspend:
            return (
                f"includes {quoted:g} warehouse-seconds no statement will report: "
                f"{name} is still in the idle tail a previous dex command already "
                "paid for, so this command is quoted only the time since that "
                "command's last statement, not a second "
                f"{auto_suspend}-second AUTO_SUSPEND tail"
            )
        return (
            f"includes {quoted:g} warehouse-seconds no statement will report: "
            f"{name} idles {auto_suspend} seconds (AUTO_SUSPEND) after the last "
            "statement before it suspends"
        )

    def tail_advice(self, work: float) -> str | None:
        """Name the fix where the idle tail, not the work, sets the price:
        what the work alone is, what AUTO_SUSPEND makes it bill, and what would
        bring it down. Advice only: dex never changes the warehouse."""

        before = self.before
        if before is None or before.get("auto_suspend") is None:
            return None
        auto_suspend = int(before["auto_suspend"])
        quoted = 0.0 if self._settled else self.pending()
        if auto_suspend <= _RESUME_MINIMUM_SECONDS or quoted <= max(work, 0.0):
            return None
        name = before["name"] or "the warehouse"
        return (
            f"this command's own work is about {work:.0f} warehouse-seconds, and "
            f"{name}'s {auto_suspend}-second AUTO_SUSPEND makes it bill about "
            f"{work + quoted:.0f}. A dedicated warehouse with AUTO_SUSPEND = 60, "
            f"or {_adoption(name, self._role)}, would bring it to about "
            f"{max(work, _RESUME_MINIMUM_SECONDS):.0f}. "
            "Dex never changes AUTO_SUSPEND itself"
        )

    def settle(self) -> tuple[float, list[str]]:
        """Book the command's overhead once its statements are done.

        The cost gate's ``settle_overhead``, and called directly by a command
        that runs dbt, so the overhead can join dbt's own settlement row and the
        command keeps one estimate-to-billed ratio. Idempotent: a second call
        returns nothing, so whichever runs first books it. Suspends the
        warehouse first where :meth:`_may_suspend` allows it, and records this
        command's claim on the warehouse's current resume cycle last.
        """

        if self._settled:
            return 0.0, []
        self._settled = True
        if self._first_start is None:
            return 0.0, []
        before = self.before
        try:
            after: dict | None = self._read_state()
        except Exception:
            after = None
        if (
            before is not None
            and after is not None
            and before["state"] != "STARTED"
            and after["state"] != "STARTED"
            and before["resumed_on"] is not None
            and before["resumed_on"] == after["resumed_on"]
        ):
            # Answered without waking it: a result-cache hit, a count served
            # from metadata, a statement refused at compile.
            return 0.0, []
        facts = after or before or {}
        name, auto_suspend = (
            facts.get("name") or "the warehouse",
            facts.get("auto_suspend"),
        )
        # Whatever the state reads: SHOW WAREHOUSES can still say SUSPENDED
        # while it resumes for another session's query, which is then the one
        # leaving it idle.
        busy = after is not None and (after["running"] > 0 or after["queued"] > 0)
        resumed = self._found_suspended()
        outcome = self._suspend() if self._may_suspend(after) else None
        notes: list[str] = []
        if outcome == "suspended":
            uptime = max(self._clock() - self._first_start, _RESUME_MINIMUM_SECONDS)
            overhead = max(uptime - self._recorded, 0.0)
            start = (
                f"This command resumed {name}, which bills at least "
                f"{_RESUME_MINIMUM_SECONDS:g} seconds per resume"
            )
            end = (
                "It then suspended it (snowflake.suspend_after_command), so no "
                "idle tail is charged"
            )
        else:
            tail = 0.0 if busy or auto_suspend is None else float(auto_suspend)
            overhead = self._owed(tail=tail, resumed=resumed)
            joined = (
                not resumed
                and self._prior_claim() is not None
                and overhead < self._owed(tail=tail, resumed=resumed, cycle=False)
            )
            start = (
                f"This command resumed {name}, which bills at least "
                f"{_RESUME_MINIMUM_SECONDS:g} seconds per resume"
                if resumed
                else f"{name} was already running"
            )
            if auto_suspend is None:
                end = (
                    "AUTO_SUSPEND is off, so it bills until it is suspended by hand; "
                    "that idle time is not in this figure, which is a lower bound"
                )
            elif busy:
                end = (
                    "Other queries were running on it when this command finished, "
                    "so no idle tail is charged"
                )
            elif joined:
                end = (
                    "A previous dex command in the same resume cycle already paid "
                    f"its {auto_suspend}-second AUTO_SUSPEND tail, so this command "
                    "is charged only the time it added to that claim: the gap "
                    "since that command's last statement, plus its own span"
                )
            elif after is None:
                end = (
                    "Its state afterwards could not be read, so its "
                    f"{auto_suspend}-second AUTO_SUSPEND tail is charged as if it "
                    "were left idle"
                )
            else:
                end = (
                    f"It was left idle, so its {auto_suspend}-second AUTO_SUSPEND "
                    "tail is charged"
                )
            if outcome == "refused_grant":
                notes.append(
                    f"snowflake.suspend_after_command is on, but this role may not "
                    f"suspend {name}, so it was left to idle out its AUTO_SUSPEND "
                    f"tail: grant it with `{_grant(name, self._role)}`"
                )
            elif outcome is not None and outcome != "suspended":
                notes.append(
                    f"snowflake.suspend_after_command is on, but suspending {name} "
                    f"did not take ({outcome}), so its AUTO_SUSPEND tail is charged"
                )
        self._claim(after, outcome)
        return overhead, [
            f"spend includes {overhead:.1f} warehouse-seconds no statement "
            "reported, because Snowflake bills uptime rather than statement time. "
            f"{start}. {end}. Modeled from Snowflake's billing rules, so "
            "approximate (its idle check runs about every 30 seconds); "
            "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY is the authority",
            *notes,
        ]

    # Clock skew tolerated between this machine and Snowflake when placing the
    # warehouse's resume against this command's first statement.
    _RESUME_SKEW_SECONDS = 15.0

    def _may_suspend(self, after: dict | None) -> bool:
        """Whether dex may suspend the warehouse now. Only one it woke and left
        idle: the setting is on; this command found it suspended; it reads
        STARTED with nothing running or queued; AUTO_RESUME is on, so the next
        query anyone sends wakes it rather than failing; and its ``resumed_on`` moved
        during this command's first statement, so the resume is this command's
        rather than another session's."""

        before = self.before
        if self._suspend is None or before is None or after is None:
            return False
        if before["state"] == "STARTED" or after["state"] != "STARTED":
            return False
        if after["running"] > 0 or after["queued"] > 0:
            return False
        if not after.get("auto_resume"):
            return False
        if after["resumed_on"] is None or after["resumed_on"] == before["resumed_on"]:
            return False
        resumed_at = after.get("resumed_at")
        if resumed_at is None or self._first_start is None or self._first_end is None:
            return False
        earliest = self._wall(self._first_start) - self._RESUME_SKEW_SECONDS
        latest = self._wall(self._first_end) + self._RESUME_SKEW_SECONDS
        return earliest <= resumed_at <= latest

    def _claim(self, after: dict | None, outcome: str | None) -> None:
        """Record what this command claimed of the warehouse's current resume
        cycle, for the next command to read back (see :meth:`_prior_claim`)."""

        if self._record_claim is None or self._last_end is None:
            return
        facts = after or {}
        if not facts.get("resumed_on") and outcome is None:
            return
        with contextlib.suppress(Exception):
            self._record_claim(
                {
                    "warehouse": facts.get("name"),
                    "resumed_on": facts.get("resumed_on"),
                    "last_statement_end": round(self._wall(self._last_end), 3),
                    "auto_suspend": facts.get("auto_suspend"),
                    "suspend": outcome,
                }
            )

    def _ledger_claims(self) -> list[dict]:
        if self._claims is None:
            self._claims = []
            if self._prior_claims is not None:
                with contextlib.suppress(Exception):
                    self._claims = [
                        c for c in self._prior_claims() if isinstance(c, dict)
                    ]
        return self._claims

    def _prior_claim(self) -> dict | None:
        """The latest claim a previous dex command recorded on the resume cycle
        this command found the warehouse in, or ``None``. Only a warehouse found
        running has one: a suspended warehouse starts a new cycle."""

        before = self.before
        if before is None or before["state"] != "STARTED" or not before["resumed_on"]:
            return None
        name = str(before["name"] or "").upper()
        matching = [
            claim
            for claim in self._ledger_claims()
            if str(claim.get("warehouse") or "").upper() == name
            and claim.get("resumed_on") == before["resumed_on"]
            and isinstance(claim.get("last_statement_end"), int | float)
        ]
        if not matching:
            return None
        return max(matching, key=lambda claim: claim["last_statement_end"])

    def _wall(self, monotonic: float) -> float:
        return monotonic + self._wall_offset

    def _found_suspended(self) -> bool:
        before = self.before
        return before is None or before["state"] != "STARTED"

    def _owed(self, *, tail: float, resumed: bool, cycle: bool = True) -> float:
        span = 0.0
        if self._first_start is not None and self._last_end is not None:
            span = self._last_end - self._first_start
        uptime = span + tail
        if resumed:
            uptime = max(uptime, _RESUME_MINIMUM_SECONDS)
        owed = max(uptime - self._recorded, 0.0)
        prior = self._prior_claim() if cycle and not resumed else None
        if prior is None:
            return owed
        # The previous claim ran to its last statement plus its tail; this
        # command's runs to its own last statement (or now, for a quote) plus
        # this tail. It owes the difference, never more than the model alone.
        end = self._wall(
            self._last_end if self._last_end is not None else self._clock()
        )
        claimed_until = float(prior["last_statement_end"]) + float(
            prior.get("auto_suspend") or 0
        )
        extends = (end + tail) - claimed_until - self._recorded
        return max(min(owed, extends), 0.0)


class SnowflakeAdapter:
    """Holds one Snowflake connection plus the cost gate for one command.

    ``connection`` is injectable (class DI) so unit tests drive a fake; the
    real connection is built by ``connect.py`` from discovered parameters.
    Credentials live only inside this process and are never surfaced.
    ``clock`` is injectable so the fake can simulate statement duration; it is
    what actual billed seconds are measured with. ``wall_clock`` stamps the
    warehouse claims a later command reads back, so it is epoch time.

    ``owns_connection`` is False when the connection came from outside dex (a host
    supplying its own principal). Then :meth:`close` leaves it open, because
    closing a handle the caller still holds would break the caller.
    """

    name = "snowflake"
    dialect = DIALECT
    paradigm = Paradigm.COMPUTE_TIME
    # Warehouse-seconds modelled from table statistics and the pinned size.
    estimate_quality = EstimateQuality.APPROXIMATE

    def __init__(
        self,
        *,
        connection: Any,
        cost_gate: CostGate,
        target: SnowflakeTarget | None = None,
        account: str | None = None,
        auth_method: str = "unknown",
        scope_override: list[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        owns_connection: bool = True,
    ):
        self._conn = connection
        self._owns_connection = owns_connection
        self.cost_gate = cost_gate
        self.target = target or SnowflakeTarget()
        self.account = account
        self.auth_method = auth_method
        # A per-command `--scope`, kept apart from the committed allowlist in the
        # target: it may only narrow that allowlist, and deciding whether it does
        # requires resolving bare schema names against the account first.
        self._scope_override = list(scope_override or [])
        self._clock = clock
        self._wall_clock = wall_clock
        # Imported lazily (the caller constructed the connection, so the
        # library is present); the error types drive refusal translation.
        from snowflake.connector import errors as sf_errors

        self._sf_errors = sf_errors
        self._visible_databases: set[str] | None = None
        self._dev_target: str | None = None
        self.forget_metadata()
        self._warehouse_info: dict | None = None
        self._notes: dict[str, list[str]] = {}
        self._uptime: _WarehouseUptime | None = None
        self._uptime_gate: CostGate | None = None
        self._session_prepared = False

    # --- capabilities (free) ---------------------------------------------------

    def capabilities(self) -> dict[str, object]:
        # SHOW DATABASES is the live probe: cloud-services layer, no warehouse,
        # and it fails on a stale or underprivileged credential.
        databases = self._database_scope()
        caps: dict[str, object] = {
            "connector": self.name,
            "dialect": self.dialect,
            "read_only": True,
            "paradigm": self.paradigm.value,
            "account": self.account,
            "auth_method": self.auth_method,
            "database_count": len(databases),
            "required_grants": [
                "USAGE on the pinned warehouse",
                "USAGE + SELECT (or IMPORTED PRIVILEGES) on source databases",
                *(
                    [
                        "OPERATE on the pinned warehouse "
                        "(snowflake.suspend_after_command)"
                    ]
                    if self.target.suspend_after_command
                    else []
                ),
            ],
        }
        cost = self.cost_gate.cost()
        budget: dict[str, object] = {
            "ceiling_seconds": cost.ceiling,
            "session_spent_today_seconds": self.cost_gate.session_spent_now(),
        }
        if self.target.warehouse:
            info = self._warehouse()
            state = self.warehouse_uptime.before or {}
            budget["warehouse"] = {
                "name": info["name"],
                "size": info["size"],
                "credits_per_hour": info["credits_per_hour"],
                "state": state.get("state", "unknown"),
                "auto_suspend_seconds": state.get("auto_suspend"),
            }
            budget["warehouse"]["suspend_after_command"] = self._suspend_report()
            if cost.ceiling is not None:
                budget["ceiling_credits"] = self._to_credits(cost.ceiling)
            warning = self._idle_tail_warning(info["name"], state)
            if warning:
                caps["warnings"] = [warning]
        else:
            caps["warnings"] = [
                "no snowflake.warehouse pinned in .dex/config.yml; metadata "
                "commands work, but nothing that scans will run until one is set"
            ]
        caps["budget"] = budget
        return caps

    def _suspend_report(self) -> dict[str, object]:
        """Whether dex suspends the warehouse after a command it woke, and what
        happened the last time it tried, from the claims in the spend ledger.
        ``last_outcome`` is ``None`` until a suspend has been attempted, since
        dex cannot test the OPERATE grant without suspending something."""

        enabled = bool(self.target.suspend_after_command)
        name = str(self.target.warehouse or "").upper()
        outcomes = [
            claim.get("suspend")
            for claim in (self.warehouse_uptime._ledger_claims() if enabled else [])
            if str(claim.get("warehouse") or "").upper() == name
            and claim.get("suspend") is not None
        ]
        return {"enabled": enabled, "last_outcome": outcomes[-1] if outcomes else None}

    def _idle_tail_warning(self, name: str, state: dict) -> str | None:
        """The advice ``connect test`` gives about the idle tail: AUTO_SUSPEND
        off is unbounded; a long AUTO_SUSPEND with no suspend costs every
        command its length; a suspend the role may not issue names the grant.
        Advice only: dex never changes the warehouse's settings."""

        if not state:
            return None
        auto_suspend = state.get("auto_suspend")
        if auto_suspend is None:
            return (
                f"AUTO_SUSPEND is off on {name}: it bills until it is "
                "suspended by hand, and dex can neither bound nor attribute that "
                "idle time; set AUTO_SUSPEND (60 seconds suits short commands)"
            )
        if self.target.suspend_after_command:
            if self._suspend_report()["last_outcome"] == "refused_grant":
                grant = _grant(name, self._session_role())
                return (
                    "snowflake.suspend_after_command is on, but the last suspend "
                    f"was refused: grant it with `{grant}`. "
                    "Until then every command that resumes "
                    f"{name} bills its {auto_suspend}-second AUTO_SUSPEND tail"
                )
            return None
        if auto_suspend <= _RESUME_MINIMUM_SECONDS:
            return None
        return (
            f"{name} idles {auto_suspend} seconds (AUTO_SUSPEND) before it "
            f"suspends, so a command that resumes it bills about {auto_suspend} "
            "warehouse-seconds even when its own work takes one or two. A "
            "dedicated warehouse with AUTO_SUSPEND = 60, or "
            f"{_adoption(name, self._session_role())}, brings that to about 60. "
            "Dex never changes AUTO_SUSPEND itself"
        )

    # --- introspection (free SHOW metadata; no warehouse, no billing) ----------

    def forget_metadata(self) -> None:
        """Drop everything this command has listed, because the warehouse changed.

        SHOW results are cached per command: the estimate pass and the confirmed
        run share table facts, and each SHOW is free but a round-trip. That
        holds while the warehouse holds still, and a build is the one command
        where it does not: dbt writes between the pricing pass and the
        verification that judges its output, so a schema that did not exist, or
        a table's row count, is stale by then.

        What survives is what this command has paid for or what dbt cannot
        change: the session, the warehouse facts, the uptime this command owes
        the warehouse, and the visible databases (dbt creates schemas, never a
        database).
        """

        self._objects: dict[str, dict] = {}
        # Row counts learned from a profiling aggregate, which for a view is the
        # only count there is: SHOW TABLES maintains none. Supersedes the SHOW
        # figure for the rest of the command, so uniqueness proofs and grain
        # verdicts compare against rows that were counted rather than reported.
        self._exact_rows: dict[str, int] = {}
        self._columns: dict[str, list[ColumnMeta]] = {}
        self._inventory_loaded = False
        self._resolved_scopes: list[str] | None = None
        self._schemas_by_database: dict[str, set[str]] = {}

    def read_dev_target(self, scope: str | None) -> None:
        """Also read ``DATABASE.SCHEMA``, the namespace this command's build
        writes into, or stop reading it with ``None``.

        Kept apart from ``target.databases`` because it is not a source the
        user committed: it is dex reading its own output for one command, and
        it may not exist yet (see :func:`with_dev_target`). A change forgets
        what was listed, since the scope that produced it has changed.
        """

        scope = scope.strip().upper() if scope else None
        if scope != self._dev_target:
            self._dev_target = scope
            self.forget_metadata()

    def list_objects(self, *, include_views: bool = True) -> list[ObjectMeta]:
        self._load_inventory()
        objects = [
            self._object_meta(entry)
            for entry in self._objects.values()
            if include_views or entry["object_type"] != "view"
        ]
        objects.sort(key=lambda o: o.identifier)
        return objects

    def table_metadata(self, identifier: str) -> tuple[ObjectMeta, list[ColumnMeta]]:
        self._load_inventory()
        entry = self._objects.get(identifier) or self._objects.get(identifier.upper())
        if entry is None:
            raise SnowflakeConnectionError(
                f"object '{identifier}' not found in the configured scope; "
                "check snowflake.databases in .dex/config.yml"
            )
        return self._object_meta(entry), self._column_metas(entry["identifier"])

    def table_notes(self, identifier: str) -> list[str]:
        """Data-quality notes the profiling run accumulated for one object
        (sampling degradation, skipped escalations). Merged into the dataset's
        ``data_quality`` by the profile engine."""

        return list(self._notes.get(identifier, []))

    def _load_inventory(self) -> None:
        if self._inventory_loaded:
            return
        for scope in self._scopes():
            in_clause = self._scope_clause(scope)
            # A database-wide SHOW lists that database's INFORMATION_SCHEMA
            # views too, which are never a source (see _RESERVED_SCHEMA): kept
            # out here, or a database scope profiles Snowflake's own catalog.
            for row in self._show(f"SHOW TABLES {in_clause}"):
                if not _reserved(row):
                    self._register(row, "table")
            for row in self._show(f"SHOW VIEWS {in_clause}"):
                if not _reserved(row):
                    self._register(row, "view")
            # One SHOW COLUMNS per scope (not per table) keeps inventory a
            # handful of round-trips. SHOW output is capped at 10K rows, which
            # a db.schema allowlist keeps comfortably distant.
            by_table: dict[str, list[ColumnMeta]] = {}
            for row in self._show(f"SHOW COLUMNS {in_clause}"):
                if _reserved(row):
                    continue
                identifier = self._identifier_of(row)
                columns = by_table.setdefault(identifier, [])
                columns.append(
                    ColumnMeta(
                        name=str(row["column_name"]),
                        data_type=self._render_type(str(row["data_type"])),
                        nullable=self._column_nullable(str(row["data_type"])),
                        ordinal=len(columns),
                    )
                )
            self._columns.update(by_table)
        for identifier, columns in self._columns.items():
            if identifier in self._objects:
                self._objects[identifier]["column_count"] = len(columns)
        self._inventory_loaded = True

    def _register(self, row: dict, object_type: str) -> None:
        identifier = self._identifier_of(row)
        # SHOW TABLES row/byte counts are metadata-maintained and free; views
        # have no stored rows (their count arrives via profiling).
        rows = row.get("rows") if object_type == "table" else None
        size = row.get("bytes") if object_type == "table" else None
        self._objects[identifier] = {
            "identifier": identifier,
            "object_type": object_type,
            "schema": str(row["schema_name"]),
            "name": str(row["name"]),
            "row_count": int(rows) if rows is not None else None,
            "byte_size": int(size) if size is not None else None,
            "column_count": 0,
        }

    @staticmethod
    def _identifier_of(row: dict) -> str:
        name = row.get("table_name") or row.get("name")
        return f"{row['database_name']}.{row['schema_name']}.{name}"

    def _object_meta(self, entry: dict) -> ObjectMeta:
        return ObjectMeta(
            identifier=entry["identifier"],
            object_type=entry["object_type"],
            schema=entry["schema"],
            name=entry["name"],
            row_count=self._exact_rows.get(entry["identifier"], entry["row_count"]),
            byte_size=entry["byte_size"],
            column_count=entry["column_count"],
        )

    def _column_metas(self, identifier: str) -> list[ColumnMeta]:
        columns = self._columns.get(identifier)
        if columns is None:
            columns = []
            rows = self._show(f"SHOW COLUMNS IN TABLE {self._quote(identifier)}")
            for ordinal, row in enumerate(rows):
                columns.append(
                    ColumnMeta(
                        name=str(row["column_name"]),
                        data_type=self._render_type(str(row["data_type"])),
                        nullable=self._column_nullable(str(row["data_type"])),
                        ordinal=ordinal,
                    )
                )
            self._columns[identifier] = columns
        return columns

    @staticmethod
    def _render_type(data_type_json: str) -> str:
        """SHOW COLUMNS carries the type as a JSON document; surface its type
        token (TEXT, FIXED, VARIANT, ...) rather than the raw JSON."""

        try:
            parsed = json.loads(data_type_json)
            return str(parsed.get("type", data_type_json))
        except (json.JSONDecodeError, AttributeError):
            return data_type_json

    @staticmethod
    def _column_nullable(data_type_json: str) -> bool:
        try:
            return bool(json.loads(data_type_json).get("nullable", True))
        except (json.JSONDecodeError, AttributeError):
            return True

    def _scopes(self) -> list[str]:
        """Every source scope this command reads, resolved and proven to exist,
        plus the dev target when this command reads one and it exists.

        Resolution is free (SHOW only, no warehouse) and cached for the command.
        It runs before anything is estimated, because an unresolvable scope that
        silently falls back to the whole allowlist is a cost-safety bug: the
        estimate the user confirms would cover tables they never named.
        """

        if self._resolved_scopes is None:
            self._resolved_scopes = self._resolve_scopes()
        return self._resolved_scopes

    def _resolve_scopes(self) -> list[str]:
        committed = self.target.databases
        if committed:
            origin = "snowflake.databases in .dex/config.yml"
            with blame(origin, SnowflakeConnectionError):
                configured = sorted(
                    {
                        self._resolve_scope(entry, sorted(self._databases()))
                        for entry in committed
                    }
                )
        else:
            configured = sorted(self._databases())

        def exists(scope: str) -> bool:
            database, _, schema = scope.partition(".")
            return database in self._databases() and schema in self._schemas(database)

        if not self._scope_override:
            return with_dev_target(configured, self._dev_target, exists)

        # A --scope searches only the databases the config already allows, so a
        # bare schema name can never resolve into a database outside the committed
        # boundary; a qualified one that tries is refused below.
        searchable = sorted({scope.split(".")[0] for scope in configured})
        with blame("--scope", SnowflakeConnectionError):
            requested = sorted(
                {
                    self._resolve_scope(entry, searchable)
                    for entry in self._scope_override
                }
            )
        outside = [scope for scope in requested if not scope_within(scope, configured)]
        if outside:
            raise SnowflakeConnectionError(
                f"scope {name_list(outside)} is outside the committed allowlist "
                f"(snowflake.databases: {name_list(configured)}); --scope narrows "
                "the configured scope, it never widens it"
            )
        return with_dev_target(requested, self._dev_target, exists)

    def _resolve_scope(self, entry: str, searchable: list[str]) -> str:
        """One scope entry, resolved to ``DATABASE`` or ``DATABASE.SCHEMA`` and
        proven to exist. ``searchable`` bounds where a bare schema may be found.

        Every failure path here names the entry and lists the near misses, because
        Snowflake's own answer to a bad scope is ``002043: Object does not exist``,
        which names neither the object nor the fix.
        """

        token = entry.strip().upper()
        if not token:
            raise SnowflakeConnectionError("empty scope entry")

        if "." in token:
            database, _, schema = token.partition(".")
            if "." in schema:
                raise SnowflakeConnectionError(
                    f"scope '{entry}' has too many parts; a source scope is "
                    "<database> or <database>.<schema>, never a table"
                )
            if database not in self._databases():
                reserved = (
                    f" (the account-internal {_RESERVED_DATABASE} database is never "
                    "a source)"
                    if database == _RESERVED_DATABASE
                    else ""
                )
                raise SnowflakeConnectionError(
                    f"scope '{entry}' names no database this role can see{reserved}; "
                    f"{self._visible_hint()}"
                )
            schemas = self._schemas(database)
            if schema not in schemas:
                raise SnowflakeConnectionError(
                    f"scope '{entry}' does not exist: database {database} has no "
                    f"schema {schema}; schemas there: {name_list(sorted(schemas))}"
                )
            return f"{database}.{schema}"

        if token in self._databases():
            return token

        # Qualifying a bare schema costs one SHOW SCHEMAS per candidate database.
        # That is free but not instant, so a wide-open account is asked to qualify
        # rather than made to wait through hundreds of round-trips.
        if len(searchable) > _BARE_SCOPE_SEARCH_LIMIT:
            raise SnowflakeConnectionError(
                f"scope '{entry}' is a bare schema name and this role can see "
                f"{len(searchable)} databases; qualify it as <database>.{token}, or "
                "narrow snowflake.databases in .dex/config.yml first"
            )

        matches = sorted(db for db in searchable if token in self._schemas(db))
        if len(matches) == 1:
            return f"{matches[0]}.{token}"
        if not matches:
            schemas = sorted({s for db in searchable for s in self._schemas(db)})
            raise SnowflakeConnectionError(
                f"scope '{entry}' names no database and no schema in "
                f"{name_list(searchable)}; schemas there: {name_list(schemas)}; "
                f"{self._visible_hint()}"
            )
        raise SnowflakeConnectionError(
            f"scope '{entry}' is ambiguous: it names a schema in "
            f"{name_list(matches)}; qualify it as <database>.{token}"
        )

    def _visible_hint(self) -> str:
        return f"visible databases: {name_list(sorted(self._databases()))}"

    def _databases(self) -> set[str]:
        """Every database the role can see. Free, and doubles as the live
        credential probe: SHOW DATABASES fails on a stale or underprivileged
        credential."""

        if self._visible_databases is None:
            self._visible_databases = {
                str(row["name"]).upper() for row in self._show("SHOW DATABASES")
            } - {_RESERVED_DATABASE}
        return self._visible_databases

    def _schemas(self, database: str) -> set[str]:
        """Every source schema of one database. Free, and cached: a bare scope
        entry searches several databases and must not re-ask for each."""

        if database not in self._schemas_by_database:
            rows = self._show(f"SHOW SCHEMAS IN DATABASE {_quote_ident(database)}")
            self._schemas_by_database[database] = {
                str(row["name"]).upper() for row in rows
            } - {_RESERVED_SCHEMA}
        return self._schemas_by_database[database]

    def _database_scope(self) -> list[str]:
        """Distinct databases across the resolved scopes."""

        return sorted({scope.split(".")[0] for scope in self._scopes()})

    def missing_dev_namespaces(self, database: str) -> list[str]:
        """Which parts of a dbt dev target do not exist yet. Free: SHOW only.

        dbt creates schemas but never databases, so ``dev_schema`` is deliberately
        not checked: its absence is normal on a first build. A missing database is
        fatal, and left to dbt it surfaces from the ``list_schemas`` macro as
        ``002043: Object does not exist``, naming neither the database nor the fix.
        The list shape (rather than a bool) is what the catalog-plus-schema
        connectors will want when they grow the same preflight.
        """

        rows = self._show(f"SHOW DATABASES LIKE '{_escape_literal(database.upper())}'")
        return [] if rows else [f'dev_database "{database}"']

    def list_namespace_objects(self, database: str, schema: str) -> list[str]:
        """Table and view names already in one schema. Free: SHOW only, no
        warehouse. A schema (or database) that does not exist holds nothing to
        collide with, so the ProgrammingError it raises reads as empty."""

        scope = f"{_quote_ident(database.upper())}.{_quote_ident(schema.upper())}"
        names: set[str] = set()
        for kind in ("TABLES", "VIEWS"):
            try:
                rows = self._show(f"SHOW {kind} IN SCHEMA {scope}")
            except self._sf_errors.ProgrammingError:
                return []
            names.update(str(row["name"]) for row in rows)
        return sorted(names)

    def _scope_clause(self, scope: str) -> str:
        if "." in scope:
            db, schema = scope.split(".", 1)
            return f"IN SCHEMA {_quote_ident(db)}.{_quote_ident(schema)}"
        return f"IN DATABASE {_quote_ident(scope)}"

    # --- the warehouse and the credit translation -------------------------------

    def _warehouse(self) -> dict:
        """The pinned warehouse's facts that hold still (size, credit rate),
        fetched free via SHOW WAREHOUSES and cached for the adapter's life; what
        moves (state, load, auto-suspend) belongs to :attr:`warehouse_uptime`,
        which this seeds from the same SHOW. Refuses when the config pins
        nothing or the pin does not resolve: dex never spends on a warehouse
        the config does not name."""

        if self._warehouse_info is None:
            state = self._read_warehouse()  # refuses when nothing is pinned
            self.warehouse_uptime.seed(state)
        return self._warehouse_info

    def _read_warehouse(self) -> dict:
        """One free SHOW WAREHOUSES for the pinned warehouse: caches the facts
        that hold still the first time, and returns the live state. The uptime
        model's ``read_state``, so its before and after readings cost one SHOW
        each and the first one also serves :meth:`_warehouse`."""

        pinned = self.target.warehouse
        if not pinned:
            raise SnowflakeConnectionError(
                "no warehouse pinned: set snowflake.warehouse in "
                ".dex/config.yml (dex never spends on a connection-default "
                "warehouse)"
            )
        rows = self._show(f"SHOW WAREHOUSES LIKE '{_escape_literal(pinned)}'")
        if not rows:
            raise SnowflakeConnectionError(
                f"warehouse '{pinned}' not found or not granted; create it or "
                "grant USAGE to this role, or pin a different "
                "snowflake.warehouse in .dex/config.yml"
            )
        row = rows[0]
        if self._warehouse_info is None:
            size_token = (
                str(row.get("size", "")).upper().replace("-", "").replace(" ", "")
            )
            credits = _CREDITS_PER_HOUR.get(size_token)
            generation = str(row.get("resource_constraint") or "")
            gen2 = "GEN_2" in generation.upper()
            if credits is not None and gen2:
                credits *= _GEN2_MULTIPLIER
            self._warehouse_info = {
                "name": str(row.get("name", pinned)),
                "size": str(row.get("size", "unknown")),
                "credits_per_hour": credits,
                "gen2": gen2,
            }
        return _live_state(row)

    @property
    def warehouse_uptime(self) -> _WarehouseUptime | None:
        """This command's uptime model, or ``None`` when no warehouse is pinned
        and nothing can run. Rebuilt whenever the cost gate is, and installed on
        it, so the gate books what the model owes when the command settles.
        A command that runs dbt reports dbt's statements to it through
        :meth:`_WarehouseUptime.observe`."""

        if not self.target.warehouse:
            return None
        if self._uptime is None or self._uptime_gate is not self.cost_gate:
            gate = self.cost_gate
            self._uptime = _WarehouseUptime(
                read_state=self._read_warehouse,
                clock=self._clock,
                wall_clock=self._wall_clock,
                suspend=(
                    self._suspend_warehouse
                    if self.target.suspend_after_command
                    else None
                ),
                prior_claims=getattr(gate, "warehouse_claims", None),
                record_claim=getattr(gate, "record_claim", None),
                role=self._session_role(),
            )
            self._uptime_gate = self.cost_gate
            self.cost_gate.settle_overhead = self._uptime.settle
        return self._uptime

    def _session_role(self) -> str | None:
        """The role this session runs as, from the login response: no statement.
        ``None`` where the connection does not say (a test double)."""

        role = getattr(self._conn, "role", None)
        return role if isinstance(role, str) and role else None

    def _to_credits(self, seconds: float) -> float | None:
        info = self._warehouse_info or (
            self._warehouse() if self.target.warehouse else None
        )
        if not info or info.get("credits_per_hour") is None:
            return None
        return round(seconds * info["credits_per_hour"] / 3600.0, 6)

    def describe_estimate(
        self, estimate: float, per_table: dict[str, float] | None = None
    ) -> dict:
        """The compute-time handshake payload: seconds are the binding number,
        credits (and dollars when the price is configured) ride alongside as
        the familiar translation."""

        data: dict[str, object] = {
            "estimated_seconds": estimate,
            "estimate_quality": "heuristic",
            "hint": (
                "review the estimate, then re-run with --confirm --budget "
                "<seconds> (the ceiling in warehouse-seconds; the same number "
                "becomes the server-side statement timeout)"
            ),
            "notes": [_ESTIMATE_QUALITY_NOTE],
        }
        uptime = self.warehouse_uptime
        uptime_note = uptime.estimate_note() if uptime is not None else None
        if uptime_note:
            data["notes"].append(uptime_note)
        if uptime is not None:
            work = sum(
                seconds
                for line, seconds in (per_table or {}).items()
                if line != UPTIME_LINE
            )
            advice = uptime.tail_advice(
                work if per_table else estimate - uptime.quote()
            )
            if advice:
                data["notes"].append(advice)
        if per_table:
            data["per_table_seconds"] = per_table
        credits = self._to_credits(estimate)
        if credits is not None:
            info = self._warehouse_info or {}
            data["estimated_credits"] = credits
            data["credit_rate"] = {
                "warehouse": info.get("name"),
                "size": info.get("size"),
                "credits_per_hour": info.get("credits_per_hour"),
                "approximate": bool(info.get("gen2")),
            }
            if self.target.credit_price_usd is not None:
                data["estimated_usd"] = round(credits * self.target.credit_price_usd, 4)
        return data

    def spend_display(self) -> dict:
        """Credit translation of actual seconds, merged into ``data.spend``."""

        return self.compute_spend_translation(
            self.cost_gate.spend_summary()["seconds_billed"]
        )

    def compute_spend_translation(self, seconds: float) -> dict[str, float]:
        """Translate settled warehouse-seconds into credits (and dollars when
        ``snowflake.credit_price_usd`` is set), for a command the gate settled
        and for a dbt run alike, so a build reports the unit Snowflake bills
        in exactly as an explore scan does."""

        translated: dict[str, float] = {}
        credits = self._to_credits(seconds)
        if credits is not None:
            translated["credits_billed"] = credits
            if self.target.credit_price_usd is not None:
                translated["usd_billed"] = round(
                    credits * self.target.credit_price_usd, 4
                )
        return translated

    # --- estimation (free; feeds the confirm handshake) -------------------------

    def profile_estimate(
        self, identifiers: list[str], *, include_blobs: set[str] | None = None
    ) -> tuple[float, dict[str, float]]:
        """The heuristic warehouse-seconds estimate for profiling: per table,
        its bytes over the size-scaled scan rate times the number of aggregate
        batches. Free: everything comes from SHOW metadata. The uptime no
        statement will report (the resume minimum and the idle tail) is not in
        it: the handshake that shows the estimate adds it once per command
        (:meth:`_WarehouseUptime.quote`).

        Blob-type columns are excluded from the batch count the same way
        ``explore.profile.profile`` excludes them from the scan itself
        (``include_blobs`` names the ``identifier.column`` paths a human
        opted back in), so this estimate's batch count matches what the run
        will actually issue."""

        blob_paths = include_blobs or set()
        per_table: dict[str, float] = {}
        for identifier in identifiers:
            meta, columns = self.table_metadata(identifier)
            scan_columns = [
                c
                for c in columns
                if not is_blob_type(c.data_type)
                or f"{identifier}.{c.name}".lower() in blob_paths
            ]
            batches = max((len(scan_columns) + _COLUMN_BATCH - 1) // _COLUMN_BATCH, 1)
            per_table[identifier] = batches * self._scan_seconds(meta.byte_size)
        return sum(per_table.values()), per_table

    def query_estimate(self, sql: str) -> float:
        """The heuristic estimate for one firewall-approved query: the summed
        bytes of every referenced table over the scan rate. Like
        :meth:`profile_estimate`, the warehouse's uptime is the handshake's to
        add, once."""

        checked = assert_select_only(sql, dialect=self.dialect)
        return self._statement_estimate(checked)

    def _statement_estimate(self, sql: str) -> float:
        total_bytes = 0
        known = 0
        self._load_inventory()
        for identifier in self._referenced_tables(sql):
            entry = self._objects.get(identifier.upper())
            if entry and entry["byte_size"] is not None:
                total_bytes += entry["byte_size"]
                known += 1
        if known == 0:
            return _MIN_STATEMENT_SECONDS
        return self._scan_seconds(total_bytes)

    def _scan_seconds(self, byte_size: int | None) -> float:
        if not byte_size:
            return _MIN_STATEMENT_SECONDS
        # Bigger warehouses scan proportionally faster; the size multiple is
        # the credit rate stripped of any Gen2 billing markup (Gen2 bills more
        # per second without scanning proportionally more).
        size_factor = 1.0
        info = self._warehouse_info
        if info and info.get("credits_per_hour"):
            size_factor = info["credits_per_hour"]
            if info.get("gen2"):
                size_factor /= _GEN2_MULTIPLIER
        rate = _XSMALL_SCAN_BYTES_PER_SECOND * max(size_factor, 1.0)
        return max(byte_size / rate, _MIN_STATEMENT_SECONDS)

    def _referenced_tables(self, sql: str) -> set[str]:
        try:
            import sqlglot
            from sqlglot import expressions as sqlglot_exp

            parsed = sqlglot.parse_one(sql, read=self.dialect)
        except Exception:
            return set()
        return {
            ".".join(part for part in (t.catalog, t.db, t.name) if part)
            for t in parsed.find_all(sqlglot_exp.Table)
        }

    # --- profiling (billed; every statement estimated and gated) ----------------

    def column_aggregates(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        *,
        safe_min_max: set[str] | None = None,
        shape_stats: set[str] | None = None,
        type_stats: set[str] | None = None,
        key_shape_stats: set[str] | None = None,
        temporal_stats: set[str] | None = None,
    ) -> list[ColumnAggregate]:
        safe = safe_min_max or set()
        shape = shape_stats or set()
        type_req = type_stats or set()
        key_shape_req = key_shape_stats or set()
        temporal_req = temporal_stats or set()
        meta, _ = self.table_metadata(identifier)
        sample_percent = self._sample_percent(identifier, meta.byte_size)
        results: list[ColumnAggregate] = []
        for start in range(0, len(columns), _COLUMN_BATCH):
            batch = columns[start : start + _COLUMN_BATCH]
            sql, plan = self._build_aggregate_sql(
                identifier,
                batch,
                safe,
                shape,
                type_req,
                key_shape_req,
                temporal_req,
                sample_percent=sample_percent,
            )
            rows, labels = self._execute(
                sql, estimate=self._scan_seconds(meta.byte_size)
            )
            values = dict(zip(labels, rows[0], strict=True))
            if sample_percent is None:
                # The batch counted the table exactly, and for a view that is the
                # only count available: capture it so the metadata re-read after
                # this scan can hand it to the probes that decline to run without
                # a row count. Not under sampling, where the count is the
                # sample's rather than the table's.
                self._exact_rows[identifier] = int(values["n_total"])
            results.extend(
                self._read_aggregates(values, plan, sampled=sample_percent is not None)
            )
        return results

    def _sample_percent(self, identifier: str, byte_size: int | None) -> float | None:
        threshold = self.target.max_full_profile_bytes
        if threshold is None or not byte_size or byte_size <= threshold:
            return None
        percent = max(round(100.0 * threshold / byte_size, 2), 0.01)
        self._note(
            identifier,
            f"profiled from a ~{percent}% block sample (table exceeds "
            "snowflake.max_full_profile_bytes); counts and extremes are "
            "approximate and uniqueness is not judged",
        )
        return percent

    def _build_aggregate_sql(
        self,
        identifier: str,
        columns: list[ColumnMeta],
        safe: set[str],
        shape: set[str],
        type_req: set[str],
        key_shape_req: set[str],
        temporal_req: set[str],
        *,
        sample_percent: float | None = None,
    ) -> tuple[str, list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]]]:
        # One aggregate statement per batch: COUNT(*) once, then per column a
        # non-null count, an approximate distinct, min/max only where allowed,
        # and value-shape/type-contradiction/key-shape/temporal-continuity
        # fractions only where requested. Pure (no connection), so the
        # SELECT-only property is testable offline. Semi-structured columns
        # get the non-null count only (APPROX_COUNT_DISTINCT and MIN/MAX are
        # invalid or meaningless on them).
        source = self._quote(identifier)
        if sample_percent is not None:
            source += f" SAMPLE SYSTEM ({sample_percent})"
        select_parts = ["COUNT(*) AS n_total"]
        plan: list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]] = []
        for i, col in enumerate(columns):
            qcol = _quote_ident(col.name)
            nested = self._is_nested(col.data_type)
            select_parts.append(f"COUNT({qcol}) AS nn_{i}")
            wants_distinct = not nested
            if wants_distinct:
                select_parts.append(f"APPROX_COUNT_DISTINCT({qcol}) AS nd_{i}")
            wants_min_max = (col.name in safe) and not nested
            if wants_min_max:
                select_parts.append(f"MIN({qcol}) AS mn_{i}")
                select_parts.append(f"MAX({qcol}) AS mx_{i}")
            wants_shape = (col.name in shape) and not nested
            if wants_shape:
                select_parts.extend(shape_stat_expressions(qcol, i, _regexp_predicate))
            wants_type = (col.name in type_req) and not nested
            if wants_type:
                select_parts.extend(
                    type_contradiction_expressions(
                        qcol,
                        i,
                        is_string=is_string_type(col.data_type),
                        is_integer=is_integer_type(col.data_type),
                        regexp_predicate=_regexp_predicate,
                        bigint_type=_BIGINT_TYPE,
                    )
                )
            wants_key_shape = (col.name in key_shape_req) and not nested
            if wants_key_shape:
                select_parts.extend(key_shape_expressions(qcol, i, _regexp_predicate))
            wants_temporal = (col.name in temporal_req) and not nested
            if wants_temporal:
                select_parts.extend(
                    temporal_alignment_expressions(qcol, i, _date_trunc_expr)
                )
                for unit in temporal_units_for(col.data_type):
                    select_parts.extend(
                        temporal_continuity_sql(
                            qcol,
                            i,
                            unit,
                            source,
                            _date_trunc_expr,
                            _date_diff_expr,
                        )
                    )
            plan.append(
                (
                    i,
                    col,
                    wants_distinct,
                    wants_min_max,
                    wants_shape,
                    wants_type,
                    wants_key_shape,
                    wants_temporal,
                )
            )
        # Interpolated parts are quoted identifiers and fixed aggregate
        # keywords, never values; the result is guarded as a read-only SELECT.
        sql = f"SELECT {', '.join(select_parts)} FROM {source}"  # noqa: S608
        return assert_select_only(sql, dialect=self.dialect), plan

    @staticmethod
    def _is_nested(data_type: str) -> bool:
        return data_type.upper().startswith(_NESTED_TYPE_PREFIXES)

    @staticmethod
    def _read_aggregates(
        values: dict,
        plan: list[tuple[int, ColumnMeta, bool, bool, bool, bool, bool, bool]],
        *,
        sampled: bool,
    ) -> list[ColumnAggregate]:
        n_total = int(values["n_total"])
        aggregates: list[ColumnAggregate] = []
        for (
            i,
            col,
            wants_distinct,
            wants_min_max,
            wants_shape,
            wants_type,
            wants_key_shape,
            wants_temporal,
        ) in plan:
            nn = values.get(f"nn_{i}")
            null_fraction = (
                (1 - int(nn) / n_total) if nn is not None and n_total > 0 else None
            )
            distinct = (
                int(values[f"nd_{i}"]) if wants_distinct and n_total > 0 else None
            )
            # Under sampling, counts describe the sample, so a uniqueness
            # verdict would be unfounded either way.
            is_unique = (
                (distinct == int(nn) == n_total and n_total > 0)
                if distinct is not None and nn is not None and not sampled
                else None
            )
            aggregates.append(
                ColumnAggregate(
                    name=col.name,
                    null_fraction=null_fraction,
                    distinct_count=distinct,
                    is_unique=is_unique,
                    min_value=(
                        json_safe(values.get(f"mn_{i}")) if wants_min_max else None
                    ),
                    max_value=(
                        json_safe(values.get(f"mx_{i}")) if wants_min_max else None
                    ),
                    upper_vocab_fraction=shape_stat_value(
                        values, f"su_{i}", wants_shape
                    ),
                    person_shape_fraction=shape_stat_value(
                        values, f"sp_{i}", wants_shape
                    ),
                    avg_token_count=shape_stat_value(values, f"st_{i}", wants_shape),
                    **type_contradiction_aggregate_kwargs(values, i, wants_type),
                    **key_shape_aggregate_kwargs(values, i, wants_key_shape),
                    **temporal_continuity_aggregate_kwargs(values, i, wants_temporal),
                )
            )
        return aggregates

    def exact_distinct_counts(
        self, identifier: str, columns: list[str]
    ) -> dict[str, int]:
        """Exact COUNT(DISTINCT) for near-unique columns, spent only within the
        already-confirmed budget: when the remaining budget cannot cover the
        extra scan, return nothing and let uniqueness verdicts stay
        approximate. A metered adapter never self-escalates past its ceiling.
        """

        if not columns:
            return {}
        meta, _ = self.table_metadata(identifier)
        estimate = self._scan_seconds(meta.byte_size)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "distinct-count escalation skipped: the remaining budget could "
                "not cover the extra scan; uniqueness verdicts stay approximate",
            )
            return {}
        select_parts = [
            f"COUNT(DISTINCT {_quote_ident(name)}) AS d_{i}"
            for i, name in enumerate(columns)
        ]
        sql = assert_select_only(
            f"SELECT {', '.join(select_parts)} FROM {self._quote(identifier)}",  # noqa: S608
            dialect=self.dialect,
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        return {name: int(values[f"d_{i}"]) for i, name in enumerate(columns)}

    def distinct_combination_counts(
        self, identifier: str, combinations: list[list[str]]
    ) -> dict[tuple[str, ...], int]:
        """Exact distinct count per column combination, spent only within the
        already-confirmed budget. Each combination is a further scan, so when
        the budget cannot cover all of them the probe narrows to the pairs it
        can afford (they arrive best-ranked first) and says so, rather than
        giving up the grain wholesale. A metered adapter never self-escalates
        past its ceiling.
        """

        if not combinations:
            return {}
        meta, _ = self.table_metadata(identifier)
        unit = self._scan_seconds(meta.byte_size)
        probed, note = affordable_combinations(
            combinations,
            lambda prefix: unit * len(prefix),
            self.cost_gate.try_charge,
        )
        if note:
            self._note(identifier, note)
        if not probed:
            return {}
        sql = assert_select_only(
            distinct_combination_sql(self._quote(identifier), probed, _quote_ident),
            dialect=self.dialect,
        )
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        return {tuple(combo): int(values[f"d_{i}"]) for i, combo in enumerate(probed)}

    def value_domain_counts(
        self, identifier: str, columns: list[str], *, limit: int
    ) -> dict[str, ValueDomainSample]:
        """Top ``limit`` values by frequency per column, plus each column's
        exact distinct-group count, spent only within the already-confirmed
        budget: when the remaining budget cannot cover the extra scan,
        return nothing and report no value domain. A metered adapter never
        self-escalates past its ceiling.
        """

        if not columns:
            return {}
        meta, _ = self.table_metadata(identifier)
        estimate = self._scan_seconds(meta.byte_size)
        if not self.cost_gate.try_charge(estimate):
            self._note(
                identifier,
                "value-domain probe skipped: the remaining budget could not "
                "cover the extra scan; no value domain reported",
            )
            return {}
        table = self._quote(identifier)
        parts = []
        for i, name in enumerate(columns):
            qcol = _quote_ident(name)
            parts.append(
                "(SELECT ARRAY_AGG(OBJECT_CONSTRUCT('v', v, 'c', c)) "  # noqa: S608
                "WITHIN GROUP (ORDER BY c DESC, v) FROM "
                f"(SELECT {qcol} AS v, COUNT(*) AS c FROM {table} "
                f"WHERE {qcol} IS NOT NULL GROUP BY {qcol} ORDER BY c DESC, v "
                f"LIMIT {limit}) AS q_{i}) AS d_{i}"
            )
            parts.append(
                f"(SELECT COUNT(*) FROM (SELECT DISTINCT {qcol} FROM {table} "  # noqa: S608
                f"WHERE {qcol} IS NOT NULL) AS n_{i}) AS n_{i}"
            )
        sql = assert_select_only(f"SELECT {', '.join(parts)}", dialect=self.dialect)
        rows, labels = self._run(sql)
        values = dict(zip(labels, rows[0], strict=True))
        result = {}
        for i, name in enumerate(columns):
            domain = values[f"d_{i}"]
            if isinstance(domain, str):
                domain = json.loads(domain)
            result[name] = ValueDomainSample(
                values=[(entry["v"], entry["c"]) for entry in (domain or [])],
                total_distinct=int(values[f"n_{i}"]),
            )
        return result

    # --- execution (the single billed door) --------------------------------------

    def pin_utc_session(self) -> None:
        """Run this adapter's later statements in a UTC session.

        For SQL rendered by a layer that assumes one: Cube's own drivers set the
        session time zone to UTC, and a statement it renders reads a timestamp
        with no zone through that setting, so run under another zone it buckets
        rows differently. Only UTC, so nothing caller-supplied is interpolated.
        """

        self._utc_session = True

    def describe_query(self, sql: str) -> list[ColumnMeta]:
        """Output columns of one SELECT, compiled and not run: the connector's
        ``describe`` is served by the cloud-services layer with no warehouse."""

        from snowflake.connector.constants import FIELD_ID_TO_NAME

        checked = assert_select_only(sql, dialect=self.dialect)
        columns: list[ColumnMeta] = []
        for ordinal, field in enumerate(self._conn.cursor().describe(checked) or []):
            kind = FIELD_ID_TO_NAME.get(field.type_code, str(field.type_code))
            if kind == "FIXED":
                kind = f"NUMBER({field.precision},{field.scale})"
            columns.append(
                ColumnMeta(
                    name=str(field.name),
                    data_type=kind,
                    nullable=bool(field.is_nullable),
                    ordinal=ordinal,
                )
            )
        return columns

    def run_query(
        self,
        sql: str,
        *,
        max_rows: int,
        timeout_seconds: float,
    ) -> QueryResult:
        """Execute one firewall-approved SELECT, bounded in rows, wall time,
        and warehouse-seconds (client preflight plus the server-side statement
        timeout)."""

        rows, labels, types = self._execute_query(
            sql,
            estimate=self._statement_estimate(sql),
            timeout_seconds=timeout_seconds,
            max_rows=max_rows,
        )
        return QueryResult(
            columns=labels,
            types=types,
            cells=[[json_safe(v) for v in row] for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )

    def _execute(self, sql: str, *, estimate: float) -> tuple[list, list[str]]:
        """SELECT-only guard, heuristic charge, then the timeout-capped run."""

        assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(estimate + self._uptime_share())
        return self._run(sql)

    def _execute_query(
        self, sql: str, *, estimate: float, timeout_seconds: float, max_rows: int
    ) -> tuple[list, list[str], list[str]]:
        assert_select_only(sql, dialect=self.dialect)
        self.cost_gate.charge(estimate + self._uptime_share())
        cursor = self._billed_cursor()
        started = self._clock()
        try:
            cursor.execute(sql, timeout=int(max(timeout_seconds, 1)))
            rows = cursor.fetchmany(max_rows + 1)
        except self._sf_errors.ProgrammingError as exc:
            self._record_elapsed(started, cursor, sql)
            raise self._translate(exc, timeout_seconds) from exc
        self._record_elapsed(started, cursor, sql)
        labels = [d[0].lower() for d in cursor.description]
        types = [self._description_type(d) for d in cursor.description]
        return rows, labels, types

    def _run(self, sql: str) -> tuple[list, list[str]]:
        """The single billed door past the gate: statement timeout set to the
        remaining budget, wall-clock elapsed recorded to the ledger."""

        cursor = self._billed_cursor()
        started = self._clock()
        try:
            cursor.execute(sql)
            rows = cursor.fetchall()
        except self._sf_errors.ProgrammingError as exc:
            self._record_elapsed(started, cursor, sql)
            raise self._translate(exc, None) from exc
        self._record_elapsed(started, cursor, sql)
        labels = [d[0].lower() for d in cursor.description]
        return rows, labels

    def _billed_cursor(self):
        """A cursor prepared for spend: warehouse pinned (strict), query tag
        set, and the server-side statement timeout wound down to what remains
        of the budget once the uptime this command will still owe is held back,
        so even a wrong heuristic cannot overrun the ceiling."""

        info = self._warehouse()  # refuses when config pins no warehouse
        remaining = self.cost_gate.statement_cap(
            unit="warehouse-second", reserve=self.warehouse_uptime.pending()
        )
        cursor = self._conn.cursor()
        if not self._session_prepared:
            # dex-built session statements, not agent SQL: the warehouse
            # name comes from validated SHOW output and the tag is a constant.
            cursor.execute(f"USE WAREHOUSE {_quote_ident(info['name'])}")
            cursor.execute("ALTER SESSION SET QUERY_TAG = 'dex'")
            self._session_prepared = True
        if getattr(self, "_utc_session", False):
            # dex-built session statement, a constant.
            cursor.execute("ALTER SESSION SET TIMEZONE = 'UTC'")
        if remaining is not None:
            cursor.execute(
                "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = "
                f"{max(int(remaining), 1)}"
            )
        return cursor

    def _record_elapsed(self, started: float, cursor: Any, sql: str) -> None:
        elapsed = max(self._clock() - started, 0.0)
        self.cost_gate.record_billed(
            elapsed,
            job_id=getattr(cursor, "sfqid", None),
            statement=sql,
        )
        self.warehouse_uptime.observe(elapsed)

    def _uptime_share(self) -> float:
        uptime = self.warehouse_uptime
        return uptime.charge_share() if uptime is not None else 0.0

    def _suspend_warehouse(self) -> str:
        """Suspend the pinned warehouse, the one statement dex issues that acts
        on the account rather than on its own session (issue #664).

        Typed so nothing but this can be rendered: the name is the pinned
        warehouse's, as SHOW WAREHOUSES spells it, and it must equal the pin
        (SHOW's LIKE treats ``_`` as a wildcard, so a near-miss name is
        refused rather than suspended). It runs on a plain cursor, outside the
        SELECT-only door and the query firewall, which go on refusing every
        ALTER. Only :class:`_WarehouseUptime` calls it, and only on a
        warehouse this command woke and left idle. Suspending stops compute and
        touches no data, schema or file; Snowflake lets running statements
        finish first. Returns ``suspended``, ``refused_grant`` (the role lacks
        OPERATE), ``already_suspended``, or ``failed``.
        """

        info = self._warehouse()
        name = info["name"]
        if name.upper() != str(self.target.warehouse or "").upper():
            return "failed"
        cursor = self._conn.cursor()
        try:
            cursor.execute(f"ALTER WAREHOUSE {_quote_ident(name)} SUSPEND")
        except self._sf_errors.ProgrammingError as exc:
            message = str(exc).lower()
            if (
                "insufficient privileges" in message
                or getattr(exc, "errno", None) == 3001
            ):
                return "refused_grant"
            if "cannot be suspended" in message or "invalid state" in message:
                return "already_suspended"
            return "failed"
        except Exception:
            return "failed"
        return "suspended"

    def _translate(self, exc: Exception, timeout_seconds: float | None) -> Exception:
        message = str(exc)
        if "statement or warehouse timeout" in message.lower():
            return OverCeilingError(
                "the statement hit the server-side timeout derived from the "
                "remaining budget (STATEMENT_TIMEOUT_IN_SECONDS); raise "
                "--budget or narrow the work"
            )
        if timeout_seconds is not None and (
            "canceled" in message.lower() or "cancelled" in message.lower()
        ):
            return TimeoutError(
                f"query exceeded {timeout_seconds:g}s and was cancelled; "
                "narrow it (tighter filter, fewer columns) and retry"
            )
        # Everything else the server refused (a compilation error, a missing
        # grant, an unsupported construct). Typed, so the envelope carries
        # `execution_failure` and Snowflake's own words rather than the
        # `internal` an untyped driver exception falls through to. No separate
        # code: the driver already prefixes its message with the error number
        # and the SQLSTATE.
        return warehouse_refusal(message)

    @staticmethod
    def _description_type(description: Any) -> str:
        type_code = description[1]
        return str(getattr(type_code, "name", type_code))

    # --- helpers ------------------------------------------------------------------

    def _show(self, sql: str) -> list[dict]:
        """Free metadata door: SHOW commands only, engine-built, served by the
        cloud-services layer with no warehouse. Results come back as dicts
        keyed by the (lowercase) SHOW column names."""

        if not sql.upper().startswith("SHOW "):
            raise ValueError("only SHOW statements pass through the metadata door")
        cursor = self._conn.cursor()
        cursor.execute(sql)
        labels = [d[0].lower() for d in cursor.description]
        return [dict(zip(labels, row, strict=True)) for row in cursor.fetchall()]

    def _note(self, identifier: str, note: str) -> None:
        notes = self._notes.setdefault(identifier, [])
        if note not in notes:
            notes.append(note)

    @staticmethod
    def _split(identifier: str) -> tuple[str, str, str]:
        parts = identifier.rsplit(".", 2)
        if len(parts) != 3:
            raise ValueError(f"expected database.schema.table, got '{identifier}'")
        return parts[0], parts[1], parts[2]

    def _quote(self, identifier: str) -> str:
        return ".".join(_quote_ident(p) for p in self._split(identifier))

    def close(self) -> None:
        # A host-supplied connection stays open: dex closes only what it opened.
        if not self._owns_connection:
            return
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()


def _quote_ident(name: str) -> str:
    """Quote one identifier component with double quotes (preserving case,
    which unquoted Snowflake identifiers would fold to upper), doubling
    embedded quotes."""

    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _escape_literal(value: str) -> str:
    return value.replace("'", "''")
