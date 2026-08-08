"""Naming a project format: what resolves, what refuses, and what each says.

The construction contract is the half of the project seam a host reaching dex as
a subprocess can actually use, so these assertions are about reachability rather
than about reading a project. Every refusal below is a wiring mistake, and every
message has to name the fix, because the reader hit it while editing a config file
and has no traceback worth reading.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from exmergo_dex_core.adapters.project import (
    DbtProject,
    EditableProject,
    ProjectContext,
    tier_of,
)
from exmergo_dex_core.adapters.project_resolver import (
    ENTRY_POINT_GROUP,
    SHIPPED,
    build_project,
    resolve_project_factory,
)
from exmergo_dex_core.config import DexConfig, ProjectConfig
from exmergo_dex_core.dbt_project import ProjectDefinitions
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.errors import ConfigurationError, RepoRootRequiredError

_PROJECT_YML = 'name: dex_test\nversion: "1.0.0"\nprofile: dex_test\n'


def _dbt_project(root: Path) -> Path:
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text(_PROJECT_YML, encoding="utf-8")
    return root


class _Recorded:
    """A format built from options alone, keyed by nothing on the filesystem."""

    name = "recorded"

    def __init__(self, context: ProjectContext) -> None:
        self.context = context

    def definitions(self) -> ProjectDefinitions:
        return ProjectDefinitions(notes=[f"built with {dict(self.context.options)}"])


@pytest.fixture
def registered(monkeypatch):
    """A format reachable by dotted path, installed the way a host would install it."""

    module = types.ModuleType("dex_conformance_format")
    module.my_project = _Recorded
    monkeypatch.setitem(sys.modules, "dex_conformance_format", module)
    return module


# --- what resolves ------------------------------------------------------------


def test_the_shipped_name_builds_the_dbt_format(tmp_path: Path):
    root = str(_dbt_project(tmp_path))
    project = build_project("dbt", ProjectContext(repo_root=root))

    assert isinstance(project, DbtProject)
    assert tier_of(project) == 3


def test_a_dotted_path_builds_a_format_dex_does_not_ship(registered):
    project = build_project(
        "dex_conformance_format:my_project",
        ProjectContext(options={"graph": "orders"}),
    )

    assert project.name == "recorded"
    assert "orders" in project.definitions().notes[0]


def test_options_reach_the_factory_verbatim(registered):
    """dex does not interpret options, so what the config said is what arrives.

    A resolver that normalized, filtered, or lower-cased on the way through would
    make a format's own validation a lie, and the format is the only party that
    knows what its keys mean.
    """

    options = {"Tenant": "ACME", "nested": {"a": [1, 2]}}

    project = build_project(
        "dex_conformance_format:my_project", ProjectContext(options=options)
    )

    assert dict(project.context.options) == options


def test_a_shipped_name_cannot_be_shadowed_by_a_registration(monkeypatch):
    """Installing a package can never silently move where dex reads a project from.

    The same rule the store resolver holds, and it matters more here: a shadowed
    project format would have dex reasoning about a different set of models than
    the repo declares, and nothing in the output would say so.
    """

    def hijack(group):
        entry = types.SimpleNamespace(name="dbt", load=lambda: _Recorded)
        return [entry] if group == ENTRY_POINT_GROUP else []

    monkeypatch.setattr("importlib.metadata.entry_points", hijack)

    assert resolve_project_factory("dbt") is SHIPPED["dbt"]


# --- what refuses, and what it says -------------------------------------------


def test_an_empty_name_refuses_rather_than_defaulting():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_project_factory("  ")

    assert "project.format is empty" in str(refusal.value)
    assert "dbt" in str(refusal.value)


def test_an_unknown_name_lists_the_three_ways_to_name_one():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_project_factory("sqlmesh")

    message = str(refusal.value)
    assert "unknown project format 'sqlmesh'" in message
    assert "dotted path" in message
    assert ENTRY_POINT_GROUP in message


def test_a_dotted_path_without_a_colon_says_where_the_colon_goes():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_project_factory("mypkg.projects.my_project:")

    assert "'module.path:name'" in str(refusal.value)


def test_a_module_that_will_not_import_names_the_environment():
    with pytest.raises(ConfigurationError) as refusal:
        resolve_project_factory("dex_no_such_module:my_project")

    assert "will not import" in str(refusal.value)
    assert "installed in the environment running dex" in str(refusal.value)


def test_a_missing_attribute_names_the_module_it_did_import(registered):
    with pytest.raises(ConfigurationError) as refusal:
        resolve_project_factory("dex_conformance_format:absent")

    assert "has no 'absent'" in str(refusal.value)


def test_a_factory_that_builds_something_else_names_what_is_missing(registered):
    registered.not_a_project = lambda context: object()

    with pytest.raises(ConfigurationError) as refusal:
        build_project("dex_conformance_format:not_a_project", ProjectContext())

    message = str(refusal.value)
    assert "is not a project" in message
    assert "definitions" in message
    assert "conformance" in message


def test_a_formats_own_refusal_survives_intact(tmp_path: Path):
    """The format said why, and a generic wrapper would bury it.

    The dbt format refuses options it cannot honor by naming them. Re-wrapping
    that as "failed to build" would leave the reader with no idea which config
    line to delete.
    """

    with pytest.raises(ConfigurationError) as refusal:
        build_project(
            "dbt",
            ProjectContext(repo_root=str(tmp_path), options={"warehouse": "x"}),
        )

    assert "takes no options, and got: warehouse" in str(refusal.value)


def test_the_dbt_format_refuses_to_build_without_a_repo_root():
    with pytest.raises(RepoRootRequiredError) as refusal:
        build_project("dbt", ProjectContext())

    assert "git-reviewable filesystem artifact" in str(refusal.value)


# --- how the engine selects ---------------------------------------------------


def test_from_repo_resolves_the_configured_format(tmp_path: Path, registered):
    _dbt_project(tmp_path)
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        "project:\n"
        "  format: dex_conformance_format:my_project\n"
        "  options:\n"
        "    graph: orders\n",
        encoding="utf-8",
    )

    engine = DexEngine.from_repo(tmp_path)

    assert engine.project_format().name == "recorded"


def test_an_injected_project_wins_over_the_configured_name(tmp_path: Path):
    """The instance a host built is the decision configuration exists to make.

    Same precedence as ``store=``, and for the same reason: a host holding an
    object has already answered the question, and letting a config file override
    it would mean a committed file could redirect a host's own project.
    """

    injected = _Recorded(ProjectContext())

    engine = DexEngine.from_repo(
        _dbt_project(tmp_path), project_format=injected, connector="duckdb"
    )

    assert engine.project_format() is injected


def test_the_flag_overrides_the_configured_format_and_leaves_its_options(
    tmp_path: Path, registered
):
    """Options are not namespaced by format, so they do not travel to another one.

    The same rule ``--cache-backend`` holds. Carrying them would hand the dbt
    format another format's coordinates, and the dbt format refuses options
    outright, so the override would refuse rather than do the one useful thing it
    is for: falling back to the shipped format for a single command.
    """

    _dbt_project(tmp_path)
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\n"
        "project:\n"
        "  format: dex_conformance_format:my_project\n"
        "  options:\n"
        "    graph: orders\n",
        encoding="utf-8",
    )

    engine = DexEngine.from_repo(tmp_path, project_format_name="dbt")

    assert isinstance(engine.project_format(), DbtProject)


def test_a_project_dex_built_is_not_held_across_commands(tmp_path: Path):
    """Two reads give two instances, which is what keeps a stale view impossible.

    The memo that makes routing maintain through the tier affordable lives on the
    instance, so an instance held across commands would serve the project as it
    was before ``transform apply`` rewrote it. A host-supplied instance is held,
    because its freshness is the host's to know.
    """

    engine = DexEngine.from_repo(_dbt_project(tmp_path), connector="duckdb")

    assert engine.project_format() is not engine.project_format()


def test_a_tier_two_format_is_not_editable(tmp_path: Path):
    """``editable_project()`` answers ``None`` rather than raising.

    ``reconcile`` is the caller, and a format declining the write tier is a
    supported answer that degrades to proposal-only, not a refusal.
    """

    engine = DexEngine(project_format=_Recorded(ProjectContext()))

    assert engine.editable_project() is None
    assert isinstance(
        DexEngine.from_repo(
            _dbt_project(tmp_path), connector="duckdb"
        ).editable_project(),
        EditableProject,
    )


def test_the_engine_checks_the_tier_floor_on_what_a_name_built(
    tmp_path: Path, registered
):
    """The floor check has to be on the engine's path, not only on `build_project`.

    The engine resolves a configured name once and constructs per command, so it
    holds a factory rather than calling `build_project`. Calling that factory
    directly is the obvious shortcut and it silently skips the check: a factory
    returning the wrong thing then becomes a project that answers nothing, and the
    first thing the operator sees is a command warning about a missing tier, with
    no mention of the config line that caused it. Found by running a deliberately
    broken factory through the CLI, which is the only place the two paths differ.
    """

    registered.not_a_project = lambda context: object()
    _dbt_project(tmp_path)
    (tmp_path / ".dex").mkdir()
    (tmp_path / ".dex" / "config.yml").write_text(
        "connector: duckdb\nproject:\n  format: dex_conformance_format:not_a_project\n",
        encoding="utf-8",
    )

    engine = DexEngine.from_repo(tmp_path)

    with pytest.raises(ConfigurationError) as refusal:
        engine.project_format()

    assert "is not a project" in str(refusal.value)


def test_a_format_reaching_no_tier_is_reported_as_tier_zero():
    """The note counts tiers rather than naming the one above the gap.

    Saying "implements only the explore tier" of something implementing nothing
    sends the reader to look for the method they already have.
    """

    engine = DexEngine(project_format=object())

    assert engine.maintain_project() is None
    assert "reaches tier 0 of 3" in engine.project_tier_note()


def test_the_default_config_selects_the_shipped_format():
    """A repo that configures nothing behaves exactly as it did before the setting.

    Worth an assertion rather than an assumption: this default is the entire
    backward-compatibility story for the config schema change.
    """

    assert ProjectConfig().format == "dbt"
    assert DexConfig().project.format == "dbt"
