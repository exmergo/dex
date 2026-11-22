"""What ``dex init`` reads and writes to install the Elementary package (#1464).

A project only gets the history Elementary records once the package is installed
and running in production. ``dex init`` offers it on every install: a project it
creates declares the package in the files it writes, a project it adopts gets
the same two edits as a stored ``transform`` plan, and a project that already
declares it gets its release reported and nothing offered.

The two edits, built here and nowhere else:

- ``packages.yml`` (or the ``dependencies.yml`` that declares the project's
  packages): the package pinned to :data:`INSTALL_RELEASE`, the newest release
  the Elementary fixture runs and above 0.20.0, the first release that honours
  the per-test sample flag Dex renders.
- ``dbt_project.yml``: the package's models disabled on the dev target, which
  disables its run hooks with them, so a dev build runs none of its work; its
  schema named, so the user knows where its tables land; and samples off for the
  project (``test_sample_row_count: 0``).

The dev-target condition names one target, the one Dex builds on. Every other
target runs the package, so production records history even where Dex was told
nothing about it, and a second development target (a CI target, say) runs the
package too until the condition is widened by hand.

Everything here reads the repository only: no dbt parse, no connection. What
the package does once installed is :mod:`.elementary`'s to read, from a
manifest.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import yaml

from ..errors import RequestError

#: The release the install pins: the newest the Elementary fixture runs
#: (``CURRENT_VERSION`` in ``tests/transform/elementary_fixture.py``, which a
#: test holds this to), never below 0.20.0.
INSTALL_RELEASE = "0.26.1"

#: The package's name on the dbt hub.
HUB_PACKAGE = "elementary-data/elementary"

#: The repository a git declaration of the package names.
GIT_REPOSITORY = "elementary-data/dbt-data-reliability"

#: The custom schema the install writes for the package's models. dbt's default
#: ``generate_schema_name`` builds them into ``<target schema>_elementary``.
SCHEMA = "elementary"

#: The intent every install plan is stored under, so a second ``dex init`` finds
#: the plan the first one stored instead of storing another.
INTENT = f"install the Elementary package ({HUB_PACKAGE} {INSTALL_RELEASE})"


def intent(project_dir: str) -> str:
    """The install plan's intent for the project at ``project_dir`` (relative
    to the repository root). A plan id hashes the intent and the edits and not
    the project, so two projects with identical files would otherwise store
    one plan under one id, the second overwriting the first."""

    return INTENT if project_dir in {"", "."} else f"{INTENT} in {project_dir}"


#: The package's own name: its models' ``package_name``, and the key under
#: ``models:`` that configures them.
PACKAGE = "elementary"


class InstallRefusedError(RequestError):
    """The install cannot be written into a file as it stands; the message names
    the file and the manual edit."""


def _is_elementary(entry: Any) -> bool:
    return (
        (entry.source == "hub" and entry.name == HUB_PACKAGE)
        or (entry.source == "git" and GIT_REPOSITORY in entry.name)
        or (entry.source == "local" and entry.install_name == PACKAGE)
    )


def detect(project_dir: Path | str) -> dict[str, Any]:
    """Whether the project declares the package, installs it, and at which release.

    ``declared_in`` names the file that declares the project's packages,
    ``declared_version`` the version or revision the declaration pins, and
    ``release`` the ``version`` in the installed package's own
    ``dbt_project.yml`` (``None`` where none can be read).
    """

    from .build import _root_text, declared_packages
    from .package_overrides import _installed

    project = Path(project_dir)
    declared = next((p for p in declared_packages(project) if _is_elementary(p)), None)
    release: str | None = None
    installed = False
    for name, directory, _ in _installed(project):
        if name != PACKAGE:
            continue
        installed = True
        with contextlib.suppress(OSError, UnicodeError, yaml.YAMLError):
            own = yaml.safe_load(
                (directory / "dbt_project.yml").read_text(encoding="utf-8")
            )
            if isinstance(own, dict) and own.get("version") is not None:
                release = str(own["version"])
    declared_in = None
    if declared is not None:
        declared_in = (
            "packages.yml"
            if _root_text(project, "packages.yml", None) is not None
            else "dependencies.yml"
        )
    return {
        "declared": declared is not None,
        "declared_in": declared_in,
        "declared_version": declared.version if declared else None,
        "source": declared.source if declared else None,
        "installed": installed,
        "release": release,
    }


def enabled_expression(dev_target: str) -> str:
    """The ``+enabled`` value that disables the package on ``dev_target`` only."""

    return "{{ target.name != " + _jinja_string(dev_target) + " }}"


def _jinja_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _item(column: int) -> str:
    pad = " " * column
    return f"{pad}- package: {HUB_PACKAGE}\n{pad}  version: {INSTALL_RELEASE}\n"


def _append_package(text: str | None, filename: str) -> str:
    """``text`` with the package appended to its ``packages:`` list, in place.

    Every comment, key and other entry is kept. A file with no ``packages:`` key
    gains one at the end; a flow-style list, or a root that is not a block
    mapping, is refused, since rewriting it would reformat what a person wrote.
    """

    from ..yaml_spans import after_node, before_trailing_blanks, sequence_item_span

    def refuse(why: str) -> InstallRefusedError:
        return InstallRefusedError(
            f"{filename} {why}; add `- package: {HUB_PACKAGE}` with "
            f"`version: {INSTALL_RELEASE}` to its packages by hand"
        )

    if text is None or not text.strip():
        return (text or "") + "packages:\n" + _item(2)
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise refuse("does not parse") from exc
    if root is None:
        return text + ("" if text.endswith("\n") else "\n") + "packages:\n" + _item(2)
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise refuse("is not a block mapping")
    found = next(
        (
            (key, value)
            for key, value in root.value
            if isinstance(key, yaml.ScalarNode) and key.value == "packages"
        ),
        None,
    )
    if found is None:
        end = before_trailing_blanks(text, len(text))
        prefix = text[:end]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        return prefix + "packages:\n" + _item(2) + text[end:]
    key, value = found
    if isinstance(value, yaml.ScalarNode) and value.tag.endswith(":null"):
        eol = text.find("\n", key.end_mark.index)
        at = len(text) if eol < 0 else eol + 1
        prefix = text[:at] if eol >= 0 else text + "\n"
        return prefix + _item(key.start_mark.column + 2) + text[at:]
    if not isinstance(value, yaml.SequenceNode) or value.flow_style:
        raise refuse("writes packages in a form Dex will not rewrite (a flow list)")
    if not value.value:
        raise refuse("writes an empty packages list")
    first = sequence_item_span(text, value.value[0].start_mark.index)
    last = sequence_item_span(text, value.value[-1].start_mark.index)
    if first is None or last is None:
        raise refuse("writes a package entry on a line it shares")
    column = len(text[first[0] :]) - len(text[first[0] :].lstrip(" "))
    # After the last entry's last line, which a comment written shallower than
    # its keys cannot cut short.
    end = after_node(text, value.value[-1])
    prefix = text[:end]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + _item(column) + text[end:]


def _set(text: str, path: list[str], value: Any) -> str:
    from .package_repair import RepairRefusedError, set_nested

    try:
        return set_nested(text, path, value)
    except RepairRefusedError as exc:
        raise InstallRefusedError(
            f"{exc}; add `{'.'.join(path)}: {json.dumps(value)}` to "
            "dbt_project.yml by hand"
        ) from exc


def project_yml(text: str, dev_target: str) -> str:
    """``dbt_project.yml`` with the package disabled on ``dev_target``, its schema
    named and samples off, written in place."""

    out = _set(text, ["models", PACKAGE, "+schema"], SCHEMA)
    out = _set(out, ["models", PACKAGE, "+enabled"], enabled_expression(dev_target))
    return _set(out, ["vars", "test_sample_row_count"], 0)


def packages_file(project_dir: Path | str) -> str:
    """The file the install edits: ``packages.yml`` where it exists, else a
    ``dependencies.yml`` that exists, else a new ``packages.yml``."""

    project = Path(project_dir)
    if (project / "packages.yml").is_file():
        return "packages.yml"
    if (project / "dependencies.yml").is_file():
        return "dependencies.yml"
    return "packages.yml"


def install_texts(
    packages_text: str | None,
    filename: str,
    project_text: str,
    dev_target: str,
) -> tuple[str, str]:
    """The two files after the install: ``(packages file, dbt_project.yml)``.

    ``packages_text`` is the current text of ``filename`` (``None`` where the
    file does not exist yet). Raises :class:`InstallRefusedError` where either
    file is written in a form an in-place edit would reformat.
    """

    return (
        _append_package(packages_text, filename),
        project_yml(project_text, dev_target),
    )


def install_edits(project_dir: Path | str, dev_target: str) -> list[Any]:
    """The install as the two ``transform`` plan edits for an existing project."""

    from .plans import EditKind, PlanEdit

    project = Path(project_dir)
    filename = packages_file(project)
    path = project / filename
    packages_text = path.read_text(encoding="utf-8") if path.is_file() else None
    project_text = (project / "dbt_project.yml").read_text(encoding="utf-8")
    new_packages, new_project = install_texts(
        packages_text, filename, project_text, dev_target
    )
    return [
        PlanEdit(path=filename, kind=EditKind.PACKAGES_YML, new_content=new_packages),
        PlanEdit(
            path="dbt_project.yml", kind=EditKind.PROJECT_YML, new_content=new_project
        ),
    ]


def production_steps(prod_target: str | None) -> list[dict[str, str]]:
    """What the user runs on production, since Dex builds only on dev."""

    target = prod_target or "<production target>"
    return [
        {
            "command": "dbt deps",
            "purpose": (
                "yours to run where production runs: install the Elementary "
                "package there"
            ),
        },
        {
            "command": f"dbt run --select elementary --target {target}",
            "purpose": (
                "yours to run once on production: create the package's tables. "
                "From then on its hooks run on every production run and upload "
                "results, work those runs did not do before"
            ),
        },
    ]
