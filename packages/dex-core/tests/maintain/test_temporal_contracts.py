"""Temporal contracts against real DuckDB relations (#1027).

Every expected number below is worked out by hand from the versions written in
the test and the declared policy, not by running any model's logic: that is what
makes the check independent of the conventions a candidate's SQL adopted. The
model rows are written as literal answers, right or wrong on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
import sqlglot

from exmergo_dex_core.maintain import declared_facts as facts_mod
from exmergo_dex_core.maintain import temporal_contracts as temporal_mod
from exmergo_dex_core.semantic_declarations import History

D1, D2, D3, D4 = (
    "TIMESTAMP '2026-01-01'",
    "TIMESTAMP '2026-01-02'",
    "TIMESTAMP '2026-01-03'",
    "TIMESTAMP '2026-01-04'",
)


def _history(**overrides) -> dict:
    history = {
        "snapshot": "hosts_snapshot",
        "entity": {"host_id": "host_id"},
        "status": {"column": "is_super", "target": True},
        "initial_observation": "baseline",
        "null_status": "ignore",
        "population": "all_observed",
        "time": {"column": "dbt_valid_from", "provenance": "capture"},
        "measures": {
            "transition_count": "changes",
            "latest_target_transition_at": "latest_at",
            "first_target_at": "first_at",
        },
    }
    history.update(overrides)
    return history


def _run(
    tmp_path: Path,
    versions: list[tuple],
    answers: list[tuple],
    history: dict | None = None,
    *,
    strategy: str = "check",
    hard_deletes: str | None = None,
    run_status: str | None = "success",
):
    """``versions``: (host_id, name, is_super, source_at, valid_from) SQL literals.
    ``answers``: (host_id, changes, latest_at, first_at) SQL literals."""

    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "create table hosts_snapshot (host_id int, name varchar, is_super boolean, "
        "source_at timestamp, dbt_valid_from timestamp, dbt_updated_at timestamp)"
    )
    for host, name, status, source_at, valid_from in versions:
        conn.execute(
            f"insert into hosts_snapshot values ({host}, '{name}', {status}, "  # noqa: S608
            f"{source_at}, {valid_from}, {valid_from})"
        )
    conn.execute(
        "create table host_history (host_id int, changes int, latest_at timestamp, "
        "first_at timestamp)"
    )
    for answer in answers:
        conn.execute(f"insert into host_history values ({', '.join(map(str, answer))})")  # noqa: S608
    conn.close()

    target = tmp_path / "target"
    target.mkdir(exist_ok=True)
    snapshot_config: dict = {"strategy": strategy}
    if hard_deletes:
        snapshot_config["hard_deletes"] = hard_deletes
    nodes = {
        "model.p.host_history": {
            "name": "host_history",
            "resource_type": "model",
            "relation_name": '"warehouse"."main"."host_history"',
            "config": {
                "materialized": "table",
                "meta": {"dex": {"history": history or _history()}},
            },
            "columns": {},
        },
        "snapshot.p.hosts_snapshot": {
            "name": "hosts_snapshot",
            "resource_type": "snapshot",
            "relation_name": '"warehouse"."main"."hosts_snapshot"',
            "config": snapshot_config,
        },
    }
    (target / "manifest.json").write_text(json.dumps({"nodes": nodes}))
    if run_status:
        (target / "run_results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "unique_id": "snapshot.p.hosts_snapshot",
                            "status": run_status,
                        }
                    ]
                }
            )
        )

    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    adapter = DuckDBAdapter(path)
    try:
        models, _notes = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        prepared = facts_mod.prepare(adapter, models, live)
        return facts_mod.findings(adapter, prepared)
    finally:
        adapter.close()


def _record(result) -> dict:
    (record,) = result.temporal_contracts
    return record


def _codes(result) -> list[str]:
    return sorted(f.code for f in result.findings)


def _assertion(record: dict, name: str) -> dict:
    return next(a for a in record["assertions"] if a["assertion"] == name)


# --- 1. first observed true, under each initial-state policy ----------------------

FIRST_TRUE = [(1, "ann", "true", D1, D1)]


def test_a_first_true_observation_is_no_transition_under_baseline(tmp_path):
    # Baseline: nothing changed, so zero transitions and no transition time. It
    # first attained the status when it was first seen.
    result = _run(tmp_path, FIRST_TRUE, [(1, 0, "NULL", D1)])
    record = _record(result)
    assert _codes(result) == []
    assert _assertion(record, "transition_count")["status"] == "satisfied"
    # The columns agree, but no transition was exercised, and it does not say so.
    assert record["status"] == "not_applicable"
    assert record["reason_code"] == "no_history_observed"


def test_counting_an_invented_prior_state_contradicts_a_baseline_policy(tmp_path):
    result = _run(tmp_path, FIRST_TRUE, [(1, 1, D1, D1)])
    assert _codes(result) == [
        "temporal_latest_transition_mismatch",
        "temporal_transition_count_mismatch",
    ]
    assert _record(result)["status"] == "contradicted"


def test_a_first_true_observation_counts_once_when_that_policy_is_selected(tmp_path):
    history = _history(initial_observation="transition")
    result = _run(tmp_path, FIRST_TRUE, [(1, 1, D1, D1)], history)
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "satisfied"


def test_a_first_false_observation_is_no_transition_under_either_policy(tmp_path):
    versions = [(1, "ann", "false", D1, D1)]
    history = _history(initial_observation="transition")
    result = _run(tmp_path, versions, [(1, 0, "NULL", "NULL")], history)
    assert _codes(result) == []


# --- 2. unchanged true and an attribute-only edit ---------------------------------


def test_a_name_only_edit_is_not_a_transition(tmp_path):
    versions = [(1, "ann", "true", D1, D1), (1, "anne", "true", D2, D2)]
    result = _run(tmp_path, versions, [(1, 0, "NULL", D1)])
    assert _codes(result) == []


def test_counting_a_name_edit_as_a_transition_is_reported(tmp_path):
    versions = [
        (1, "ann", "false", D1, D1),
        (1, "ann", "true", D2, D2),
        (1, "anne", "true", D3, D3),
    ]
    # One real change, at D2. The wrong answer counted the edit and dated it D3.
    result = _run(tmp_path, versions, [(1, 2, D3, D2)])
    assert _codes(result) == [
        "temporal_latest_transition_mismatch",
        "temporal_transition_count_mismatch",
    ]
    bad = next(
        f for f in result.findings if f.code == "temporal_transition_count_mismatch"
    )
    assert bad.data["mismatched_entities"] == 1
    assert bad.data["compared"] == 1


# --- 3. false, true, false, true --------------------------------------------------

FLIPPING = [
    (1, "ann", "false", D1, D1),
    (1, "ann", "true", D2, D2),
    (1, "ann", "false", D3, D3),
    (1, "ann", "true", D4, D4),
]


def test_three_changes_with_the_latest_into_target_at_the_fourth_version(tmp_path):
    result = _run(tmp_path, FLIPPING, [(1, 3, D4, D2)])
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "satisfied"
    assert record["measured"]["entities_with_transition"] == 1


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ((1, 2, D4, D2), "temporal_transition_count_mismatch"),
        ((1, 3, D2, D2), "temporal_latest_transition_mismatch"),
        ((1, 3, D4, D4), "temporal_first_attainment_mismatch"),
    ],
)
def test_each_wrong_measure_is_named_on_its_own(tmp_path, answer, code):
    assert _codes(_run(tmp_path, FLIPPING, [answer])) == [code]


# --- 4. NULL statuses under the selected policy -----------------------------------

WITH_NULL = [
    (1, "ann", "true", D1, D1),
    (1, "ann", "NULL", D2, D2),
    (1, "ann", "true", D3, D3),
]


def test_a_null_version_is_no_change_when_null_is_ignored(tmp_path):
    # true, (dropped), true: no change at all.
    result = _run(tmp_path, WITH_NULL, [(1, 0, "NULL", D1)])
    assert _codes(result) == []


def test_a_null_version_is_two_changes_when_null_is_a_state(tmp_path):
    # true -> NULL at D2, NULL -> true at D3; only the second enters the target.
    history = _history(null_status="state")
    result = _run(tmp_path, WITH_NULL, [(1, 2, D3, D1)], history)
    assert _codes(result) == []
    assert _record(result)["status"] == "satisfied"


def test_the_same_answer_is_wrong_under_the_other_null_policy(tmp_path):
    result = _run(tmp_path, WITH_NULL, [(1, 2, D3, D1)])
    assert "temporal_transition_count_mismatch" in _codes(result)


def test_an_entity_with_only_null_status_is_still_observed(tmp_path):
    versions = [(1, "ann", "NULL", D1, D1)]
    result = _run(tmp_path, versions, [])
    assert _codes(result) == ["temporal_population_missing"]


def test_an_all_null_entity_with_a_row_and_no_transitions_agrees(tmp_path):
    versions = [(1, "ann", "NULL", D1, D1)]
    assert _codes(_run(tmp_path, versions, [(1, 0, "NULL", "NULL")])) == []


# --- 5. frozen or missing source timestamps versus advancing capture --------------


def _business(**overrides) -> dict:
    return _history(time={"column": "source_at", "provenance": "business"}, **overrides)


def test_a_frozen_source_timestamp_leaves_the_times_unchecked_not_guessed(tmp_path):
    # The status moved at D2 by capture, but the source says D1 both times.
    versions = [(1, "ann", "false", D1, D1), (1, "ann", "true", D1, D2)]
    result = _run(tmp_path, versions, [(1, 1, "NULL", "NULL")], _business())
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "unchecked"
    (item,) = record["unchecked"]
    assert item["reason_code"] == "business_timestamp_unreliable"
    assert item["entities"] == 1
    # Capture order is intact, so the count is still held to the policy.
    assert _assertion(record, "transition_count")["status"] == "satisfied"
    assert _assertion(record, "latest_target_transition_at")["compared"] == 0


def test_a_wrong_count_is_still_caught_on_a_frozen_source_timestamp(tmp_path):
    versions = [(1, "ann", "false", D1, D1), (1, "ann", "true", D1, D2)]
    result = _run(tmp_path, versions, [(1, 5, "NULL", "NULL")], _business())
    assert _codes(result) == ["temporal_transition_count_mismatch"]


def test_a_missing_source_timestamp_at_a_change_is_unreliable(tmp_path):
    versions = [(1, "ann", "false", D1, D1), (1, "ann", "true", "NULL", D2)]
    result = _run(tmp_path, versions, [(1, 1, "NULL", "NULL")], _business())
    assert _record(result)["unchecked"][0]["reason_code"] == (
        "business_timestamp_unreliable"
    )


def test_advancing_capture_time_holds_where_the_source_is_frozen(tmp_path):
    versions = [(1, "ann", "false", D1, D1), (1, "ann", "true", D1, D2)]
    result = _run(tmp_path, versions, [(1, 1, D2, D2)])
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "satisfied"
    assert record["unchecked"] == []


def test_a_source_time_equal_to_creation_is_not_flagged_on_its_own(tmp_path):
    # One observation whose timestamp may equal creation proves nothing either way.
    versions = [(1, "ann", "true", D1, D1)]
    result = _run(tmp_path, versions, [(1, 0, "NULL", D1)], _business())
    assert _record(result)["unchecked"] == []


# --- 6. a late-arriving source event ----------------------------------------------


def test_late_arriving_order_is_explained_and_never_guessed(tmp_path):
    # Captured false then true, but the source dates the true before the false.
    versions = [
        (1, "ann", "false", D3, D1),
        (1, "ann", "true", D2, D2),
        (2, "bob", "false", D1, D1),
        (2, "bob", "true", D2, D2),
    ]
    # Host 1's answer would be wrong under any order: excluded, not judged.
    answers = [(1, 99, D4, D4), (2, 1, D2, D2)]
    result = _run(tmp_path, versions, answers, _business())
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "unchecked"
    (item,) = record["unchecked"]
    assert item["reason_code"] == "business_order_conflict"
    assert item["entities"] == 1
    assert _assertion(record, "transition_count")["compared"] == 1


def test_versions_sharing_a_validity_time_have_no_order_to_judge(tmp_path):
    versions = [(1, "ann", "false", D1, D1), (1, "ann", "true", D1, D1)]
    result = _run(tmp_path, versions, [(1, 7, D4, D4)])
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "unchecked"
    assert record["unchecked"][0]["reason_code"] == "ambiguous_version_order"


# --- policy left out, population, provenance --------------------------------------


def test_no_policy_is_assumed_for_a_field_the_declaration_leaves_out(tmp_path):
    history = _history()
    del history["initial_observation"]
    del history["null_status"]
    # A wrong count cannot be called wrong with no policy to hold it to.
    result = _run(tmp_path, FLIPPING, [(1, 99, D4, D2)], history)
    record = _record(result)
    assert _codes(result) == []
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "policy_missing"
    count = _assertion(record, "transition_count")
    assert count["owed"] == ["initial_observation", "null_status"]
    # First attainment rests on neither, so it is still judged.
    assert _assertion(record, "first_target_at")["status"] == "satisfied"


def test_a_missing_provenance_leaves_only_the_time_fields_unchecked(tmp_path):
    history = _history(time={"column": "dbt_valid_from"})
    record = _record(_run(tmp_path, FLIPPING, [(1, 3, D4, D2)], history))
    assert record["status"] == "unchecked"
    assert _assertion(record, "transition_count")["status"] == "satisfied"
    assert _assertion(record, "first_target_at")["reason_code"] == "policy_missing"


def test_ever_target_population_counts_who_is_missing_and_who_should_not_be_there(
    tmp_path,
):
    versions = [
        (1, "ann", "false", D1, D1),
        (1, "ann", "true", D2, D2),
        (2, "bob", "true", D1, D1),
        (3, "cid", "false", D1, D1),
    ]
    history = _history(population="ever_target")
    # Hosts 1 and 2 ever attained it. The answer drops 2 and adds 3.
    result = _run(tmp_path, versions, [(1, 1, D2, D2), (3, 0, "NULL", "NULL")], history)
    assert _codes(result) == [
        "temporal_population_missing",
        "temporal_population_unexpected",
    ]
    record = _record(result)
    assert record["measured"]["missing_entities"] == 1
    assert record["measured"]["unexpected_entities"] == 1
    assert record["measured"]["eligible_entities"] == 2


def test_capture_provenance_is_contradicted_under_the_timestamp_strategy(tmp_path):
    result = _run(tmp_path, FLIPPING, [(1, 3, D4, D2)], strategy="timestamp")
    (finding,) = result.findings
    assert finding.code == "temporal_provenance_contradicted"
    assert finding.severity == "medium"
    assert _record(result)["snapshot_execution"]["strategy"] == "timestamp"


def test_business_provenance_is_contradicted_on_a_check_strategy_validity_column(
    tmp_path,
):
    history = _history(time={"column": "dbt_valid_from", "provenance": "business"})
    result = _run(tmp_path, FLIPPING, [(1, 3, D4, D2)], history)
    assert _codes(result) == ["temporal_provenance_contradicted"]


# --- execution, unsupported shapes, cost ------------------------------------------


def test_snapshot_execution_is_reported_apart_from_the_history_claim(tmp_path):
    result = _run(tmp_path, FLIPPING, [(1, 0, D4, D2)])
    record = _record(result)
    # The snapshot built and the history is still wrong: two different facts.
    assert record["snapshot_execution"]["status"] == "success"
    assert record["status"] == "contradicted"


def test_a_failed_snapshot_is_unchecked_rather_than_measured_stale(tmp_path):
    result = _run(tmp_path, FLIPPING, [(1, 3, D4, D2)], run_status="error")
    record = _record(result)
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "snapshot_not_built"
    assert "temporal" in result.suppressed


def test_a_snapshot_that_did_not_run_says_so(tmp_path):
    record = _record(_run(tmp_path, FLIPPING, [(1, 3, D4, D2)], run_status=None))
    assert record["snapshot_execution"]["status"] == "not_in_last_run"


def test_hard_delete_records_are_an_unsupported_shape(tmp_path):
    record = _record(
        _run(tmp_path, FLIPPING, [(1, 3, D4, D2)], hard_deletes="new_record")
    )
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "hard_delete_records"


def test_an_unknown_snapshot_is_unchecked(tmp_path):
    history = _history(snapshot="elsewhere")
    record = _record(_run(tmp_path, FLIPPING, [(1, 3, D4, D2)], history))
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "relation_not_found"


def test_a_column_the_snapshot_lacks_is_named(tmp_path):
    history = _history(status={"column": "nope", "target": True})
    record = _record(_run(tmp_path, FLIPPING, [(1, 3, D4, D2)], history))
    assert record["reason_code"] == "column_missing"
    assert "hosts_snapshot.nope" in record["reason"]


def test_a_malformed_hand_written_history_is_unchecked_not_absent(tmp_path):
    history = _history(initial_observation="whatever")
    record = _record(_run(tmp_path, FLIPPING, [(1, 3, D4, D2)], history))
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "malformed_declaration"
    # The location is named; the value is untrusted text and is not repeated.
    assert "initial_observation" in record["reason"]
    assert "whatever" not in record["reason"]


def test_an_empty_snapshot_and_model_have_nothing_to_judge(tmp_path):
    result = _run(tmp_path, [], [])
    record = _record(result)
    assert record["status"] == "not_applicable"
    assert record["reason_code"] == "empty_input"
    assert "temporal" in result.suppressed


def test_nothing_declared_is_suppressed_by_name(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "manifest.json").write_text(json.dumps({"nodes": {}}))

    class _Adapter:
        pass

    prepared = facts_mod.prepare(_Adapter(), [], [])
    result = facts_mod.findings(_Adapter(), prepared)
    assert "history temporal contract" in result.suppressed["temporal"]
    assert result.temporal_contracts == []


def test_an_offered_scan_leaves_every_contract_unchecked(tmp_path, monkeypatch):
    original = facts_mod.findings

    def offering(adapter, prepared, **kwargs):
        return original(adapter, prepared, handshake=lambda _e, _c: object())

    monkeypatch.setattr(facts_mod, "findings", offering)
    result = _run(tmp_path, FLIPPING, [(1, 0, D4, D2)])
    record = _record(result)
    assert result.findings == []
    assert result.offer is not None
    assert record["status"] == "unchecked"
    assert record["reason_code"] == "scan_offered"


def test_the_statement_returns_counts_and_never_a_value(tmp_path):
    result = _run(tmp_path, FLIPPING, [(1, 0, D2, D2)])
    record = _record(result)
    assert all(isinstance(v, int) for v in record["measured"].values())
    rendered = json.dumps(record) + json.dumps(
        [f.model_dump(mode="json") for f in result.findings]
    )
    # Neither a timestamp nor a name from the data reaches the envelope.
    assert "2026-01" not in rendered
    assert "ann" not in rendered


@pytest.mark.parametrize(
    "dialect",
    [
        "duckdb",
        "bigquery",
        "snowflake",
        "databricks",
        "postgres",
        "redshift",
        "clickhouse",
        "trino",
    ],
)
def test_the_statement_transpiles_to_every_connector(dialect):
    history = History.model_validate(_history())
    sql = temporal_mod._statement(
        history,
        relation="db.main.hosts_snapshot",
        model_relation="db.main.host_history",
        snapshot_keys=["host_id"],
        model_keys=["host_id"],
        status=("is_super", "BOOLEAN"),
        order="dbt_valid_from",
        time="dbt_valid_from",
        measures={
            "transition_count": ("changes", "INTEGER"),
            "latest_target_transition_at": ("latest_at", "TIMESTAMP"),
            "first_target_at": ("first_at", "DATE"),
        },
    )
    written = facts_mod._transpile(sql, dialect)
    (parsed,) = sqlglot.parse(written, read=dialect)
    assert parsed.find(sqlglot.exp.Select) is not None
    assert "IS DISTINCT" not in written.upper()


def test_a_date_measure_compares_at_date_precision(tmp_path):
    path_versions = [
        (1, "ann", "false", D1, "TIMESTAMP '2026-01-01 03:00:00'"),
        (1, "ann", "true", D2, "TIMESTAMP '2026-01-02 03:00:00'"),
    ]
    history = _history(measures={"first_target_at": "first_on"}, population=None)
    del history["population"]
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        "create table hosts_snapshot (host_id int, name varchar, is_super boolean, "
        "source_at timestamp, dbt_valid_from timestamp, dbt_updated_at timestamp)"
    )
    for host, name, status, source_at, valid_from in path_versions:
        conn.execute(
            f"insert into hosts_snapshot values ({host}, '{name}', {status}, "  # noqa: S608
            f"{source_at}, {valid_from}, {valid_from})"
        )
    conn.execute("create table host_history (host_id int, first_on date)")
    conn.execute("insert into host_history values (1, DATE '2026-01-02')")
    conn.close()
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.host_history": {
                        "name": "host_history",
                        "resource_type": "model",
                        "relation_name": '"warehouse"."main"."host_history"',
                        "config": {"meta": {"dex": {"history": history}}},
                        "columns": {},
                    },
                    "snapshot.p.hosts_snapshot": {
                        "name": "hosts_snapshot",
                        "resource_type": "snapshot",
                        "relation_name": '"warehouse"."main"."hosts_snapshot"',
                        "config": {"strategy": "check"},
                    },
                }
            }
        )
    )
    from exmergo_dex_core.adapters.duckdb import DuckDBAdapter

    adapter = DuckDBAdapter(path)
    try:
        models, _ = facts_mod.plan(tmp_path)
        live = [meta.identifier for meta in adapter.list_objects()]
        result = facts_mod.findings(adapter, facts_mod.prepare(adapter, models, live))
    finally:
        adapter.close()
    assert [f.code for f in result.findings] == []
    assert _assertion(_record(result), "first_target_at")["status"] == "satisfied"
