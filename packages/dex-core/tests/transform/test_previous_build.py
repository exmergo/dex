"""Historical build context stays separate from current-run evidence (#1064)."""

import json

import pytest

from exmergo_dex_core.transform.artifact_runtime import dbt_runtime_key
from exmergo_dex_core.transform.prerequisites import diagnose
from exmergo_dex_core.transform.previous_build import (
    SNAPSHOT,
    node_context,
    preserve_previous_build,
)


def _results(root, *, status="error", which="build", target="dev", version=None):
    document = {
        "metadata": {
            "invocation_id": "earlier",
            "generated_at": "2026-11-03T00:00:00Z",
            "dbt_version": version or dbt_runtime_key().removeprefix("dbt-"),
            "env": {"TOKEN": "must not persist"},
        },
        "args": {
            "which": which,
            "target": target,
            "select": ["+report"],
            "password": "must not persist",
        },
        "results": [
            {
                "unique_id": "model.p.upstream",
                "status": status,
                "message": "must not persist",
            }
        ],
    }
    (root / "run_results.json").write_text(json.dumps(document))


@pytest.mark.parametrize("status", ["success", "error", "skipped", "unknown"])
def test_previous_node_status_is_observation_only(tmp_path, status):
    _results(tmp_path, status=status)
    previous = preserve_previous_build(tmp_path, "dev")
    context = node_context(previous, "model.p.upstream")
    assert context["available"] is True
    assert context["status"] == status
    assert context["invocation_id"] == "earlier"
    assert context["selection"] == ["+report"]
    assert "statuses" not in context
    assert "must not persist" not in (tmp_path / SNAPSHOT).read_text()
    manifest = {
        "metadata": {"project_name": "p"},
        "nodes": {
            "model.p.upstream": {"name": "upstream", "depends_on": {"nodes": []}},
        },
    }
    [diagnosis] = diagnose(
        [("model.p.upstream", "error", None)],
        manifest,
        previous_run=previous,
    )
    assert diagnosis["plan_relation"] == "undetermined"
    assert diagnosis["previous_run"] == context


@pytest.mark.parametrize("which", ["compile", "seed", "test", "run"])
def test_pricing_and_other_commands_never_replace_saved_build(tmp_path, which):
    _results(tmp_path)
    first = preserve_previous_build(tmp_path, "dev")
    _results(tmp_path, status="success", which=which)
    assert preserve_previous_build(tmp_path, "dev") == first
    (tmp_path / "run_results.json").unlink()
    assert preserve_previous_build(tmp_path, "dev") == first


@pytest.mark.parametrize("current", [False, True])
def test_corrupt_history_is_explicitly_unknown(tmp_path, current):
    (tmp_path / ("run_results.json" if current else SNAPSHOT)).write_text("{bad")
    assert preserve_previous_build(tmp_path, "dev") == {
        "available": False,
        "reason_code": "unreadable_previous_build",
    }


def test_missing_history_and_unselected_nodes_are_unknown(tmp_path):
    assert (
        preserve_previous_build(tmp_path, "dev")["reason_code"] == "no_previous_build"
    )
    _results(tmp_path)
    context = node_context(preserve_previous_build(tmp_path, "dev"), "model.p.other")
    assert context["available"] is False
    assert context["reason_code"] == "node_not_observed"
    assert context["invocation_id"] == "earlier"


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"target": "other_dev"}, "incompatible_target"),
        ({"version": "0.0.0"}, "incompatible_runtime"),
    ],
)
def test_incompatible_history_is_not_used(tmp_path, options, reason):
    _results(tmp_path, **options)
    assert preserve_previous_build(tmp_path, "dev")["reason_code"] == reason


def test_cold_and_ordinary_history_are_separate(tmp_path):
    cold = tmp_path / "cold"
    cold.mkdir()
    _results(tmp_path, status="success")
    _results(cold, status="error")
    assert (
        preserve_previous_build(tmp_path, "dev")["statuses"]
        != (preserve_previous_build(cold, "dev")["statuses"])
    )
