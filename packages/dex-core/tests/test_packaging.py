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


def _run_isolated(
    wheel: str,
    code: str,
    *,
    extras: list[str] | None = None,
    pins: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``code`` against the built wheel in a fresh environment.

    ``--isolated --no-project`` keeps the repo's own virtualenv, lockfile, and
    settings out of it, so what runs is the wheel and its declared dependencies
    and nothing else. That isolation is the whole point: without it the dev
    environment would satisfy imports the wheel never declared.

    ``pins`` adds extra requirements to the resolve, which is how a test can hold
    a declared dependency at its floor instead of taking whatever the resolver
    would otherwise pick (always the newest release, so always the version least
    likely to expose a stale lower bound).
    """

    spec = f"exmergo-dex-core[{','.join(extras)}] @ {wheel}" if extras else wheel
    withs = [
        arg for requirement in [spec, *(pins or [])] for arg in ("--with", requirement)
    ]
    return subprocess.run(  # noqa: S603  (a fixed argv, no shell)
        [
            _uv(),
            "run",
            "--isolated",
            "--no-project",
            *withs,
            "python",
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        cwd=PACKAGE_ROOT.parent,
    )


def _project_metadata() -> dict:
    """The `[project]` table as declared, so a test asserts against the numbers and
    names the wheel actually ships rather than copies that can drift from them."""

    import tomllib

    parsed = tomllib.loads(
        (PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return parsed["project"]


def _declared_sqlglot_floor() -> str:
    """The floor from the one place sqlglot is pinned, the `[sql]` extra.

    Also asserts the pin is bounded at both ends. The ceiling is not decoration:
    the firewall matches sqlglot expression classes by name at module scope, so an
    open upper bound means a future major can break every new install on something
    no test here could have seen. If this assertion ever fails, read the `[sql]`
    comment in pyproject.toml before deleting it.
    """

    extras = _project_metadata()["optional-dependencies"]
    pins = [d for d in extras["sql"] if d.startswith("sqlglot")]
    assert len(pins) == 1, f"expected exactly one sqlglot pin in [sql], got {pins}"
    floor, _, ceiling = pins[0].removeprefix("sqlglot>=").partition(",")
    assert floor and ceiling.startswith("<"), (
        f"expected a floor and a ceiling, got {pins[0]}"
    )
    return floor


def test_a_bare_install_imports_and_exposes_the_api(wheel: str):
    """No extras at all. The connector clients live behind extras, so an
    `__init__` that imported them eagerly would fail here while every other test
    in this suite passed."""

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


def test_an_install_that_cannot_parse_sql_refuses_and_names_the_fix(wheel: str):
    """A guarded command on an install with no connector extra must refuse with the
    install to run, not die on an import.

    Absent the dialect engine dex cannot promise a query is read-only, so refusing
    is the only safe answer and there is deliberately no weaker fallback. What is
    tested here is that the refusal is *usable*: the earlier failure surfaced as
    `No module named 'sqlglot'` inside an error envelope, which reads as a broken
    environment rather than a missing extra, and cost a consumer a day of
    diagnosis. The message has to name the extra instead.
    """

    import json

    done = _run_isolated(
        wheel, "from exmergo_dex_core.cli import main;main(['explore', 'inventory'])"
    )
    envelope = json.loads(done.stdout)
    assert envelope["status"] == "error", done.stdout
    message = envelope["errors"][0]
    assert "sqlglot" in message and "exmergo-dex-core[duckdb]" in message, message
    assert "No module named" not in message, (
        f"the refusal must name the extra, not the missing module: {message}"
    )

    # And the guard is reached before anything is opened or priced, so asking is
    # free: `ensure_available` must not need the thing whose absence it reports.
    done = _run_isolated(
        wheel,
        "from exmergo_dex_core.guards.dialect import ensure_available,"
        " DialectDependencyError;"
        "\ntry: ensure_available(); print('unexpectedly available')"
        "\nexcept DialectDependencyError: print('refused cleanly')",
    )
    assert done.returncode == 0, done.stderr
    assert "refused cleanly" in done.stdout


def test_the_declared_sqlglot_floor_is_high_enough_for_the_guards(wheel: str):
    """Installed at exactly the declared floor, the guards must import.

    The firewall's unnest allowlist names sqlglot expression classes at module
    scope, so a floor below the release that introduced one of them turns into an
    `AttributeError` on import for any user whose resolver picks an older sqlglot.
    A `>=` bound that the code has outgrown is the same bug as an undeclared
    dependency, and neither shows up when tests run against the newest release.

    The companion risk, a *ceiling* the code has outgrown, cannot be tested here
    because the breaking release does not exist yet. The `sqlglot-canary` CI job
    covers that direction by running the guards against the newest sqlglot.
    """

    done = _run_isolated(
        wheel,
        "import sqlglot;"
        "from exmergo_dex_core.guards import query_firewall, sql_guard;"
        "import exmergo_dex_core.explore.commands, exmergo_dex_core.maintain.drift;"
        "print(sqlglot.__version__)",
        extras=["duckdb"],
        pins=[f"sqlglot=={_declared_sqlglot_floor()}"],
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith(_declared_sqlglot_floor())


def test_the_hosted_semantic_backend_needs_only_its_own_extra(wheel: str):
    """`[semantic-api]` is the whole install for a pure-remote user: dbt Cloud owns
    the warehouse connection and executes server-side, so there is no local
    project, no warehouse client, no dbt-core, and no SQL for dex to parse.

    The first assertion is that sqlglot is genuinely absent. Without it the rest of
    this test passes for the wrong reason the moment anything puts a dialect engine
    back in this environment, which is exactly the regression that shipped once
    already.
    """

    import json

    done = _run_isolated(
        wheel,
        "from exmergo_dex_core.explore.semantic.hosted import HostedDbtCloudBackend;"
        "\ntry: import sqlglot; raise SystemExit('sqlglot must not be installed here')"
        "\nexcept ImportError: pass"
        "\nprint(HostedDbtCloudBackend.__name__)",
        extras=["semantic-api"],
    )
    assert done.returncode == 0, done.stderr
    assert "HostedDbtCloudBackend" in done.stdout

    # Both entry points, because they route differently: the CLI through its own
    # dispatch, the library through DexEngine. Each must reach the backend and come
    # back with the real refusal (coordinates it cannot invent), never an
    # ImportError for something this extra does not name.
    done = _run_isolated(
        wheel,
        "from exmergo_dex_core.cli import main;main(['explore', 'semantic', '--api'])",
        extras=["semantic-api"],
    )
    envelope = json.loads(done.stdout)
    assert envelope["status"] == "error", done.stdout
    assert "environment id" in envelope["errors"][0]

    done = _run_isolated(
        wheel,
        "from exmergo_dex_core import DexEngine, DexConfig;"
        "from exmergo_dex_core.explore.semantic import SemanticBackendError;"
        "eng = DexEngine(config=DexConfig(connector='duckdb'));"
        "\ntry: eng.semantic_list(api=True); print('unexpectedly succeeded')"
        "\nexcept SemanticBackendError as exc: print('refused:', exc)",
        extras=["semantic-api"],
    )
    assert done.returncode == 0, done.stderr
    assert "environment id" in done.stdout, done.stdout


def test_the_all_extra_installs_every_optional_capability(wheel: str):
    """`[all]` has to mean all of them, and they have to co-resolve.

    Two failure modes, one test, because either one alone makes the promise false.
    The extra self-references the others rather than restating their requirement
    lists, which keeps each client list defined once but makes the reference list
    the part that silently rots: an extra added later is simply absent from `[all]`
    and nothing complains. It named only the six connectors for exactly that
    reason, leaving both semantic backends and clustering out of an install
    documented as everything. Then, because this is the only install that puts six
    dbt adapters and MetricFlow in one environment, it is also the only place a
    version conflict between them can surface at all.

    `dev` is excluded deliberately: contributor tooling, not a capability.
    """

    extras = _project_metadata()["optional-dependencies"]
    assert len(extras["all"]) == 1, f"[all] should be one self-reference: {extras}"
    referenced = set(
        extras["all"][0].removeprefix("exmergo-dex-core[").removesuffix("]").split(",")
    )
    assert referenced == set(extras) - {"all", "dev"}, (
        f"[all] does not cover {sorted(set(extras) - {'all', 'dev'} - referenced)}"
    )

    # Every client the extras exist to deliver, plus each dbt adapter, since a
    # co-resolution that installs but cannot import an adapter is still broken.
    done = _run_isolated(
        wheel,
        "import duckdb, google.cloud.bigquery, snowflake.connector, psycopg;"
        "import redshift_connector, databricks.sql, sklearn, httpx, metricflow;"
        "import sqlglot;"
        "import dbt.adapters.duckdb, dbt.adapters.bigquery, dbt.adapters.snowflake;"
        "import dbt.adapters.postgres, dbt.adapters.redshift, dbt.adapters.databricks;"
        "from exmergo_dex_core.explore.semantic.hosted import HostedDbtCloudBackend;"
        "from exmergo_dex_core.explore.semantic.local import LocalMetricFlowBackend;"
        "print('every capability imported')",
        extras=["all"],
    )
    assert done.returncode == 0, done.stderr
    assert "every capability imported" in done.stdout


def test_the_local_semantic_read_view_needs_no_connector_extra(wheel: str):
    """`explore semantic list --local` is a read of the compiled manifest, so it is
    documented as needing no extra, and it has to actually hold.

    It shares a command module with the hosted backend for that reason. Reaching it
    must produce the refusal that belongs to the missing *project* (there is no
    manifest in a temp directory), not a refusal about a missing dialect engine or
    connector client, which is what a shared module with the rest of explore would
    have produced.
    """

    import json

    done = _run_isolated(
        wheel,
        "from exmergo_dex_core.cli import main;"
        "main(['explore', 'semantic', '--local'])",
    )
    envelope = json.loads(done.stdout)
    assert envelope["status"] == "error", done.stdout
    message = envelope["errors"][0]
    assert "sqlglot" not in message and "No module named" not in message, message


def test_the_quickstart_example_runs_against_the_installed_package(wheel: str):
    """The documented example, run as a consumer runs it.

    Testing the example rather than a copy of it means the example cannot rot:
    it is executable documentation, and a change that breaks the usage we
    publish fails here.
    """

    done = _run_isolated(
        wheel, QUICKSTART.read_text(encoding="utf-8"), extras=["duckdb"]
    )
    assert done.returncode == 0, done.stderr

    out = done.stdout
    # The flow really ran: a map, the inferred join, the PII flag, a query.
    assert "mapped 2 objects, 1 relationship(s)" in out
    assert "join: shop.main.orders.customer_id -> shop.main.customers.id" in out
    assert "PII: shop.main.customers.email is email" in out
    # Exact cells, which is only stable because the example's query orders its
    # groups. A bare GROUP BY returns them in whatever order the hash aggregate
    # produced, so this assertion used to fail roughly one run in fifty.
    assert "[['EU', 2], ['US', 1]]" in out
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
