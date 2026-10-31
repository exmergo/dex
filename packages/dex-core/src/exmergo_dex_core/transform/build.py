"""Dev-target dbt builds: prod-refusing, preflight-gated, subprocess-isolated.

The refusal and the cost gate live here in the engine, not in the command layer,
so every caller is gated: prod-target execution is never initiated by dex, and
nothing runs unconfirmed. The target rule is two-layered: an allowlist (``dev``
or the configured ``dbt_target``) and a denylist backstop that config cannot
whitelist, so a misconfigured ``dbt_target: prod`` still refuses.

dbt runs as a subprocess rather than in-process: dbt logs to stdout, and the
command contract is exactly one JSON envelope there, so the run is isolated and
only a sanitized summary crosses the boundary. Node results come from dbt's own
``run_results.json`` in the artifact directory for this build, not from
scraping log text. That directory is ``<project>/target`` unless the caller
named a runtime or an artifact directory.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from ..dbt_project import (
    PROFILES_FILE,
    DbtProjectError,
    Edit,
    EditOp,
    contained_path,
    manifest_node_label,
    profiles_dir,
    strip_relation_quoting,
)
from ..dbt_project import load as load_project
from ..envelope import Cost, EstimateQuality, Paradigm, redact
from ..errors import DexError, PrerequisiteError
from ..guards.cost_guard import UPTIME_LINE, preflight
from .artifact_runtime import (
    DEFAULT_ARTIFACT_POLICY,
    artifact_paths,
    clear_run_results,
    prepare_artifact_dir,
    readable_artifact_dir,
    resolve_build_artifact_dir,
    sanitize_artifact_directory,
    shared_target_dir,
)

# Names that mean production no matter what the config says. The build target
# must additionally be on the allowlist (dev or the configured dbt_target); this
# set is the backstop config cannot override.
_PROD_TARGET_NAMES = {"prod", "production", "prd", "live", "release", "main"}

_DBT_TIMEOUT_SECONDS = 600.0
_DEPS_TIMEOUT_SECONDS = 300.0
_PARSE_TIMEOUT_SECONDS = 120.0

# Envelope hygiene for dbt output: enough of each message to act on, never a
# multi-KB traceback, never the same message twice.
_MESSAGE_MAX_CHARS = 400
_MESSAGE_CAP = 20

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class DependencyPolicy(str, Enum):
    """What a build does when a declared package is not installed.

    ``INSTALL`` is what an interactive user wants and what every build did before
    this existed: run ``dbt deps`` post-gate so a first build never fails on a
    step the agent has no verb for. ``REFUSE`` is what a sandbox wants: no
    network, a pinned dependency set, and a build that must not quietly reach out
    and resolve a package. The refusal names the packages, which a dbt failure
    would not.
    """

    INSTALL = "install"
    REFUSE = "refuse"


class ProdTargetRefusedError(DexError):
    pass


class DbtRunError(DexError):
    pass


def assert_dev_target(target: str, configured: str | None = None) -> str:
    """Refuse anything that is not an explicit dev target.

    The denylist is checked first and wins even over the configured target, so
    configuration alone can never route a build at production.
    """

    if target.lower() in _PROD_TARGET_NAMES:
        raise ProdTargetRefusedError(
            f"target '{target}' looks like production; dex never initiates "
            "prod-target execution (builds are dev-target only)"
        )
    allowed = {"dev"} | ({configured} if configured else set())
    if target not in allowed:
        raise ProdTargetRefusedError(
            f"target '{target}' is not the dev target; allowed here: "
            f"{', '.join(sorted(allowed))} (set dbt_target in .dex/config.yml to "
            "name your dev target)"
        )
    return target


def build(
    project_dir: Path | str | None = None,
    *,
    target: str,
    configured_target: str | None = None,
    select: str | None = None,
    ceiling: float | None = None,
    confirmed: bool = False,
    paradigm: Paradigm = Paradigm.FREE_LOCAL,
    connector: str | None = None,
    estimate: float | None = None,
    estimate_quality: EstimateQuality | None = None,
    dev_target_check: Callable[[], list[str]] | None = None,
    runner: Runner | None = None,
    timeout: float = _DBT_TIMEOUT_SECONDS,
    dependencies: DependencyPolicy = DependencyPolicy.INSTALL,
    approved_functions: frozenset[str] | None = None,
    runtime_key: str | None = None,
    artifact_directory: Path | str | None = None,
    artifact_policy: str = DEFAULT_ARTIFACT_POLICY,
    relation_columns: Callable[[list[str]], dict[str, list[str]]] | None = None,
    full_refresh: bool = False,
) -> tuple[dict[str, Any], Cost]:
    """Run ``dbt build`` against a dev target, gated. Returns (summary, cost).

    Refusal order is deliberate. The target check runs first, so a prod target is
    refused outright rather than merely unconfirmed. ``dev_target_check`` runs
    next, before the cost gate: it is free, and a dev target that has drifted or
    does not exist makes the build impossible, so the user should learn that
    instead of being asked to weigh a budget for a run that cannot succeed. It
    returns warnings and raises to refuse; the callable is injected so the engine
    stays independent of connection handling and testable without a warehouse.

    On a billed paradigm ``estimate`` is the whole-build cost the command layer
    priced from a free ``dbt compile`` dry-run of each node (see
    :func:`compile_estimate`); it is ``None`` only when that pricing could not be
    produced (no reachable connection, a compile failure), in which case the
    ceiling and confirmation gates still bind and the profile's per-statement
    server-side cap (``maximum_bytes_billed`` on BigQuery, a statement timeout on
    the others) is the backstop. The gate here is defense in depth: the command
    layer has already run the confirm handshake from the same estimate, and this
    re-check binds every caller of the engine regardless.

    Where dbt writes is decided once, before anything runs, and every dbt
    invocation below gets it as ``--target-path`` (issue #1014).
    ``artifact_directory`` names it outright. ``runtime_key`` is the older
    spelling from #883: reuse a clean shared target path, isolate on a
    collision. Otherwise ``artifact_policy`` decides, through
    :func:`~.artifact_runtime.resolve_build_artifact_dir`: ``isolated`` (the
    default) is Dex's own ``<target-path>/dex/<runtime>``, and ``shared`` is the
    project's target path, refused when another runtime left it incompatible.
    The summary names the directory under ``artifact_directory``.

    ``full_refresh`` passes ``--full-refresh`` to ``dbt build``, which rebuilds
    each selected incremental model and seed from scratch (issue #1015). The
    summary's ``full_refresh`` then names them, with any whose own
    ``full_refresh: false`` config makes dbt ignore the flag. The stale-seed
    pre-step is skipped, since the build itself now refreshes every seed it
    selects.

    ``relation_columns`` reads the dev target: it takes seed relation names and
    returns the columns of each one that exists. With it, a seed whose CSV header
    no longer matches its relation (see :func:`stale_seeds`) is rebuilt with
    ``dbt seed --full-refresh`` right before the build, and the summary says so
    under ``full_refresh``. Injected like ``dev_target_check``, and for the same
    reason; without it the build runs as it always has.
    """

    assert_dev_target(target, configured_target)

    if project_dir is None:
        raise DbtRunError("no dbt project directory resolved for the build")
    # Absolute so --project-dir/--profiles-dir cannot resolve a second time
    # against the cwd we pin below: a relative project dir would otherwise
    # double (dbt would look for project/project and fail).
    project = Path(project_dir).resolve()

    # Resolved ahead of every gate: it is free, and a shared directory another
    # runtime left incompatible refuses here, before anyone weighs a budget.
    if artifact_directory is not None:
        artifacts = Path(artifact_directory)
        if not artifacts.is_absolute():
            artifacts = project / artifacts
        artifacts.mkdir(parents=True, exist_ok=True)
    elif runtime_key is not None:
        artifacts = prepare_artifact_dir(
            project,
            runtime_key,
            shared_relative=str(shared_target_dir(project)),
        )
    else:
        artifacts = resolve_build_artifact_dir(project, artifact_policy)

    target_warnings = dev_target_check() if dev_target_check is not None else []

    if dependencies is DependencyPolicy.REFUSE:
        assert_packages_installed(project)

    gate_estimate = 0.0 if paradigm is Paradigm.FREE_LOCAL else estimate
    cost = preflight(
        gate_estimate,
        ceiling,
        paradigm=paradigm,
        confirmed=confirmed,
        # Carried so a finished build reports its estimate's worth exactly as the
        # confirmation ask did. Without it the handshake said `exact` and the run
        # it authorized came back claiming nothing had been priced.
        estimate_quality=estimate_quality,
    )

    # Most real projects carry a packages.yml, and dbt refuses to compile until
    # its packages are installed; running deps here (post-gate) means the first
    # build never fails on a missing `dbt deps` step the agent has no verb for.
    # A sandbox with no network asks for the refusal before the subprocess, so
    # the policy is read ahead of the presence check rather than inside it: the
    # useful message names the packages, and `dbt deps` failing to reach a
    # registry does not.
    deps_ran = False
    if needs_deps(project):
        deps_summary = deps(project, runner=runner)
        deps_ran = True
        if not deps_summary["success"]:
            return {
                "target": target,
                "success": False,
                "returncode": deps_summary["returncode"],
                "nodes": [],
                "counts": {},
                "messages": deps_summary["messages"],
                "deps": {"ran": True, "success": False},
            }, cost

    flags = [
        "--target",
        target,
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(profiles_dir(project).resolve()),
        "--log-format",
        "json",
        "--target-path",
        str(artifacts),
    ]
    argv = [_dbt_executable(), "build", *flags]
    if select:
        argv += ["--select", select]
    if full_refresh:
        argv.append("--full-refresh")

    # A hard compile-time/parse-time failure (a Jinja context error, for
    # instance) dies before dbt ever reaches node execution, so it never
    # (re)writes run_results.json -- a stale one left over from a prior
    # successful build would otherwise still be sitting there, and _summarize
    # would report its node results as if they belonged to this invocation
    # (issue #76). Clearing the resolved directory first means "file absent"
    # always means "this invocation wrote nothing", never "an old invocation's
    # results carried over". The shared directory is left untouched when this
    # runtime was isolated away from it.
    clear_run_results(artifacts)

    # cwd is pinned to the project dir so relative paths in profiles.yml (for
    # example a DuckDB `path: ./dev.duckdb`) resolve against the project, never
    # against whatever directory the caller happened to launch from.
    run = runner or _default_runner(
        timeout, project, env=_build_env(connector, paradigm, ceiling)
    )
    stale: list[dict[str, Any]] = []
    # A requested full refresh rebuilds every seed the build selects, stale or
    # not, so the pre-step would only drop the same tables twice.
    if relation_columns is not None and not full_refresh:
        try:
            stale = _seeds_to_refresh(
                project, artifacts, flags, select, run, relation_columns
            )
        except Exception as exc:
            # Reported, never raised: a check that cannot read the dev target
            # must not break a build dbt could run, nor go quiet about it.
            target_warnings = [
                *target_warnings,
                "could not compare seed headers with the dev target "
                f"({type(exc).__name__}: {exc}); a seed whose columns changed "
                "fails as dbt reports it",
            ]
    if approved_functions:
        from ..adapters import get_dialect

        _compile_project(
            project,
            target=target,
            select=select,
            runner=run,
            artifacts=artifacts,
            full_refresh=full_refresh,
        )
        _assert_compiled_functions(
            artifacts,
            dialect=get_dialect(connector or "duckdb"),
            approved_functions=approved_functions,
        )
        # Compilation also writes run_results; it is not build evidence.
        clear_run_results(artifacts)
    # After every gate above, so a refused build has dropped nothing.
    refreshed: list[str] = []
    if stale:
        seed_argv = [_dbt_executable(), "seed", "--full-refresh", *flags]
        seed_argv += ["--select", " ".join(_exact_seed(node) for node in stale)]
        clear_run_results(artifacts)
        seeded = _summarize(
            project, target, run(seed_argv), artifact_directory=artifacts
        )
        refreshed = [node["name"] for node in seeded["nodes"]]
        if not seeded["success"]:
            seeded["full_refresh"] = {
                "seeds": refreshed or [str(node["name"]) for node in stale],
                "success": False,
            }
            return seeded, cost
        # The build that follows reloads these seeds and tests them, so its own
        # results are the evidence; the refresh's are not.
        clear_run_results(artifacts)
    completed = run(argv)

    summary = _summarize(project, target, completed, artifact_directory=artifacts)
    if deps_ran:
        summary["deps"] = {"ran": True, "success": True}
    if full_refresh:
        rebuilt = full_refresh_nodes(artifacts)
        summary["full_refresh"] = {
            "requested": True,
            **rebuilt,
            "success": bool(summary["success"]),
        }
        if not select:
            target_warnings = [
                *target_warnings,
                full_refresh_unselected_warning(rebuilt),
            ]
    elif refreshed:
        summary["full_refresh"] = {"seeds": refreshed, "success": True}
        target_warnings = [
            *target_warnings,
            f"rebuilt {', '.join(refreshed)} with dbt seed --full-refresh before "
            "the build: its CSV header no longer matched the columns of its "
            "relation in the dev target, and a changed header either fails to "
            "load into the table dbt already built or loads values into the "
            "wrong columns",
        ]
    if target_warnings:
        # Notes, not failure causes: kept out of `messages`, whose first entry
        # is promoted to the envelope's error line on failure.
        summary["notes"] = list(target_warnings)
    return summary, cost


def stale_seeds(
    manifest_path: Path,
    project: Path,
    relation_columns: Callable[[list[str]], dict[str, list[str]]],
) -> dict[str, dict[str, Any]]:
    """The seeds whose CSV header no longer matches the relation they load into,
    as ``unique_id`` to manifest node.

    dbt reloads a seed into the table it already built unless told to rebuild
    it, so a header that gained or lost a column fails to load (issue #943), and
    on dbt-duckdb, which loads by position, one that reordered its columns loads
    each value under the wrong name. Only ``--full-refresh`` recreates the
    table. Names compare case-insensitively, since a warehouse may fold them,
    and in order. A seed whose relation does not exist yet is not stale: dbt
    creates it.
    """

    nodes: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError, AttributeError):
        nodes = json.loads(manifest_path.read_text(encoding="utf-8")).get("nodes") or {}
    seeds = {
        strip_relation_quoting(node["relation_name"]): (unique_id, node)
        for unique_id, node in nodes.items()
        if isinstance(node, dict)
        and node.get("resource_type") == "seed"
        and node.get("fqn")
        and node.get("relation_name")
        and node.get("original_file_path")
    }
    if not seeds:
        return {}
    live = relation_columns(list(seeds))
    stale: dict[str, dict[str, Any]] = {}
    for relation, (unique_id, node) in seeds.items():
        columns = live.get(relation)
        if not columns:
            continue
        delimiter = (node.get("config") or {}).get("delimiter") or ","
        csv_file = Path(node.get("root_path") or project) / node["original_file_path"]
        try:
            with csv_file.open(encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle, delimiter=delimiter), [])
        except OSError:
            continue
        if [c.lower() for c in columns] != [h.lower() for h in header]:
            stale[unique_id] = node
    return stale


def _seeds_to_refresh(
    project: Path,
    artifacts: Path,
    flags: list[str],
    select: str | None,
    run: Runner,
    relation_columns: Callable[[list[str]], dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """The manifest node of each stale seed this build selects.

    A full refresh drops the table, so a seed the build would not select is left
    alone. dex does not evaluate dbt's selector language, which includes a
    project's default selector, so ``dbt ls`` answers which seeds the build
    selects. It parses afresh for this target, so neither a cleaned target
    directory nor a manifest another target left behind decides which table a
    seed loads into.
    """

    argv = [_dbt_executable(), "ls", "--quiet", "--resource-type", "seed"]
    argv += ["--output", "json", "--output-keys", "unique_id", *flags]
    if select:
        argv += ["--select", select]
    listed: set[str] = set()
    for line in (run(argv).stdout or "").splitlines():
        # Under --log-format json each printed node is a PrintEvent whose
        # message is that node's own JSON.
        with contextlib.suppress(ValueError, TypeError, KeyError, AttributeError):
            event = json.loads(line)
            if event["info"]["name"] == "PrintEvent":
                listed.add(json.loads(event["data"]["msg"])["unique_id"])
    if not listed:
        return []
    stale = stale_seeds(artifact_paths(artifacts).manifest, project, relation_columns)
    return [stale[unique_id] for unique_id in sorted(stale) if unique_id in listed]


def _exact_seed(node: dict[str, Any]) -> str:
    """A dbt selection matching this one seed and nothing else.

    dbt's fqn method also matches an fqn without its package and as a prefix,
    which reaches a same-named seed in another package or the seeds in a
    directory of the same name, so the package and the file name pin it.
    """

    fqn = [str(part) for part in node["fqn"]]
    name = Path(node["original_file_path"]).name
    return f"package:{fqn[0]},fqn:{'.'.join(fqn)},file:{name}"


@contextlib.contextmanager
def shadow_project(
    project_dir: Path | str, edits: Sequence[Edit] = ()
) -> Iterator[Path]:
    """A throwaway copy of the project with ``edits`` overlaid, yielded by path.

    The copy is what lets dex run dbt against a hypothetical version of a project
    without touching the real one: everything dbt writes into it (``target/``,
    ``logs/``, any stray database a relative profile path would create) lives and
    dies with the copy.

    ``dbt_packages/`` is deliberately copied, because parsing needs the installed
    macros. Warehouse files deliberately are not: they can be huge, and a copy of
    a database is not the database, so a caller that needs the real warehouse must
    reach it through the profile rather than through the tree. ``.dex`` matters
    when the project is the repo root.
    """

    project = Path(project_dir).resolve()
    view = load_project(project)
    by_name = shutil.ignore_patterns(
        "target", "logs", ".git", ".venv", ".dex", "*.duckdb", "*.db"
    )
    # A configured target-path holds artifacts as surely as `target/` does,
    # Dex's own isolated ones included (issue #1014). It is skipped by its full
    # path: `target-path: models/target` must not skip `models/`.
    artifacts: Path | None = None
    with contextlib.suppress(ValueError, OSError):
        configured = shared_target_dir(project).resolve()
        if configured != project and configured.is_relative_to(project):
            artifacts = configured

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = set(by_name(directory, names))
        if artifacts is not None and Path(directory).resolve() == artifacts.parent:
            skipped.update(name for name in names if name == artifacts.name)
        return skipped

    with tempfile.TemporaryDirectory(prefix="dex-shadow-") as tmp:
        shadow = Path(tmp) / (project.name or "project")
        shutil.copytree(project, shadow, ignore=ignore)
        for edit in edits:
            edit_path = contained_path(shadow, edit.path, view)
            if edit.op is EditOp.DELETE:
                # Remove it from the copy so the parse runs against the true
                # post-deletion tree: a surviving ref() to it fails dbt's parse.
                edit_path.unlink(missing_ok=True)
            else:
                edit_path.parent.mkdir(parents=True, exist_ok=True)
                edit_path.write_text(edit.new_content, encoding="utf-8")
        yield shadow


def shadow_parse(
    project_dir: Path | str,
    edits: list[Edit],
    *,
    target: str | None = None,
    runner: Runner | None = None,
    timeout: float = _PARSE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Validate proposed edits with dbt's own parser, without touching the project.

    The project is copied to a throwaway directory (warehouse files and build
    artifacts excluded), the edits are overlaid there, and ``dbt parse`` runs
    against the copy with cwd pinned to it, so everything dbt writes (target/,
    logs/, any stray database a relative profile path would create) lives and
    dies with the copy.

    Returns ``{"available", "reason", "success", "messages"}``: unavailable
    means the caller degrades to a warning (dbt or profiles missing, mirroring
    the schema-validation fallback). When available, ``success`` says whether
    the parse itself passed; ``messages`` is populated either way, since dbt
    logs deprecation notices on a project that parses cleanly, and a plan that
    validates today should not go on to warn about them for the first time at
    `transform build` (#55). On failure, ``messages`` are the parse errors
    (dbt's own summary event leads if present; see :func:`_collect_messages`).
    """

    # Absolute so --profiles-dir (pointing at the real project) does not
    # resolve against the shadow tempdir we pin as cwd below.
    project = Path(project_dir).resolve()
    try:
        executable = _dbt_executable()
    except DbtRunError:
        return {
            "available": False,
            "reason": "dbt is not installed; plan validated by schema checks only",
            "success": None,
            "messages": [],
        }
    try:
        profiles = profiles_dir(project)
    except DbtProjectError:
        return {
            "available": False,
            "reason": "no profiles.yml found; dbt parse skipped",
            "success": None,
            "messages": [],
        }

    with shadow_project(project, edits) as shadow:
        # A profiles.yml edit only takes effect if dbt reads the shadowed copy;
        # pointing --profiles-dir at the real project would parse the edit
        # against the unedited profile. Redirect to the shadow when the
        # project-local profiles is the one being edited (the copytree already
        # placed it there and the overlay above wrote the new content).
        profiles_arg = profiles.resolve()
        if profiles_arg == project and any(
            Path(edit.path).name == PROFILES_FILE and Path(edit.path).parent == Path()
            for edit in edits
        ):
            profiles_arg = shadow.resolve()
        argv = [
            executable,
            "parse",
            "--project-dir",
            str(shadow),
            "--profiles-dir",
            str(profiles_arg),
            "--log-format",
            "json",
        ]
        if target:
            argv += ["--target", target]
        run = runner or _default_runner(timeout, shadow)
        completed = run(argv)
        success = completed.returncode == 0
        # Collected unconditionally: a passing parse can still have logged
        # deprecation notices, which the caller surfaces as plan-time warnings
        # rather than letting the author discover them for the first time at
        # `transform build`.
        messages = _collect_messages(completed)
        if not success and not messages:
            messages = ["dbt parse failed"]
    return {"available": True, "reason": None, "success": success, "messages": messages}


# dbt reads these from the environment, and each one can silently change which
# tests run, what a status means, or where dbt writes. A mutation run has to mean
# the same thing on every machine, and the one that matters most is
# `DBT_INDIRECT_SELECTION`: set to `cautious` it drops the tests from the
# selection, every mutant then survives, and the report says the suite is weak
# when in fact it was never asked.
_ISOLATED_ENV_SCRUBBED = (
    "DBT_TARGET_PATH",
    "DBT_LOG_PATH",
    "DBT_STATE",
    "DBT_DEFER_STATE",
    "DBT_STORE_FAILURES",
    "DBT_WARN_ERROR",
    "DBT_WARN_ERROR_OPTIONS",
    "DBT_RESOURCE_TYPES",
    "DBT_EXCLUDE_RESOURCE_TYPES",
    "DBT_SELECTOR",
    "DBT_EMPTY",
    "DBT_FULL_REFRESH",
    "DBT_INDIRECT_SELECTION",
    "DBT_DEFER",
    "DBT_FAVOR_STATE",
    "DBT_FAIL_FAST",
)

# Credentials in the agent's environment that dbt never reads, withheld from
# every dbt child. dbt runs project and package code, and Jinja's `env_var()`
# reads any variable the child holds, so whatever is left here is readable by
# every macro in every installed package. A deny list rather than an allowlist,
# because dex-rendered profiles read warehouse credentials (`PGPASSWORD`,
# `SNOWFLAKE_PASSWORD`, `DATABRICKS_TOKEN`, ...) from this same environment, and
# a hand-written profile may read any name it likes.
_CHILD_ENV_WITHHELD = (
    # ClickHouse Cloud control-plane keys: dex's usage preflight reads them, dbt
    # connects with the SQL user's password.
    "CLICKHOUSE_CLOUD_API_KEY",
    "CLICKHOUSE_CLOUD_API_SECRET",
    "DEX_TEST_CH_CLOUD_API_KEY",
    "DEX_TEST_CH_CLOUD_API_SECRET",
    # The hosted Semantic Layer token belongs to dex's own GraphQL client.
    "DBT_SL_TOKEN",
    # Source-forge and agent-host tokens.
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GHRS_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _child_environ(*, isolated: bool = False) -> dict[str, str]:
    """The environment every dbt child starts from.

    The parent's environment minus the credentials dbt never reads, with usage
    tracking off. ``isolated`` also drops the ``DBT_*`` settings that change
    what a run means, for the shadow runs whose verdict must not depend on the
    caller's machine; an ordinary build keeps them, since a user's own
    ``DBT_TARGET_PATH`` is theirs to set.
    """

    withheld = set(_CHILD_ENV_WITHHELD)
    if isolated:
        withheld.update(_ISOLATED_ENV_SCRUBBED)
        # dbt 1.12 reads each of these first as `DBT_ENGINE_<NAME>`, so the old
        # name alone would leave the setting in place.
        withheld.update(
            name.replace("DBT_", "DBT_ENGINE_", 1) for name in _ISOLATED_ENV_SCRUBBED
        )
    env = {key: value for key, value in os.environ.items() if key not in withheld}
    # dbt writes a `.user.yml` into the profiles directory when usage tracking
    # is on, and the profiles directory here is often the real project.
    env["DO_NOT_TRACK"] = "1"
    return env


class ShadowRun:
    """A copied project that dbt can be run against many times, never the real one.

    :func:`shadow_parse` copies a project to parse it once. This is the same
    isolation held open across a sequence of invocations, which is what mutation
    coverage needs: one copy, then a compile and N test runs against it, each
    with a different version of one model's file, and an aggregate ``show`` for
    each survivor. The verbs are ``parse``, ``compile``, ``test`` and ``show``,
    never ``run`` or ``build``: none of them can materialize a relation.

    Stateful by nature, hence a class rather than a function taking the same six
    arguments each call: it owns the copy's lifetime, and dbt's partial-parse
    cache inside the copy is what keeps run N+1 from re-parsing the whole project.

    **cwd stays at the real project, and every artifact path is passed as a flag.**
    That split is deliberate and it is the only arrangement that is safe on every
    connector. dbt resolves ``target/`` and ``logs/`` against ``--project-dir``,
    so the flags keep its writes inside the copy; dbt-duckdb resolves a relative
    ``path:`` in the profile against the *process* cwd, so leaving cwd at the real
    project is what lets a copied project still reach the real dev database.
    Copying or linking the database instead would split it from its
    write-ahead log, which risks the user's data rather than merely confusing dbt.
    """

    def __init__(
        self,
        project_dir: Path | str,
        *,
        target: str,
        connector: str | None = None,
        paradigm: Paradigm = Paradigm.FREE_LOCAL,
        ceiling: float | None = None,
        runner: Runner | None = None,
        timeout: float = _DBT_TIMEOUT_SECONDS,
    ):
        self.project = Path(project_dir).resolve()
        self.target = target
        self._connector = connector
        self._paradigm = paradigm
        self._ceiling = ceiling
        self._runner = runner
        self._timeout = timeout
        self._stack = contextlib.ExitStack()
        self.shadow: Path | None = None
        self.view = None

    def __enter__(self) -> ShadowRun:
        self.view = load_project(self.project)
        self.shadow = self._stack.enter_context(shadow_project(self.project))
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()
        self.shadow = None

    def write(self, rel_path: str, text: str) -> None:
        """Put a file into the copy, confined to the project's editing surface."""

        path = contained_path(self._require_shadow(), rel_path, self.view)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def remove(self, rel_path: str) -> None:
        """Take a file back out of the copy, confined the same way as ``write``."""

        contained_path(self._require_shadow(), rel_path, self.view).unlink(
            missing_ok=True
        )

    def strip_run_hooks(self) -> bool:
        """Drop ``on-run-start`` and ``on-run-end`` from the copy. True if any went.

        A hook fires once per invocation, and this runs dbt once per mutant, so a
        project whose hooks grant permissions or write an audit row would do that
        N+1 times for a command the user thinks of as read-only.
        """

        manifest = self._require_shadow() / "dbt_project.yml"
        if not manifest.is_file():
            return False
        parsed = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        present = [key for key in ("on-run-start", "on-run-end") if key in parsed]
        if not present:
            return False
        for key in present:
            parsed.pop(key)
        manifest.write_text(yaml.safe_dump(parsed, sort_keys=False), encoding="utf-8")
        return True

    def strip_semantic_models(self, model: str) -> bool:
        """Drop semantic models referencing ``model`` from the copy.

        An ephemeral mutant has no relation name, so dbt's semantic validator
        (PydanticSemanticModel) fails project parsing when any semantic model
        references it. Mutation testing only runs data and unit tests, which
        never evaluate the semantic layer, so dropping referencing semantic
        models (and dependent metrics/saved queries) from the shadow copy allows
        dbt to compile and test the ephemeral mutant safely.

        Returns True if any semantic declarations were removed.
        """

        from ..dbt_project import REF_PATTERN

        shadow = self._require_shadow()
        yaml_files = [
            p
            for p in shadow.rglob("*")
            if p.is_file()
            and p.suffix in (".yml", ".yaml")
            and not any(part in {"target", "logs", ".git", ".dex"} for part in p.parts)
        ]

        matching_files: list[tuple[Path, dict[str, Any]]] = []
        model_referenced = False

        for path in yaml_files:
            try:
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                continue
            if not isinstance(parsed, dict):
                continue
            matching_files.append((path, parsed))
            for entry in parsed.get("semantic_models") or []:
                if not isinstance(entry, dict):
                    continue
                target = entry.get("model")
                if not isinstance(target, str):
                    continue
                m = REF_PATTERN.search(target)
                ref_name = m.group(1) if m else target.strip()
                if ref_name == model:
                    model_referenced = True

        if not model_referenced:
            return False

        stripped = False
        semantic_keys = ("semantic_models", "metrics", "saved_queries")
        for path, parsed in matching_files:
            present = [k for k in semantic_keys if k in parsed]
            if not present:
                continue
            for k in present:
                parsed.pop(k)
            stripped = True
            remaining = [k for k in parsed if k != "version"]
            if not remaining:
                path.unlink(missing_ok=True)
            else:
                path.write_text(
                    yaml.safe_dump(parsed, sort_keys=False), encoding="utf-8"
                )
        return stripped

    def manifest(self) -> dict[str, Any]:
        path = self._require_shadow() / "target" / "manifest.json"
        if not path.is_file():
            raise DbtRunError("dbt wrote no manifest for the copied project")
        return json.loads(path.read_text(encoding="utf-8"))

    def compile(self, select: str) -> dict[str, Any]:
        """Compile one selection in the copy and return the manifest dbt wrote."""

        completed = self._invoke("compile", "--select", select)
        if completed.returncode != 0:
            messages = _collect_messages(completed)
            raise DbtRunError(messages[0] if messages else "dbt compile failed")
        return self.manifest()

    def test(
        self, select: str, *, exclude: Sequence[str] = ()
    ) -> dict[str, Any] | None:
        """Run one selection's tests. ``None`` when dbt wrote no results at all.

        ``dbt test``, never ``dbt build``. Two reasons, and both are load-bearing.
        A build runs a model's unit tests before the model, so one failing unit
        test marks the model skipped and the skip cascades onto every data test
        attached to it: the run would then report four tests as skipped and the
        caller could not tell which of them would have caught the defect. And
        ``dbt test`` executes no model materialization at all, so even a config
        override that failed to apply could not write a relation.
        """

        args = ["--select", select]
        for name in exclude:
            args += ["--exclude", name]
        completed = self._invoke("test", *args)
        results = self._require_shadow() / "target" / "run_results.json"
        if not results.is_file():
            return None
        return _summarize(
            self._require_shadow(),
            self.target,
            completed,
            read_from=self._require_shadow() / "target",
        )

    def show(
        self, select: str
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None]:
        """Run one node's SELECT and hand back its first row, for an aggregate.

        Only for a node whose every projection is an aggregate dex wrote itself,
        because what comes back is the row. ``dbt show`` executes the compiled
        SELECT and nothing else: no materialization, no DDL, no hook.

        The summary settles spend exactly as a test run's does, and it carries
        no dbt messages: a warehouse error can quote a value from the data it
        failed on, and nothing here is worth that risk. The rows are ``None``
        when dbt failed or printed no preview.
        """

        completed = self._invoke(
            "show", "--select", select, "--limit", "1", "--output", "json"
        )
        results = self._require_shadow() / "target" / "run_results.json"
        summary = None
        if results.is_file():
            summary = {
                **_summarize(
                    self._require_shadow(),
                    self.target,
                    completed,
                    read_from=self._require_shadow() / "target",
                ),
                "messages": [],
            }
        if completed.returncode != 0:
            return summary, None
        return summary, _show_preview(completed)

    def _invoke(self, verb: str, *args: str) -> subprocess.CompletedProcess:
        shadow = self._require_shadow()
        # Cleared first: dbt writes this as part of running, so a leftover from
        # the previous mutant would otherwise be read as this one's answer.
        (shadow / "target" / "run_results.json").unlink(missing_ok=True)
        argv = [
            _dbt_executable(),
            verb,
            "--target",
            self.target,
            "--project-dir",
            str(shadow),
            "--profiles-dir",
            str(profiles_dir(self.project).resolve()),
            "--target-path",
            str(shadow / "target"),
            "--log-path",
            str(shadow / "logs"),
            "--log-format",
            "json",
            # Pinned rather than inherited, for the reason the environment is
            # scrubbed: each of these changes what a run means.
            "--indirect-selection",
            "eager",
            "--no-defer",
            "--no-favor-state",
            "--no-fail-fast",
            *args,
        ]
        run = self._runner or _default_runner(
            self._timeout, self.project, env=self._env()
        )
        return run(argv)

    def _env(self) -> dict[str, str]:
        # Only the cap knobs are merged on top: merging a whole capped
        # environment would put back every `DBT_*` setting just scrubbed.
        env = _child_environ(isolated=True)
        env.update(_cap_overrides(self._connector, self._paradigm, self._ceiling))
        return env

    def _require_shadow(self) -> Path:
        if self.shadow is None:
            raise DbtRunError("the shadow project is only open inside a `with` block")
        return self.shadow


def has_package_spec(project_dir: Path | str) -> bool:
    """True when the project declares dbt packages (packages.yml, or a
    dependencies.yml with a ``packages:`` key)."""

    project = Path(project_dir)
    if (project / "packages.yml").is_file():
        return True
    dependencies = project / "dependencies.yml"
    if dependencies.is_file():
        try:
            parsed = yaml.safe_load(dependencies.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            return False
        return isinstance(parsed, dict) and bool(parsed.get("packages"))
    return False


class DeclaredPackage(BaseModel):
    """One entry from ``packages.yml`` or ``dependencies.yml``.

    ``install_name`` is the directory dbt installs it into, which is what a
    presence check compares against, and it is ``None`` wherever the declaration
    does not determine it. A git URL names a repository and a dbt package names
    itself, and those disagree often enough that guessing would report an
    installed package as missing. ``None`` means dex cannot check this one, never
    that it is fine.
    """

    source: str
    name: str
    version: str | None = None
    install_name: str | None = None

    def describe(self) -> str:
        return f"{self.name}@{self.version}" if self.version else self.name


def _package_entries(project: Path) -> list[dict[str, Any]]:
    """The raw ``packages:`` list, from whichever file declares it."""

    for filename in ("packages.yml", "dependencies.yml"):
        candidate = project / filename
        if not candidate.is_file():
            continue
        try:
            parsed = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("packages"), list):
            return [e for e in parsed["packages"] if isinstance(e, dict)]
    return []


def declared_packages(project_dir: Path | str) -> list[DeclaredPackage]:
    """Every package the project declares, in the three forms dbt accepts.

    Read rather than resolved: this says what the project asks for, not what a
    registry would hand back. That is the right half for a refusal, whose whole
    job is to name what the sandbox was expected to already have.
    """

    project = Path(project_dir)
    packages: list[DeclaredPackage] = []
    for entry in _package_entries(project):
        if isinstance(entry.get("package"), str):
            name = entry["package"]
            packages.append(
                DeclaredPackage(
                    source="hub",
                    name=name,
                    version=_version_text(entry.get("version")),
                    install_name=name.rsplit("/", 1)[-1] or None,
                )
            )
        elif isinstance(entry.get("git"), str):
            packages.append(
                DeclaredPackage(
                    source="git",
                    name=entry["git"],
                    version=_version_text(entry.get("revision")),
                    # A repository name is not a dbt package name, and dbt
                    # installs under the latter. Left unknown rather than guessed.
                    install_name=None,
                )
            )
        elif isinstance(entry.get("local"), str):
            local = entry["local"]
            packages.append(
                DeclaredPackage(
                    source="local",
                    name=local,
                    install_name=Path(local).name or None,
                )
            )
        elif isinstance(entry.get("tarball"), str):
            packages.append(
                DeclaredPackage(
                    source="tarball", name=entry["tarball"], install_name=None
                )
            )
    return packages


def _version_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def installed_packages(project_dir: Path | str) -> set[str]:
    """What is actually in ``dbt_packages/``, by directory and by declared name.

    Both, because dbt installs a hub package into a directory named after the
    package's second segment while the package's own ``dbt_project.yml`` may say
    something else, and a check that reads only one of them reports a present
    package as absent.
    """

    installed: set[str] = set()
    root = Path(project_dir) / "dbt_packages"
    if not root.is_dir():
        return installed
    for child in root.iterdir():
        if not child.is_dir():
            continue
        installed.add(child.name)
        manifest = child / "dbt_project.yml"
        if not manifest.is_file():
            continue
        try:
            parsed = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("name"), str):
            installed.add(parsed["name"])
    return installed


def missing_packages(project_dir: Path | str) -> list[DeclaredPackage]:
    """Declared packages this project does not have installed.

    An empty ``dbt_packages/`` means every declaration is missing, which is the
    common sandbox case and the one where naming them all is most useful.
    Otherwise only the ones dex can check are reported: a declaration whose
    install name is unknown is left out rather than named on a guess, and
    :func:`unverifiable_packages` is how a caller learns that happened.
    """

    project = Path(project_dir)
    declared = declared_packages(project)
    if not declared:
        return []
    installed = installed_packages(project)
    if not installed:
        return declared
    return [
        package
        for package in declared
        if package.install_name is not None and package.install_name not in installed
    ]


def unverifiable_packages(project_dir: Path | str) -> list[DeclaredPackage]:
    """Declared packages whose presence dex cannot check by name."""

    if not installed_packages(project_dir):
        return []
    return [p for p in declared_packages(project_dir) if p.install_name is None]


class MissingPackagesError(PrerequisiteError):
    """A build refused because a declared package is not installed.

    A :class:`~..errors.PrerequisiteError` because that is the one refusal family
    a caller can resolve automatically: run the named command and retry. In a
    sandbox with no network the command is not runnable there, and the refusal
    still names what the image was supposed to bake in, which is the actionable
    half.
    """

    def __init__(
        self,
        packages: list[DeclaredPackage],
        unverifiable: list[DeclaredPackage] | None = None,
    ):
        self.packages = packages
        self.unverifiable = unverifiable or []
        detail = (
            "these declared dbt packages are not installed: "
            + ", ".join(p.describe() for p in packages)
            if packages
            else "the declared dbt package installation cannot be verified"
        )
        if self.unverifiable:
            detail += (
                "; dex could not check "
                + ", ".join(p.describe() for p in self.unverifiable)
                + " by name, so those may be missing too"
            )
        super().__init__(
            f"this build may not install dependencies, and {detail}. "
            "Run `transform deps` "
            "where the network is available, or bake dbt_packages/ into the "
            "environment before the build"
        )


def assert_packages_installed(project_dir: Path | str) -> None:
    """Check every declaration, even when some packages are already installed.

    REFUSE cannot establish an installation whose declared name is unknown;
    report that uncertainty instead of admitting it as an installed package.
    The interactive installation heuristic remains separate.
    """

    missing = missing_packages(project_dir)
    unverifiable = unverifiable_packages(project_dir)
    if missing or unverifiable or needs_deps(project_dir):
        raise MissingPackagesError(missing, unverifiable)


def needs_deps(project_dir: Path | str) -> bool:
    """True when declared packages are not installed yet (dbt_packages/ missing
    or empty). Lockfile staleness is not tracked; `transform deps` is the
    explicit refresh."""

    project = Path(project_dir)
    if not has_package_spec(project):
        return False
    packages_dir = project / "dbt_packages"
    return not packages_dir.is_dir() or not any(packages_dir.iterdir())


def deps(
    project_dir: Path | str,
    *,
    runner: Runner | None = None,
    timeout: float = _DEPS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``dbt deps``. Returns a sanitized summary dict.

    Not confirmation-gated: deps never connects to a warehouse and writes only
    ``dbt_packages/`` plus the lockfile under the project dir. Inside ``build()``
    it runs after the target check and the cost gate anyway.
    """

    # Absolute so --project-dir does not double against the pinned cwd.
    project = Path(project_dir).resolve()
    argv = [
        _dbt_executable(),
        "deps",
        "--project-dir",
        str(project),
        "--log-format",
        "json",
    ]
    run = runner or _default_runner(timeout, project)
    completed = run(argv)
    messages: list[str] = []
    if completed.returncode != 0:
        messages = _collect_messages(completed, log_hint=project / "logs" / "dbt.log")
    packages_dir = project / "dbt_packages"
    return {
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "packages_dir_exists": packages_dir.is_dir() and any(packages_dir.iterdir()),
        "messages": messages,
    }


_COMPILE_TIMEOUT_SECONDS = 300.0

# Node kinds worth pricing: models materialize a table/view (ephemeral ones are
# inlined into their dependents, so they have no scan of their own), snapshots
# write a table, and data tests run a scanning SELECT. Seeds load a local CSV and
# scan nothing; everything else (operations, analyses, sources) issues no billed
# build statement.
_PRICED_RESOURCE_TYPES = {"model", "snapshot", "test"}


def compile_estimate(
    project_dir: Path | str,
    adapter: Any,
    *,
    target: str,
    select: str | None = None,
    runner: Runner | None = None,
    env: dict[str, str] | None = None,
    timeout: float = _COMPILE_TIMEOUT_SECONDS,
    approved_functions: frozenset[str] | None = None,
    overhead: float = 0.0,
    artifact_directory: Path | str | None = None,
    full_refresh: bool = False,
) -> tuple[float, dict[str, float], list[str]]:
    """Price a ``dbt build`` upfront, for free, by dry-running its compiled SQL.

    ``dbt compile`` renders every node's fully-compiled SQL against the dev
    target without executing it, so the build graph can be priced the same way
    ``explore`` prices an ad-hoc query: each node's compiled SQL goes through the
    connector's own free, execution-free estimator (``query_estimate`` -- a real
    dry-run on BigQuery, an ``EXPLAIN`` planner cost on Postgres, a size-based
    heuristic on the compute-time connectors). The per-node estimates are summed
    into a whole-build total in the connector's paradigm unit.

    Returns ``(total, per_node, notes)``. ``per_node`` maps each priced node's
    short name to its estimate (the breakdown the confirm handshake surfaces);
    ``notes`` explains anything left out, so nothing is silently dropped.

    Degrades rather than fabricates: a node whose compiled SQL cannot be priced
    (a downstream model referencing a dev relation an earlier layer has not built
    yet, an unparseable statement) is skipped and summarized in ``notes``, so on
    a cold dev target the total is an honest partial floor rather than a failure.
    ``query_estimate`` is a duck-typed convention, not part of the ``Adapter``
    protocol, so an adapter that does not expose it yields ``(0.0, {}, [note])``.

    ``overhead`` is what the warehouse bills the build as a whole rather than
    any node (Snowflake's resume minimum and idle tail), quoted by the caller
    before anything is priced. It is a line of its own under
    :data:`UPTIME_LINE`, so the breakdown says where it went and a build with
    nothing to scan still quotes it.

    ``artifact_directory`` is where the compile writes and where its artifacts
    are read back, the directory the build itself will write (issue #1014); by
    default the one :func:`~.artifact_runtime.resolve_build_artifact_dir`
    resolves for the isolated policy. ``full_refresh`` compiles with
    ``--full-refresh``, so ``is_incremental()`` renders false and each
    incremental node is priced as the full rebuild the build will run rather
    than as a batch (issue #1015).

    Raises :class:`DbtRunError` when ``dbt compile`` itself fails: that same
    failure would sink the build, and the caller degrades to no-estimate so dbt's
    own error is what the user sees, not a masked one from here.
    """

    project = Path(project_dir).resolve()
    artifacts = (
        Path(artifact_directory)
        if artifact_directory is not None
        else resolve_build_artifact_dir(project)
    )
    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None and not approved_functions:
        return (
            0.0,
            {},
            ["connector exposes no estimator; build cost not priced upfront"],
        )

    _compile_project(
        project,
        target=target,
        select=select,
        runner=runner or _default_runner(timeout, project, env),
        artifacts=artifacts,
        full_refresh=full_refresh,
    )
    if approved_functions:
        _assert_compiled_functions(
            artifacts, dialect=adapter.dialect, approved_functions=approved_functions
        )
    if estimator is None:
        return (
            0.0,
            {},
            ["connector exposes no estimator; build cost not priced upfront"],
        )

    compiled = _compiled_nodes(artifacts)
    total = 0.0
    per_node: dict[str, float] = {}
    skipped: list[str] = []
    for name, code in compiled:
        try:
            per_node[name] = estimator(code)
            total += per_node[name]
        except Exception:  # a node dbt could compile but the connector cannot price
            skipped.append(name)
    notes: list[str] = []
    if skipped:
        shown = ", ".join(skipped[:_MESSAGE_CAP])
        more = (
            f", and {len(skipped) - _MESSAGE_CAP} more"
            if len(skipped) > _MESSAGE_CAP
            else ""
        )
        notes.append(
            f"{len(skipped)} node(s) could not be priced and are excluded from the "
            "estimate (their inputs may not exist in the dev target until an earlier "
            f"layer is built): {shown}{more}; the total is a partial floor"
        )
    if not per_node and not skipped and overhead <= 0:
        notes.append("no scanning build nodes to price; the estimate is zero")
    if full_refresh:
        rebuilt = full_refresh_nodes(artifacts)
        if rebuilt["models"] or rebuilt["seeds"]:
            notes.append(
                "priced as a full refresh: "
                f"{_count(len(rebuilt['models']), 'incremental model')} and "
                f"{_count(len(rebuilt['seeds']), 'seed')} are priced as the full "
                "rebuild they will run, not as an incremental batch"
            )
        if not select:
            notes.append(full_refresh_unselected_warning(rebuilt))
    if overhead > 0:
        per_node[UPTIME_LINE] = overhead
        total += overhead
    return total, per_node, notes


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def full_refresh_nodes(artifact_directory: Path | str) -> dict[str, list[str]]:
    """What a ``--full-refresh`` run over this selection rebuilds from scratch.

    Read from the artifacts of the run (a build's, or the compile that priced
    it, which selects the same nodes): ``models`` are the selected incremental
    models and ``seeds`` the selected seeds. ``not_refreshed`` names the ones
    whose own ``full_refresh: false`` config makes dbt ignore the flag, so a
    caller does not read the rows they keep as rebuilt. A table or a view is
    rebuilt by every build and is not listed; a snapshot ignores the flag.
    """

    paths = artifact_paths(artifact_directory)
    found: dict[str, list[str]] = {"models": [], "seeds": [], "not_refreshed": []}
    try:
        results = json.loads(paths.run_results.read_text(encoding="utf-8"))["results"]
        nodes = json.loads(paths.manifest.read_text(encoding="utf-8"))["nodes"]
    except (OSError, ValueError, KeyError, TypeError):
        return found
    for result in results if isinstance(results, list) else []:
        unique_id = str((result or {}).get("unique_id", ""))
        node = nodes.get(unique_id) if isinstance(nodes, dict) else None
        if not isinstance(node, dict):
            continue
        config = node.get("config") or {}
        kind = node.get("resource_type")
        if kind == "seed":
            bucket = "seeds"
        elif kind == "model" and config.get("materialized") == "incremental":
            bucket = "models"
        else:
            continue
        label = manifest_node_label(unique_id, node)
        if config.get("full_refresh") is False:
            found["not_refreshed"].append(label)
        else:
            found[bucket].append(label)
    return {key: sorted(set(names)) for key, names in found.items()}


def full_refresh_unselected_warning(rebuilt: dict[str, list[str]]) -> str:
    """The warning a ``--full-refresh`` with no ``--select`` carries."""

    models = _count(len(rebuilt["models"]), "incremental model")
    seeds = _count(len(rebuilt["seeds"]), "seed")
    return (
        "--full-refresh with no --select rebuilds every incremental model and "
        "seed dbt's default selection reaches (the whole project, unless a "
        f"default selector narrows it): {models} and {seeds}. Name the models "
        "whose logic changed with --select to rebuild only those"
    )


def compiled_model_ids(
    project: Path, artifact_directory: Path | str | None = None
) -> set[str]:
    """The models dbt's last compile selected, by unique id.

    The same ``run_results.json`` the estimate is built from, which is what
    makes this the build's own selection rather than the whole project: a
    ``--select`` narrows the compile, so it narrows this too.
    """

    directory = (
        Path(artifact_directory)
        if artifact_directory is not None
        else readable_artifact_dir(project)
    )
    run_results = artifact_paths(directory).run_results
    if not run_results.is_file():
        return set()
    try:
        results = json.loads(run_results.read_text(encoding="utf-8")).get("results", [])
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        str(r["unique_id"])
        for r in results
        if str(r.get("unique_id", "")).startswith("model.")
    }


def _compile_project(
    project: Path,
    *,
    target: str,
    select: str | None,
    runner: Runner,
    artifacts: Path,
    full_refresh: bool = False,
) -> None:
    """Compile the selection afresh into ``artifacts``; never reuse a prior
    invocation's results."""

    clear_run_results(artifacts)
    argv = [
        _dbt_executable(),
        "compile",
        "--target",
        target,
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(profiles_dir(project).resolve()),
        "--log-format",
        "json",
        "--target-path",
        str(artifacts),
    ]
    if select:
        argv += ["--select", select]
    if full_refresh:
        argv.append("--full-refresh")
    completed = runner(argv)
    if completed.returncode != 0:
        messages = _collect_messages(completed, log_hint=project / "logs" / "dbt.log")
        raise DbtRunError(messages[0] if messages else "dbt compile failed")


def _assert_compiled_functions(
    artifacts: Path, *, dialect: str, approved_functions: frozenset[str]
) -> None:
    """Adjudicate every selected SQL node before pricing or building it.

    Pricing may omit unreadable nodes. An enabled execution restriction cannot,
    so missing artifacts or compiled SQL are hard failures on this path.
    """

    from ..guards.execution import guarded_statement_verdict
    from ..guards.sql_guard import NotSelectOnlyError, RefusalReason

    try:
        paths = artifact_paths(artifacts)
        results = json.loads(paths.run_results.read_text())
        manifest = json.loads(paths.manifest.read_text())
        selected = results["results"]
        nodes = manifest["nodes"]
        if not isinstance(selected, list) or not isinstance(nodes, dict):
            raise ValueError("invalid compile artifact shape")
        for result in selected:
            uid = result["unique_id"]
            node = nodes[uid]
            if node.get("resource_type") == "seed":
                continue
            code = node.get("compiled_code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError(f"{uid}: no compiled SQL available")
            verdict = guarded_statement_verdict(
                code,
                node=uid,
                dialect=dialect,
                approved_functions=approved_functions,
            )
            if not verdict.allowed:
                raise NotSelectOnlyError(
                    f"{uid}: {verdict.detail}",
                    reason=RefusalReason(verdict.reason),
                )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DbtRunError(f"cannot enforce guards.approved_functions: {exc}") from exc


def dynamic_bound_nodes(
    project_dir: Path | str,
    *,
    dialect: str,
    artifact_directory: Path | str | None = None,
) -> dict[str, str]:
    """The incremental nodes whose batch filter a dry run prices unpruned.

    An incremental model's batch filter is usually a watermark computed by a
    subquery over the model's own relation (``where d >= (select max(d) ...
    from {{ this }})``). ``dbt compile`` renders that branch once the dev
    relation exists, so the batch ``SELECT`` is what gets priced, but its bound
    is not a constant: a warehouse that prunes partitions only on constant
    predicates in a dry run prices the scan as though the filter pruned nothing,
    and prunes it only when the job runs. Billed then tracks the incremental
    window while the estimate tracks the accumulated history (issue #323).

    Returns each such node's label (the key :func:`compile_estimate` priced it
    under) mapped to the reason, naming the bounded column, so the caller can
    mark that line of the breakdown an upper bound rather than a measurement.
    Read from the same compile artifacts the estimate was built from, after it;
    a node whose SQL does not parse is left out rather than guessed at.
    """

    import sqlglot
    from sqlglot import expressions as exp

    bounded: dict[str, str] = {}
    directory = (
        Path(artifact_directory)
        if artifact_directory is not None
        else readable_artifact_dir(Path(project_dir).resolve())
    )
    for label, node, code in _priced_manifest_nodes(directory):
        if (node.get("config") or {}).get("materialized") != "incremental":
            continue
        try:
            statements = sqlglot.parse(code, dialect=dialect)
        except sqlglot.errors.SqlglotError:
            continue
        columns: set[str] = set()
        for statement in statements:
            if statement is None:
                continue
            for where in statement.find_all(exp.Where):
                for predicate in where.find_all(
                    exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between
                ):
                    if predicate.find(exp.Subquery) is None:
                        continue
                    columns.update(
                        column.name
                        for column in predicate.find_all(exp.Column)
                        if column.find_ancestor(exp.Subquery) is None
                    )
        if columns:
            named = ", ".join(sorted(columns))
            bounded[label] = (
                f"incremental batch filter bounds {named} by a subquery, which "
                "the dry run cannot prune on; the warehouse prunes it when the "
                "job runs, so this line is an upper bound"
            )
    return bounded


def _compiled_nodes(artifacts: Path) -> list[tuple[str, str]]:
    """The (label, compiled SQL) of each priced node dbt just compiled."""

    return [(label, code) for label, _node, code in _priced_manifest_nodes(artifacts)]


def _priced_manifest_nodes(artifacts: Path) -> list[tuple[str, dict[str, Any], str]]:
    """The (label, manifest node, compiled SQL) of each priced node.

    The selected set comes from compile's ``run_results.json`` (which honors the
    same ``--select`` a build would), joined to ``manifest.json`` for each node's
    compiled SQL, resource type, and materialization. Ephemeral models and nodes
    without compiled SQL contribute no billed statement and are dropped here.
    """

    paths = artifact_paths(artifacts)
    run_results, manifest = paths.run_results, paths.manifest
    if not run_results.is_file() or not manifest.is_file():
        return []
    compiled_ids = [
        str(r.get("unique_id"))
        for r in json.loads(run_results.read_text(encoding="utf-8")).get("results", [])
        if r.get("unique_id")
    ]
    nodes = json.loads(manifest.read_text(encoding="utf-8")).get("nodes", {})
    priced: list[tuple[str, dict[str, Any], str]] = []
    for uid in compiled_ids:
        node = nodes.get(uid)
        if not node or node.get("resource_type") not in _PRICED_RESOURCE_TYPES:
            continue
        if (node.get("config") or {}).get("materialized") == "ephemeral":
            continue
        code = node.get("compiled_code")
        if not code or not str(code).strip():
            continue
        priced.append((manifest_node_label(uid, node), node, str(code)))
    return priced


# --- helpers -----------------------------------------------------------------


def _dbt_executable() -> str:
    # Prefer the dbt installed next to this interpreter (the [duckdb] extra pulls
    # dbt-duckdb, and with it dbt-core); PATH is the fallback.
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("dbt")
    if found:
        return found
    raise DbtRunError(
        "dbt executable not found; install the connector extra that carries it "
        "(e.g. exmergo-dex-core[duckdb])"
    )


def _postgres_cap_env(ceiling: int) -> dict[str, str]:
    """libpq honors PGOPTIONS, so the ceiling becomes a per-statement
    server-side ``statement_timeout``."""

    cap = f"-c statement_timeout={ceiling}s"
    existing = os.environ.get("PGOPTIONS", "")
    return {"PGOPTIONS": f"{existing} {cap}".strip()}


# Kept in step with adapters.clickhouse._SCAN_BYTES_PER_SECOND: the cap and the
# quote are two views of one number, so a byte cap derived from a different
# rate than the estimate would bind somewhere the user was never shown.
_CH_SCAN_BYTES_PER_SECOND = 200 * 1024 * 1024


def _clickhouse_cap_env(ceiling: int) -> dict[str, str]:
    """ClickHouse takes its caps as query settings, which dbt-clickhouse carries
    in the profile's ``custom_settings``; a dex-rendered profile references
    these two variables so the value can be tightened per invocation.

    Both are set, not just time: ``max_execution_time`` is checked at block
    boundaries and a single fast block can overshoot it, so the byte cap is the
    one that actually binds on a scan.
    """

    from . import init

    return {
        init.CH_MAX_EXECUTION_TIME_ENV: str(ceiling),
        init.CH_MAX_BYTES_TO_READ_ENV: str(ceiling * _CH_SCAN_BYTES_PER_SECOND),
    }


# Kept in step with adapters.trino._SCAN_BYTES_PER_SECOND, for the same reason
# as ClickHouse's: the byte cap and the quote are two views of one number.
_TRINO_SCAN_BYTES_PER_SECOND = 200 * 1024 * 1024


def _trino_cap_env(ceiling: int) -> dict[str, str]:
    """dbt-trino sends the profile's ``session_properties`` with every
    statement, CTAS included; a dex-rendered profile references these two
    variables so the value can be tightened per invocation. Rendered with the
    units Trino's duration and data-size properties take."""

    from . import init

    scan_bytes = ceiling * _TRINO_SCAN_BYTES_PER_SECOND
    return {
        init.TRINO_QUERY_MAX_RUN_TIME_ENV: f"{ceiling}s",
        init.TRINO_QUERY_MAX_SCAN_BYTES_ENV: f"{scan_bytes}B",
    }


# Which environment variable carries the server-side cap into the dbt
# subprocess, per connector. Keyed by connector rather than by paradigm because
# there is more than one db-load connector and they cap by different mechanisms;
# keying on the paradigm alone would hand ClickHouse a libpq variable it ignores
# and then report that the build had been capped, which is a false safety claim
# rather than a missing feature. An unlisted db-load connector returns None
# rather than inheriting another connector's variable.
_CAP_ENV_BUILDERS: dict[str, Callable[[int], dict[str, str]]] = {
    "postgres": _postgres_cap_env,
    "clickhouse": _clickhouse_cap_env,
    "trino": _trino_cap_env,
}


def _cap_overrides(
    connector: str | None, paradigm: Paradigm, ceiling: float | None
) -> dict[str, str]:
    """The server-side cap variables alone, empty where no cap applies.

    On db-load gating the ceiling becomes a per-statement server-side cap: the
    ``maximum_bytes_billed`` analogue, so a build statement cannot load the
    database past the budget even if the upfront estimate under-priced it.
    """

    if ceiling is None:
        return {}
    # ClickHouse uses the same two server settings under both deployments; only
    # the meaning of the seconds changes. Other compute-time connectors own
    # their caps elsewhere, and other db-load connectors remain opt-in below.
    supported = paradigm is Paradigm.DB_LOAD or (
        connector == "clickhouse" and paradigm is Paradigm.COMPUTE_TIME
    )
    if not supported:
        return {}
    builder = _CAP_ENV_BUILDERS.get(connector or "")
    if builder is None:
        return {}
    return builder(max(int(ceiling), 1))


def _build_env(
    connector: str | None, paradigm: Paradigm, ceiling: float | None
) -> dict[str, str] | None:
    """The whole environment for a capped dbt subprocess, or ``None`` where no
    cap applies, which leaves :func:`_default_runner` to use
    :func:`_child_environ`."""

    overrides = _cap_overrides(connector, paradigm, ceiling)
    if not overrides:
        return None
    return {**_child_environ(), **overrides}


def _default_runner(
    timeout: float, cwd: Path, env: dict[str, str] | None = None
) -> Runner:
    def run(argv: list[str]) -> subprocess.CompletedProcess:
        # The argv is engine-built (dbt executable + validated target/paths),
        # never raw user input, and shell=False.
        return subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd),
            # Never `None`: inheriting would hand every macro the credentials
            # `_child_environ` withholds.
            env=env if env is not None else _child_environ(),
        )

    return run


# dbt 1.11 emits deprecation notices (e.g. PropertyMovedToConfigDeprecation) on
# every run of a normally-authored project, tagged `[WARNING]` in their own
# message text regardless of `info.level`. Left undistinguished from a real
# failure, one of these reliably wins the errors[0] slot merely by logging
# before the actual cause does. `MainEncounteredError` is dbt's own summary of
# what actually killed a *run-level* fatal (a connection or parse failure,
# before any node executes) and always leads when present.
#
# A per-node failure has no MainEncounteredError at all; instead dbt fires, in
# order, a bare progress line (`LogModelResult`, e.g. "1 of 1 ERROR creating
# sql view model x .... [ERROR in 0.1s]"), a bare `RunResultFailure` header
# ("Failure in model x (models/x.sql)"), and only then `RunResultError`, whose
# `msg` is the actual dbt exception text (`result.message`) -- the one event
# that names *why* the node failed. None of the first two are deprecation-
# tagged, so without also promoting `RunResultError`, whichever of them
# logged first silently wins errors[0] ahead of the real cause -- verified
# against a real `dbt build --log-format json` failure (issue #76).
_DEPRECATION_MARKER = "[WARNING]"
_MAIN_ENCOUNTERED_ERROR = "MainEncounteredError"
_RUN_RESULT_ERROR = "RunResultError"

# dbt wraps a failure's actual cause behind one or more generic headers with no
# information of their own. dbt_common's DbtRuntimeError (and its Database/
# Compilation/Validation/Runtime subclasses) render a per-node failure as
# "<Type> Error in <node> (<path>)", with the cause on the next line (e.g.
# "Database Error in model x (models/x.sql)\n  Argument 2 to JSON_VALUE must
# be a constant expression\n  compiled code at ..."). A whole-invocation fatal
# caught by dbt's own top-level handler is wrapped again in "Encountered an
# error:", and a chained/nested exception repeats a bare "<Type> Error" once
# per level before the innermost, actual message (verified against a real
# `dbt build --log-format json` run: "Encountered an error:\nRuntime Error\n
# Compilation Error\n    Could not render {{ 1/0 }}: division by zero"). This
# shape comes from dbt_common, not from any connector, so it is identical on
# Snowflake, BigQuery, or any other adapter. Keeping only the first line (a
# bare "first line of msg" truncation) silently dropped the cause behind
# whichever of these wrappers led; the #50 deprecation fix never caught it
# because #50's own repro didn't spell one out in its test message (#76).
_GENERIC_HEADER = re.compile(
    r"^(?:\[[A-Z]+\]:\s*)?(?:Encountered an error:|"
    r"(?:Database|Runtime|Compilation|Validation|Internal|Parsing) Error(?: in .+)?)$"
)
_VALIDATION_HEADER = re.compile(r"^\d+ validation errors? for .+$")

# dbt colors its console messages (red failures, yellow warnings) even under
# --log-format json, baking raw ANSI escapes into info.msg; harmless on a
# terminal, but unreadable noise once that text crosses into a JSON envelope.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _collect_messages(
    completed: subprocess.CompletedProcess, log_hint: Path | None = None
) -> list[str]:
    """Reduce dbt's output to actionable one-liners for the envelope.

    Keeps the first line of each error/warn message (redacted, length-capped,
    deduplicated) -- except a generic header (see _GENERIC_HEADER), which says
    nothing about why the node or run failed on its own, so the nearest
    following line that isn't itself another generic header rides along with
    it. Deprecation notices sink below real errors so they cannot poison the
    errors[0] slot, and dbt's own structured summaries of what actually
    failed -- MainEncounteredError for a run-level fatal, RunResultError for a
    per-node one -- always lead over an uninformative progress line or bare
    failure header that merely logged first. When anything was cut, the last
    entry points at the full log
    instead of letting a raw traceback cross the envelope boundary.
    """

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    trimmed = False
    for line in (completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = event.get("info", {})
        if info.get("level") not in {"error", "warn"} or not info.get("msg"):
            continue
        msg = redact(_ANSI_ESCAPE.sub("", str(info["msg"])))
        msg_lines = msg.splitlines()
        first_line = msg_lines[0].strip() if msg_lines else msg.strip()
        if _VALIDATION_HEADER.match(first_line):
            details = []
            for candidate in msg_lines[1:]:
                s = candidate.strip()
                if (
                    not s
                    or _GENERIC_HEADER.match(s)
                    or s.startswith(
                        ("Traceback", "compiled code at", "compiled SQL at")
                    )
                ):
                    break
                details.append(s)
            if details:
                first_line = f"{first_line}: {': '.join(details)}"
        elif _GENERIC_HEADER.match(first_line):
            idx = None
            for i, candidate in enumerate(msg_lines[1:], start=1):
                stripped = candidate.strip()
                if stripped and not _GENERIC_HEADER.match(stripped):
                    idx = i
                    break
            if idx is not None:
                cause = msg_lines[idx].strip()
                if _VALIDATION_HEADER.match(cause):
                    details = []
                    for candidate in msg_lines[idx + 1 :]:
                        s = candidate.strip()
                        if (
                            not s
                            or _GENERIC_HEADER.match(s)
                            or s.startswith(
                                ("Traceback", "compiled code at", "compiled SQL at")
                            )
                        ):
                            break
                        details.append(s)
                    if details:
                        cause = f"{cause}: {': '.join(details)}"
                sep = "" if first_line.endswith(":") else ":"
                first_line = f"{first_line}{sep} {cause}"
        if len(first_line) > _MESSAGE_MAX_CHARS:
            first_line = first_line[:_MESSAGE_MAX_CHARS] + "..."
            trimmed = True
        if first_line != msg:
            trimmed = True
        if first_line in seen:
            trimmed = True
            continue
        seen.add(first_line)
        entries.append((str(info.get("name") or ""), first_line))

    primary = [m for _, m in entries if _DEPRECATION_MARKER not in m]
    deprecations = [m for _, m in entries if _DEPRECATION_MARKER in m]
    main_error = next(
        (m for name, m in entries if name == _MAIN_ENCOUNTERED_ERROR), None
    )
    # Per-node cause(s), in case a build fails several models at once; a
    # run-level MainEncounteredError (when present) still leads them, since it
    # is the reason none of them, or nothing after them, ran at all.
    node_causes = [
        m for name, m in entries if name == _RUN_RESULT_ERROR and m != main_error
    ]
    promoted = ([main_error] if main_error is not None else []) + node_causes
    if promoted:
        primary = promoted + [m for m in primary if m not in promoted]
    messages = primary + deprecations

    if not messages and completed.stderr:
        messages.append(redact(completed.stderr.strip().splitlines()[-1]))
    if len(messages) > _MESSAGE_CAP:
        messages = messages[:_MESSAGE_CAP]
        trimmed = True
    if trimmed and log_hint is not None:
        messages.append(f"full output: {log_hint}")
    return messages


def _show_preview(
    completed: subprocess.CompletedProcess,
) -> list[dict[str, Any]] | None:
    """The rows ``dbt show --output json`` printed, from its ``ShowNode`` event.

    ``None`` when no such event was logged or its preview is not a JSON list of
    objects. The caller decides what a row may contain; this only finds it.
    """

    for line in (completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if (event.get("info") or {}).get("name") != "ShowNode":
            continue
        try:
            rows = json.loads((event.get("data") or {}).get("preview") or "")
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
            return rows
        return None
    return None


def _logged_invocation_id(completed: subprocess.CompletedProcess) -> str | None:
    """The invocation id dbt's ``--log-format json`` stamps on its log lines."""

    for line in (completed.stdout or "").splitlines():
        if not line.startswith("{"):
            continue
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            found = (json.loads(line).get("info") or {}).get("invocation_id")
            if found:
                return str(found)
    return None


def _summarize(
    project: Path,
    target: str,
    completed: subprocess.CompletedProcess,
    *,
    artifact_directory: Path | None = None,
    read_from: Path | None = None,
) -> dict[str, Any]:
    """Reduce a dbt run to a sanitized summary; raw log text stays behind.

    Each node carries both its ``unique_id`` and its readable ``name``, the
    label the manifest gives it. The id is the only unambiguous identifier (it
    names the resource type and the package, and it is what `run_results.json`
    can be cross-referenced on); the name is what a reader scans.

    ``artifact_directory``, when passed, is the directory this build resolved.
    The summary then carries that directory's publishable label. A caller that
    omits it reads ``read_from`` when given (a shadow copy's own directory,
    which no environment variable may redirect) and otherwise the newest
    artifacts on disk (:func:`~.artifact_runtime.readable_artifact_dir`), and
    gets no label.

    Results another invocation wrote are never reported as this one's (issue
    #1014). dbt's JSON log names the invocation on every line. When the
    ``run_results.json`` on disk names a different one, its nodes are dropped
    and the run is a failure that says so, rather than a success borrowed from
    an earlier or a foreign run.
    """

    nodes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    bytes_billed = 0.0
    saw_billing = False
    elapsed_seconds: float | None = None
    paths = artifact_paths(
        artifact_directory or read_from or readable_artifact_dir(project)
    )
    run_results = paths.run_results
    foreign: str | None = None
    if run_results.is_file():
        document = json.loads(run_results.read_text(encoding="utf-8"))
        results = document.get("results", [])
        written_by = (document.get("metadata") or {}).get("invocation_id")
        logged = _logged_invocation_id(completed)
        if written_by and logged and str(written_by) != logged:
            foreign = str(written_by)
            results = []
        # dbt writes this at the top of run_results, not inside `metadata`.
        if isinstance(elapsed := document.get("elapsed_time"), (int, float)):
            elapsed_seconds = float(elapsed)
        # The manifest names each node the way a reader knows it; without one
        # (a build that died before writing it) the label falls back to the id.
        manifest_nodes: dict[str, Any] = {}
        with contextlib.suppress(OSError, json.JSONDecodeError, AttributeError):
            manifest_nodes = (
                json.loads(paths.manifest.read_text(encoding="utf-8")).get("nodes")
                or {}
            )
        for result in results:
            status = str(result.get("status", "unknown"))
            unique_id = str(result.get("unique_id", ""))
            nodes.append(
                {
                    "name": manifest_node_label(
                        unique_id, manifest_nodes.get(unique_id)
                    ),
                    "unique_id": unique_id,
                    "status": status,
                    "execution_time": result.get("execution_time"),
                }
            )
            counts[status] = counts.get(status, 0) + 1
            # dbt-bigquery stamps per-node billing into adapter_response; free
            # adapters do not, so the key's absence means nothing to report.
            response = result.get("adapter_response") or {}
            if "bytes_billed" in response:
                saw_billing = True
                bytes_billed += float(response.get("bytes_billed") or 0)

    messages: list[str] = []
    if completed.returncode != 0:
        messages = _collect_messages(completed, log_hint=project / "logs" / "dbt.log")
    if foreign is not None:
        label = sanitize_artifact_directory(paths.directory, project_root=project)
        messages = [
            f"the run results in {label} "
            f"belong to another dbt invocation ({foreign}), not this run's; none "
            "of its nodes are reported, and the build is not a success",
            *messages,
        ]

    summary = {
        "target": target,
        "success": completed.returncode == 0 and foreign is None,
        "returncode": completed.returncode,
        "nodes": nodes,
        "counts": counts,
        "messages": messages,
    }
    # Internal hand-offs, not envelope shape: `_shape_build_result` pops these to
    # ledger the build and report it under `data.spend`, the one key every billed
    # command reports spend under (issue #276). `bytes_billed` is present only
    # when dbt actually reported a figure, so the shaper can tell "billed
    # nothing" from "billed an amount dbt never told us". `elapsed_seconds` is
    # the wall time the run's statements were spread over, which a warehouse
    # that bills uptime rather than statement time charges for in full.
    if saw_billing:
        summary["bytes_billed"] = bytes_billed
    if elapsed_seconds is not None:
        summary["elapsed_seconds"] = elapsed_seconds
    if artifact_directory is not None:
        summary["artifact_directory"] = sanitize_artifact_directory(
            paths.directory, project_root=project
        )
    # An internal hand-off like `bytes_billed`, popped by the command layer
    # before shaping: the evidence read is held to the invocation this run
    # logged, so a foreign run_results never yields evidence either.
    logged_id = _logged_invocation_id(completed)
    if artifact_directory is not None and logged_id is not None:
        summary["logged_invocation_id"] = logged_id
    return summary
