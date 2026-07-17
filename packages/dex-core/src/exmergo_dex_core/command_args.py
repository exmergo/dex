"""Argument-to-engine bridges and plumbing shared by the command orchestrators.

These adapt an ``argparse.Namespace`` into the inputs the engine speaks (an open
adapter, a repo root, a project directory) and carry the cost-before-spend
handshake every billed command goes through. They live at the command layer,
deliberately not in the engine core, so the engines never depend on argparse,
and every ``cmd_*`` module (explore, transform, maintain) shares one handshake
instead of re-deriving it.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from . import envelope as env
from .adapters.base import Adapter
from .connect import open_adapter
from .guards.cost_guard import ConfirmationRequiredError, CostGate


def repo_root(args: argparse.Namespace) -> str:
    return getattr(args, "repo_root", ".")


def open_from_args(args: argparse.Namespace) -> Adapter:
    group = getattr(args, "group", None)
    subcommand = getattr(args, "subcommand", None)
    command = " ".join(part for part in (group, subcommand) if part) or None
    return open_adapter(
        connector=getattr(args, "connector", None),
        path=getattr(args, "path", None),
        project=getattr(args, "project", None),
        datasets=getattr(args, "dataset", None),
        scopes=getattr(args, "scope", None),
        repo_root=repo_root(args),
        budget=getattr(args, "budget", None),
        confirmed=getattr(args, "confirm", False),
        command=command,
    )


def cost_gate(adapter: Adapter) -> CostGate | None:
    """The adapter's cost gate when it is a billed connector; free adapters
    (DuckDB) have none, and their commands stay confirmation-free."""

    return getattr(adapter, "cost_gate", None)


def billed_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    per_table: dict[str, float] | None = None,
    notes: list[str] | None = None,
) -> env.Envelope | None:
    """The cost-before-spend handshake on billed connectors.

    The estimate comes from free dry-runs, so the unconfirmed pass spends
    nothing: it either passes the gate (confirmed, within budget) or returns
    the ``needs_confirmation`` envelope for the agent to surface and re-issue
    with ``--confirm --budget``. Over-ceiling and no-ceiling refusals propagate
    as errors (confirmation cannot override them).
    """

    gate = cost_gate(adapter)
    if gate is None:
        return None
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
        if notes:
            data.setdefault("notes", [])
            data["notes"] = [*data["notes"], *notes]
        return env.needs_confirmation(data, cost=exc.cost)
    return None


def verify_handshake(
    command: str,
    adapter: Adapter,
    estimate: float,
    *,
    candidate_count: int,
    object_count: int,
) -> env.Envelope | None:
    """The mid-command checkpoint for the verify phase on billed connectors.

    Verify probes can only be priced after profiling finds the candidate
    relationships, so this runs after inference on an already-confirmed
    command: the probes are dry-run priced (free), and only when that estimate
    does not fit what remains of the confirmed budget does it return the
    ``needs_confirmation`` envelope — otherwise the confirmed budget already
    covers verify and the command proceeds in one pass.
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
                    "relationships are saved to .dex/cache.json; re-run the "
                    "same command with --confirm --budget "
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
        return env.needs_confirmation(data, cost=exc.cost)
    return None


def stamp_spend(envelope: env.Envelope, adapter: Adapter) -> env.Envelope:
    """Stamp the preflight cost and the actual spend onto an OK envelope. The
    ``cost`` field stays a preflight estimate by contract; actual billed bytes
    live in ``data.spend``. An envelope already carrying a cost (a phase
    checkpoint) keeps it; only the spend summary is refreshed."""

    gate = cost_gate(adapter)
    if gate is not None:
        if envelope.cost.estimate is None:
            envelope.cost = gate.cost()
        spend = gate.spend_summary()
        display = getattr(adapter, "spend_display", None)
        if display is not None:
            spend.update(display())
        envelope.data["spend"] = spend
    return envelope


def project_dir(args: argparse.Namespace) -> Path:
    """The dbt project directory: the config pin wins, discovery is the default."""

    from .config import load_config
    from .dbt_project import find_project

    root = repo_root(args)
    config = load_config(root)
    # Absolute so downstream dbt subprocess calls (which pin cwd to this dir)
    # never re-resolve a relative --project-dir against it and double the path.
    if config and config.dbt_project_dir:
        return (Path(root) / config.dbt_project_dir).resolve()
    return find_project(root).resolve()
