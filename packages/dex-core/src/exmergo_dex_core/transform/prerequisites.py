"""Who owns a failed prerequisite, and which repairs dbt supports for it (#860).

A build's completion block says a deliverable did not build and which node
stopped it. Fixing that node well takes three more facts, all of them in the
manifest: whether the project or an installed package owns it, whether the plan
under validation could have caused it, and which repair routes dbt actually has.

The options describe; ``dex transform repair`` generates the override route as a
plan (#1229), and each entry's ``next`` names it. An edit under ``dbt_packages/``
is not one of them, because the next ``dbt deps`` replaces it, and neither is a change
made only in the warehouse, because nothing reproduces it. A package model is not
always overridable either: dbt resolves ``ref('pkg', 'model')`` to the package's
own node, so disabling that node to shadow it fails to compile.

``plan_relation`` claims only what the graph can prove. ``independent`` means the
node neither is nor descends from anything the plan edits, so this plan cannot
have broken it. ``edited`` and ``downstream_of_edit`` say the plan could have, not
that it did, since dex holds no baseline run to compare with. A plan that edits a
macro, the project config or the packages can reach any node, so it is
``undetermined``, as is a build naming no plan, and so is a deletion or a schema
YAML that changes more than the nodes it names (a ``sources:`` block).

Read-only and connectionless: it reads a manifest and optional historical context
supplied by the caller. Earlier statuses never revise the graph's plan relation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from ..edits import EditOp
from .plans import EditKind, PlanEdit

#: The edit kinds that can change any node's result, wherever they sit.
_GLOBAL_KINDS = frozenset(
    {
        EditKind.MACRO_SQL,
        EditKind.PROJECT_YML,
        EditKind.PACKAGES_YML,
        EditKind.PROFILES_YML,
        EditKind.TEST_SQL,
    }
)

#: Keys of a schema YAML that only describe the nodes ``required`` already names.
#: Anything else (``sources:`` above all) can change what a model reads.
_DESCRIBING_KEYS = frozenset(
    {
        "version",
        "models",
        "unit_tests",
        "semantic_models",
        "metrics",
        "saved_queries",
        "exposures",
    }
)

#: dbt's own macros and the adapters', which a project does not dispatch over the
#: way it does a package's such as ``dbt_utils``.
_INTERNAL_PACKAGES = frozenset(
    {
        "dbt",
        "dbt_postgres",
        "dbt_bigquery",
        "dbt_snowflake",
        "dbt_redshift",
        "dbt_duckdb",
        "dbt_spark",
        "dbt_databricks",
        "dbt_clickhouse",
        "clickhouse",
        "trino",
        "dbt_trino",
    }
)

#: How many entries the payload lists, and how long one dbt message may run.
MAX_PREREQUISITES = 20
_MESSAGE = 400
_NAMES = 5


def _section(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = manifest.get(name)
    return section if isinstance(section, Mapping) else {}


def _name_of(unique_id: str) -> str:
    parts = unique_id.split(".")
    return parts[2] if len(parts) >= 3 else unique_id


def _required_names(unique_id: str, node: Any) -> set[str]:
    """Every name a required deliverable may use for this node, lowercased: the
    manifest name, and the file stem an edit's path gives it, which differs for a
    versioned model (``dim_customers.v2`` in ``dim_customers_v2.sql``)."""

    parts = unique_id.split(".")
    if len(parts) < 3 or parts[0] in ("test", "unit_test"):
        return set()
    names = {parts[2].lower()}
    if parts[0] == "model" and len(parts) == 4 and parts[3].startswith("v"):
        names.add(f"{parts[2]}_{parts[3]}".lower())
    path = node.get("original_file_path") if isinstance(node, Mapping) else None
    if path:
        names.add(PurePosixPath(str(path)).stem.lower())
    return names


def _children(graph: Mapping[str, list[str]]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for node, parents in graph.items():
        for parent in parents:
            children.setdefault(parent, []).append(node)
    return children


def _reach(start: Iterable[str], edges: Mapping[str, list[str]]) -> set[str]:
    reached: set[str] = set()
    pending = list(start)
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(edges.get(current, ()))
    return reached


def _plan_relation(
    unique_id: str,
    graph: Mapping[str, list[str]],
    edited: set[str] | None,
) -> str:
    if edited is None or not graph:
        return "undetermined"
    if unique_id in edited:
        return "edited"
    if _reach([unique_id], graph) & edited:
        return "downstream_of_edit"
    return "independent"


def _reaches_any_node(edit: PlanEdit) -> bool:
    """Whether an edit can change a node its own path or entry does not name."""

    if edit.kind in _GLOBAL_KINDS or edit.op is EditOp.DELETE:
        return True
    if edit.kind is not EditKind.SCHEMA_YML:
        return False
    try:
        parsed = yaml.safe_load(edit.new_content or "")
    except yaml.YAMLError:
        return True
    return not isinstance(parsed, Mapping) or bool(set(parsed) - _DESCRIBING_KEYS)


def _edited_nodes(
    nodes: Mapping[str, Any],
    graph: Mapping[str, list[str]],
    root: str | None,
    required: list[str] | None,
    edits: list[PlanEdit] | None,
) -> set[str] | None:
    """The root project's nodes the plan edits, or None where it could reach any."""

    if edits is None or required is None:
        return None
    if any(_reaches_any_node(edit) for edit in edits):
        return None
    names = {name.lower() for name in required}
    edited = set()
    for unique_id in graph:
        parts = unique_id.split(".")
        if (
            len(parts) >= 3
            and parts[1] == root
            and names & _required_names(unique_id, nodes.get(unique_id))
        ):
            edited.add(unique_id)
    return edited


def _package_entry(node: Mapping[str, Any], install_dir: str | None) -> str | None:
    """The package YAML that documents ``node``, from its manifest ``patch_path``
    (``<package>://models/x.yml``), as a path under the project where the install
    directory is known, and as dbt writes it otherwise."""

    patch = node.get("patch_path")
    if not isinstance(patch, str) or "://" not in patch:
        return None
    package, path = patch.split("://", 1)
    if not package or not path:
        return None
    if install_dir:
        return f"{install_dir.rstrip('/')}/{package}/{path}"
    return patch


def _override_option(
    unique_id: str,
    node: Mapping[str, Any],
    nodes: Mapping[str, Any],
    install_dir: str | None = None,
) -> dict[str, Any]:
    """The ``project_override`` route for a failed package node, or why not."""

    name = str(node.get("name") or _name_of(unique_id))
    package = node.get("package_name")

    def refused(reason: str) -> dict[str, Any]:
        """The route, refused for ``reason``."""

        return {"kind": "project_override", "supported": False, "reason": reason}

    if node.get("resource_type") != "model":
        return refused(
            f"it is a {node.get('resource_type') or 'non-model node'}, and only a "
            "model can be replaced by a project model of the same name"
        )
    if node.get("language") == "python":
        return refused("it is a Python model, whose body dex cannot read to restate")
    if node.get("version") is not None:
        return refused(
            "it is a versioned model, and a same-named project model does not take "
            "over its version resolution"
        )
    readers = sorted(
        str(other.get("name") or uid)
        for uid, other in nodes.items()
        if uid != unique_id
        and isinstance(other, Mapping)
        and any(
            isinstance(ref, Mapping)
            and ref.get("name") == name
            and ref.get("package") == package
            for ref in other.get("refs") or []
        )
    )
    if readers:
        return refused(
            f"{', '.join(readers[:_NAMES])} read it as ref('{package}', '{name}'), "
            "which binds to the package's node, so disabling that node fails to "
            "compile"
        )
    how = (
        f"`dex transform repair {name}` drafts it as a plan: a project model named "
        f"{name} that restates its SQL with the repair, plus `+enabled: false` "
        f"for {name} under models.{package} in dbt_project.yml; without the "
        "disable dbt refuses two resources with one database representation"
    )
    option: dict[str, Any] = {
        "kind": "project_override",
        "supported": True,
        "repair": repair_command(name),
    }
    entry = _package_entry(node, install_dir)
    if entry is not None:
        # dbt applies the package's entry to the override, and refuses a second
        # entry the project writes for it (#1161).
        option["package_entry"] = entry
        how += (
            f". {package} documents {name} in {entry}: dbt applies that entry to "
            "the override, so the override takes no YAML entry or declaration of "
            "its own; declare the models that read it"
        )
    option["how"] = how
    return option


def repair_command(name: str) -> str:
    """The command that drafts the override of package model ``name`` (#1229)."""

    return f"dex transform repair {name}"


def _next(entry: Mapping[str, Any]) -> str | None:
    """The one line a truncating reader keeps: the command that repairs it."""

    for option in entry.get("options") or ():
        if option.get("kind") == "project_override" and option.get("supported"):
            return f"repair: {option['repair']}"
    if entry.get("owner") == "project":
        return f"repair: transform plan on {entry.get('path') or entry.get('node')}"
    return None


def _dispatch_option(
    node: Mapping[str, Any], macros: Mapping[str, Any], root: str | None
) -> dict[str, Any]:
    # Walk depends_on.macros transitively: a plain wrapper that calls a
    # dispatching package macro still supports the macro_dispatch repair
    # (issue #1063). Cycle guard via seen set.
    dispatching: list[str] = []
    seen: set[str] = set()
    stack = list((node.get("depends_on") or {}).get("macros") or [])
    while stack:
        macro_id = stack.pop()
        if macro_id in seen:
            continue
        seen.add(macro_id)
        macro = macros.get(macro_id)
        if not isinstance(macro, Mapping):
            continue
        package = macro.get("package_name")
        if package == root or package in _INTERNAL_PACKAGES:
            continue
        if "adapter.dispatch" in str(macro.get("macro_sql") or ""):
            dispatching.append(f"{package}.{macro.get('name')}")
        stack.extend(
            nested
            for nested in (macro.get("depends_on") or {}).get("macros") or []
            if nested not in seen
        )
    if not dispatching:
        return {
            "kind": "macro_dispatch",
            "supported": False,
            "reason": "it calls no package macro that goes through adapter.dispatch",
        }
    return {
        "kind": "macro_dispatch",
        "supported": True,
        "macros": dispatching[:_NAMES],
        "how": (
            "a project macro plus a `dispatch` search order in dbt_project.yml, "
            "where the failing logic is in one of these macros; a `dispatch` edit "
            "is classified authority_bearing and goes through admission"
        ),
    }


def _options(
    unique_id: str,
    node: Mapping[str, Any] | None,
    owner: str,
    nodes: Mapping[str, Any],
    macros: Mapping[str, Any],
    root: str | None,
    install_dir: str | None = None,
) -> list[dict[str, Any]]:
    if owner == "project":
        return [
            {
                "kind": "project_edit",
                "supported": True,
                "how": "edit it through transform plan; the project owns it",
            }
        ]
    if node is None:
        return []
    return [
        _override_option(unique_id, node, nodes, install_dir),
        _dispatch_option(node, macros, root),
        {
            "kind": "pinned_dependency",
            "supported": True,
            "how": (
                "pin a repaired revision of the package in packages.yml; a "
                "dependency source or pin edit is classified and goes through "
                "admission, and it needs a repaired revision to exist"
            ),
        },
    ]


def diagnose(
    failed: Iterable[tuple[str, str, str | None]],
    manifest: Mapping[str, Any] | None,
    *,
    required: list[str] | None = None,
    edits: list[PlanEdit] | None = None,
    previous_run: dict[str, Any] | None = None,
    install_dir: str | None = None,
) -> list[dict[str, Any]]:
    """One entry per failed node: ``(unique_id, status, message)`` in, the owner,
    plan relation, historical context, what it blocks and repair routes out."""

    manifest = manifest or {}
    from .previous_build import node_context

    # Unit tests live in their own section, and they fail like any other node.
    nodes = {**_section(manifest, "nodes"), **_section(manifest, "unit_tests")}
    macros = _section(manifest, "macros")
    metadata = _section(manifest, "metadata")
    root = metadata.get("project_name")
    graph = {
        uid: list((node.get("depends_on") or {}).get("nodes") or [])
        for uid, node in nodes.items()
        if isinstance(node, Mapping)
    }
    children = _children(graph)
    edited = _edited_nodes(nodes, graph, root, required, edits)
    wanted = {name.lower(): name for name in required or []}

    out = []
    for unique_id, status, message in failed:
        parts = unique_id.split(".")
        package = parts[1] if len(parts) >= 3 else None
        node = nodes.get(unique_id)
        node = node if isinstance(node, Mapping) else None
        known = root is not None and package is not None
        owner = (
            "unknown" if not known else ("project" if package == root else "package")
        )
        blocks = sorted(
            {
                wanted[name]
                for uid in _reach([unique_id], children)
                for name in _required_names(uid, nodes.get(uid))
                if name in wanted
            }
        )
        entry: dict[str, Any] = {
            "node": str((node or {}).get("name") or _name_of(unique_id)),
            "unique_id": unique_id,
            "owner": owner,
            "status": status,
            "plan_relation": _plan_relation(unique_id, graph, edited),
            "previous_run": node_context(previous_run, unique_id),
            "blocks": blocks[:MAX_PREREQUISITES],
        }
        if owner == "package":
            entry["package"] = package
        if node is not None and node.get("original_file_path"):
            entry["path"] = node["original_file_path"]
        if message:
            entry["message"] = message[:_MESSAGE]
        if owner != "unknown":
            entry["options"] = _options(
                unique_id, node, owner, nodes, macros, root, install_dir
            )
        if (pointer := _next(entry)) is not None:
            entry["next"] = pointer
        out.append(entry)
    return out
