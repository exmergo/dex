"""One supported Python range, stated the same way everywhere (#1134).

Dex supports the Pythons CI tests, 3.11 to 3.14 today. The range is written in
several places, and each one matters to a different reader:

- each skill wrapper's PEP 723 `requires-python`, the one that carries the
  ceiling, because uv picks the interpreter from it: on a machine whose newest
  Python is unsupported, the wrapper gets a supported one rather than building
  wheel-less dependencies from source;
- the engine's `requires-python`, which keeps the floor and deliberately no
  ceiling (under one, pip on a newer Python falls back to an older dex release
  instead of refusing; pyproject.toml says so beside it), and the lock's copy;
- the classifiers PyPI shows;
- every CI matrix and every interpreter a workflow names;
- packages/dex-core/.python-version, what `uv sync` picks for a contributor (uv
  reads it only inside the project, so it cannot sit at the repository root);
- the prose a user or contributor reads first.

This holds them to one list, taken from the wrappers, so widening the range for
the next Python (the `python-next` canary says when) is one deliberate change
that this test walks through, rather than a scatter of edits that half-lands.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]
WRAPPERS = [
    ROOT / "skills" / name / "scripts" / "run.py"
    for name in ("explore", "maintain", "transform")
]
WORKFLOWS = ROOT / ".github" / "workflows"

pytestmark = pytest.mark.skipif(
    not (ROOT / "skills").is_dir() or not WORKFLOWS.is_dir(),
    reason="needs a repository checkout",
)


def _wrapper_specifier(path: Path) -> str:
    match = re.search(
        r'^# /// script\n# requires-python = "([^"]+)"\n',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, f"{path}: no PEP 723 requires-python"
    return match.group(1)


def _minors(specifier: str) -> list[str]:
    spec = SpecifierSet(specifier)
    return [f"3.{minor}" for minor in range(30) if spec.contains(f"3.{minor}.0")]


@pytest.fixture(scope="module")
def supported() -> list[str]:
    specifiers = {_wrapper_specifier(path) for path in WRAPPERS}
    assert len(specifiers) == 1, f"the wrappers disagree: {specifiers}"
    minors = _minors(specifiers.pop())
    assert minors, "the wrappers admit no Python"
    return minors


def _next(supported: list[str]) -> str:
    return f"3.{int(supported[-1].split('.')[1]) + 1}"


def test_the_wrappers_carry_a_ceiling(supported):
    spec = _wrapper_specifier(WRAPPERS[0])
    assert "<" in spec, "the ceiling is what keeps uv off an unsupported Python"
    assert not SpecifierSet(spec).contains(f"{_next(supported)}.0")


def test_the_engine_keeps_the_floor_and_no_ceiling(supported):
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    engine = project["project"]["requires-python"]
    assert engine == f">={supported[0]}", (
        "the engine states only the floor: a ceiling in published metadata makes "
        "pip on a newer Python install an older dex instead of refusing"
    )
    lock = tomllib.loads((PACKAGE / "uv.lock").read_text(encoding="utf-8"))
    assert lock["requires-python"] == engine


def test_the_classifiers_list_the_range(supported):
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    listed = [
        c.rsplit(" :: ", 1)[1]
        for c in project["project"]["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    ]
    assert listed == supported


def _matrices() -> dict[str, list[str]]:
    found = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        jobs = yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs", {})
        for name, job in jobs.items():
            matrix = (job.get("strategy") or {}).get("matrix") or {}
            if "python-version" in matrix:
                found[f"{workflow.name}:{name}"] = [
                    str(v) for v in matrix["python-version"]
                ]
    return found


def test_every_python_matrix_is_the_range(supported):
    matrices = _matrices()
    assert "ci.yml:test" in matrices
    assert {key: value for key, value in matrices.items() if value != supported} == {}


def test_every_named_interpreter_is_supported(supported):
    named = {}
    pattern = re.compile(r'(?:python-version:\s*"|--python\s+)(3\.\d+)\b')
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), 1
        ):
            for version in pattern.findall(line):
                named[f"{workflow.name}:{number}"] = version
    assert named, "expected workflows to name their interpreters"
    assert {k: v for k, v in named.items() if v not in supported} == {}


def test_the_canary_tries_the_next_python(supported):
    canary = yaml.safe_load(
        (WORKFLOWS / "upstream-deps-canaries.yml").read_text(encoding="utf-8")
    )
    assert canary["jobs"]["python-next"]["env"]["NEXT_PYTHON"] == _next(supported)


def test_contributors_default_to_a_supported_python():
    # The range rather than one version, so a contributor's `.venv` on any
    # supported Python is kept and a new one lands on the newest supported.
    pinned = (PACKAGE / ".python-version").read_text(encoding="utf-8").strip()
    assert pinned == _wrapper_specifier(WRAPPERS[0])


@pytest.mark.parametrize(
    "document",
    [
        "CONTRIBUTING.md",
        "README.md",
        "packages/dex-core/README.md",
        "docs/installation.mdx",
    ],
)
def test_the_prose_states_the_range(supported, document):
    text = (ROOT / document).read_text(encoding="utf-8")
    assert re.search(rf"Python\**\s+{supported[0]} to {supported[-1]}\b", text), (
        document
    )
    assert _next(supported) in text, (
        f"{document} should say {_next(supported)} is not supported yet"
    )
