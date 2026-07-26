"""The installed package, exercised the way a consumer gets it from PyPI.

Every other test imports `exmergo_dex_core` from the source tree, which is a
different thing from what `pip install` produces: it cannot see a module missing
from the wheel, a dependency that is only present because the dev environment
happens to have it, or an `__init__` that imports something living behind an
extra. That last one is not hypothetical: this package's exports are resolved
lazily precisely so a bare install imports, and nothing else in the suite would
notice if that regressed.

So these build a real wheel, install it into an isolated environment, and run
code against it as a consumer would. They are the slowest tests here (a wheel
build plus a resolve), hence the `packaging` marker: `-m "not packaging"` skips
them, and CI runs them because a broken install is a broken release.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
QUICKSTART = PACKAGE_ROOT / "examples" / "quickstart.py"


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:  # pragma: no cover - uv is the documented toolchain
        pytest.skip("uv is not on PATH; it builds and isolates the environment")
    return found


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Build the distribution once and reuse it across these tests."""

    out = tmp_path_factory.mktemp("dist")
    subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [_uv(), "build", "--wheel", "--out-dir", str(out), str(PACKAGE_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    built = list(out.glob("*.whl"))
    assert len(built) == 1, built
    return str(built[0])


def _run_isolated(wheel: str, code: str, *extras: str) -> subprocess.CompletedProcess:
    """Run ``code`` against the built wheel in a fresh environment.

    ``--isolated --no-project`` keeps the repo's own virtualenv, lockfile, and
    settings out of it, so what runs is the wheel and its declared dependencies
    and nothing else. That isolation is the whole point: without it the dev
    environment would satisfy imports the wheel never declared.
    """

    spec = f"exmergo-dex-core[{','.join(extras)}] @ {wheel}" if extras else wheel
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [
            _uv(),
            "run",
            "--isolated",
            "--no-project",
            "--with",
            spec,
            "python",
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        cwd=PACKAGE_ROOT.parent,
    )


def test_a_bare_install_imports_and_exposes_the_api(wheel: str):
    """No extras at all. The connector clients and the dialect engine live
    behind extras, so an `__init__` that imported them eagerly would fail here
    while every other test in this suite passed."""

    done = _run_isolated(
        wheel,
        "import exmergo_dex_core as dex;"
        """print(
            dex.DexEngine.__name__, dex.DexConfig.__name__, dex.MemoryStore.__name__
        );"""
        "print(dex.__version__)",
    )
    assert done.returncode == 0, done.stderr
    assert "DexEngine DexConfig MemoryStore" in done.stdout


def test_importing_the_package_pulls_in_no_connector_client(wheel: str):
    """Import stays cheap and extra-free. The CLI runs as a fresh subprocess per
    command, so anything imported at package level is latency on every call."""

    done = _run_isolated(
        wheel,
        "import sys;"
        "before={m.split('.')[0] for m in sys.modules};"
        "import exmergo_dex_core;"
        "clients={'google','snowflake','databricks','psycopg','redshift_connector',"
        "'duckdb','sqlglot','sklearn','httpx','metricflow'};"
        "print(sorted(({m.split('.')[0] for m in sys.modules} - before) & clients))",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_the_quickstart_example_runs_against_the_installed_package(wheel: str):
    """The documented example, run as a consumer runs it.

    Testing the example rather than a copy of it means the example cannot rot:
    it is executable documentation, and a change that breaks the usage we
    publish fails here.
    """

    done = _run_isolated(wheel, QUICKSTART.read_text(encoding="utf-8"), "duckdb")
    assert done.returncode == 0, done.stderr

    out = done.stdout
    # The flow really ran: a map, the inferred join, the PII flag, a query.
    assert "mapped 2 objects, 1 relationship(s)" in out
    assert "join: shop.main.orders.customer_id -> shop.main.customers.id" in out
    assert "PII: shop.main.customers.email is email" in out
    assert "[['US', 1], ['EU', 2]]" in out
    assert "refused, as designed" in out
    # PII stayed flagged and never surfaced, all the way out to stdout.
    assert "@example.com" not in out
    # And the default store wrote nothing beside the warehouse.
    assert "files in the workspace: ['shop.duckdb']" in out


def test_the_installed_console_script_speaks_the_command_contract(wheel: str):
    """`dex` is a console script the wheel declares, and every agent wrapper
    invokes it expecting exactly one JSON envelope on stdout and nothing else.

    `viz preview` is the command to prove that with: it is scaffolded against
    the contract but not yet implemented, so it returns a real envelope without
    needing a warehouse, a config, or a credential.
    """

    import json

    done = subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [
            _uv(),
            "run",
            "--isolated",
            "--no-project",
            "--with",
            wheel,
            "dex",
            "viz",
            "preview",
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("\n") == 1, "exactly one line on stdout"
    envelope = json.loads(done.stdout)
    assert envelope["status"] == "not_implemented"
    assert envelope["data"]["command"] == "viz preview"
