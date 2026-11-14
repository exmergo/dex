"""What a plan did to the models below the ones it edited (#1231).

Every other check of a plan judges the models it edits: change scope reads
their SQL, ``transform compare`` compares one of them, and the sweep attributes
findings by lineage. A repair at the staging or intermediate layer, the commonest
kind of dbt fix, can still move rows in a mart nobody edited, and that is where
the numbers are read. In one benchmark run an id repair in ``stg_products`` made
the unedited ``dim_products`` collapse from 45 rows to 40, and nothing said so.

This module measures that, after a ``--for-plan --verify`` build, for the
models the plan rewrote and every model below them it can reach. Nothing
before the plan survives a build (dbt overwrote the dev relations, and the sweep
history keeps fingerprints, not counts), so the baseline is re-executed rather
than remembered, the way :mod:`.refactor_compare` does it:

* Each side compiles in its own throwaway copy of the project. The baseline
  copy holds the edited files as they read before ``transform apply`` wrote
  over them (:attr:`TransformPlan.baseline_sources`, else the git ``HEAD``
  blob whose hash the plan recorded); the proposed copy holds the tree as it
  stands.
* In both, the edited models, every model on a path from them to a measured
  model, and the measured models themselves are made ephemeral, so dbt inlines
  them and both statements read only relations the plan does not change. The
  inputs are the same on both sides, and nothing is written.
* Per model, one aggregate statement counts rows and keys on each side (keys
  cast to text, so a key whose type the repair changed still matches), and,
  for a mart whose key is unique on both sides, a second counts the kept keys
  each column changed on. Names and counts only, never a value.

A descendant whose rows or keys moved is a ``downstream_population_changed``
finding naming the edited model upstream, unless the model's own declaration
records that population as an assumption at ``request`` or ``contract`` tier.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import sqlglot
import sqlglot.expressions as exp

if TYPE_CHECKING:
    from ..adapters.base import Adapter
    from ..engine import DexEngine
    from ..maintain.drift import DriftFinding
    from ..results import ConfirmationRequest

#: How many models one build measures, edited ones included, nearest first.
MAX_MODELS = 25

#: On a billed connector, a model whose own statements are estimated above this
#: share of the command's ceiling is skipped as ``unchecked: cost`` rather than
#: allowed to crowd out every other check of the build.
COST_SHARE = 0.25

#: The tiers at which a model's declaration settles which rows it holds.
SETTLING_TIERS = frozenset({"request", "contract"})

AXIS = "downstream_impact"
CODE = "downstream_population_changed"

#: Edit kinds whose effect on a model the re-executed baseline cannot isolate.
_GLOBAL_KINDS = frozenset({"macro_sql", "project_yml", "packages_yml"})
#: Edit kinds that change a relation both sides read: the dev relation already
#: holds what this build wrote.
_UNSYNCHRONIZING_KINDS = frozenset({"seed_csv", "snapshot_sql"})

_TEXT_TYPES = {
    "bigquery": "STRING",
    "databricks": "STRING",
    "clickhouse": "Nullable(String)",
}

_SIMPLE = re.compile(r"^[a-z_][a-z0-9_]*$")


# --- selection -----------------------------------------------------------------


@dataclass
class Target:
    """One model to measure."""

    unique_id: str
    label: str
    path: str
    role: str  # "edited" | "descendant"
    distance: int
    upstream: list[str] = field(default_factory=list)


@dataclass
class Selection:
    targets: list[Target] = field(default_factory=list)
    #: Models reached but not measurable, as ``unchecked`` records.
    cut: list[dict[str, Any]] = field(default_factory=list)
    #: Paths (relative to the project) inlined on both sides.
    inline: set[str] = field(default_factory=set)
    #: Edited model paths, created model paths, deleted model paths.
    edited_paths: set[str] = field(default_factory=set)
    created_paths: set[str] = field(default_factory=set)
    deleted_paths: set[str] = field(default_factory=set)
    #: Why nothing could be measured at all, with a stable code.
    reason_code: str | None = None
    reason: str | None = None


def _kind(edit: Any) -> str:
    kind = getattr(edit, "kind", None)
    return str(getattr(kind, "value", kind) or "")


def _op(edit: Any) -> str:
    op = getattr(edit, "op", None)
    return str(getattr(op, "value", op) or "upsert")


def _path(edit: Any) -> str:
    return PurePosixPath(str(getattr(edit, "path", ""))).as_posix()


def select_targets(
    edits: Sequence[Any], manifest: Mapping[str, Any], *, cap: int = MAX_MODELS
) -> Selection:
    """The edited models that existed before the plan, and every model below
    them, nearest first, with what could not be measured named.

    A plan that edits a macro, the project config or the packages changes how
    any model compiles, and one that edits a seed or a snapshot changes a
    relation both sides read; neither leaves a baseline this can re-execute.
    """

    from ..dbt_project import manifest_node_label
    from .mutation import _not_routable

    selection = Selection()
    kinds = {_kind(edit) for edit in edits}
    if kinds & _GLOBAL_KINDS:
        selection.reason_code = "global_edit"
        selection.reason = (
            "the plan edits a macro, the project config or the packages, which "
            "can change how any model compiles, so no model's pre-plan SQL can "
            "be re-executed on its own"
        )
        return selection
    if kinds & _UNSYNCHRONIZING_KINDS:
        selection.reason_code = "inputs_unsynchronized"
        selection.reason = (
            "the plan changes a seed or a snapshot, and its dev relation already "
            "holds what this build wrote, so the pre-plan rows cannot be read"
        )
        return selection

    nodes = manifest.get("nodes") or {}
    project = (manifest.get("metadata") or {}).get("project_name")
    by_file: dict[str, str] = {}
    for uid, node in nodes.items():
        # A package's model can sit at the same relative path as one of the
        # project's: only the project's own files are what a plan edits.
        if (
            str(uid).startswith("model.")
            and isinstance(node, Mapping)
            and (project is None or node.get("package_name") == project)
        ):
            path = node.get("original_file_path")
            if isinstance(path, str):
                by_file[PurePosixPath(path).as_posix()] = str(uid)

    roots: dict[str, str] = {}
    for edit in edits:
        if _kind(edit) != "model_sql":
            continue
        path = _path(edit)
        if _op(edit) == "delete":
            selection.deleted_paths.add(path)
            continue
        if getattr(edit, "old_content_hash", None) is None:
            selection.created_paths.add(path)
            continue
        selection.edited_paths.add(path)
        uid = by_file.get(path)
        if uid is not None:
            roots[uid] = manifest_node_label(uid, dict(nodes[uid]))
    if not roots:
        selection.reason_code = "no_rewritten_model"
        selection.reason = (
            "the plan rewrites no model that existed before it, so nothing it "
            "changed has rows to compare"
        )
        return selection

    children = _children(manifest, nodes)
    label = {
        str(uid): manifest_node_label(str(uid), dict(node))
        for uid, node in nodes.items()
        if isinstance(node, Mapping)
    }
    created_or_edited = {
        by_file[p]
        for p in selection.created_paths | selection.edited_paths
        if p in by_file
    }

    # Nearest first, by name within one distance, from every root at once.
    distance: dict[str, int] = dict.fromkeys(roots, 0)
    upstream: dict[str, set[str]] = {uid: {roots[uid]} for uid in roots}
    frontier = sorted(roots, key=lambda uid: label.get(uid, uid))
    blocked: set[str] = set()
    depth = 0
    while frontier:
        depth += 1
        reached: dict[str, set[str]] = {}
        for parent in frontier:
            for child in children.get(parent, ()):
                if not str(child).startswith(("model.", "snapshot.")):
                    continue
                reached.setdefault(child, set()).update(upstream.get(parent, set()))
        frontier = []
        for uid in sorted(reached, key=lambda u: (label.get(u, u), u)):
            if uid in distance or uid in blocked:
                upstream.setdefault(uid, set()).update(reached[uid])
                continue
            node = nodes.get(uid) or {}
            reason = _not_routable(dict(node), project)
            if reason is not None:
                blocked.add(uid)
                selection.cut.append(
                    _unchecked_record(
                        label.get(uid, uid),
                        "descendant",
                        sorted(reached[uid]),
                        "not_inlinable",
                        reason,
                    )
                )
                continue
            distance[uid] = depth
            upstream[uid] = set(reached[uid])
            frontier.append(uid)

    # Which edited models reach each one, read off the walk's own graph: two
    # edited models on one chain both reach what is below the second.
    for root in roots:
        seen = {root}
        pending = [root]
        while pending:
            current = pending.pop()
            for child in children.get(current, ()):
                if child in distance and child not in seen:
                    seen.add(child)
                    pending.append(child)
                    upstream.setdefault(child, set()).add(roots[root])

    # A model that also reads a node no copy can inline (a Python model or a
    # snapshot below the edits) would read that node's post-plan relation on
    # both sides, so its baseline would hide what came through it.
    parents = _parents(nodes)
    through_blocked: dict[str, str] = {}
    for uid in distance:
        seen: set[str] = set()
        pending = list(parents.get(uid, ()))
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in blocked:
                through_blocked[uid] = label.get(current, current)
                break
            if current in distance:
                pending.extend(parents.get(current, ()))

    ordered = sorted(
        distance,
        key=lambda uid: (distance[uid], label.get(uid, uid), uid),
    )
    for uid in ordered:
        if uid in created_or_edited and uid not in roots:
            # Another file of this plan: measured as edited if it existed.
            continue
        if uid in through_blocked and uid not in roots:
            selection.cut.append(
                _unchecked_record(
                    label.get(uid, uid),
                    "descendant",
                    sorted(upstream.get(uid, set()) - {label.get(uid, uid)}),
                    "not_inlinable",
                    f"it also reads {through_blocked[uid]}, below the plan's edits, "
                    "which cannot be inlined, so both sides would read its "
                    "post-plan relation",
                )
            )
            continue
        node = nodes.get(uid) or {}
        role = "edited" if uid in roots else "descendant"
        entry = Target(
            unique_id=uid,
            label=label.get(uid, uid),
            path=PurePosixPath(str(node.get("original_file_path") or "")).as_posix(),
            role=role,
            distance=distance[uid],
            upstream=sorted(upstream.get(uid, set()) - {label.get(uid, uid)})
            if role == "descendant"
            else [],
        )
        if len(selection.targets) >= cap:
            selection.cut.append(
                _unchecked_record(
                    entry.label,
                    role,
                    entry.upstream,
                    "cap",
                    f"beyond the cap of {cap} measured models, nearest first",
                )
            )
            continue
        selection.targets.append(entry)

    # Inline every reached model that sits on a path to a measured one: dbt
    # then compiles it into the measured model's SQL on both sides, so its dev
    # relation (which this build may have rewritten) is never what either reads.
    measured = {t.unique_id for t in selection.targets}
    ancestors: set[str] = set()
    pending = list(measured)
    while pending:
        current = pending.pop()
        for parent in parents.get(current, ()):
            if parent in distance and parent not in ancestors:
                ancestors.add(parent)
                pending.append(parent)
    for uid in measured | ancestors | set(roots):
        path = (nodes.get(uid) or {}).get("original_file_path")
        if isinstance(path, str):
            selection.inline.add(PurePosixPath(path).as_posix())
    return selection


def _children(
    manifest: Mapping[str, Any], nodes: Mapping[str, Any]
) -> dict[str, list[str]]:
    child_map = manifest.get("child_map")
    if isinstance(child_map, Mapping) and child_map:
        return {str(k): [str(c) for c in v or ()] for k, v in child_map.items()}
    children: dict[str, list[str]] = {}
    for uid, node in nodes.items():
        if isinstance(node, Mapping):
            for parent in (node.get("depends_on") or {}).get("nodes") or ():
                children.setdefault(str(parent), []).append(str(uid))
    return children


def _parents(nodes: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        str(uid): [str(p) for p in (node.get("depends_on") or {}).get("nodes") or ()]
        for uid, node in nodes.items()
        if isinstance(node, Mapping)
    }


# --- the pre-plan SQL ----------------------------------------------------------


def baseline_sources(
    edits: Sequence[Any],
    recorded: Mapping[str, str] | None,
    repo_root: Path | None,
    project_dir: str,
) -> tuple[dict[str, str], list[str], str]:
    """Each rewritten or deleted model file as it read before the plan.

    ``recorded`` is what ``transform apply`` kept; a file it did not keep is
    read from git's ``HEAD`` when that blob hashes to the preimage the plan
    recorded. Returns the sources, the paths with none, and where they came
    from (``apply``, ``git``, ``mixed`` or ``none``).
    """

    from ..dbt_project import content_hash

    found: dict[str, str] = {}
    missing: list[str] = []
    origins: set[str] = set()
    for edit in edits:
        if _kind(edit) != "model_sql":
            continue
        expected = getattr(edit, "old_content_hash", None)
        if expected is None and _op(edit) != "delete":
            continue
        path = _path(edit)
        if recorded and path in recorded:
            found[path] = recorded[path]
            origins.add("apply")
            continue
        content = _git_head(repo_root, project_dir, path)
        if content is not None and (
            expected is None or content_hash(content) == expected
        ):
            found[path] = content
            origins.add("git")
            continue
        missing.append(path)
    origin = (
        "none" if not origins else next(iter(origins)) if len(origins) == 1 else "mixed"
    )
    return found, missing, origin


def _git_head(repo_root: Path | None, project_dir: str, path: str) -> str | None:
    if repo_root is None:
        return None
    relative = PurePosixPath(project_dir or ".") / path
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo_root), "show", f"HEAD:{relative.as_posix()}"],  # noqa: S607
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- the two compiles ----------------------------------------------------------


@dataclass
class Compiled:
    """One measured model, compiled on both sides."""

    target: Target
    baseline_sql: str
    proposed_sql: str
    key: list[str]
    key_source: str | None
    settled_by: dict[str, Any] | None = None


@dataclass
class Prepared:
    """What a build measures, compiled and ready to price and run."""

    selection: Selection
    compiled: list[Compiled] = field(default_factory=list)
    unchecked: list[dict[str, Any]] = field(default_factory=list)
    reason_code: str | None = None
    reason: str | None = None
    baseline_origin: str = "none"
    notes: list[str] = field(default_factory=list)
    #: Per model label, the estimate of its statements (billed connectors only).
    estimates: dict[str, float] = field(default_factory=dict)
    #: The mart folders (``transform.marts_paths``), each ending in ``/``.
    marts: list[str] = field(default_factory=list)
    #: Whether :func:`price` ran (upfront, with the build's estimate).
    priced: bool = False


@dataclass
class ImpactInput:
    """What a ``--for-plan`` build hands the impact phase: the plan's edits and
    pre-plan sources, and, once priced upfront, the compiled sides, so the
    sweep does not compile them twice."""

    edits: list[Any]
    recorded: dict[str, str] | None
    repo_root: Path | None
    project_dir: str
    target: str
    connector: str
    marts_paths: list[str] = field(default_factory=list)
    prepared: Prepared | None = None


def ensure_prepared(
    engine: DexEngine, project: Path, given: ImpactInput, manifest_path: Path | None
) -> Prepared:
    """``given.prepared``, compiling it first if nothing has. Total: a failure
    is a ``Prepared`` whose reason says why nothing could be measured."""

    if given.prepared is not None:
        return given.prepared
    import json

    try:
        if manifest_path is None:
            from ..dbt_project import manifest_file

            manifest_path = manifest_file(project)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        given.prepared = prepare(
            engine,
            project,
            given.edits,
            recorded=given.recorded,
            repo_root=given.repo_root,
            project_dir=given.project_dir,
            target=given.target,
            connector=given.connector,
            manifest=manifest,
            marts_paths=given.marts_paths,
        )
    except Exception as exc:
        from ..envelope import failure_text, redact

        given.prepared = Prepared(
            selection=Selection(),
            reason_code="compile_failed",
            reason=redact(
                f"the models below the plan's edits could not be prepared "
                f"({failure_text(exc)})"
            ),
        )
    return given.prepared


def prepare(
    engine: DexEngine,
    project: Path,
    edits: Sequence[Any],
    *,
    recorded: Mapping[str, str] | None,
    repo_root: Path | None,
    project_dir: str,
    target: str,
    connector: str,
    manifest: Mapping[str, Any],
    marts_paths: Sequence[str] = (),
) -> Prepared:
    """Select, then compile both sides. Free: dbt compiles, nothing is queried."""

    selection = select_targets(edits, manifest)
    marts = [m.rstrip("/") + "/" for m in marts_paths]
    prepared = Prepared(selection=selection, unchecked=list(selection.cut), marts=marts)
    if selection.reason_code is not None:
        prepared.reason_code = selection.reason_code
        prepared.reason = selection.reason
        return prepared
    sources, missing, origin = baseline_sources(edits, recorded, repo_root, project_dir)
    prepared.baseline_origin = origin
    if missing:
        prepared.reason_code = "no_baseline_source"
        prepared.reason = (
            "the pre-plan SQL of "
            + ", ".join(sorted(missing))
            + " was neither recorded when the plan was applied nor found at git "
            "HEAD with the hash the plan recorded, so the tree before the plan "
            "cannot be compiled"
        )
        return prepared

    current: dict[str, str] = {}
    for path in selection.inline:
        file = project / path
        with contextlib.suppress(OSError, UnicodeDecodeError):
            current[path] = file.read_text(encoding="utf-8")
    baseline_files = {
        path: sources.get(path, current.get(path, ""))
        for path in selection.inline
        if path not in selection.created_paths
    }
    for path in selection.deleted_paths:
        if path in sources:
            baseline_files[path] = sources[path]
    proposed_files = {
        path: content for path, content in current.items() if path in selection.inline
    }
    names = [t.label for t in selection.targets]
    try:
        base_manifest, base_failed = _compile(
            engine,
            project,
            target,
            connector,
            baseline_files,
            removed=sorted(selection.created_paths),
            select=names,
        )
        prop_manifest, prop_failed = _compile(
            engine, project, target, connector, proposed_files, removed=[], select=names
        )
    except Exception as exc:
        from ..envelope import failure_text, redact

        prepared.reason_code = "compile_failed"
        prepared.reason = redact(
            f"the two sides could not be compiled ({failure_text(exc)})"
        )
        return prepared

    from .refactor_compare import nondeterministic_expression

    dialect = _dialect(connector)
    for entry in selection.targets:
        failed = base_failed.get(entry.path) or prop_failed.get(entry.path)
        base_node = (base_manifest.get("nodes") or {}).get(entry.unique_id) or {}
        prop_node = (prop_manifest.get("nodes") or {}).get(entry.unique_id) or {}
        base_sql = _sql(base_node)
        prop_sql = _sql(prop_node)
        if failed or not base_sql or not prop_sql:
            side = (
                "baseline"
                if (entry.path in base_failed or not base_sql)
                else "proposed"
            )
            prepared.unchecked.append(
                _unchecked_record(
                    entry.label,
                    entry.role,
                    entry.upstream,
                    f"{side}_not_compilable",
                    f"the {side} side of this model did not compile",
                    layer=_layer(entry.path, marts),
                )
            )
            continue
        found = nondeterministic_expression(
            base_sql, dialect
        ) or nondeterministic_expression(prop_sql, dialect)
        if found is not None:
            prepared.unchecked.append(
                _unchecked_record(
                    entry.label,
                    entry.role,
                    entry.upstream,
                    "nondeterministic_expression",
                    f"this model calls {found}, so two runs can differ with no change",
                    layer=_layer(entry.path, marts),
                )
            )
            continue
        key, key_source = model_key(
            entry.unique_id, prop_node, prop_manifest, prop_sql, dialect
        )
        prepared.compiled.append(
            Compiled(
                target=entry,
                baseline_sql=base_sql,
                proposed_sql=prop_sql,
                key=key,
                key_source=key_source,
                settled_by=_settlement(prop_node, key)
                if entry.role == "descendant"
                else None,
            )
        )
    return prepared


def _compile(
    engine: DexEngine,
    project: Path,
    target: str,
    connector: str,
    files: Mapping[str, str],
    *,
    removed: Sequence[str],
    select: Sequence[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    from .mutation import EPHEMERAL_HEADER
    from .refactor_compare import _shadow

    with _shadow(engine, project, target, connector) as shadow:
        shadow.strip_run_hooks()
        for path in removed:
            with contextlib.suppress(Exception):
                shadow.remove(path)
        for path, content in files.items():
            # Appended: dbt gives a scalar config key to the last config() call.
            shadow.write(path, content + "\n" + EPHEMERAL_HEADER + "\n")
            shadow.strip_semantic_models(PurePosixPath(path).stem)
        return shadow.compile_isolating(" ".join(select))


def _sql(node: Mapping[str, Any]) -> str:
    if node.get("language") == "python" or (node.get("config") or {}).get("sql_header"):
        return ""
    return str(node.get("compiled_code") or "").strip().rstrip(";").strip()


def _dialect(connector: str) -> str:
    from ..adapters import get_dialect

    try:
        return get_dialect(connector)
    except Exception:
        return connector


def _layer(path: str, marts: Sequence[str]) -> str:
    return "mart" if any(path.startswith(m) for m in marts) else "model"


# --- the key -------------------------------------------------------------------


def model_key(
    unique_id: str,
    node: Mapping[str, Any],
    manifest: Mapping[str, Any],
    sql: str,
    dialect: str,
) -> tuple[list[str], str | None]:
    """The model's key: its declared grain, else a uniqueness test on it, else
    the grain its own SQL proves (an outer ``GROUP BY`` or ``SELECT DISTINCT``
    of plain columns). ``([], None)`` when none says."""

    meta = (node.get("config") or {}).get("meta") or {}
    dex = meta.get("dex") if isinstance(meta, Mapping) else None
    grain = dex.get("grain") if isinstance(dex, Mapping) else None
    if isinstance(grain, list) and grain and all(isinstance(g, str) for g in grain):
        return list(grain), "declaration"
    tested = _tested_key(unique_id, manifest)
    if tested:
        return tested, "unique_test"
    drafted = _sql_grain(sql, dialect)
    if drafted:
        return drafted, "drafted"
    return [], None


def _tested_key(unique_id: str, manifest: Mapping[str, Any]) -> list[str]:
    single: list[str] = []
    for node in (manifest.get("nodes") or {}).values():
        if not isinstance(node, Mapping) or node.get("resource_type") != "test":
            continue
        if node.get("attached_node") != unique_id and unique_id not in (
            (node.get("depends_on") or {}).get("nodes") or ()
        ):
            continue
        metadata = node.get("test_metadata") or {}
        name = str(metadata.get("name") or "")
        kwargs = metadata.get("kwargs") or {}
        if name == "unique_combination_of_columns":
            columns = kwargs.get("combination_of_columns")
            if (
                isinstance(columns, list)
                and columns
                and all(isinstance(c, str) for c in columns)
            ):
                return list(columns)
        if name == "unique" and node.get("attached_node") == unique_id:
            column = node.get("column_name") or kwargs.get("column_name")
            if isinstance(column, str) and _SIMPLE.match(column.lower()) and not single:
                single = [column]
    return single


def _sql_grain(sql: str, dialect: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []
    if not isinstance(tree, exp.Select):
        return []
    outputs = [projection.alias_or_name for projection in tree.expressions]
    if any(not name or name == "*" for name in outputs):
        return []
    if tree.args.get("distinct") is not None and not tree.args.get("group"):
        return outputs
    group = tree.args.get("group")
    if group is None:
        return []
    keys: list[str] = []
    # Keyed by the qualified column, so `a.id` and `b.id` stay two columns; an
    # unqualified group-by column that matches more than one is ambiguous.
    by_source: dict[tuple[str, str], str] = {}
    for projection in tree.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Column):
            by_source[(inner.table.lower(), inner.name.lower())] = (
                projection.alias_or_name
            )
    for expression in group.expressions:
        if not isinstance(expression, exp.Column):
            return []
        table, name = expression.table.lower(), expression.name.lower()
        matches = [
            output
            for (source_table, source_name), output in by_source.items()
            if source_name == name
            and (not table or not source_table or source_table == table)
        ]
        if len(matches) != 1:
            return []
        keys.append(matches[0])
    if len(set(keys)) != len(keys):
        return []
    return keys


def _settlement(node: Mapping[str, Any], key: Sequence[str]) -> dict[str, Any] | None:
    """The assumption on this model that records its population, if any: one
    naming a key column at ``request`` or ``contract`` tier."""

    meta = (node.get("config") or {}).get("meta") or {}
    dex = meta.get("dex") if isinstance(meta, Mapping) else None
    assumptions = dex.get("assumptions") if isinstance(dex, Mapping) else None
    wanted = {k.lower() for k in key}
    # With no key there is nothing an assumption could name that settles
    # which rows the model holds.
    if not wanted:
        return None
    for item in assumptions or ():
        if not isinstance(item, Mapping):
            continue
        column = str(item.get("column") or "").lower()
        if item.get("evidence") in SETTLING_TIERS and column in wanted:
            return {"column": item.get("column"), "evidence": item.get("evidence")}
    return None


# --- the statements ------------------------------------------------------------


def _identifier(name: str, dialect: str, described: Iterable[str] | None) -> str:
    if described is not None:
        from .multiset_compare import _as_described

        return exp.to_identifier(_as_described(name, described), quoted=True).sql(
            dialect=dialect
        )
    if _SIMPLE.match(name):
        return name
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def counts_sql(
    baseline: str,
    proposed: str,
    key: Sequence[str],
    dialect: str,
    *,
    baseline_names: Iterable[str] | None = None,
    proposed_names: Iterable[str] | None = None,
) -> str:
    """Rows and keys on each side, in one statement that returns one row.

    With no key, the two row counts. With one, the keys are grouped across
    both sides (grouping is NULL-safe on every connector), cast to text so a
    key whose type the plan changed still matches, and counted: rows, distinct
    keys, kept, removed, added, and keys held more than once, per side.
    """

    if not key:
        return (
            f"select (select count(*) from ({baseline}) as dex_l) as dex_l_rows, "  # noqa: S608
            f"(select count(*) from ({proposed}) as dex_r) as dex_r_rows"
        )
    text = _TEXT_TYPES.get(dialect, "VARCHAR")
    left = ", ".join(
        f"cast({_identifier(k, dialect, baseline_names)} as {text}) as dex_k{i}"
        for i, k in enumerate(key)
    )
    right = ", ".join(
        f"cast({_identifier(k, dialect, proposed_names)} as {text}) as dex_k{i}"
        for i, k in enumerate(key)
    )
    group = ", ".join(f"dex_k{i}" for i in range(len(key)))
    return (
        "select coalesce(sum(dex_ln), 0) as dex_l_rows, "  # noqa: S608
        "coalesce(sum(dex_rn), 0) as dex_r_rows, "
        "count(case when dex_ln > 0 then 1 end) as dex_l_keys, "
        "count(case when dex_rn > 0 then 1 end) as dex_r_keys, "
        "count(case when dex_ln > 0 and dex_rn > 0 then 1 end) as dex_kept, "
        "count(case when dex_ln > 0 and dex_rn = 0 then 1 end) as dex_removed, "
        "count(case when dex_ln = 0 and dex_rn > 0 then 1 end) as dex_added, "
        "count(case when dex_ln > 1 then 1 end) as dex_l_dup, "
        "count(case when dex_rn > 1 then 1 end) as dex_r_dup "
        "from (select sum(dex_sl) as dex_ln, sum(dex_sr) as dex_rn from ("
        f"select {left}, 1 as dex_sl, 0 as dex_sr from ({baseline}) as dex_l "
        "union all "
        f"select {right}, 0 as dex_sl, 1 as dex_sr from ({proposed}) as dex_r"
        f") as dex_u group by {group}) as dex_g"
    )


def columns_sql(
    baseline: str,
    proposed: str,
    key: Sequence[str],
    columns: Sequence[tuple[str, str]],
    dialect: str,
) -> str:
    """Per compared column, how many kept keys it changed on (#1157's test: a
    key held once per side changed where the column has two distinct values,
    or one value and one NULL). ``columns`` are (baseline, proposed) names."""

    text = _TEXT_TYPES.get(dialect, "VARCHAR")

    def quote(name: str) -> str:
        return exp.to_identifier(name, quoted=True).sql(dialect=dialect)

    left = ", ".join(
        [f"cast({quote(b)} as {text}) as dex_k{i}" for i, (b, _) in enumerate(key)]
        + [f"{quote(b)} as dex_c{i}" for i, (b, _) in enumerate(columns)]
    )
    right = ", ".join(
        [f"cast({quote(p)} as {text}) as dex_k{i}" for i, (_, p) in enumerate(key)]
        + [f"{quote(p)} as dex_c{i}" for i, (_, p) in enumerate(columns)]
    )
    group = ", ".join(f"dex_k{i}" for i in range(len(key)))
    flags = ", ".join(
        f"case when count(distinct dex_c{i}) > 1 or (count(dex_c{i}) = 1 "
        f"and count(*) = 2) then 1 else 0 end as dex_f{i}"
        for i in range(len(columns))
    )
    sums = ", ".join(
        f"coalesce(sum(dex_f{i}), 0) as dex_n{i}" for i in range(len(columns))
    )
    return (
        f"select count(*) as dex_kept, {sums} from ("  # noqa: S608
        f"select {flags} from ("
        f"select {left}, 1 as dex_sl, 0 as dex_sr from ({baseline}) as dex_l "
        "union all "
        f"select {right}, 0 as dex_sl, 1 as dex_sr from ({proposed}) as dex_r"
        f") as dex_u group by {group} "
        "having sum(dex_sl) = 1 and sum(dex_sr) = 1) as dex_g"
    )


# --- running and judging -------------------------------------------------------


@dataclass
class ImpactResult:
    """What the impact phase measured, ready for the build's payload."""

    ran: bool
    records: list[dict[str, Any]] = field(default_factory=list)
    findings: list[DriftFinding] = field(default_factory=list)
    reason_code: str | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)
    offer: ConfirmationRequest | None = None
    baseline_origin: str | None = None

    def payload(self) -> dict[str, Any]:
        measured = [r for r in self.records if r.get("status") == "measured"]
        changed = [r for r in measured if r.get("change") == "changed"]
        out: dict[str, Any] = {
            "ran": self.ran,
            "measured": len(measured),
            "changed": len(changed),
            "unchecked": len(self.records) - len(measured),
        }
        if self.reason_code is not None:
            out["reason_code"] = self.reason_code
            out["reason"] = self.reason
        if self.baseline_origin is not None:
            out["baseline"] = self.baseline_origin
        out["models"] = self.records
        if self.notes:
            out["notes"] = self.notes
        return out


def price(prepared: Prepared, adapter: Adapter, ceiling: float | None) -> float:
    """The impact statements' estimate, with each model above
    :data:`COST_SHARE` of ``ceiling`` moved to ``unchecked: cost``.

    A mart's column statement reads both sides again, so it is priced as a
    second counts statement. Raises when the warehouse cannot price them (a
    relation the first build has not written yet): the caller prices again
    after the build.
    """

    estimator = getattr(adapter, "query_estimate", None)
    if estimator is None:
        return 0.0
    dialect = adapter.dialect
    total = 0.0
    kept: list[Compiled] = []
    skipped: list[dict[str, Any]] = []
    estimates: dict[str, float] = {}
    for item in prepared.compiled:
        sql = counts_sql(item.baseline_sql, item.proposed_sql, item.key, dialect)
        each = float(estimator(sql)) * (2 if _is_mart(item, prepared) else 1)
        if ceiling is not None and ceiling > 0 and each > COST_SHARE * ceiling:
            skipped.append(
                _unchecked_record(
                    item.target.label,
                    item.target.role,
                    item.target.upstream,
                    "cost",
                    f"its statements are estimated at {each:.0f}, above "
                    f"{int(COST_SHARE * 100)}% of the command ceiling ({ceiling:.0f})",
                    layer=_layer(item.target.path, _marts(prepared)),
                )
            )
            continue
        estimates[item.target.label] = each
        total += each
        kept.append(item)
    # Committed only once every statement priced, so a retry after a failure
    # starts from the same models.
    prepared.unchecked.extend(skipped)
    prepared.estimates = estimates
    prepared.compiled = kept
    prepared.priced = True
    return total


def _marts(prepared: Prepared) -> list[str]:
    return prepared.marts


def _is_mart(item: Compiled, prepared: Prepared) -> bool:
    return _layer(item.target.path, _marts(prepared)) == "mart"


def unmeasured(prepared: Prepared) -> ImpactResult:
    """The answer when nothing could be measured, saying why."""

    return ImpactResult(
        ran=False,
        records=sort_records(prepared.unchecked),
        reason_code=prepared.reason_code,
        reason=prepared.reason,
        notes=list(prepared.notes),
        baseline_origin=prepared.baseline_origin,
    )


def run(
    prepared: Prepared,
    adapter: Adapter,
    *,
    timeout_seconds: float,
    handshake: Callable[[float, int], ConfirmationRequest | None] | None,
    ceiling: float | None = None,
    billed: bool = False,
) -> ImpactResult:
    """Measure every prepared model and judge the descendants."""

    from ..guards.cost_guard import CostGuardError
    from ..guards.sql_guard import assert_select_only

    if prepared.reason_code is not None:
        return unmeasured(prepared)
    result = ImpactResult(ran=True, baseline_origin=prepared.baseline_origin)
    result.notes.extend(prepared.notes)

    if billed and not prepared.priced and prepared.compiled:
        try:
            price(prepared, adapter, ceiling)
        except CostGuardError:
            raise
        except Exception as exc:
            from ..envelope import failure_text, redact

            result.notes.append(
                redact(
                    f"the impact statements could not be priced ({failure_text(exc)})"
                )
            )
            for item in prepared.compiled:
                prepared.unchecked.append(
                    _unchecked_record(
                        item.target.label,
                        item.target.role,
                        item.target.upstream,
                        "not_priced",
                        "the warehouse could not price this model's statements",
                        layer=_layer(item.target.path, _marts(prepared)),
                    )
                )
            prepared.compiled = []
    if billed and handshake is not None and prepared.compiled:
        offer = handshake(sum(prepared.estimates.values()), len(prepared.compiled))
        if offer is not None:
            result.offer = offer
            for item in prepared.compiled:
                prepared.unchecked.append(
                    _unchecked_record(
                        item.target.label,
                        item.target.role,
                        item.target.upstream,
                        "cost",
                        "measuring it does not fit what remains of the confirmed "
                        "budget; offered above",
                        layer=_layer(item.target.path, _marts(prepared)),
                    )
                )
            prepared.compiled = []

    dialect = adapter.dialect
    describe = getattr(adapter, "describe_query", None)
    describe_free = callable(describe) and bool(
        getattr(adapter, "describe_query_free", True)
    )
    records: list[dict[str, Any]] = list(prepared.unchecked)
    for item in prepared.compiled:
        layer = _layer(item.target.path, _marts(prepared))
        try:
            record = _measure(
                item,
                adapter,
                dialect,
                layer=layer,
                describe=describe if describe_free else None,
                timeout_seconds=timeout_seconds,
                assert_sql=lambda sql: assert_select_only(sql, dialect=dialect),
            )
        except CostGuardError:
            raise
        except Exception as exc:
            # Class name only: a warehouse error can quote a value.
            records.append(
                _unchecked_record(
                    item.target.label,
                    item.target.role,
                    item.target.upstream,
                    "statement_failed",
                    f"the measuring statement failed ({type(exc).__name__})",
                    layer=layer,
                )
            )
            continue
        finding = judge(record, item)
        if finding is not None:
            result.findings.append(finding)
            record["finding"] = finding.code
        records.append(record)
    result.records = sort_records(records)
    return result


def _measure(
    item: Compiled,
    adapter: Adapter,
    dialect: str,
    *,
    layer: str,
    describe: Callable[[str], Any] | None,
    timeout_seconds: float,
    assert_sql: Callable[[str], Any],
) -> dict[str, Any]:
    from ..guards.cost_guard import CostGuardError
    from .multiset_compare import _as_described, _base_type, _normal_type, _read_counts

    base_cols = prop_cols = None
    if describe is not None:
        with contextlib.suppress(Exception):
            base_cols = list(describe(item.baseline_sql))
            prop_cols = list(describe(item.proposed_sql))
    base_names = [c.name for c in base_cols] if base_cols is not None else None
    prop_names = [c.name for c in prop_cols] if prop_cols is not None else None

    key = list(item.key)
    key_note: str | None = None
    if key and base_names is not None and prop_names is not None:
        lowered_b = {n.lower() for n in base_names}
        lowered_p = {n.lower() for n in prop_names}
        absent = [
            k for k in key if k.lower() not in lowered_b or k.lower() not in lowered_p
        ]
        if absent:
            key_note = (
                "the key column(s) "
                + ", ".join(absent)
                + " are not output on both sides, so only rows were compared"
            )
            key = []
    sql = counts_sql(
        item.baseline_sql,
        item.proposed_sql,
        key,
        dialect,
        baseline_names=base_names,
        proposed_names=prop_names,
    )
    assert_sql(sql)
    width = 9 if key else 2
    counts = _read_counts(
        adapter.run_query(sql, max_rows=1, timeout_seconds=timeout_seconds), width
    )
    record: dict[str, Any] = {
        "model": item.target.label,
        "role": item.target.role,
        "layer": layer,
        "status": "measured",
    }
    if item.target.upstream:
        record["upstream"] = item.target.upstream
    record["key"] = key
    record["key_source"] = item.key_source if key else None
    rows_b, rows_p = counts[0], counts[1]
    record["rows"] = {"baseline": rows_b, "proposed": rows_p, "delta": rows_p - rows_b}
    moved = rows_b != rows_p
    if key:
        lk, rk, kept, removed, added, ldup, rdup = counts[2:]
        record["keys"] = {
            "baseline": lk,
            "proposed": rk,
            "kept": kept,
            "removed": removed,
            "added": added,
            "duplicated_baseline": ldup,
            "duplicated_proposed": rdup,
        }
        moved = moved or removed > 0 or added > 0 or ldup != rdup
    if key_note:
        record["note"] = key_note

    changed_columns: list[dict[str, Any]] = []
    if (
        layer == "mart"
        and key
        and record["keys"]["duplicated_baseline"] == 0
        and record["keys"]["duplicated_proposed"] == 0
        and record["keys"]["kept"] > 0
    ):
        if base_cols is None or prop_cols is None:
            record["columns"] = {
                "compared": False,
                "reason": "reading the output columns is not free on this connector",
            }
        else:
            keys_lower = {k.lower() for k in key}
            prop_by = {c.name.lower(): c for c in prop_cols}
            base_by = {c.name.lower(): c for c in base_cols}
            from .multiset_compare import _UNGROUPABLE_TYPES

            refused = _UNGROUPABLE_TYPES.get(dialect, frozenset())
            pairs: list[tuple[str, str]] = []
            type_changed: list[str] = []
            not_compared: list[str] = []
            for column in base_cols:
                lower = column.name.lower()
                if lower in keys_lower or lower not in prop_by:
                    continue
                other = prop_by[lower]
                if _normal_type(column.data_type) != _normal_type(other.data_type):
                    type_changed.append(column.name)
                    continue
                base = _base_type(column.data_type)
                if (
                    base in refused
                    or base.isdigit()
                    or not getattr(column, "groupable", True)
                ):
                    not_compared.append(column.name)
                    continue
                pairs.append((column.name, other.name))
            columns_info: dict[str, Any] = {
                "compared": bool(pairs),
                "added": sorted(
                    c.name for c in prop_cols if c.name.lower() not in base_by
                ),
                "removed": sorted(
                    c.name for c in base_cols if c.name.lower() not in prop_by
                ),
                "type_changed": sorted(type_changed),
            }
            if not_compared:
                columns_info["not_compared"] = sorted(not_compared)
            if pairs:
                key_pairs = [
                    (
                        _as_described(k, base_names or []),
                        _as_described(k, prop_names or []),
                    )
                    for k in key
                ]
                sql = columns_sql(
                    item.baseline_sql, item.proposed_sql, key_pairs, pairs, dialect
                )
                try:
                    assert_sql(sql)
                    values = _read_counts(
                        adapter.run_query(
                            sql, max_rows=1, timeout_seconds=timeout_seconds
                        ),
                        len(pairs) + 1,
                    )
                except CostGuardError:
                    raise
                except Exception as exc:
                    # The rows and keys above stand; only the columns are unread.
                    columns_info["compared"] = False
                    columns_info["reason"] = (
                        f"the column statement failed ({type(exc).__name__})"
                    )
                else:
                    for (name, _), count in zip(pairs, values[1:], strict=True):
                        if count:
                            changed_columns.append({"column": name, "keys": count})
                    changed_columns.sort(key=lambda c: (-c["keys"], c["column"]))
                    columns_info["changed"] = changed_columns
            record["columns"] = columns_info
    any_column = bool(changed_columns) or bool(
        (record.get("columns") or {}).get("added")
        or (record.get("columns") or {}).get("removed")
        or (record.get("columns") or {}).get("type_changed")
    )
    record["change"] = "changed" if (moved or any_column) else "unchanged"
    record["population"] = "moved" if moved else "kept"
    record["size"] = _size(record)
    if item.settled_by is not None and moved:
        record["settled_by"] = item.settled_by
    return record


def _size(record: Mapping[str, Any]) -> int:
    rows = abs(int((record.get("rows") or {}).get("delta") or 0))
    keys = record.get("keys") or {}
    moved = int(keys.get("removed") or 0) + int(keys.get("added") or 0)
    columns = max(
        (
            int(c.get("keys") or 0)
            for c in (record.get("columns") or {}).get("changed") or ()
        ),
        default=0,
    )
    return max(rows, moved) * 1_000_000 + columns


def sort_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Largest change first, then measured before unchecked, then by name."""

    return sorted(
        records,
        key=lambda r: (
            -int(r.get("size") or 0),
            0 if r.get("status") == "measured" else 1,
            0 if r.get("role") == "descendant" else 1,
            str(r.get("model") or ""),
        ),
    )


def judge(record: Mapping[str, Any], item: Compiled) -> DriftFinding | None:
    """A finding for a descendant whose population moved, unless its own
    declaration records that population at ``request`` or ``contract`` tier.
    The edited models' own change is ``transform compare``'s to judge."""

    from ..maintain.drift import DriftFinding

    if record.get("role") != "descendant" or record.get("population") != "moved":
        return None
    if record.get("settled_by"):
        return None
    rows = record["rows"]
    keys = record.get("keys") or {}
    removed = int(keys.get("removed") or 0)
    added = int(keys.get("added") or 0)
    lost = removed > 0 or rows["delta"] < 0
    upstream = ", ".join(record.get("upstream") or ()) or "the plan's edits"
    parts = []
    if keys:
        if removed:
            parts.append(f"{removed} key(s) removed")
        if added:
            parts.append(f"{added} key(s) added")
        if not removed and not added:
            parts.append("keys now held a different number of times")
    moved = ", ".join(parts) if parts else "rows moved"
    on = f" on {', '.join(record.get('key') or [])}" if keys else ""
    detail = (
        f"{record['model']}: {moved}{on} ({rows['baseline']} -> {rows['proposed']} "
        f"rows), caused by {upstream}. The plan did not edit this model and nothing "
        "in its declaration records the new population: fix the upstream edit, or "
        "record the population as an assumption naming its key column at request "
        "or contract tier"
    )
    return DriftFinding(
        axis=AXIS,
        code=CODE,
        identifier=str(record["model"]),
        severity="high" if lost else "medium",
        detail=detail,
        data={
            "upstream": list(record.get("upstream") or ()),
            "rows": dict(rows),
            **({"keys": dict(keys)} if keys else {}),
            "key": list(record.get("key") or ()),
            "key_source": record.get("key_source"),
        },
    )


def _unchecked_record(
    model: str,
    role: str,
    upstream: Sequence[str],
    reason_code: str,
    reason: str,
    *,
    layer: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "model": model,
        "role": role,
        "status": "unchecked",
        "reason_code": reason_code,
        "reason": reason,
    }
    if layer is not None:
        record["layer"] = layer
    if upstream:
        record["upstream"] = list(upstream)
    return record
