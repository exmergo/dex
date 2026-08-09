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
from .guards.cost_guard import ConfirmationRequiredError, CostGate
from .results import ConfirmationRequest, Result
from .storage import DEX_DIR


def _resolve_dex_root(start: Path) -> Path | None:
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
    resolved = _resolve_dex_root(Path(raw))
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
    try:
        gate.preflight_command(estimate)
    except ConfirmationRequiredError as exc:
        # The payload speaks the connector's unit. An adapter that knows more
        # than the raw magnitude (Snowflake's credit translation, its
        # estimate-quality caveat) describes its own estimate; the bytes shape
        # is the default the bytes-scanned connectors settled on.
        describe = getattr(adapter, "describe_estimate", None)
        if describe is not None:
            data = {"command": command, **describe(estimate, per_table)}
        else:
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
        gate.settle()
        if result.cost.estimate is None:
            result.cost = gate.cost()
        spend = gate.spend_summary()
        display = getattr(adapter, "spend_display", None)
        if display is not None:
            spend.update(display())
        result.spend = spend
        # Settlement is the honest moment to say what did not bind: the command
        # has spent, and the caller is looking at the number.
        result.warnings = [*result.warnings, *gate.warnings()]
    return result
