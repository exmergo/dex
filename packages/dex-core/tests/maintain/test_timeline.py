"""The coverage timeline (#1466): never an unobserved interval read as good.

Each acceptance criterion is a pair: a case built to break the guarantee and
one built to hold it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from exmergo_dex_core.maintain.incidents import fold_incidents
from exmergo_dex_core.maintain.observations import (
    UNKNOWN,
    Observation,
    ObservedPoint,
    RunRef,
    Signal,
    Subject,
)
from exmergo_dex_core.maintain.timeline import (
    Heartbeat,
    build_timeline,
    choose_heartbeat,
    subject_histories,
    subject_history,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
TEST = "test.shop.not_null_orders_order_id.abc123"
SUBJECT = Subject.of(TEST)
ELEMENTARY = "elementary"
DBT = "dbt_run_results"


def _run(n: int, *, hours: float | None = None, **kwargs) -> RunRef:
    return RunRef(
        run=f"inv-{n}",
        sequence=n,
        at=T0 + timedelta(hours=n if hours is None else hours),
        commit=kwargs.pop("commit", f"{n:07x}"),
        **kwargs,
    )


def _obs(
    n: int,
    outcome: str,
    *,
    source: str = ELEMENTARY,
    subject: Subject = SUBJECT,
    test: str = TEST,
) -> Observation:
    return Observation(
        subject=subject,
        test=test,
        outcome=outcome,
        source=source,
        run=f"inv-{n}",
        sequence=n,
        at=T0 + timedelta(hours=n),
        commit=f"{n:07x}",
    )


def _invocations(runs: list[RunRef]) -> Heartbeat:
    return choose_heartbeat(invocation_upload=True, invocations=runs)


def _history(runs, observations, **kwargs):
    timeline = build_timeline(ELEMENTARY, _invocations(runs), observations, **kwargs)
    return timeline, subject_history(timeline, SUBJECT, Signal(), observations)


# -- the break -----------------------------------------------------------------


def test_a_run_with_hooks_off_widens_the_break() -> None:
    # Runs 1 and 2 pass, run 3 has hooks off (the orchestrator lists it, the
    # package recorded nothing), run 4 fails.
    runs = [_run(1), _run(2), _run(3, unobserved="hooks_off"), _run(4)]
    observations = [_obs(1, "good"), _obs(2, "good"), _obs(4, "bad")]
    _, history = _history(runs, observations)
    states = [(s.state, s.reason, s.from_run, s.to_run) for s in history.segments]
    assert states[1:] == [
        ("observed_good", None, "inv-1", "inv-2"),
        ("unobserved", "hooks_off", "inv-3", "inv-3"),
        ("observed_bad", None, "inv-4", "inv-4"),
    ]
    (found,) = history.breaks
    assert found.after != UNKNOWN and found.after.run == "inv-2"
    assert found.through.run == "inv-4"
    assert found.unobserved_runs == 1
    assert found.reasons == ("hooks_off",)


def test_the_same_series_observed_passing_places_it_after_run_3() -> None:
    runs = [_run(1), _run(2), _run(3), _run(4)]
    observations = [
        _obs(1, "good"),
        _obs(2, "good"),
        _obs(3, "good"),
        _obs(4, "bad"),
    ]
    _, history = _history(runs, observations)
    (found,) = history.breaks
    assert found.after != UNKNOWN and found.after.run == "inv-3"
    assert found.unobserved_runs == 0 and found.reasons == ()


def test_a_run_with_uploads_off_is_unobserved_not_good() -> None:
    runs = [_run(1), _run(2, unobserved="uploads_off"), _run(3)]
    _, history = _history(runs, [_obs(1, "good"), _obs(3, "bad")])
    (found,) = history.breaks
    assert found.after != UNKNOWN and found.after.run == "inv-1"
    assert found.reasons == ("uploads_off",)


def test_dbts_own_test_materialization_leaves_the_run_observed() -> None:
    # Elementary uploads a row for every test dbt ran even where its own
    # materialization cached nothing; nothing about that makes a run
    # unobserved, so the row's outcome stands.
    runs = [_run(1), _run(2), _run(3)]
    _, history = _history(runs, [_obs(1, "good"), _obs(2, "good"), _obs(3, "bad")])
    assert history.breaks[0].after.run == "inv-2"  # type: ignore[union-attr]


def test_a_first_failure_no_pass_precedes_breaks_after_unknown() -> None:
    _, history = _history([_run(1), _run(2)], [_obs(1, "bad"), _obs(2, "bad")])
    (found,) = history.breaks
    assert found.after == UNKNOWN
    assert found.reasons == ("retention",)
    assert history.segments[0].kind == "retention"


def test_a_flap_is_two_breaks() -> None:
    runs = [_run(n) for n in range(1, 6)]
    observations = [
        _obs(1, "good"),
        _obs(2, "bad"),
        _obs(3, "good"),
        _obs(4, "bad"),
        _obs(5, "bad"),
    ]
    _, history = _history(runs, observations)
    assert [(b.after.run, b.through.run) for b in history.breaks] == [  # type: ignore[union-attr]
        ("inv-1", "inv-2"),
        ("inv-3", "inv-4"),
    ]


# -- gaps against the schedule ---------------------------------------------------


def _hourly(*missing: int) -> list[RunRef]:
    return [_run(n) for n in range(1, 9) if n not in missing]


def test_a_gap_in_dbt_invocations_reads_unobserved() -> None:
    runs = _hourly(5, 6)
    observations = [_obs(r.sequence, "good") for r in runs[:-1]] + [_obs(8, "bad")]
    timeline, history = _history(runs, observations)
    missed = [e for e in timeline.entries if e.kind == "missed"]
    assert len(missed) == 1
    gap = missed[0]
    assert (gap.after_run, gap.before_run, gap.missed_runs) == ("inv-4", "inv-7", 2)
    assert gap.reason == "missed_run"
    assert timeline.schedule.basis == "derived"
    assert timeline.schedule.gap_seconds == 3600
    assert [s.kind for s in history.segments if s.state == "unobserved"] == [
        "retention",
        "missed",
    ]


def test_the_same_schedule_with_no_gap_reads_every_run_observed() -> None:
    runs = _hourly()
    observations = [_obs(n, "good") for n in range(1, 9)]
    timeline, history = _history(runs, observations)
    assert not [e for e in timeline.entries if e.kind == "missed"]
    assert [(s.kind, s.state) for s in history.segments] == [
        ("retention", "unobserved"),
        ("runs", "observed_good"),
    ]


def test_a_missed_run_between_good_and_bad_widens_the_break() -> None:
    runs = [_run(1), _run(2), _run(3), _run(6)]
    observations = [_obs(1, "good"), _obs(2, "good"), _obs(3, "good"), _obs(6, "bad")]
    _, history = _history(runs, observations)
    (found,) = history.breaks
    assert found.after.run == "inv-3"  # type: ignore[union-attr]
    assert found.missed_runs == 2 and found.reasons == ("missed_run",)


def test_a_burst_of_hand_runs_does_not_read_a_pause_as_missed() -> None:
    # Builds seconds apart, then a ten-minute pause: not a schedule.
    runs = [
        RunRef(run=f"inv-{n}", sequence=n, at=T0 + timedelta(seconds=5 * n))
        for n in range(1, 6)
    ]
    runs.append(RunRef(run="inv-6", sequence=6, at=T0 + timedelta(minutes=10)))
    timeline = build_timeline(ELEMENTARY, _invocations(runs))
    assert timeline.schedule.missed_after_seconds == 3600
    assert not [e for e in timeline.entries if e.kind == "missed"]


def test_a_declared_schedule_has_no_floor() -> None:
    runs = [
        RunRef(run=f"inv-{n}", sequence=n, at=T0 + timedelta(minutes=15 * n))
        for n in (1, 2, 4)
    ]
    timeline = build_timeline(ELEMENTARY, _invocations(runs), expected_gap_seconds=900)
    assert timeline.schedule.basis == "declared"
    (gap,) = [e for e in timeline.entries if e.kind == "missed"]
    assert gap.missed_runs == 1
    derived = build_timeline(ELEMENTARY, _invocations(runs))
    assert not [e for e in derived.entries if e.kind == "missed"]


def test_too_little_history_reads_no_gap() -> None:
    timeline = build_timeline(ELEMENTARY, _invocations([_run(1), _run(50)]))
    assert timeline.schedule.basis == "unknown"
    assert not [e for e in timeline.entries if e.kind == "missed"]


def test_a_missed_snapshot_is_named_so() -> None:
    snapshots = [_run(1, hours=0), _run(2, hours=24), _run(3, hours=48)]
    snapshots.append(_run(4, hours=120))
    heartbeat = choose_heartbeat(invocation_upload=False, snapshots=snapshots)
    timeline = build_timeline(ELEMENTARY, heartbeat)
    assert timeline.granularity == "snapshot"
    (gap,) = [e for e in timeline.entries if e.kind == "missed"]
    assert gap.reason == "snapshot_missed" and gap.missed_runs == 2


def test_timeline_settings_are_checked() -> None:
    heartbeat = _invocations([_run(1)])
    with pytest.raises(ValueError, match="gap_tolerance"):
        build_timeline(ELEMENTARY, heartbeat, gap_tolerance=1)
    with pytest.raises(ValueError, match="gap_floor_seconds"):
        build_timeline(ELEMENTARY, heartbeat, gap_floor_seconds=-1)
    with pytest.raises(ValueError, match="expected_gap_seconds"):
        build_timeline(ELEMENTARY, heartbeat, expected_gap_seconds=0)


# -- the heartbeat ---------------------------------------------------------------


def test_with_the_invocation_upload_on_it_names_dbt_invocations() -> None:
    heartbeat = choose_heartbeat(
        invocation_upload=True,
        invocations=[_run(1)],
        orchestrator=[_run(1)],
    )
    assert heartbeat.basis == "dbt_invocations"
    assert heartbeat.invocation_upload == "on"
    assert heartbeat.fallback_reason is None
    assert build_timeline(ELEMENTARY, heartbeat).heartbeat == "dbt_invocations"


def test_with_the_invocation_upload_off_it_names_its_fallback() -> None:
    # Rows written before the upload was turned off are not the heartbeat.
    common = {"invocation_upload": False, "invocations": [_run(1)]}
    orchestrated = choose_heartbeat(
        **common, orchestrator=[_run(1)], snapshots=[_run(1)]
    )
    assert orchestrated.basis == "orchestrator"
    assert orchestrated.fallback_reason == "invocation_upload_off"
    snapshotted = choose_heartbeat(**common, snapshots=[_run(1)])
    assert snapshotted.basis == "snapshots"
    bare = choose_heartbeat(**common)
    assert bare.basis == "observations"
    timeline = build_timeline(ELEMENTARY, orchestrated)
    assert (
        timeline.heartbeat,
        timeline.fallback_reason,
        timeline.invocation_upload,
    ) == (
        "orchestrator",
        "invocation_upload_off",
        "off",
    )


def test_an_unknown_upload_setting_is_reported_as_unknown() -> None:
    held = choose_heartbeat(invocation_upload=None, invocations=[_run(1)])
    assert (held.basis, held.invocation_upload) == ("dbt_invocations", "unknown")
    empty = choose_heartbeat(invocation_upload=None, orchestrator=[_run(1)])
    assert (empty.basis, empty.fallback_reason) == ("orchestrator", "no_invocations")


def test_a_run_only_the_observations_name_is_kept_off_the_heartbeat() -> None:
    timeline = build_timeline(
        ELEMENTARY, _invocations([_run(1), _run(3)]), [_obs(2, "good")]
    )
    run_2 = next(e for e in timeline.entries if e.run == "inv-2")
    assert not run_2.in_heartbeat


# -- commits ---------------------------------------------------------------------


def test_a_run_with_a_null_git_sha_carries_its_commit_as_unknown() -> None:
    runs = [_run(1, commit=None), _run(2, commit="b7c41e0")]
    observations = [
        _obs(1, "good").model_copy(update={"commit": None}),
        _obs(2, "bad").model_copy(update={"commit": None}),
    ]
    timeline, history = _history(runs, observations)
    commits = [e.commit for e in timeline.entries if e.kind == "run"]
    assert commits == [UNKNOWN, "b7c41e0"]
    (found,) = history.breaks
    assert found.after.commit == UNKNOWN  # type: ignore[union-attr]
    assert found.through.commit == "b7c41e0"


def test_an_observations_commit_fills_a_run_its_heartbeat_left_unknown() -> None:
    timeline = build_timeline(
        ELEMENTARY, _invocations([_run(1, commit=None)]), [_obs(1, "good")]
    )
    assert [e.commit for e in timeline.entries if e.kind == "run"] == ["0000001"]


# -- per-subject history ---------------------------------------------------------


def test_a_deselected_test_keeps_its_last_verified_pass() -> None:
    # #1529: a broad build ran seven tests; a selected build then ran only the
    # one model's two. The five deselected tests keep their earlier pass as
    # last verified, and the later run left them unobserved.
    tests = [Subject.of(f"test.shop.t{n}") for n in range(7)]
    observations = [
        Observation(
            subject=s, test=s.key, outcome="good", source=DBT, run="inv-1", sequence=1
        )
        for s in tests
    ] + [
        Observation(
            subject=s, test=s.key, outcome="good", source=DBT, run="inv-2", sequence=2
        )
        for s in tests[:2]
    ]
    heartbeat = Heartbeat(basis="observations")
    timeline = build_timeline(DBT, heartbeat, observations)
    histories = {h.subject: h for h in subject_histories(timeline, observations)}
    for selected in tests[:2]:
        assert histories[selected].last_verified.run == "inv-2"  # type: ignore[union-attr]
        assert histories[selected].latest.state == "observed_good"  # type: ignore[union-attr]
    for deselected in tests[2:]:
        history = histories[deselected]
        assert history.last_verified.run == "inv-1"  # type: ignore[union-attr]
        assert history.latest is not None
        assert (history.latest.run, history.latest.state, history.latest.reason) == (
            "inv-2",
            "unobserved",
            "not_checked",
        )


def test_a_missed_run_or_uploads_off_leaves_last_verified_where_it_was() -> None:
    runs = [_run(1), _run(2), _run(3, unobserved="uploads_off"), _run(4)]
    _, history = _history(runs, [_obs(1, "good"), _obs(2, "good")])
    assert history.last_verified.run == "inv-2"  # type: ignore[union-attr]
    assert history.latest is not None and history.latest.reason == "not_checked"


def test_latest_is_a_run_off_the_heartbeat_where_one_is_newest() -> None:
    # The invocation upload was off on run 3: no row, but its result is there.
    _, history = _history([_run(1), _run(2)], [_obs(2, "good"), _obs(3, "good")])
    assert history.last_verified.run == "inv-3"  # type: ignore[union-attr]
    assert history.latest is not None and history.latest.run == "inv-3"


def test_a_subject_never_passing_has_no_last_verified() -> None:
    _, history = _history([_run(1)], [_obs(1, "bad")])
    assert history.last_verified == UNKNOWN
    assert history.last_observed.run == "inv-1"  # type: ignore[union-attr]


def test_two_sources_on_one_run_read_the_failure() -> None:
    runs = [_run(1), _run(2)]
    observations = [
        _obs(1, "good"),
        _obs(2, "good", source=DBT),
        _obs(2, "bad"),
    ]
    _, history = _history(runs, observations)
    assert history.breaks[0].through.run == "inv-2"


def test_a_retention_bound_is_reported_as_read() -> None:
    bound = ObservedPoint(run="inv-0", at=T0, source=ELEMENTARY)
    timeline = build_timeline(
        ELEMENTARY, _invocations([_run(1)]), retention_bound=bound
    )
    assert timeline.retention_bound == bound
    assert timeline.entries[0].kind == "retention"
    assert timeline.entries[0].to_at == T0


# -- incidents carry it ------------------------------------------------------------


def test_an_incidents_coverage_is_laid_on_the_timeline() -> None:
    runs = [_run(1), _run(2), _run(3, unobserved="hooks_off"), _run(4), _run(7)]
    observations = [_obs(1, "good"), _obs(2, "bad"), _obs(4, "bad"), _obs(7, "bad")]
    timeline = build_timeline(ELEMENTARY, _invocations(runs), observations)
    fold = fold_incidents(
        observations,
        timelines={ELEMENTARY: timeline},
        now=T0 + timedelta(hours=7, minutes=5),
    )
    (incident,) = fold.incidents
    assert incident.coverage.heartbeat == "dbt_invocations"
    assert [
        (s.kind, s.state, s.reason, s.runs) for s in incident.coverage.segments
    ] == [
        ("runs", "observed_bad", None, 1),
        ("runs", "unobserved", "hooks_off", 1),
        ("runs", "observed_bad", None, 1),
        ("missed", "unobserved", "missed_run", 2),
        ("runs", "observed_bad", None, 1),
    ]
    assert incident.onset is not None
    assert incident.onset.after.run == "inv-1"  # type: ignore[union-attr]
    assert incident.onset.through.run == "inv-2"


def test_an_onset_follows_the_lifecycle_not_the_merged_run() -> None:
    # close_after=1. Run 2: another source fails while the opening source
    # passes, which closes the first incident; run 3 opens a second one. The
    # merged history reads run 2 bad, yet the second onset is after run 2.
    observations = [
        _obs(1, "bad"),
        _obs(2, "bad", source=DBT),
        _obs(2, "good"),
        _obs(3, "bad"),
    ]
    timeline = build_timeline(ELEMENTARY, _invocations([_run(n) for n in (1, 2, 3)]))
    fold = fold_incidents(
        observations,
        timelines={ELEMENTARY: timeline},
        close_after=1,
        now=T0 + timedelta(hours=3, minutes=5),
    )
    second = [i for i in fold.incidents if i.opening_source == ELEMENTARY][-1]
    assert second.first_observed != UNKNOWN
    assert second.onset is not None
    assert second.onset.after.run == "inv-2"  # type: ignore[union-attr]
    assert second.onset.through.run == "inv-3"


def test_runs_only_the_observations_name_split_a_heartbeat_gap() -> None:
    # Heartbeat holds runs 1, 2, 3 and 6; runs 4 and 5 were observed anyway.
    observations = [_obs(n, "good") for n in range(1, 6)] + [_obs(6, "bad")]
    runs = [_run(1), _run(2), _run(3), _run(6)]
    timeline, history = _history(runs, observations)
    assert not [e for e in timeline.entries if e.kind == "missed"]
    (found,) = history.breaks
    assert found.after.run == "inv-5"  # type: ignore[union-attr]
    assert found.missed_runs == 0 and found.reasons == ()


def test_a_run_another_source_observed_splits_a_missed_interval() -> None:
    # The opening source's heartbeat holds runs 1, 2, 3 and 6, so 3 to 6 is
    # missed. Another source passed at run 5: only 3 to 5 can hold a missed
    # run, and the break from 5 to 6 holds none.
    runs = [_run(1), _run(2), _run(3), _run(6)]
    observations = [_obs(3, "good"), _obs(5, "good", source=DBT), _obs(6, "bad")]
    timeline = build_timeline(ELEMENTARY, _invocations(runs), observations)
    history = subject_history(timeline, SUBJECT, Signal(), observations)
    (found,) = history.breaks
    assert found.after.run == "inv-5"  # type: ignore[union-attr]
    assert (found.missed_runs, found.reasons) == (0, ())
    missed = [s for s in history.segments if s.kind == "missed"]
    assert [(s.after_run, s.before_run, s.runs) for s in missed] == [
        ("inv-3", "inv-5", 1)
    ]
    fold = fold_incidents(
        observations,
        timelines={ELEMENTARY: timeline},
        now=T0 + timedelta(hours=6, minutes=5),
    )
    onset = fold.incidents[0].onset
    assert onset is not None and onset.missed_runs == 0


def test_an_undated_run_another_source_observed_keeps_the_interval() -> None:
    runs = [_run(1), _run(2), _run(3), _run(6)]
    undated = _obs(5, "good", source=DBT).model_copy(update={"at": None})
    observations = [_obs(3, "good"), undated, _obs(6, "bad")]
    timeline = build_timeline(ELEMENTARY, _invocations(runs), observations)
    history = subject_history(timeline, SUBJECT, Signal(), observations)
    missed = [s for s in history.segments if s.kind == "missed"]
    assert [(s.after_run, s.before_run, s.runs) for s in missed] == [
        ("inv-3", "inv-6", 2)
    ]


def test_an_incident_without_a_timeline_names_no_heartbeat() -> None:
    observations = [_obs(1, "bad"), _obs(2, "bad")]
    (incident,) = fold_incidents(observations, now=T0 + timedelta(hours=2)).incidents
    assert incident.coverage.heartbeat == "observations"
    assert incident.onset is not None and incident.onset.after == UNKNOWN
    assert incident.first_observed == UNKNOWN


def test_a_timeline_under_another_sources_name_is_refused() -> None:
    timeline = build_timeline(DBT, _invocations([_run(1)]))
    with pytest.raises(ValueError, match="timeline of"):
        fold_incidents([], timelines={ELEMENTARY: timeline})


# -- no value crosses --------------------------------------------------------------


def test_a_serialized_timeline_and_history_hold_no_input_value() -> None:
    marker = "SECRET-ROW-VALUE-7731"
    raw = {"message": marker, "failing_rows": [marker], "compiled_code": marker}
    observations = [_obs(1, "good"), _obs(2, "bad")]
    timeline, history = _history([_run(1), _run(2)], observations)
    dumped = json.dumps(
        [timeline.model_dump(mode="json"), history.model_dump(mode="json")]
    )
    assert marker not in dumped
    assert all(key not in dumped for key in raw)
    with pytest.raises(ValueError):
        RunRef(run="inv-1", sequence=1, message=marker)  # type: ignore[call-arg]
