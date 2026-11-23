"""Incidents read from dbt's ``run_results.json`` (#1468).

Each guarantee is a pair: a case built to break it, which must not, and a case
built to hold it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from exmergo_dex_core.maintain import run_results_incidents as rr
from exmergo_dex_core.maintain.incidents import fold_incidents
from exmergo_dex_core.maintain.observations import Subject
from exmergo_dex_core.maintain.timeline import (
    Heartbeat,
    build_timeline,
    subject_histories,
)
from exmergo_dex_core.transform.artifact_runtime import isolated_artifact_dir
from exmergo_dex_core.transform.claims import manifest_claims

INV_A = "11111111-1111-1111-1111-111111111111"
INV_B = "22222222-2222-2222-2222-222222222222"
AT = "2026-11-15T11:00:00.000000Z"
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)

NOT_NULL = "test.shop.not_null_orders_order_id.abc123"
UNIQUE = "test.shop.unique_orders_order_id.def456"
SINGULAR = "test.shop.assert_positive_amount"
UNIT = "unit_test.shop.orders.test_orders_full"
GONE = "test.shop.accepted_values_orders_status.0ff1ce"

MARKER = "SECRET-ROW-VALUE-7f3a"


def _node(**config: Any) -> dict[str, Any]:
    return {"resource_type": "test", "config": {"enabled": True, **config}}


def _manifest(**nodes: dict[str, Any]) -> dict[str, Any]:
    tests = {NOT_NULL: _node(), UNIQUE: _node(), SINGULAR: _node()}
    tests.update(nodes)
    tests["model.shop.orders"] = {"resource_type": "model", "config": {}}
    return {"nodes": tests, "unit_tests": {UNIT: {"config": {"enabled": True}}}}


def _document(
    rows: dict[str, str] | list[dict[str, Any]],
    *,
    invocation: str | None = INV_A,
    generated_at: str = AT,
    which: str = "build",
) -> dict[str, Any]:
    if isinstance(rows, dict):
        rows = [{"unique_id": uid, "status": status} for uid, status in rows.items()]
    results = [
        {
            "message": f"Got 3 results, configured to fail if != 0: {MARKER}",
            "failures": 3,
            "compiled_code": f"select '{MARKER}' as v from orders",  # noqa: S608
            "adapter_response": {"_message": MARKER},
            "relation_name": f'"dev"."{MARKER}"',
            "timing": [],
            **row,
        }
        for row in rows
    ]
    metadata: dict[str, Any] = {
        "dbt_schema_version": "https://schemas.getdbt.com/dbt/run-results/v6.json",
        "dbt_version": "1.12.0",
        "generated_at": generated_at,
    }
    if invocation is not None:
        metadata["invocation_id"] = invocation
    return {
        "metadata": metadata,
        "results": results,
        "elapsed_time": 1.5,
        "args": {"which": which},
    }


def _write(directory: Path, document: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run_results.json").write_text(json.dumps(document))


def _log(directory: Path, *invocations: str) -> None:
    logs = directory / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "dbt.log").write_text(
        "\n".join(json.dumps({"info": {"invocation_id": i}}) for i in invocations)
        + "\n"
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("DBT_ENGINE_TARGET_PATH", "DBT_TARGET_PATH", "DBT_LOG_PATH"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: shop\n")
    return root


def _dex_dir(project: Path) -> Path:
    directory = isolated_artifact_dir(project)
    assert directory is not None
    return directory


def _observe(rows, manifest=None, **kwargs) -> rr.ObservedRun:
    observed = rr.observe_run_results(
        _document(rows, **kwargs), manifest or _manifest()
    )
    assert observed is not None
    return observed


def _fold(observed: rr.ObservedRun, manifest=None):
    current = [
        Subject.of(uid, manifest_claims(manifest or _manifest()).get(uid))
        for uid in (NOT_NULL, UNIQUE, SINGULAR, UNIT)
    ]
    return fold_incidents(
        observed.observations,
        sources=[observed.history],
        current_subjects=current,
        now=NOW,
    )


# --- one failing test, one passing ------------------------------------------------


def test_one_failing_test_is_one_open_incident_with_no_known_start() -> None:
    observed = _observe({NOT_NULL: "fail", UNIQUE: "pass"})
    [incident] = _fold(observed).incidents

    assert incident.subject == Subject.of(NOT_NULL)
    assert incident.state == "open"
    assert incident.first_observed == "unknown"
    assert incident.opening_source == "dbt_run_results"
    assert incident.onset is not None and incident.onset.after == "unknown"
    assert incident.retention_bound == {
        "dbt_run_results": incident.last_observed,
    }


def test_the_same_file_with_the_test_passing_has_no_incident() -> None:
    observed = _observe({NOT_NULL: "pass", UNIQUE: "pass"})
    assert _fold(observed).incidents == ()
    assert {o.outcome for o in observed.observations} == {"good"}


# --- skipped is unobserved, never good --------------------------------------------


def test_a_skipped_test_is_an_unobserved_interval_not_a_pass() -> None:
    observed = _observe({NOT_NULL: "skipped"})
    [only] = observed.observations
    assert only.outcome == "unobserved"

    timeline = build_timeline(
        rr.SOURCE,
        Heartbeat(basis="source_runs", runs=observed.history.runs),
        observed.observations,
        retention_bound=observed.history.retention_bound,
    )
    [history] = subject_histories(timeline, observed.observations)
    assert [s.state for s in history.segments if s.kind == "runs"] == ["unobserved"]
    assert history.last_verified == "unknown"
    assert _fold(observed).incidents == ()


def test_a_passing_test_is_observed_good() -> None:
    observed = _observe({NOT_NULL: "pass"})
    timeline = build_timeline(
        rr.SOURCE,
        Heartbeat(basis="source_runs", runs=observed.history.runs),
        observed.observations,
    )
    [history] = subject_histories(timeline, observed.observations)
    assert [s.state for s in history.segments if s.kind == "runs"] == ["observed_good"]
    assert history.last_verified != "unknown"


def test_a_test_the_run_did_not_reach_has_no_observation() -> None:
    observed = _observe({NOT_NULL: "pass"})
    assert [o.test for o in observed.observations] == [NOT_NULL]


# --- only tests the manifest still has --------------------------------------------


def test_a_result_for_a_removed_or_disabled_test_is_not_observed() -> None:
    manifest = _manifest(**{UNIQUE: _node(enabled=False)})
    observed = _observe({GONE: "fail", UNIQUE: "fail"}, manifest)
    assert observed.observations == ()
    assert observed.counts.dropped == 2
    assert _fold(observed, manifest).incidents == ()


def test_a_result_for_a_test_the_manifest_has_is_one_observation() -> None:
    observed = _observe({NOT_NULL: "fail", UNIT: "pass", "model.shop.orders": "error"})
    assert sorted(o.test for o in observed.observations) == [NOT_NULL, UNIT]
    assert observed.counts.dropped == 0


# --- no value crosses -------------------------------------------------------------


def test_no_message_sql_or_row_reaches_an_observation_or_incident(
    project: Path,
) -> None:
    _write(project / "target", _document({NOT_NULL: "fail", UNIQUE: "warn"}))
    _log(project, INV_A)
    read = rr.read_run_results(project, _manifest())
    assert read.status == "read"
    fold = _fold(rr.ObservedRun(read.observations, read.history, read.counts))
    assert len(fold.incidents) == 2

    serialized = json.dumps(
        {
            "observations": [o.model_dump(mode="json") for o in read.observations],
            "history": read.history.model_dump(mode="json"),
            "run": read.run,
            "notes": read.notes,
            "warnings": read.warnings,
            "incidents": fold.model_dump(mode="json"),
        }
    )
    assert MARKER not in serialized
    assert "Got 3 results" not in serialized


def test_the_marker_is_in_the_file_the_adapter_read(project: Path) -> None:
    # The other half of the pair: the file does carry the values.
    _write(project / "target", _document({NOT_NULL: "fail"}))
    assert MARKER in (project / "target" / "run_results.json").read_text()


# --- statuses and severity --------------------------------------------------------


def test_a_warning_is_observed_bad_with_severity_warn() -> None:
    observed = _observe({SINGULAR: "warn"})
    [only] = observed.observations
    assert (only.outcome, only.severity) == ("bad", "warn")
    [incident] = _fold(observed).incidents
    assert incident.severity == "warn"


def test_a_failure_keeps_the_severity_its_test_configures() -> None:
    manifest = _manifest(**{NOT_NULL: _node(severity="ERROR")})
    [failure] = _observe({NOT_NULL: "fail"}, manifest).observations
    assert failure.severity == "ERROR"
    [passed] = _observe({NOT_NULL: "pass"}, manifest).observations
    assert passed.severity is None


def test_a_severity_that_is_not_a_token_is_left_out() -> None:
    manifest = _manifest(**{NOT_NULL: _node(severity="{{ var('sev') }}")})
    [failure] = _observe({NOT_NULL: "fail"}, manifest).observations
    assert failure.severity is None


def test_runtime_error_is_bad_and_an_unknown_status_is_not_observed() -> None:
    observed = _observe({NOT_NULL: "runtime error", UNIQUE: "partial success"})
    [only] = observed.observations
    assert (only.test, only.outcome) == (NOT_NULL, "bad")
    assert observed.counts.unrecognized == 1


def test_error_and_fail_are_bad() -> None:
    observed = _observe({NOT_NULL: "error", UNIQUE: "fail"})
    assert {o.outcome for o in observed.observations} == {"bad"}


# --- subject ----------------------------------------------------------------------


def test_a_claimed_test_is_keyed_by_its_claim() -> None:
    claimed = {"meta": {"dex": {"claim": "4f1c2a9be0d3c711"}}}
    manifest = _manifest(**{NOT_NULL: _node(**claimed)})
    [only] = _observe({NOT_NULL: "fail"}, manifest).observations
    assert only.subject == Subject(kind="claim", key="4f1c2a9be0d3c711")
    assert only.test == NOT_NULL


def test_an_unclaimed_test_is_keyed_by_its_unique_id() -> None:
    [only] = _observe({NOT_NULL: "fail"}).observations
    assert only.subject == Subject(kind="unique_id", key=NOT_NULL)


def test_a_renamed_test_with_the_same_claim_is_one_subject() -> None:
    renamed = "test.shop.not_null_orders_order_id.999fff"
    claim = {"meta": {"dex": {"claim": "4f1c2a9be0d3c711"}}}
    old = _observe({NOT_NULL: "fail"}, _manifest(**{NOT_NULL: _node(**claim)}))
    new = _observe(
        {renamed: "fail"},
        _manifest(**{renamed: _node(**claim)}),
        invocation=INV_B,
    )
    assert old.observations[0].subject == new.observations[0].subject


def test_a_claim_too_long_to_hold_falls_back_to_the_unique_id() -> None:
    manifest = _manifest(**{NOT_NULL: _node(meta={"dex": {"claim": "c" * 600}})})
    observed = _observe({NOT_NULL: "fail"}, manifest)
    assert observed.observations[0].subject == Subject.of(NOT_NULL)
    assert observed.counts.claims_unread == 1


def test_manifest_claims_reads_config_meta_and_top_level_meta() -> None:
    manifest = {
        "nodes": {
            NOT_NULL: _node(meta={"dex": {"claim": "a1"}}),
            UNIQUE: {"resource_type": "test", "meta": {"dex": {"claim": "b2"}}},
            SINGULAR: _node(meta={"dex": {}}),
            "model.shop.orders": {
                "resource_type": "model",
                "config": {"meta": {"dex": {"claim": "not-a-test"}}},
            },
        }
    }
    assert manifest_claims(manifest) == {NOT_NULL: "a1", UNIQUE: "b2"}
    assert manifest_claims({}) == {}


def test_an_explicit_claims_map_stands_in_for_the_manifest() -> None:
    observed = rr.observe_run_results(
        _document({NOT_NULL: "fail"}), _manifest(), claims={NOT_NULL: "zz"}
    )
    assert observed is not None
    assert observed.observations[0].subject == Subject(kind="claim", key="zz")


# --- malformed rows ---------------------------------------------------------------


def test_repeated_rows_for_one_test_keep_the_failure() -> None:
    observed = _observe(
        [
            {"unique_id": NOT_NULL, "status": "pass"},
            {"unique_id": NOT_NULL, "status": "fail"},
            {"unique_id": NOT_NULL, "status": "skipped"},
        ]
    )
    [only] = observed.observations
    assert only.outcome == "bad"
    assert observed.counts.merged == 2


def test_a_row_with_no_unique_id_is_unattributed() -> None:
    document = _document([{"status": "fail"}, {"unique_id": "", "status": "fail"}])
    document["results"].append("junk")
    observed = rr.observe_run_results(document, _manifest())
    assert observed is not None
    assert observed.observations == ()
    assert observed.counts.unattributed == 3


def test_a_document_with_no_invocation_id_is_not_placed() -> None:
    assert (
        rr.observe_run_results(
            _document({NOT_NULL: "fail"}, invocation=None), _manifest()
        )
        is None
    )
    assert (
        rr.observe_run_results(
            _document({NOT_NULL: "fail"}, invocation="x" * 200), _manifest()
        )
        is None
    )


def test_the_history_is_one_run_and_its_own_retention_bound() -> None:
    observed = _observe({NOT_NULL: "fail"})
    [run] = observed.history.runs
    assert (run.run, run.sequence, run.commit) == (INV_A, 1, None)
    assert run.at == datetime(2026, 11, 15, 11, 0, tzinfo=UTC)
    assert observed.history.retention_bound is not None
    assert observed.history.retention_bound.run == INV_A
    assert all(o.commit is None for o in observed.observations)


# --- discovery on disk ------------------------------------------------------------


def test_no_run_results_is_missing(project: Path) -> None:
    read = rr.read_run_results(project, _manifest())
    assert (read.status, read.reason_code) == ("missing", "no_run_results")
    assert read.observations == () and read.history is None


def test_a_compile_alone_is_missing_not_a_run(project: Path) -> None:
    _write(project / "target", _document({NOT_NULL: "pass"}, which="compile"))
    _log(project, INV_A)
    read = rr.read_run_results(project, _manifest())
    assert (read.status, read.reason_code) == ("missing", "no_run_results")


def test_a_build_is_read(project: Path) -> None:
    _write(_dex_dir(project), _document({NOT_NULL: "fail"}))
    _log(project, INV_A)
    read = rr.read_run_results(project, _manifest())
    assert read.status == "read" and read.reason_code is None
    assert read.run == {
        "invocation_id": INV_A,
        "generated_at": AT,
        "command": "build",
        "artifact": "dex",
        "cold": False,
        "logged": True,
    }
    assert [o.outcome for o in read.observations] == ["bad"]
    assert any("first_observed: unknown" in note for note in read.notes)


def test_an_unlogged_run_is_refused(project: Path) -> None:
    _write(project / "target", _document({NOT_NULL: "fail"}))
    _log(project, INV_B)
    read = rr.read_run_results(project, _manifest())
    assert (read.status, read.reason_code) == ("refused", "unlogged_invocation")
    assert read.observations == () and read.history is None
    assert read.run is not None and read.run["logged"] is False
    assert read.warnings


def test_a_run_with_no_log_to_check_is_read_with_a_note(project: Path) -> None:
    _write(project / "target", _document({NOT_NULL: "fail"}))
    read = rr.read_run_results(project, _manifest())
    assert read.status == "read"
    assert read.run is not None and read.run["logged"] is None
    assert any("no dbt log" in note for note in read.notes)


def test_a_file_with_no_invocation_id_is_refused(project: Path) -> None:
    _write(project / "target", _document({NOT_NULL: "fail"}, invocation=None))
    read = rr.read_run_results(project, _manifest())
    assert (read.status, read.reason_code) == ("refused", "no_invocation_id")
    assert read.observations == ()


def test_the_newer_run_is_read_and_never_stitched_to_the_older(
    project: Path,
) -> None:
    _write(
        project / "target",
        _document(
            {NOT_NULL: "pass"}, invocation=INV_A, generated_at="2026-11-15T09:00:00Z"
        ),
    )
    _write(
        _dex_dir(project),
        _document(
            {NOT_NULL: "fail"}, invocation=INV_B, generated_at="2026-11-15T10:00:00Z"
        ),
    )
    _log(project, INV_A, INV_B)
    read = rr.read_run_results(project, _manifest())
    assert read.run is not None and read.run["invocation_id"] == INV_B
    assert {o.run for o in read.observations} == {INV_B}
    assert read.history is not None and len(read.history.runs) == 1
    [incident] = _fold(
        rr.ObservedRun(read.observations, read.history, read.counts)
    ).incidents
    # The older passing run is not this source's history: the start stays unknown.
    assert incident.first_observed == "unknown"


def test_the_older_run_alone_is_read_when_it_is_the_newest(project: Path) -> None:
    _write(
        project / "target",
        _document(
            {NOT_NULL: "pass"}, invocation=INV_A, generated_at="2026-11-15T09:00:00Z"
        ),
    )
    _log(project, INV_A)
    read = rr.read_run_results(project, _manifest())
    assert read.run is not None and read.run["invocation_id"] == INV_A


def test_dropped_and_unrecognized_results_are_named_in_notes(project: Path) -> None:
    _write(project / "target", _document({GONE: "fail", NOT_NULL: "no-op"}))
    _log(project, INV_A)
    read = rr.read_run_results(project, _manifest())
    assert read.counts.dropped == 1 and read.counts.unrecognized == 1
    joined = " ".join(read.notes)
    assert "no longer has" in joined and "does not map" in joined


def test_a_later_run_that_ran_no_test_does_not_hide_the_failures(
    project: Path,
) -> None:
    _write(
        _dex_dir(project),
        _document(
            {NOT_NULL: "fail"}, invocation=INV_A, generated_at="2026-11-15T09:00:00Z"
        ),
    )
    _write(
        project / "target",
        _document(
            {"seed.shop.raw_orders": "success"},
            invocation=INV_B,
            generated_at="2026-11-15T10:00:00Z",
            which="seed",
        ),
    )
    _log(project, INV_A, INV_B)
    read = rr.read_run_results(project, _manifest())
    assert read.status == "read"
    assert read.run is not None and read.run["invocation_id"] == INV_A
    assert [o.outcome for o in read.observations] == ["bad"]
    assert any("ran no test" in note and "seed" in note for note in read.notes)


def test_a_later_run_that_tested_is_the_one_read(project: Path) -> None:
    _write(
        _dex_dir(project),
        _document(
            {NOT_NULL: "fail"}, invocation=INV_A, generated_at="2026-11-15T09:00:00Z"
        ),
    )
    _write(
        project / "target",
        _document(
            {NOT_NULL: "pass"},
            invocation=INV_B,
            generated_at="2026-11-15T10:00:00Z",
            which="test",
        ),
    )
    _log(project, INV_A, INV_B)
    read = rr.read_run_results(project, _manifest())
    assert read.run is not None and read.run["invocation_id"] == INV_B
    assert [o.outcome for o in read.observations] == ["good"]
    assert not any("ran no test" in note for note in read.notes)


def test_runs_that_never_tested_are_missing_not_clean(project: Path) -> None:
    _write(
        project / "target",
        _document({"model.shop.orders": "success"}, which="run"),
    )
    _log(project, INV_A)
    read = rr.read_run_results(project, _manifest())
    assert (read.status, read.reason_code) == ("missing", "no_test_results")
    assert read.history is None
