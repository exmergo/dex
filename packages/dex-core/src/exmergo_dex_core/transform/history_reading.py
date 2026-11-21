"""What a plan reads of a declared ``history`` block's snapshot (#1458).

``maintain verify`` judges a temporal contract from the compiled manifest: it
measures nothing where the model does not descend from the declared snapshot
(``snapshot_not_ancestor``), where the manifest cannot say
(``lineage_unavailable``), or where the snapshot writes hard-delete records
(``hard_delete_records``). A plan rendering ``dex_temporal_contract`` has no
manifest of the project it leaves, so it reads the same facts from the
project's files: the ``ref()`` graph for the lineage, and the snapshot's
config (its ``config()`` call, its YAML entry, ``dbt_project.yml``) for
``hard_deletes`` and the validity column ``snapshot_meta_column_names`` may
rename. A test is rendered only where the reading settles that verify would
measure the measure; anything the reading cannot settle is withheld, never
guessed.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any

import yaml

from .. import generated_tests as gt
from .. import model_versions as mv

#: The snapshot config keys the reading needs, and nothing else.
_KEYS = ("hard_deletes", "snapshot_meta_column_names", "invalidate_hard_deletes")
_CONFIG = re.compile(r"\{\{-?\s*config\s*\(", re.DOTALL)
_LITERAL_NAMES = {"true": True, "false": False, "none": None}


def history_reader(view: Any, files: Mapping[str, str]) -> Callable[[str, Any], Any]:
    """A reader of ``(model, history)`` over the project as ``files`` leave it.

    The reference index is built once, on the first call, so a plan declaring
    no history pays nothing for it."""

    built: list[Any] = []

    def index() -> Any:
        if not built:
            from ..dbt_project import SourceFile, content_hash
            from ..references import ReferenceIndex

            after = view.model_copy(
                update={
                    "files": {
                        path: SourceFile(
                            path=path, content=content, sha256=content_hash(content)
                        )
                        for path, content in files.items()
                    }
                }
            )
            built.append(ReferenceIndex(after))
        return built[0]

    def read(model: str, history: Any) -> gt.HistoryReading:
        config = snapshot_config(files, view, str(history.snapshot))
        lineage, why, others = _lineage(index, files, view, model, history.snapshot)
        renamed = config.get("snapshot_meta_column_names")
        valid_from = "dbt_valid_from"
        if isinstance(renamed, Mapping) and isinstance(
            renamed.get("dbt_valid_from"), str
        ):
            valid_from = renamed["dbt_valid_from"]
        hard_deletes = config.get("hard_deletes")
        return gt.HistoryReading(
            lineage=lineage,
            why=why,
            other_snapshots=others,
            hard_deletes=hard_deletes if isinstance(hard_deletes, str) else None,
            valid_from=valid_from,
            config_unread=config.get("_unread"),
        )

    return read


# --- lineage ----------------------------------------------------------------------


def _lineage(
    index: Callable[[], Any],
    files: Mapping[str, str],
    view: Any,
    model: str,
    snapshot: str,
) -> tuple[str, str | None, tuple[str, ...]]:
    """``(lineage, why, other snapshots)`` as :class:`~..generated_tests.
    HistoryReading` spells them. A snapshot found among the ancestors settles
    it whatever else the walk met; one not found settles it only where nothing
    on the way could hide a relation, as ``snapshot_lineage`` reasons over the
    manifest."""

    from ..dbt_project import jinja_regions
    from .contradictions import _hidden_call
    from .declarations import model_sql_path

    graph = index()
    _base, version = mv.split_label(model)
    nodes = graph.version_nodes([model]) if version is not None else {model}
    nodes = nodes or {model}
    if not any(
        graph.node_kind(n) is not None or graph.package_node_kind(n) is not None
        for n in nodes
    ):
        return (
            "unknown",
            f"'{model}' is not a SQL model the project's ref() graph holds",
            (),
        )
    ancestors: set[str] = set()
    for node in nodes:
        ancestors |= graph.ancestors_of(node)
    lowered = {name.lower() for name in ancestors}
    if snapshot.lower() in lowered:
        return "descends", None, ()
    for reference in graph.indeterminate_for("model"):
        if PurePosixPath(reference.path).stem.lower() in lowered:
            return (
                "unknown",
                f"{reference.path} carries a ref() dex could not resolve",
                (),
            )
    for name in sorted(ancestors):
        known = graph.node_kind(name) or graph.package_node_kind(name)
        if name not in nodes and known is None:
            return (
                "unknown",
                f"'{name}' is not a node the project's ref() graph can read",
                (),
            )
        path = model_sql_path(files, view.model_paths, name)
        if path is None:
            continue
        hidden = _hidden_call(jinja_regions(files[path])[0])
        if hidden is not None:
            return (
                "unknown",
                f"'{name}' calls {hidden}(), which may read a relation dex cannot see",
                (),
            )
    others = tuple(
        sorted(name for name in ancestors if graph.node_kind(name) == "snapshot")
    )
    return "not_ancestor", None, others


# --- snapshot config --------------------------------------------------------------


#: A config value no static read settles (a Jinja expression), per key.
_UNREAD = object()


def snapshot_config(files: Mapping[str, str], view: Any, name: str) -> dict[str, Any]:
    """The keys of :data:`_KEYS` the snapshot ``name`` sets, layered as dbt
    layers them, per key: ``dbt_project.yml``'s ``snapshots:`` tree, then the
    snapshot's YAML properties entry, then each ``config()`` call in its SQL
    block, in order, the later winning. ``_unread`` names a key whose effective
    value is something no static read can settle (a Jinja expression); a
    literal at a later layer settles it."""

    roots = [PurePosixPath(p) for p in view.snapshot_paths]
    block = re.compile(
        rf"{{%-?\s*snapshot\s+{re.escape(name)}\s*-?%}}(?P<body>.*?){{%-?\s*endsnapshot",
        re.DOTALL,
    )
    sql_path: PurePosixPath | None = None
    yaml_path: PurePosixPath | None = None
    inline: list[dict[str, Any]] = []
    properties: dict[str, Any] = {}
    for path in sorted(files):
        candidate = PurePosixPath(path)
        if candidate.suffix == ".sql" and any(r in candidate.parents for r in roots):
            found = block.search(files[path]) if sql_path is None else None
            if found is not None:
                sql_path, inline = candidate, _config_calls(found.group("body"))
        elif candidate.suffix in (".yml", ".yaml") and "snapshots" in files[path]:
            entry = _yaml_entry(files[path], name) if yaml_path is None else None
            if entry is not None:
                yaml_path = candidate
                config = entry.get("config")
                properties = {
                    key: _settled(config[key])
                    for key in _KEYS
                    if isinstance(config, Mapping) and key in config
                }
    layers = [
        _project_config(files, view, sql_path or yaml_path, sql=sql_path is not None),
        properties,
        *inline,
    ]
    merged: dict[str, Any] = {}
    for layer in layers:
        merged.update(layer)
    out = {key: value for key, value in merged.items() if value is not _UNREAD}
    unread = [key for key in _KEYS if merged.get(key) is _UNREAD]
    if unread:
        out["_unread"] = unread[0]
    if out.get("invalidate_hard_deletes") is True and "hard_deletes" not in merged:
        out["hard_deletes"] = "invalidate"
    return out


def _settled(value: Any) -> Any:
    """``value``, or :data:`_UNREAD` where it holds a Jinja expression (a
    string, or any string inside a mapping or list)."""

    if isinstance(value, str):
        return _UNREAD if ("{{" in value or "{%" in value) else value
    if isinstance(value, Mapping):
        return _UNREAD if any(_settled(v) is _UNREAD for v in value.values()) else value
    if isinstance(value, list):
        return _UNREAD if any(_settled(v) is _UNREAD for v in value) else value
    return value


def _yaml_entry(content: str, name: str) -> Mapping[str, Any] | None:
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    entries = parsed.get("snapshots") if isinstance(parsed, Mapping) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, Mapping) and entry.get("name") == name:
            return entry
    return None


def _config_calls(body: str) -> list[dict[str, Any]]:
    """The :data:`_KEYS` each ``config()`` call in a snapshot block sets, in
    the order written (a later call wins a key, as dbt merges them). A key set
    to an expression maps to :data:`_UNREAD`; a call whose arguments do not
    parse leaves every key it names unread."""

    out: list[dict[str, Any]] = []
    for found in _CONFIG.finditer(body):
        depth, end = 1, None
        for offset in range(found.end(), len(body)):
            if body[offset] == "(":
                depth += 1
            elif body[offset] == ")":
                depth -= 1
                if depth == 0:
                    end = offset
                    break
        arguments = body[found.end() : end] if end is not None else body[found.end() :]
        try:
            call = ast.parse(f"f({arguments})", mode="eval").body
        except SyntaxError:
            out.append(_unread_if_named(arguments))
            continue
        layer: dict[str, Any] = {}
        for keyword in getattr(call, "keywords", []):
            if keyword.arg is None:
                # `**something`: any key may come from it.
                layer.update(dict.fromkeys(_KEYS, _UNREAD))
            elif keyword.arg in _KEYS:
                try:
                    layer[keyword.arg] = _settled(_literal(keyword.value))
                except ValueError:
                    layer[keyword.arg] = _UNREAD
        out.append(layer)
    return out


def _unread_if_named(text: str) -> dict[str, Any]:
    return {key: _UNREAD for key in _KEYS if re.search(rf"\b{key}\b", text)}


def _literal(node: ast.AST) -> Any:
    """A Python literal, with Jinja's lower-case ``true``/``false``/``none``."""

    if isinstance(node, ast.Name) and node.id.lower() in _LITERAL_NAMES:
        return _LITERAL_NAMES[node.id.lower()]
    if isinstance(node, ast.Dict):
        return {
            _literal(k) if k is not None else None: _literal(v)
            for k, v in zip(node.keys, node.values, strict=True)
        }
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError) as exc:
        raise ValueError("not a literal") from exc


def _project_config(
    files: Mapping[str, str],
    view: Any,
    where: PurePosixPath | None,
    *,
    sql: bool,
) -> dict[str, Any]:
    """What ``dbt_project.yml``'s ``snapshots:`` tree sets for a snapshot
    defined at ``where``, walked along its fqn as dbt resolves it, the deeper
    level winning: the project, each folder, and, for a SQL snapshot, the
    file's stem (dbt puts it in the fqn; a YAML-defined snapshot's fqn has no
    file level)."""

    try:
        parsed = yaml.safe_load(files.get("dbt_project.yml") or "")
    except yaml.YAMLError:
        return {}
    tree = parsed.get("snapshots") if isinstance(parsed, Mapping) else None
    if not isinstance(tree, Mapping):
        return {}
    out: dict[str, Any] = {}

    def take(level: Mapping[str, Any]) -> None:
        for key in _KEYS:
            for spelled in (key, f"+{key}"):
                if spelled in level:
                    out[key] = _settled(level[spelled])

    take(tree)
    level = tree.get(view.project_name)
    if not isinstance(level, Mapping):
        return out
    take(level)
    if where is None:
        return out
    parts: list[str] = []
    for root in view.snapshot_paths:
        root_path = PurePosixPath(root)
        if root_path in where.parents:
            parts = list(where.relative_to(root_path).parent.parts)
            break
    if sql:
        parts.append(where.stem)
    for part in parts:
        level = level.get(part)
        if not isinstance(level, Mapping):
            break
        take(level)
    return out
