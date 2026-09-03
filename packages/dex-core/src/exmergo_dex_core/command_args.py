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
import math
from pathlib import Path

from .adapters.base import Adapter
from .config import CONFIG_FILE
from .envelope import Cost
from .guards.cost_guard import (
    SUGGESTED_SESSION_CEILING_MULTIPLE,
    ConfirmationRequiredError,
    CostGate,
    OverCeilingError,
    SessionCeilingDecisionRequiredError,
    suggested_session_ceiling,
)
from .results import ConfirmationRequest, Result
from .storage import DEX_DIR


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


def billed_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    per_table: dict[str, float] | None = None,
    notes: list[str] | None = None,
    axes: list[str] | None = None,
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
    """

    gate = cost_gate(adapter)
    if gate is None:
        return
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
        if reserve is None:
            raise
        raise OverCeilingError(f"{exc}. {reserve['note']}", cost=exc.cost) from exc
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


def verify_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    candidate_count: int,
    object_count: int,
) -> ConfirmationRequest | None:
    """The mid-command checkpoint for the verify phase on billed connectors.

    Verify probes can only be priced after profiling finds the candidate
    relationships, so this runs after inference on an already-confirmed
    command: the probes are dry-run priced (free), and only when that estimate
    does not fit what remains of the confirmed budget does it return the
    request, which the caller carries on its result as
    ``pending_confirmation``. Otherwise the confirmed budget already covers
    verify and the command proceeds in one pass.

    A request rather than a raise, unlike :func:`billed_handshake`, because the
    profiles and unverified relationships up to this point are already paid for.
    Raising would discard them and bill the user twice for the same scan.
    """

    gate = cost_gate(adapter)
    if gate is None or candidate_count == 0:
        return None
    try:
        gate.preflight_phase(estimate)
    except ConfirmationRequiredError as exc:
        # Same aggregate key maintain grain uses for probe pricing, so agents
        # see one vocabulary for overlap-probe cost across commands.
        per_table = {"(join overlap probes)": estimate}
        describe = getattr(adapter, "describe_estimate", None)
        if describe is not None:
            data = {"command": command, **describe(estimate, per_table)}
        else:
            data = {
                "command": command,
                "estimated_bytes": estimate,
                "per_table_bytes": per_table,
            }
        data.update(
            {
                "phase": "verify",
                "candidate_count": candidate_count,
                "object_count": object_count,
                "hint": (
                    f"found {candidate_count} candidate relationship(s) across "
                    f"{object_count} object(s); verifying them all is estimated "
                    f"at {estimate:.0f} {gate.paradigm.value} beyond what "
                    "remains of the confirmed budget. Profiles and unverified "
                    "relationships are already saved to the exploration cache; "
                    "re-run the same command with --confirm --budget "
                    f"{math.ceil(exc.cost.estimate)} to profile and verify in "
                    "one pass (a re-run re-profiles first)"
                ),
            }
        )
        if (
            gate.session_ceiling is not None
            and gate.session_spent is not None
            and gate.session_ceiling - gate.session_spent < exc.cost.estimate
        ):
            data.setdefault("notes", [])
            data["notes"] = [
                *data["notes"],
                "the session budget is the binding ceiling; raising --budget "
                "alone will not unlock this, raise the session budget in "
                ".dex/config.yml instead",
            ]
        return ConfirmationRequest(cost=exc.cost, data=data)
    return None


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
    """The mid-command checkpoint for ``--infer-by-overlap``'s sweep phase on
    billed connectors, structurally the same two-phase pattern as
    :func:`verify_handshake`.

    The sweep's candidate pool can only be built once inference (and
    ``--verify``, if also requested) has run, so this prices the sweep's own
    batch of probes after that, on an already-confirmed command: a dry-run
    estimate that fits what remains of the confirmed budget proceeds in one
    pass, and one that doesn't returns the request rather than raising, since
    everything found so far is already paid for.

    ``cap``/``elided`` are carried on the checkpoint payload unconditionally
    (not only when they bind), so a caller sees the sweep's bound even on a
    confirmed run and never has to guess whether the reported candidate count
    is the whole pool or a capped slice of it.
    """

    gate = cost_gate(adapter)
    if gate is None or candidate_count == 0:
        return None
    try:
        gate.preflight_phase(estimate)
    except ConfirmationRequiredError as exc:
        per_table = {"(overlap sweep probes)": estimate}
        describe = getattr(adapter, "describe_estimate", None)
        if describe is not None:
            data = {"command": command, **describe(estimate, per_table)}
        else:
            data = {
                "command": command,
                "estimated_bytes": estimate,
                "per_table_bytes": per_table,
            }
        data.update(
            {
                "phase": "overlap",
                "candidate_count": candidate_count,
                "object_count": object_count,
                "cap": cap,
                "elided": elided,
                "hint": (
                    f"found {candidate_count} unmatched key-shaped column "
                    f"pair(s) across {object_count} object(s) to probe for "
                    f"value overlap; probing them all is estimated at "
                    f"{estimate:.0f} {gate.paradigm.value} beyond what "
                    "remains of the confirmed budget. Relationships found so "
                    "far are already saved to the exploration cache; re-run "
                    "the same command with --confirm --budget "
                    f"{math.ceil(exc.cost.estimate)} to profile, infer, and "
                    "sweep in one pass (a re-run re-profiles first)"
                ),
            }
        )
        if (
            gate.session_ceiling is not None
            and gate.session_spent is not None
            and gate.session_ceiling - gate.session_spent < exc.cost.estimate
        ):
            data.setdefault("notes", [])
            data["notes"] = [
                *data["notes"],
                "the session budget is the binding ceiling; raising --budget "
                "alone will not unlock this, raise the session budget in "
                ".dex/config.yml instead",
            ]
        return ConfirmationRequest(cost=exc.cost, data=data)
    return None


def cumulative_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    candidate_count: int,
    object_count: int,
) -> ConfirmationRequest | None:
    """The mid-command checkpoint for the cumulative-measure phase on billed
    connectors, ``--check-cumulative``'s analogue of :func:`verify_handshake`.

    The window-function probe can only be priced once profiling finds an
    entity/temporal pair to test, so this runs after inference on an
    already-confirmed command: the probes are dry-run priced (free), and only
    when that estimate does not fit what remains of the confirmed budget does
    it return the request, which the caller carries on its result as
    ``pending_confirmation``. Otherwise the confirmed budget already covers
    the check and the command proceeds in one pass.

    A request rather than a raise, unlike :func:`billed_handshake`: the
    profiles up to this point are already paid for, and raising would discard
    them and bill the user twice for the same scan.
    """

    gate = cost_gate(adapter)
    if gate is None or candidate_count == 0:
        return None
    try:
        gate.preflight_phase(estimate)
    except ConfirmationRequiredError as exc:
        per_table = {"(cumulative-measure probes)": estimate}
        describe = getattr(adapter, "describe_estimate", None)
        if describe is not None:
            data = {"command": command, **describe(estimate, per_table)}
        else:
            data = {
                "command": command,
                "estimated_bytes": estimate,
                "per_table_bytes": per_table,
            }
        data.update(
            {
                "phase": "check-cumulative",
                "candidate_count": candidate_count,
                "object_count": object_count,
                "hint": (
                    f"found {candidate_count} entity/temporal candidate(s) "
                    f"across {object_count} object(s); checking them all for "
                    f"cumulative measures is estimated at {estimate:.0f} "
                    f"{gate.paradigm.value} beyond what remains of the "
                    "confirmed budget. Profiles are already saved to the "
                    "exploration cache; re-run the same command with "
                    f"--confirm --budget {math.ceil(exc.cost.estimate)} to "
                    "profile and check in one pass (a re-run re-profiles "
                    "first)"
                ),
            }
        )
        if (
            gate.session_ceiling is not None
            and gate.session_spent is not None
            and gate.session_ceiling - gate.session_spent < exc.cost.estimate
        ):
            data.setdefault("notes", [])
            data["notes"] = [
                *data["notes"],
                "the session budget is the binding ceiling; raising --budget "
                "alone will not unlock this, raise the session budget in "
                ".dex/config.yml instead",
            ]
        return ConfirmationRequest(cost=exc.cost, data=data)
    return None


def sample_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    notes: list[str] | None = None,
) -> ConfirmationRequest | None:
    """The mid-command checkpoint for a scan whose price needed an earlier scan.

    Sibling of :func:`verify_handshake` over the same ``preflight_phase`` gate, for
    the other shape that cannot be priced up front: clustering picks its feature
    columns out of a profile, so its sample statement does not exist until that
    profile has been paid for. Returning rather than raising follows the same rule
    those two already state, that a command which has already spent must not
    discard the result and bill for it twice.

    None means the confirmed budget already covers the scan and the command
    proceeds in one pass, which is the ordinary outcome.
    """

    gate = cost_gate(adapter)
    if gate is None:
        return None
    try:
        gate.preflight_phase(estimate)
    except ConfirmationRequiredError as exc:
        describe = getattr(adapter, "describe_estimate", None)
        if describe is not None:
            data = {"command": command, **describe(estimate, None)}
        else:
            data = {"command": command, "estimated_bytes": estimate}
        data.update(
            {
                "phase": "sample",
                "hint": (
                    "the profile this command needed is done and saved; the "
                    f"sample scan is estimated at {estimate:.0f} "
                    f"{gate.paradigm.value} beyond what remains of the confirmed "
                    "budget. Re-run with --confirm --budget "
                    f"{math.ceil(exc.cost.estimate)}; the cached profile is "
                    "reused, so the re-run pays for the sample alone"
                ),
            }
        )
        if notes:
            data.setdefault("notes", [])
            data["notes"] = [*data["notes"], *notes]
        return ConfirmationRequest(cost=exc.cost, data=data)
    return None


def stamp_spend(result: Result, adapter: Adapter) -> Result:
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
        # has spent, and the caller is looking at the number.
        result.warnings = [*result.warnings, *gate.warnings()]
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
