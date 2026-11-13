"""Installed package models a project overrides, and the YAML entries dbt gives them.

A project override (#860) is a project model with a package model's name, with
the package's model disabled under ``models.<package>`` in ``dbt_project.yml``.
When the package also documents that model in a ``models:`` entry, dbt applies
the package's entry to the project's override: the package's own lookup misses
its disabled node and falls back to a lookup in any package, which finds the
override (``dbt/parser/schemas.py``, the same in every dbt we support). A second
entry in the project for the same name then fails dbt's parse as two
``schema.yml`` entries for one resource, whichever file dbt reads first, so the
override can carry no YAML entry, and therefore no declaration, of its own
(#1161).

Read from the repository alone: the project's ``dbt_project.yml`` as a plan
leaves it, and each installed package's ``dbt_project.yml``, model files and
YAML. Nothing here opens a connection or runs dbt.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PackageModel:
    """One model file an installed package defines."""

    package: str
    name: str
    enabled: bool


@dataclass(frozen=True)
class DocumentedOverride:
    """A project model overriding a package model the package documents.

    ``package_entry`` is the package's YAML file, relative to the project
    directory (``dbt_packages/tiny_roster/models/roster_member.yml``).
    """

    model: str
    package: str
    package_entry: str


def models_config(project_yml: str | None) -> dict[str, Any]:
    """The ``models:`` block of a ``dbt_project.yml``'s text, or empty."""

    with contextlib.suppress(yaml.YAMLError, AttributeError, TypeError):
        loaded = yaml.safe_load(project_yml or "") or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("models"), dict):
            return loaded["models"]
    return {}


def seeds_config(project_yml: str | None) -> dict[str, Any]:
    """the ``seeds:`` block of a ``dbt_project.yml``'s text, or empty."""

    with contextlib.suppress(yaml.YAMLError, AttributeError, TypeError):
        loaded = yaml.safe_load(project_yml or "") or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("seeds"), dict):
            return loaded["seeds"]
    return {}


def package_custom_schema(
    project_config: Mapping[str, Any], package: str, parts: Iterable[str]
) -> str | None:
    """the custom schema configured for a package node under ``+schema``."""

    custom = None
    level: Any = project_config.get(package)
    for part in [None, *parts]:
        if part is not None:
            level = level.get(part) if isinstance(level, dict) else None
        if not isinstance(level, dict):
            break
        for key in ("+schema", "schema"):
            if isinstance(level.get(key), str):
                custom = level[key]
    return custom


def package_enabled(
    project_config: Mapping[str, Any], package: str, parts: Iterable[str]
) -> bool:
    """Whether the project leaves a package model enabled.

    dbt reads ``enabled`` from the most specific level of
    ``models.<package>.<folder>...<model>``, and that is what is read here.
    Package SQL that disables itself with ``config(enabled=false)`` is not read.
    """

    state = True
    level: Any = project_config.get(package)
    for part in [None, *parts]:
        if part is not None:
            level = level.get(part) if isinstance(level, dict) else None
        if not isinstance(level, dict):
            break
        for key in ("+enabled", "enabled"):
            if isinstance(level.get(key), bool):
                state = level[key]
    return state


def _installed(root: Path) -> list[tuple[str, Path, list[Path]]]:
    """Each installed package as ``(name, directory, model roots)``."""

    from .build import packages_dir

    install = packages_dir(root)
    if not install.is_dir():
        return []
    found: list[tuple[str, Path, list[Path]]] = []
    for directory in sorted(p for p in install.iterdir() if p.is_dir()):
        manifest: dict[str, Any] = {}
        with contextlib.suppress(OSError, UnicodeError, yaml.YAMLError):
            loaded = yaml.safe_load(
                (directory / "dbt_project.yml").read_text(encoding="utf-8")
            )
            manifest = loaded if isinstance(loaded, dict) else {}
        name = manifest.get("name") if isinstance(manifest.get("name"), str) else None
        paths = manifest.get("model-paths") or ["models"]
        roots = [
            directory / str(path)
            for path in (paths if isinstance(paths, list) else ["models"])
        ]
        existing = [r for r in roots if r.is_dir()]
        found.append((name or directory.name, directory, existing))
    return found


def package_models(
    root: Path | str, names: set[str], project_yml: str | None
) -> list[PackageModel]:
    """The installed package models named in ``names``, with whether the
    project's ``dbt_project.yml`` (``project_yml``, the text after a plan)
    leaves each enabled."""

    if not names:
        return []
    project_config = models_config(project_yml)
    found: list[PackageModel] = []
    for package, _directory, roots in _installed(Path(root)):
        for base in roots:
            for path in sorted(base.rglob("*")):
                if path.suffix not in (".sql", ".py") or path.stem not in names:
                    continue
                folders = list(path.relative_to(base).parent.parts)
                found.append(
                    PackageModel(
                        package=package,
                        name=path.stem,
                        enabled=package_enabled(
                            project_config, package, [*folders, path.stem]
                        ),
                    )
                )
    return found


def _documented(directory: Path, roots: list[Path], names: set[str]) -> dict[str, Path]:
    """The models in ``names`` a package's YAML documents, by name, with the
    first file that does. A versioned entry is left out: a versioned package
    model is never offered as an override."""

    documented: dict[str, Path] = {}
    for base in roots:
        for path in sorted(base.rglob("*")):
            if path.suffix not in (".yml", ".yaml"):
                continue
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            entries = loaded.get("models") if isinstance(loaded, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or entry.get("versions"):
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name in names:
                    documented.setdefault(name, path)
    return documented


def documented_overrides(
    root: Path | str, models: Iterable[str], project_yml: str | None
) -> dict[str, DocumentedOverride]:
    """The project models in ``models`` that override a package model the
    package documents, by model name.

    An override here is a package model of the same name that ``project_yml``
    leaves disabled. A package model left enabled is not one: dbt then applies
    the package's entry to the package's own node and refuses the pair for
    sharing one database representation, which is a different repair.
    """

    names = {m for m in models if m}
    if not names:
        return {}
    project = Path(root)
    project_config = models_config(project_yml)
    found: dict[str, DocumentedOverride] = {}
    for package, directory, roots in _installed(project):
        disabled: set[str] = set()
        for base in roots:
            for path in base.rglob("*"):
                if path.suffix not in (".sql", ".py") or path.stem not in names:
                    continue
                folders = list(path.relative_to(base).parent.parts)
                if not package_enabled(project_config, package, [*folders, path.stem]):
                    disabled.add(path.stem)
        if not disabled:
            continue
        for name, entry in _documented(directory, roots, disabled).items():
            if name in found:
                continue
            try:
                shown = entry.relative_to(project).as_posix()
            except ValueError:
                shown = entry.as_posix()
            found[name] = DocumentedOverride(
                model=name, package=package, package_entry=shown
            )
    return found


def override_refusal(
    override: DocumentedOverride, project_paths: list[str], *, declaring: bool = False
) -> str:
    """The refusal for a project entry on ``override``, naming every file.

    ``declaring`` is a declaration that would write the entry, as opposed to
    one already in the project or the plan's edits.
    """

    where = ", ".join(f"`{p}`" for p in project_paths) or "this project"
    second = (
        f"a declaration would write a second one in {where}"
        if declaring
        else f"the entry in {where} is a second one"
    )
    return (
        f"'{override.model}' overrides the model {override.package} documents in "
        f"`{override.package_entry}`. dbt applies that entry to this project's "
        f"override, so the override can hold no YAML entry of its own: {second}, "
        "and dbt's parse refuses two entries for one resource. Plan the override "
        "without a declaration or YAML entry (the package's entry documents it, "
        "and its tests run against it), and declare the models that read it "
        "instead"
    )
