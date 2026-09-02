"""The stdout envelope is the eval/safety boundary: it must be well-shaped and it
must never carry secrets or raw rows."""

from __future__ import annotations

import json

import pytest

from exmergo_dex_core import envelope as env


def test_envelope_round_trips_to_json():
    e = env.ok({"hello": "world"}, warnings=["heads up"])
    payload = json.loads(json.dumps(e.model_dump(mode="json")))
    assert payload["status"] == "ok"
    assert payload["data"] == {"hello": "world"}
    assert payload["connection"] == {
        "connector": None,
        "target": {},
        "source": None,
    }
    assert payload["warnings"] == ["heads up"]
    assert payload["diffs"] == [] and payload["errors"] == []


def test_an_unstamped_cost_claims_no_paradigm():
    """The default has to be an absence, not `free_local`.

    `free_local` is DuckDB's own paradigm, so defaulting to it makes every
    envelope built without a cost assert that the connector bills nothing. On a
    refusal that is the opposite of true, and it is the field a host reaches for
    to ask whether the refusal was about money.
    """

    for envelope in (env.ok(), env.error("no"), env.needs_confirmation()):
        assert envelope.cost.paradigm is None
    assert json.loads(json.dumps(env.error("no").model_dump(mode="json")))["cost"] == {
        "paradigm": None,
        "estimate": None,
        "ceiling": None,
    }


def test_not_implemented_is_a_valid_envelope():
    e = env.not_implemented("explore inventory")
    assert e.status is env.Status.NOT_IMPLEMENTED
    assert e.data["command"] == "explore inventory"


@pytest.mark.parametrize(
    "data",
    [
        {"password": "hunter2"},
        {"connection": {"client_secret": "abc"}},
        {"nested": [{"api_key": "x"}]},
        {"auth": {"session_token": "t"}},
    ],
)
def test_sanitize_refuses_secret_like_keys(data):
    with pytest.raises(env.SanitizationError):
        env.sanitize(env.ok(data))


def test_sanitize_scans_connection_target_too():
    with pytest.raises(env.SanitizationError):
        env.sanitize(
            env.ok(
                connection=env.Connection(
                    connector="postgres",
                    target={"password": "never"},
                    source="flag",
                )
            )
        )


@pytest.mark.parametrize(
    "data",
    [
        {"rows": [{"id": 1, "email": "a@example.com"}]},
        {"profile": {"sample_rows": [{"v": 1}, {"v": 2}]}},
        {"preview_rows": [{"col": "value"}]},
    ],
)
def test_sanitize_refuses_raw_row_payloads(data):
    with pytest.raises(env.SanitizationError):
        env.sanitize(env.ok(data))


def test_sanitize_allows_aggregates_and_flags():
    # The shape profiling is *allowed* to emit: counts and flags, not row values.
    safe = {
        "datasets": [
            {
                "identifier": "main.main.customers",
                "row_count": 2,
                "columns": [
                    {
                        "name": "email",
                        "null_fraction": 0.0,
                        "pii": {"category": "email", "confidence": 0.9},
                    }
                ],
            }
        ]
    }
    assert env.sanitize(env.ok(safe)).data == safe


def test_emit_writes_single_json_line(capsys):
    env.emit(env.ok({"x": 1}))
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1
    assert json.loads(out)["data"] == {"x": 1}


def test_redact_masks_dsn_password():
    assert "hunter2" not in env.redact("postgres://user:hunter2@host:5432/db")


# --- reason_for / error_for: a machine-readable refusal reason (#170) --------


def test_reason_for_classifies_representative_families():
    from exmergo_dex_core.dbt_project import DbtProjectError
    from exmergo_dex_core.errors import ConnectorError, RequestError
    from exmergo_dex_core.guards.cost_guard import OverCeilingError
    from exmergo_dex_core.maintain.commands import NoBaselineError

    assert env.reason_for(OverCeilingError("x")) is env.Reason.GUARD
    assert env.reason_for(NoBaselineError("x")) is env.Reason.PREREQUISITE
    assert env.reason_for(ConnectorError("x")) is env.Reason.CONNECTION
    assert env.reason_for(DbtProjectError("x")) is env.Reason.CONFIGURATION
    assert env.reason_for(RequestError("x")) is env.Reason.REQUEST
    assert env.reason_for(ValueError("x")) is env.Reason.REQUEST
    assert env.reason_for(RuntimeError("x")) is env.Reason.INTERNAL


def test_reason_for_execution_failure():
    from exmergo_dex_core.transform.build import DbtRunError

    assert (
        env.reason_for(DbtRunError("dbt build failed")) is env.Reason.EXECUTION_FAILURE
    )


def test_reason_for_subclass_precedence():
    """A more specific bucket must win over its parent's default, proving the
    override ordering (not just isinstance against the wrong ancestor)."""

    from exmergo_dex_core.explore.cluster import ClusterDependencyError, ClusterError
    from exmergo_dex_core.explore.semantic import (
        SemanticBackendError,
        SemanticQueryRefusedError,
    )

    # SemanticQueryRefusedError IS-A SemanticBackendError, but is policy
    # (GUARD), not a backend failure (CONFIGURATION, its parent's bucket).
    assert env.reason_for(SemanticQueryRefusedError("x")) is env.Reason.GUARD
    assert env.reason_for(SemanticBackendError("x")) is env.Reason.CONFIGURATION

    # ClusterDependencyError IS-A ClusterError, but a missing extra is a setup
    # step (PREREQUISITE), not the bare-input REQUEST its parent defaults to.
    assert env.reason_for(ClusterDependencyError("x")) is env.Reason.PREREQUISITE
    assert env.reason_for(ClusterError("x")) is env.Reason.REQUEST


def test_error_for_defaults_message_to_str_and_derives_reason():
    from exmergo_dex_core.errors import RequestError

    envelope = env.error_for(RequestError("bad thing"))
    assert envelope.status is env.Status.ERROR
    assert envelope.errors == ["bad thing"]
    assert envelope.reason is env.Reason.REQUEST


def test_error_for_message_override():
    from exmergo_dex_core.guards.query_firewall import QueryRefusedError

    exc = QueryRefusedError("nope")
    envelope = env.error_for(exc, f"query refused: {exc}")
    assert envelope.errors == ["query refused: nope"]
    assert envelope.reason is env.Reason.GUARD


def test_error_for_passes_through_kwargs():
    from exmergo_dex_core.errors import RequestError

    envelope = env.error_for(
        RequestError("x"), cost=env.Cost(paradigm=env.Paradigm.BYTES_SCANNED)
    )
    assert envelope.cost.paradigm is env.Paradigm.BYTES_SCANNED


def test_reason_is_none_outside_error_status():
    assert env.ok().reason is None
    assert env.needs_confirmation().reason is None
    assert env.not_implemented("x").reason is None
