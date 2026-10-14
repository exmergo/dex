"""The SLayer semantic source and the `[slayer]` extra behind it (#596).

What this release promises under `semantic.vendor: slayer`: the coordinates are
checked again where the source is built, a missing or mismatched extra is a named
prerequisite rather than an ImportError, MetricFlow is not read in SLayer's
place, and the extra stays pinned to exactly the release the code names.

Most of this runs without SLayer installed, by standing in for the installed
distribution. The tests at the bottom need the real extra and skip without it;
the `slayer-sqlglot` CI job is where they run.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Reason, reason_for
from exmergo_dex_core.errors import (
    ConfigurationError,
    ProjectError,
    RepoRootRequiredError,
)
from exmergo_dex_core.explore.semantic import SemanticBackendError
from exmergo_dex_core.explore.semantic.backend import resolve_semantic_layer
from exmergo_dex_core.maintain.commands import _semantic_layer
from exmergo_dex_core.semantic_source import (
    SemanticCatalogSource,
    SemanticSnapshotSource,
    SemanticSourceContext,
)
from exmergo_dex_core.slayer import dependency
from exmergo_dex_core.slayer.dependency import (
    SLAYER_DISTRIBUTION,
    SLAYER_VERSION,
    SlayerDependencyError,
    ensure_available,
)
from exmergo_dex_core.slayer.project import SlayerSemanticLayer, build_semantic_layer

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
MODELS = ["models/warehouse/orders.yaml"]
CONFIG = """connector: duckdb
semantic:
  vendor: slayer
  slayer:
    base_dir: semantics/slayer
    models:
      - models/warehouse/orders.yaml
"""

_HAS_SLAYER = dependency.installed_version() is not None


@pytest.fixture
def installed(monkeypatch):
    """Stand in for the pinned release being installed."""

    monkeypatch.setattr(dependency, "installed_version", lambda: SLAYER_VERSION)
    monkeypatch.setattr(dependency, "find_spec", lambda name: object())


@pytest.fixture
def absent(monkeypatch):
    """Stand in for an install with no SLayer, whatever this one has."""

    monkeypatch.setattr(dependency, "installed_version", lambda: None)


def _context(root, **options) -> SemanticSourceContext:
    return SemanticSourceContext(
        repo_root=str(root),
        connector="duckdb",
        options={"base_dir": "semantics/slayer", "models": MODELS, **options},
    )


def _engine(root: Path) -> DexEngine:
    (root / ".dex").mkdir(parents=True, exist_ok=True)
    (root / ".dex" / "config.yml").write_text(CONFIG, encoding="utf-8")
    return DexEngine.from_repo(root)


# --- the dependency check ----------------------------------------------------------


def test_a_missing_extra_is_a_named_prerequisite(absent):
    with pytest.raises(SlayerDependencyError) as raised:
        ensure_available()

    message = str(raised.value)
    assert 'pip install "exmergo-dex-core[slayer]"' in message
    assert reason_for(raised.value) is Reason.PREREQUISITE


def test_a_different_slayer_release_is_refused_naming_both(monkeypatch):
    monkeypatch.setattr(dependency, "installed_version", lambda: "1.1.2")
    monkeypatch.setattr(dependency, "find_spec", lambda name: object())

    with pytest.raises(SlayerDependencyError) as raised:
        ensure_available()

    assert SLAYER_VERSION in str(raised.value)
    assert f"{SLAYER_DISTRIBUTION} 1.1.2" in str(raised.value)


def test_an_unrelated_slayer_module_does_not_count(monkeypatch):
    """`import slayer` answering is not enough: the PyPI project named `slayer` is
    a different one, so the check asks for the distribution by its own name."""

    monkeypatch.setattr(dependency, "installed_version", lambda: None)
    monkeypatch.setattr(dependency, "find_spec", lambda name: object())

    with pytest.raises(SlayerDependencyError):
        ensure_available()


def test_checking_never_imports_slayer(absent):
    import sys

    with pytest.raises(SlayerDependencyError):
        ensure_available()
    assert "slayer" not in sys.modules or _HAS_SLAYER


# --- the source --------------------------------------------------------------------


def test_the_source_builds_from_its_coordinates(tmp_path, installed):
    source = build_semantic_layer(_context(tmp_path))

    assert isinstance(source, SlayerSemanticLayer)
    assert source.model_paths() == [
        tmp_path / "semantics" / "slayer" / "models" / "warehouse" / "orders.yaml"
    ]


def test_the_source_answers_a_catalog_and_declines_the_baseline(tmp_path, installed):
    """Not a snapshot source, so `maintain` reports the baseline absent instead
    of fingerprinting something else in its place."""

    source = build_semantic_layer(_context(tmp_path))

    assert isinstance(source, SemanticCatalogSource)
    assert not isinstance(source, SemanticSnapshotSource)
    with pytest.raises(ProjectError, match="does not read SLayer models"):
        source.semantic_catalog()


def test_building_without_the_extra_refuses_by_name(tmp_path, absent):
    with pytest.raises(SlayerDependencyError):
        build_semantic_layer(_context(tmp_path))


def test_a_bad_coordinate_is_reported_before_the_install(tmp_path, absent):
    with pytest.raises(ConfigurationError, match="is a pattern"):
        build_semantic_layer(_context(tmp_path, models=["models/w/*.yaml"]))


@pytest.mark.parametrize(
    ("options", "named"),
    [
        ({"models": ["datasources/pg.yaml"]}, "is not a model"),
        ({"base_dir": "/abs"}, "inside the repository"),
        ({"models": []}, "needs semantic.slayer.base_dir and semantic.slayer.models"),
        ({"files": ["a.ossie.yaml"]}, "takes `base_dir` and `models`, and got: files"),
    ],
)
def test_the_source_refuses_coordinates_the_config_would(
    tmp_path, installed, options, named
):
    with pytest.raises(ConfigurationError, match=named):
        build_semantic_layer(_context(tmp_path, **options))


def test_the_source_needs_a_repository(installed):
    with pytest.raises(RepoRootRequiredError):
        SlayerSemanticLayer.from_context(
            SemanticSourceContext(options={"base_dir": "s", "models": MODELS})
        )


# --- through the engine ------------------------------------------------------------


def test_the_engine_reports_a_missing_extra_as_a_prerequisite(tmp_path, absent):
    """Construction errors are re-wrapped as configuration problems unless they
    are dex's own refusals, and a missing extra is not fixed by editing config."""

    engine = _engine(tmp_path)
    with pytest.raises(SlayerDependencyError) as raised:
        engine.semantic_catalog_source()
    assert reason_for(raised.value) is Reason.PREREQUISITE


def test_the_engine_builds_the_slayer_source_not_the_project(tmp_path, installed):
    assert isinstance(_engine(tmp_path).semantic_catalog_source(), SlayerSemanticLayer)


def test_maintain_does_not_fingerprint_metricflow_in_slayer_s_place():
    """A vendor source that declines the baseline leaves it absent. Falling
    through to the project would report the MetricFlow layer's drift as
    SLayer's."""

    class Project:
        def semantic_layer(self):
            raise AssertionError("read the MetricFlow layer under vendor: slayer")

    engine = SimpleNamespace(
        config=SimpleNamespace(semantic=SimpleNamespace(vendor="slayer")),
        semantic_catalog_source=lambda: SimpleNamespace(semantic_catalog=None),
    )
    assert _semantic_layer(engine, Project()) is None


def test_explore_semantic_names_the_missing_backend(tmp_path, installed):
    with pytest.raises(SemanticBackendError, match="no `explore semantic` backend"):
        resolve_semantic_layer(_engine(tmp_path))


# --- the pin -----------------------------------------------------------------------


def _extras() -> dict[str, list[str]]:
    parsed = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return parsed["project"]["optional-dependencies"]


def test_the_extra_pins_exactly_the_release_the_code_reads():
    pins = [d for d in _extras()["slayer"] if d.startswith(SLAYER_DISTRIBUTION)]
    assert pins == [f"{SLAYER_DISTRIBUTION}=={SLAYER_VERSION}"]


def test_the_extra_carries_the_dialect_ceiling():
    """SLayer brings sqlglot either way; [sql] is what bounds it to a version
    dex's guards were run against."""

    assert "exmergo-dex-core[sql]" in _extras()["slayer"]


def _imports_slayer(path: Path) -> bool:
    return any(
        line.strip().startswith(("import slayer", "from slayer"))
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_no_core_module_imports_slayer():
    """SLayer is imported from `exmergo_dex_core.slayer` or nowhere. A source scan,
    because an import inside a function is invisible to a `sys.modules` probe
    until that function runs."""

    package = Path(__file__).resolve().parents[2] / "src" / "exmergo_dex_core"
    offenders = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if path.relative_to(package).parts[0] != "slayer" and _imports_slayer(path)
    ]
    assert offenders == []


def test_the_check_and_the_seam_import_no_slayer():
    """What every command under `vendor: slayer` reaches before the extra is
    known to be there. A reader that imports SLayer belongs in a module of its
    own, reached only through a source that was allowed to build."""

    package = Path(__file__).resolve().parents[2] / "src" / "exmergo_dex_core"
    for name in ("__init__.py", "dependency.py", "project.py"):
        assert not _imports_slayer(package / "slayer" / name), name


# --- with the real extra (the `slayer-sqlglot` CI job) -------------------------------

needs_slayer = pytest.mark.skipif(
    not _HAS_SLAYER, reason="the [slayer] extra is not installed"
)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3])


@needs_slayer
def test_the_installed_slayer_is_the_pinned_release():
    ensure_available()
    assert dependency.installed_version() == SLAYER_VERSION


@needs_slayer
def test_the_installed_sqlglot_sits_inside_the_dialect_bounds():
    import sqlglot

    (pin,) = [d for d in _extras()["sql"] if d.startswith("sqlglot")]
    floor, ceiling = pin.removeprefix("sqlglot>=").split(",<")
    assert _version(floor) <= _version(sqlglot.__version__) < _version(ceiling)


@needs_slayer
def test_slayer_s_model_format_imports_beside_dex_s_guards():
    import exmergo_dex_core.guards.query_firewall  # noqa: F401

    assert importlib.util.find_spec("slayer.core.models") is not None
    import slayer.core.models  # noqa: F401
