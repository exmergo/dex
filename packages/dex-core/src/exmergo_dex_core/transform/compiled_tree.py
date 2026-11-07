"""One project tree compiled at one set of vars, and the columns it outputs.

Shared by the toggle check (#1113, #1156) and the retirement check (#858, #1170),
so both contracts judge a model by what dbt compiled rather than by its source
text, and resolve the same ``*`` the same way:

- :func:`compile_branch` compiles every model this project owns in a throwaway
  copy. A model whose own compile fails is set aside and the rest compiled
  again (:meth:`~.build.ShadowRun.compile_isolating`), so one broken model is
  that model's problem and never the whole tree's.
- :class:`Catalog` reads each model's output columns from its compiled SQL,
  following a ``*`` to a model compiled in the same branch, a seed's CSV header,
  or the exploration cache for a source, a snapshot or a package model. A
  relation it cannot read is a reason naming what would supply it, never a
  guess.
- :func:`macro_callers` names, for each project macro, the models whose compiled
  node calls it, directly or through another macro, which is how a ``var()``
  read inside a macro is attributed to the models it changes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .plans import EditOp

if TYPE_CHECKING:
    from ..dbt_project import DbtProjectView
    from ..engine import DexEngine


class Model:
    """One model's compiled form in one branch."""

    def __init__(
        self,
        sql: str,
        relation: str | None,
        package: str,
        path: str,
        *,
        macros: tuple[str, ...] = (),
        parents: tuple[str, ...] = (),
    ) -> None:
        """One model's compiled SQL, relation and file, with no columns read yet."""

        self.sql = sql
        self.relation = relation
        self.package = package
        self.path = path
        self.macros = macros
        self.parents = parents
        self.columns: list[str] | None = None
        self.reason: str | None = None


class Branch:
    """Every project model compiled at one value, or why the compile failed.

    ``disabled`` maps a model dbt left disabled to its file, ``failed`` a model
    whose own compile failed (and was set aside) to dbt's reason, and
    ``failure`` is set only when nothing in the branch compiled.
    """

    def __init__(self) -> None:
        """An empty branch: no models, nothing disabled, no failure."""

        self.models: dict[str, Model] = {}
        self.disabled: dict[str, str] = {}
        self.failed: dict[str, str] = {}
        self.failed_paths: dict[str, str] = {}
        self.failure: str | None = None
        self.manifest: Mapping[str, Any] = {}


_QUOTES = re.compile(r'["`\[\]]')


def relation_key(name: str) -> tuple[str, ...]:
    """A compiled relation name as lowercase parts, quoting removed."""

    return tuple(
        part.strip().lower() for part in _QUOTES.sub("", name).split(".") if part
    )


def _label(node: Mapping[str, Any]) -> str:
    """A model's name, with its version as ``name.vN`` where it has one."""

    label = str(node.get("name"))
    if node.get("version") is not None:
        label = f"{label}.v{node['version']}"
    return label


def project_name(view: DbtProjectView) -> str:
    """This project's name: the view's, else ``dbt_project.yml``'s, else empty."""

    import yaml

    if view.project_name:
        return str(view.project_name)
    source = view.files.get("dbt_project.yml")
    try:
        parsed = yaml.safe_load(source.content) if source is not None else None
    except yaml.YAMLError:
        return ""
    name = parsed.get("name") if isinstance(parsed, dict) else None
    return str(name) if name else ""


def compile_branch(
    engine: DexEngine,
    stored: Any,
    project: Path,
    view: DbtProjectView,
    connector: str | None,
    *,
    vars_: Mapping[str, Any] | None,
    proposed: bool,
    select: str | None = None,
) -> Branch:
    """Compile every model this project owns, in a throwaway copy.

    Nothing is made ephemeral: a star over another model resolves through the
    compiled columns of that model in this same branch (:class:`Catalog`), so
    the whole branch is read as one tree. ``proposed`` writes the plan's edits
    into the copy first; otherwise the tree is compiled as it stands.
    ``select`` narrows the compile (a retirement compiles the models it judges
    and their lineage); by default every model this project owns.
    """

    from .refactor_compare import _shadow

    branch = Branch()
    target = engine.config.dbt_target or "dev"
    name = project_name(view)
    selector = select or (
        f"package:{name},resource_type:model" if name else "resource_type:model"
    )
    try:
        with _shadow(engine, project, target, connector or "") as shadow:
            shadow.strip_run_hooks()
            if proposed:
                for edit in stored.edits:
                    if edit.op is EditOp.DELETE:
                        shadow.remove(edit.path)
                    else:
                        shadow.write(edit.path, edit.new_content or "")
            manifest, isolated = shadow.compile_isolating(selector, vars_=vars_)
    except Exception as exc:
        # dbt absent, no dev profile, or this branch does not parse: the
        # caller tells the cases apart by which branches failed.
        text = str(exc).strip()
        branch.failure = text.splitlines()[0][:300] if text else type(exc).__name__
        return branch
    if not name:
        name = str((manifest.get("metadata") or {}).get("project_name") or "")
    read_manifest(branch, manifest, name, isolated)
    return branch


def read_manifest(
    branch: Branch,
    manifest: Mapping[str, Any],
    project: str,
    isolated: Mapping[str, str] | None = None,
) -> None:
    """Fill ``branch`` with this project's compiled, failed and disabled models.

    ``isolated`` is ``{original_file_path: reason}`` for the nodes the compile
    set aside; a project model on one of those files is ``failed``, not a model
    with empty SQL.
    """

    isolated = dict(isolated or {})
    for uid, node in (manifest.get("nodes") or {}).items():
        if not uid.startswith("model.") or node.get("package_name") != project:
            continue
        label = _label(node)
        path = str(node.get("original_file_path") or "")
        if path in isolated:
            branch.failed[label] = isolated[path]
            branch.failed_paths[label] = path
            continue
        depends = node.get("depends_on") or {}
        branch.models[label] = Model(
            (node.get("compiled_code") or "").strip().rstrip(";"),
            node.get("relation_name"),
            str(node.get("package_name")),
            path,
            macros=tuple(depends.get("macros") or ()),
            parents=tuple(depends.get("nodes") or ()),
        )
    for uid, entries in (manifest.get("disabled") or {}).items():
        if uid.startswith(f"model.{project}."):
            for node in entries if isinstance(entries, list) else [entries]:
                branch.disabled[_label(node)] = str(
                    node.get("original_file_path") or ""
                )
    branch.manifest = manifest


def macro_closure(macros: Mapping[str, Any], uids: Any) -> set[str]:
    """``uids`` and every macro they call, directly or through another macro."""

    seen: set[str] = set()
    pending = [str(uid) for uid in uids]
    while pending:
        uid = pending.pop()
        if uid in seen:
            continue
        seen.add(uid)
        depends = ((macros.get(uid) or {}).get("depends_on") or {}).get("macros")
        pending.extend(str(child) for child in depends or ())
    return seen


def macro_callers(branch: Branch, project: str) -> dict[str, list[str]]:
    """``{macro file: [model, ...]}`` for this project's macros, transitively.

    A model calls a macro when its compiled node depends on it, or on a macro
    that does. Read from the manifest dbt wrote for this branch, so it is the
    call graph dbt resolved, dispatch included, not a search of macro text.
    """

    macros = branch.manifest.get("macros") or {}
    prefix = f"macro.{project}."
    out: dict[str, set[str]] = {}
    for label, model in branch.models.items():
        for uid in model.macros:
            for reached in macro_closure(macros, [str(uid)]):
                if not reached.startswith(prefix):
                    continue
                path = str((macros.get(reached) or {}).get("original_file_path") or "")
                if path:
                    out.setdefault(path, set()).add(label)
    return {path: sorted(labels) for path, labels in sorted(out.items())}


class Catalog:
    """The columns a star over a compiled relation reads.

    A project model is read from its own compiled SQL in the same branch; a
    seed from its CSV header in ``view`` (the tree the branch compiled, so a
    proposed branch reads the plan's seed); anything else (a source, a
    snapshot, a package model) from the exploration cache. A cycle or an
    unknown relation is a reason, never a guess.
    """

    def __init__(
        self,
        branch: Branch,
        view: DbtProjectView,
        dialect: str | None,
        cache: Callable[[], Any],
    ) -> None:
        """Index every relation the branch's manifest names by its parts."""

        self.branch = branch
        self.view = view
        self.dialect = dialect
        self.cache = cache
        self.by_relation: dict[tuple[str, ...], tuple[str, str]] = {}
        manifest = branch.manifest
        for label, model in branch.models.items():
            if model.relation:
                self.by_relation[relation_key(model.relation)] = ("model", label)
        for uid, node in (manifest.get("nodes") or {}).items():
            if node.get("relation_name") and uid.startswith("seed."):
                self.by_relation[relation_key(node["relation_name"])] = (
                    "seed",
                    str(node.get("original_file_path") or ""),
                )
            elif (
                node.get("relation_name")
                and uid.startswith("model.")
                and _label(node) in branch.failed
            ):
                self.by_relation[relation_key(node["relation_name"])] = (
                    "failed",
                    _label(node),
                )
        for node in (manifest.get("sources") or {}).values():
            if node.get("relation_name"):
                self.by_relation[relation_key(node["relation_name"])] = (
                    "cached",
                    f"{node.get('schema')}."
                    f"{node.get('identifier') or node.get('name')}",
                )

    def resolve(self, label: str, stack: frozenset[str] = frozenset()) -> None:
        """Read ``label``'s output columns, following stars through ``_columns``."""

        from .select_list import resolve_select_list

        model = self.branch.models[label]
        if model.columns is not None or model.reason is not None:
            return
        if not model.sql:
            model.reason = "dbt wrote no compiled SQL"
            return
        read = resolve_select_list(
            model.sql,
            dialect=self.dialect,
            relation_columns=lambda relation: self._columns(relation, stack | {label}),
        )
        if read.columns is not None:
            model.columns = sorted(c.lower() for c in read.columns)
        else:
            model.reason = read.reason or "the SELECT list could not be read"

    def _lookup(self, parts: tuple[str, ...]) -> tuple[str, str] | None:
        """The model, seed or cached relation a compiled name refers to, if unique."""

        key = tuple(p.lower() for p in parts)
        if key in self.by_relation:
            return self.by_relation[key]
        found = [
            value
            for stored, value in self.by_relation.items()
            if stored[-len(key) :] == key or key[-len(stored) :] == stored
        ]
        return found[0] if len(found) == 1 else None

    def _columns(self, relation: Any, stack: frozenset[str]) -> Any:
        """The columns a star over ``relation`` reads, or why they are unknown."""

        from .select_list import RelationColumns

        found = self._lookup(tuple(relation.parts))
        if found is None:
            return self._cached(".".join(relation.parts[-2:]), str(relation))
        kind, where = found
        if kind == "failed":
            return RelationColumns(
                reason=f"a `*` over {where}, which dbt could not compile "
                f"({self.branch.failed[where]})"
            )
        if kind == "model":
            if where in stack:
                return RelationColumns(reason=f"a cycle through {where}")
            self.resolve(where, stack)
            model = self.branch.models[where]
            if model.columns is None:
                return RelationColumns(
                    reason=f"a `*` over {where}, whose columns could not be read "
                    f"({model.reason})"
                )
            return RelationColumns(list(model.columns))
        if kind == "seed":
            source = self.view.files.get(where)
            header = source.content.splitlines()[0] if source and source.content else ""
            if header:
                return RelationColumns(
                    [c.strip().strip('"').lower() for c in header.split(",")]
                )
            return RelationColumns(reason=f"a `*` over seed {where} with no header")
        return self._cached(where, str(relation))

    def _cached(self, name: str, shown: str) -> Any:
        """A relation's columns from the cache, or a reason naming the profile."""

        from .drafting import dataset_for
        from .select_list import RelationColumns

        try:
            cache = self.cache()
        except Exception:
            cache = None
        dataset = dataset_for(cache, name)
        if dataset is None or not dataset.columns:
            return RelationColumns(
                reason=(
                    f"a `*` over {shown} with no single cached profile; "
                    f"`dex explore profile {name}` would supply one"
                )
            )
        return RelationColumns(
            [c.name.lower() for c in dataset.columns], read_from=[dataset.identifier]
        )


def identifiers(sql: str, dialect: str | None) -> set[str] | None:
    """Every column a compiled statement names, lowercased; ``None`` if unread."""

    import sqlglot
    from sqlglot import expressions as exp

    if not sql:
        return None
    try:
        parsed = sqlglot.parse_one(sql, read=dialect or "duckdb")
    except Exception:
        return None
    if parsed is None:
        return None
    return {column.name.lower() for column in parsed.find_all(exp.Column)}
