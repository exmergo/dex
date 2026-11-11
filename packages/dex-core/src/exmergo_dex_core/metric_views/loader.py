"""Reading the configured Metric View files into a validated set.

Never raises for a bad file. Every problem is a :class:`Diagnostic` naming the
file and the element, and a refused element is left out of the set rather than
read best-effort:

1. **Confinement.** Each configured path resolves inside the repository,
   symlinks included, and is a file.
2. **YAML as Databricks reads it.** YAML 1.2 booleans, so a join's bare ``on:``
   key stays the text ``on`` rather than becoming ``True`` as YAML 1.1 has it.
   A key written twice in one mapping is refused, since which one a reader keeps
   is not something dex will guess, and so is a key that is not text: ``true:``
   is what a YAML 1.1 writer leaves of an ``on:``.
3. **One view per file**, by :mod:`.validator`, named after the file's stem.
4. **The expressions**, through :mod:`.lineage`, held to what Databricks
   resolves (checked against ``CREATE VIEW ... WITH METRICS``):

   - every ``MEASURE()`` names a measure defined before it;
   - no field reads a ``one_to_many`` join;
   - a nested join is read through its whole path, never by its own name alone.

   A field reading a later field's name, or a ``filter`` reading a field's name,
   warns: Databricks reads such a name as a source column, which only the
   warehouse can say exists. A refused measure retracts the measures that read
   it, to a fixed point.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..yaml_items import IndentedDumper
from . import validator
from .model import MetricView

__all__ = [
    "ERROR",
    "WARNING",
    "Diagnostic",
    "LoadResult",
    "dump_yaml",
    "load_files",
    "read_yaml",
    "validate_view_contents",
]

ERROR = validator.ERROR
WARNING = validator.WARNING


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with a Metric View file.

    ``element`` names where (``measures['revenue']``), ``(root)`` for the file
    as a whole, and ``(config)`` for the coordinate that named it. ``rule`` says
    which check spoke, so a caller can act on a class of finding without
    matching on prose.
    """

    file: str
    element: str
    rule: str
    severity: str
    message: str

    def render(self) -> str:
        """The diagnostic as one line: file, element, rule and message."""
        return f"{self.file}: {self.element}: [{self.rule}] {self.message}"


@dataclass
class LoadResult:
    """Every view dex could read, and every diagnostic about the rest."""

    views: list[MetricView] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    #: The ``version`` each file declares, as text, keyed as configured: read
    #: for every file that parses as a mapping, a refused one included, so the
    #: fingerprint can say a view moved to a spec version dex does not read
    #: rather than only that the view disappeared (#1259).
    declared_versions: dict[str, str] = field(default_factory=dict)

    @property
    def errors(self) -> list[Diagnostic]:
        """The diagnostics that refused something."""
        return [d for d in self.diagnostics if d.severity == ERROR]

    @property
    def empty(self) -> bool:
        """Whether no view could be read."""
        return not self.views

    def notes(self) -> list[str]:
        """Every diagnostic, as a line for a catalog's or envelope's notes."""
        return [d.render() for d in self.diagnostics]

    def view(self, name: str) -> MetricView | None:
        """The view named ``name``, or ``None``."""
        return next((v for v in self.views if v.name == name), None)


# --- YAML as Databricks reads it ----------------------------------------------------


class _Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2's booleans and no duplicate keys."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _mapping(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> Any:
    """Construct a mapping, refusing a duplicate key and a key that is not text."""
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            hint = (
                ". A YAML 1.1 reader or writer (PyYAML's default, for one) turns a "
                "bare `on:` into `true:`; write the join condition's key as `on:` "
                "again"
                if key is True
                else ""
            )
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"the key `{key}` is not text, and every Metric View key is{hint}",
                key_node.start_mark,
            )
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"the key `{key}` is written twice in one mapping",
                key_node.start_mark,
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(text: str) -> Any:
    """``text`` parsed the way a Metric View body is: YAML 1.2 booleans, and a
    duplicate key refused (``yaml.YAMLError``)."""

    return yaml.load(text, Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


class Dumper(IndentedDumper):
    """The writer's half of :class:`_Loader`: YAML 1.2 booleans, block lists
    indented under their key.

    PyYAML's own dumper quotes any text a YAML 1.1 reader would take for a
    boolean (``'on'``), and a 1.1 round trip writes a parsed ``True`` key as
    ``true:``. With 1.2's resolvers a join's ``on`` key is text and is written
    plain, as Databricks' own examples write it, and reads back through
    :func:`read_yaml` as the same text.
    """


Dumper.yaml_implicit_resolvers = dict(_Loader.yaml_implicit_resolvers)


def dump_yaml(value: Any) -> str:
    """``value`` as a Metric View body: block style, keys in the order given,
    YAML 1.2 scalars, so it reads back through :func:`read_yaml` unchanged."""

    return yaml.dump(value, Dumper=Dumper, sort_keys=False, allow_unicode=True)


# --- the files ---------------------------------------------------------------------


def load_files(repo_root: Path | str, files: Sequence[str]) -> LoadResult:
    """Read, validate and return every configured file, named as configured."""

    root = Path(repo_root).resolve()
    contents: dict[str, str] = {}
    result = LoadResult()
    for name in files:
        path, refusal = _resolve(root, name)
        if path is None:
            result.diagnostics.append(refusal)  # type: ignore[arg-type]
            continue
        try:
            contents[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.diagnostics.append(
                _diagnostic(name, "(root)", "unreadable", f"could not read it: {exc}")
            )
    return _load(contents, result)


def validate_view_contents(contents: Mapping[str, str]) -> LoadResult:
    """Judge files given as text, keyed as ``semantic.databricks.files`` names
    them: every check :func:`load_files` runs, over content not on disk."""

    return _load(dict(contents), LoadResult())


def _diagnostic(
    file: str, element: str, rule: str, message: str, severity: str = ERROR
) -> Diagnostic:
    """One :class:`Diagnostic`, an error unless ``severity`` says otherwise."""
    return Diagnostic(
        file=file, element=element, rule=rule, severity=severity, message=message
    )


def _resolve(root: Path, name: str) -> tuple[Path | None, Diagnostic | None]:
    """The configured name as a file inside the repository, or a refusal.
    Checked on the resolved path, so a symlink out of the repository is caught."""

    if PurePosixPath(name).is_absolute():
        return None, _diagnostic(
            name,
            "(config)",
            "confinement",
            "a Metric View file is named relative to the repository root, and "
            "this is an absolute path",
        )
    resolved = (root / name).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, _diagnostic(
            name,
            "(config)",
            "confinement",
            f"resolves outside the repository ({resolved}); dex reads Metric View "
            "files only from inside it",
        )
    if not resolved.is_file():
        return None, _diagnostic(name, "(config)", "missing", "no such file")
    return resolved, None


def _load(contents: dict[str, str], result: LoadResult) -> LoadResult:
    """Read and validate every file given as text, into ``result``."""
    for name, text in contents.items():
        view = _read_file(name, text, result)
        if view is not None:
            result.views.append(_expressions(view, result))
    return result


def _read_file(name: str, text: str, result: LoadResult) -> MetricView | None:
    """One file's view, or ``None`` with the diagnostics that say why."""
    try:
        raw = read_yaml(text)
    except yaml.YAMLError as exc:
        result.diagnostics.append(
            _diagnostic(name, "(root)", "malformed", f"not valid YAML: {exc}")
        )
        return None
    expanded = _expansion(raw)
    if expanded is None or expanded > MAX_EXPANDED_VALUES:
        reason = (
            "a YAML alias refers to the value that contains it (`&a [*a]`), so "
            "the definition has no end; write the value out"
            if expanded is None
            else f"its YAML aliases expand to more than {MAX_EXPANDED_VALUES} "
            "values, far past what a Metric View needs; write the values out"
        )
        result.diagnostics.append(_diagnostic(name, "(root)", "malformed", reason))
        return None
    if raw is None:
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "empty_file",
                "the file is empty, so it declares no view",
                WARNING,
            )
        )
        return None
    written = _version_text(text)
    if isinstance(raw, Mapping) and raw.get("version") is not None:
        result.declared_versions[name] = (
            written if written is not None else str(raw.get("version")).strip()
        )
    if (
        isinstance(raw, Mapping)
        and written is not None
        and written != validator.METRIC_VIEW_SPEC_VERSION
        and str(raw.get("version")).strip() == validator.METRIC_VIEW_SPEC_VERSION
    ):
        # `version: 1.10` parses as the number 1.1; the version is the text.
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "version",
                f"version {written} is not one dex reads: it reads Metric View "
                f"YAML {validator.METRIC_VIEW_SPEC_VERSION} only",
            )
        )
        return None
    stem = PurePosixPath(name).stem
    try:
        view, findings = validator.check_view(raw, name, stem)
    except (TypeError, ValueError, AttributeError) as exc:
        # A shape no rule anticipated is this file's problem, never the layer's:
        # the other files still read.
        result.diagnostics.append(
            _diagnostic(
                name,
                "(root)",
                "malformed",
                f"could not be read as a Metric View ({type(exc).__name__}: {exc})",
            )
        )
        return None
    for finding in findings:
        result.diagnostics.append(
            _diagnostic(
                name, finding.element, finding.rule, finding.message, finding.severity
            )
        )
    return view


def _version_text(text: str) -> str | None:
    """The root ``version`` scalar as written, before YAML reads it as a number
    (``1.10`` would read as ``1.1``), or None where there is none to read."""

    try:
        root = yaml.compose(text, Loader=_Loader)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return None
    for key, value in root.value:
        if key.value == "version" and isinstance(value, yaml.ScalarNode):
            return str(value.value).strip()
    return None


def _expressions(view: MetricView, result: LoadResult) -> MetricView:
    """The rules that need a view's expressions parsed. Imports lineage, the one
    sqlglot importer, here rather than at module level, so loading this module
    costs nothing on an install without the extra."""

    from . import lineage

    def refuse(element: str, rule: str, message: str) -> None:
        """Record one finding against this view, at the severity its rule carries."""
        result.diagnostics.append(
            _diagnostic(view.file, element, rule, message, validator.RULES[rule])
        )

    def nested_by_name(refs: list[Any]) -> str | None:
        """A qualifier naming a nested join by its own name rather than its
        path: Databricks reads `orders.customer.c_name`, never `customer.c_name`."""

        for ref in refs:
            if not ref.qualifier or view.join(ref.qualifier) is not None:
                continue
            nested = next(
                (
                    join
                    for join in view.all_joins()
                    if len(join.path) > 1
                    and join.name.lower() == ref.qualifier[0].lower()
                ),
                None,
            )
            if nested is not None:
                return (
                    f"reads `{'.'.join((*ref.qualifier, ref.column))}`, but "
                    f"`{nested.name}` is a nested join, read through its whole "
                    f"path: `{nested.dotted}.{ref.column}`"
                )
        return None

    field_names = [m.name.lower() for m in view.fields]
    if view.filter:
        filter_refs = lineage.column_refs(view.filter) or []
        named = sorted(
            {r.column for r in filter_refs if not r.qualifier}
            & {m.name for m in view.fields}
        )
        if named:
            refuse(
                "(root)",
                "filter_reads_field",
                f"`filter` reads `{named[0]}`, which is a field's name; a view's "
                "filter reads source columns only, so Databricks reads it as a "
                "source column of that name, and refuses the view if there is none",
            )

    one_to_many = {
        tuple(p.lower() for p in join.path)
        for join in view.all_joins()
        if join.cardinality == "one_to_many"
    }
    fields = []
    for member in view.fields:
        refs = lineage.column_refs(member.expr)
        if refs is None:
            refuse(
                f"{view.fields_key}['{member.name}']",
                "unparsed_expression",
                "its `expr` could not be parsed as Databricks SQL, so the column "
                "behind it is not resolved",
            )
            fields.append(member)
            continue
        if (problem := nested_by_name(refs)) is not None:
            refuse(f"{view.fields_key}['{member.name}']", "nested_join_path", problem)
            continue
        position = field_names.index(member.name.lower())
        later = sorted(
            {r.column for r in refs if not r.qualifier}
            & {m.name for m in view.fields[position + 1 :]}
        )
        if later:
            refuse(
                f"{view.fields_key}['{member.name}']",
                "forward_field",
                f"reads `{later[0]}`, a field defined after it. A field reads only "
                "the fields before it, so Databricks reads this as a source column "
                "of that name, and refuses the view if there is none",
            )
        reached = [
            ref
            for ref in refs
            if any(
                tuple(q.lower() for q in ref.qualifier[: len(path)]) == path
                for path in one_to_many
            )
        ]
        if reached:
            refuse(
                f"{view.fields_key}['{member.name}']",
                "one_to_many_field",
                f"reads `{'.'.join(reached[0].qualifier)}`, a one_to_many join, and "
                "a field cannot read one: only a measure can aggregate over it",
            )
            continue
        fields.append(member)

    measures = {m.name.lower(): m for m in view.measures}
    order = {m.name.lower(): i for i, m in enumerate(view.measures)}
    reads: dict[str, list[str]] = {}
    nested: set[str] = set()
    for member in view.measures:
        problem = nested_by_name(lineage.column_refs(member.expr) or [])
        if problem is not None:
            refuse(f"measures['{member.name}']", "nested_join_path", problem)
            nested.add(member.name.lower())
        names = lineage.measure_refs(member.expr)
        if names is None:
            refuse(
                f"measures['{member.name}']",
                "unparsed_expression",
                "its `expr` could not be parsed as Databricks SQL, so what it reads "
                "is not resolved",
            )
            names = []
        reads[member.name.lower()] = [n.lower() for n in names]
    refused: set[str] = set(nested)
    for name, targets in reads.items():
        if name in refused:
            continue
        unknown = [t for t in targets if t not in measures]
        later = [t for t in targets if t in order and order[t] >= order[name]]
        if unknown:
            refuse(
                f"measures['{measures[name].name}']",
                "unknown_measure",
                f"reads MEASURE({unknown[0]}), and the view has no measure of that "
                "name",
            )
            refused.add(name)
        elif later and later != [name]:
            refuse(
                f"measures['{measures[name].name}']",
                "forward_measure",
                f"reads MEASURE({measures[later[0]].name}), a measure defined after "
                "it; a measure reads only the measures before it",
            )
            refused.add(name)
    for name in reads:
        if name not in refused and _cycles(name, reads):
            refuse(
                f"measures['{measures[name].name}']",
                "measure_cycle",
                "reads itself through MEASURE(), directly or through other measures",
            )
            refused.add(name)
    changed = True
    while changed:
        changed = False
        for name, targets in reads.items():
            if name in refused:
                continue
            gone = [t for t in targets if t in refused]
            if gone:
                refuse(
                    f"measures['{measures[name].name}']",
                    "unknown_measure",
                    f"reads MEASURE({measures[gone[0]].name}), which is refused",
                )
                refused.add(name)
                changed = True
    return replace(
        view,
        fields=tuple(fields),
        measures=tuple(m for m in view.measures if m.name.lower() not in refused),
    )


#: How many values a file may expand to once its YAML aliases are followed. A
#: Metric View is a few hundred; past this a file is a "billion laughs" shape
#: that would stall every later read of it (the fingerprint serializes it whole).
MAX_EXPANDED_VALUES = 100_000


def _expansion(value: Any) -> int | None:
    """How many values ``value`` expands to with aliases followed, or ``None``
    where it contains itself (a recursive alias).

    Each container is visited once: a shared alias is sized once and its size
    reused, so the check is linear in the parsed document, not in its expansion.
    A size saturates at ``MAX_EXPANDED_VALUES + 1``, so a count never grows past
    what the caller compares it with, and cycles are still looked for after it
    saturates.
    """

    sizes: dict[int, int] = {}
    visiting: set[int] = set()

    def size(node: Any) -> int | None:
        if not isinstance(node, (dict, list)):
            return 1
        key = id(node)
        if key in sizes:
            return sizes[key]
        if key in visiting:
            return None
        visiting.add(key)
        total = 1
        for child in node.values() if isinstance(node, dict) else node:
            inner = size(child)
            if inner is None:
                return None
            total = min(total + inner, MAX_EXPANDED_VALUES + 1)
        visiting.discard(key)
        sizes[key] = total
        return total

    return size(value)


def _cycles(start: str, reads: Mapping[str, list[str]]) -> bool:
    """Whether ``start`` reaches itself through the measures it reads."""
    pending = list(reads.get(start, []))
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == start:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(reads.get(current, []))
    return False
