"""Installed package nodes a declaration may name as its parent (#1155).

A declaration's ``population.parent`` and a column's ``references`` name the
model, seed or snapshot the declaring model's rows or keys come from. In most
real projects that is a package's staging model (Fivetran's, dbt-labs'), which
the project reads with ``ref()`` and never edits. This module finds such a node
among the installed packages and says what columns it produces, so the
declaration can be checked against it the way a project parent is.

The project always wins: a caller looks here only once the project itself has no
node of that name, so a project override of a package model is the parent, as
it is for ``ref()`` and for ``transform references``. A package model the
project's ``dbt_project.yml`` disables is no node dbt builds, so it is reported
as disabled rather than accepted.

Columns are read from the package's own SQL, then from the compiled SQL the
project's manifest holds for that node, and a seed's from its CSV header. Where
none of those can be read (a staging model that lists its columns through a
macro, with no compiled manifest yet) the columns are ``None`` with the reason,
and the caller reports the column as unchecked instead of refusing it.

Read from the repository alone: nothing here writes a package file, opens a
connection or runs dbt.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .package_overrides import (
    _installed,
    models_config,
    package_custom_schema,
    package_enabled,
    seeds_config,
)

_SQL_CONFIG_SCHEMA = re.compile(
    r"""config\s*\([^)]*\bschema\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def _sql_schema(text: str) -> str | None:
    """the custom schema a model's sql declares in ``config(schema=...)``."""

    match = _SQL_CONFIG_SCHEMA.search(text)
    return match.group(1) if match is not None else None


def _manifest_schema(
    manifest: Mapping[str, Any] | None, package: str, name: str
) -> str | None:
    """the custom schema from the compiled manifest for a package node."""

    nodes = manifest.get("nodes") if isinstance(manifest, Mapping) else None
    if not isinstance(nodes, Mapping):
        return None
    for node in nodes.values():
        if (
            isinstance(node, Mapping)
            and node.get("package_name") == package
            and node.get("name") == name
        ):
            config = node.get("config")
            if isinstance(config, Mapping) and isinstance(config.get("schema"), str):
                return config["schema"]
    return None


@dataclass(frozen=True)
class PackageTarget:
    """A model, seed or snapshot an installed package defines.

    ``path`` is the defining file relative to the project directory
    (``dbt_packages/intercom/models/stg_intercom__conversations.sql``).
    ``columns`` is the lowercased set the node produces, or ``None`` with
    ``unread`` saying why it could not be read. ``enabled`` is false for a
    package model the project disables, which dbt does not build.
    """

    name: str
    package: str
    kind: str
    path: str
    columns: frozenset[str] | None
    unread: str | None = None
    enabled: bool = True
    schema: str | None = None

    def described(self) -> str:
        """How a message names this node: its name, package and file."""

        return f"'{self.name}' ({self.kind} of package {self.package}, `{self.path}`)"


def find_package_target(
    root: Path | str,
    name: str,
    *,
    project_yml: str | None,
    manifest: Mapping[str, Any] | None = None,
) -> PackageTarget | None:
    """The installed package node called ``name``, or ``None``.

    An enabled model is preferred over a disabled one of the same name in
    another package, since that is the one ``ref()`` resolves. ``project_yml``
    is the project's ``dbt_project.yml`` as the plan leaves it, which decides
    whether a package model is enabled.
    """

    if not name or "." in name:
        return None
    project = Path(root)
    found: list[PackageTarget] = []
    for package, directory, _roots in _installed(project):
        target = _in_package(project, package, directory, name, project_yml, manifest)
        if target is not None:
            found.append(target)
    if not found:
        return None
    return next((t for t in found if t.enabled), found[0])


def _in_package(
    project: Path,
    package: str,
    directory: Path,
    name: str,
    project_yml: str | None,
    manifest: Mapping[str, Any] | None,
) -> PackageTarget | None:
    """The node called ``name`` in one installed package, or ``None``."""
    from ..dbt_project import DbtProjectError
    from ..dbt_project import load as load_project
    from .native_population_parent import (
        _is_seed_or_snapshot,
        _model_sql_path,
        _select_columns_if_available,
    )

    try:
        view = load_project(directory)
    except (DbtProjectError, OSError, UnicodeError):
        return None
    files = {path: source.content for path, source in view.files.items()}

    def shown(relative: str) -> str:
        """A package file's path, relative to the project where it can be."""
        absolute = directory / relative
        try:
            return absolute.relative_to(project).as_posix()
        except ValueError:
            return absolute.as_posix()

    pkg_yml_src = view.files.get("dbt_project.yml")
    pkg_yml_text = pkg_yml_src.content if pkg_yml_src is not None else None

    sql_path = _model_sql_path(files, view.model_paths, name)
    if sql_path is not None:
        base = next(
            (
                Path(p)
                for p in view.model_paths
                if Path(sql_path).is_relative_to(Path(p))
            ),
            Path(sql_path).parent,
        )
        folders = list(Path(sql_path).relative_to(base).parent.parts)
        enabled = package_enabled(models_config(project_yml), package, [*folders, name])
        schema = (
            _manifest_schema(manifest, package, name)
            or package_custom_schema(
                models_config(project_yml), package, [*folders, name]
            )
            or _sql_schema(files[sql_path])
            or package_custom_schema(
                models_config(pkg_yml_text), package, [*folders, name]
            )
        )
        columns = _select_columns_if_available(files[sql_path])
        unread = None
        if columns is None:
            compiled = _compiled_code(manifest, package, name)
            columns = _select_columns_if_available(compiled) if compiled else None
            if columns is None:
                unread = (
                    "its SELECT list could not be read statically (a `select *` or "
                    "a macro standing in for its columns)"
                    + (
                        ""
                        if compiled
                        else ", and the project has no compiled manifest to read "
                        "it from"
                    )
                )
        return PackageTarget(
            name=name,
            package=package,
            kind="model",
            path=shown(sql_path),
            columns=frozenset(columns) if columns is not None else None,
            unread=unread,
            enabled=enabled,
            schema=schema,
        )

    seed = _seed_path(files, view.seed_paths, name)
    if seed is not None:
        header = _csv_header(files[seed])
        schema = (
            _manifest_schema(manifest, package, name)
            or package_custom_schema(seeds_config(project_yml), package, [name])
            or package_custom_schema(seeds_config(pkg_yml_text), package, [name])
        )
        return PackageTarget(
            name=name,
            package=package,
            kind="seed",
            path=shown(seed),
            columns=header,
            unread=None if header is not None else "its CSV header could not be read",
            schema=schema,
        )

    if _is_seed_or_snapshot(files, view, name):
        schema = _manifest_schema(manifest, package, name)
        return PackageTarget(
            name=name,
            package=package,
            kind="snapshot",
            path=shown(""),
            columns=None,
            unread="a snapshot's columns are not read statically",
            schema=schema,
        )
    return None


def _seed_path(
    files: Mapping[str, str], seed_paths: list[str], name: str
) -> str | None:
    """The seed CSV called ``name`` under the package's seed paths."""
    roots = [Path(p) for p in seed_paths]
    for path in sorted(files):
        candidate = Path(path)
        if (
            candidate.suffix == ".csv"
            and candidate.stem == name
            and any(candidate.is_relative_to(root) for root in roots)
        ):
            return path
    return None


def _csv_header(text: str) -> frozenset[str] | None:
    """A seed's lowercased column names, from its header row."""
    with contextlib.suppress(csv.Error, StopIteration):
        row = next(csv.reader(io.StringIO(text)))
        names = [c.strip().lower() for c in row if c.strip()]
        if names:
            return frozenset(names)
    return None


def _compiled_code(
    manifest: Mapping[str, Any] | None, package: str, name: str
) -> str | None:
    """The compiled SQL the project's manifest holds for the package model."""

    nodes = manifest.get("nodes") if isinstance(manifest, Mapping) else None
    if not isinstance(nodes, Mapping):
        return None
    for node in nodes.values():
        if (
            isinstance(node, Mapping)
            and node.get("resource_type") == "model"
            and node.get("package_name") == package
            and node.get("name") == name
        ):
            code = node.get("compiled_code") or node.get("compiled_sql")
            return code if isinstance(code, str) and code.strip() else None
    return None
