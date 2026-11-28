"""The coverage timeline ordered by commit where runs and commits disagree (#1467).

Each acceptance criterion is a pair: a case built to break the guarantee and
one built to hold it. Most cases run on an in-memory commit graph; the last
group runs the same rules against a real ``git`` repository.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from exmergo_dex_core.maintain.commit_history import (
    GitHistory,
    HistoryUnreadableError,
    MappedHistory,
)
from exmergo_dex_core.maintain.incidents import fold_incidents
from exmergo_dex_core.maintain.observations import (
    UNKNOWN,
    Observation,
    RunRef,
    Signal,
    Subject,
    normalize_commit,
    resolve_commit,
)
from exmergo_dex_core.maintain.timeline import (
    Break,
    CommitRange,
    RunPoint,
    build_timeline,
    choose_heartbeat,
    commit_range,
    subject_history,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
TEST = "test.shop.unique_orders_order_id.abc123"
SUBJECT = Subject.of(TEST)
ELEMENTARY = "elementary"


def _sha(letter: str) -> str:
    return (letter * 40)[:40]


# A -- A2 -- B -- C      (main: B breaks the test, C fixes it)
#  \
#   D                    (a branch diverged from A)
#   \
#    E                   (a second branch diverged from A)
# D, E -- M -- F         (M merges both branches; F builds on it)
A, A2, B, C = _sha("a"), _sha("1"), _sha("b"), _sha("c")
D, E, M, F = _sha("d"), _sha("e"), _sha("2"), _sha("f")
GRAPH = MappedHistory(
    {
        A: (),
        A2: (A,),
        B: (A2,),
        C: (B,),
        D: (A,),
        E: (A,),
        M: (D, E),
        F: (M,),
    }
)


def _run(n: int, commit: str | None, **kwargs) -> RunRef:
    return RunRef(
        run=f"inv-{n}",
        sequence=n,
        at=T0 + timedelta(hours=n),
        commit=commit,
        **kwargs,
    )


def _obs(n: int, outcome: str, commit: str | None = None) -> Observation:
    return Observation(
        subject=SUBJECT,
        test=TEST,
        outcome=outcome,
        source=ELEMENTARY,
        run=f"inv-{n}",
        sequence=n,
        at=T0 + timedelta(hours=n),
        commit=commit,
    )


def _series(
    deploys: list[tuple[str | None, str]],
    *,
    history=GRAPH,
    orchestrator: list[str | None] | None = None,
) -> tuple[Break, ...]:
    """Runs 1..n deploying each commit with its outcome; the breaks."""

    runs = [
        _run(
            n,
            commit,
            orchestrator_commit=None if orchestrator is None else orchestrator[n - 1],
        )
        for n, (commit, _) in enumerate(deploys, start=1)
    ]
    observations = [_obs(n, outcome) for n, (_, outcome) in enumerate(deploys, start=1)]
    timeline = build_timeline(
        ELEMENTARY,
        choose_heartbeat(invocation_upload=True, invocations=runs),
        observations,
    )
    return subject_history(
        timeline, SUBJECT, Signal(), observations, history=history
    ).breaks


def _range(found: tuple[Break, ...], index: int = 0) -> CommitRange:
    assert found[index].commits is not None
    return found[index].commits  # type: ignore[return-value]


# -- runs and commits disagree -------------------------------------------------


def test_a_rollback_order_reads_the_range_from_history_not_from_run_order() -> None:
    # Deployed A, C, B: C (which descends from B) ran before B.
    found = _range(_series([(A, "good"), (C, "good"), (B, "bad")]))
    assert found.status == "ordered"
    assert found.through.commit == B
    assert found.last_good_run.commit == C  # type: ignore[union-attr]
    assert [ref.commit for ref in found.after] == [A]
    assert found.commits == (B, A2)
    assert C not in found.commits
    assert found.run_order == "disagrees"


def test_the_same_series_in_commit_order_gives_the_same_range() -> None:
    rollback = _range(_series([(A, "good"), (C, "good"), (B, "bad")]))
    in_order = _range(_series([(A, "good"), (B, "bad"), (C, "good")]))
    assert in_order.status == "ordered"
    assert in_order.commits == rollback.commits == (B, A2)
    assert [ref.commit for ref in in_order.after] == [A]
    assert in_order.run_order == "agrees"


def test_the_break_keeps_its_runs_and_adds_the_commits() -> None:
    found = _series([(A, "good"), (C, "good"), (B, "bad")])[0]
    # The run axis is unchanged: after run 2 through run 3.
    assert found.after.run == "inv-2"  # type: ignore[union-attr]
    assert found.through.run == "inv-3"


def test_a_good_run_whose_commit_descends_from_the_failure_alone_bounds_nothing() -> (
    None
):
    found = _range(_series([(C, "good"), (B, "bad")]))
    assert found.status == "unknown"
    assert found.reason == "no_good_ancestor"
    assert found.commits == ()


# -- where a run's commit comes from -------------------------------------------


def test_a_null_git_sha_reads_the_orchestrators_commit() -> None:
    found = _range(
        _series([(None, "good"), (None, "bad")], orchestrator=[A, B]),
    )
    assert found.status == "ordered"
    assert found.through.basis == "orchestrator"
    assert found.after[0].basis == "orchestrator"
    assert found.commits == (B, A2)


def test_with_neither_commit_the_range_is_unknown() -> None:
    found = _range(_series([(None, "good"), (None, "bad")], orchestrator=[A, None]))
    assert found.status == "unknown"
    assert found.reason == "commit_unknown"
    assert found.side == "through"
    assert found.through.commit == UNKNOWN
    assert found.through.basis == "unknown"
    assert found.commits == ()


def test_the_invocations_git_sha_comes_before_the_orchestrators() -> None:
    assert resolve_commit(B, C) == (B, "git_sha")
    assert resolve_commit(None, C) == (C, "orchestrator")
    assert resolve_commit(None, None) == (UNKNOWN, "unknown")
    found = _range(_series([(A, "good"), (B, "bad")], orchestrator=[D, E]))
    assert found.through.commit == B
    assert found.through.basis == "git_sha"
    assert found.status == "ordered"


def test_an_observations_git_sha_replaces_an_orchestrator_commit() -> None:
    runs = [_run(1, None, orchestrator_commit=D)]
    timeline = build_timeline(
        ELEMENTARY,
        choose_heartbeat(invocation_upload=True, invocations=runs),
        [_obs(1, "good", commit=A)],
    )
    entry = next(e for e in timeline.entries if e.kind == "run")
    assert (entry.commit, entry.commit_basis) == (A, "git_sha")


def test_the_orchestrator_heartbeat_carries_the_orchestrators_commit() -> None:
    runs = [_run(1, None, orchestrator_commit=A), _run(2, None, orchestrator_commit=B)]
    heartbeat = choose_heartbeat(invocation_upload=False, orchestrator=runs)
    observations = [_obs(1, "good"), _obs(2, "bad")]
    timeline = build_timeline(ELEMENTARY, heartbeat, observations)
    assert [e.commit_basis for e in timeline.entries if e.kind == "run"] == [
        "orchestrator",
        "orchestrator",
    ]
    found = subject_history(timeline, SUBJECT, Signal(), observations, history=GRAPH)
    assert found.breaks[0].commits.through.basis == "orchestrator"  # type: ignore[union-attr]


# -- diverged branches ---------------------------------------------------------


def test_two_commits_on_diverged_branches_are_not_ordered() -> None:
    found = _range(_series([(D, "good"), (E, "bad")]))
    assert found.status == "not_ordered"
    assert found.reason == "diverged"
    assert found.commits == ()
    assert found.count is None
    assert found.through.commit == E
    assert found.last_good_run.commit == D  # type: ignore[union-attr]


def test_an_ancestor_and_its_descendant_are_ordered() -> None:
    found = _range(_series([(A, "good"), (E, "bad")]))
    assert found.status == "ordered"
    assert found.commits == (E,)


def test_diverged_is_not_rescued_by_an_older_good_ancestor() -> None:
    # A passed earlier and is an ancestor of E, but the last pass (D) is on
    # another branch: which side changed is not known, so no range is guessed.
    found = _range(_series([(A, "good"), (D, "good"), (E, "bad")]))
    assert found.status == "not_ordered"


def test_passes_on_two_merged_branches_bound_the_range_together() -> None:
    found = _range(_series([(D, "good"), (E, "good"), (F, "bad")]))
    assert found.status == "ordered"
    assert sorted(ref.commit for ref in found.after) == sorted([D, E])
    assert found.commits == (F, M)
    assert found.count == 2


# -- other ends of the range ---------------------------------------------------


def test_a_commit_that_passed_before_fails_with_no_commit_in_range() -> None:
    found = _range(_series([(B, "good"), (B, "bad")]))
    assert found.status == "same_commit"
    assert found.count == 0
    assert found.commits == ()
    assert found.run_order == "agrees"


def test_a_failing_commit_that_passed_in_an_earlier_run_is_the_same_commit() -> None:
    # B passed in run 1, A passed in run 2 (a rollback), B fails in run 3.
    found = _range(_series([(B, "good"), (A, "good"), (B, "bad")]))
    assert found.status == "same_commit"
    assert found.after[0].run == "inv-1"
    assert found.run_order == "disagrees"


def test_no_prior_pass_leaves_the_range_unknown() -> None:
    found = _range(_series([(B, "bad")]))
    assert found.status == "unknown"
    assert found.reason == "no_prior_pass"
    assert found.last_good_run == UNKNOWN


def test_without_a_history_the_break_carries_no_range() -> None:
    assert _series([(A, "good"), (B, "bad")], history=None)[0].commits is None


def test_a_commit_the_history_lacks_is_named() -> None:
    absent = _sha("9")
    found = _range(_series([(A, "good"), (absent, "bad")]))
    assert (found.status, found.reason, found.side) == (
        "unknown",
        "commit_not_in_history",
        "through",
    )
    found = _range(_series([(absent, "good"), (B, "bad")]))
    assert (found.reason, found.side) == ("commit_not_in_history", "after")


def test_a_good_run_with_no_commit_is_counted_as_unplaced() -> None:
    found = _range(_series([(A, "good"), (None, "good"), (A2, "good"), (B, "bad")]))
    assert found.status == "ordered"
    assert found.commits == (B,)
    assert found.unplaced_good_runs == 1


def test_a_long_range_is_capped_and_counts_what_it_cut() -> None:
    through = RunPoint(run="inv-2", sequence=2, commit=F, commit_basis="git_sha")
    good = RunPoint(run="inv-1", sequence=1, commit=A, commit_basis="git_sha")
    found = commit_range(through, good, [good], GRAPH, limit=2)
    assert found.count == 4  # F, M, D, E
    assert len(found.commits) == 2
    assert found.elided == 2


def test_an_abbreviated_commit_resolves_to_its_full_name() -> None:
    found = _range(_series([(A[:7], "good"), (B[:7], "bad")]))
    assert found.status == "ordered"
    assert found.commits == (B, A2)
    assert found.through.commit == B[:7]


class _Broken:
    kind = "git"

    def resolve(self, commit: str) -> str | None:
        raise HistoryUnreadableError("no repository")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        raise HistoryUnreadableError("no repository")

    def independent(self, commits):
        raise HistoryUnreadableError("no repository")

    def between(self, bad, goods, limit):
        raise HistoryUnreadableError("no repository")


def test_an_unreadable_history_is_not_a_missing_commit() -> None:
    found = _range(_series([(A, "good"), (B, "bad")], history=_Broken()))
    assert (found.status, found.reason) == ("unknown", "history_unreadable")


# -- no value crosses ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["main", "--output=/tmp/x", "HEAD~1", "a" * 65, "", "abc", "deadbeef\n--all"],
)
def test_a_commit_that_is_not_an_object_name_is_refused(text: str) -> None:
    with pytest.raises(ValidationError):
        RunRef(run="inv-1", sequence=1, commit=text)
    with pytest.raises(ValidationError):
        RunRef(run="inv-1", sequence=1, orchestrator_commit=text)
    assert normalize_commit(text) is None


def test_normalize_commit_keeps_an_object_name_lowercased() -> None:
    assert normalize_commit(" DEADBEEF ") == "deadbeef"
    assert normalize_commit(None) is None
    assert normalize_commit(1234) is None


def test_a_history_reader_refuses_what_is_not_an_object_name() -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        GRAPH.resolve("--all")


def test_the_range_serializes_to_ids_statuses_and_commits_only() -> None:
    found = _range(_series([(A, "good"), (C, "good"), (B, "bad")]))
    payload = json.loads(found.model_dump_json())
    assert set(payload) == {
        "status",
        "reason",
        "side",
        "history",
        "through",
        "last_good_run",
        "after",
        "run_order",
        "commits",
        "count",
        "elided",
        "unplaced_good_runs",
    }
    assert set(payload["through"]) == {"commit", "run", "sequence", "basis"}


# -- incidents -----------------------------------------------------------------


def test_an_incidents_onset_carries_its_commit_range() -> None:
    runs = [_run(1, A), _run(2, C), _run(3, B)]
    observations = [_obs(1, "good"), _obs(2, "good"), _obs(3, "bad")]
    timeline = build_timeline(
        ELEMENTARY,
        choose_heartbeat(invocation_upload=True, invocations=runs),
        observations,
    )
    fold = fold_incidents(observations, timelines={ELEMENTARY: timeline}, history=GRAPH)
    onset = fold.incidents[0].onset
    assert onset is not None and onset.commits is not None
    assert onset.commits.commits == (B, A2)
    assert onset.commits.run_order == "disagrees"
    without = fold_incidents(observations, timelines={ELEMENTARY: timeline})
    assert without.incidents[0].onset.commits is None  # type: ignore[union-attr]


def test_an_incident_keeps_the_orchestrators_commit_basis() -> None:
    runs = [_run(1, None, orchestrator_commit=A), _run(2, None, orchestrator_commit=B)]
    observations = [_obs(1, "good"), _obs(2, "bad")]
    timeline = build_timeline(
        ELEMENTARY,
        choose_heartbeat(invocation_upload=False, orchestrator=runs),
        observations,
    )
    fold = fold_incidents(observations, timelines={ELEMENTARY: timeline}, history=GRAPH)
    onset = fold.incidents[0].onset
    assert onset is not None and onset.commits is not None
    assert onset.commits.through.basis == "orchestrator"
    assert onset.commits.status == "ordered"


# -- a real repository ---------------------------------------------------------

_GIT = shutil.which("git")


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "dex",
        "GIT_AUTHOR_EMAIL": "dex@example.invalid",
        "GIT_COMMITTER_NAME": "dex",
        "GIT_COMMITTER_EMAIL": "dex@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(root),
    }
    done = subprocess.run(  # noqa: S603
        [_GIT or "git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return done.stdout.strip()


def _commit(root: Path, name: str) -> str:
    (root / f"{name}.sql").write_text(f"select 1 as {name}\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", name)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, str]:
    if _GIT is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    shas = {"a": _commit(root, "a"), "a2": _commit(root, "a2")}
    shas["b"] = _commit(root, "b")
    shas["c"] = _commit(root, "c")
    _git(root, "checkout", "-q", "-b", "side", shas["a"])
    shas["d"] = _commit(root, "d")
    shas["root"] = str(root)
    return shas


def test_git_reads_a_rollback_range_from_history(repo: dict[str, str]) -> None:
    git = GitHistory(repo["root"])
    found = _range(
        _series(
            [(repo["a"], "good"), (repo["c"], "good"), (repo["b"], "bad")],
            history=git,
        )
    )
    assert found.history == "git"
    assert found.status == "ordered"
    assert found.commits == (repo["b"], repo["a2"])
    assert repo["c"] not in found.commits
    assert found.run_order == "disagrees"


def test_git_reports_diverged_branches_as_not_ordered(repo: dict[str, str]) -> None:
    git = GitHistory(repo["root"])
    found = _range(_series([(repo["d"], "good"), (repo["b"], "bad")], history=git))
    assert (found.status, found.reason) == ("not_ordered", "diverged")


def test_git_resolves_an_abbreviation_and_names_an_absent_commit(
    repo: dict[str, str],
) -> None:
    git = GitHistory(repo["root"])
    assert git.resolve(repo["b"][:10]) == repo["b"]
    assert git.resolve("0" * 40) is None
    found = _range(_series([(repo["a"], "good"), ("0" * 40, "bad")], history=git))
    assert found.reason == "commit_not_in_history"


def test_git_outside_a_repository_is_unreadable(tmp_path: Path) -> None:
    if _GIT is None:
        pytest.skip("git is not installed")
    git = GitHistory(tmp_path / "nowhere")
    with pytest.raises(HistoryUnreadableError):
        git.resolve("a" * 40)
    found = _range(_series([(A, "good"), (B, "bad")], history=git))
    assert found.reason == "history_unreadable"


def _shallow_clone(repo: dict[str, str], tmp_path: Path, depth: int) -> Path:
    clone = tmp_path / f"shallow-{depth}"
    subprocess.run(  # noqa: S603
        [
            _GIT or "git",
            "clone",
            "-q",
            "--no-single-branch",
            f"--depth={depth}",
            f"file://{repo['root']}",
            str(clone),
        ],
        check=True,
        capture_output=True,
    )
    return clone


def test_a_shallow_clone_never_reads_a_cut_ancestry_as_diverged(
    repo: dict[str, str], tmp_path: Path
) -> None:
    # Depth 2 keeps C and B on main, with B's parents cut. A is still held
    # (the `other` branch reaches it), so the full history says A..B is
    # ordered, but the clone cannot show it.
    git = GitHistory(_shallow_clone(repo, tmp_path, 2))
    assert git.shallow
    assert git.resolve(repo["a"]) == repo["a"]
    found = _range(_series([(repo["a"], "good"), (repo["c"], "bad")], history=git))
    assert (found.status, found.reason) == ("unknown", "history_shallow")
    full = _range(
        _series(
            [(repo["a"], "good"), (repo["c"], "bad")],
            history=GitHistory(repo["root"]),
        )
    )
    assert full.status == "ordered"


def test_a_shallow_clone_still_reads_a_range_inside_what_it_holds(
    repo: dict[str, str], tmp_path: Path
) -> None:
    git = GitHistory(_shallow_clone(repo, tmp_path, 2))
    found = _range(_series([(repo["b"], "good"), (repo["c"], "bad")], history=git))
    assert found.status == "ordered"
    assert found.commits == (repo["c"],)
    assert not GitHistory(repo["root"]).shallow


def test_git_answers_are_kept_and_an_unreadable_history_fails_fast(
    repo: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    real = subprocess.run

    def counting(argv, *args, **kwargs):
        calls.append(tuple(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting)
    git = GitHistory(repo["root"])
    for _ in range(3):
        git.resolve(repo["b"])
        git.is_ancestor(repo["a"], repo["b"])
    assert len(calls) == 3  # the repository check, one resolve, one ancestry

    calls.clear()
    nowhere = GitHistory(tmp_path / "nowhere")
    for _ in range(3):
        with pytest.raises(HistoryUnreadableError):
            nowhere.resolve(A)
    assert len(calls) == 1


def test_a_cached_answer_does_not_outlive_an_unreachable_git(
    repo: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitHistory(repo["root"])
    assert git.resolve(repo["b"]) == repo["b"]
    assert git.is_ancestor(repo["a"], repo["b"])

    def unreachable(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", unreachable)
    with pytest.raises(HistoryUnreadableError):
        git.between(repo["b"], [repo["a"]], 10)
    for call in (
        lambda: git.resolve(repo["b"]),
        lambda: git.is_ancestor(repo["a"], repo["b"]),
        lambda: git.independent([repo["a"]]),
        lambda: git.shallow,
    ):
        with pytest.raises(HistoryUnreadableError):
            call()


def test_one_failing_command_does_not_mark_the_history_unreadable(
    repo: dict[str, str],
) -> None:
    git = GitHistory(repo["root"])
    with pytest.raises(HistoryUnreadableError):
        git.between("0" * 40, [], 10)  # an object the repository lacks
    assert git.resolve(repo["b"]) == repo["b"]
