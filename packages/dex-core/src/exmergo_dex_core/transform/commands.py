"""Transform command orchestrators.

Each ``cmd_*`` resolves the dbt project, drives the plan/apply/build engine, and
shapes the result into the sanitized envelope. The transform skill fronts the
authoring CLI groups (``transform``, ``semantic``); they share one plan store and
one write path, which is why they live in one package.

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
from ..dbt_project import ApplyResult, EditOp
from . import plans as plans_mod
from . import semantic as semantic_mod
from .plans import EditKind, PlanEdit, PlanStore

# What actually caps a dbt statement server-side, per compute-time connector:
# a per-connector fact, kept out of the shared build arm so the next connector
# adds an entry instead of nesting a conditional. dex cannot inject a per-build
# cap through any of these dbt adapters.
_COMPUTE_TIME_CAP_NOTES = {
    "redshift": (
        "a statement_timeout on the dbt dev user and a workgroup usage "
        "limit are the server-side caps (dex cannot inject one per build)"
    ),
}
_DEFAULT_COMPUTE_TIME_CAP_NOTE = (
    "the warehouse-level statement timeout and auto-suspend are the server-side caps"
)


def cmd_init(args: argparse.Namespace) -> env.Envelope:
    from ..config import DexConfig, load_config
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

    layered = bool(getattr(args, "layered_schemas", False))
    result = init_mod.init_project(
        getattr(args, "argument", None) or "",
        connector,
        path=getattr(args, "path", None),
        repo_root=repo_root,
        layered_schemas=layered,
    )

    # The renderers persisted the resolved dev namespaces into .dex/config.yml,
    # so a fresh load has exactly the names the profile was rendered from. The
    # check is advisory and files are already written: existing content in a
    # namespace the project will build into is a warning, never a refusal.
    from . import dev_target

    fresh = load_config(repo_root) or DexConfig()
    preflight = dev_target.content_check(fresh, repo_root, layered=layered)

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
        warnings=preflight,
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

    parse_notes: list[str] = []
    has_delete = any(e.op is EditOp.DELETE for e in edits)
    has_config = any(
        e.kind in (EditKind.PROJECT_YML, EditKind.PROFILES_YML) for e in edits
    )
    if has_delete or has_config:
        # A broken dbt_project.yml or profiles.yml breaks the whole project, and
        # a delete can orphan a downstream ref, so both are gated by dbt's own
        # parser at plan time (the same authoritative gate semantic and macro
        # plans use), not left to surface for the first time at build. The
        # secret-guard runs first, so an inlined credential is never handed to
        # the dbt subprocess.
        from ..config import DexConfig, load_config
        from ..dbt_project import load as load_project
        from .build import shadow_parse
        from .validate import assert_profiles_safe

        project = command_args.project_dir(args)
        view = load_project(project)
        assert_profiles_safe(view, edits)
        # Refuse an orphaning delete with a precise, dbt-independent message
        # before the subprocess runs (which would otherwise report the same
        # danglers as a lower-level parse error). This is the always-available
        # hard gate; the parse below is the authoritative backstop.
        if has_delete:
            plans_mod.validate_deletions(view, edits)
        config = load_config(command_args.repo_root(args)) or DexConfig()
        parse_result = shadow_parse(project, edits, target=config.dbt_target)
        if not parse_result["available"]:
            parse_notes.append(parse_result["reason"])
        elif not parse_result["success"]:
            return env.error(
                _failure_message("dbt parse failed", parse_result["messages"]),
                warnings=parse_result["messages"][1:],
            )
        elif parse_result["messages"]:
            parse_notes = [f"dbt: {m}" for m in parse_result["messages"]]

    envelope = _make_plan(args, intent, edits)
    if parse_notes and envelope.status is env.Status.OK:
        envelope.warnings.extend(parse_notes)
    return envelope


def cmd_macro(args: argparse.Namespace) -> env.Envelope:
    """List the shipped macros, or plan scaffolding one into the project."""

    from ..dbt_project import load as load_project
    from . import scaffold as scaffold_mod

    name = getattr(args, "argument", None)
    if not name:
        return env.ok(
            {
                "macros": [
                    {"name": macro_name, "description": description}
                    for macro_name, description in sorted(
                        scaffold_mod.MACRO_ASSETS.items()
                    )
                ],
                "next": "scaffold one with `transform macro <name>`",
            }
        )

    project = command_args.project_dir(args)
    view = load_project(project)
    edit = scaffold_mod.macro_edit(name, view.macro_paths[0])

    warnings: list[str] = []
    existing = view.files.get(edit.path)
    if existing is not None and existing.content == edit.new_content:
        return env.ok(
            {
                "macro": name,
                "path": edit.path,
                "up_to_date": True,
            },
            warnings=[f"{edit.path} already matches the shipped version"],
        )
    if existing is not None:
        warnings.append(
            f"{edit.path} differs from the shipped version (customized or "
            "stale); the diff below reconciles them, and applying it "
            "overwrites the project's copy"
        )

    # The authoritative gate, same layering as semantic plans: dbt's own
    # parser sees the macro in a shadow copy of the project, which also
    # catches a name collision with a macro defined elsewhere in the project.
    from ..config import DexConfig, load_config
    from .build import shadow_parse

    config = load_config(command_args.repo_root(args)) or DexConfig()
    parse_result = shadow_parse(project, [edit], target=config.dbt_target)
    if not parse_result["available"]:
        warnings.append(parse_result["reason"])
    elif not parse_result["success"]:
        return env.error(
            _failure_message("dbt parse failed", parse_result["messages"]),
            warnings=parse_result["messages"][1:],
        )

    envelope = _make_plan(args, f"scaffold macro {name}", [edit])
    envelope.warnings.extend(warnings)
    return envelope


def cmd_apply(args: argparse.Namespace) -> env.Envelope:
    plan_id = getattr(args, "argument", None)
    if not plan_id:
        # No id means the latest unapplied plan of any kind: apply does not
        # dispatch on kind, a plan is a plan (a semantic plan applies the same
        # way a model plan does).
        latest = PlanStore(command_args.repo_root(args)).latest(None)
        if latest is None:
            return env.error(
                "no unapplied plan found; run `transform plan` or `semantic "
                "define|update|plan` first, or pass a plan id"
            )
        plan_id = latest.plan_id
    return _apply_plan(args, plan_id)


def cmd_plans(args: argparse.Namespace) -> env.Envelope:
    """List stored plans (pending and applied), newest first."""

    plans = PlanStore(command_args.repo_root(args)).list_all()
    return env.ok(
        {
            "plans": [
                {
                    "plan_id": p.plan_id,
                    "intent": p.intent,
                    "kinds": sorted({e.kind.value for e in p.edits}),
                    "paths": [e.path for e in p.edits],
                    "created_at": p.created_at,
                    "applied_at": p.applied_at,
                    "pending": p.applied_at is None,
                }
                for p in plans
            ],
            "count": len(plans),
        }
    )


def cmd_build(args: argparse.Namespace) -> env.Envelope:
    from ..config import DexConfig, load_config
    from ..envelope import Paradigm
    from ..guards.cost_guard import ConfirmationRequiredError

    # `from .build import ...` rather than `from . import build`: the package
    # re-exports the build *function* under the same name as the module, and the
    # submodule-path form resolves the module unambiguously.
    from . import dev_target
    from .build import build as run_build

    repo_root = command_args.repo_root(args)
    config = load_config(repo_root) or DexConfig()
    target = getattr(args, "target", None) or config.dbt_target or "dev"
    budget = getattr(args, "budget", None)
    ceiling = budget if budget is not None else config.budget.ceiling
    connector = getattr(args, "connector", None) or config.connector
    paradigm = {
        "bigquery": Paradigm.BYTES_SCANNED,
        "snowflake": Paradigm.COMPUTE_TIME,
        "databricks": Paradigm.COMPUTE_TIME,
        "redshift": Paradigm.COMPUTE_TIME,
        "postgres": Paradigm.DB_LOAD,
    }.get(connector, Paradigm.FREE_LOCAL)

    project = command_args.project_dir(args)
    # A --connector flag governs this build, so the drift check must compare the
    # profile against that connector's config block, not the committed default.
    effective = config.model_copy(update={"connector": connector})

    try:
        summary, cost = run_build(
            project,
            target=target,
            configured_target=config.dbt_target,
            select=getattr(args, "select", None),
            ceiling=ceiling,
            confirmed=bool(getattr(args, "confirm", False)),
            paradigm=paradigm,
            dev_target_check=lambda: dev_target.check(
                project, target, effective, repo_root
            ),
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
    notes = summary.pop("notes", [])
    if paradigm is Paradigm.BYTES_SCANNED:
        notes = [
            "dbt has no dry-run, so this build's scan size could not be "
            "estimated upfront; each statement was capped server-side by the "
            "profile's maximum_bytes_billed (a per-statement cap, not per run)",
            *notes,
        ]
        billed = summary.get("bytes_billed")
        if billed:
            _record_build_spend(repo_root, connector, billed, "billed_bytes")
    elif paradigm is Paradigm.COMPUTE_TIME:
        cap_note = _COMPUTE_TIME_CAP_NOTES.get(
            connector, _DEFAULT_COMPUTE_TIME_CAP_NOTE
        )
        notes = [
            "dbt has no dry-run, so this build's warehouse time could not be "
            f"estimated upfront; {cap_note}",
            *notes,
        ]
        # dbt-snowflake, dbt-databricks, and dbt-redshift report no billing
        # figure; per-node execution time is the honest compute-seconds actual.
        seconds = sum(
            float(node.get("execution_time") or 0) for node in summary.get("nodes", [])
        )
        if seconds:
            summary["seconds_billed"] = seconds
            _record_build_spend(repo_root, connector, seconds, "billed_seconds")
    elif paradigm is Paradigm.DB_LOAD:
        notes = [
            "dbt has no dry-run, so this build's database time could not be "
            "estimated upfront; each statement was capped server-side by a "
            "statement_timeout set to the ceiling (injected via PGOPTIONS)",
            *notes,
        ]
        # dbt-postgres reports no billing figure; per-node execution time is
        # the honest database-seconds actual.
        seconds = sum(
            float(node.get("execution_time") or 0) for node in summary.get("nodes", [])
        )
        if seconds:
            summary["seconds_billed"] = seconds
            _record_build_spend(repo_root, connector, seconds, "billed_seconds")
    if summary["success"]:
        return env.ok(summary, cost=cost, warnings=[*notes, *messages])
    # Agents triage from `errors` first, so the first real dbt message rides
    # there; the rest stay in warnings.
    deps_info = summary.get("deps")
    prefix = (
        "dbt deps failed"
        if deps_info and not deps_info.get("success", True)
        else "dbt build failed"
    )
    return env.error(
        _failure_message(prefix, messages),
        data=summary,
        cost=cost,
        warnings=[*notes, *(messages[1:] if messages else [])],
    )


def cmd_deps(args: argparse.Namespace) -> env.Envelope:
    from .build import deps as run_deps
    from .build import has_package_spec

    project = command_args.project_dir(args)
    if not has_package_spec(project):
        return env.ok(
            {
                "ran": False,
                "reason": "no packages.yml (or dependencies.yml with packages) "
                "in the project",
            }
        )
    # An explicit invocation is a refresh: run even when dbt_packages/ exists.
    summary = run_deps(project)
    messages = summary.pop("messages", [])
    data = {"ran": True, **summary}
    if summary["success"]:
        return env.ok(data, warnings=messages)
    return env.error(
        _failure_message("dbt deps failed", messages),
        data=data,
        warnings=messages[1:] if messages else [],
    )


def cmd_semantic_define(args: argparse.Namespace) -> env.Envelope:
    return _semantic_plan(args, mode="define")


def cmd_semantic_update(args: argparse.Namespace) -> env.Envelope:
    return _semantic_plan(args, mode="update")


def cmd_semantic_plan(args: argparse.Namespace) -> env.Envelope:
    """Mixed-intent semantic authoring: one payload may evolve existing
    definitions and add the new ones they depend on; each name is classified
    as defined or updated instead of the whole payload being refused."""

    return _semantic_plan(args, mode="plan")


# --- helpers -----------------------------------------------------------------


def _record_build_spend(repo_root, connector: str, billed: float, field: str) -> None:
    """Account a billed dbt build in the `.dex/spend.jsonl` ledger, so builds
    draw against the same session budget as explore scans. ``field`` carries
    the connector's unit (bytes or seconds), matching what its cost gate
    records."""

    from datetime import UTC, datetime

    from ..cache import DexStore

    DexStore(repo_root).append_spend_log(
        {
            "at": datetime.now(UTC).isoformat(),
            "connector": connector,
            "command": "transform build",
            field: float(billed),
            "job_id": None,
            "statement_sha256": None,
        }
    )


def _failure_message(prefix: str, messages: list[str]) -> str:
    return f"{prefix}: {messages[0]}" if messages else prefix


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
    deletions = [e.path for e in edits if e.op is EditOp.DELETE]
    if deletions:
        return env.error(
            f"semantic {mode} authors content and does not delete; remove the "
            "semantic YAML with `transform plan` instead, for: " + ", ".join(deletions)
        )

    from ..dbt_project import load as load_project

    project = command_args.project_dir(args)
    view = load_project(project)
    parsed_edits = [yaml.safe_load(e.new_content) for e in edits]
    classification = semantic_mod.check_mode(mode, parsed_edits, view)
    semantic_mod.check_references(parsed_edits, view)
    spine_warning = semantic_mod.time_spine_warning(view, parsed_edits)

    # The authoritative gate: a plan that dbt cannot parse is never stored.
    # Skipped when the time-spine warning fires (dbt would refuse to parse for
    # that already-surfaced reason, and authoring the spine comes next), or
    # with --no-parse.
    parse_warning: str | None = None
    parse_deprecations: list[str] = []
    if getattr(args, "no_parse", False):
        pass
    elif spine_warning:
        parse_warning = "dbt parse skipped until the project has a time spine"
    else:
        from ..config import DexConfig, load_config
        from .build import shadow_parse

        config = load_config(command_args.repo_root(args)) or DexConfig()
        parse_result = shadow_parse(project, edits, target=config.dbt_target)
        if not parse_result["available"]:
            parse_warning = parse_result["reason"]
        elif not parse_result["success"]:
            return env.error(
                _failure_message("dbt parse failed", parse_result["messages"]),
                warnings=parse_result["messages"][1:],
            )
        elif parse_result["messages"]:
            # The parse passed, but dbt logged deprecation notices against this
            # exact YAML (#55): surface them now, at plan time, rather than
            # let the author discover them for the first time at `transform
            # build` (where they also poison the failure-error channel, #50).
            parse_deprecations = [f"dbt: {m}" for m in parse_result["messages"]]

    envelope = _make_plan(args, intent, edits)
    if envelope.status is env.Status.OK:
        plan_id = envelope.data["plan_id"]
        envelope.data["next"] = f"review the diffs, then `transform apply {plan_id}`"
        envelope.data["defined"] = classification["defined"]
        envelope.data["updated"] = classification["updated"]
        if spine_warning:
            envelope.warnings.append(spine_warning)
        if parse_warning:
            envelope.warnings.append(parse_warning)
        envelope.warnings.extend(parse_deprecations)
    return envelope


def _edits_from_payload(
    edits_file: str | None, default_kind: EditKind | None = None
) -> list[PlanEdit]:
    """Read the agent-authored edits payload (a file path, or ``-`` for stdin).

    Shape: ``{"edits": [{"path": ..., "kind": ..., "op": ..., "content": ...},
    ...]}``. ``op`` defaults to ``"upsert"`` (create or update): those carry
    ``content``. An ``op`` of ``"delete"`` removes the file and carries no
    ``content``. ``kind`` may be omitted when the command implies it (semantic
    define/update).
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
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(f"edits[{i}] needs at least a path")
        try:
            op = EditOp(entry.get("op") or EditOp.UPSERT.value)
        except ValueError as exc:
            raise ValueError(
                f"edits[{i}] has an unknown op '{entry.get('op')}': one of "
                + ", ".join(o.value for o in EditOp)
            ) from exc
        kind = entry.get("kind") or default_kind
        if kind is None:
            raise ValueError(
                f"edits[{i}] needs a kind: one of "
                + ", ".join(k.value for k in EditKind)
            )
        has_content = "content" in entry
        if op is EditOp.UPSERT and not has_content:
            raise ValueError(f"edits[{i}] is an upsert and needs content")
        if op is EditOp.DELETE and has_content:
            raise ValueError(f"edits[{i}] is a delete and must not carry content")
        edits.append(
            PlanEdit(
                path=entry["path"],
                kind=EditKind(kind),
                op=op,
                new_content=entry.get("content"),
            )
        )
    return edits


def _read_file(path: str) -> str:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise ValueError(f"edits file not found: {path}")
    return p.read_text(encoding="utf-8")
