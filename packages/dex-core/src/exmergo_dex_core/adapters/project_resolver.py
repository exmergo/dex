"""Turning a format's *name* into a project: the open registry.

A name reaches here from `.dex/config.yml` (`project.format`) or from
``--project-format``, and leaves as a constructed project. Three kinds of name
resolve, in this order:

- a **shipped name** (``dbt``), which can never be shadowed by
  anything installed, so a config that says ``dbt`` means dex's own reader on
  every machine that reads it;
- a **dotted path**, ``mypkg.projects:my_project``, which needs no packaging work
  at all and is what a host reaches for first;
- an **entry-point name**, registered by an installed distribution under
  ``exmergo_dex_core.projects``, which is how a format published as its own
  package becomes selectable by a short name.

An open registry rather than a closed enum, because the alternative would make
out-of-tree formats library-only permanently, and opening a released config
schema afterwards costs a deprecation. It is also the half of this seam a
constructor argument cannot serve: a host that reaches dex as a subprocess can
pass a name and cannot pass an object.

What a name resolves *to* is a :class:`~.project.ProjectFactory`, and what it is
called with is a :class:`~.project.ProjectContext`. Neither is this module's
invention; see ``references/project.md``. This module only chooses.

Every refusal here is a :class:`~..errors.ConfigurationError` naming the fix,
because every one of them is a wiring mistake in how the engine was configured
rather than a bad request from a user.
"""

from __future__ import annotations

from ..errors import ConfigurationError
from .project import DbtProject, ExploreProject, ProjectContext, ProjectFactory

ENTRY_POINT_GROUP = "exmergo_dex_core.projects"


#: The formats dex ships and will construct by name. Checked before anything
#: installed, so no third-party registration can take over a shipped name.
SHIPPED: dict[str, ProjectFactory] = {
    "dbt": DbtProject.from_context,
}

# The narrowest tier, and so the floor for anything selected: a format that
# cannot say what it declares cannot serve any command that reads a project.
# Which *wider* tier a given command needs is checked where the command runs (see
# DexEngine.require_editable_project), because that is where the answer depends on
# the command rather than on the configuration.
_EXPLORE_MEMBERS = ("name", "definitions")


def resolve_project_factory(name: str) -> ProjectFactory:
    """The factory ``name`` selects, or a refusal naming what to fix.

    Resolution only. The factory is not called here, which is what lets the engine
    resolve a configured name once and construct a project per command from it,
    and lets a caller that wants to inspect or wrap what a name selects do so.
    :func:`build_project` is the one that constructs.
    """

    selected = (name or "").strip()
    if not selected:
        raise ConfigurationError(
            "project.format is empty; name a shipped format "
            f"({', '.join(sorted(SHIPPED))}), a dotted path such as "
            "'mypkg.projects:my_project', or an installed entry-point name"
        )

    if selected in SHIPPED:
        return SHIPPED[selected]

    if ":" in selected:
        return _load_dotted_path(selected)

    registered = _load_entry_point(selected)
    if registered is not None:
        return registered

    raise ConfigurationError(
        f"unknown project format '{selected}'. dex ships "
        f"{', '.join(sorted(SHIPPED))}; anything else is either a dotted path "
        "('mypkg.projects:my_project', note the colon between module and name) "
        "or a name registered by an installed distribution under the "
        f"'{ENTRY_POINT_GROUP}' entry-point group. Nothing installed registers "
        f"'{selected}'"
    )


def build_project(name: str, context: ProjectContext) -> ExploreProject:
    """Resolve ``name`` and construct it, checking what comes back is a project."""

    return construct_project(name, resolve_project_factory(name), context)


def construct_project(
    name: str, factory: ProjectFactory, context: ProjectContext
) -> ExploreProject:
    """Call an already-resolved factory, checking what comes back is a project.

    Separate from :func:`build_project` because the engine resolves a name once
    and constructs per command, so the checking half has to be reachable without
    re-resolving: an entry-point scan walks every installed distribution, and
    paying that on a path that already knows its factory would be waste. Skipping
    the check instead is the more expensive mistake, and it is easy to make,
    because a factory returning the wrong thing degrades into a project that
    answers nothing rather than failing where it was configured.

    The tier check is on the constructed object rather than on the factory,
    deliberately: a factory is a callable, and a callable protocol can only
    verify that ``__call__`` exists, which every callable satisfies. The project
    tiers are genuinely structural, so building first is what makes the check
    mean anything.
    """

    try:
        project = factory(context)
    except ConfigurationError:
        # The format refused its own coordinates and said why. Re-wrapping would
        # bury the specific message under a generic one.
        raise
    except Exception as exc:
        raise ConfigurationError(
            f"the project format '{name}' failed to build: {exc}. A factory takes "
            "one ProjectContext and returns a project; check that it accepts the "
            "context dex passes and reads its coordinates out of context.options"
        ) from exc

    if not isinstance(project, ExploreProject):
        missing = [m for m in _EXPLORE_MEMBERS if not hasattr(project, m)]
        raise ConfigurationError(
            f"the project format '{name}' built a {type(project).__name__}, which "
            f"is not a project: it is missing {', '.join(missing)}. Every format "
            "has to satisfy at least "
            "exmergo_dex_core.adapters.project.ExploreProject; run "
            "the shipped conformance suite against it "
            "(exmergo_dex_core.adapters.conformance)"
        )
    return project


def _load_dotted_path(path: str) -> ProjectFactory:
    from importlib import import_module

    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        raise ConfigurationError(
            f"'{path}' is not a usable dotted path; write it as "
            "'module.path:name', with a colon between the module and the factory"
        )
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"project format '{path}' names a module that will not import: {exc}. "
            "Check the spelling, and that the distribution providing it is "
            "installed in the environment running dex"
        ) from exc

    resolved = module
    for part in attribute.split("."):
        try:
            resolved = getattr(resolved, part)
        except AttributeError as exc:
            raise ConfigurationError(
                f"project format '{path}' imported {module_name} but it has no "
                f"'{attribute}'"
            ) from exc

    if not callable(resolved):
        raise ConfigurationError(
            f"project format '{path}' resolved to a {type(resolved).__name__}, "
            "which cannot be called. Name a factory: a function taking a "
            "ProjectContext, a class whose __init__ takes one, or a classmethod "
            "like DbtProject.from_context"
        )
    return resolved


def _load_entry_point(name: str) -> ProjectFactory | None:
    """The factory an installed distribution registered under ``name``, if any.

    Imported here rather than at module scope: the CLI runs as a fresh process
    per command, and scanning installed distributions is work no invocation that
    selects a shipped format should pay for.
    """

    from importlib.metadata import entry_points

    for entry in entry_points(group=ENTRY_POINT_GROUP):
        if entry.name != name:
            continue
        try:
            loaded = entry.load()
        except Exception as exc:
            raise ConfigurationError(
                f"project format '{name}' is registered under "
                f"'{ENTRY_POINT_GROUP}' by an installed distribution, but loading "
                f"it failed: {exc}. That is a problem with the distribution "
                "providing the format, not with this configuration"
            ) from exc
        if not callable(loaded):
            raise ConfigurationError(
                f"project format '{name}' is registered under "
                f"'{ENTRY_POINT_GROUP}' but points at a "
                f"{type(loaded).__name__}, which cannot be called. The entry "
                "point has to name a factory taking a ProjectContext"
            )
        return loaded
    return None
