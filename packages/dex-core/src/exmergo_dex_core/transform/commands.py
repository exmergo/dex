"""Transform command orchestrators.

Each ``cmd_*`` resolves the dbt project, drives the plan/apply/build engine, and
shapes the result into the sanitized envelope. The transform skill fronts the
authoring CLI groups (``transform``, ``semantic``, ``emit dbt``); they share one
plan store and one write path, which is why they live in one package.

The agent is the author: model SQL and semantic YAML arrive via ``--edits-file``
(a JSON payload; ``-`` reads stdin). The engine validates, diffs, and stores;
nothing touches the dbt project until an explicit apply.
"""

from __future__ import annotations

import argparse
import json
import sys

import yaml

from .. import command_args
from .. import envelope as env
from ..dbt_project import ApplyResult
from . import plans as plans_mod
from . import semantic as semantic_mod
from .plans import EditKind, PlanEdit, PlanStore


def cmd_init(args: argparse.Namespace) -> env.Envelope:
    from ..config import load_config
    from . import init as init_mod

    repo_root = command_args.repo_root(args)

    # Init bakes the connector into the generated profiles.yml, so the engine-wide
    # DuckDB fall-through is deliberately not used here: an explicit --connector
    # wins, a connector: committed in .dex/config.yml is accepted (and attributed),
    # and bare init is an error. Misconfiguration that works is the worst kind.
    connector = getattr(args, "connector", None)
    connector_source = "flag"
    if not connector:
        config = load_config(repo_root)
        if config is not None and "connector" in config.model_fields_set:
            connector, connector_source = config.connector, "config"
    if not connector:
        return env.error(
            "transform init needs an explicit connector and never defaults: pass "
            "--connector <" + "|".join(init_mod.VALID_CONNECTORS) + "> or declare "
            "connector: in .dex/config.yml"
        )

    result = init_mod.init_project(
        getattr(args, "argument", None) or "",
        connector,
        path=getattr(args, "path", None),
        repo_root=repo_root,
    )
    return env.ok(
        {
            "project_name": result.project_name,
            "project_dir": result.project_dir,
            "connector": result.connector,
            "connector_source": connector_source,
            "created": result.created,
            "next": "run `explore map` if you have not yet, then propose staging "
            "models with `transform plan --scaffold <table>`",
        },
        diffs=result.diffs,
    )


def cmd_plan(args: argparse.Namespace) -> env.Envelope:
    intent = getattr(args, "argument", None) or ""
    edits = _edits_from_payload(getattr(args, "edits_file", None))

    scaffold_tables = getattr(args, "scaffold", None) or []
    if scaffold_tables:
        from . import scaffold as scaffold_mod

        edits = (
            scaffold_mod.scaffold_edits(scaffold_tables, command_args.repo_root(args))
            + edits
        )

    if not edits:
        return env.error(
            "transform plan needs content: pass --edits-file <path|-> with the "
            "authored edits, or --scaffold <table> for a staging skeleton"
        )
    return _make_plan(args, intent, edits)


def cmd_apply(args: argparse.Namespace) -> env.Envelope:
    plan_id = getattr(args, "argument", None)
    if not plan_id:
        return env.error("transform apply needs a plan id (from `transform plan`)")
    return _apply_plan(args, plan_id)


def cmd_build(args: argparse.Namespace) -> env.Envelope:
    from ..config import DexConfig, load_config
    from ..guards.cost_guard import ConfirmationRequiredError

    # `from .build import ...` rather than `from . import build`: the package
    # re-exports the build *function* under the same name as the module, and the
    # submodule-path form resolves the module unambiguously.
    from .build import build as run_build

    repo_root = command_args.repo_root(args)
    config = load_config(repo_root) or DexConfig()
    target = getattr(args, "target", None) or config.dbt_target or "dev"
    budget = getattr(args, "budget", None)
    ceiling = budget if budget is not None else config.budget.ceiling

    try:
        summary, cost = run_build(
            command_args.project_dir(args),
            target=target,
            configured_target=config.dbt_target,
            select=getattr(args, "select", None),
            ceiling=ceiling,
            confirmed=bool(getattr(args, "confirm", False)),
        )
    except ConfirmationRequiredError as exc:
        return env.needs_confirmation(
            {
                "command": "transform build",
                "target": target,
                "hint": "review the cost, then re-run with --confirm (and --budget "
                "on billed connectors)",
            },
            cost=exc.cost,
        )

    messages = summary.pop("messages", [])
    if summary["success"]:
        return env.ok(summary, cost=cost, warnings=messages)
    return env.error("dbt build failed", data=summary, cost=cost, warnings=messages)


def cmd_semantic_define(args: argparse.Namespace) -> env.Envelope:
    return _semantic_plan(args, mode="define")


def cmd_semantic_update(args: argparse.Namespace) -> env.Envelope:
    return _semantic_plan(args, mode="update")


def cmd_emit_dbt(args: argparse.Namespace) -> env.Envelope:
    """Apply a semantic plan's YAML into the dbt project (the semantic `apply`)."""

    plan_id = getattr(args, "plan_id", None)
    if not plan_id:
        latest = PlanStore(command_args.repo_root(args)).latest(EditKind.SEMANTIC_YML)
        if latest is None:
            return env.error(
                "no unapplied semantic plan found; run `semantic define` or "
                "`semantic update` first, or pass a plan id"
            )
        plan_id = latest.plan_id
    return _apply_plan(args, plan_id)


# --- helpers -----------------------------------------------------------------


def _make_plan(
    args: argparse.Namespace, intent: str, edits: list[PlanEdit]
) -> env.Envelope:
    repo_root = command_args.repo_root(args)
    project = command_args.project_dir(args)
    plan, diffs, warnings = plans_mod.plan(intent, edits, project, repo_root)
    return env.ok(
        {
            "plan_id": plan.plan_id,
            "intent": plan.intent,
            "edit_count": len(plan.edits),
            "paths": [e.path for e in plan.edits],
            "plan_path": str(PlanStore(repo_root).path_for(plan.plan_id)),
            "next": f"review the diffs, then `transform apply {plan.plan_id}`",
        },
        diffs=diffs,
        warnings=warnings,
    )


def _apply_plan(args: argparse.Namespace, plan_id: str) -> env.Envelope:
    confirmed = bool(getattr(args, "confirm", False))
    result: ApplyResult = plans_mod.apply(
        plan_id, command_args.repo_root(args), confirmed=confirmed
    )
    if result.conflicts and not result.written:
        return env.needs_confirmation(
            {
                "plan_id": plan_id,
                "conflicts": [c.model_dump(mode="json") for c in result.conflicts],
                "hint": (
                    "these files changed after the plan was made (human edits are "
                    "authoritative); re-plan against current state, or re-run "
                    "with --confirm to overwrite deliberately"
                ),
            },
            diffs=result.diffs,
        )
    return env.ok(
        {
            "plan_id": plan_id,
            "written": result.written,
            "conflicts_overridden": [c.path for c in result.conflicts],
        },
        diffs=result.diffs,
    )


def _semantic_plan(args: argparse.Namespace, mode: str) -> env.Envelope:
    intent = getattr(args, "argument", None) or ""
    edits = _edits_from_payload(
        getattr(args, "edits_file", None), default_kind=EditKind.SEMANTIC_YML
    )
    if not edits:
        return env.error(
            f"semantic {mode} needs content: pass --edits-file <path|-> with the "
            "authored dbt semantic YAML"
        )
    non_semantic = [e.path for e in edits if e.kind is not EditKind.SEMANTIC_YML]
    if non_semantic:
        return env.error(
            f"semantic {mode} takes only semantic_yml edits; got other kinds for: "
            + ", ".join(non_semantic)
        )

    from ..dbt_project import load as load_project

    view = load_project(command_args.project_dir(args))
    parsed_edits = [yaml.safe_load(e.new_content) for e in edits]
    semantic_mod.check_mode(mode, parsed_edits, view)
    envelope = _make_plan(args, intent, edits)
    if envelope.status is env.Status.OK:
        plan_id = envelope.data["plan_id"]
        envelope.data["next"] = f"review the diffs, then `emit dbt {plan_id}`"
        spine_warning = semantic_mod.time_spine_warning(view, parsed_edits)
        if spine_warning:
            envelope.warnings.append(spine_warning)
    return envelope


def _edits_from_payload(
    edits_file: str | None, default_kind: EditKind | None = None
) -> list[PlanEdit]:
    """Read the agent-authored edits payload (a file path, or ``-`` for stdin).

    Shape: ``{"edits": [{"path": ..., "kind": ..., "content": ...}, ...]}``.
    ``kind`` may be omitted when the command implies it (semantic define/update).
    """

    if edits_file is None:
        return []
    raw = sys.stdin.read() if edits_file == "-" else _read_file(edits_file)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"edits payload is not valid JSON: {exc}") from exc
    entries = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError('edits payload must be {"edits": [...]}')

    edits: list[PlanEdit] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or "path" not in entry or "content" not in entry:
            raise ValueError(f"edits[{i}] needs at least path and content")
        kind = entry.get("kind") or default_kind
        if kind is None:
            raise ValueError(
                f"edits[{i}] needs a kind: one of "
                + ", ".join(k.value for k in EditKind)
            )
        edits.append(
            PlanEdit(
                path=entry["path"], kind=EditKind(kind), new_content=entry["content"]
            )
        )
    return edits


def _read_file(path: str) -> str:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise ValueError(f"edits file not found: {path}")
    return p.read_text(encoding="utf-8")
