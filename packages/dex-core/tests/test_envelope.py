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
    assert payload["cost"]["paradigm"] == "free_local"
    assert payload["warnings"] == ["heads up"]
    assert payload["diffs"] == [] and payload["errors"] == []


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
