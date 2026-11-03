"""Reading the configured Cube model files into a validated set.

Never raises for a bad file. Every problem is a :class:`Diagnostic` naming the
file and the element, and a refused element is left out of the set rather than
read best-effort:

1. **Confinement.** Each configured path resolves inside the repository,
   symlinks included, and is a file.
2. **Compiled-only.** A ``.js`` file, and a YAML file carrying Jinja (``{{``,
   ``{%`` or ``{#``), is named and not parsed: Cube renders it before it
   compiles, and a static read of the template is a read of something Cube never
   sees. A cube or view using ``extends`` is named the same way, element by
   element, because what it declares is the parent's definition merged by the
   compiler.
3. **Each element**, by :mod:`.validator`.
4. **The whole set**: names unique across cubes and views (Cube keeps both in
   one namespace), every join naming a cube in the set, every ``{reference}``
   resolving, and every view ``join_path`` walking declared joins to members
   that exist. A refusal can retract an element others depend on, so the set is
   checked again until nothing more is retracted.

A reference to, or a join naming, something the set does not hold is a warning
rather than a refusal while a compiled-only file is configured: the cube it names
may be one that file defines.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..semantic_catalog import CompiledOnly
from . import validator
from .model import Cube, Member, View, ViewMember, ViewSource
from .references import resolve, scan

__all__ = [
    "ERROR",
    "EXTENDS_REASON",
    "JINJA_REASON",
    "JS_REASON",
    "WARNING",
    "Diagnostic",
    "LoadResult",
    "is_template",
    "load_files",
    "validate_model_contents",
]

ERROR = validator.ERROR
WARNING = validator.WARNING

#: What marks a YAML file as a Jinja template Cube renders before it compiles.
_JINJA_MARKERS = ("{{", "{%", "{#")

#: Why each kind of compiled-only definition is not read. Shared with authoring
#: (:mod:`.authoring`), which refuses to write one for the same reason the
#: catalog names it under ``compiled_only``, so the two always agree.
JS_REASON = "a JavaScript model needs Node to compile"
JINJA_REASON = "a Jinja template Cube renders before compiling"
EXTENDS_REASON = "`extends` is resolved by Cube's compiler"

#: Top-level keys of a Cube YAML file (`YamlCompiler.transpileYamlFile`).
_TOP_LEVEL = ("cubes", "views", "view_groups")


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with a model file.

    ``element`` names where (``cubes['orders'].dimensions['status']``),
    ``(root)`` for the file as a whole, and ``(config)`` for the coordinate that
    named it. ``rule`` says which check spoke, so a caller can act on a class of
    finding without matching on prose.
    """

    file: str
    element: str
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        return f"{self.file}: {self.element}: [{self.rule}] {self.message}"


@dataclass
class LoadResult:
    """Every element dex could read, every one it cannot, and why."""

    cubes: list[Cube] = field(default_factory=list)
    views: list[View] = field(default_factory=list)
    compiled_only: list[CompiledOnly] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == ERROR]

    @property
    def empty(self) -> bool:
        return not self.cubes and not self.views

    def notes(self) -> list[str]:
        """Every diagnostic and every compiled-only definition, as a line."""

        lines = [d.render() for d in self.diagnostics]
        lines.extend(_compiled_only_note(item) for item in self.compiled_only)
        return lines

    def cube(self, name: str) -> Cube | None:
        return next((c for c in self.cubes if c.name == name), None)


def _compiled_only_note(item: CompiledOnly) -> str:
    where = f"{item.file}: {item.element}" if item.element else item.file
    return (
        f"{where}: compiled-only ({item.reason}). Only Cube's compiler can read "
        "it, so dex names it and translates nothing out of it"
    )


def load_files(repo_root: Path | str, files: Sequence[str]) -> LoadResult:
    """Read, validate and return every configured file, named as configured."""

    root = Path(repo_root).resolve()
    contents: dict[str, str] = {}
    result = LoadResult()
    for name in files:
        path, refusal = _resolve(root, name)
        if path is None:
            if refusal is not None:
                result.diagnostics.append(refusal)
            continue
        try:
            contents[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.diagnostics.append(
                _diagnostic(name, "(root)", "unreadable", f"could not read it: {exc}")
            )
    return _load(contents, result)


def validate_model_contents(contents: Mapping[str, str]) -> LoadResult:
    """Judge files given as text, keyed as ``semantic.cube.files`` names them.

    Every check :func:`load_files` runs, over content that need not exist on
    disk, so authoring can validate the set a plan would leave.
    """

    return _load(dict(contents), LoadResult())


def is_template(text: str) -> bool:
    """Whether ``text`` carries Jinja, which Cube renders before it compiles."""

    return any(marker in text for marker in _JINJA_MARKERS)


def _diagnostic(
    file: str, element: str, rule: str, message: str, severity: str = ERROR
) -> Diagnostic:
    return Diagnostic(
        file=file, element=element, rule=rule, severity=severity, message=message
    )


def _resolve(root: Path, name: str) -> tuple[Path | None, Diagnostic | None]:
    """The configured name as a file inside the repository, or a refusal.

    Checked on the resolved path, so a symlink pointing out of the repository is
    caught along with a ``..``.
    """

    if PurePosixPath(name).is_absolute():
        return None, _diagnostic(
            name,
            "(config)",
            "confinement",
            "a Cube model file is named relative to the repository root, and this "
            "is an absolute path",
        )
    resolved = (root / name).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, _diagnostic(
            name,
            "(config)",
            "confinement",
            f"resolves outside the repository ({resolved}); dex reads Cube model "
            "files only from inside it",
        )
    if not resolved.is_file():
        return None, _diagnostic(name, "(config)", "missing", "no such file")
    return resolved, None


# --- one file ------------------------------------------------------------------


def _load(contents: dict[str, str], result: LoadResult) -> LoadResult:
    cubes: list[Cube] = []
    views: list[View] = []
    refused: set[str] = set()
    for name, text in contents.items():
        found_cubes, found_views = _read_file(name, text, result, refused)
        cubes.extend(found_cubes)
        views.extend(found_views)
    return _across_set(cubes, views, result, refused)


def _read_file(
    name: str, text: str, result: LoadResult, refused: set[str]
) -> tuple[list[Cube], list[View]]:
    """One file's cubes and views. A refused element's name joins ``refused``,
    so a reference to it is reported as unreadable rather than unknown."""

    if name.endswith(".js"):
        result.compiled_only.append(CompiledOnly(file=name, reason=JS_REASON))
        return [], []
    if is_template(text):
        result.compiled_only.append(CompiledOnly(file=name, reason=JINJA_REASON))
        return [], []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        result.diagnostics.append(
            _diagnostic(name, "(root)", "malformed", f"not valid YAML: {exc}")
        )
        return [], []
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "root",
                "a Cube model file is a mapping of `cubes` and `views`, and this "
                f"parsed as {type(raw).__name__}",
            )
        )
        return [], []
    for key in raw:
        if key not in _TOP_LEVEL:
            result.diagnostics.append(
                _diagnostic(
                    name,
                    "(root)",
                    "top_level_key",
                    f"unexpected key `{key}`: a Cube model file holds only cubes, "
                    "views and view_groups",
                )
            )
    if raw.get("view_groups"):
        result.diagnostics.append(
            _diagnostic(
                name,
                "view_groups",
                "not_mapped",
                "view groups organize views for Cube's own listings and have no "
                "home in dex's catalog, so they are not read",
                severity=WARNING,
            )
        )

    cubes: list[Cube] = []
    views: list[View] = []
    sections: tuple[tuple[str, Any, list[Any]], ...] = (
        ("cubes", validator.check_cube, cubes),
        ("views", validator.check_view, views),
    )
    for key, check, into in sections:
        listed = raw.get(key)
        if listed is None:
            continue
        if not isinstance(listed, list):
            result.diagnostics.append(
                _diagnostic(name, key, "type", f"`{key}` must be defined as a list")
            )
            continue
        for entry in listed:
            if isinstance(entry, Mapping) and entry.get("extends") is not None:
                result.compiled_only.append(
                    CompiledOnly(
                        file=name,
                        element=f"{key}['{entry.get('name')}']",
                        reason=EXTENDS_REASON,
                    )
                )
                continue
            if not isinstance(entry, Mapping):
                result.diagnostics.append(
                    _diagnostic(name, key, "type", f"each of `{key}` is a mapping")
                )
                continue
            element, findings = check(entry, name)
            result.diagnostics.extend(
                _diagnostic(name, f.element, f.rule, f.message, f.severity)
                for f in findings
            )
            if element is not None:
                into.append(element)
            elif isinstance(entry.get("name"), str):
                refused.add(entry["name"])
    return cubes, views


# --- the whole set -------------------------------------------------------------


def _across_set(
    cubes: list[Cube], views: list[View], result: LoadResult, refused: set[str]
) -> LoadResult:
    cubes, views = _unique_names(cubes, views, result)
    # A cube or view a compiled-only file or element defines is one dex cannot
    # see, so naming something absent is only certainly wrong without them.
    hidden = bool(result.compiled_only)
    unknown_severity = WARNING if hidden else ERROR
    hint = (
        "; it may be defined in a compiled-only file or element, which dex does "
        "not read"
        if hidden
        else ""
    )

    while True:
        by_name = {cube.name: cube for cube in cubes}
        retract: set[str] = set()
        for cube in cubes:
            for diagnostic in _cube_problems(
                cube, by_name, refused, unknown_severity, hint
            ):
                if diagnostic not in result.diagnostics:
                    result.diagnostics.append(diagnostic)
                if diagnostic.severity == ERROR:
                    retract.add(cube.name)
        if not retract:
            break
        refused |= retract
        cubes = [cube for cube in cubes if cube.name not in retract]

    by_name = {cube.name: cube for cube in cubes}
    kept_views: list[View] = []
    for view in views:
        problems = _resolve_view(view, by_name, refused, unknown_severity, hint)
        result.diagnostics.extend(problems)
        if not any(p.severity == ERROR for p in problems):
            kept_views.append(view)
    result.cubes = cubes
    result.views = kept_views
    return result


def _unique_names(
    cubes: list[Cube], views: list[View], result: LoadResult
) -> tuple[list[Cube], list[View]]:
    """Retract every cube and view whose name is declared more than once.

    Every declaration, not the second: which one Cube would keep depends on file
    order, and a catalog reading either would be answering for a model the
    author did not decide on.
    """

    declared: dict[str, list[str]] = {}
    everything: list[Cube | View] = [*cubes, *views]
    for element in everything:
        declared.setdefault(element.name, []).append(element.file)
    duplicated = {name for name, files in declared.items() if len(files) > 1}
    for name in sorted(duplicated):
        kinds = {
            "cube" if isinstance(e, Cube) else "view"
            for e in everything
            if e.name == name
        }
        for element in everything:
            if element.name != name:
                continue
            kind = "cubes" if isinstance(element, Cube) else "views"
            others = sorted(set(declared[name]))
            result.diagnostics.append(
                _diagnostic(
                    element.file,
                    f"{kind}['{name}']",
                    "duplicate",
                    f"'{name}' is declared {len(declared[name])} times "
                    f"({' and '.join(sorted(kinds))}, in {', '.join(others)}); "
                    "cubes and views share one namespace, so none of them is read",
                )
            )
    return (
        [c for c in cubes if c.name not in duplicated],
        [v for v in views if v.name not in duplicated],
    )


def _cube_problems(
    cube: Cube,
    cubes: Mapping[str, Cube],
    refused: set[str],
    unknown_severity: str,
    hint: str,
) -> list[Diagnostic]:
    """Joins and references that do not resolve within the set."""

    problems: list[Diagnostic] = []
    element = f"cubes['{cube.name}']"
    pairs: dict[tuple[str, str], int] = {}
    for join in cube.joins:
        at = f"{element}.joins['{join.reached_as}']"
        if join.target not in cubes:
            if join.target in refused:
                problems.append(
                    _diagnostic(
                        cube.file,
                        at,
                        "join_target",
                        f"joins '{join.target}', which could not be read (see its "
                        "own diagnostics)",
                    )
                )
            else:
                problems.append(
                    _diagnostic(
                        cube.file,
                        at,
                        "join_target",
                        f"joins '{join.target}', which is not a cube in the "
                        f"configured files{hint}",
                        severity=unknown_severity,
                    )
                )
            continue
        key = (cube.name, join.reached_as)
        pairs[key] = pairs.get(key, 0) + 1
        problems.extend(_reference_problems(cube, at, join.sql, cubes, refused, hint))
    for (_, reached), count in pairs.items():
        if count > 1:
            problems.append(
                _diagnostic(
                    cube.file,
                    f"{element}.joins['{reached}']",
                    "duplicate_join",
                    f"joins '{reached}' {count} times; Cube allows one join "
                    "between a pair of cubes (give a second one an `alias`)",
                )
            )
    for member in (*cube.dimensions, *cube.measures, *cube.segments):
        if member.sql is None:
            continue
        at = f"{element}.{member.kind}s['{member.name}']"
        problems.extend(_reference_problems(cube, at, member.sql, cubes, refused, hint))
        for predicate in member.filters:
            problems.extend(
                _reference_problems(cube, at, predicate, cubes, refused, hint)
            )
    return problems


def _reference_problems(
    cube: Cube,
    at: str,
    sql: str,
    cubes: Mapping[str, Cube],
    refused: set[str],
    hint: str,
) -> list[Diagnostic]:
    holes, problem = scan(sql)
    if problem:
        return [_diagnostic(cube.file, at, "reference", f"its sql has {problem}")]
    problems: list[Diagnostic] = []
    for hole in holes:
        resolved, why = resolve(hole, cube, cubes)
        if resolved is not None:
            continue
        head = (hole.path or (hole.head or "",))[0]
        if head in refused:
            message = (
                f"`{{{hole.text}}}` names '{head}', which could not be read (see "
                "its own diagnostics)"
            )
            severity = ERROR
        else:
            message = f"{why}{hint}"
            severity = WARNING if hint and hole.path else ERROR
        problems.append(
            _diagnostic(cube.file, at, "unresolved_reference", message, severity)
        )
    return problems


def _resolve_view(
    view: View,
    cubes: Mapping[str, Cube],
    refused: set[str],
    unknown_severity: str,
    hint: str,
) -> list[Diagnostic]:
    """Resolve a view's members from its join paths, or say why not."""

    problems: list[Diagnostic] = []
    element = f"views['{view.name}']"
    members: list[ViewMember] = []
    leaves: dict[str, dict[str, set[str]]] = {}
    for source in view.sources:
        at = f"{element}.cubes['{'.'.join(source.join_path)}']"
        target, reached, problem = _walk(source, cubes, refused, unknown_severity, hint)
        if problem is not None:
            problems.append(_diagnostic(view.file, at, *problem))
            continue
        if target is None:  # _walk returns a cube or a problem, never neither
            continue
        root = source.join_path[0]
        for depth in range(1, len(reached) + 1):
            leaves.setdefault(root, {}).setdefault(reached[depth - 1], set()).add(
                ".".join(source.join_path[:depth])
            )
        if source.split:
            problems.append(
                _diagnostic(
                    view.file,
                    at,
                    "not_mapped",
                    "a `split` source becomes a view of its own in Cube, which dex "
                    "does not read yet, so its members are not listed",
                    severity=WARNING,
                )
            )
            continue
        found, source_problems = _view_members(view, source, target, at)
        problems.extend(source_problems)
        members.extend(found)
    for root, by_cube in leaves.items():
        for reached_cube, paths in by_cube.items():
            if len(paths) > 1:
                problems.append(
                    _diagnostic(
                        view.file,
                        element,
                        "join_paths",
                        f"reaches '{reached_cube}' by more than one path from "
                        f"'{root}' ({', '.join(sorted(paths))}); Cube allows one "
                        "path to a cube within a root",
                    )
                )
    names: dict[str, int] = {}
    for member in members:
        names[member.name] = names.get(member.name, 0) + 1
    for name, count in names.items():
        if count > 1:
            problems.append(
                _diagnostic(
                    view.file,
                    element,
                    "duplicate",
                    f"exposes '{name}' {count} times; give one an alias or set "
                    "`prefix: true`",
                )
            )
    view.members = members
    return problems


def _walk(
    source: ViewSource,
    cubes: Mapping[str, Cube],
    refused: set[str],
    unknown_severity: str,
    hint: str,
) -> tuple[Cube | None, list[str], tuple[str, str, str] | None]:
    # The third item is `(rule, message, severity)`, the order `_diagnostic`
    # takes them in after the file and element.
    """The cube a join path reaches, the cubes along it, or why it reaches none.

    Each hop is a join the previous cube declares (by alias, then by target),
    because Cube walks a view's path through declared joins and in their
    direction only.
    """

    root = source.join_path[0]
    cube = cubes.get(root)
    if cube is None:
        if root in refused:
            return (
                None,
                [],
                (
                    "join_path",
                    f"starts at '{root}', which could not be read",
                    ERROR,
                ),
            )
        return (
            None,
            [],
            (
                "join_path",
                f"starts at '{root}', which is not a cube in the configured "
                f"files{hint}",
                unknown_severity,
            ),
        )
    reached = [cube.name]
    for segment in source.join_path[1:]:
        join = cube.join_to(segment)
        if join is None:
            return (
                None,
                [],
                (
                    "join_path",
                    f"'{cube.name}' declares no join to '{segment}', and a view's "
                    "join_path follows declared joins only",
                    ERROR,
                ),
            )
        nxt = cubes.get(join.target)
        if nxt is None:
            return (
                None,
                [],
                (
                    "join_path",
                    f"walks to '{join.target}', which is not a cube in the configured "
                    f"files{hint}",
                    unknown_severity,
                ),
            )
        cube = nxt
        reached.append(cube.name)
    return cube, reached, None


def _view_members(
    view: View, source: ViewSource, target: Cube, at: str
) -> tuple[list[ViewMember], list[Diagnostic]]:
    problems: list[Diagnostic] = []
    reached_as = source.alias or source.join_path[-1]

    def named(name: str) -> str:
        return f"{reached_as}_{name}" if source.prefix else name

    exposed: list[ViewMember] = []
    chosen: list[tuple[Member, str, str | None, str | None]]
    if source.everything:
        chosen = [
            (member, named(member.name), None, None)
            for member in (*target.dimensions, *target.measures, *target.segments)
        ]
    else:
        chosen = []
        for include in source.includes or ():
            member = target.member(include.name)
            if member is None:
                problems.append(
                    _diagnostic(
                        view.file,
                        at,
                        "include",
                        f"includes '{include.name}', which '{target.name}' does "
                        "not declare",
                    )
                )
                continue
            chosen.append(
                (
                    member,
                    named(include.alias or include.name),
                    include.title,
                    include.description,
                )
            )
    excluded = set(source.excludes)
    problems.extend(
        _diagnostic(
            view.file,
            at,
            "exclude",
            f"excludes '{name}', which '{target.name}' does not declare",
            severity=WARNING,
        )
        for name in sorted(excluded)
        if target.member(name) is None
    )
    for member, name, title, description in chosen:
        if member.name in excluded:
            continue
        exposed.append(
            ViewMember(
                name=name,
                kind=member.kind,
                cube=target.name,
                member=member,
                join_path=source.join_path,
                title=title,
                description=description,
            )
        )
    return exposed, problems
