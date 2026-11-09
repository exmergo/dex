"""The Cube source behind the seam: built by the engine from configuration, and
importing nothing optional until it reads (#910)."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from exmergo_dex_core.cube import dependency
from exmergo_dex_core.cube.dependency import CubeDependencyError
from exmergo_dex_core.engine import DexEngine
from exmergo_dex_core.envelope import Reason, reason_for
from exmergo_dex_core.maintain.commands import _semantic_layer

from .conftest import ORDERS_LAYER, repo

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "exmergo_dex_core"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_the_engine_builds_the_cube_source_not_the_project(tmp_path):
    pytest.importorskip("sqlglot")
    from exmergo_dex_core.cube.project import CubeSemanticLayer
    from exmergo_dex_core.semantic_source import SemanticSnapshotSource

    built = DexEngine.from_repo(repo(tmp_path, *ORDERS_LAYER)).semantic_catalog_source()

    assert isinstance(built, CubeSemanticLayer)
    assert isinstance(built, SemanticSnapshotSource)


def test_a_missing_dialect_engine_is_refused_by_name_as_a_prerequisite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dependency, "find_spec", lambda name: None)
    engine = DexEngine.from_repo(repo(tmp_path, *ORDERS_LAYER))

    with pytest.raises(
        CubeDependencyError, match=r"exmergo-dex-core\[cube\]"
    ) as raised:
        engine.semantic_catalog_source()
    assert reason_for(raised.value) is Reason.PREREQUISITE


def test_maintain_does_not_fingerprint_metricflow_in_cube_s_place():
    class Project:
        def semantic_layer(self):
            raise AssertionError("read the MetricFlow layer under vendor: cube")

    engine = SimpleNamespace(
        config=SimpleNamespace(semantic=SimpleNamespace(vendor="cube")),
        semantic_catalog_source=lambda: SimpleNamespace(semantic_catalog=None),
    )
    assert _semantic_layer(engine, Project()) is None


def test_the_extra_is_the_dialect_engine_and_all_covers_it():
    extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]

    assert extras["cube"] == ["exmergo-dex-core[sql]"]
    assert "cube" in extras["all"][0]


def _imports_sqlglot(path: Path) -> bool:
    return any(
        line.strip().startswith(("import sqlglot", "from sqlglot"))
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_only_the_lineage_module_imports_sqlglot():
    """A source scan, because an import inside a function is invisible to a
    `sys.modules` probe until that function runs."""

    importers = {
        path.relative_to(PACKAGE).as_posix()
        for path in (PACKAGE / "cube").rglob("*.py")
        if _imports_sqlglot(path)
    }
    assert importers == {"cube/lineage.py"}
    assert not _imports_sqlglot(PACKAGE / "explore" / "semantic" / "cube.py")


def test_reading_the_config_and_building_the_seam_import_no_dialect_engine(tmp_path):
    """What `dex config validate` and source construction reach: neither may
    pull sqlglot in, so a base install can refuse `vendor: cube` by name."""

    root = repo(tmp_path, *ORDERS_LAYER)
    code = (
        "import sys\n"
        "from exmergo_dex_core.config import load_config\n"
        "from exmergo_dex_core.cube import project, catalog, snapshot, loader\n"
        f"load_config({str(root)!r})\n"
        "assert 'sqlglot' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603
