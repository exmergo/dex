"""Native dbt model versions, read from the repository alone (#859, #895).

A versioned model is one YAML entry with a ``versions:`` list and one file per
version. Everything here is computed from the project's files, with no compile,
so a plan can reason about the project it is about to write and every YAML
reader sees a version the way dbt will.

The rules are dbt's own (``dbt/contracts/graph/unparsed.py`` and
``dbt/parser/schemas.py``, 1.5 onwards), reproduced exactly because a reader
that disagrees with dbt about what a version holds is worse than one that does
not read versions at all:

- A version's file is its ``defined_in`` stem, else ``<name>_v<v>``. Only the
  latest version may fall back to the unsuffixed ``<name>``. A version with no
  file is only a *warning* to dbt, and the version is silently absent.
- The unversioned ``ref('<name>')`` resolves to ``latest_version``, or to the
  greatest ``v`` when none is declared (compared as numbers, else as text).
- A version's columns are the top-level ``columns`` its single
  ``include``/``exclude`` element admits (``include`` defaults to all), followed
  by the version's own column entries. Columns are keyed by name, so a version's
  entry replaces an inherited one of the same name wholesale.
- ``config`` is dbt's ``deep_merge`` of the top-level and the version's: mappings
  merge key by key, and a *list* written at both levels is concatenated, the
  version's items first.
- ``description`` is the version's, else the top-level one; tests are the
  version's ``data_tests``/``tests`` when it states any, else the top-level ones.
- Each version is the node ``model.<package>.<name>.v<v>``, labelled
  ``<name>.v<v>`` (:func:`~.dbt_project.manifest_node_label`).
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml

#: Where a version's file came from. ``missing`` is a version no file builds.
FILE_SOURCES = ("defined_in", "suffix", "unsuffixed_latest", "missing")

_INCLUDE_ALL = ("all", "*")
_MODEL_SUFFIXES = (".sql", ".py")


@dataclass(frozen=True)
class ModelVersion:
    """One item of a model's ``versions:`` list."""

    v: str
    label: str
    file: str | None
    file_source: str
    index: int
    item: dict[str, Any] = field(compare=False, repr=False)

    @property
    def stem(self) -> str | None:
        return PurePosixPath(self.file).stem if self.file else None


@dataclass
class VersionedModel:
    """A model whose YAML entry carries ``versions:``."""

    base: str
    yaml_path: str
    entry: dict[str, Any] = field(repr=False)
    latest_version: str | None
    default: str | None
    versions: list[ModelVersion]

    def version(self, v: str) -> ModelVersion | None:
        return next((item for item in self.versions if item.v == v), None)

    @property
    def default_label(self) -> str | None:
        return f"{self.base}.v{self.default}" if self.default is not None else None

    @property
    def labels(self) -> list[str]:
        return [item.label for item in self.versions]


@dataclass(frozen=True)
class VersionProblem:
    """A versioned model dbt would build wrong, or silently not at all."""

    code: str
    model: str
    detail: str
    path: str | None = None


def version_text(value: Any) -> str:
    """A ``v:`` value the way dbt spells it in ``v<v>``: ``2``, ``1.5``, ``beta``."""

    return str(value)


def _order_key(v: str) -> tuple[int, float, str]:
    try:
        return (0, float(v), v)
    except ValueError:
        return (1, 0.0, v)


def default_version(entry: Mapping[str, Any]) -> str | None:
    """The version an unversioned ``ref()`` resolves to, as dbt decides it."""

    items = [i for i in entry.get("versions") or [] if isinstance(i, dict) and "v" in i]
    if not items:
        return None
    latest = entry.get("latest_version")
    if latest is not None:
        return version_text(latest)
    # dbt: numeric order only when every identifier is numeric; otherwise
    # alphabetical (so mixed ``2`` / ``1-beta`` picks ``2``, not ``1-beta``).
    versions = [version_text(i["v"]) for i in items]
    if all(_order_key(v)[0] == 0 for v in versions):
        return max(versions, key=_order_key)
    return max(versions)


# --- reading the files ------------------------------------------------------------


def _yaml_documents(files: Mapping[str, str]) -> Iterable[tuple[str, dict[str, Any]]]:
    for path in sorted(files):
        if not path.endswith((".yml", ".yaml")) or path == "dbt_project.yml":
            continue
        try:
            parsed = yaml.safe_load(files[path])
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            yield path, parsed


def model_files(
    files: Mapping[str, str], model_paths: Iterable[str] | None
) -> dict[str, list[str]]:
    """Model file stem -> the files with that stem under the model paths.

    ``model_paths`` of ``None`` reads every ``.sql`` and ``.py`` file, for a
    caller that holds no project view.
    """

    roots = [PurePosixPath(p.strip("/") or ".") for p in model_paths or ["."]]
    out: dict[str, list[str]] = {}
    for path in sorted(files):
        candidate = PurePosixPath(path)
        if candidate.suffix not in _MODEL_SUFFIXES:
            continue
        if not any(
            root == PurePosixPath(".") or root in candidate.parents for root in roots
        ):
            continue
        out.setdefault(candidate.stem, []).append(path)
    return out


def version_map(
    files: Mapping[str, str], model_paths: Iterable[str] | None = None
) -> dict[str, VersionedModel]:
    """Every versioned model the project's YAML declares, keyed by its name.

    A model entry in two files (which dbt refuses) is read from the first path
    in sorted order, as every other YAML reader does.
    """

    stems = model_files(files, model_paths)
    out: dict[str, VersionedModel] = {}
    for path, parsed in _yaml_documents(files):
        for entry in parsed.get("models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            items = entry.get("versions")
            if not isinstance(items, list) or not items:
                continue
            base = entry["name"]
            if base in out:
                continue
            default = default_version(entry)
            versions: list[ModelVersion] = []
            for index, item in enumerate(items):
                if not isinstance(item, dict) or "v" not in item:
                    continue
                v = version_text(item["v"])
                file, source = _version_file(base, v, item, default, stems)
                versions.append(
                    ModelVersion(
                        v=v,
                        label=f"{base}.v{v}",
                        file=file,
                        file_source=source,
                        index=index,
                        item=item,
                    )
                )
            latest = entry.get("latest_version")
            out[base] = VersionedModel(
                base=base,
                yaml_path=path,
                entry=entry,
                latest_version=version_text(latest) if latest is not None else None,
                default=default,
                versions=versions,
            )
    return out


def _version_file(
    base: str,
    v: str,
    item: Mapping[str, Any],
    default: str | None,
    stems: Mapping[str, list[str]],
) -> tuple[str | None, str]:
    defined_in = item.get("defined_in")
    if isinstance(defined_in, str) and defined_in:
        found = stems.get(defined_in)
        return (found[0], "defined_in") if found else (None, "missing")
    found = stems.get(f"{base}_v{v}")
    if found:
        return found[0], "suffix"
    if v == default and stems.get(base):
        return stems[base][0], "unsuffixed_latest"
    return None, "missing"


_CACHE: dict[tuple[Any, ...], tuple[Mapping[str, str], dict[str, VersionedModel]]] = {}


def cached_version_map(
    files: Mapping[str, str], model_paths: Iterable[str] | None = None
) -> dict[str, VersionedModel]:
    """:func:`version_map`, memoised on the files' YAML content and model paths.

    The readers that call it run once per model over the same files, and each
    call would otherwise parse every YAML file again. The key is the content's
    hash, so a file rewritten between two calls is read afresh.
    """

    key = (
        tuple(model_paths) if model_paths is not None else None,
        tuple(
            sorted(
                (path, hash(content))
                for path, content in files.items()
                if path.endswith((".yml", ".yaml"))
            )
        ),
        tuple(sorted(p for p in files if p.endswith(_MODEL_SUFFIXES))),
    )
    hit = _CACHE.get(key)
    if hit is not None:
        return hit[1]
    result = version_map(files, model_paths)
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = (files, result)
    return result


def lookup(vmap: Mapping[str, VersionedModel], base: str) -> VersionedModel | None:
    """The versioned model named ``base``, case-insensitively."""

    return vmap.get(base) or next(
        (m for k, m in vmap.items() if k.lower() == base.lower()), None
    )


def expand(names: Iterable[str], vmap: Mapping[str, VersionedModel]) -> set[str]:
    """``names`` with every version name resolved to its labels.

    A bare versioned name becomes every version's label and a version's file
    stem its label; anything else is kept as written.
    """

    out: set[str] = set()
    for name in names:
        labels = resolve(name, vmap)
        out.update(labels if labels else [name])
    return out


# --- naming a version -------------------------------------------------------------


def split_label(name: str) -> tuple[str, str | None]:
    """``dim_teams.v2`` -> (``dim_teams``, ``2``); anything else -> (name, None)."""

    base, dot, rest = name.partition(".")
    if dot and rest.startswith("v") and len(rest) > 1:
        return base, rest[1:]
    return name, None


def resolve(name: str, vmap: Mapping[str, VersionedModel]) -> list[str] | None:
    """The version labels ``name`` selects, or ``None`` when it names no version.

    ``dim_teams.v2`` selects that version, a bare ``dim_teams`` every version
    (as dbt's ``--select`` does), and a version's file stem (``dim_teams_v2``, or
    the stem a ``defined_in`` names) the version it builds. Case-insensitive.
    """

    lowered = {key.lower(): model for key, model in vmap.items()}
    base, v = split_label(name)
    model = lowered.get(base.lower())
    if model is not None:
        if v is None:
            return model.labels
        found = model.version(v)
        return [found.label] if found else []
    label = label_for_stem(name, vmap)
    return [label] if label else None


def label_for_stem(stem: str, vmap: Mapping[str, VersionedModel]) -> str | None:
    """The version a file stem builds, if it builds one."""

    for model in vmap.values():
        for item in model.versions:
            if item.stem is not None and item.stem.lower() == stem.lower():
                return item.label
    return None


def label_for_file(path: str, vmap: Mapping[str, VersionedModel]) -> str | None:
    """The version label a model file builds, if it is a version's file."""

    for model in vmap.values():
        for item in model.versions:
            if item.file == path:
                return item.label
    return None


def ref_target(
    base: str, version: str | None, vmap: Mapping[str, VersionedModel]
) -> str | None:
    """What ``ref(base)`` or ``ref(base, v=version)`` resolves to.

    The label of a version of a versioned model, or ``None`` when ``base`` is
    not versioned (the ref then names the plain model ``base``) or the version
    does not exist.
    """

    model = lookup(vmap, base)
    if model is None:
        return None
    if version is None:
        return model.default_label
    found = model.version(version_text(version))
    return found.label if found else None


# --- what one version holds -------------------------------------------------------


def deep_merge(base: Any, override: Any) -> Any:
    """dbt's ``deep_merge`` of two values: dicts by key, lists concatenated.

    ``override``'s items come first in a concatenated list, as dbt writes them.
    """

    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict):
            out[key] = deep_merge(
                out.get(key) if isinstance(out.get(key), dict) else {}, value
            )
        elif isinstance(value, list) and key in out and isinstance(out[key], list):
            out[key] = list(copy.deepcopy(value)) + list(out[key])
        else:
            out[key] = copy.deepcopy(value)
    return out


def include_exclude(item: Mapping[str, Any]) -> tuple[Any, list[str], int | None]:
    """A version's ``include``, its ``exclude``, and the element's list position."""

    for position, column in enumerate(item.get("columns") or []):
        if isinstance(column, dict) and "include" in column:
            exclude = column.get("exclude") or []
            return (
                column.get("include"),
                [str(e) for e in exclude] if isinstance(exclude, list) else [],
                position,
            )
    return "*", [], None


def includes(include: Any, exclude: list[str], column: str) -> bool:
    included = include in _INCLUDE_ALL or (
        isinstance(include, list) and column in include
    )
    return included and column not in exclude


def own_columns(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The column entries a version writes itself (not its include/exclude)."""

    return [
        column
        for column in item.get("columns") or []
        if isinstance(column, dict)
        and "include" not in column
        and isinstance(column.get("name"), str)
    ]


def inherited_columns(
    entry: Mapping[str, Any], item: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """The top-level column entries version ``item`` admits."""

    include, exclude, _ = include_exclude(item)
    return [
        column
        for column in entry.get("columns") or []
        if isinstance(column, dict)
        and isinstance(column.get("name"), str)
        and includes(include, exclude, column["name"])
    ]


def effective_entry(
    entry: Mapping[str, Any], item: Mapping[str, Any]
) -> dict[str, Any]:
    """The model entry dbt builds version ``item`` from, as one plain entry.

    Shaped like an unversioned ``models:`` entry, so a reader that understands
    one reads a version with no further change.
    """

    out = {
        key: copy.deepcopy(value)
        for key, value in entry.items()
        if key
        not in (
            "versions",
            "latest_version",
            "columns",
            "config",
            "tests",
            "data_tests",
        )
    }
    description = item.get("description") or entry.get("description")
    if description:
        out["description"] = description
    else:
        out.pop("description", None)
    config = deep_merge(
        entry.get("config") if isinstance(entry.get("config"), dict) else {},
        item.get("config") if isinstance(item.get("config"), dict) else {},
    )
    if config:
        out["config"] = config
    columns: dict[str, dict[str, Any]] = {}
    for column in inherited_columns(entry, item) + own_columns(item):
        columns[column["name"]] = copy.deepcopy(column)
    if columns:
        out["columns"] = list(columns.values())
    for key in ("data_tests", "tests"):
        if item.get(key) is not None:
            out[key] = copy.deepcopy(item[key])
            break
    else:
        for key in ("data_tests", "tests"):
            if entry.get(key) is not None:
                out[key] = copy.deepcopy(entry[key])
                break
    out["version"] = version_text(item["v"])
    return out


def entry_variants(entry: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``(label, entry)`` for each node a ``models:`` entry declares.

    A plain entry is itself under its name; a versioned one is each version's
    :func:`effective_entry` under ``<name>.v<v>``.
    """

    name = entry.get("name")
    if not isinstance(name, str):
        return []
    items = [i for i in entry.get("versions") or [] if isinstance(i, dict) and "v" in i]
    if not items:
        return [(name, dict(entry))]
    return [
        (f"{name}.v{version_text(item['v'])}", effective_entry(entry, item))
        for item in items
    ]


# --- what dbt would get wrong -----------------------------------------------------


def structural_problems(
    files: Mapping[str, str],
    model_paths: Iterable[str] | None = None,
    *,
    models: Iterable[str] | None = None,
) -> list[VersionProblem]:
    """Every versioned model dbt would build wrong or silently drop.

    ``models`` narrows the check to those names (a plan judges only what it
    touches); ``None`` checks every versioned model.
    """

    stems = model_files(files, model_paths)
    vmap = version_map(files, model_paths)
    plain = _plain_entries(files)
    wanted = {m.lower() for m in models} if models is not None else None
    problems: list[VersionProblem] = []
    for base, model in vmap.items():
        if wanted is not None and base.lower() not in wanted:
            continue
        path = model.yaml_path
        listed = [
            version_text(i["v"])
            for i in model.entry.get("versions") or []
            if isinstance(i, dict) and "v" in i
        ]
        seen: set[str] = set()
        for v in listed:
            if v in seen:
                problems.append(
                    VersionProblem(
                        "version_duplicate",
                        base,
                        f"'{base}' lists version {v} twice; each `v:` appears once",
                        path,
                    )
                )
            seen.add(v)
        if model.latest_version is not None and model.latest_version not in listed:
            problems.append(
                VersionProblem(
                    "latest_version_unknown",
                    base,
                    f"'{base}' sets `latest_version: {model.latest_version}`, which is "
                    f"not one of its versions ({', '.join(listed)})",
                    path,
                )
            )
        claimed: dict[str, str] = {}
        for item in model.versions:
            defined_in = item.item.get("defined_in")
            if defined_in is not None and (
                not isinstance(defined_in, str)
                or "/" in defined_in
                or defined_in.endswith(_MODEL_SUFFIXES)
            ):
                problems.append(
                    VersionProblem(
                        "defined_in_not_a_stem",
                        base,
                        f"'{item.label}' has `defined_in: {defined_in}`; dbt reads "
                        "it as a model file's name without its directory or extension "
                        f"(`defined_in: {PurePosixPath(str(defined_in)).stem}`)",
                        path,
                    )
                )
                continue
            if item.file is None:
                expected = defined_in or f"{base}_v{item.v}"
                hint = (
                    f" Only the latest version may live in the unsuffixed `{base}`; "
                    f"keep that file with `defined_in: {base}` on this version"
                    if stems.get(base) and item.v != model.default
                    else ""
                )
                problems.append(
                    VersionProblem(
                        "version_file_missing",
                        base,
                        f"no model file builds '{item.label}': dbt looks for "
                        f"`{expected}.sql` and only warns when it is absent, so the "
                        f"version would silently not exist.{hint}",
                        path,
                    )
                )
                continue
            if item.file in claimed:
                problems.append(
                    VersionProblem(
                        "version_file_collision",
                        base,
                        f"'{claimed[item.file]}' and '{item.label}' are both built "
                        f"from {item.file}; each version needs its own file",
                        path,
                    )
                )
            claimed[item.file] = item.label
            stem = item.stem or ""
            if stem in plain and stem != base:
                problems.append(
                    VersionProblem(
                        "version_file_collision",
                        base,
                        f"{item.file} builds '{item.label}', and {plain[stem]} also "
                        f"declares a plain model entry named '{stem}'; remove that "
                        f"entry and declare the version under `{base}`'s `versions:`",
                        plain[stem],
                    )
                )
        problems.extend(
            VersionProblem(
                "version_file_collision",
                base,
                f"{unversioned} is an unversioned model named '{base}', beside the "
                f"versioned '{base}'. Only the latest version may live in it; map "
                f"it to a version with `defined_in: {base}` or rename it",
                unversioned,
            )
            for unversioned in stems.get(base, [])
            if unversioned not in claimed
        )
    return problems


def _plain_entries(files: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, parsed in _yaml_documents(files):
        for entry in parsed.get("models") or []:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and not entry.get("versions")
            ):
                out.setdefault(entry["name"], path)
    return out
