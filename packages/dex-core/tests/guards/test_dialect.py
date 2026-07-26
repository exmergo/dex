"""The dialect-engine availability guard.

sqlglot ships with the connector extras rather than the base install, because the
hosted semantic layer parses no SQL and should not have to carry a parser. That
makes "this install cannot validate SQL" a reachable state, and these assert it is
reached the way it should be: refused, before any warehouse work, with the install
to run named, and without gating the one surface that genuinely does not need it.

Forced with monkeypatch rather than gated on what happens to be installed, so they
run in every environment including the dev one where sqlglot is always present.
"""

from __future__ import annotations

import json
import sys

import pytest

from exmergo_dex_core import cli
from exmergo_dex_core.guards import dialect


def test_a_missing_dialect_engine_names_the_install_not_the_module(monkeypatch):
    # Importing a module whose sys.modules entry is None raises ImportError, which
    # is how an absent sqlglot is simulated without uninstalling anything.
    monkeypatch.setitem(sys.modules, "sqlglot", None)

    with pytest.raises(dialect.DialectDependencyError) as caught:
        dialect.ensure_available()

    message = str(caught.value)
    # The failure a consumer actually hit read `No module named 'sqlglot'`, which
    # looks like a broken environment rather than a missing extra. The message has
    # to name the fix, and the hosted alternative for a deployment that wants one.
    assert "sqlglot" in message
    assert "exmergo-dex-core[duckdb]" in message
    assert "[semantic-api]" in message
    assert "No module named" not in message


def test_the_engine_is_available_in_a_normal_install():
    # The other side of the guard: it must not refuse when the extra is present, or
    # every guarded command breaks at once.
    dialect.ensure_available()


def test_a_guarded_command_refuses_before_any_warehouse_work(monkeypatch, capsys):
    """The refusal is an envelope, and it arrives before a connection is opened.

    `explore inventory` is given no target at all: if the guard ran late, the
    failure would be about the missing connection instead, which is how a
    late-checked dependency announces itself.
    """

    def _boom() -> None:
        raise dialect.DialectDependencyError("the dialect engine is not installed")

    monkeypatch.setattr(cli, "ensure_dialect_available", _boom)

    assert cli.main(["explore", "inventory"]) == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one line on stdout"
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert "dialect engine" in payload["errors"][0]


@pytest.mark.parametrize("backend_flag", ["--api", "--local"])
def test_the_semantic_subcommand_is_not_gated_on_the_dialect_engine(
    backend_flag, monkeypatch, capsys
):
    """`explore semantic` must reach its backend without the guard.

    The hosted backend sends the query to dbt Cloud, which renders and executes it,
    so dex has no statement to validate and a pure-remote install has no parser to
    validate it with. `--local` is here too: `list` is a manifest read-view, and a
    local `query` reaches the dialect engine through MetricFlow's own path, not
    through this gate.

    If someone later routes this subcommand through the guard, or back through the
    heavier explore command module, this fails: the envelope stops being about the
    backend and starts being about the missing engine.
    """

    def _boom() -> None:  # pragma: no cover - must never be called on this route
        raise dialect.DialectDependencyError("the dialect engine is not installed")

    monkeypatch.setattr(cli, "ensure_dialect_available", _boom)

    cli.main(["explore", "semantic", backend_flag, "--repo-root", "missing-dex-dir"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "dialect engine" not in payload["errors"][0], payload["errors"]
