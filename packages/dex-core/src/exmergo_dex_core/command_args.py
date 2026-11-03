"""Argument-to-engine bridges and plumbing shared by the command orchestrators.

Two jobs. The first is resolving where a CLI run is rooted, which is a question
only the CLI asks. The second is the cost-before-spend handshake every billed
command goes through, shared here so explore, transform, and maintain cannot
drift apart on the one thing that governs spend.

Nothing here opens a connection. That is :meth:`DexEngine._adapter`, and it is the
only opener, so credential discovery and the cost gate have exactly one seam.
"""

from __future__ import annotations

import argparse
import inspect
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .adapters.base import Adapter
from .config import CONFIG_FILE
from .envelope import Cost
from .guards.cost_guard import (
    SUGGESTED_SESSION_CEILING_MULTIPLE,
    UPTIME_LINE,
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
    SessionCeilingDecisionRequiredError,
    suggested_session_ceiling,
)
from .results import ConfirmationRequest, Result
from .storage import DEX_DIR

#: How many upper-bound lines the note names before counting the rest; the
#: structured `upper_bounds` key carries every one.
_UPPER_BOUND_LINES_SHOWN = 10


def resolve_dex_root(start: Path) -> Path | None:
    """Nearest ancestor of ``start`` holding a ``.dex/config.yml``, or None.

    dex is used like git or dbt: the config lives at the project root, but
    commands are run from anywhere inside the tree. So resolution walks up rather
    than trusting the current directory to be the root. The enclosing git repo is
    the ceiling, and a project without one does not walk above ``start`` at all,
    so a stray ``~/.dex/config.yml`` can never capture a session run from inside a
    real project. Anchoring on the file (not just a ``.dex/`` directory) means a
    subdirectory that holds only a ``.dex/`` cache never shadows the real config
    higher up.
    """

    start = start.resolve()
    ceiling = start
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            ceiling = directory
            break
    for directory in (start, *start.parents):
        if (directory / DEX_DIR / CONFIG_FILE).is_file():
            return directory
        if directory == ceiling:
            break
    return None


def repo_root(args: argparse.Namespace) -> str:
    """The project root every command-layer path keys off (config load, cache
    store, dbt discovery, dev-target preflight). Resolved by walking up from the
    ``--repo-root`` run directory to the ``.dex/config.yml`` that owns it; when
    none is found the raw value is returned unchanged, so the no-config paths
    (an explicit ``--connector``/``--path`` read, or the loud refusal in
    ``open_adapter``) behave as designed."""

    raw = getattr(args, "repo_root", ".")
    resolved = resolve_dex_root(Path(raw))
    return str(resolved) if resolved is not None else raw


def command_name(args: argparse.Namespace) -> str:
    """The subcommand as the contract spells it, for ledger entries and payloads."""

    group = getattr(args, "group", None)
    subcommand = getattr(args, "subcommand", None)
    return " ".join(part for part in (group, subcommand) if part)


def cost_gate(adapter: Adapter) -> CostGate | None:
    """The adapter's cost gate when it is a billed connector; free adapters
    (DuckDB) have none, and their commands stay confirmation-free."""

    return getattr(adapter, "cost_gate", None)


def preflight_cost(adapter: Adapter) -> Cost:
    """The cost to report for a command that never priced anything.

    The free metadata commands (``inventory``, ``connect test``, the free drift
    axes) still report a cost, because its paradigm tells a caller what the
    *next* command will bill in. A free connector has no gate, so the paradigm
    comes from the adapter itself rather than from a default: ``free_local`` is
    DuckDB's own answer here, never a placeholder for not having asked.
    """

    gate = cost_gate(adapter)
    return gate.cost() if gate is not None else Cost(paradigm=adapter.paradigm)


def _estimate_payload(
    command: str, adapter: Adapter, estimate: float, per_table: dict | None
) -> dict:
    """The connector's own description of an estimate, or the bytes default.

    Shared by the two asks the whole-command handshake can raise, so both speak
    one vocabulary: an adapter that translates its unit (Snowflake's credits,
    ClickHouse's compute-unit-hours) describes its estimate the same way
    whichever ask the caller is answering.
    """

    describe = getattr(adapter, "describe_estimate", None)
    if describe is not None:
        return {"command": command, **describe(estimate, per_table)}
    data = {
        "command": command,
        "estimated_bytes": estimate,
        "hint": (
            "review the estimate, then re-run with --confirm --budget "
            "<bytes> (the ceiling in bytes; 10000000000 is 10 GB, about "
            "$0.06 on-demand)"
        ),
    }
    if per_table:
        data["per_table_bytes"] = per_table
    return data


def session_ceiling_ask(
    command: str,
    adapter: Adapter,
    gate: CostGate,
    estimate: float,
    *,
    per_table: dict[str, float] | None = None,
) -> dict:
    """The payload for the one-time cumulative-ceiling ask (issue #283).

    Built on the same estimate description the cost ask carries, because picking
    a ceiling for the day is the same judgement as picking one for the command,
    made in the same unit and from the same number. The connector's own
    ``hint`` is replaced rather than appended to: it names ``--confirm
    --budget``, and a caller who reached this ask has already passed both.
    """

    suggested = suggested_session_ceiling(estimate)
    unit = gate.paradigm.value
    data = _estimate_payload(command, adapter, estimate, per_table)
    instruction = (
        "nothing bounds the day's total spend for this project: "
        "budget.session_ceiling is unset in .dex/config.yml, so this command "
        "and every one after it is bound by its own budget alone. Re-run with "
        f"--session-ceiling {suggested:.0f} to set one (in {unit}, "
        f"{SUGGESTED_SESSION_CEILING_MULTIPLE:.0f}x this command's estimate as "
        "a starting point -- pass your own number instead), or with "
        "--no-session-ceiling to record that this project runs unbounded. "
        "Either answer is written to .dex/config.yml and you are asked once; "
        "neither changes the per-command --budget"
    )
    data.update(
        {
            "session_ceiling": None,
            "suggested_session_ceiling": suggested,
            "hint": instruction,
            # The same sentence under a key nothing else writes. `to_envelope`
            # merges a two-phase command's own payload over the pending ask's,
            # and a command that returns findings alongside the ask (`maintain
            # check`, `maintain semantic`) carries its own `hint`, so `hint`
            # alone is not a channel this ask can rely on reaching the caller.
            "session_ceiling_hint": instruction,
        }
    )
    return data


def upper_bound_notes(
    upper_bounds: dict[str, str] | None, per_table: dict[str, float] | None
) -> list[str]:
    """One sentence naming the estimate's lines that are upper bounds, or none.

    A line priced as though its filter pruned nothing is still the right number
    to reserve against, since the job can bill no more than it; what it cannot
    be is read as what the job will bill. The sentence says how much of the
    estimate those lines are, so an operator can discount them deliberately
    rather than guess at an over-estimate (issue #323).
    """

    if not upper_bounds:
        return []
    lines = sorted(upper_bounds)
    amount = sum((per_table or {}).get(line, 0.0) for line in lines)
    shown = ", ".join(lines[:_UPPER_BOUND_LINES_SHOWN])
    more = (
        f", and {len(lines) - _UPPER_BOUND_LINES_SHOWN} more"
        if len(lines) > _UPPER_BOUND_LINES_SHOWN
        else ""
    )
    return [
        f"{len(lines)} line(s) of this estimate, {amount:,.0f} in all, are upper "
        f"bounds rather than measurements ({shown}{more}): each one's incremental "
        "batch filter is bounded by a subquery the dry run cannot prune on, and "
        "the warehouse prunes it when the job runs, so these lines will likely "
        "bill less"
    ]


def with_uptime(
    adapter: Adapter, estimate: float, per_table: dict[str, float] | None
) -> tuple[float, dict[str, float] | None, Any]:
    """Add the warehouse's uptime overhead to an estimate a handshake is about
    to show, once per command (issues #656, #936).

    The overhead (Snowflake's resume minimum and idle tail) belongs to the
    command, not to any statement, so the estimators leave it out and the two
    handshakes add it here: whichever priced phase the caller actually sees
    carries it, and a phase priced and then discarded cannot take it. A caller
    that already broke it out as :data:`UPTIME_LINE` (a build, mutation
    coverage) is left as it is. Returns the estimate, the breakdown, and the
    uptime model to :meth:`admit` once the gate has admitted the estimate, or
    ``None`` on a connector that bills no uptime.
    """

    uptime = getattr(adapter, "warehouse_uptime", None)
    if uptime is None:
        return estimate, per_table, None
    if per_table is not None and UPTIME_LINE in per_table:
        return estimate, per_table, uptime
    overhead = uptime.quote()
    if overhead <= 0:
        return estimate, per_table, uptime
    if per_table is not None:
        per_table = {UPTIME_LINE: overhead, **per_table}
    return estimate + overhead, per_table, uptime


def billed_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    per_table: dict[str, float] | None = None,
    notes: list[str] | None = None,
    axes: list[str] | None = None,
    upper_bounds: dict[str, str] | None = None,
) -> None:
    """The cost-before-spend handshake on billed connectors.

    The estimate comes from free dry-runs, so the unconfirmed pass spends
    nothing: it either passes the gate (confirmed, within budget) or raises
    :class:`~.results.ConfirmationRequired` carrying the payload the caller
    surfaces before re-issuing confirmed. Raising rather than returning is what
    lets the same handshake serve an API, where a transport object would be
    meaningless. Over-ceiling and no-ceiling refusals propagate as their own
    errors, because confirmation cannot override either.

    Free connectors have no gate, so this is a no-op on them and their commands
    stay confirmation-free.

    ``upper_bounds`` names the lines of ``per_table`` the estimator priced as
    though nothing pruned them, with why (issue #323). Both the ask and the
    over-ceiling refusal carry them, because the refusal is the message an
    operator acts on, and without them a build refused on an estimate it would
    never bill reads as a genuinely expensive one.
    """

    gate = cost_gate(adapter)
    if gate is None:
        return
    estimate, per_table, uptime = with_uptime(adapter, estimate, per_table)
    # What a connector can say about the composition of the estimate it just
    # priced, asked once and used by both refusals below. Only this handshake
    # asks: it is the one whose estimate the connector's profile pricing built,
    # and a mid-command phase (verify probes) prices something else entirely.
    reserve = getattr(adapter, "profile_reserve", None)
    reserve = reserve(estimate) if reserve is not None else None
    try:
        gate.preflight_command(estimate)
    except SessionCeilingDecisionRequiredError as exc:
        # Before the plain confirmation clause below, which would otherwise
        # catch this subclass and answer it with the cost ask's payload: the two
        # asks are re-issued with different flags, so they cannot share a hint.
        exc.request = ConfirmationRequest(
            cost=exc.cost,
            data=session_ceiling_ask(
                command, adapter, gate, estimate, per_table=per_table
            ),
            warnings=gate.warnings(),
        )
        raise
    except OverCeilingError as exc:
        # A refusal is the message an operator acts on and the one that carries
        # the least. Without this, an estimate padded with escalation reserve
        # reads as work the warehouse is about to do, and the only way to tell
        # the two apart is to reconstruct the split from the spend ledger
        # afterwards (issue #299). Re-raised rather than mutated so the
        # exception stays a plain immutable refusal, carrying the same cost.
        explained = [
            *([reserve["note"]] if reserve is not None else []),
            *upper_bound_notes(upper_bounds, per_table),
        ]
        if not explained:
            raise
        raise OverCeilingError(
            ". ".join([str(exc), *explained]), cost=exc.cost
        ) from exc
    except ConfirmationRequiredError as exc:
        # The payload speaks the connector's unit. An adapter that knows more
        # than the raw magnitude (Snowflake's credit translation, its
        # estimate-quality caveat) describes its own estimate; the bytes shape
        # is the default the bytes-scanned connectors settled on.
        data = _estimate_payload(command, adapter, estimate, per_table)
        if gate.session_ceiling_pending(estimate):
            # The cost ask comes first, and a caller who reads it can answer
            # both in one re-run instead of confirming the cost and meeting the
            # cumulative-ceiling ask on the next round trip (issue #283).
            suggested = suggested_session_ceiling(estimate)
            advice = (
                "this project has not decided whether the day's total spend is "
                "bounded, and the confirmed run will stop to ask: add "
                f"--session-ceiling {suggested:.0f} to set one "
                f"({SUGGESTED_SESSION_CEILING_MULTIPLE:.0f}x this estimate, in "
                f"{gate.paradigm.value}) or --no-session-ceiling to record that "
                "it runs unbounded, and answer both in one re-run"
            )
            data["suggested_session_ceiling"] = suggested
            # `hint` belongs to the cost ask here, since that is what this
            # payload is answered with; the advice rides in `notes` and again
            # under its own key, which no merge can overwrite.
            data["session_ceiling_hint"] = advice
            data.setdefault("notes", [])
            data["notes"] = [*data["notes"], advice]
        if reserve is not None:
            # Prose and flat keys both: the note is what a person reads when
            # deciding whether raising the budget buys work or headroom, and
            # the keys are so a host branching on the split does not have to
            # parse the sentence to find it.
            data["reserved_bytes"] = reserve["reserved_bytes"]
            data["reserved_queries"] = reserve["reserved_queries"]
            data["notes"] = [*data.get("notes", []), reserve["note"]]
        if upper_bounds:
            # Keyed like `per_table_bytes`, so a host can discount exactly those
            # lines without parsing the note that explains them.
            data["upper_bounds"] = dict(upper_bounds)
            data["notes"] = [
                *data.get("notes", []),
                *upper_bound_notes(upper_bounds, per_table),
            ]
        if axes:
            # What the estimate would add. Load-bearing for an offer, whose
            # envelope reports `ok`: without it, an axis that did not run is
            # indistinguishable from one that ran and found nothing.
            data["axes"] = axes
        if notes:
            data.setdefault("notes", [])
            data["notes"] = [*data["notes"], *notes]
        # The handshake is where a caller picks a budget, so it is the one place
        # a missing cumulative cap can still change what they choose.
        exc.request = ConfirmationRequest(
            cost=exc.cost, data=data, warnings=gate.warnings()
        )
        raise
    if uptime is not None:
        uptime.admit()


def confirmation_request(
    command: str,
    adapter: Adapter,
    estimate: float,
    **kwargs,
) -> ConfirmationRequest | None:
    """The confirm handshake as a returned request rather than a raise.

    The third spelling of one handshake, beside :func:`billed_handshake` (which
    raises) and :func:`verify_handshake` (which prices a mid-command phase).
    Raise-versus-return is a property of the caller's situation, not of the
    gate: a command whose free half has already produced real findings cannot
    let this raise, because discarding them to ask about the billed half would
    make the caller pay attention twice for one answer. That is ``maintain
    check`` and ``maintain semantic``, whose free axes always complete.

    Those two carry the returned request as ``Result.pending_offer`` rather than
    ``pending_confirmation``, because the caller never asked for the scanning
    axes: the request is priced work on offer, not a charge dex is waiting on.
    Pass ``axes`` so the offer names what the estimate would add.
    """

    try:
        billed_handshake(command, adapter, estimate, **kwargs)
    except ConfirmationRequiredError as exc:
        return exc.request
    return None


def _render_phase_hint(
    hint: Callable[..., str],
    cost: float,
    paradigm: str,
    budget: int,
    *,
    confirmed: bool,
) -> str:
    """Format a phase checkpoint hint, passing ``confirmed`` if the hint accepts it."""

    try:
        sig = inspect.signature(hint)
        if "confirmed" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            return hint(cost, paradigm, budget, confirmed=confirmed)
        if any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()
        ):
            return hint(cost, paradigm, budget)
        positional_count = sum(
            1
            for p in sig.parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        )
        if positional_count >= 4:
            return hint(cost, paradigm, budget, confirmed)
        return hint(cost, paradigm, budget)
    except (ValueError, TypeError):
        try:
            return hint(cost, paradigm, budget, confirmed=confirmed)
        except TypeError:
            return hint(cost, paradigm, budget)


def phase_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    phase: str,
    hint: Callable[..., str],
    per_table_key: str | None = None,
    extra: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    skip: bool = False,
) -> ConfirmationRequest | None:
    """The mid-command checkpoint every two-phase command shares.

    Some work cannot be priced until earlier work has been paid for: overlap
    probes need the candidates inference found, a cluster sample needs the
    profile that chose its features, a build's row counts need the relations
    the build wrote. All of them run on an already-confirmed command, so they
    gate on :meth:`CostGate.preflight_phase`, which extends the reservation the
    command is already holding rather than booking a second one, and asks only
    when the phase would not fit what remains of the ceiling.

    Returning rather than raising is the rule those callers share and the
    reason this is one function: a command that has already spent must not
    discard what it bought in order to ask about the rest, because the caller
    would then pay for the same scan twice. ``None`` is the ordinary outcome,
    and it means the confirmed budget already covers the phase.

    ``hint`` is handed the estimate, the paradigm's name, and the whole-command
    budget a re-run needs, and writes the sentence saying what was found and
    what to re-run. ``skip`` is the caller's own "nothing to price" test, which
    differs per phase and is not this function's to guess.
    """

    gate = cost_gate(adapter)
    if gate is None or skip:
        return None
    phase_lines = {per_table_key: estimate} if per_table_key else None
    estimate, phase_lines, uptime = with_uptime(adapter, estimate, phase_lines)
    try:
        gate.preflight_phase(estimate)
    except ConfirmationRequiredError as exc:
        per_table = phase_lines if per_table_key else None
        describe = getattr(adapter, "describe_estimate", None)
        if describe is not None:
            data = {"command": command, **describe(estimate, per_table)}
        elif per_table is not None:
            data = {
                "command": command,
                "estimated_bytes": estimate,
                "per_table_bytes": per_table,
            }
        else:
            data = {"command": command, "estimated_bytes": estimate}
        data.update(
            {
                "phase": phase,
                **(extra or {}),
                "hint": _render_phase_hint(
                    hint,
                    estimate,
                    gate.paradigm.value,
                    math.ceil(exc.cost.estimate),
                    confirmed=gate.confirmed,
                ),
            }
        )
        extra_notes = list(notes or [])
        if (
            gate.session_ceiling is not None
            and gate.session_spent is not None
            and gate.session_ceiling - gate.session_spent < exc.cost.estimate
        ):
            cap = gate.session_ceiling_cap
            extra_notes.append(
                "the session budget is the binding ceiling; raising --budget "
                "alone will not unlock this, raise the session budget in "
                ".dex/config.yml instead"
                if cap is None or gate.session_ceiling < cap.value
                else f"the session budget is the binding ceiling, and it is "
                f"{cap.named()}: neither --budget nor .dex/config.yml can raise "
                "it, only the host can"
            )
        if extra_notes:
            data["notes"] = [*data.get("notes", []), *extra_notes]
        return ConfirmationRequest(cost=exc.cost, data=data)
    if uptime is not None:
        uptime.admit()
    return None


def verify_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    candidate_count: int,
    object_count: int,
) -> ConfirmationRequest | None:
    """The checkpoint for ``--verify``'s overlap-probe phase.

    Verify probes can only be priced after profiling finds the candidate
    relationships, which is the original case :func:`phase_handshake` exists
    for. The caller carries the returned request on its result as
    ``pending_confirmation``, because ``--verify`` is work it was asked for.
    """

    def hint(cost: float, paradigm: str, budget: int, confirmed: bool = True) -> str:
        condition = (
            "beyond what remains of the confirmed budget"
            if confirmed
            else "and needs confirming"
        )
        re_run_note = " (a re-run re-profiles first)" if confirmed else ""
        return (
            f"found {candidate_count} candidate relationship(s) across "
            f"{object_count} object(s); verifying them all is estimated "
            f"at {cost:.0f} {paradigm} {condition}. Profiles and unverified "
            "relationships are already saved to the exploration cache; "
            f"re-run the same command with --confirm --budget {budget} "
            f"to profile and verify in one pass{re_run_note}"
        )

    return phase_handshake(
        command,
        adapter,
        estimate,
        phase="verify",
        # Same aggregate key maintain grain uses for probe pricing, so agents
        # see one vocabulary for overlap-probe cost across commands.
        per_table_key="(join overlap probes)",
        extra={"candidate_count": candidate_count, "object_count": object_count},
        skip=candidate_count == 0,
        hint=hint,
    )


def overlap_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    candidate_count: int,
    object_count: int,
    cap: int,
    elided: int,
) -> ConfirmationRequest | None:
    """The checkpoint for ``--infer-by-overlap``'s sweep phase.

    The sweep's candidate pool can only be built once inference (and
    ``--verify``, if also requested) has run, so it prices its own batch of
    probes after that.

    ``cap``/``elided`` ride the payload unconditionally (not only when they
    bind), so a caller sees the sweep's bound even on a confirmed run and never
    has to guess whether the reported candidate count is the whole pool or a
    capped slice of it.
    """

    def hint(cost: float, paradigm: str, budget: int, confirmed: bool = True) -> str:
        condition = (
            "beyond what remains of the confirmed budget"
            if confirmed
            else "and needs confirming"
        )
        re_run_note = " (a re-run re-profiles first)" if confirmed else ""
        return (
            f"found {candidate_count} unmatched key-shaped column "
            f"pair(s) across {object_count} object(s) to probe for "
            f"value overlap; probing them all is estimated at "
            f"{cost:.0f} {paradigm} {condition}. Relationships found so "
            "far are already saved to the exploration cache; re-run "
            f"the same command with --confirm --budget {budget} to "
            f"profile, infer, and sweep in one pass{re_run_note}"
        )

    return phase_handshake(
        command,
        adapter,
        estimate,
        phase="overlap",
        per_table_key="(overlap sweep probes)",
        extra={
            "candidate_count": candidate_count,
            "object_count": object_count,
            "cap": cap,
            "elided": elided,
        },
        skip=candidate_count == 0,
        hint=hint,
    )


def cumulative_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    candidate_count: int,
    object_count: int,
) -> ConfirmationRequest | None:
    """The checkpoint for ``--check-cumulative``'s window-function phase, which
    can only be priced once profiling has found an entity/temporal pair to
    test."""

    def hint(cost: float, paradigm: str, budget: int, confirmed: bool = True) -> str:
        condition = (
            "beyond what remains of the confirmed budget"
            if confirmed
            else "and needs confirming"
        )
        re_run_note = " (a re-run re-profiles first)" if confirmed else ""
        return (
            f"found {candidate_count} entity/temporal candidate(s) "
            f"across {object_count} object(s); checking them all for "
            f"cumulative measures is estimated at {cost:.0f} "
            f"{paradigm} {condition}. Profiles are already saved to the "
            "exploration cache; re-run the same command with "
            f"--confirm --budget {budget} to "
            f"profile and check in one pass{re_run_note}"
        )

    return phase_handshake(
        command,
        adapter,
        estimate,
        phase="check-cumulative",
        per_table_key="(cumulative-measure probes)",
        extra={"candidate_count": candidate_count, "object_count": object_count},
        skip=candidate_count == 0,
        hint=hint,
    )


def sample_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    notes: list[str] | None = None,
) -> ConfirmationRequest | None:
    """The checkpoint for a scan whose price needed an earlier scan.

    Clustering picks its feature columns out of a profile, so its sample
    statement does not exist until that profile has been paid for. No
    per-table breakdown: one statement over one object is not a breakdown.
    """

    def hint(cost: float, paradigm: str, budget: int, confirmed: bool = True) -> str:
        condition = (
            "beyond what remains of the confirmed budget"
            if confirmed
            else "and needs confirming"
        )
        return (
            "the profile this command needed is done and saved; the "
            f"sample scan is estimated at {cost:.0f} "
            f"{paradigm} {condition}. Re-run with --confirm --budget {budget}; "
            "the cached profile is reused, so the re-run pays for the "
            "sample alone"
        )

    return phase_handshake(
        command,
        adapter,
        estimate,
        phase="sample",
        notes=notes,
        hint=hint,
    )


_ResultT = TypeVar("_ResultT", bound=Result)


def stamp_spend(result: _ResultT, adapter: Adapter) -> _ResultT:
    """Stamp the preflight cost and the actual spend onto a result.

    ``cost`` stays a preflight estimate by contract; actual billed bytes or
    seconds land in ``spend``, which the shim surfaces under ``data.spend``. A
    result already carrying a cost (a phase checkpoint priced its own ask) keeps
    it; only the spend summary is refreshed. Free connectors have no gate and so
    report no spend at all, rather than a row of zeroes.

    This is the settlement funnel for every billed command that returns a
    result, so it is where the gate releases the headroom it booked to be
    admitted. Settling before reading the summary is what makes
    ``session_spent_today`` the day's total rather than this command's share of
    it.
    """

    gate = cost_gate(adapter)
    if gate is not None:
        spend = settled_spend(adapter)
        if result.cost.estimate is None:
            result.cost = gate.cost()
        result.spend = spend
        # Settlement is the honest moment to say what did not bind: the command
        # has spent, and the caller is looking at the number. What settling
        # added beyond the statements is explained beside it.
        result.warnings = [
            *result.warnings,
            *gate.warnings(),
            *gate.settlement_notes,
        ]
    return result


def settled_spend(adapter: Adapter) -> dict | None:
    """Settle the gate and read back what this command actually billed, in the
    connector's own unit plus whatever translation the adapter adds.

    Split out of :func:`stamp_spend` because the failure path needs the same
    number and has no ``Result`` to stamp it onto: a command that died partway
    still burned the seconds it burned, and an error envelope reporting nothing
    tells a caller on a metered connector that its failure was free. ``None``
    on a free connector, which has no gate and so no spend to report rather
    than a row of zeroes.
    """

    gate = cost_gate(adapter)
    if gate is None:
        return None
    gate.settle()
    spend = gate.spend_summary()
    display = getattr(adapter, "spend_display", None)
    if display is not None:
        spend.update(display())
    return spend
