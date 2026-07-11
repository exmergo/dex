"""The scoping flags: honored, or named in an error. Never accepted and dropped.

A `--dataset` silently discarded on Snowflake let a confirm handshake quote an
estimate spanning tables the user never asked for, so the rule is that every
scoping flag either scopes the command or refuses it. These tests pin the
vocabulary each connector speaks and the narrow-only rule that keeps a committed
allowlist a real cost boundary. Nothing here opens a connection.
"""

from __future__ import annotations

import pytest

from exmergo_dex_core.config import (
    BigQueryTarget,
    DatabricksTarget,
    PostgresTarget,
)
from exmergo_dex_core.connect import ScopeError, assert_scope_vocabulary, narrow_target


def _assert(connector, *, project=None, datasets=None, scopes=None):
    assert_scope_vocabulary(
        connector, project=project, datasets=datasets, scopes=scopes
    )


# --- vocabulary --------------------------------------------------------------------


@pytest.mark.parametrize("connector", ["snowflake", "databricks", "postgres"])
@pytest.mark.parametrize(
    ("flag", "kwargs"),
    [("--project", {"project": "p"}), ("--dataset", {"datasets": ["d"]})],
)
def test_bigquery_flags_are_refused_on_other_connectors(connector, flag, kwargs):
    with pytest.raises(ScopeError) as exc:
        _assert(connector, **kwargs)
    message = str(exc.value)
    assert flag in message
    assert connector in message
    # The error has to name the flag that would have worked.
    assert "--scope" in message


@pytest.mark.parametrize(
    "kwargs",
    [{"project": "p"}, {"datasets": ["d"]}, {"scopes": ["s"]}],
)
def test_duckdb_refuses_every_scope_flag(kwargs):
    with pytest.raises(ScopeError) as exc:
        _assert("duckdb", **kwargs)
    assert "--path" in str(exc.value)


def test_bigquery_accepts_its_own_flags():
    _assert("bigquery", project="p", datasets=["analytics"])


@pytest.mark.parametrize(
    "connector", ["bigquery", "snowflake", "databricks", "postgres"]
)
def test_scope_is_accepted_on_every_warehouse_connector(connector):
    _assert(connector, scopes=["analytics"])


def test_bigquery_refuses_dataset_and_scope_together():
    with pytest.raises(ScopeError) as exc:
        _assert("bigquery", datasets=["a"], scopes=["b"])
    assert "pass one" in str(exc.value)


def test_no_flags_is_always_fine():
    for connector in ("duckdb", "bigquery", "snowflake", "databricks", "postgres"):
        _assert(connector)


# --- narrow, never widen -------------------------------------------------------------


def test_scope_sets_the_allowlist_when_none_is_committed():
    """This is what makes `connect test --scope X` work before a config block
    exists, which is the reason the BigQuery flags were introduced."""

    target = narrow_target(BigQueryTarget(), "bigquery", ["analytics"])
    assert target.datasets == ["analytics"]


def test_scope_narrows_a_committed_allowlist():
    target = narrow_target(
        DatabricksTarget(catalogs=["prod", "raw"]), "databricks", ["raw.events"]
    )
    assert target.catalogs == ["raw.events"]


def test_scope_outside_the_committed_allowlist_is_refused():
    with pytest.raises(ScopeError) as exc:
        narrow_target(BigQueryTarget(datasets=["analytics"]), "bigquery", ["billing"])
    message = str(exc.value)
    assert "billing" in message
    assert "bigquery.datasets: analytics" in message
    assert "never widens" in message


def test_containment_is_case_insensitive():
    """A case mismatch is not an escape attempt: the connectors disagree about
    identifier case, and Postgres folds to lower while Snowflake folds to upper."""

    target = narrow_target(PostgresTarget(schemas=["Public"]), "postgres", ["public"])
    assert target.schemas == ["public"]


def test_a_coarser_scope_cannot_escape_a_finer_allowlist():
    with pytest.raises(ScopeError):
        narrow_target(DatabricksTarget(catalogs=["raw.events"]), "databricks", ["raw"])


def test_no_override_leaves_the_target_untouched():
    committed = BigQueryTarget(datasets=["analytics"])
    assert narrow_target(committed, "bigquery", None) is committed
